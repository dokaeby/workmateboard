"""Permission routes — agent capability 권한 조회/설정 (paperclip spec 3차 D).

GET/PUT /api/v1/management/agents/{id}/permissions + effective 판정 조회.
판정 규칙(deny 우선, scope precedence, 기본 deny capability)은
services/permissions.py가 담당하고, 서버는 모든 agent-actor 실행 경로에서
require_permission으로 재검증한다 — UI 버튼 숨김은 편의 기능일 뿐이다.

인증: PUT은 '명시적' lead+(_explicit_lead_user) — 머신 API 키는
_lead_user에서는 admin이지만 agent runner가 자기/타인 permission을
지우는 self-escalation 경로가 되므로 명시적 세션 lead/admin 또는
인증 비활성 로컬 모드만 허용한다. GET list/effective는 member+ —
/api/* 미들웨어가 인증을 강제하므로 추가 게이트 없이 읽기만 허용한다.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ...services import permissions as perm_svc
from ...services.agent_management import ManagementError
from ..schemas import AgentPermissionsPutRequest

logger = logging.getLogger("jobmate.api")


def register(app: FastAPI, ctx: Any) -> None:
    store = ctx.store
    err = ctx.err
    _explicit_lead_user = ctx._explicit_lead_user
    _audit_api = ctx._audit_api
    _actor_label = ctx._actor_label

    @app.get("/api/v1/management/agents/{agent_id}/permissions")
    def get_agent_permissions(agent_id: str, request: Request) -> JSONResponse:
        """agent의 permission 규칙 목록(member+) — 프론트엔드는
        {"permissions": [...]} 형태를 기대한다."""
        if store.get_agent(agent_id) is None:
            return err(404, "not_found", "agent not found", request)
        rules = store.list_agent_permissions(agent_id)
        return JSONResponse(content={"agent_id": agent_id, "permissions": rules})

    @app.put("/api/v1/management/agents/{agent_id}/permissions")
    def put_agent_permissions(
        agent_id: str, body: AgentPermissionsPutRequest, request: Request
    ) -> JSONResponse:
        """permission 규칙 전체 교체(명시적 lead+) — body.permissions 배열이
        새 집합의 전부다. 검증 실패는 422, agent 미존재는 404.

        머신 API 키는 _lead_user상 admin이지만 여기서는 거부한다 —
        agent runner가 API 키로 자기 permission deny 규칙을 지우는
        self-escalation을 막기 위해 _explicit_lead_user(세션 lead/admin
        또는 인증 비활성 로컬 모드)만 허용한다."""
        if _explicit_lead_user(request) is None:
            return err(403, "forbidden", "explicit lead session required", request)
        try:
            rules = perm_svc.validate_permission_rules(
                store,
                agent_id,
                [r.model_dump() for r in body.permissions],
            )
            saved = store.replace_agent_permissions(agent_id, rules)
        except ManagementError as e:
            return err(e.status_code, e.code, e.message, request)
        _audit_api(
            request,
            "mgmt_agent_permissions_replace",
            agent_id,
            {"rules": len(saved)},
        )
        logger.info(
            "agent permissions replaced agent=%s rules=%d actor=%s",
            agent_id,
            len(saved),
            _actor_label(request),
        )
        return JSONResponse(content={"agent_id": agent_id, "permissions": saved})

    @app.get("/api/v1/management/agents/{agent_id}/permissions/effective")
    def get_effective_permission(
        agent_id: str,
        request: Request,
        capability: str = "",
        scope_type: str = "",
        scope_id: str = "",
    ) -> JSONResponse:
        """capability의 실효 판정(member+) — evaluate_permission 결과.

        query: capability(필수), scope_type(global|project|task),
        scope_id(task/project 스코프 id). task 스코프 판정은 서버가
        task의 project_id를 함께 해석한다."""
        if store.get_agent(agent_id) is None:
            return err(404, "not_found", "agent not found", request)
        cap = (capability or "").strip().lower()
        if cap not in perm_svc.AGENT_CAPABILITIES:
            return err(
                422,
                "validation",
                f"capability must be one of {list(perm_svc.AGENT_CAPABILITIES)}",
                request,
            )
        st = (scope_type or "").strip().lower()
        if st and st not in perm_svc.PERMISSION_SCOPE_TYPES:
            return err(
                422,
                "validation",
                f"scope_type must be one of {list(perm_svc.PERMISSION_SCOPE_TYPES)}",
                request,
            )
        sid = (scope_id or "").strip()
        if st in ("project", "task") and not sid:
            return err(
                422,
                "validation",
                "scope_id is required for project/task scope",
                request,
            )
        result = perm_svc.evaluate_permission(
            store,
            agent_id,
            cap,
            scope_type=st,
            scope_id=sid,
        )
        return JSONResponse(content=result)
