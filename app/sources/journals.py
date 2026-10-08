"""Crossref 期刊元数据。期刊只走实验室通道，主题词不能单独把一篇期刊论文放进来。"""

from __future__ import annotations

from app.models import Author, Candidate, Institution
from app.textutil import normalize_doi


def candidates_from_crossref(message: dict, journal_name: str) -> tuple[list[Candidate], str | None]:
    items = message.get("items") or []
    next_cursor = (message.get("next-cursor") or "").strip() or None
    candidates: list[Candidate] = []
    for item in items:
        titles = item.get("title") or []
        title = (titles[0] if titles else "").strip()
        if not title:
            continue
        authors: list[Author] = []
        for author in item.get("author") or []:
            name = " ".join(part for part in (author.get("given"), author.get("family")) if part).strip()
            if not name:
                continue
            institutions = []
            for affiliation in author.get("affiliation") or []:
                affiliation_name = (affiliation.get("name") or "").strip()
                if affiliation_name:
                    institutions.append(Institution(name=affiliation_name))
            authors.append(Author(name=name, institutions=institutions))
        issued = item.get("issued", {}).get("date-parts") or []
        published = None
        if issued and issued[0]:
            parts = issued[0]
            year = parts[0]
            month = parts[1] if len(parts) > 1 else 1
            day = parts[2] if len(parts) > 2 else 1
            published = f"{year:04d}-{month:02d}-{day:02d}"
        doi = normalize_doi(item.get("DOI"))
        abstract = item.get("abstract") or ""
        candidates.append(
            Candidate(
                title=title,
                abstract=_strip_tags(abstract),
                authors=authors,
                venue=journal_name,
                presentation_type=None,
                doi=doi,
                url=item.get("URL") or (f"https://doi.org/{doi}" if doi else ""),
                source=f"crossref:{journal_name}",
                source_id=doi or title,
                source_url="https://api.crossref.org",
                published_at=published,
                kind="journal",
            )
        )
    return candidates, next_cursor


def _strip_tags(value: str) -> str:
    import re

    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", value or "")).strip()
