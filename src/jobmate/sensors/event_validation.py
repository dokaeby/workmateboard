"""일정 후보 결정적 검증 — timezone/DST/모호/반복."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..models import ExtractedEvent


def validate_event(ev: ExtractedEvent) -> tuple[bool, str]:
    """(통과 여부, 사유). 모호/반복/불량 tz는 needs_review로 보낸다."""
    if ev.is_ambiguous:
        return False, "ambiguous"
    if ev.is_recurring:
        return False, "recurring"
    try:
        ZoneInfo(ev.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return False, "bad_timezone"
    try:
        start = datetime.fromisoformat(ev.start_at)
        end = datetime.fromisoformat(ev.end_at)
    except ValueError:
        return False, "bad_datetime"
    # aware/naive 혼합 비교는 TypeError — naive 쪽을 이벤트 timezone으로 통일
    tz = ZoneInfo(ev.timezone)
    if start.tzinfo is None:
        start = start.replace(tzinfo=tz)
    if end.tzinfo is None:
        end = end.replace(tzinfo=tz)
    if end <= start:
        return False, "end_before_start"
    if not ev.title.strip():
        return False, "empty_title"
    if ev.confidence < 0.5:
        return False, "low_confidence"
    return True, "ok"


def idempotency_key(connection_id: str, message_id: str) -> str:
    import hashlib

    return hashlib.sha256(f"{connection_id}:{message_id}".encode()).hexdigest()[:32]
