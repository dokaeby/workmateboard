"""spec 008 US3 — 리포트 구독 라우트.

GET/PATCH/POST/DELETE/test 전부 lead 이상(_lead_gate) — target URL은
slack/webhook 채널에서 자격증명이므로 member에게조차 원문을 노출하지
않는다(spec 004 webhook 구독 목록이 admin 게이트인 것과 같은 이유).
mutation은 _audit_api로 감사 로그를 남긴다.

webhook 채널은 구독 생성 시 HMAC 서명 시크릿을 발급해 secret_enc에
암호화 저장하고, 원문은 이 POST 응답에서 1회만 반환한다(이후 조회·로그
노출 금지). target URL은 slack/webhook 모두 저장·발송 시점에 SSRF 가드
(services.reporting.validate_fields + webutil.validate_outbound_url +
webhooks.webhook_url_allowed)를 통과해야 한다.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ...reports import build_report
from ...security import encrypt_str
from ...services import reporting as reporting_svc

logger = logging.getLogger("jobmate.api")


class ReportSubscriptionCreateRequest(BaseModel):
    kind: str = Field(min_length=1, max_length=20)
    channel: str = Field(min_length=1, max_length=20)
    target: str = Field(min_length=1, max_length=2000)
    enabled: bool = True


class ReportSubscriptionPatchRequest(BaseModel):
    kind: str | None = Field(default=None, min_length=1, max_length=20)
    channel: str | None = Field(default=None, min_length=1, max_length=20)
    target: str | None = Field(default=None, min_length=1, max_length=2000)
    enabled: bool | None = None


def _sub_out(sub: dict[str, Any]) -> dict[str, Any]:
    """API 응답 — secret_enc 등 내부 필드 제외."""
    return {
        "id": sub.get("id"),
        "kind": sub.get("kind"),
        "channel": sub.get("channel"),
        "target": sub.get("target"),
        "enabled": bool(sub.get("enabled")),
        "last_sent_at": sub.get("last_sent_at"),
        "created_by": sub.get("created_by") or "",
        "created_at": sub.get("created_at"),
        "updated_at": sub.get("updated_at"),
    }


def register(app: FastAPI, ctx: Any) -> None:
    store = ctx.store
    settings = ctx.settings
    err = ctx.err
    _lead_gate = ctx._lead_gate
    _audit_api = ctx._audit_api
    _actor_label = ctx._actor_label

    @app.get("/api/v1/reports/subscriptions")
    def list_report_subscriptions(request: Request) -> JSONResponse:
        # target은 webhook/slack 자격증명 — member에게도 원문 비노출.
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        subs = store.list_report_subscriptions()
        return JSONResponse(content={"items": [_sub_out(s) for s in subs]})

    @app.post("/api/v1/reports/subscriptions", status_code=201)
    def create_report_subscription(
        body: ReportSubscriptionCreateRequest, request: Request
    ) -> JSONResponse:
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        try:
            kind, channel, target = reporting_svc.validate_fields(
                body.kind, body.channel, body.target
            )
        except ValueError as e:
            return err(422, "validation", str(e), request)
        # webhook 채널만 HMAC 서명 시크릿 발급 — email/slack은 URL 자체가
        # credential이라 별도 시크릿이 없다.
        secret = ""
        secret_enc = ""
        if channel == "webhook":
            secret = secrets.token_urlsafe(32)
            secret_enc = encrypt_str(secret)
        sub = store.create_report_subscription(
            kind=kind,
            channel=channel,
            target=target,
            enabled=body.enabled,
            created_by=_actor_label(request),
            secret_enc=secret_enc,
        )
        _audit_api(
            request,
            "report_subscription_create",
            str(sub.get("id") or ""),
            {"kind": kind, "channel": channel},
        )
        logger.info(
            "report subscription created id=%s kind=%s channel=%s",
            sub.get("id"),
            kind,
            channel,
        )
        content: dict[str, Any] = {"item": _sub_out(sub)}
        if secret:
            # 시크릿 원문은 이 응답에서 1회만 — 이후 조회·로그에 나타나지 않는다.
            content["secret"] = secret
        return JSONResponse(status_code=201, content=content)

    @app.patch("/api/v1/reports/subscriptions/{sub_id}")
    def patch_report_subscription(
        sub_id: str, body: ReportSubscriptionPatchRequest, request: Request
    ) -> JSONResponse:
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        existing = store.get_report_subscription(sub_id)
        if existing is None:
            return err(404, "not_found", "report subscription not found", request)
        sent = body.model_fields_set
        # 변경 후 상태 기준으로 target URL을 재검증 — kind/channel/target이
        # 함께 바뀌는 경우도 있으므로 병합 후 validate_fields를 한 번 돌린다.
        merged = {
            "kind": str(body.kind) if "kind" in sent else str(existing.get("kind") or ""),
            "channel": (
                str(body.channel) if "channel" in sent else str(existing.get("channel") or "")
            ),
            "target": (str(body.target) if "target" in sent else str(existing.get("target") or "")),
        }
        try:
            kind, channel, target = reporting_svc.validate_fields(
                merged["kind"], merged["channel"], merged["target"]
            )
        except ValueError as e:
            return err(422, "validation", str(e), request)
        # webhook 채널인데 시크릿이 없으면(구버전 행/채널 전환) 새로 발급.
        secret = ""
        secret_enc: str | None = None
        if channel == "webhook" and not str(existing.get("secret_enc") or ""):
            secret = secrets.token_urlsafe(32)
            secret_enc = encrypt_str(secret)
        elif channel != "webhook" and str(existing.get("secret_enc") or ""):
            # webhook → 다른 채널 전환 시 시크릿 폐기(불필요한 credential 잔존 방지).
            secret_enc = ""
        fields: dict[str, Any] = {}
        if "kind" in sent:
            fields["kind"] = kind
        if "channel" in sent:
            fields["channel"] = channel
        if "target" in sent:
            fields["target"] = target
        if "enabled" in sent:
            fields["enabled"] = body.enabled
        if secret_enc is not None:
            fields["secret_enc"] = secret_enc
        if not fields:
            return err(422, "validation", "no fields to update", request)
        sub = store.update_report_subscription(sub_id, **fields)
        if sub is None:
            return err(422, "validation", "invalid subscription fields", request)
        _audit_api(request, "report_subscription_update", sub_id, {"fields": sorted(fields)})
        logger.info("report subscription updated id=%s fields=%s", sub_id, sorted(fields))
        content: dict[str, Any] = {"item": _sub_out(sub)}
        if secret:
            content["secret"] = secret
        return JSONResponse(content=content)

    @app.delete("/api/v1/reports/subscriptions/{sub_id}")
    def delete_report_subscription(sub_id: str, request: Request) -> JSONResponse:
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        if not store.delete_report_subscription(sub_id):
            return err(404, "not_found", "report subscription not found", request)
        _audit_api(request, "report_subscription_delete", sub_id, {})
        logger.info("report subscription deleted id=%s", sub_id)
        return JSONResponse(content={"ok": True})

    @app.post("/api/v1/reports/subscriptions/{sub_id}/test")
    def test_report_subscription(sub_id: str, request: Request) -> JSONResponse:
        """테스트 리포트 즉시 생성 + 해당 구독으로 1회 발송.

        kind의 실제 리포트를 만들어 발송한다 — 채널 백엔드 미설정 등
        실패는 200 + {ok:false, detail}로 반환해 프론트가 사유를 표시한다."""
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        sub = store.get_report_subscription(sub_id)
        if sub is None:
            return err(404, "not_found", "report subscription not found", request)
        kind = str(sub.get("kind") or "")
        try:
            report = build_report(store, kind, settings)
        except Exception as e:
            logger.warning("report test build failed sub=%s: %s", sub_id, str(e)[:200])
            return JSONResponse(content={"ok": False, "detail": "report_build_failed"})
        try:
            ok, detail = reporting_svc.deliver_subscription(store, settings, sub, report)
        except Exception as e:
            logger.warning("report test delivery crashed sub=%s: %s", sub_id, str(e)[:200])
            ok, detail = False, "delivery_error"
        _audit_api(
            request,
            "report_subscription_test",
            sub_id,
            {"kind": kind, "channel": sub.get("channel"), "ok": ok},
        )
        logger.info("report subscription test sub=%s ok=%s detail=%s", sub_id, ok, detail)
        return JSONResponse(content={"ok": ok, "detail": detail})
