"""calendar 일정 충돌/중복 검사 function."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from ..sensors import idempotency_key
from .fn_common import FunctionContext, _prev_output, _src

logger = logging.getLogger("jobmate.engine.functions")


def _event_key(title: str, start_at: str, end_at: str, timezone: str = "Asia/Seoul") -> str:
    """calendar-agent event_key 포팅 — 동일 일정 판별용 정규화 해시."""
    import hashlib
    import json
    from datetime import UTC
    from zoneinfo import ZoneInfo

    def aware(v: str) -> str:
        dt = datetime.fromisoformat(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo(timezone))
        return dt.astimezone(UTC).isoformat()

    data = [" ".join(title.casefold().split()), aware(start_at), aware(end_at)]
    return hashlib.sha256(json.dumps(data).encode()).hexdigest()


def calendar_conflict_check(ctx: FunctionContext) -> dict[str, Any]:
    """calendar-agent RegistrationService의 중복/충돌 검사 (결정적)."""
    validated = _prev_output(ctx, "validate")
    ev = validated.get("event", {})
    if not ev:
        return {"status": "invalid"}
    provider, source_id, conn, thread, _ = _src(ctx)
    cal = ctx.adapters.get("calendar")

    # 동일 소스가 이미 등록/확인 대기면 중복
    existing = ctx.store.get_scheduled_by_source(provider, source_id)
    if existing and existing.get("status") in ("registered", "needs_confirmation"):
        return {"status": "duplicate", "calendar_event_id": existing.get("calendar_event_id", "")}

    sched_id = f"{provider}:{source_id}"
    start_at, end_at = ev.get("start_at", ""), ev.get("end_at", "")
    tz = ev.get("timezone", "Asia/Seoul")

    matches: list[dict[str, Any]] = []
    if cal is not None and hasattr(cal, "search_events"):
        try:
            matches = cal.search_events(start_at, end_at)
        except Exception as e:
            ctx.store.audit(
                ctx.run_id, "tool", "calendar_search_error", "", {"error": str(e)[:200]}
            )
            logger.warning("calendar search_events failed run=%s: %s", ctx.run_id, str(e)[:200])
            matches = []

    key = _event_key(ev.get("title", ""), start_at, end_at, tz)
    for m in matches:
        if m.get("status") == "cancelled":
            continue
        # 같은 마커(우리 멱등 키)거나 동일 title+시간 → 자기 자신 (복구 경로)
        marker = m.get("marker", "")
        if marker == idempotency_key(f"{provider}:{conn}", source_id):
            return {"status": "self", "calendar_event_id": m.get("id", "")}
        try:
            ms = m.get("start", {}).get("dateTime", "")
            me = m.get("end", {}).get("dateTime", "")
            mkey = _event_key(m.get("summary", ""), ms, me, tz)
        except Exception:
            mkey = ""
        if mkey and mkey == key:
            return {"status": "self", "calendar_event_id": m.get("id", "")}

    conflict = any(
        m.get("status") != "cancelled" and m.get("transparency") != "transparent" for m in matches
    )
    status = "conflict" if conflict else "clear"
    # 후보 레지스트리 기록 — 사람 확인 큐의 원천
    ctx.store.save_scheduled_event(
        event_id=sched_id,
        source_provider=provider,
        source_id=source_id,
        thread_id=thread,
        title=ev.get("title", ""),
        start_at=start_at,
        end_at=end_at,
        timezone=tz,
        location=ev.get("location", ""),
        summary=ev.get("summary", ""),
        status="needs_confirmation" if conflict else "candidate",
        confidence=float(ev.get("confidence", 0.0)),
        reason="calendar_conflict" if conflict else "",
        source_refs=source_id,
    )
    return {"status": status, "match_count": len(matches)}
