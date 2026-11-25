"""gmail_* function + 웹푸시/알림 예약 헬퍼."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from ..models import LinkStatus, NotificationStatus
from ..sensors import idempotency_key, is_safe_source_url, validate_event
from .fn_common import FunctionContext, _prev_output, _src

logger = logging.getLogger("jobmate.engine.functions")


def gmail_validate(ctx: FunctionContext) -> dict[str, Any]:
    """추출 후보를 결정적 검증한다."""
    ext = _prev_output(ctx, "extract")
    from ..models import ExtractedEvent

    try:
        ev = ExtractedEvent.model_validate(ext.get("event", ext))
    except Exception as e:
        return {"status": "invalid", "reason": str(e)[:200]}
    ok, reason = validate_event(ev)
    if not ok:
        # 검토 큐(scheduled_events)에 needs_review로 남겨 사람이 승인/수정할 수 있게 한다
        provider, source_id, _, thread, _ = _src(ctx)
        ctx.store.save_scheduled_event(
            event_id=f"{provider}:{source_id}",
            source_provider=provider,
            source_id=source_id,
            thread_id=thread,
            title=ev.title,
            start_at=ev.start_at,
            end_at=ev.end_at,
            timezone=ev.timezone,
            location=ev.location,
            summary=ev.summary,
            status="needs_review",
            confidence=ev.confidence,
            reason=reason,
            source_refs=source_id,
        )
    return {
        "status": "validated" if ok else "needs_review",
        "reason": reason,
        "event": ev.model_dump(),
    }


def gmail_register(ctx: FunctionContext) -> dict[str, Any]:
    """멱등 Calendar 등록 — pending 예약→marker 조회→생성→ID 저장.
    멀티채널: state의 source_provider/source_id 사용 (기본 gmail)."""
    calendar = ctx.adapters.get("calendar")
    if calendar is None:
        return {"status": "no_adapter"}
    validated = _prev_output(ctx, "validate")
    ev = validated.get("event", {})
    provider, source_id, conn, thread, _ = _src(ctx)
    key = idempotency_key(f"{provider}:{conn}", source_id)

    _, _, conn, _, source_url = _src(ctx)
    link = ctx.store.reserve_link(key, source_id, conn, source_url)
    if link is None:
        # 기존 링크 — 완료됐으면 duplicate, pending이면 marker 복구 시도
        existing = ctx.store.get_link_by_key(key)
        if existing is None:
            return {"status": "duplicate_pending", "idempotency_key": key}
        if existing.calendar_event_id:
            return {
                "status": "duplicate",
                "calendar_event_id": existing.calendar_event_id,
                "idempotency_key": key,
            }
        found = calendar.find_by_marker(key)
        if found:
            ctx.store.update_link(
                existing.id, status=LinkStatus.CALENDAR_CREATED, calendar_event_id=found
            )
            ctx.store.audit(ctx.run_id, "tool", "calendar_recovered", key, {"event_id": found})
            return {"status": "recovered", "calendar_event_id": found, "idempotency_key": key}
        return {"status": "duplicate_pending", "idempotency_key": key}

    # pending 링크 확보 후 외부 호출 — 타임아웃 시 marker 조회로 복구
    try:
        existing = calendar.find_by_marker(key)
        if existing:
            ctx.store.update_link(
                link.id, status=LinkStatus.CALENDAR_CREATED, calendar_event_id=existing
            )
            return {"status": "recovered", "calendar_event_id": existing, "idempotency_key": key}
        event_id = calendar.create_event(
            title=ev.get("title", ""),
            start_at=ev.get("start_at", ""),
            end_at=ev.get("end_at", ""),
            timezone=ev.get("timezone", "Asia/Seoul"),
            location=ev.get("location", ""),
            marker=key,
        )
    except Exception as e:
        ctx.store.update_link(
            link.id, status=LinkStatus.RETRY_WAIT, last_error=str(e)[:300], attempts_inc=True
        )
        ctx.store.audit(ctx.run_id, "tool", "calendar_error", key, {"error": str(e)[:200]})
        logger.warning(
            "calendar create_event failed run=%s key=%s: %s", ctx.run_id, key, str(e)[:200]
        )
        return {"status": "retry_wait", "error": str(e)[:300]}

    ctx.store.update_link(link.id, status=LinkStatus.CALENDAR_CREATED, calendar_event_id=event_id)
    ctx.store.audit(ctx.run_id, "tool", "calendar_created", key, {"event_id": event_id})
    # scheduled_events 레지스트리 갱신 (candidate → registered)
    provider, source_id, _, thread, _ = _src(ctx)
    ctx.store.save_scheduled_event(
        event_id=f"{provider}:{source_id}",
        source_provider=provider,
        source_id=source_id,
        thread_id=thread,
        calendar_event_id=event_id,
        title=ev.get("title", ""),
        start_at=ev.get("start_at", ""),
        end_at=ev.get("end_at", ""),
        timezone=ev.get("timezone", "Asia/Seoul"),
        location=ev.get("location", ""),
        summary=ev.get("summary", ""),
        status="registered",
        confidence=float(ev.get("confidence", 0.0)),
        source_refs=source_id,
    )
    return {
        "status": "created",
        "calendar_event_id": event_id,
        "idempotency_key": key,
        "scheduled_event_id": f"{provider}:{source_id}",
    }


def gmail_notify(ctx: FunctionContext) -> dict[str, Any]:
    """알림 큐잉 + 발송. 실패해도 Calendar 이벤트는 유지된다."""
    reg = _prev_output(ctx, "register")
    link_key = reg.get("idempotency_key", "")
    link = ctx.store.get_link_by_key(link_key) if link_key else None
    if link is None:
        return {"status": "no_link"}
    if reg.get("status") == "duplicate" and link.status == LinkStatus.NOTIFIED:
        return {"status": "already_notified"}

    ev = _prev_output(ctx, "validate").get("event", {})
    _, _, _, _, source_url = _src(ctx)
    if not source_url or not is_safe_source_url(source_url):
        source_url = ""
    notifier = ctx.adapters.get("notifier")
    if notifier is None:
        return {"status": "no_adapter"}
    n = ctx.store.add_notification(
        title=f"일정 등록: {ev.get('title', '')}",
        body=ev.get("start_at", ""),
        source_url=source_url,
        run_id=ctx.run_id,
        link_id=link.id,
    )
    try:
        notifier.send(title=n.title, body=n.body, source_url=n.source_url)
        ctx.store.set_notification_status(n.id, NotificationStatus.SENT)
        ctx.store.update_link(link.id, status=LinkStatus.NOTIFIED, notification_id=n.id)
        jobs = _schedule_event_notifications(ctx, ev, reg)
        _send_web_push(ctx, n)
        return {"status": "notified", "notification_id": n.id, "notification_jobs": jobs}
    except Exception as e:
        ctx.store.set_notification_status(n.id, NotificationStatus.FAILED)
        ctx.store.update_link(
            link.id,
            status=LinkStatus.NOTIFICATION_QUEUED,
            notification_id=n.id,
            last_error=str(e)[:300],
        )
        ctx.store.audit(ctx.run_id, "tool", "notify_failed", link.id, {"error": str(e)[:200]})
        logger.warning("notify failed run=%s link=%s: %s", ctx.run_id, link.id, str(e)[:200])
        return {"status": "notify_failed", "notification_id": n.id, "error": str(e)[:300]}


def _send_web_push(ctx: FunctionContext, n: Any) -> None:
    """Web Push 채널 — best-effort. 실패해도 알림 상태는 SENT 유지."""
    push = ctx.adapters.get("webpush")
    if push is None:
        return
    try:
        push.send(title=n.title, body=n.body, source_url=n.source_url)
    except Exception as e:
        ctx.store.audit(ctx.run_id, "tool", "webpush_failed", str(n.id), {"error": str(e)[:200]})
        logger.warning("webpush send failed run=%s: %s", ctx.run_id, str(e)[:200])


def _schedule_event_notifications(
    ctx: FunctionContext, ev: dict[str, Any], reg: dict[str, Any]
) -> list[str]:
    """calendar-agent NotificationScheduler 포팅 — 시작 2h/1h 전 예약."""
    sched_id = reg.get("scheduled_event_id") or ""
    if not sched_id:
        provider, source_id, _, _, _ = _src(ctx)
        sched_id = f"{provider}:{source_id}"
    try:
        start = datetime.fromisoformat(ev.get("start_at", ""))
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
    except ValueError:
        return []
    now = datetime.now(UTC)
    jobs: list[str] = []
    # 90분 이내 등록이면 2h 알림 생략, 30분 이내면 1h 알림 생략 (원본 규칙)
    if (start - now).total_seconds() > 90 * 60:
        jobs.append(
            ctx.store.save_notification_job(
                sched_id, "push", (start - timedelta(hours=2)).isoformat()
            )
        )
    if (start - now).total_seconds() > 30 * 60:
        jobs.append(
            ctx.store.save_notification_job(
                sched_id, "email", (start - timedelta(hours=1)).isoformat()
            )
        )
    return jobs
