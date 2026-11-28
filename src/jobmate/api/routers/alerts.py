"""spec 006 US1 — 알림 규칙 CRUD + 발사 이력 라우트.

GET은 인증된 member 이상 누구나 조회 가능(조직 스코프는 store의
orgctx 필터가 강제). POST/PATCH/DELETE는 lead 이상(_lead_gate) —
생성/수정/삭제는 _audit_api로 감사 로그를 남긴다(FR-006).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ...services import alert_rules as alert_svc

logger = logging.getLogger("jobmate.api")


class AlertRuleCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    condition: dict[str, Any]
    channels: list[Any] = Field(default_factory=list)
    cooldown_minutes: int = Field(default=60, ge=0, le=10080)
    enabled: bool = True


class AlertRulePatchRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    condition: dict[str, Any] | None = None
    channels: list[Any] | None = None
    cooldown_minutes: int | None = Field(default=None, ge=0, le=10080)
    enabled: bool | None = None


def _rule_json(rule: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": rule.get("id"),
        "name": rule.get("name"),
        "enabled": bool(rule.get("enabled")),
        "condition": rule.get("condition") or {},
        "channels": rule.get("channels") or [],
        "cooldown_minutes": int(rule.get("cooldown_minutes") or 0),
        "last_fired_at": rule.get("last_fired_at"),
        "created_by": rule.get("created_by") or "",
        "created_at": rule.get("created_at"),
    }


def _fire_json(fire: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": fire.get("id"),
        "rule_id": fire.get("rule_id"),
        "event_fingerprint": fire.get("event_fingerprint"),
        "status": fire.get("status"),
        "channels": fire.get("channels") or [],
        "created_at": fire.get("created_at"),
    }


def register(app: FastAPI, ctx: Any) -> None:
    store = ctx.store
    err = ctx.err
    _lead_gate = ctx._lead_gate
    _audit_api = ctx._audit_api
    _actor_label = ctx._actor_label

    @app.get("/api/v1/alert-rules")
    def list_alert_rules(request: Request, enabled: bool | None = None) -> JSONResponse:
        rules = store.list_alert_rules(enabled_only=bool(enabled))
        return JSONResponse(content={"items": [_rule_json(r) for r in rules]})

    @app.post("/api/v1/alert-rules", status_code=201)
    def create_alert_rule(body: AlertRuleCreateRequest, request: Request) -> JSONResponse:
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        try:
            condition = alert_svc.validate_condition(body.condition)
            channels = alert_svc.validate_channels(body.channels)
        except ValueError as e:
            return err(422, "validation", str(e), request)
        rule = store.create_alert_rule(
            name=body.name,
            condition=condition,
            channels=channels,
            cooldown_minutes=body.cooldown_minutes,
            enabled=body.enabled,
            created_by=_actor_label(request),
        )
        _audit_api(
            request,
            "alert_rule_create",
            str(rule.get("id") or ""),
            {
                "name": body.name,
                "condition": condition,
                "channels": [c if isinstance(c, str) else c.get("type") for c in channels],
            },
        )
        logger.info("alert rule created id=%s name=%s", rule.get("id"), body.name[:80])
        return JSONResponse(status_code=201, content=_rule_json(rule))

    @app.get("/api/v1/alert-rules/{rule_id}")
    def get_alert_rule(rule_id: str, request: Request) -> JSONResponse:
        rule = store.get_alert_rule(rule_id)
        if rule is None:
            return err(404, "not_found", "alert rule not found", request)
        return JSONResponse(content=_rule_json(rule))

    @app.patch("/api/v1/alert-rules/{rule_id}")
    def patch_alert_rule(
        rule_id: str, body: AlertRulePatchRequest, request: Request
    ) -> JSONResponse:
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        existing = store.get_alert_rule(rule_id)
        if existing is None:
            return err(404, "not_found", "alert rule not found", request)
        sent = body.model_fields_set
        condition: dict[str, Any] | None = None
        if "condition" in sent:
            if body.condition is None:
                return err(422, "validation", "condition must be an object", request)
            try:
                condition = alert_svc.validate_condition(body.condition)
            except ValueError as e:
                return err(422, "validation", str(e), request)
        channels: list[Any] | None = None
        if "channels" in sent:
            try:
                channels = alert_svc.validate_channels(body.channels or [])
            except ValueError as e:
                return err(422, "validation", str(e), request)
        ok = store.update_alert_rule(
            rule_id,
            name=body.name if "name" in sent else None,
            enabled=body.enabled if "enabled" in sent else None,
            condition=condition,
            channels=channels,
            cooldown_minutes=(body.cooldown_minutes if "cooldown_minutes" in sent else None),
        )
        if not ok:
            return err(404, "not_found", "alert rule not found", request)
        _audit_api(request, "alert_rule_update", rule_id, {"fields": sorted(sent)})
        logger.info("alert rule updated id=%s fields=%s", rule_id, sorted(sent))
        rule = store.get_alert_rule(rule_id)
        return JSONResponse(content=_rule_json(rule or {"id": rule_id}))

    @app.delete("/api/v1/alert-rules/{rule_id}")
    def delete_alert_rule(rule_id: str, request: Request) -> JSONResponse:
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        if not store.delete_alert_rule(rule_id):
            return err(404, "not_found", "alert rule not found", request)
        _audit_api(request, "alert_rule_delete", rule_id, {})
        logger.info("alert rule deleted id=%s", rule_id)
        return JSONResponse(content={"ok": True})

    @app.get("/api/v1/alert-rules/{rule_id}/fires")
    def list_alert_rule_fires(rule_id: str, request: Request, limit: int = 100) -> JSONResponse:
        if store.get_alert_rule(rule_id) is None:
            return err(404, "not_found", "alert rule not found", request)
        fires = store.list_alert_fires(rule_id=rule_id, limit=limit)
        return JSONResponse(content={"items": [_fire_json(f) for f in fires]})

    @app.get("/api/v1/alert-fires")
    def list_alert_fires(request: Request, limit: int = 100) -> JSONResponse:
        fires = store.list_alert_fires(limit=limit)
        return JSONResponse(content={"items": [_fire_json(f) for f in fires]})
