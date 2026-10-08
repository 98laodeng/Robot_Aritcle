"""NeurIPS、ICLR、CVPR 虚拟站快照。

这些 JSON 同时包含 oral、spotlight、highlight 和 poster。
展示类型只从 decision / eventtype 里的明确用词读取，award 单独标出，
不参与展示类型。
"""

from __future__ import annotations

import re

from app.models import Author, Candidate, Institution

_WORD = lambda word: re.compile(rf"\b{word}\b", re.I)


def presentation_from_virtual(decision: str | None, eventtype: str | None, event_type: str | None) -> tuple[str | None, bool]:
    """返回 (展示类型, 是否获奖)。award 不会被当成一种展示类型。"""
    decision_text = decision or ""
    event_text = f"{eventtype or ''} {event_type or ''}"
    award = bool(_WORD("award").search(decision_text) or _WORD("finalist").search(decision_text))
    award = award or bool(_WORD("award").search(event_text) or _WORD("finalist").search(event_text))
    for text in (decision_text, event_text):
        if _WORD("spotlight").search(text):
            return "spotlight", award
        if _WORD("highlight").search(text):
            return "highlight", award
        if _WORD("oral").search(text):
            return "oral", award
    if _WORD("poster").search(decision_text) or _WORD("poster").search(event_text):
        return "poster", award
    return None, award


def candidates_from_virtual(payload: dict, venue: str, year: int, source_url: str) -> list[Candidate]:
    candidates: list[Candidate] = []
    for item in payload.get("results") or []:
        title = (item.get("name") or "").strip()
        if not title:
            continue
        presentation, award = presentation_from_virtual(
            item.get("decision"),
            item.get("eventtype"),
            item.get("event_type"),
        )
        authors: list[Author] = []
        for author in item.get("authors") or []:
            name = (author.get("fullname") or "").strip()
            if not name:
                continue
            institution_name = (author.get("institution") or "").strip()
            institutions = [Institution(name=institution_name)] if institution_name else []
            authors.append(Author(name=name, institutions=institutions))
        start = item.get("starttime") or ""
        published = start[:10] if isinstance(start, str) and len(start) >= 10 else f"{year}-01-01"
        candidates.append(
            Candidate(
                title=title,
                abstract=item.get("abstract") or "",
                authors=authors,
                venue=venue,
                conference_year=year,
                presentation_type=presentation,
                award=award,
                url=item.get("paper_url") or item.get("sourceurl") or source_url,
                pdf_url=item.get("paper_pdf_url") or "",
                source=f"virtual:{venue}",
                source_id=str(item.get("id") or item.get("uid") or title),
                source_url=source_url,
                published_at=published,
                source_updated_at=None,
                kind="conference",
            )
        )
    return candidates
