"""实验室别名、主题词和 awesome 章节的确定性规则。"""

from __future__ import annotations

from app.config import AppConfig, LabConfig
from app.models import Institution
from app.textutil import alias_matches, phrase_in_text, tokens


def enabled_labs(config: AppConfig) -> list[LabConfig]:
    return [lab for lab in config.labs if lab.enabled]


def match_labs(config: AppConfig, institutions: list[Institution]) -> list[str]:
    """先认 OpenAlex id / ROR，再用 token 别名。不在摘要里搜实验室。

    返回的是 lab id，顺序与配置一致，同一实验室只出现一次。
    """
    hits: list[str] = []
    for lab in enabled_labs(config):
        if _lab_hit(lab, institutions):
            hits.append(lab.id)
    return hits


def _lab_hit(lab: LabConfig, institutions: list[Institution]) -> bool:
    openalex_ids = set(lab.openalex_ids)
    rors = {item.lower() for item in lab.rors}
    for institution in institutions:
        if institution.openalex_id and institution.openalex_id in openalex_ids:
            return True
        if institution.ror and institution.ror.lower() in rors:
            return True
        for alias in lab.names:
            if alias_matches(institution.name, alias):
                return True
    return False


def classify_topics(
    config: AppConfig,
    title: str,
    abstract: str,
    *,
    allow_weak: bool,
) -> tuple[list[str], bool]:
    """返回（命中的主题 id，这条主题规则是否足以单独入库）。

    强标题命中即可入库。弱标题必须再在摘要里看到任一主题词，
    并且调用方只在 cs.RO 上把 allow_weak 设为真。
    """
    abstract_terms = _abstract_terms(config)
    abstract_hit = any(phrase_in_text(abstract, term) for term in abstract_terms)
    matched: list[str] = []
    keep = False
    for topic in config.topics:
        strong = any(phrase_in_text(title, phrase) for phrase in topic.strong_title)
        weak = allow_weak and any(phrase_in_text(title, phrase) for phrase in topic.weak_title)
        if strong or (weak and abstract_hit):
            matched.append(topic.id)
            keep = True
    return matched, keep


def _abstract_terms(config: AppConfig) -> list[str]:
    terms: list[str] = []
    for topic in config.topics:
        terms.extend(topic.strong_title)
        terms.extend(topic.weak_title)
    return terms


def section_allowed(config: AppConfig, section_path: list[str]) -> bool:
    """从根标题走到叶子。同一层 exclude 覆盖 include，更深的标题可以再改回来。

    因此 VLN 大节下的论文不会因为父级没写 exclude 以外的原因进雷达；
    若叶子标题本身落在 include（例如 Manipulation），则仍然收录。
    """
    decision = False
    for heading in section_path:
        if _heading_matches(heading, config.awesome_include):
            decision = True
        if _heading_matches(heading, config.awesome_exclude):
            decision = False
    return decision


def topics_from_sections(config: AppConfig, section_path: list[str]) -> list[str]:
    found: list[str] = []
    for heading in section_path:
        for phrase, topic_id in config.section_topics.items():
            if _heading_matches(heading, [phrase]) and topic_id not in found:
                found.append(topic_id)
    return found


def _heading_matches(heading: str, phrases: list[str]) -> bool:
    heading_tokens = set(tokens(heading))
    if not heading_tokens:
        return False
    for phrase in phrases:
        phrase_tokens = tokens(phrase)
        if phrase_tokens and set(phrase_tokens).issubset(heading_tokens):
            return True
    return False
