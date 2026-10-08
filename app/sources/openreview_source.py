"""OpenReview API。

接口目前会返回浏览器挑战。连续失败达到阈值后暂停一天，
暂停期间不再请求，也不把失败日志堆进每一次同步。
恢复后按 note 的 mdate 理解修改，而不是只看创建时间。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.models import Author, Candidate

_CHALLENGE = "ChallengeRequiredError"


def openreview_is_paused(row, now: str) -> bool:
    if row is None or not row["disabled_until"]:
        return False
    return str(row["disabled_until"]) > now


def next_failure_state(failures: int, limit: int, pause_hours: int, now: datetime) -> tuple[int, str | None, str]:
    """返回 (新的连续失败次数, disabled_until, 状态)。"""
    failures += 1
    if failures >= limit:
        until = (now + timedelta(hours=pause_hours)).replace(microsecond=0)
        return failures, until.isoformat(), "paused"
    return failures, None, "error"


def candidates_from_openreview(payload: dict, venue: str, year: int) -> list[Candidate]:
    notes = payload.get("notes") or []
    candidates: list[Candidate] = []
    for note in notes:
        content = note.get("content") or {}
        title = _value(content.get("title"))
        if not title:
            continue
        venue_text = " ".join(
            part
            for part in (
                _value(content.get("venue")),
                _value(content.get("venueid")),
                str(note.get("invitation") or ""),
            )
            if part
        )
        presentation, award = _signals(venue_text)
        authors = []
        for name in _value_list(content.get("authors")):
            authors.append(Author(name=name))
        mdate = _millis_to_iso(note.get("mdate"))
        pdate = _millis_to_iso(note.get("pdate"))
        candidates.append(
            Candidate(
                title=title,
                abstract=_value(content.get("abstract")),
                authors=authors,
                venue=venue,
                conference_year=year,
                presentation_type=presentation,
                award=award,
                openreview_id=note.get("id"),
                url=f"https://openreview.net/forum?id={note.get('id')}",
                source="openreview",
                source_id=str(note.get("id") or ""),
                source_url="https://api2.openreview.net/notes",
                published_at=(pdate or mdate or "")[:10] or None,
                source_updated_at=mdate,
                kind="conference",
            )
        )
    return candidates


def is_challenge(status: int, body: str) -> bool:
    return status == 403 or _CHALLENGE in body


def _signals(text: str) -> tuple[str | None, bool]:
    lowered = text.lower()
    award = "award" in lowered or "finalist" in lowered
    if "spotlight" in lowered:
        return "spotlight", award
    if "highlight" in lowered:
        return "highlight", award
    if "oral" in lowered:
        return "oral", award
    if "poster" in lowered:
        return "poster", award
    return None, award


def _value(field) -> str:
    if isinstance(field, dict):
        raw = field.get("value")
        if isinstance(raw, str):
            return raw.strip()
        return ""
    if isinstance(field, str):
        return field.strip()
    return ""


def _value_list(field) -> list[str]:
    raw = field.get("value") if isinstance(field, dict) else field
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


def _millis_to_iso(value) -> str | None:
    if not isinstance(value, (int, float)):
        return None
    moment = datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    return moment.replace(microsecond=0).isoformat()
