"""아웃바운드 웹훅 구독 관리 라우트 — spec 004 US1 (FR-004/FR-005/FR-009).

Prefix는 /api/v1/webhook-subscriptions — /api/v1/webhooks/*는 인바운드
Slack/Telegram webhook이 이미 사용한다(routers/messaging.py).

수신자 서명 검증 규약(services/webhooks.py와 동일):
  X-JobMate-Timestamp: <unix seconds>
  X-JobMate-Signature: t=<unix>,v1=<hex>
  v1 = hex( HMAC_SHA256( secret, "<t>.<raw_body_utf8>" ) )
  수신자는 "{t}." + 요청 본문 원문 UTF-8 바이트열을 secret 키로 HMAC-SHA256
  해 hex로 비교하면 된다(ー 비교 권장).

게이트: 조직 admin(org_admin/org_owner) 또는 플랫폼 admin — ctx._admin_gate.
모든 mutation은 store.audit(webhook_create|webhook_update|webhook_delete|
webhook_rotate_secret)로 기록한다. 시크릿 원문은 create/rotate 응답에
1회만 포함되고 이후 API 응답·로그·delivery payload에 나타나지 않는다.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ... import webutil
from ...security import encrypt_str
from ...services import webhooks as webhooks_svc

logger = logging.getLogger("jobmate.api")

_MAX_URL_LEN = 2000
_MAX_EVENTS = 100


class WebhookSubscriptionCreate(BaseModel):
    url: str = Field(min_length=1, max_length=_MAX_URL_LEN)
    events: list[str] = Field(default_factory=list, max_length=_MAX_EVENTS)
    enabled: bool = True


class WebhookSubscriptionPatch(BaseModel):
    url: str | None = Field(default=None, max_length=_MAX_URL_LEN)
    events: list[str] | None = Field(default=None, max_length=_MAX_EVENTS)
    enabled: bool | None = None


def _public_sub(sub: dict[str, Any]) -> dict[str, Any]:
    """API 응답용 — secret_enc 등 내부 필드 제거."""
    return {
        "id": sub.get("id"),
        "url": sub.get("url"),
        "events": sub.get("events") or [],
        "enabled": bool(sub.get("enabled")),
        "created_by": sub.get("created_by") or "",
        "created_at": sub.get("created_at"),
        "updated_at": sub.get("updated_at"),
        "last_delivery_at": sub.get("last_delivery_at"),
    }


def _clean_events(events: list[str] | None) -> list[str] | None:
    if events is None:
        return None
    out: list[str] = []
    for e in events:
        s = str(e or "").strip()[:100]
        if s and s not in out:
            out.append(s)
    return out


def register(app: FastAPI, ctx: Any) -> None:
    store = ctx.store
    err = ctx.err
    _admin_gate = ctx._admin_gate
    _audit_api = ctx._audit_api
    _actor_label = ctx._actor_label

    def _validate_url(request: Request, url: str) -> str | JSONResponse:
        """FR-009 — 구독 URL 정책 검증. 위반 시 422 (outbound 전).

        validate_outbound_url(SSRF 가드) 통과 후 loopback 포트
        allowlist(JOBMATE_WEBHOOK_LOOPBACK_PORTS)도 검사한다 —
        localhost:6379 같은 내부 TCP 서비스로의 SSRF 차단."""
        try:
            checked = webutil.validate_outbound_url(url, what="webhook_url")
        except webutil.FetchError as e:
            return err(422, "validation", f"webhook url rejected: {e}", request)
        port_err = webhooks_svc.webhook_url_allowed(checked)
        if port_err is not None:
            return err(422, "validation", f"webhook url rejected: {port_err}", request)
        return checked

    @app.post("/api/v1/webhook-subscriptions", status_code=201)
    def create_subscription(body: WebhookSubscriptionCreate, request: Request) -> JSONResponse:
        gate = _admin_gate(request)
        if gate is not None:
            return gate
        url = _validate_url(request, body.url)
        if isinstance(url, JSONResponse):
            return url
        secret = secrets.token_urlsafe(32)
        sub = store.create_webhook_subscription(
            url=url,
            secret_enc=encrypt_str(secret),
            events=_clean_events(body.events) or [],
            created_by=_actor_label(request),
            enabled=body.enabled,
        )
        _audit_api(
            request,
            "webhook_create",
            str(sub.get("id") or ""),
            {"url": url, "events": sub.get("events") or []},
        )
        logger.info("webhook subscription created id=%s", sub.get("id"))
        # secret은 이 응답에서 1회만 반환 — 이후 조회 불가(FR-005).
        return JSONResponse(
            status_code=201,
            content={"subscription": _public_sub(sub), "secret": secret},
        )

    @app.get("/api/v1/webhook-subscriptions")
    def list_subscriptions(request: Request) -> JSONResponse:
        gate = _admin_gate(request)
        if gate is not None:
            return gate
        subs = store.list_webhook_subscriptions()
        return JSONResponse(content={"subscriptions": [_public_sub(s) for s in subs]})

    @app.get("/api/v1/webhook-subscriptions/{sub_id}")
    def get_subscription(sub_id: str, request: Request) -> JSONResponse:
        gate = _admin_gate(request)
        if gate is not None:
            return gate
        sub = store.get_webhook_subscription(sub_id)
        if sub is None:
            return err(404, "not_found", "webhook subscription not found", request)
        return JSONResponse(content={"subscription": _public_sub(sub)})

    @app.patch("/api/v1/webhook-subscriptions/{sub_id}")
    def update_subscription(
        sub_id: str, body: WebhookSubscriptionPatch, request: Request
    ) -> JSONResponse:
        gate = _admin_gate(request)
        if gate is not None:
            return gate
        sub = store.get_webhook_subscription(sub_id)
        if sub is None:
            return err(404, "not_found", "webhook subscription not found", request)
        url: str | None = None
        if body.url is not None:
            checked = _validate_url(request, body.url)
            if isinstance(checked, JSONResponse):
                return checked
            url = checked
        events = _clean_events(body.events)
        if not store.update_webhook_subscription(
            sub_id, url=url, events=events, enabled=body.enabled
        ):
            return err(422, "validation", "no fields to update", request)
        _audit_api(
            request,
            "webhook_update",
            sub_id,
            {
                "url": url,
                "events": events,
                "enabled": body.enabled,
            },
        )
        updated = store.get_webhook_subscription(sub_id) or sub
        return JSONResponse(content={"subscription": _public_sub(updated)})

    @app.delete("/api/v1/webhook-subscriptions/{sub_id}")
    def delete_subscription(sub_id: str, request: Request) -> JSONResponse:
        gate = _admin_gate(request)
        if gate is not None:
            return gate
        if not store.delete_webhook_subscription(sub_id):
            return err(404, "not_found", "webhook subscription not found", request)
        _audit_api(request, "webhook_delete", sub_id, {})
        logger.info("webhook subscription deleted id=%s", sub_id)
        return JSONResponse(content={"deleted": True, "id": sub_id})

    @app.post("/api/v1/webhook-subscriptions/{sub_id}/rotate-secret")
    def rotate_secret(sub_id: str, request: Request) -> JSONResponse:
        gate = _admin_gate(request)
        if gate is not None:
            return gate
        if store.get_webhook_subscription(sub_id) is None:
            return err(404, "not_found", "webhook subscription not found", request)
        secret = secrets.token_urlsafe(32)
        if not store.rotate_webhook_secret(sub_id, encrypt_str(secret)):
            return err(404, "not_found", "webhook subscription not found", request)
        _audit_api(request, "webhook_rotate_secret", sub_id, {})
        logger.info("webhook secret rotated id=%s", sub_id)
        # 새 시크릿도 이 응답에서 1회만 반환(FR-005).
        return JSONResponse(content={"id": sub_id, "secret": secret})

    @app.post("/api/v1/webhook-subscriptions/{sub_id}/test")
    def test_subscription(sub_id: str, request: Request) -> JSONResponse:
        """샘플 webhook.test delivery를 적재 — 실제 전송은 다음
        automation cycle의 delivery 잡이 수행한다(멱등 event_id)."""
        gate = _admin_gate(request)
        if gate is not None:
            return gate
        sub = store.get_webhook_subscription(sub_id)
        if sub is None:
            return err(404, "not_found", "webhook subscription not found", request)
        event_id = webhooks_svc.enqueue_test_event(store, sub)
        if not event_id:
            return err(500, "internal_error", "failed to enqueue test delivery", request)
        _audit_api(request, "webhook_test", sub_id, {"event_id": event_id})
        return JSONResponse(
            status_code=202, content={"delivery_event_id": event_id, "status": "pending"}
        )

    @app.get("/api/v1/webhook-subscriptions/{sub_id}/deliveries")
    def list_deliveries(sub_id: str, request: Request, limit: int = 50) -> JSONResponse:
        gate = _admin_gate(request)
        if gate is not None:
            return gate
        if store.get_webhook_subscription(sub_id) is None:
            return err(404, "not_found", "webhook subscription not found", request)
        deliveries = store.list_webhook_deliveries(sub_id, limit=limit)
        return JSONResponse(content={"deliveries": deliveries})
