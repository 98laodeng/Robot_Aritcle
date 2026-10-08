"""用 OpenAlex 的 authorship 判断这篇论文发表时的单位。

不使用作者当前单位。机构 id 和 ROR 优先，名称只作为 token 别名的后备。
"""

from __future__ import annotations

from app.models import Author, Candidate, Institution
from app.textutil import normalize_arxiv_id, normalize_doi, normalize_openalex_id, normalize_ror


def abstract_from_inverted(inverted: dict | None) -> str:
    if not inverted:
        return ""
    positions: list[tuple[int, str]] = []
    for word, indexes in inverted.items():
        for index in indexes:
            positions.append((index, word))
    positions.sort()
    return " ".join(word for _, word in positions)


def candidates_from_openalex(works: list[dict]) -> list[Candidate]:
    candidates: list[Candidate] = []
    for work in works:
        title = ((work.get("title") or work.get("display_name") or "")).strip()
        if not title:
            continue
        ids = work.get("ids") or {}
        doi = normalize_doi(ids.get("doi") or work.get("doi"))
        arxiv_id = normalize_arxiv_id(ids.get("arxiv")) or _arxiv_from_doi(doi)
        authors: list[Author] = []
        for authorship in work.get("authorships") or []:
            author = authorship.get("author") or {}
            name = (author.get("display_name") or "").strip()
            if not name:
                continue
            institutions = []
            for institution in authorship.get("institutions") or []:
                institution_name = (institution.get("display_name") or "").strip()
                if not institution_name:
                    continue
                institutions.append(
                    Institution(
                        name=institution_name,
                        openalex_id=normalize_openalex_id(institution.get("id")),
                        ror=normalize_ror(institution.get("ror")),
                    )
                )
            if not institutions:
                for raw in authorship.get("raw_affiliation_strings") or []:
                    if raw:
                        institutions.append(Institution(name=raw))
            authors.append(Author(name=name, institutions=institutions))
        location = work.get("primary_location") or {}
        source = (location.get("source") or {}).get("display_name") or ""
        landing = location.get("landing_page_url") or ""
        pdf = ((work.get("open_access") or {}).get("oa_url")) or ""
        openalex_id = normalize_openalex_id(work.get("id"))
        published = work.get("publication_date")
        kind = "openalex"
        presentation = "preprint" if arxiv_id and not source else None
        candidates.append(
            Candidate(
                title=title,
                abstract=abstract_from_inverted(work.get("abstract_inverted_index")),
                authors=authors,
                venue=source or ("arXiv" if arxiv_id else ""),
                presentation_type=presentation,
                arxiv_id=arxiv_id,
                doi=doi,
                openalex_id=openalex_id,
                url=landing or (f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else ""),
                pdf_url=pdf or "",
                source="openalex",
                source_id=openalex_id or doi or title,
                source_url="https://api.openalex.org/works",
                published_at=published,
                kind=kind,
            )
        )
    return candidates


def _arxiv_from_doi(doi: str | None) -> str | None:
    if not doi:
        return None
    return normalize_arxiv_id(doi)
