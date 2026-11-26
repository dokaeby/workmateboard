"""Agent registry routes — 조직도/어댑터 레지스트리 CRUD (Paperclip agents 포팅).

GET/POST/PATCH/DELETE 모두 lead 이상 — AgentOut에 budget/cost 집계
(monthly_budget_usd, cost_30d_usd)가 포함돼 member에게 노출하면 안 된다
(조직도 nav/페이지도 lead 전용). DELETE는 하드 삭제가 아니라
status='archived' 소프트 삭제 — runs.agent_id가 참조하는 이력을 보존한다.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ... import agents as agents_svc
from ..schemas import AgentCreateRequest, AgentPatchRequest

logger = logging.getLogger("jobmate.api")


def register(app: FastAPI, ctx: Any) -> None:
    store = ctx.store
    err = ctx.err
    _lead_gate = ctx._lead_gate
    _audit_api = ctx._audit_api
    _actor_label = ctx._actor_label

    @app.get("/api/v1/agents")
    def list_agents(request: Request, status: str | None = None) -> JSONResponse:
        """에이전트 레지스트리 목록(lead+) — reports_to로 조직도 트리 구성.
        status 필터 선택(active|paused|archived); 빈 문자열은 미지정과 동일."""
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        status = (status or "").strip().lower() or None
        if status is not None and status not in agents_svc.AGENT_STATUSES:
            return err(
                422,
                "validation",
                f"status must be one of {list(agents_svc.AGENT_STATUSES)}",
                request,
            )
        if status:
            run_stats, cost_map = agents_svc.stats_by_agent(store)
            items = [
                agents_svc.agent_out_with_stats(
                    a, run_stats.get(str(a.get("id") or ""), {}), cost_map
                )
                for a in store.list_agents(status=status)
            ]
            return JSONResponse(content={"items": items})
        return JSONResponse(content=agents_svc.agents_payload(store))

    @app.post("/api/v1/agents", status_code=201)
    def create_agent(body: AgentCreateRequest, request: Request) -> JSONResponse:
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        fields = body.model_dump(exclude_unset=True)
        verr = agents_svc.validate_agent_fields(store, fields)
        if verr is not None:
            code = 409 if verr == agents_svc.ERR_NAME_CONFLICT else 422
            return err(code, "conflict" if code == 409 else "validation", verr, request)
        caps = agents_svc.clean_capabilities(fields.get("capabilities"))
        agent = store.create_agent(
            str(fields.get("name") or ""),
            str(fields.get("adapter_type") or ""),
            role=str(fields.get("role") or ""),
            description=str(fields.get("description") or ""),
            capabilities=caps,
            reports_to=(
                str(fields["reports_to"]).strip() or None if fields.get("reports_to") else None
            ),
            status=str(fields.get("status") or "active"),
            monthly_budget_usd=fields.get("monthly_budget_usd"),
            created_by=_actor_label(request),
        )
        if agent is None:
            return err(409, "conflict", agents_svc.ERR_NAME_CONFLICT, request)
        out = agents_svc.agent_out(store, agent)
        _audit_api(request, "agent_create", agent["id"], {"name": agent["name"][:80]})
        logger.info("agent created id=%s name=%s", agent["id"], agent["name"])
        return JSONResponse(status_code=201, content=out)

    @app.patch("/api/v1/agents/{agent_id}")
    def patch_agent(agent_id: str, body: AgentPatchRequest, request: Request) -> JSONResponse:
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        if store.get_agent(agent_id) is None:
            return err(404, "not_found", "agent not found", request)
        fields = body.model_dump(exclude_unset=True)
        verr = agents_svc.validate_agent_fields(store, fields, agent_id=agent_id)
        if verr is not None:
            code = 409 if verr == agents_svc.ERR_NAME_CONFLICT else 422
            return err(code, "conflict" if code == 409 else "validation", verr, request)
        if "capabilities" in fields:
            cleaned = agents_svc.clean_capabilities(fields["capabilities"])
            if cleaned is not None:
                fields["capabilities"] = cleaned
        agent = store.update_agent(agent_id, **fields)
        if agent is None:
            # store는 '없음'과 'name 중복(integrity/사전검사)'을 같은 None으로
            # 반환한다 — 재조회로 구분해 name 충돌은 409로 매핑한다.
            if store.get_agent(agent_id) is None:
                return err(404, "not_found", "agent not found", request)
            return err(409, "conflict", agents_svc.ERR_NAME_CONFLICT, request)
        out = agents_svc.agent_out(store, agent)
        _audit_api(request, "agent_update", agent_id, {"fields": sorted(fields)})
        logger.info("agent updated id=%s fields=%s", agent_id, sorted(fields))
        return JSONResponse(content=out)

    @app.delete("/api/v1/agents/{agent_id}")
    def delete_agent(agent_id: str, request: Request) -> JSONResponse:
        """소프트 삭제 — status='archived'. runs 참조 이력은 보존."""
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        agent = store.get_agent(agent_id)
        if agent is None:
            return err(404, "not_found", "agent not found", request)
        if not store.archive_agent(agent_id):
            return err(404, "not_found", "agent not found", request)
        _audit_api(request, "agent_archive", agent_id, {"name": agent["name"][:80]})
        logger.info("agent archived id=%s name=%s", agent_id, agent["name"])
        return JSONResponse(content={"ok": True, "status": "archived"})
