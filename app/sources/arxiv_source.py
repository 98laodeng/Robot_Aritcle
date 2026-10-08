"""arXiv Atom API。弱主题只扫 cs.RO，其他分类只按强标题词检索。"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from app.models import Author, Candidate
from app.textutil import normalize_arxiv_id

_ATOM = "{http://www.w3.org/2005/Atom}"
_ARXIV = "{http://arxiv.org/schemas/atom}"


def arxiv_query(category: str, start: str, end: str, title_clause: str | None = None) -> str:
    """拼出 arXiv 的 submittedDate 查询。日期格式 YYYY-MM-DD。"""
    start_token = start.replace("-", "") + "0000"
    end_token = end.replace("-", "") + "2359"
    query = f"cat:{category} AND submittedDate:[{start_token} TO {end_token}]"
    if title_clause:
        query = f"({query}) AND ({title_clause})"
    return query


def strong_title_clause(phrases: list[str]) -> str:
    """每个强标题短语变成 arXiv 的 ti: 查询。多词短语要求这些词同时出现在标题里。"""
    parts = []
    for phrase in phrases:
        words = [word for word in phrase.replace("-", " ").split() if word]
        if not words:
            continue
        if len(words) == 1:
            parts.append(f"ti:{words[0]}")
        else:
            parts.append("(" + " AND ".join(f"ti:{word}" for word in words) + ")")
    return " OR ".join(parts)


def candidates_from_arxiv(xml_text: str) -> list[Candidate]:
    root = ET.fromstring(xml_text)
    candidates: list[Candidate] = []
    for entry in root.findall(f"{_ATOM}entry"):
        title = _text(entry, "title")
        if not title or title.lower().startswith("error"):
            continue
        arxiv_url = _text(entry, "id")
        arxiv_id = normalize_arxiv_id(arxiv_url)
        published = _text(entry, "published")[:10]
        updated = _text(entry, "updated")[:10]
        categories = []
        primary = entry.find(f"{_ARXIV}primary_category")
        if primary is not None and primary.attrib.get("term"):
            categories.append(primary.attrib["term"])
        for category in entry.findall(f"{_ATOM}category"):
            term = category.attrib.get("term")
            if term and term not in categories:
                categories.append(term)
        authors = []
        for author in entry.findall(f"{_ATOM}author"):
            name = _text(author, "name")
            if name:
                authors.append(Author(name=name))
        pdf_url = ""
        for link in entry.findall(f"{_ATOM}link"):
            if link.attrib.get("title") == "pdf" or link.attrib.get("type") == "application/pdf":
                pdf_url = link.attrib.get("href") or ""
        candidates.append(
            Candidate(
                title=title,
                abstract=_text(entry, "summary"),
                authors=authors,
                venue="arXiv",
                presentation_type="preprint",
                arxiv_id=arxiv_id,
                url=arxiv_url,
                pdf_url=pdf_url,
                source="arxiv",
                source_id=arxiv_id or title,
                source_url="https://export.arxiv.org/api/query",
                published_at=published or None,
                source_updated_at=updated or None,
                categories=categories,
                kind="arxiv",
            )
        )
    return candidates


def _text(node: ET.Element, name: str) -> str:
    child = node.find(f"{_ATOM}{name}")
    if child is None or child.text is None:
        return ""
    return " ".join(child.text.split())
