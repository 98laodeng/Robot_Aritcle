"""CoRL、ICRA、IROS 的公开页面。

只有链接附近明确出现 oral、spotlight、highlight、award、finalist 时才生成候选。
页面上的普通录用标题不会入库。IROS 那种人人都有的短口头报告因此不会变成 oral。
"""

from __future__ import annotations

import re
from urllib.parse import urljoin

from app.models import Candidate
from app.textutil import normalize_arxiv_id, normalize_doi, normalize_phrase

_LINK = re.compile(r"<a\b[^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>", re.I | re.S)
_TAGS = re.compile(r"<[^>]+>")
_SIGNAL = re.compile(r"\b(oral|spotlight|highlight|award|finalist)\b", re.I)
_PAPER_HOSTS = ("arxiv.org", "doi.org", "openreview.net", "proceedings.mlr.press", ".pdf")


def candidates_from_program_html(html: str, venue: str, year: int, page_url: str) -> list[Candidate]:
    candidates: list[Candidate] = []
    seen: set[str] = set()
    for match in _LINK.finditer(html):
        href = match.group(1)
        if not any(host in href.lower() for host in _PAPER_HOSTS):
            continue
        # 只看链接自身和它前面的说明。向后看会把下一节的 oral 误记到上一篇论文上。
        window = html[max(0, match.start() - 220) : match.end()]
        window_text = _TAGS.sub(" ", window)
        signals = {item.lower() for item in _SIGNAL.findall(window_text)}
        if not signals:
            continue
        title = re.sub(r"\s+", " ", _TAGS.sub(" ", match.group(2))).strip()
        if len(title) < 16:
            continue
        presentation = None
        if "spotlight" in signals:
            presentation = "spotlight"
        elif "highlight" in signals:
            presentation = "highlight"
        elif "oral" in signals:
            presentation = "oral"
        award = "award" in signals or "finalist" in signals
        if presentation is None and not award:
            continue
        key = normalize_phrase(title)
        if key in seen:
            continue
        seen.add(key)
        url = urljoin(page_url, href)
        candidates.append(
            Candidate(
                title=title,
                venue=venue,
                conference_year=year,
                presentation_type=presentation,
                award=award,
                arxiv_id=normalize_arxiv_id(url),
                doi=normalize_doi(url) if "doi.org" in url.lower() else None,
                url=url,
                source=f"program:{venue}",
                source_id=key,
                source_url=page_url,
                published_at=f"{year}-01-01",
                kind="conference",
            )
        )
    return candidates
