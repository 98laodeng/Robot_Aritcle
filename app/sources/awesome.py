"""解析 awesome 列表，并记住每条论文所在的 Markdown 章节。

收录与否不看标题关键词，而看章节是否落在配置的 include / exclude 里。
"""

from __future__ import annotations

import re

from app.models import Candidate
from app.textutil import normalize_arxiv_id, normalize_doi

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_YEAR = re.compile(r"\b(20\d{2})\b")
_GENERIC = {
    "pdf",
    "arxiv",
    "paper",
    "link",
    "code",
    "project",
    "abs",
    "html",
    "video",
    "site",
    "github",
    "page",
    "website",
    "demo",
}


def parse_awesome_markdown(markdown: str, repo: str, source_url: str) -> list[Candidate]:
    stack: list[tuple[int, str]] = []
    candidates: list[Candidate] = []
    for line in markdown.splitlines():
        heading = _HEADING.match(line.strip())
        if heading:
            level = len(heading.group(1))
            title = heading.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            continue
        section_path = [item[1] for item in stack]
        years = [int(item) for item in _YEAR.findall(line)]
        if years and max(years) < 2024:
            continue
        bold = _BOLD.search(line)
        for match in _LINK.finditer(line):
            link_text = match.group(1).strip()
            url = match.group(2).strip()
            if not _is_paper_url(url):
                continue
            title = link_text
            if link_text.lower() in _GENERIC and bold:
                title = bold.group(1).strip()
            title = re.sub(r"\s+", " ", title).strip(" -")
            if len(title) < 8 or title.lower() in _GENERIC:
                continue
            year = max(years) if years else None
            candidates.append(
                Candidate(
                    title=title,
                    venue="",
                    conference_year=year,
                    arxiv_id=normalize_arxiv_id(url),
                    doi=normalize_doi(url) if "doi.org" in url.lower() else None,
                    openreview_id=_openreview_id(url),
                    url=url,
                    source=f"awesome:{repo}",
                    source_id=url,
                    source_url=source_url,
                    published_at=f"{year}-01-01" if year else None,
                    section_path=list(section_path),
                    kind="awesome",
                )
            )
    return candidates


def _is_paper_url(url: str) -> bool:
    lowered = url.lower()
    return any(
        token in lowered
        for token in ("arxiv.org", "doi.org", "openreview.net", "proceedings.mlr.press")
    )


def _openreview_id(url: str) -> str | None:
    match = re.search(r"[?&]id=([^&#]+)", url)
    if match and "openreview.net" in url.lower():
        return match.group(1)
    return None
