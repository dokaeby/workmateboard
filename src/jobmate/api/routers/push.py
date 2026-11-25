"""Web Push 구독 라우트 — vapid-key/subscribe/unsubscribe/test."""

from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ..support import _endpoint_fp

logger = logging.getLogger("jobmate.api")


def register(app: FastAPI, ctx: Any) -> None:
    store = ctx.store
    err = ctx.err
    adapters = ctx.adapters
    _audit_api = ctx._audit_api
    _lead_gate = ctx._lead_gate

    # ---- Web Push 구독 ----
    @app.get("/api/v1/push/vapid-key")
    def vapid_key() -> dict[str, str]:
        return {"public_key": os.environ.get("VAPID_PUBLIC_KEY", "")}

    @app.post("/api/v1/push/subscribe")
    def push_subscribe(body: dict[str, Any], request: Request) -> Any:
        from ...adapters.push import valid_push_subscription

        endpoint = body.get("endpoint", "")
        keys = body.get("keys") or {}
        if not valid_push_subscription(endpoint, keys):
            return err(422, "invalid_subscription", "invalid push subscription", request)
        store.save_push_subscription(endpoint, keys)
        _audit_api(request, "push_subscribe", _endpoint_fp(endpoint))
        logger.info("push subscribed: sub=%s", _endpoint_fp(endpoint))
        return {"status": "subscribed"}

    @app.post("/api/v1/push/unsubscribe")
    def push_unsubscribe(body: dict[str, Any], request: Request) -> dict[str, str]:
        endpoint = body.get("endpoint", "")
        store.delete_push_subscription(endpoint)
        _audit_api(request, "push_unsubscribe", _endpoint_fp(endpoint))
        if endpoint:
            logger.info("push unsubscribed: sub=%s", _endpoint_fp(endpoint))
        return {"status": "unsubscribed"}

    @app.post("/api/v1/push/test")
    def push_test(request: Request) -> JSONResponse:
        """구독된 모든 디바이스로 테스트 푸시 발송 — 연결 검증용. lead 이상."""
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        subs = store.list_push_subscriptions()
        if not subs:
            return JSONResponse(content={"status": "no_subscriptions", "sent": 0})
        from ...engine.functions import send_web_push_text

        send_web_push_text(adapters, "jobMate 테스트 알림", "Web Push 연결이 정상입니다.", "/")
        logger.info("push test sent: subs=%d", len(subs))
        return JSONResponse(content={"status": "sent", "sent": len(subs)})
