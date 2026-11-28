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
from ...models import (
    AgentStatus,
    ApprovalActionType,
    can_transition_agent,
    canonical_agent_status,
)
from ...services import agent_management as mgmt
from ..schemas import AgentCreateRequest, AgentPatchRequest

logger = logging.getLogger("jobmate.api")


def register(app: FastAPI, ctx: Any) -> None:
    store = ctx.store
    err = ctx.err
    _lead_gate = ctx._lead_gate
    _audit_api = ctx._audit_api
    _actor_label = ctx._actor_label
    _require_session_capability = ctx._require_session_capability

    @app.get("/api/v1/agents")
    def list_agents(request: Request, status: str | None = None) -> JSONResponse:
        """에이전트 레지스트리 목록(lead+) — reports_to로 조직도 트리 구성.
        status 필터는 통합 어휘(canonical 7값 + 레거시 'archived' 별칭)를
        받아들인다 — 'archived'/'terminated' 모두 canonical terminated로
        매칭돼 신규 상태(idle/running/error 등)도 필터 가능하다(L9).
        빈 문자열은 미지정과 동일."""
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        status = (status or "").strip().lower() or None
        canon_status = ""
        if status is not None:
            canon_status = canonical_agent_status(status)
            if canon_status not in {s.value for s in AgentStatus}:
                return err(
                    422,
                    "validation",
                    f"status must be one of {sorted(s.value for s in AgentStatus)} or 'archived'",
                    request,
                )
        if canon_status:
            run_stats, cost_map = agents_svc.stats_by_agent(store)
            # 저장값이 레거시 'archived'일 수 있으므로 raw 컬럼이 아니라
            # canonical 판정으로 필터한다.
            items = [
                agents_svc.agent_out_with_stats(
                    a, run_stats.get(str(a.get("id") or ""), {}), cost_map
                )
                for a in store.list_agents()
                if canonical_agent_status(a.get("status")) == canon_status
            ]
            return JSONResponse(content={"items": items})
        return JSONResponse(content=agents_svc.agents_payload(store))

    @app.post("/api/v1/agents", status_code=201)
    def create_agent(body: AgentCreateRequest, request: Request) -> JSONResponse:
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        denied = _require_session_capability(request, "agent:manage", actor=_actor_label(request))
        if denied is not None:
            return denied
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
        denied = _require_session_capability(request, "agent:manage", actor=_actor_label(request))
        if denied is not None:
            return denied
        agent_row = store.get_agent(agent_id)
        if agent_row is None:
            return err(404, "not_found", "agent not found", request)
        fields = body.model_dump(exclude_unset=True)
        # status는 전이 allowlist를 거친다 — 통합 어휘(canonical 7값 +
        # 레거시 'archived' 별칭)만 허용하고, 전이 불가/terminal 출발은
        # 409. 비-status 필드 쓰기 전에 전이 가능 여부를 먼저 검증해
        # 부분 적용을 막는다(H2/M4와 동일 원자성 규칙).
        to_status = fields.pop("status", None)
        to_canon = ""
        if to_status is not None:
            to_canon = canonical_agent_status(str(to_status))
            if to_canon not in {s.value for s in AgentStatus}:
                return err(
                    422,
                    "validation",
                    f"status must be one of {sorted(s.value for s in AgentStatus)} or 'archived'",
                    request,
                )
            frm = canonical_agent_status(agent_row.get("status"))
            if to_canon != frm and not can_transition_agent(frm, to_canon):
                return err(
                    409,
                    "invalid_transition",
                    f"cannot transition agent from {frm} to {to_canon}",
                    request,
                )
            # spec 2차 D — terminated 전이는 승인된 agent_terminate
            # approval_request가 필요하다(서비스 transition_agent와 동일 규칙).
            if (
                to_canon == AgentStatus.TERMINATED.value
                and to_canon != frm
                and not store.has_decided_approval(
                    ApprovalActionType.AGENT_TERMINATE.value, "agent", agent_id
                )
            ):
                return err(
                    409,
                    "approval_required",
                    "agent terminate requires an approved agent_terminate approval_request",
                    request,
                )
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
        # status 전이 — allowlist 통과분만 canonical 값으로 기록하고
        # agent_events에도 남긴다(management transition과 동일 흔적).
        # CAS 전이 — 읽기~쓰기 사이 status가 바뀌었으면(terminated 포함)
        # 덮어쓰지 않고 409로 재시도 가능하게 한다.
        status_changed = False
        if to_canon:
            frm = canonical_agent_status(agent.get("status"))
            if to_canon != frm:
                transitioned = store.transition_agent_status(agent_id, frm, to_canon)
                if transitioned is None:
                    fresh = store.get_agent(agent_id)
                    if fresh is not None and canonical_agent_status(fresh.get("status")) == (
                        to_canon
                    ):
                        agent = fresh  # 경합 중 동일 목표 도달 — no-op
                    else:
                        return err(
                            409,
                            "invalid_transition",
                            "agent status changed concurrently; retry",
                            request,
                        )
                else:
                    agent = transitioned
                status_changed = True
                mgmt.emit_event(
                    store,
                    "agent_status_changed",
                    agent_id=agent_id,
                    actor=_actor_label(request),
                    summary=f"agent {frm} -> {to_canon} (legacy patch)",
                    detail={"from": frm, "to": to_canon, "via": "legacy_patch"},
                )
        out = agents_svc.agent_out(store, agent)
        audit_fields = sorted(fields) + (["status"] if status_changed else [])
        _audit_api(request, "agent_update", agent_id, {"fields": audit_fields})
        logger.info("agent updated id=%s fields=%s", agent_id, audit_fields)
        return JSONResponse(content=out)

    @app.delete("/api/v1/agents/{agent_id}")
    def delete_agent(agent_id: str, request: Request) -> JSONResponse:
        """소프트 삭제 — status='archived'(canonical terminated). runs 참조 이력은 보존.

        spec 2차 D: archived는 terminated 별칭이므로 PATCH/transition과 동일하게
        승인된 agent_terminate approval_request를 요구한다 — 이미 terminal인
        agent의 archive는 no-op이라 허용한다."""
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        denied = _require_session_capability(request, "agent:manage", actor=_actor_label(request))
        if denied is not None:
            return denied
        agent = store.get_agent(agent_id)
        if agent is None:
            return err(404, "not_found", "agent not found", request)
        frm = canonical_agent_status(agent.get("status"))
        if frm != AgentStatus.TERMINATED.value and not store.has_decided_approval(
            ApprovalActionType.AGENT_TERMINATE.value, "agent", agent_id
        ):
            return err(
                409,
                "approval_required",
                "agent terminate requires an approved agent_terminate approval_request",
                request,
            )
        if not store.archive_agent(agent_id):
            return err(404, "not_found", "agent not found", request)
        # transition/PATCH와 동일한 agent_events 흔적 — archive 경로도 기록.
        if frm != AgentStatus.TERMINATED.value:
            mgmt.emit_event(
                store,
                "agent_status_changed",
                agent_id=agent_id,
                actor=_actor_label(request),
                summary=f"agent {frm} -> terminated (archive)",
                detail={"from": frm, "to": "terminated", "via": "delete"},
            )
        _audit_api(request, "agent_archive", agent_id, {"name": agent["name"][:80]})
        logger.info("agent archived id=%s name=%s", agent_id, agent["name"])
        return JSONResponse(content={"ok": True, "status": "archived"})
