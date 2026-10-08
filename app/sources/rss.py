"""RSS 录用名单。

RSS 官方同时有 spotlight talk 和 poster，所以默认展示类型是 spotlight，
入库理由是 rss_selective，而不是 conference_oral。
只有 session 文本自己写了 oral 时，展示类型才改成 oral。
"""

from __future__ import annotations

import re
from urllib.parse import urljoin

from app.models import Author, Candidate
from app.textutil import normalize_phrase

_ROW = re.compile(r"<tr\b([^>]*)>(.*?)</tr>", re.I | re.S)
_BOLD = re.compile(r"<b>(.*?)</b>", re.I | re.S)
_CELL = re.compile(r"<td\b[^>]*>(.*?)</td>", re.I | re.S)
_HREF = re.compile(r'href="([^"]+)"', re.I)
_SESSION = re.compile(r'session="([^"]*)"', re.I)
_TAGS = re.compile(r"<[^>]+>")
_ORAL = re.compile(r"\boral\b", re.I)
_SPOTLIGHT = re.compile(r"\bspotlight\b", re.I)


def candidates_from_rss(html: str, year: int, page_url: str, default_presentation: str) -> list[Candidate]:
    candidates: list[Candidate] = []
    for attrs, body in _ROW.findall(html):
        title_match = _BOLD.search(body)
        if not title_match:
            continue
        title = _clean(_TAGS.sub("", title_match.group(1)))
        if not title or title.lower() == "title":
            continue
        session_match = _SESSION.search(attrs)
        session = session_match.group(1) if session_match else ""
        presentation = default_presentation
        if _ORAL.search(session):
            presentation = "oral"
        elif _SPOTLIGHT.search(session):
            presentation = "spotlight"
        href_match = _HREF.search(body)
        url = urljoin(page_url, href_match.group(1)) if href_match else page_url
        authors = _authors_from_row(body)
        candidates.append(
            Candidate(
                title=title,
                authors=authors,
                venue="RSS",
                conference_year=year,
                presentation_type=presentation,
                url=url,
                source="rss",
                source_id=f"{year}:{normalize_phrase(title)}",
                source_url=page_url,
                published_at=f"{year}-06-21",
                kind="rss",
            )
        )
    return candidates


def _authors_from_row(body: str) -> list[Author]:
    cells = _CELL.findall(body)
    if len(cells) < 4:
        return []
    text = _clean(_TAGS.sub(" ", cells[-1]))
    authors: list[Author] = []
    for name in text.split(","):
        name = name.strip()
        if name:
            authors.append(Author(name=name))
    return authors


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()
