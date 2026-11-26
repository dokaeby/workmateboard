"""Agent management routes — /api/v1/management (paperclip spec 1차).

에이전트 레지스트리 상태 전이/wake/heartbeat + task CRUD/assign/checkout +
통합 agent_events 조회. 쓰기 동작은 lead 이상, heartbeat·external checkout은
머신 API 키 전용(세션 쿠키만으로는 403).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse

from ... import agents as agents_svc
from ... import budget_scope
from ...models import (
    AgentKind,
    AgentStatus,
    TaskStatus,
    can_transition_agent,
    canonical_agent_status,
)
from ...services import agent_management as mgmt
from ..schemas import (
    AgentHeartbeatRequest,
    AgentTransitionRequest,
    AgentWakeRequest,
    CostEventCreateRequest,
    ManagementAgentCreateRequest,
    ManagementAgentPatchRequest,
    ManagementTaskCreateRequest,
    ManagementTaskPatchRequest,
    TaskAssignRequest,
    TaskCheckoutRequest,
    TaskReleaseRequest,
)

logger = logging.getLogger("jobmate.api")


def register(app: FastAPI, ctx: Any) -> None:
    store = ctx.store
    err = ctx.err
    _lead_gate = ctx._lead_gate
    _explicit_lead_user = ctx._explicit_lead_user
    _audit_api = ctx._audit_api
    _actor_label = ctx._actor_label
    _run_rate_limited = ctx._run_rate_limited
    _monthly_budget_usd = ctx._monthly_budget_usd
    _mtd_llm_cost_usd = ctx._mtd_llm_cost_usd

    from ...security import authorized

    def _machine_gate(request: Request) -> JSONResponse | None:
        """머신 전용 — 유효 API 키만 허용. 세션 쿠키만으로는 403.

        authorized()는 api_key 미설정 로컬 개발 모드에서도 True를 반환해
        개발 편의는 유지된다."""
        if not authorized(request):
            logger.warning("management machine endpoint rejected: session-only auth")
            return err(403, "forbidden", "machine API key required", request)
        return None

    # ==================== agents ====================

    @app.get("/api/v1/management/agents")
    def mgmt_list_agents(
        request: Request, status: str | None = None, kind: str | None = None
    ) -> JSONResponse:
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        want_status = ""
        if status:
            want_status = canonical_agent_status(status)
            if want_status not in mgmt.AGENT_STATUS_VALUES:
                return err(
                    422,
                    "validation",
                    f"invalid status: {status}",
                    request,
                )
        want_kind = (kind or "").strip().lower()
        if want_kind and want_kind not in (AgentKind.WORKFLOW.value, AgentKind.EXTERNAL.value):
            return err(
                422,
                "validation",
                f"invalid kind: {kind}",
                request,
            )
        items = []
        for a in store.list_agents():
            canon = canonical_agent_status(a.get("status"))
            if want_status and canon != want_status:
                continue
            if want_kind and str(a.get("kind") or "") != want_kind:
                continue
            items.append(mgmt.agent_out_with_task(store, a))
        return JSONResponse(content={"items": items})

    @app.post("/api/v1/management/agents", status_code=201)
    def mgmt_create_agent(body: ManagementAgentCreateRequest, request: Request) -> JSONResponse:
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        fields = body.model_dump(exclude_unset=True)
        verr = mgmt.validate_agent_create_fields(fields)
        if verr is not None:
            return err(422, "validation", verr, request)
        # name/adapter_type/reports_to/capabilities는 기존 agents 서비스 규칙 재사용
        base_fields: dict[str, Any] = {
            k: fields[k]
            for k in ("name", "adapter_type", "reports_to", "capabilities")
            if k in fields
        }
        verr = agents_svc.validate_agent_fields(store, base_fields)
        if verr is not None:
            code = 409 if verr == agents_svc.ERR_NAME_CONFLICT else 422
            return err(code, "conflict" if code == 409 else "validation", verr, request)
        caps = agents_svc.clean_capabilities(fields.get("capabilities"))
        status = canonical_agent_status(str(fields.get("status") or "active"))
        agent = store.create_agent(
            str(fields.get("name") or ""),
            str(fields.get("adapter_type") or ""),
            role=str(fields.get("role") or ""),
            title=str(fields.get("title") or ""),
            description=str(fields.get("description") or ""),
            capabilities=caps,
            kind=str(fields.get("kind") or "workflow"),
            adapter_config=fields.get("adapter_config"),
            reports_to=(
                str(fields["reports_to"]).strip() or None if fields.get("reports_to") else None
            ),
            status=status,
            monthly_budget_usd=fields.get("monthly_budget_usd"),
            monthly_budget_microusd=int(fields.get("monthly_budget_microusd") or 0),
            created_by=_actor_label(request),
        )
        if agent is None:
            return err(409, "conflict", agents_svc.ERR_NAME_CONFLICT, request)
        _audit_api(request, "mgmt_agent_create", agent["id"], {"name": agent["name"][:80]})
        mgmt.emit_event(
            store,
            "agent_created",
            agent_id=agent["id"],
            actor=_actor_label(request),
            summary=f"agent {agent['name']} created",
            detail={"kind": agent.get("kind"), "status": agent.get("status")},
        )
        logger.info("management agent created id=%s name=%s", agent["id"], agent["name"])
        return JSONResponse(status_code=201, content=mgmt.agent_out_with_task(store, agent))

    @app.get("/api/v1/management/agents/{agent_id}")
    def mgmt_get_agent(agent_id: str, request: Request) -> JSONResponse:
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        agent = store.get_agent(agent_id)
        if agent is None:
            return err(404, "not_found", "agent not found", request)
        return JSONResponse(content=mgmt.agent_out_with_task(store, agent))

    @app.patch("/api/v1/management/agents/{agent_id}")
    def mgmt_patch_agent(
        agent_id: str, body: ManagementAgentPatchRequest, request: Request
    ) -> JSONResponse:
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        agent = store.get_agent(agent_id)
        if agent is None:
            return err(404, "not_found", "agent not found", request)
        fields = body.model_dump(exclude_unset=True)
        verr = mgmt.validate_agent_create_fields(fields)
        if verr is not None:
            return err(422, "validation", verr, request)
        # status는 전이 규칙으로만 변경 — PATCH에서 직접 쓰지 않는다.
        # 전이 가능 여부를 필드 쓰기 전에 검증한다 — 409 시 이미 커밋된
        # 필드 변경만 남는 비원자적 부분 적용을 막는다(M4).
        to_status = fields.pop("status", None)
        if to_status is not None:
            to = canonical_agent_status(str(to_status))
            if to not in {s.value for s in AgentStatus}:
                return err(
                    422,
                    "validation",
                    f"invalid agent status: {to_status}",
                    request,
                )
            frm = canonical_agent_status(agent.get("status"))
            if not can_transition_agent(frm, to):
                return err(
                    409,
                    "invalid_transition",
                    f"cannot transition agent from {frm} to {to}",
                    request,
                )
        if fields:
            base_fields: dict[str, Any] = {
                k: fields[k]
                for k in ("name", "adapter_type", "reports_to", "capabilities")
                if k in fields
            }
            verr = agents_svc.validate_agent_fields(store, base_fields, agent_id=agent_id)
            if verr is not None:
                code = 409 if verr == agents_svc.ERR_NAME_CONFLICT else 422
                return err(code, "conflict" if code == 409 else "validation", verr, request)
            if "capabilities" in fields:
                cleaned = agents_svc.clean_capabilities(fields["capabilities"])
                if cleaned is not None:
                    fields["capabilities"] = cleaned
            if "adapter_config" in fields and fields["adapter_config"] is None:
                fields["adapter_config"] = {}
            updated = store.update_agent(agent_id, **fields)
            if updated is None:
                return err(409, "conflict", agents_svc.ERR_NAME_CONFLICT, request)
            agent = updated
        if to_status is not None:
            try:
                agent = mgmt.transition_agent(
                    store, agent_id, str(to_status), actor=_actor_label(request)
                )
                agent = store.get_agent(agent_id) or agent
            except mgmt.ManagementError as e:
                return err(e.status_code, e.code, e.message, request)
        _audit_api(request, "mgmt_agent_update", agent_id, {"fields": sorted(fields)})
        full = store.get_agent(agent_id)
        return JSONResponse(
            content=mgmt.agent_out_with_task(store, full) if full else mgmt.agent_out(agent)
        )

    @app.post("/api/v1/management/agents/{agent_id}/transition")
    def mgmt_transition_agent(
        agent_id: str, body: AgentTransitionRequest, request: Request
    ) -> JSONResponse:
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        try:
            mgmt.transition_agent(store, agent_id, body.status, actor=_actor_label(request))
        except mgmt.ManagementError as e:
            return err(e.status_code, e.code, e.message, request)
        _audit_api(request, "mgmt_agent_transition", agent_id, {"to": body.status[:20]})
        agent = store.get_agent(agent_id)
        return JSONResponse(
            content=mgmt.agent_out_with_task(store, agent) if agent else {"id": agent_id}
        )

    @app.post("/api/v1/management/agents/{agent_id}/wake", status_code=202)
    def mgmt_wake_agent(
        agent_id: str,
        body: AgentWakeRequest,
        request: Request,
        background_tasks: BackgroundTasks,
    ) -> JSONResponse:
        """wake — Idempotency-Key 헤더 또는 body.idempotency_key 필수.

        workflow kind는 기존 run 생성 경로를 재사용해 dev_review run을 만들고
        task_runs에 연결한다(동일 키 재전송 시 run 중복 없음).
        external kind는 wake 이벤트만 기록하고 202 — 프로세스 미생성."""
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        key = (body.idempotency_key or request.headers.get("idempotency-key") or "").strip()
        if not key:
            return err(
                422, "validation", "Idempotency-Key header or idempotency_key is required", request
            )
        # 멱등 replay는 예산/레이트리밋 검사를 건너뛴다 — 기록된 결과를
        # 재생하는 재시도가 예산 소진 후 429로 거절되면 안 된다(H2).
        # wake_agent 내부의 replay 판정과 동일한 agent_events 키를 먼저
        # 조회한다(저렴한 단일 행 조회).
        ev_key = mgmt.wake_event_key(agent_id, key)
        is_replay = store.get_agent_event_by_key(ev_key) is not None
        # run 생성과 동일한 비용 방어선 — management wake의 workflow kind는
        # admit_run으로 run을 만들므로 POST /runs와 같은 버스트/월간/스코프
        # 예산 검사를 먼저 통과해야 한다(H1). external kind는 run을 만들지
        # 않으므로 이 검사를 건너뛴다. workflow 키는 서비스와 동일하게
        # adapter_config.workflow에서 해석한다(미해석 시 dev_review 기본값).
        agent_row = store.get_agent(agent_id)
        is_workflow_kind = str((agent_row or {}).get("kind") or AgentKind.WORKFLOW.value) == (
            AgentKind.WORKFLOW.value
        )
        if agent_row is not None and is_workflow_kind and not is_replay:
            if _run_rate_limited(_actor_label(request)):
                return err(429, "rate_limited", "run 생성 요청이 너무 많습니다", request)
            cfg = agent_row.get("adapter_config") or {}
            workflow = str(cfg.get("workflow") or "dev_review").strip() or "dev_review"
            budget = _monthly_budget_usd()
            if budget is not None:
                mtd = _mtd_llm_cost_usd()
                if mtd > budget:
                    _audit_api(
                        request,
                        "mgmt_wake_budget_exceeded",
                        agent_id,
                        {"budget_usd": budget, "mtd_cost_usd": round(mtd, 6)},
                    )
                    logger.warning(
                        "management wake rejected: monthly budget exceeded mtd=%.4f budget=%.4f",
                        mtd,
                        budget,
                    )
                    return err(
                        429,
                        "budget_exceeded",
                        "monthly LLM budget exceeded",
                        request,
                    )
            # 스코프드 예산(global/project/workflow/agent) — hard_stop 차단.
            # wake는 project 태그가 없으므로 project=""로 심사한다.
            blocks = budget_scope.evaluate_admission(store, workflow, "", agent_id=agent_id)
            if blocks:
                _audit_api(
                    request,
                    "mgmt_wake_budget_exceeded",
                    agent_id,
                    {"workflow": workflow, "blocks": blocks[:5]},
                )
                logger.warning(
                    "management wake rejected: scoped budget exceeded "
                    "agent=%s workflow=%s blocks=%s",
                    agent_id,
                    workflow,
                    blocks[:3],
                )
                return err(
                    429,
                    "budget_exceeded",
                    f"budget exceeded: {blocks[0]['scope']} "
                    f"(spend ${blocks[0]['month_spend_usd']:.4f} / "
                    f"limit ${blocks[0]['amount_usd']:.4f})",
                    request,
                )
        _schedule_run_jobs = ctx._schedule_run_jobs

        def _sched(run: Any, job_payload: dict[str, Any]) -> None:
            if run is None:
                return
            _schedule_run_jobs([(run.id, run.workflow_key, job_payload, None)], background_tasks)

        try:
            result = mgmt.wake_agent(
                store,
                agent_id,
                task_id=body.task_id,
                idempotency_key=key,
                actor=_actor_label(request),
                schedule=_sched,
            )
        except mgmt.ManagementError as e:
            return err(e.status_code, e.code, e.message, request)
        _audit_api(
            request,
            "mgmt_agent_wake",
            agent_id,
            {"action": result.get("action"), "run_id": result.get("run_id")},
        )
        return JSONResponse(status_code=202, content=result)

    @app.post("/api/v1/management/agents/{agent_id}/heartbeat")
    def mgmt_heartbeat(
        agent_id: str, body: AgentHeartbeatRequest, request: Request
    ) -> JSONResponse:
        """머신 heartbeat — API 키 전용. last_seen_at + 상태 투영."""
        gate = _machine_gate(request)
        if gate is not None:
            return gate
        try:
            out = mgmt.heartbeat(
                store,
                agent_id,
                status=body.status,
                task_id=body.task_id,
                task_status=body.task_status,
                detail=body.detail,
                actor="machine",
            )
        except mgmt.ManagementError as e:
            return err(e.status_code, e.code, e.message, request)
        return JSONResponse(content=out)

    # ==================== tasks ====================

    @app.post("/api/v1/management/tasks", status_code=201)
    def mgmt_create_task(body: ManagementTaskCreateRequest, request: Request) -> JSONResponse:
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        try:
            task = mgmt.create_task(
                store,
                title=body.title,
                description=body.description,
                status=body.status or TaskStatus.BACKLOG.value,
                priority=body.priority or "medium",
                assignee_agent_id=body.assignee_agent_id,
                reviewer_agent_id=body.reviewer_agent_id,
                parent_id=body.parent_id,
                created_by=_actor_label(request),
                actor=_actor_label(request),
            )
        except mgmt.ManagementError as e:
            return err(e.status_code, e.code, e.message, request)
        _audit_api(request, "mgmt_task_create", str(task["id"]), {"title": task["title"][:80]})
        return JSONResponse(status_code=201, content=task)

    @app.get("/api/v1/management/tasks")
    def mgmt_list_tasks(
        request: Request,
        status: str | None = None,
        assignee: str | None = None,
        limit: int = 50,
    ) -> JSONResponse:
        """task 목록 — 최신순, limit 최대 200. 인증된 사용자 조회 가능."""
        if status:
            st = status.strip().lower()
            if st not in {s.value for s in TaskStatus}:
                return err(422, "validation", f"invalid status: {status}", request)
            status = st
        items = store.list_tasks(status=status, assignee_agent_id=assignee, limit=limit)
        return JSONResponse(content={"items": items})

    @app.get("/api/v1/management/tasks/{task_id}")
    def mgmt_get_task(task_id: str, request: Request) -> JSONResponse:
        task = store.get_task(task_id)
        if task is None:
            return err(404, "not_found", "task not found", request)
        return JSONResponse(content=task)

    @app.patch("/api/v1/management/tasks/{task_id}")
    def mgmt_patch_task(
        task_id: str, body: ManagementTaskPatchRequest, request: Request
    ) -> JSONResponse:
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        fields = body.model_dump(exclude_unset=True)
        try:
            task = mgmt.patch_task(store, task_id, fields, actor=_actor_label(request))
        except mgmt.ManagementError as e:
            return err(e.status_code, e.code, e.message, request)
        _audit_api(request, "mgmt_task_update", task_id, {"fields": sorted(fields)})
        return JSONResponse(content=task)

    @app.post("/api/v1/management/tasks/{task_id}/assign")
    def mgmt_assign_task(task_id: str, body: TaskAssignRequest, request: Request) -> JSONResponse:
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        try:
            task = mgmt.assign_task(
                store,
                task_id,
                body.agent_id,
                reviewer_agent_id=body.reviewer_agent_id,
                actor=_actor_label(request),
            )
        except mgmt.ManagementError as e:
            return err(e.status_code, e.code, e.message, request)
        _audit_api(request, "mgmt_task_assign", task_id, {"agent_id": body.agent_id})
        return JSONResponse(content=task)

    @app.post("/api/v1/management/tasks/{task_id}/checkout")
    def mgmt_checkout_task(
        task_id: str, body: TaskCheckoutRequest, request: Request
    ) -> JSONResponse:
        """checkout — external kind agent는 머신 API 키 필수, 그 외는 lead.

        유효 API 키(머신)는 kind 무관하게 허용한다."""
        agent = store.get_agent(body.agent_id)
        kind = str((agent or {}).get("kind") or AgentKind.WORKFLOW.value)
        if not authorized(request):
            if kind == AgentKind.EXTERNAL.value:
                return err(403, "forbidden", "machine API key required", request)
            gate = _lead_gate(request)
            if gate is not None:
                return gate
        try:
            task = mgmt.checkout_task(store, task_id, body.agent_id, actor=_actor_label(request))
        except mgmt.ManagementError as e:
            return err(e.status_code, e.code, e.message, request)
        _audit_api(request, "mgmt_task_checkout", task_id, {"agent_id": body.agent_id})
        return JSONResponse(content=task)

    @app.post("/api/v1/management/tasks/{task_id}/release")
    def mgmt_release_task(task_id: str, body: TaskReleaseRequest, request: Request) -> JSONResponse:
        """checkout 해제 — 명시적 lead/세션 admin 또는 로컬 개발 모드는
        임의 task를 해제할 수 있다. 그 외 호출자(머신 API 키, member 세션
        +키 등 _lead_user를 통과하지만 '명시적' lead가 아닌 경우)는
        checkout 소유 agent_id를 body로 증명해야 한다 — 머신 키 하나가
        모든 checkout을 해제하지 못하게 holder 검사를 유지한다(H2).
        외부 runner가 죽어 checkout이 stranded된 경우의 탈출 경로.

        참고: checkout_agent_id는 task GET 응답에 포함되므로 이 검사는
        소유권의 암호학적 증명이 아니라 '실수/캐주얼한' cross-release를
        막는 수준의 통제다 — 머신 키 보유자는 task를 읽어 holder id를
        알 수 있다. 머신 키 = admin 동일시라는 기존 trust model 안에서
        허용되는 잔여 위험으로 문서화한다."""
        task = store.get_task(task_id)
        if task is None:
            return err(404, "not_found", "task not found", request)
        holder = str(task.get("checkout_agent_id") or "")
        if _explicit_lead_user(request) is None:
            # 비-명시 호출 — 유효 인증(머신 키 등)은 필요하고, checkout
            # 소유 agent_id 일치를 요구한다.
            if not authorized(request):
                return err(403, "forbidden", "lead role required", request)
            claimed = (body.agent_id or "").strip()
            if not holder or claimed != holder:
                return err(
                    403,
                    "forbidden",
                    "only the checkout owner agent can release this task",
                    request,
                )
        try:
            out = mgmt.release_task(store, task_id, actor=_actor_label(request))
        except mgmt.ManagementError as e:
            return err(e.status_code, e.code, e.message, request)
        _audit_api(
            request,
            "mgmt_task_release",
            task_id,
            {"checkout_agent_id": holder},
        )
        return JSONResponse(content=out)

    # ==================== events ====================

    @app.get("/api/v1/management/events")
    def mgmt_list_events(
        request: Request,
        agent_id: str | None = None,
        task_id: str | None = None,
        run_id: str | None = None,
        after: str | None = None,
        limit: int = 50,
    ) -> JSONResponse:
        """통합 agent_events — 최신순 고정, limit 최대 200.

        after는 이벤트 id 또는 ISO 타임스탬프(그 이후만 반환).
        heartbeat detail 등 임의 JSON을 포함하므로 lead 이상 전용 —
        agents/management 읽기 게이트와 동일 수준으로 맞춘다(M2)."""
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        items = store.list_agent_events(
            agent_id=agent_id,
            task_id=task_id,
            run_id=run_id,
            after=after,
            limit=limit,
        )
        return JSONResponse(content={"items": items})

    # ==================== cost ledger (2차) ====================

    @app.post("/api/v1/management/cost-events", status_code=201)
    def mgmt_create_cost_event(body: CostEventCreateRequest, request: Request) -> JSONResponse:
        """외부 runner의 비용 보고 — 머신 API 키 전용(세션 쿠키 불가).

        spec 2차 A: 정수 microusd만 허용(스키마 StrictInt + store 재검증),
        idempotency_key UNIQUE로 재전송 중복 청구를 막는다."""
        gate = _machine_gate(request)
        if gate is not None:
            return gate
        try:
            row, created = store.record_cost_event(
                idempotency_key=body.idempotency_key,
                agent_id=body.agent_id,
                task_id=body.task_id,
                run_id=body.run_id,
                stage_run_id=body.stage_run_id,
                provider=body.provider,
                model=body.model,
                billing_type=body.billing_type,
                cost_status=body.cost_status,
                input_tokens=body.input_tokens,
                cached_input_tokens=body.cached_input_tokens,
                output_tokens=body.output_tokens,
                cost_microusd=body.cost_microusd,
                pricing_source=body.pricing_source,
                pricing_version=body.pricing_version,
                occurred_at=body.occurred_at,
            )
        except ValueError as e:
            return err(422, "validation", str(e), request)
        if row is None:
            return err(500, "internal_error", "cost event not recorded", request)
        if created:
            # spec 2차 H: cost event accepted — ids/provider/model/cost_status.
            # 금액은 허용되지만 credential은 절대 기록하지 않는다.
            logger.info(
                "cost event accepted id=%s key=%s agent=%s run=%s stage_run=%s "
                "provider=%s model=%s status=%s microusd=%s",
                row.get("id"),
                body.idempotency_key[:80],
                body.agent_id[:40] or "-",
                body.run_id or "-",
                body.stage_run_id or "-",
                body.provider[:40] or "-",
                body.model[:60] or "-",
                body.cost_status,
                body.cost_microusd,
            )
            _audit_api(
                request,
                "mgmt_cost_event_create",
                str(row.get("id") or ""),
                {
                    "key": body.idempotency_key[:80],
                    "cost_status": body.cost_status,
                    "model": body.model[:60],
                },
            )
            return JSONResponse(status_code=201, content=row)
        # 동일 idempotency_key 재전송 — 기존 행 재생(200).
        return JSONResponse(content=row)

    @app.get("/api/v1/management/cost-events")
    def mgmt_list_cost_events(
        request: Request,
        agent_id: str | None = None,
        task_id: str | None = None,
        run_id: str | None = None,
        stage_run_id: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        cost_status: str | None = None,
        billing_type: str | None = None,
        occurred_from: str | None = None,
        occurred_to: str | None = None,
        limit: int = 100,
    ) -> JSONResponse:
        """비용 원장 조회 — lead 이상. 최신순, limit 최대 500."""
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        items = store.list_cost_events(
            agent_id=agent_id,
            task_id=task_id,
            run_id=run_id,
            stage_run_id=stage_run_id,
            provider=provider,
            model=model,
            cost_status=cost_status,
            billing_type=billing_type,
            occurred_from=occurred_from,
            occurred_to=occurred_to,
            limit=limit,
        )
        return JSONResponse(content={"items": items})

    @app.get("/api/v1/management/costs/summary")
    def mgmt_costs_summary(
        request: Request,
        scope: str = "global",
        scope_id: str = "",
        occurred_from: str | None = None,
        occurred_to: str | None = None,
    ) -> JSONResponse:
        """스코프·기간별 비용 합계 — lead 이상.

        scope는 'global' 또는 'type:id' 형태(agent:X, task:Y, workflow:Z,
        project:P). spec 쿼리명(from/to)과 호환되도록 occurred_from/to를
        별칭으로도 받는다."""
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        st = "global"
        sid = ""
        raw = (scope or "").strip().lower()
        if ":" in raw:
            st, _, sid = raw.partition(":")
            sid = sid.strip()
        else:
            st = raw or "global"
            sid = (scope_id or "").strip()
        if st == "global":
            sid = ""
        if st not in budget_scope.SCOPE_TYPES:
            return err(
                422,
                "validation",
                f"scope must be one of {list(budget_scope.SCOPE_TYPES)} or 'type:id'",
                request,
            )
        frm = occurred_from or request.query_params.get("from")
        to = occurred_to or request.query_params.get("to")
        return JSONResponse(content=store.cost_summary(st, sid, occurred_from=frm, occurred_to=to))
