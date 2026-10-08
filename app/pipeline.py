"""回填、增量和每天对账共用一条流水线。

arXiv 用提交日期游标向前走。会议 JSON 和 RSS 是快照，对账时整份重读。
awesome 用 README 哈希判断有没有新条目。OpenReview 连续失败会暂停。
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote

import httpx

from app.config import AppConfig, load_config
from app.db import Database
from app.matching import enabled_labs
from app.models import Candidate
from app.selector import select
from app.sources.arxiv_source import arxiv_query, candidates_from_arxiv, strong_title_clause
from app.sources.awesome import parse_awesome_markdown
from app.sources.journals import candidates_from_crossref
from app.sources.openalex_source import candidates_from_openalex
from app.sources.openreview_source import (
    candidates_from_openreview,
    is_challenge,
    next_failure_state,
    openreview_is_paused,
)
from app.sources.programs import candidates_from_program_html
from app.sources.rss import candidates_from_rss
from app.sources.virtual_json import candidates_from_virtual
from app.textutil import alias_matches, normalize_openalex_id

logger = logging.getLogger(__name__)

USER_AGENT = "article-radar/0.1"


@dataclass
class SyncContext:
    mode: str
    config: AppConfig
    db: Database
    client: httpx.Client
    venue: str = ""
    year: int | None = None
    notes: list[str] = field(default_factory=list)
    affiliation_lookups: int = 25

    def note(self, message: str) -> None:
        logger.info(message)
        self.notes.append(message)


def run_sync(
    mode: str = "incremental",
    *,
    only: list[str] | None = None,
    venue: str = "",
    year: int | None = None,
    config: AppConfig | None = None,
    database: Database | None = None,
) -> str:
    """执行一轮同步。mode 取 incremental、reconcile 或 backfill。"""
    config = config or load_config()
    own_db = database is None
    db = database or Database(config.database)
    run_id = db.start_run(mode)
    selected = {item.strip() for item in only or [] if item.strip()}
    headers = {"User-Agent": f"{USER_AGENT} (mailto:{config.mailto})"}
    try:
        with httpx.Client(headers=headers, follow_redirects=True, timeout=60) as client:
            ctx = SyncContext(
                mode=mode,
                config=config,
                db=db,
                client=client,
                venue=venue,
                year=year,
            )
            steps = (
                ("virtual", sync_virtual),
                ("rss", sync_rss),
                ("awesome", sync_awesome),
                ("arxiv", sync_arxiv),
                ("openalex", sync_openalex),
                ("journal", sync_journals),
                ("openreview", sync_openreview),
                ("program", sync_programs),
                ("s2", enrich_abstracts),
            )
            for name, step in steps:
                if selected and name not in selected:
                    continue
                # 会议快照和程序页每天对账或回填时重读，不放进 6 小时增量。
                if mode == "incremental" and name in {"virtual", "rss", "program"}:
                    continue
                try:
                    step(ctx)
                except Exception as exc:
                    logger.exception("源 %s 失败", name)
                    ctx.note(f"{name} 失败：{exc}")
                    db.save_cursor(name, status="error", message=str(exc), last_error=str(exc))
        status = "partial" if any("失败" in item for item in ctx.notes) else "ok"
        message = "\n".join(ctx.notes) or "没有新的变更"
        db.finish_run(run_id, status, message)
        return message
    except Exception as exc:
        db.finish_run(run_id, "error", str(exc))
        raise
    finally:
        if own_db:
            db.close()


def ingest_candidates(db: Database, config: AppConfig, candidates: list[Candidate]) -> dict[str, int]:
    """选择并合并。未命中任何通道的候选不会写入 papers。"""
    counts = {"inserted": 0, "updated": 0, "dropped": 0}
    for candidate in candidates:
        if _outside_window(candidate, config):
            counts["dropped"] += 1
            continue
        decision = select(candidate, config)
        if not decision.keep:
            counts["dropped"] += 1
            continue
        action = db.upsert(candidate, decision)
        counts[action] = counts.get(action, 0) + 1
    return counts


def sync_virtual(ctx: SyncContext) -> None:
    for site in ctx.config.virtual_sites:
        if ctx.venue and site.venue.lower() != ctx.venue.lower():
            continue
        if ctx.year and site.year != ctx.year:
            continue
        status, body = _get(ctx, site.url, timeout=120, cache=False)
        source = f"virtual:{site.venue}:{site.year}"
        if status != 200:
            ctx.db.save_cursor(source, status="error", message=f"HTTP {status}", last_error=body[:300])
            ctx.note(f"{site.venue} {site.year} HTTP {status}")
            continue
        payload = json.loads(body)
        candidates = candidates_from_virtual(payload, site.venue, site.year, site.url)
        counts = ingest_candidates(ctx.db, ctx.config, candidates)
        message = f"看到 {len(candidates)} 篇，新增 {counts['inserted']}，更新 {counts['updated']}，丢弃 {counts['dropped']}"
        ctx.db.save_cursor(source, status="ok", message=message, success=True)
        ctx.note(f"{site.venue} {site.year}：{message}")


def sync_rss(ctx: SyncContext) -> None:
    for feed in ctx.config.rss_feeds:
        if ctx.year and feed.year != ctx.year:
            continue
        status, body = _get(ctx, feed.url, cache=False)
        source = f"rss:{feed.year}"
        if status != 200:
            ctx.db.save_cursor(source, status="error", message=f"HTTP {status}", last_error=body[:300])
            ctx.note(f"RSS {feed.year} HTTP {status}")
            continue
        candidates = candidates_from_rss(body, feed.year, feed.url, feed.default_presentation)
        counts = ingest_candidates(ctx.db, ctx.config, candidates)
        message = f"录用 {len(candidates)} 篇，新增 {counts['inserted']}，更新 {counts['updated']}"
        ctx.db.save_cursor(source, status="ok", message=message, success=True)
        ctx.note(f"RSS {feed.year}：{message}")


def sync_awesome(ctx: SyncContext) -> None:
    for repo in ctx.config.awesome_repos:
        markdown, source_url = _readme(ctx, repo)
        source = f"awesome:{repo}"
        if markdown is None:
            ctx.note(f"{repo} 没有读到 README")
            continue
        digest = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
        current = ctx.db.get_cursor(source)
        if ctx.mode == "incremental" and current and current["content_hash"] == digest:
            ctx.note(f"{repo} README 没有变化")
            continue
        candidates = parse_awesome_markdown(markdown, repo, source_url)
        counts = ingest_candidates(ctx.db, ctx.config, candidates)
        message = f"解析 {len(candidates)} 条，新增 {counts['inserted']}，更新 {counts['updated']}，丢弃 {counts['dropped']}"
        ctx.db.save_cursor(source, content_hash=digest, status="ok", message=message, success=True)
        ctx.note(f"{repo}：{message}")


def sync_arxiv(ctx: SyncContext) -> None:
    if ctx.mode == "reconcile":
        start = _lookback_start(ctx)
        _scan_arxiv(ctx, "arxiv:reconcile", "cs.RO", start, ctx.config.window_end, None, update_cursor=False, max_pages=3)
        _scan_arxiv(
            ctx,
            "arxiv:strong:reconcile",
            None,
            start,
            ctx.config.window_end,
            _other_category_query(ctx),
            update_cursor=False,
            max_pages=1,
        )
        return
    max_pages = None if ctx.mode == "backfill" else ctx.config.arxiv_pages_per_incremental
    _scan_arxiv(
        ctx,
        "arxiv:cs.RO",
        "cs.RO",
        ctx.config.window_start,
        ctx.config.window_end,
        None,
        update_cursor=True,
        max_pages=max_pages,
    )
    _scan_arxiv(
        ctx,
        "arxiv:strong",
        None,
        ctx.config.window_start,
        ctx.config.window_end,
        _other_category_query(ctx),
        update_cursor=True,
        max_pages=1 if ctx.mode == "incremental" else 5,
    )


def sync_openalex(ctx: SyncContext) -> None:
    pages = 20 if ctx.mode == "backfill" else 1
    for lab in enabled_labs(ctx.config):
        institution_ids = lab.openalex_ids or _resolve_institutions(ctx, lab)
        if not institution_ids:
            ctx.note(f"OpenAlex 没有解析到 {lab.label} 的机构 id")
            continue
        for institution_id in institution_ids:
            _scan_openalex_institution(ctx, lab.id, institution_id, pages)


def sync_journals(ctx: SyncContext) -> None:
    pages = 10 if ctx.mode == "backfill" else 1
    for journal in ctx.config.journals:
        source = f"journal:{journal.issn}"
        cursor_row = ctx.db.get_cursor(source)
        cursor = "*"
        if ctx.mode == "backfill" and cursor_row and cursor_row["cursor"]:
            cursor = cursor_row["cursor"]
        total_inserted = 0
        for _ in range(pages):
            url = (
                f"https://api.crossref.org/journals/{journal.issn}/works"
                f"?filter=from-pub-date:{ctx.config.window_start},until-pub-date:{ctx.config.window_end}"
                f"&rows=100&cursor={quote(cursor, safe='*')}&mailto={quote(ctx.config.mailto)}"
            )
            status, body = _get(ctx, url, ttl=6 * 3600)
            if status != 200:
                ctx.db.save_cursor(source, status="error", message=f"HTTP {status}", last_error=body[:300])
                ctx.note(f"{journal.name} HTTP {status}")
                break
            payload = json.loads(body).get("message") or {}
            candidates, next_cursor = candidates_from_crossref(payload, journal.name)
            for candidate in candidates:
                _attach_openalex_affiliations(ctx, candidate)
            counts = ingest_candidates(ctx.db, ctx.config, candidates)
            total_inserted += counts["inserted"]
            cursor = next_cursor or ""
            ctx.db.save_cursor(
                source,
                cursor=cursor,
                status="ok",
                message=f"本页新增 {counts['inserted']}，丢弃 {counts['dropped']}",
                success=True,
            )
            if not next_cursor or not candidates:
                break
            time.sleep(0.2)
        ctx.note(f"{journal.name} 本轮新增 {total_inserted}")


def sync_openreview(ctx: SyncContext) -> None:
    source = "openreview"
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    row = ctx.db.get_cursor(source)
    if openreview_is_paused(row, now):
        ctx.note(f"OpenReview 暂停至 {row['disabled_until']}")
        return
    failures = 0
    for venue in ctx.config.openreview_venues:
        if ctx.venue and venue.venue.lower() != ctx.venue.lower():
            continue
        if ctx.year and venue.year != ctx.year:
            continue
        url = (
            "https://api2.openreview.net/notes?content.venueid="
            f"{quote(venue.url, safe='')}&limit=200"
        )
        status, body = _get(ctx, url, cache=False)
        if is_challenge(status, body) or status != 200:
            error = "ChallengeRequiredError" if is_challenge(status, body) else f"HTTP {status}"
            count, disabled, state = next_failure_state(
                int(row["consecutive_failures"]) if row else failures,
                ctx.config.openreview_failure_limit,
                ctx.config.openreview_pause_hours,
                datetime.now(timezone.utc),
            )
            ctx.db.save_cursor(
                source,
                status=state,
                message=error if not disabled else f"连续失败 {count} 次，暂停至 {disabled}",
                failures=count,
                last_error=error,
                disabled_until=disabled,
            )
            ctx.note(f"OpenReview {venue.venue} {venue.year}：{error}")
            return
        payload = json.loads(body)
        candidates = candidates_from_openreview(payload, venue.venue, venue.year)
        counts = ingest_candidates(ctx.db, ctx.config, candidates)
        ctx.note(f"OpenReview {venue.venue} {venue.year}：新增 {counts['inserted']}，丢弃 {counts['dropped']}")
        failures = 0
        row = None
    ctx.db.save_cursor(source, status="ok", message="本轮已完成", success=True)


def sync_programs(ctx: SyncContext) -> None:
    pages = list(ctx.config.corl_pages) + list(ctx.config.program_pages)
    for page in pages:
        if ctx.venue and page.venue.lower() != ctx.venue.lower():
            continue
        if ctx.year and page.year != ctx.year:
            continue
        source = f"program:{page.venue}:{page.year}:{page.url}"
        status, body = _get(ctx, page.url, cache=False)
        if status != 200:
            ctx.db.save_cursor(source, status="error", message=f"HTTP {status}", last_error=body[:200])
            ctx.note(f"{page.venue} {page.year} 程序页 HTTP {status}")
            continue
        candidates = candidates_from_program_html(body, page.venue, page.year, page.url)
        if not candidates:
            ctx.db.save_cursor(source, status="ok", message="页面没有明确的 oral / spotlight / award 链接", success=True)
            ctx.note(f"{page.venue} {page.year}：没有明确的会议信号")
            continue
        counts = ingest_candidates(ctx.db, ctx.config, candidates)
        message = f"明确信号 {len(candidates)} 条，新增 {counts['inserted']}"
        ctx.db.save_cursor(source, status="ok", message=message, success=True)
        ctx.note(f"{page.venue} {page.year}：{message}")


def enrich_abstracts(ctx: SyncContext) -> None:
    """Semantic Scholar 只补缺失摘要，不用它判断单位。遇到 429 就停。"""
    rows = ctx.db.papers_missing_abstract(12)
    if not rows:
        return
    headers = {}
    import os

    api_key = os.environ.get("SEMANTIC_SCHOLAR_API_KEY")
    if api_key:
        headers["x-api-key"] = api_key
    filled = 0
    for row in rows:
        if row["arxiv_id"]:
            paper_ref = f"ARXIV:{row['arxiv_id']}"
        else:
            paper_ref = f"DOI:{row['doi']}"
        url = (
            "https://api.semanticscholar.org/graph/v1/paper/"
            f"{quote(paper_ref, safe=':')}?fields=abstract,paperId"
        )
        status, body = _get(ctx, url, headers=headers, ttl=7 * 24 * 3600)
        if status == 429:
            ctx.note("Semantic Scholar 限流，本轮停止补摘要")
            ctx.db.save_cursor("semantic-scholar", status="error", message="429", last_error="429")
            return
        if status == 200:
            payload = json.loads(body)
            abstract = (payload.get("abstract") or "").strip()
            if abstract:
                ctx.db.set_abstract(int(row["id"]), abstract, payload.get("paperId"))
                filled += 1
        time.sleep(1.1)
    ctx.db.save_cursor("semantic-scholar", status="ok", message=f"补上 {filled} 篇摘要", success=True)
    if filled:
        ctx.note(f"Semantic Scholar 补上 {filled} 篇摘要")


def _scan_arxiv(
    ctx: SyncContext,
    source: str,
    category: str | None,
    default_start: str,
    end: str,
    raw_query: str | None,
    *,
    update_cursor: bool,
    max_pages: int | None,
) -> None:
    row = ctx.db.get_cursor(source) if update_cursor else None
    query_start, offset = _parse_arxiv_cursor(row["cursor"] if row else None, default_start)
    pages = 0
    limit = max_pages if max_pages is not None else 40
    while pages < limit:
        query = raw_query or arxiv_query(category or "cs.RO", query_start, end)
        if raw_query:
            query = arxiv_query_raw(raw_query, query_start, end)
        url = (
            "https://export.arxiv.org/api/query?search_query="
            f"{quote(query)}&start={offset}&max_results={ctx.config.arxiv_page_size}"
            "&sortBy=submittedDate&sortOrder=ascending"
        )
        status, body = _get(ctx, url, timeout=90, cache=False)
        if status != 200:
            ctx.db.save_cursor(source, status="error", message=f"HTTP {status}", last_error=body[:300])
            ctx.note(f"{source} HTTP {status}")
            return
        if "http://arxiv.org/api/errors" in body and "<entry>" in body and "Error" in body:
            ctx.db.save_cursor(source, status="error", message="arXiv 拒绝了查询", last_error=body[:300])
            ctx.note(f"{source} 查询被 arXiv 拒绝")
            return
        candidates = candidates_from_arxiv(body)
        counts = ingest_candidates(ctx.db, ctx.config, candidates)
        pages += 1
        ctx.note(f"{source} 偏移 {offset}：新增 {counts['inserted']}，丢弃 {counts['dropped']}")
        if len(candidates) < ctx.config.arxiv_page_size:
            if update_cursor and candidates:
                query_start = candidates[-1].published_at or query_start
            if update_cursor:
                ctx.db.save_cursor(
                    source,
                    cursor=f"{query_start}|0",
                    status="ok",
                    message="已追到当前时间窗",
                    success=True,
                )
            return
        offset += len(candidates)
        if update_cursor:
            ctx.db.save_cursor(
                source,
                cursor=f"{query_start}|{offset}",
                status="ok",
                message=f"继续，偏移 {offset}",
                success=True,
            )
        time.sleep(3)
    if update_cursor:
        ctx.db.save_cursor(source, cursor=f"{query_start}|{offset}", status="ok", message="本轮页数已满，下次继续", success=True)


def arxiv_query_raw(extra: str, start: str, end: str) -> str:
    start_token = start.replace("-", "") + "0000"
    end_token = end.replace("-", "") + "2359"
    return f"({extra}) AND submittedDate:[{start_token} TO {end_token}]"


def _other_category_query(ctx: SyncContext) -> str:
    categories = " OR ".join(f"cat:{item}" for item in ctx.config.other_categories)
    phrases = [phrase for topic in ctx.config.topics for phrase in topic.strong_title]
    clause = strong_title_clause(phrases)
    return f"({categories}) ANDNOT cat:{ctx.config.weak_category} AND ({clause})"


def _parse_arxiv_cursor(value: str | None, default_start: str) -> tuple[str, int]:
    if not value or "|" not in value:
        return default_start, 0
    start, offset = value.split("|", 1)
    try:
        return start, int(offset)
    except ValueError:
        return default_start, 0


def _resolve_institutions(ctx: SyncContext, lab) -> list[str]:
    found: list[str] = []
    for name in lab.names:
        url = (
            "https://api.openalex.org/institutions?search="
            f"{quote(name)}&per-page=5&mailto={quote(ctx.config.mailto)}"
        )
        status, body = _get(ctx, url, ttl=7 * 24 * 3600)
        if status != 200:
            continue
        for result in (json.loads(body).get("results") or []):
            display = result.get("display_name") or ""
            if any(alias_matches(display, alias) for alias in lab.names):
                institution_id = normalize_openalex_id(result.get("id"))
                if institution_id and institution_id not in found:
                    found.append(institution_id)
        time.sleep(0.2)
    return found


def _scan_openalex_institution(ctx: SyncContext, lab_id: str, institution_id: str, pages: int) -> None:
    source = f"openalex:{lab_id}:{institution_id}"
    row = ctx.db.get_cursor(source)
    cursor = row["cursor"] if row and row["cursor"] and ctx.mode == "backfill" else "*"
    for _ in range(pages):
        url = (
            "https://api.openalex.org/works?filter="
            f"authorships.institutions.id:{institution_id},"
            f"from_publication_date:{ctx.config.window_start},"
            f"to_publication_date:{ctx.config.window_end}"
            f"&search={quote('robot OR manipulation OR embodied OR dexterous')}"
            f"&per-page=50&cursor={quote(cursor, safe='*')}"
            f"&mailto={quote(ctx.config.mailto)}"
            "&select=id,doi,title,display_name,authorships,publication_date,primary_location,open_access,ids,abstract_inverted_index"
        )
        status, body = _get(ctx, url, ttl=12 * 3600)
        if status != 200:
            ctx.db.save_cursor(source, status="error", message=f"HTTP {status}", last_error=body[:300])
            ctx.note(f"OpenAlex {lab_id} HTTP {status}")
            return
        payload = json.loads(body)
        candidates = candidates_from_openalex(payload.get("results") or [])
        counts = ingest_candidates(ctx.db, ctx.config, candidates)
        cursor = ((payload.get("meta") or {}).get("next_cursor")) or ""
        ctx.db.save_cursor(
            source,
            cursor=cursor,
            status="ok",
            message=f"新增 {counts['inserted']}，丢弃 {counts['dropped']}",
            success=True,
        )
        ctx.note(f"OpenAlex {lab_id}：新增 {counts['inserted']}，丢弃 {counts['dropped']}")
        if not cursor or not candidates:
            return
        time.sleep(0.2)


def _attach_openalex_affiliations(ctx: SyncContext, candidate: Candidate) -> None:
    if candidate.institutions() or not candidate.doi or ctx.affiliation_lookups <= 0:
        return
    ctx.affiliation_lookups -= 1
    url = f"https://api.openalex.org/works/doi:{quote(candidate.doi, safe='/')}?mailto={quote(ctx.config.mailto)}"
    status, body = _get(ctx, url, ttl=14 * 24 * 3600)
    if status != 200:
        return
    works = candidates_from_openalex([json.loads(body)])
    if not works:
        return
    candidate.authors = works[0].authors
    candidate.openalex_id = candidate.openalex_id or works[0].openalex_id
    if not candidate.abstract:
        candidate.abstract = works[0].abstract


def _readme(ctx: SyncContext, repo: str) -> tuple[str | None, str]:
    for branch in ("main", "master"):
        url = f"https://raw.githubusercontent.com/{repo}/{branch}/README.md"
        status, body = _get(ctx, url, ttl=6 * 3600)
        if status == 200 and "404: Not Found" not in body[:200]:
            return body, url
    return None, ""


def _get(
    ctx: SyncContext,
    url: str,
    *,
    timeout: float = 60,
    cache: bool = True,
    ttl: int = 12 * 3600,
    headers: dict | None = None,
) -> tuple[int, str]:
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    if cache:
        cached = ctx.db.cache_get(url, now)
        if cached is not None:
            return int(cached["status"]), cached["body"]
    response = ctx.client.get(url, timeout=timeout, headers=headers)
    body = response.text
    if cache and response.status_code == 200:
        ctx.db.cache_put(url, response.status_code, body, ttl, now)
    return response.status_code, body


def _outside_window(candidate: Candidate, config: AppConfig) -> bool:
    if candidate.kind not in {"arxiv", "journal", "openalex"}:
        return False
    if not candidate.published_at:
        return False
    day = candidate.published_at[:10]
    return day < config.window_start or day > config.window_end


def _lookback_start(ctx: SyncContext) -> str:
    start = date.today() - timedelta(days=ctx.config.reconcile_lookback_days)
    window = date.fromisoformat(ctx.config.window_start)
    if start < window:
        start = window
    return start.isoformat()
