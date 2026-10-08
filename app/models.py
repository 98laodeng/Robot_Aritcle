"""适配器交给选择器的候选论文，以及选择器的决定。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Institution:
    """一篇论文里某个作者当时的单位，不是作者今天的单位。"""

    name: str
    openalex_id: str | None = None
    ror: str | None = None
    # 选择器填上的 watchlist id。大学合作者保持为空，避免整篇论文的单位都被标成企业。
    lab_id: str | None = None


@dataclass
class Author:
    name: str
    institutions: list[Institution] = field(default_factory=list)


@dataclass
class Candidate:
    """一个来源看到的一条记录。同一篇论文可以多次出现，入库时再合并。"""

    title: str
    abstract: str = ""
    authors: list[Author] = field(default_factory=list)
    venue: str = ""
    conference_year: int | None = None
    presentation_type: str | None = None
    award: bool = False
    arxiv_id: str | None = None
    doi: str | None = None
    openreview_id: str | None = None
    openalex_id: str | None = None
    s2_id: str | None = None
    url: str = ""
    pdf_url: str = ""
    source: str = ""
    source_id: str = ""
    source_url: str = ""
    published_at: str | None = None
    source_updated_at: str | None = None
    categories: list[str] = field(default_factory=list)
    section_path: list[str] = field(default_factory=list)
    # conference / rss / arxiv / journal / awesome / openalex
    kind: str = ""

    @property
    def first_author(self) -> str:
        if not self.authors:
            return ""
        return self.authors[0].name

    def institutions(self) -> list[Institution]:
        found: list[Institution] = []
        for author in self.authors:
            found.extend(author.institutions)
        return found


@dataclass
class Decision:
    """keep 为假时调用方不得写入 papers。reasons 和 topics 都是可叠加的标签。"""

    keep: bool
    reasons: list[str] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    labs: list[str] = field(default_factory=list)
    presentation_type: str | None = None
