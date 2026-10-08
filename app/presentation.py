"""主展示类型的升级顺序。

award 不是展示类型，不出现在这个表里。论文可以同时是 spotlight 和
conference_award，获奖不会把 spotlight 改写成 award。
"""

from __future__ import annotations

# 数值越大越值得作为 papers.presentation_type 展示。只允许向更大的值升级。
PRESENTATION_PRIORITY = {
    "oral": 60,
    "spotlight": 50,
    "highlight": 40,
    "oral_poster": 30,
    "poster": 20,
    "preprint": 10,
}


def canonical_presentation(value: str | None) -> str | None:
    """丢掉 award 以及任何不在排序表里的值。"""
    if not value:
        return None
    key = value.strip().lower()
    if key in PRESENTATION_PRIORITY:
        return key
    return None


def upgrade_presentation(current: str | None, incoming: str | None) -> str | None:
    """后到的更弱展示类型不能覆盖已经记下的更强类型。"""
    current_key = canonical_presentation(current)
    incoming_key = canonical_presentation(incoming)
    if incoming_key is None:
        return current_key
    if current_key is None:
        return incoming_key
    if PRESENTATION_PRIORITY[incoming_key] > PRESENTATION_PRIORITY[current_key]:
        return incoming_key
    return current_key
