"""把一条候选记录变成「收或不收」以及一组可叠加的理由。

这里不判断论文好不好。会议 oral、RSS 接收、实验室、arXiv 主题词、
awesome 章节是五条独立通道，命中任意一条就进入雷达。
"""

from __future__ import annotations

from app.config import AppConfig
from app.matching import classify_topics, match_labs, section_allowed, topics_from_sections
from app.models import Candidate, Decision
from app.presentation import canonical_presentation

# 这些来源上的主题命中可以单独成为入库理由。会议海报和期刊不行。
_TOPIC_KEEP_KINDS = {"arxiv", "openalex"}

def _tag_institutions(config: AppConfig, candidate: Candidate) -> None:
    """每个单位单独匹配。只有命中的那一家带上 lab_id。"""
    for institution in candidate.institutions():
        hits = match_labs(config, [institution])
        institution.lab_id = hits[0] if hits else None


_CONFERENCE_REASONS = {
    "oral": "conference_oral",
    "spotlight": "conference_spotlight",
    "highlight": "conference_highlight",
}


def select(candidate: Candidate, config: AppConfig) -> Decision:
    presentation = canonical_presentation(candidate.presentation_type)
    reasons: list[str] = []
    topics: list[str] = []

    # RSS 的门槛是「被 RSS 接收」，不是 NeurIPS 式的 oral 信号。
    # 即使某届页面写了 oral，也不写 conference_oral。
    if candidate.kind == "rss":
        reasons.append("rss_selective")

    if candidate.kind == "conference":
        conference_reason = _CONFERENCE_REASONS.get(presentation or "")
        if conference_reason:
            reasons.append(conference_reason)
        if candidate.award:
            reasons.append("conference_award")

    if candidate.kind == "awesome" and section_allowed(config, candidate.section_path):
        reasons.append("awesome_list")
        for topic_id in topics_from_sections(config, candidate.section_path):
            if topic_id not in topics:
                topics.append(topic_id)

    labs = match_labs(config, candidate.institutions())
    _tag_institutions(config, candidate)
    # 实验室通道覆盖会议、预印本、期刊和 RSS。单位来自这条记录本身。
    if labs and candidate.kind in {"conference", "arxiv", "openalex", "journal", "rss", "awesome"}:
        reasons.append("target_lab")

    allow_weak = config.weak_category in candidate.categories
    matched_topics, topic_hit = classify_topics(
        config,
        candidate.title,
        candidate.abstract,
        allow_weak=allow_weak,
    )
    # arXiv / OpenAlex 上的主题命中可以单独入库。
    # 其他来源只有已经因为别的理由要入库时，才把主题补成额外理由，避免期刊或海报仅凭标题进来。
    if topic_hit and (candidate.kind in _TOPIC_KEEP_KINDS or reasons):
        if "target_topic" not in reasons:
            reasons.append("target_topic")
        for topic_id in matched_topics:
            if topic_id not in topics:
                topics.append(topic_id)
    elif reasons:
        for topic_id in matched_topics:
            if topic_id not in topics:
                topics.append(topic_id)

    return Decision(
        keep=bool(reasons),
        reasons=reasons,
        topics=topics,
        labs=labs,
        presentation_type=presentation,
    )
