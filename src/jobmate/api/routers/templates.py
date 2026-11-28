"""Template marketplace routes — export/import/share/catalog (spec 006 US3).

- export/import/share/catalog는 lead 이상(_lead_gate) — builder의
  custom_workflows/playbooks와 동일한 권한 모델.
- GET /api/v1/templates/shared/{token}은 토큰 자체가 capability인 공개
  경로 — /api/v1/share/{token}과 동일하게 인증 예외가 필요하다
  (app.py 미들웨어 exemption — 등록 시 함께 추가해야 무인증 조회 가능).
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ...services import templates as tpl
from ...workflows import WORKFLOWS
from ..session_stats import _parse_session_ts
from ..support import _BUILTIN_WF_KEYS

logger = logging.getLogger("jobmate.api")

_SHARE_DEFAULT_DAYS = 30
_SHARE_MAX_DAYS = 90


class TemplateExportRequest(BaseModel):
    kind: Literal["workflow", "playbook", "bundle"]
    ref_id: str = Field(min_length=1, max_length=200)


class TemplateShareRequest(BaseModel):
    kind: Literal["workflow", "playbook", "bundle"]
    ref_id: str = Field(min_length=1, max_length=200)
    expires_days: int = Field(default=_SHARE_DEFAULT_DAYS, ge=1, le=_SHARE_MAX_DAYS)


def register(app: FastAPI, ctx: Any) -> None:
    store = ctx.store
    err = ctx.err
    _lead_gate = ctx._lead_gate
    _actor_label = ctx._actor_label
    _audit_api = ctx._audit_api

    def _template_err(e: tpl.TemplateError, request: Request) -> JSONResponse:
        return err(e.status, e.code, e.message, request, extra=e.details or None)

    # ---- 1. EXPORT ----
    @app.post("/api/v1/templates/export")
    def export_template(body: TemplateExportRequest, request: Request) -> JSONResponse:
        """{kind, ref_id} → format_version "1.0" TemplateBundle.

        spec은 WorkflowSpec 검증을 통과한 최신 리비전(커스텀) 또는 내장 spec.
        평문 시크릿 패턴 발견 시 warnings를 포함하되 export는 허용(FR-013)."""
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        try:
            bundle = tpl.export_template(
                store, kind=body.kind, ref_id=body.ref_id, builtin_keys=_BUILTIN_WF_KEYS
            )
        except tpl.TemplateError as e:
            return _template_err(e, request)
        _audit_api(
            request,
            "template_export",
            f"{body.kind}:{bundle.get('name')}",
            {"kind": body.kind, "warnings": len(bundle.get("warnings") or [])},
        )
        logger.info(
            "template exported kind=%s name=%s warnings=%d",
            body.kind,
            bundle.get("name"),
            len(bundle.get("warnings") or []),
        )
        return JSONResponse(content=bundle)

    # ---- 2. IMPORT ----
    @app.post("/api/v1/templates/import", status_code=201)
    async def import_template(request: Request) -> JSONResponse:
        """TemplateBundle JSON → 스키마 검증 → 이름 충돌 suffix → 등록.

        workflow 먼저, playbook 나중. 실패 시 부분 등록 없이 롤백(FR-014).
        시크릿 패턴·builtin 이름·잘못된 spec은 422 + 필드 에러."""
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        try:
            payload = await request.json()
        except Exception:
            return err(422, "validation", "request body must be valid JSON", request)
        # 비정상 대형 페이로드 방어 — 번들은 spec+플레이북 몇 개 분량.
        try:
            raw_len = len(json.dumps(payload, ensure_ascii=False, default=str))
        except Exception:
            raw_len = 0
        if raw_len > 512 * 1024:
            return err(422, "validation", "template bundle exceeds 512KB", request)
        try:
            result = tpl.import_template(
                store,
                payload,
                builtin_keys=_BUILTIN_WF_KEYS,
                created_by=_actor_label(request),
            )
        except tpl.TemplateError as e:
            return _template_err(e, request)
        names = [str(i.get("name") or "") for i in result.get("imported", [])]
        _audit_api(
            request,
            "template_import",
            ",".join(names)[:200],
            {"kind": payload.get("kind"), "imported": len(names)},
        )
        logger.info("template imported kind=%s names=%s", payload.get("kind"), names)
        return JSONResponse(status_code=201, content=result)

    # ---- 3. SHARE (share_links kind='template', 기본 30일/최대 90일) ----
    @app.post("/api/v1/templates/share", status_code=201)
    def share_template(body: TemplateShareRequest, request: Request) -> JSONResponse:
        """export 번들을 share_links(kind='template')에 스냅샷으로 저장."""
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        try:
            bundle = tpl.export_template(
                store, kind=body.kind, ref_id=body.ref_id, builtin_keys=_BUILTIN_WF_KEYS
            )
        except tpl.TemplateError as e:
            return _template_err(e, request)
        token = secrets.token_urlsafe(24)
        expires_at = (
            datetime.now(UTC) + timedelta(days=min(body.expires_days, _SHARE_MAX_DAYS))
        ).isoformat()
        store.create_share_link(
            token,
            "template",
            f"{body.kind}:{bundle.get('name')}",
            bundle,
            _actor_label(request),
            expires_at,
        )
        _audit_api(
            request,
            "template_share",
            f"{body.kind}:{bundle.get('name')}",
            {"expires_days": body.expires_days},
        )
        logger.info("template share link created kind=%s name=%s", body.kind, bundle.get("name"))
        return JSONResponse(
            status_code=201,
            content={
                "url": f"/api/v1/templates/shared/{token}",
                "token": token,
                "expires_at": expires_at,
            },
        )

    # ---- 4. SHARED GET (공개 — 토큰이 capability) ----
    @app.get("/api/v1/templates/shared/{token}")
    def shared_template(token: str, request: Request) -> JSONResponse:
        """공유된 템플릿 번들 조회 — 만료 410 / 미존재·타 kind 404.

        인증 미들웨어 exemption이 필요하다(app.py — /api/v1/share/*와 동일 패턴).
        """
        link = store.get_share_link(token)
        if link is None or str(link.get("kind") or "") != "template":
            return err(404, "not_found", "share link not found", request)
        exp_dt = _parse_session_ts(link.get("expires_at") or "")
        # 만료 시각이 없거나 파싱 불가면 만료로 취급(DB 변조 방어 — share_get과 동일).
        if exp_dt is None or exp_dt <= datetime.now(UTC):
            return err(410, "gone", "share link expired", request)
        return JSONResponse(
            content={
                "kind": "template",
                "created_at": link.get("created_at"),
                "expires_at": link.get("expires_at"),
                "template": link.get("payload"),
            }
        )

    # ---- 5. CATALOG (내장 + 커스텀 + 플레이북 — 이 인스턴스 등록 가능 목록) ----
    @app.get("/api/v1/templates/catalog")
    def template_catalog(request: Request) -> JSONResponse:
        """FR-016 — builtin workflow + 커스텀 workflow + playbook 목록."""
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        items: list[dict[str, Any]] = []
        for key, spec in WORKFLOWS.items():
            items.append(
                {
                    "kind": "builtin",
                    "name": key,
                    "version": getattr(spec, "version", ""),
                    "description": getattr(spec, "description", ""),
                }
            )
        for row in store.list_custom_workflows():
            spec = row.get("spec") if isinstance(row.get("spec"), dict) else {}
            items.append(
                {
                    "kind": "custom",
                    "name": row.get("name"),
                    "version": spec.get("version", ""),
                    "description": spec.get("description", ""),
                    "latest_revision": int(row.get("latest_revision") or 0),
                }
            )
        for pb in store.list_playbooks():
            items.append(
                {
                    "kind": "playbook",
                    "id": pb.get("id"),
                    "name": pb.get("name"),
                    "workflow": pb.get("workflow"),
                }
            )
        return JSONResponse(content={"items": items})
