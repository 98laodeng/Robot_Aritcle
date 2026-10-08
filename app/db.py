"""SQLite 存储。

papers 保存当前状态，paper_sources 保存每个来源看到的历史，
paper_selection_reasons 和 paper_topics 只增不减。
外部 id 优先合并；都没有时才用标题 + 第一作者 + 年份差不超过 1。
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from app.models import Author, Candidate, Decision, Institution
from app.presentation import upgrade_presentation
from app.textutil import normalize_doi, normalize_phrase, year_from_date

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    title_norm TEXT NOT NULL,
    abstract TEXT NOT NULL DEFAULT '',
    first_author TEXT NOT NULL DEFAULT '',
    first_author_norm TEXT NOT NULL DEFAULT '',
    venue TEXT NOT NULL DEFAULT '',
    conference_year INTEGER,
    presentation_type TEXT,
    year INTEGER,
    first_published_at TEXT,
    first_seen_at TEXT NOT NULL,
    arxiv_published_at TEXT,
    conference_published_at TEXT,
    journal_published_at TEXT,
    source_updated_at TEXT,
    last_verified_at TEXT,
    arxiv_id TEXT,
    doi TEXT,
    openreview_id TEXT,
    openalex_id TEXT,
    s2_id TEXT,
    url TEXT NOT NULL DEFAULT '',
    pdf_url TEXT NOT NULL DEFAULT '',
    is_read INTEGER NOT NULL DEFAULT 0,
    is_starred INTEGER NOT NULL DEFAULT 0,
    starred_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_sources (
    id INTEGER PRIMARY KEY,
    paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    presentation_type TEXT,
    source_first_seen_at TEXT NOT NULL,
    source_updated_at TEXT,
    last_verified_at TEXT NOT NULL,
    UNIQUE(paper_id, source, source_id)
);

CREATE TABLE IF NOT EXISTS paper_selection_reasons (
    paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (paper_id, reason)
);

CREATE TABLE IF NOT EXISTS paper_topics (
    paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    topic TEXT NOT NULL,
    PRIMARY KEY (paper_id, topic)
);

CREATE TABLE IF NOT EXISTS authors (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    name_norm TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS institutions (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    name_norm TEXT NOT NULL,
    openalex_id TEXT,
    ror TEXT,
    lab_id TEXT
);

CREATE TABLE IF NOT EXISTS paper_authors (
    paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    author_id INTEGER NOT NULL REFERENCES authors(id),
    institution_id INTEGER REFERENCES institutions(id),
    position INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY,
    mode TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    message TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS source_cursors (
    source TEXT PRIMARY KEY,
    cursor TEXT,
    content_hash TEXT,
    last_run_at TEXT,
    last_status TEXT,
    last_message TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_success_at TEXT,
    last_error TEXT,
    disabled_until TEXT
);

CREATE TABLE IF NOT EXISTS http_cache (
    cache_key TEXT PRIMARY KEY,
    fetched_at TEXT NOT NULL,
    status INTEGER NOT NULL,
    body TEXT NOT NULL,
    ttl_seconds INTEGER NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_papers_arxiv
    ON papers(arxiv_id) WHERE arxiv_id IS NOT NULL AND arxiv_id != '';
CREATE UNIQUE INDEX IF NOT EXISTS idx_papers_doi
    ON papers(doi) WHERE doi IS NOT NULL AND doi != '';
CREATE UNIQUE INDEX IF NOT EXISTS idx_papers_openreview
    ON papers(openreview_id) WHERE openreview_id IS NOT NULL AND openreview_id != '';
CREATE UNIQUE INDEX IF NOT EXISTS idx_papers_openalex
    ON papers(openalex_id) WHERE openalex_id IS NOT NULL AND openalex_id != '';
CREATE UNIQUE INDEX IF NOT EXISTS idx_institutions_identity
    ON institutions(name_norm, ifnull(openalex_id, ''), ifnull(ror, ''));

CREATE VIRTUAL TABLE IF NOT EXISTS papers_fts USING fts5(
    title, abstract, content='papers', content_rowid='id'
);
"""

TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS papers_ai AFTER INSERT ON papers BEGIN
    INSERT INTO papers_fts(rowid, title, abstract) VALUES (new.id, new.title, new.abstract);
END;
CREATE TRIGGER IF NOT EXISTS papers_ad AFTER DELETE ON papers BEGIN
    INSERT INTO papers_fts(papers_fts, rowid, title, abstract)
    VALUES ('delete', old.id, old.title, old.abstract);
END;
CREATE TRIGGER IF NOT EXISTS papers_au AFTER UPDATE ON papers BEGIN
    INSERT INTO papers_fts(papers_fts, rowid, title, abstract)
    VALUES ('delete', old.id, old.title, old.abstract);
    INSERT INTO papers_fts(rowid, title, abstract) VALUES (new.id, new.title, new.abstract);
END;
"""

FOCUS_REASONS = (
    "conference_oral",
    "conference_spotlight",
    "conference_highlight",
    "conference_award",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Database:
    def __init__(self, path: Path | str):
        self.path = path
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if str(path) != ":memory:":
            # 同步线程和看板会同时打开数据库，WAL 允许一边写一边读。
            self.conn.execute("PRAGMA journal_mode = WAL")
        self._lock = threading.Lock()
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(SCHEMA)
        self.conn.executescript(TRIGGERS)
        # 已有库是在收藏功能之前建的，CREATE TABLE IF NOT EXISTS 不会补新列。
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(papers)")}
        if "is_starred" not in columns:
            self.conn.execute("ALTER TABLE papers ADD COLUMN is_starred INTEGER NOT NULL DEFAULT 0")
        if "starred_at" not in columns:
            self.conn.execute("ALTER TABLE papers ADD COLUMN starred_at TEXT")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def upsert(self, candidate: Candidate, decision: Decision, now: str | None = None) -> str:
        """写入或合并一篇已决定收录的论文。理由只插入，不删除。"""
        if not decision.keep:
            return "skipped"
        moment = now or utc_now()
        title_norm = normalize_phrase(candidate.title)
        author_norm = normalize_phrase(candidate.first_author)
        year = candidate.conference_year or year_from_date(candidate.published_at)
        with self._lock:
            existing = self._find_existing(candidate, title_norm, author_norm, year)
            if existing is None:
                paper_id = self._insert_paper(candidate, decision, moment, title_norm, author_norm, year)
                action = "inserted"
            else:
                paper_id = int(existing["id"])
                self._merge_paper(existing, candidate, decision, moment, year)
                action = "updated"
            self._add_source(paper_id, candidate, decision, moment)
            for reason in decision.reasons:
                self.conn.execute(
                    """
                    INSERT INTO paper_selection_reasons(paper_id, reason, created_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(paper_id, reason) DO NOTHING
                    """,
                    (paper_id, reason, moment),
                )
            for topic in decision.topics:
                self.conn.execute(
                    """
                    INSERT INTO paper_topics(paper_id, topic) VALUES (?, ?)
                    ON CONFLICT(paper_id, topic) DO NOTHING
                    """,
                    (paper_id, topic),
                )
            if candidate.authors and self._should_replace_authors(paper_id, candidate):
                self._replace_authors(paper_id, candidate.authors)
            self.conn.commit()
            return action

    def _find_existing(
        self,
        candidate: Candidate,
        title_norm: str,
        author_norm: str,
        year: int | None,
    ) -> sqlite3.Row | None:
        # DOI、arXiv、OpenReview、OpenAlex、Semantic Scholar 依次认领同一篇论文。
        identity_fields = (
            ("doi", normalize_doi(candidate.doi)),
            ("arxiv_id", candidate.arxiv_id),
            ("openreview_id", candidate.openreview_id),
            ("openalex_id", candidate.openalex_id),
            ("s2_id", candidate.s2_id),
        )
        for column, value in identity_fields:
            if not value:
                continue
            row = self.conn.execute(
                f"SELECT * FROM papers WHERE {column} = ?",
                (value,),
            ).fetchone()
            if row is not None:
                return row
        # 标题会撞车，所以必须同时有第一作者，并且年份相差不超过 1。
        # 这样 2025 的 arXiv 和 2026 的会议版能合并，单靠标题则不行。
        if not title_norm or not author_norm or year is None:
            return None
        rows = self.conn.execute(
            """
            SELECT * FROM papers
            WHERE title_norm = ? AND first_author_norm = ? AND year IS NOT NULL
            """,
            (title_norm, author_norm),
        ).fetchall()
        for row in rows:
            if abs(int(row["year"]) - year) <= 1:
                return row
        return None

    def _insert_paper(
        self,
        candidate: Candidate,
        decision: Decision,
        moment: str,
        title_norm: str,
        author_norm: str,
        year: int | None,
    ) -> int:
        published = _date_only(candidate.published_at)
        cursor = self.conn.execute(
            """
            INSERT INTO papers (
                title, title_norm, abstract, first_author, first_author_norm,
                venue, conference_year, presentation_type, year,
                first_published_at, first_seen_at, arxiv_published_at,
                conference_published_at, journal_published_at, source_updated_at,
                last_verified_at, arxiv_id, doi, openreview_id, openalex_id, s2_id,
                url, pdf_url, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                candidate.title.strip(),
                title_norm,
                candidate.abstract or "",
                candidate.first_author,
                author_norm,
                candidate.venue,
                candidate.conference_year,
                decision.presentation_type,
                year,
                published,
                moment,
                published if _is_arxiv_source(candidate) else None,
                published if candidate.kind in {"conference", "rss"} else None,
                published if candidate.kind == "journal" else None,
                candidate.source_updated_at,
                moment,
                candidate.arxiv_id,
                normalize_doi(candidate.doi),
                candidate.openreview_id,
                candidate.openalex_id,
                candidate.s2_id,
                candidate.url,
                candidate.pdf_url,
                moment,
                moment,
            ),
        )
        return int(cursor.lastrowid)

    def _merge_paper(
        self,
        existing: sqlite3.Row,
        candidate: Candidate,
        decision: Decision,
        moment: str,
        year: int | None,
    ) -> None:
        published = _date_only(candidate.published_at)
        presentation = upgrade_presentation(existing["presentation_type"], decision.presentation_type)
        venue = existing["venue"]
        if candidate.venue and (not venue or venue.lower() == "arxiv") and candidate.kind in {
            "conference",
            "rss",
            "journal",
        }:
            venue = candidate.venue
        conference_year = existing["conference_year"] or candidate.conference_year
        abstract = existing["abstract"] or ""
        if len(candidate.abstract or "") > len(abstract):
            abstract = candidate.abstract
        self.conn.execute(
            """
            UPDATE papers SET
                abstract = ?,
                venue = ?,
                conference_year = ?,
                presentation_type = ?,
                year = COALESCE(year, ?),
                first_published_at = ?,
                arxiv_published_at = COALESCE(arxiv_published_at, ?),
                conference_published_at = COALESCE(conference_published_at, ?),
                journal_published_at = COALESCE(journal_published_at, ?),
                source_updated_at = ?,
                last_verified_at = ?,
                arxiv_id = COALESCE(arxiv_id, ?),
                doi = COALESCE(doi, ?),
                openreview_id = COALESCE(openreview_id, ?),
                openalex_id = COALESCE(openalex_id, ?),
                s2_id = COALESCE(s2_id, ?),
                url = CASE WHEN url = '' THEN ? ELSE url END,
                pdf_url = CASE WHEN pdf_url = '' THEN ? ELSE pdf_url END,
                first_author = CASE WHEN first_author = '' THEN ? ELSE first_author END,
                first_author_norm = CASE WHEN first_author_norm = '' THEN ? ELSE first_author_norm END,
                updated_at = ?
            WHERE id = ?
            """,
            (
                abstract,
                venue,
                conference_year,
                presentation,
                year,
                _earliest(existing["first_published_at"], published),
                published if _is_arxiv_source(candidate) else None,
                published if candidate.kind in {"conference", "rss"} else None,
                published if candidate.kind == "journal" else None,
                _latest(existing["source_updated_at"], candidate.source_updated_at),
                moment,
                candidate.arxiv_id,
                normalize_doi(candidate.doi),
                candidate.openreview_id,
                candidate.openalex_id,
                candidate.s2_id,
                candidate.url,
                candidate.pdf_url,
                candidate.first_author,
                normalize_phrase(candidate.first_author),
                moment,
                existing["id"],
            ),
        )

    def _add_source(
        self,
        paper_id: int,
        candidate: Candidate,
        decision: Decision,
        moment: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO paper_sources (
                paper_id, source, source_id, source_url, presentation_type,
                source_first_seen_at, source_updated_at, last_verified_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(paper_id, source, source_id) DO UPDATE SET
                source_url = excluded.source_url,
                presentation_type = excluded.presentation_type,
                source_updated_at = excluded.source_updated_at,
                last_verified_at = excluded.last_verified_at
            """,
            (
                paper_id,
                candidate.source,
                candidate.source_id or "",
                candidate.source_url or candidate.url,
                decision.presentation_type,
                moment,
                candidate.source_updated_at,
                moment,
            ),
        )

    def _should_replace_authors(self, paper_id: int, candidate: Candidate) -> bool:
        count = self.conn.execute(
            "SELECT COUNT(*) AS n FROM paper_authors WHERE paper_id = ?",
            (paper_id,),
        ).fetchone()["n"]
        has_institution = any(author.institutions for author in candidate.authors)
        return count == 0 or has_institution

    def _replace_authors(self, paper_id: int, authors: list[Author]) -> None:
        self.conn.execute("DELETE FROM paper_authors WHERE paper_id = ?", (paper_id,))
        for position, author in enumerate(authors):
            author_id = self._author_id(author.name)
            institution = next((item for item in author.institutions if item.lab_id), None)
            if institution is None and author.institutions:
                institution = author.institutions[0]
            institution_id = self._institution_id(institution) if institution else None
            self.conn.execute(
                """
                INSERT INTO paper_authors(paper_id, author_id, institution_id, position)
                VALUES (?, ?, ?, ?)
                """,
                (paper_id, author_id, institution_id, position),
            )

    def _author_id(self, name: str) -> int:
        name_norm = normalize_phrase(name)
        row = self.conn.execute(
            "SELECT id FROM authors WHERE name_norm = ?",
            (name_norm,),
        ).fetchone()
        if row:
            return int(row["id"])
        cursor = self.conn.execute(
            "INSERT INTO authors(name, name_norm) VALUES (?, ?)",
            (name, name_norm),
        )
        return int(cursor.lastrowid)

    def _institution_id(self, institution: Institution) -> int:
        name_norm = normalize_phrase(institution.name)
        openalex_id = institution.openalex_id
        ror = institution.ror
        row = self.conn.execute(
            """
            SELECT id FROM institutions
            WHERE name_norm = ? AND ifnull(openalex_id, '') = ? AND ifnull(ror, '') = ?
            """,
            (name_norm, openalex_id or "", ror or ""),
        ).fetchone()
        # lab_id 由选择器按这一家单位单独判定，合作大学不会继承企业标签。
        lab_id = institution.lab_id
        if row:
            if lab_id:
                self.conn.execute(
                    "UPDATE institutions SET lab_id = COALESCE(lab_id, ?) WHERE id = ?",
                    (lab_id, row["id"]),
                )
            return int(row["id"])
        cursor = self.conn.execute(
            """
            INSERT INTO institutions(name, name_norm, openalex_id, ror, lab_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (institution.name, name_norm, openalex_id, ror, lab_id),
        )
        return int(cursor.lastrowid)

    def get_cursor(self, source: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM source_cursors WHERE source = ?",
            (source,),
        ).fetchone()

    def save_cursor(
        self,
        source: str,
        *,
        cursor: str | None = None,
        content_hash: str | None = None,
        status: str,
        message: str,
        now: str | None = None,
        failures: int | None = None,
        last_error: str | None = None,
        disabled_until: str | None = None,
        success: bool = False,
    ) -> None:
        moment = now or utc_now()
        current = self.get_cursor(source)
        if failures is None:
            failures = int(current["consecutive_failures"]) if current else 0
        if success:
            failures = 0
            disabled_until = None
            last_error = None
        elif disabled_until is None and current is not None:
            disabled_until = current["disabled_until"]
        last_success = moment if success else (current["last_success_at"] if current else None)
        kept_cursor = cursor if cursor is not None else (current["cursor"] if current else None)
        kept_hash = content_hash if content_hash is not None else (current["content_hash"] if current else None)
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO source_cursors (
                    source, cursor, content_hash, last_run_at, last_status, last_message,
                    consecutive_failures, last_success_at, last_error, disabled_until
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source) DO UPDATE SET
                    cursor = excluded.cursor,
                    content_hash = excluded.content_hash,
                    last_run_at = excluded.last_run_at,
                    last_status = excluded.last_status,
                    last_message = excluded.last_message,
                    consecutive_failures = excluded.consecutive_failures,
                    last_success_at = excluded.last_success_at,
                    last_error = excluded.last_error,
                    disabled_until = excluded.disabled_until
                """,
                (
                    source,
                    kept_cursor,
                    kept_hash,
                    moment,
                    status,
                    message[:2000],
                    failures,
                    last_success,
                    last_error,
                    disabled_until,
                ),
            )
            self.conn.commit()

    def start_run(self, mode: str) -> int:
        moment = utc_now()
        cursor = self.conn.execute(
            "INSERT INTO sync_runs(mode, started_at, status) VALUES (?, ?, 'running')",
            (mode, moment),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def finish_run(self, run_id: int, status: str, message: str) -> None:
        self.conn.execute(
            """
            UPDATE sync_runs SET finished_at = ?, status = ?, message = ? WHERE id = ?
            """,
            (utc_now(), status, message[:8000], run_id),
        )
        self.conn.commit()

    def recent_runs(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM sync_runs ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()

    def cursors(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM source_cursors ORDER BY source"
        ).fetchall()

    def cache_get(self, key: str, now: str) -> sqlite3.Row | None:
        row = self.conn.execute(
            "SELECT * FROM http_cache WHERE cache_key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        fetched = datetime.fromisoformat(row["fetched_at"])
        age = datetime.fromisoformat(now) - fetched
        if age.total_seconds() > int(row["ttl_seconds"]):
            return None
        return row

    def cache_put(self, key: str, status: int, body: str, ttl_seconds: int, now: str) -> None:
        if len(body) > 1_500_000:
            return
        self.conn.execute(
            """
            INSERT INTO http_cache(cache_key, fetched_at, status, body, ttl_seconds)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                fetched_at = excluded.fetched_at,
                status = excluded.status,
                body = excluded.body,
                ttl_seconds = excluded.ttl_seconds
            """,
            (key, now, status, body, ttl_seconds),
        )
        self.conn.commit()

    def list_papers(self, filters: dict) -> list[dict]:
        clauses = ["1 = 1"]
        params: list[object] = []
        group = filters.get("group") or ""
        if group == "focus":
            placeholders = ",".join("?" for _ in FOCUS_REASONS)
            clauses.append(
                f"""
                (
                    EXISTS (
                        SELECT 1 FROM paper_selection_reasons r
                        WHERE r.paper_id = papers.id AND r.reason IN ({placeholders})
                    )
                    OR (
                        EXISTS (
                            SELECT 1 FROM paper_selection_reasons r
                            WHERE r.paper_id = papers.id AND r.reason = 'target_lab'
                        )
                        AND EXISTS (
                            SELECT 1 FROM paper_selection_reasons r
                            WHERE r.paper_id = papers.id AND r.reason = 'target_topic'
                        )
                    )
                )
                """
            )
            params.extend(FOCUS_REASONS)
        elif group:
            clauses.append(
                "EXISTS (SELECT 1 FROM paper_topics t WHERE t.paper_id = papers.id AND t.topic = ?)"
            )
            params.append(group)
        if filters.get("venue"):
            clauses.append("venue = ?")
            params.append(filters["venue"])
        if filters.get("year"):
            clauses.append("year = ?")
            params.append(int(filters["year"]))
        if filters.get("presentation"):
            clauses.append("presentation_type = ?")
            params.append(filters["presentation"])
        if filters.get("reason"):
            clauses.append(
                "EXISTS (SELECT 1 FROM paper_selection_reasons r WHERE r.paper_id = papers.id AND r.reason = ?)"
            )
            params.append(filters["reason"])
        if filters.get("lab"):
            clauses.append(
                """
                EXISTS (
                    SELECT 1 FROM paper_authors pa
                    JOIN institutions i ON i.id = pa.institution_id
                    WHERE pa.paper_id = papers.id AND i.lab_id = ?
                )
                """
            )
            params.append(filters["lab"])
        if filters.get("topic"):
            clauses.append(
                "EXISTS (SELECT 1 FROM paper_topics t WHERE t.paper_id = papers.id AND t.topic = ?)"
            )
            params.append(filters["topic"])
        if filters.get("unread") == "1":
            clauses.append("is_read = 0")
        if filters.get("starred") == "1":
            clauses.append("is_starred = 1")
        if filters.get("starred") == "1":
            order_by = "starred_at DESC, id DESC"
        else:
            order_by = "COALESCE(first_published_at, first_seen_at) DESC, id DESC"
        query = filters.get("q") or ""
        if query.strip():
            fts = _fts_query(query)
            if fts:
                clauses.append(
                    "papers.id IN (SELECT rowid FROM papers_fts WHERE papers_fts MATCH ?)"
                )
                params.append(fts)
        sql = f"""
            SELECT papers.* FROM papers
            WHERE {' AND '.join(clauses)}
            ORDER BY {order_by}
            LIMIT 300
        """
        rows = self.conn.execute(sql, params).fetchall()
        return [self._card(row) for row in rows]

    def get_paper(self, paper_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM papers WHERE id = ?", (paper_id,)).fetchone()
        if row is None:
            return None
        card = self._card(row)
        card["sources"] = [
            dict(item)
            for item in self.conn.execute(
                "SELECT * FROM paper_sources WHERE paper_id = ? ORDER BY id",
                (paper_id,),
            ).fetchall()
        ]
        card["authors"] = [
            dict(item)
            for item in self.conn.execute(
                """
                SELECT a.name AS author_name, i.name AS institution_name, i.lab_id
                FROM paper_authors pa
                JOIN authors a ON a.id = pa.author_id
                LEFT JOIN institutions i ON i.id = pa.institution_id
                WHERE pa.paper_id = ?
                ORDER BY pa.position
                """,
                (paper_id,),
            ).fetchall()
        ]
        return card

    def toggle_read(self, paper_id: int) -> bool | None:
        row = self.conn.execute(
            "SELECT is_read FROM papers WHERE id = ?",
            (paper_id,),
        ).fetchone()
        if row is None:
            return None
        new_value = 0 if row["is_read"] else 1
        self.conn.execute(
            "UPDATE papers SET is_read = ?, updated_at = ? WHERE id = ?",
            (new_value, utc_now(), paper_id),
        )
        self.conn.commit()
        return bool(new_value)

    def toggle_star(self, paper_id: int) -> bool | None:
        """切换收藏。收藏时记下 starred_at，取消时清空，列表按收藏时间倒序。"""
        row = self.conn.execute(
            "SELECT is_starred FROM papers WHERE id = ?",
            (paper_id,),
        ).fetchone()
        if row is None:
            return None
        moment = utc_now()
        if row["is_starred"]:
            new_value = 0
            starred_at = None
        else:
            new_value = 1
            starred_at = moment
        self.conn.execute(
            "UPDATE papers SET is_starred = ?, starred_at = ?, updated_at = ? WHERE id = ?",
            (new_value, starred_at, moment, paper_id),
        )
        self.conn.commit()
        return bool(new_value)

    def stats(self) -> dict:
        total = self.conn.execute("SELECT COUNT(*) AS n FROM papers").fetchone()["n"]
        unread = self.conn.execute(
            "SELECT COUNT(*) AS n FROM papers WHERE is_read = 0"
        ).fetchone()["n"]
        focus = len(self.list_papers({"group": "focus", "unread": "1"}))
        starred = self.conn.execute(
            "SELECT COUNT(*) AS n FROM papers WHERE is_starred = 1"
        ).fetchone()["n"]
        return {"total": total, "unread": unread, "focus_unread": focus, "starred": starred}

    def venues(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT venue FROM papers WHERE venue != '' ORDER BY venue"
        ).fetchall()
        return [row["venue"] for row in rows]

    def _card(self, row: sqlite3.Row) -> dict:
        paper_id = row["id"]
        reasons = [
            item["reason"]
            for item in self.conn.execute(
                "SELECT reason FROM paper_selection_reasons WHERE paper_id = ? ORDER BY reason",
                (paper_id,),
            ).fetchall()
        ]
        topics = [
            item["topic"]
            for item in self.conn.execute(
                "SELECT topic FROM paper_topics WHERE paper_id = ? ORDER BY topic",
                (paper_id,),
            ).fetchall()
        ]
        labs = [
            item["lab_id"]
            for item in self.conn.execute(
                """
                SELECT DISTINCT i.lab_id AS lab_id
                FROM paper_authors pa
                JOIN institutions i ON i.id = pa.institution_id
                WHERE pa.paper_id = ? AND i.lab_id IS NOT NULL
                """,
                (paper_id,),
            ).fetchall()
        ]
        data = dict(row)
        data["reasons"] = reasons
        data["topics"] = topics
        data["labs"] = labs
        return data

    def papers_missing_abstract(self, limit: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT id, doi, arxiv_id FROM papers
            WHERE abstract = '' AND (doi IS NOT NULL OR arxiv_id IS NOT NULL)
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    def set_abstract(self, paper_id: int, abstract: str, s2_id: str | None = None) -> None:
        self.conn.execute(
            """
            UPDATE papers
            SET abstract = ?, s2_id = COALESCE(s2_id, ?), updated_at = ?
            WHERE id = ?
            """,
            (abstract, s2_id, utc_now(), paper_id),
        )
        self.conn.commit()


def _is_arxiv_source(candidate: Candidate) -> bool:
    return candidate.kind in {"arxiv", "openalex"} and bool(candidate.arxiv_id)


def _date_only(value: str | None) -> str | None:
    if not value:
        return None
    return value[:10]


def _earliest(left: str | None, right: str | None) -> str | None:
    values = [item for item in (left, right) if item]
    if not values:
        return None
    return min(values)


def _latest(left: str | None, right: str | None) -> str | None:
    values = [item for item in (left, right) if item]
    if not values:
        return None
    return max(values)


def _fts_query(text: str) -> str | None:
    parts = normalize_phrase(text).split()
    if not parts:
        return None
    return " AND ".join(f'"{part}"' for part in parts)
