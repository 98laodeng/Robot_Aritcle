"""读取 config.yaml。实验室、主题词和章节名单都在配置里，不写死在选择器中。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class LabConfig:
    id: str
    label: str
    names: list[str]
    enabled: bool = True
    openalex_ids: list[str] = field(default_factory=list)
    rors: list[str] = field(default_factory=list)


@dataclass
class TopicConfig:
    id: str
    label: str
    strong_title: list[str]
    weak_title: list[str]


@dataclass
class VirtualSite:
    venue: str
    year: int
    url: str


@dataclass
class RssFeed:
    year: int
    url: str
    default_presentation: str


@dataclass
class NamedPage:
    venue: str
    year: int
    url: str


@dataclass
class Journal:
    name: str
    issn: str


@dataclass
class AppConfig:
    window_start: str
    window_end: str
    track_interval_hours: int
    reconcile_lookback_days: int
    database: Path
    mailto: str
    labs: list[LabConfig]
    topics: list[TopicConfig]
    awesome_repos: list[str]
    awesome_include: list[str]
    awesome_exclude: list[str]
    section_topics: dict[str, str]
    virtual_sites: list[VirtualSite]
    rss_feeds: list[RssFeed]
    corl_pages: list[NamedPage]
    program_pages: list[NamedPage]
    journals: list[Journal]
    openreview_venues: list[NamedPage]
    openreview_failure_limit: int
    openreview_pause_hours: int
    arxiv_page_size: int
    arxiv_pages_per_incremental: int
    weak_category: str
    other_categories: list[str]

    def lab_label(self, lab_id: str) -> str:
        for lab in self.labs:
            if lab.id == lab_id:
                return lab.label
        return lab_id

    def topic_label(self, topic_id: str) -> str:
        for topic in self.topics:
            if topic.id == topic_id:
                return topic.label
        return topic_id


def _labs(raw: list[dict], default_enabled: bool) -> list[LabConfig]:
    labs: list[LabConfig] = []
    for item in raw or []:
        labs.append(
            LabConfig(
                id=item["id"],
                label=item.get("label") or item["id"],
                names=list(item.get("names") or []),
                enabled=bool(item.get("enabled", default_enabled)),
                openalex_ids=list(item.get("openalex_ids") or []),
                rors=list(item.get("rors") or []),
            )
        )
    return labs


def _pages(raw: list[dict]) -> list[NamedPage]:
    return [
        NamedPage(venue=item["venue"], year=int(item["year"]), url=item["url"])
        for item in raw or []
        if item.get("url")
    ]


def load_config(path: Path | None = None) -> AppConfig:
    config_path = path or ROOT / "config.yaml"
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    database = Path(raw["database"])
    if not database.is_absolute():
        database = ROOT / database
    topics = []
    for topic_id, body in (raw.get("topics") or {}).items():
        topics.append(
            TopicConfig(
                id=topic_id,
                label=body.get("label") or topic_id,
                strong_title=list(body.get("strong_title") or []),
                weak_title=list(body.get("weak_title") or []),
            )
        )
    arxiv = raw.get("arxiv") or {}
    openreview = raw.get("openreview") or {}
    awesome_sections = raw.get("awesome_sections") or {}
    return AppConfig(
        window_start=raw["window"]["start"],
        window_end=raw["window"]["end"],
        track_interval_hours=int(raw.get("track_interval_hours") or 6),
        reconcile_lookback_days=int(raw.get("reconcile_lookback_days") or 180),
        database=database,
        mailto=raw.get("mailto") or "local@localhost",
        labs=_labs(raw.get("labs"), True) + _labs(raw.get("optional_labs"), False),
        topics=topics,
        awesome_repos=list(raw.get("awesome_repos") or []),
        awesome_include=list(awesome_sections.get("include") or []),
        awesome_exclude=list(awesome_sections.get("exclude") or []),
        section_topics=dict(raw.get("section_topics") or {}),
        virtual_sites=[
            VirtualSite(venue=item["venue"], year=int(item["year"]), url=item["url"])
            for item in raw.get("virtual_sites") or []
        ],
        rss_feeds=[
            RssFeed(
                year=int(item["year"]),
                url=item["url"],
                default_presentation=item.get("default_presentation") or "spotlight",
            )
            for item in raw.get("rss") or []
        ],
        corl_pages=_pages(raw.get("corl_pages")),
        program_pages=_pages(raw.get("program_pages")),
        journals=[
            Journal(name=item["name"], issn=item["issn"])
            for item in raw.get("journals") or []
        ],
        openreview_venues=[
            NamedPage(venue=item["name"], year=int(item["year"]), url=item["id"])
            for item in openreview.get("venues") or []
        ],
        openreview_failure_limit=int(openreview.get("failure_limit") or 5),
        openreview_pause_hours=int(openreview.get("pause_hours") or 24),
        arxiv_page_size=int(arxiv.get("page_size") or 50),
        arxiv_pages_per_incremental=int(arxiv.get("pages_per_incremental") or 2),
        weak_category=arxiv.get("weak_category") or "cs.RO",
        other_categories=list(arxiv.get("other_categories") or []),
    )
