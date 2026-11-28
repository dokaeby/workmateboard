"""Settings/events routes extracted from api.app.create_app."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ..jobs import _sync_cronjob
from ..schemas import EventReviewRequest

logger = logging.getLogger("jobmate.api")


def register(app: FastAPI, ctx: Any) -> None:
    PREFS_KEY = ctx.PREFS_KEY
    _prefs = ctx._prefs
    store = ctx.store
    settings = ctx.settings
    adapters = ctx.adapters
    err = ctx.err
    _admin_user = ctx._admin_user
    _platform_admin_user = ctx._platform_admin_user
    _audit_api = ctx._audit_api
    _lead_gate = ctx._lead_gate

    @app.get("/api/v1/settings")
    def get_prefs() -> dict[str, Any]:
        p = _prefs()
        return {
            "gmail_check_minutes": p.get("gmail_check_minutes", 30),
            "radar_interval": p.get("radar_interval", "daily"),
            "notify_rules": p.get("notify_rules", []),
            "monthly_budget_usd": p.get("monthly_budget_usd"),
            "dashboard_widgets": p.get("dashboard_widgets", []),
            "sidebar_menu": p.get("sidebar_menu", {}),
        }

    @app.post("/api/v1/settings")
    def set_prefs(body: dict[str, Any], request: Request) -> JSONResponse:
        if _platform_admin_user(request) is None:
            return err(403, "forbidden", "admin role required", request)
        p = _prefs()
        minutes = int(body.get("gmail_check_minutes", p.get("gmail_check_minutes", 30)))
        if minutes not in (30, 60):
            return err(422, "validation", "gmail_check_minutes must be 30 or 60", request)
        interval = str(body.get("radar_interval", p.get("radar_interval", "daily")))
        if interval not in ("daily", "weekly"):
            return err(422, "validation", "radar_interval must be daily or weekly", request)
        p.update(
            {
                "gmail_check_minutes": minutes,
                "radar_interval": interval,
            }
        )
        if "notify_rules" in body:
            rules = body.get("notify_rules")
            if not isinstance(rules, list) or len(rules) > 100:
                return err(422, "validation", "notify_rules must be a list (max 100)", request)
            cleaned: list[dict[str, Any]] = []
            for rule in rules:
                if not isinstance(rule, dict):
                    return err(422, "validation", "notify_rules items must be objects", request)
                statuses = rule.get("statuses")
                if not isinstance(statuses, list) or not all(isinstance(s, str) for s in statuses):
                    return err(
                        422, "validation", "notify_rules[].statuses must be a string list", request
                    )
                tool = rule.get("tool")
                project = rule.get("project")
                cleaned.append(
                    {
                        "tool": str(tool)[:64] if tool else None,
                        "project": str(project)[:512] if project else None,
                        "statuses": [str(s)[:32] for s in statuses][:16],
                    }
                )
            p["notify_rules"] = cleaned
        if "monthly_budget_usd" in body:
            raw_budget = body.get("monthly_budget_usd")
            if raw_budget is None:
                p["monthly_budget_usd"] = None
            else:
                try:
                    budget_val = float(raw_budget)
                except (TypeError, ValueError):
                    return err(422, "validation", "monthly_budget_usd must be a number", request)
                if budget_val < 0:
                    return err(422, "validation", "monthly_budget_usd must be >= 0", request)
                p["monthly_budget_usd"] = budget_val
        if "dashboard_widgets" in body:
            widgets = body.get("dashboard_widgets")
            if not isinstance(widgets, list) or len(widgets) > 50:
                return err(422, "validation", "dashboard_widgets must be a list (max 50)", request)
            cleaned_widgets: list[dict[str, Any]] = []
            for w in widgets:
                if not isinstance(w, dict):
                    return err(
                        422,
                        "validation",
                        "dashboard_widgets items must be objects",
                        request,
                    )
                cleaned_widgets.append(
                    {
                        "id": str(w.get("id", ""))[:64],
                        "visible": bool(w.get("visible", True)),
                        "order": int(w.get("order", 0) or 0),
                    }
                )
            p["dashboard_widgets"] = cleaned_widgets
        if "sidebar_menu" in body:
            menu = body.get("sidebar_menu")
            if not isinstance(menu, dict) or len(menu) > 50:
                return err(422, "validation", "sidebar_menu must be an object", request)
            cleaned_menu: dict[str, bool] = {}
            for k, v in menu.items():
                key = str(k)[:64]
                if not key:
                    continue
                cleaned_menu[key] = bool(v)
            p["sidebar_menu"] = cleaned_menu
        store.set_state(PREFS_KEY, p)
        cronjob = _sync_cronjob(settings, store, p)
        _audit_api(request, "settings_update", "prefs", {"keys": sorted(body.keys())})
        return JSONResponse(content={"ok": True, "prefs": p, "cronjob": cronjob})

    @app.post("/api/v1/messages/{provider}/{source_id}/retry")
    def retry_message(provider: str, source_id: str, request: Request) -> JSONResponse:
        """메시지 재처리 큐잉 — 파이프라인 재실행을 유발하므로 lead 이상."""
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        if provider not in ("gmail", "slack", "telegram"):
            return err(400, "bad_provider", "unknown provider", request)
        if not store.has_message(provider, source_id):
            return err(404, "not_found", "message not found", request)
        store.finish_message(provider, source_id, "pending")
        return JSONResponse(content={"status": "pending"})

    @app.get("/api/v1/listeners/status")
    def listeners_status() -> dict[str, Any]:
        auto = store.get_automation()
        return {
            "auto_register": settings.auto_register,
            "gmail_enabled": bool(adapters.get("gmail")),
            "gmail_last_error": auto.get("last_gmail_error", ""),
            "slack_configured": bool(settings.slack_signing_secret),
            "telegram_configured": bool(settings.telegram_webhook_secret),
        }

    @app.get("/api/v1/events")
    def list_events(status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        return store.list_scheduled_events(status, min(max(limit, 1), 500))

    @app.get("/api/v1/notification-jobs")
    def notification_jobs(limit: int = 50) -> list[dict[str, Any]]:
        return store.list_notification_jobs(min(max(limit, 1), 200))

    @app.post("/api/v1/events/{event_id}/review")
    def review_event(event_id: str, body: EventReviewRequest, request: Request) -> JSONResponse:
        """needs_review/needs_confirmation/candidate 일정의 사람 검토.

        approve: 필드 수정분을 반영해 재검증 → Calendar 등록까지 수행.
        reject: 상태만 rejected로 전이. 외부 Calendar 쓰기 부수효과가 있으므로
        lead 이상."""
        from ...models import ExtractedEvent
        from ...sensors.event_validation import validate_event

        gate = _lead_gate(request)
        if gate is not None:
            return gate
        ev_row = store.get_scheduled_event(event_id)
        if ev_row is None:
            return err(404, "not_found", "scheduled event not found", request)
        reviewable = ("needs_review", "needs_confirmation", "candidate")
        if ev_row["status"] not in reviewable:
            return err(
                409,
                "invalid_state",
                f"event status is {ev_row['status']}, not reviewable",
                request,
            )
        # 원자 claim — 동시 승인 요청이 find_by_marker→create_event 사이에서
        # 경합해 캘린더 이벤트를 중복 생성하지 못하게 status를 'registering'으로
        # 전이한다. 실패/예외 시 아래에서 원래 상태로 복구한다.
        prev_status = str(ev_row["status"])
        stale_before = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
        if not store.claim_event_review(event_id, stale_before):
            return err(409, "invalid_state", "event is already being reviewed", request)
        if body.action == "reject":
            store.update_scheduled_event(event_id, status="rejected", reason="rejected_by_user")
            store.audit("", "api", "event_reject", event_id)
            logger.info("scheduled event %s rejected via api", event_id)
            return JSONResponse(content={"ok": True, "status": "rejected"})

        merged = {
            "title": body.title if body.title is not None else ev_row["title"],
            "start_at": body.start_at if body.start_at is not None else ev_row["start_at"],
            "end_at": body.end_at if body.end_at is not None else ev_row["end_at"],
            "timezone": body.timezone if body.timezone is not None else ev_row["timezone"],
            "location": body.location if body.location is not None else ev_row["location"],
            "summary": ev_row.get("summary") or "",
            "confidence": max(float(ev_row.get("confidence") or 0.0), 0.9),
            "is_ambiguous": False,
            "is_recurring": False,
        }
        try:
            cand = ExtractedEvent.model_validate(merged)
        except Exception as e:
            store.update_scheduled_event(event_id, status=prev_status, reason="")
            return err(422, "validation", f"invalid event fields: {str(e)[:200]}", request)
        ok, reason = validate_event(cand)
        if not ok:
            store.update_scheduled_event(
                event_id,
                title=cand.title,
                start_at=cand.start_at,
                end_at=cand.end_at,
                timezone=cand.timezone,
                location=cand.location,
                status=prev_status,
                reason=reason,
            )
            return err(422, "validation", f"still invalid: {reason}", request)

        cal = adapters.get("calendar")
        if cal is None or not hasattr(cal, "create_event"):
            store.update_scheduled_event(event_id, status=prev_status, reason="")
            return err(503, "no_adapter", "calendar adapter not configured", request)
        marker = f"review:{event_id}"
        try:
            existing = cal.find_by_marker(marker) if hasattr(cal, "find_by_marker") else None
            if existing:
                cal_event_id = existing
            else:
                cal_event_id = cal.create_event(
                    title=cand.title,
                    start_at=cand.start_at,
                    end_at=cand.end_at,
                    timezone=cand.timezone,
                    location=cand.location,
                    marker=marker,
                )
        except Exception as e:
            # 캘린더 실패 — claim 해제(원래 검토 상태로 복구) 후 에러 반환
            store.update_scheduled_event(event_id, status=prev_status, reason="")
            store.audit("", "api", "event_approve_error", event_id, {"error": str(e)[:200]})
            logger.warning("scheduled event %s approve failed: %s", event_id, str(e)[:200])
            return err(502, "calendar_error", "캘린더 등록에 실패했습니다", request)
        store.update_scheduled_event(
            event_id,
            title=cand.title,
            start_at=cand.start_at,
            end_at=cand.end_at,
            timezone=cand.timezone,
            location=cand.location,
            status="registered",
            reason="",
            calendar_event_id=cal_event_id,
        )
        store.audit(
            "",
            "api",
            "event_approve",
            event_id,
            {"calendar_event_id": cal_event_id},
        )
        logger.info("scheduled event %s approved -> calendar_event=%s", event_id, cal_event_id)
        return JSONResponse(
            content={"ok": True, "status": "registered", "calendar_event_id": cal_event_id}
        )
