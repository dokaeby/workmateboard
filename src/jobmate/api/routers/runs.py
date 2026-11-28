"""Runs/streaming/approval route handlers extracted from api/app.py."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, StreamingResponse

from ... import budget_scope, wakeups
from ...models import AgentCapability, RunStatus, StageStatus, canonical_agent_status
from ...services import agent_management as mgmt
from ...services import permissions as perm_svc
from ...services import run_analysis
from ...services.management_common import row_org_id
from ...workflows.resolver import resolve_workflow_spec
from ..jobs import _parse_diff_files
from ..schemas import ApprovalRequest, RunCreateRequest, RunPatchRequest
from ..support import _workflow_needs_lead

logger = logging.getLogger("jobmate.api")


def register(app: FastAPI, ctx: Any) -> None:
    _SSE_HEARTBEAT_LOOPS = ctx._SSE_HEARTBEAT_LOOPS
    _SSE_MAX_CONNECTIONS = ctx._SSE_MAX_CONNECTIONS
    _SSE_POLL_SECONDS = ctx._SSE_POLL_SECONDS
    _actor_label = ctx._actor_label
    _audit_api = ctx._audit_api
    _do_approve = ctx._do_approve
    _lead_gate = ctx._lead_gate
    _monthly_budget_usd = ctx._monthly_budget_usd
    _require_session_capability = ctx._require_session_capability
    _mtd_llm_cost_usd = ctx._mtd_llm_cost_usd
    _run_rate_limited = ctx._run_rate_limited
    _schedule_run_jobs = ctx._schedule_run_jobs
    _sse = ctx._sse
    _sse_lock = ctx._sse_lock
    _sse_snapshot = ctx._sse_snapshot
    err = ctx.err
    run_detail = ctx.run_detail
    settings = ctx.settings
    store = ctx.store

    @app.post("/api/v1/runs", status_code=202)
    def create_run(
        body: RunCreateRequest, request: Request, background_tasks: BackgroundTasks
    ) -> JSONResponse:
        # 사용자/IP당 분당 버스트 제한 — LLM 비용이 큰 run 무한 생성 방지
        if _run_rate_limited(_actor_label(request)):
            return err(429, "rate_limited", "run 생성 요청이 너무 많습니다", request)
        # spec 해석은 요청 조직 컨텍스트 기준 — 조직별 동명 커스텀 spec이
        # 서로를 덮어쓰지 않는다(spec 003 US3).
        wf_spec = resolve_workflow_spec(store, body.workflow)
        if wf_spec is None:
            return err(404, "unknown_workflow", f"workflow '{body.workflow}' not found", request)
        # 사이드이펙트 워크플로(gmail_calendar의 실제 캘린더 쓰기, 커스텀 spec의
        # 외부 부수효과 function stage)는 lead 이상만 실행 가능 — member는
        # dev_review* 등 읽기/검토성 워크플로만.
        if _workflow_needs_lead(store, body.workflow):
            gate = _lead_gate(request)
            if gate is not None:
                return gate
        # agents 레지스트리 태깅 — 명시된 agent_id는 존재하는 active/paused
        # 에이전트여야 runs.agent_id에 기록돼 통계 귀속이 된다. archived는
        # 신규 run 태깅을 거부한다(기존 run 이력은 보존). best-effort:
        # bridge 세션의 tool→agent 자동 추론은 하지 않는다(명시 태깅만).
        agent_id = body.agent_id.strip()
        agent_row: dict[str, Any] | None = None
        if agent_id:
            agent_row = store.get_agent(agent_id)
            if agent_row is None:
                return err(422, "validation", "agent not found", request)
            if canonical_agent_status(agent_row.get("status")) == "terminated":
                return err(422, "validation", "agent is terminated", request)
        # goals 태깅 — 명시된 goal_id는 존재하는 비cancelled 목표여야
        # runs.goal_id에 기록된다. 미지정 시 store.create_run이 task
        # effective goal → 프로젝트의 goal_id 순으로 폴백 해석한다.
        goal_id = body.goal_id.strip()
        if goal_id:
            goal_row = store.get_goal(goal_id)
            if goal_row is None:
                return err(422, "validation", "goal not found", request)
            if str(goal_row.get("status") or "") == "cancelled":
                return err(422, "validation", "goal is cancelled", request)
        # 월간 LLM 예산 집행 — MTD 비용이 monthly_budget_usd를 초과하면
        # run 생성 자체를 거부한다(usage/summary의 budget_exceeded와 동일 기준).
        # llm_usage 전수 합산 비용이 크므로 60초 인메모리 캐시를 둔다.
        budget = _monthly_budget_usd()
        if budget is not None:
            mtd = _mtd_llm_cost_usd()
            if mtd > budget:
                _audit_api(
                    request,
                    "run_budget_exceeded",
                    body.workflow,
                    {"budget_usd": budget, "mtd_cost_usd": round(mtd, 6)},
                )
                # spec 004 US1 — budget.exceeded webhook (분 단위 dedupe).
                try:
                    from ...services import webhooks as _webhooks

                    _webhooks.emit_event(
                        store,
                        "budget.exceeded",
                        f"run-budget:{body.workflow}:monthly:{datetime.now(UTC):%Y%m%d%H%M}",
                        {
                            "workflow": body.workflow,
                            "scope": "monthly_llm_budget",
                            "source": "api",
                        },
                    )
                except Exception:
                    logger.warning("webhook emit failed budget deny", exc_info=True)
                logger.warning(
                    "run rejected: monthly budget exceeded mtd=%.4f budget=%.4f",
                    mtd,
                    budget,
                )
                return err(
                    429,
                    "budget_exceeded",
                    "monthly LLM budget exceeded",
                    request,
                )
        # 스코프드 예산 정책(global/project/workflow) — hard_stop 초과 시
        # run 생성 거부. 레거시 prefs.monthly_budget_usd 검사와 병행한다.
        # project는 명시 필드를 우선하고 metadata.project로 폴백 —
        # runs.project 컬럼/input.project에 넣어야 생성 시 예산 귀속과
        # 지출 집계가 일치한다. budget policy scope_id와 동일 정규화 적용.
        project = budget_scope.clean_scope_id(body.project or "")
        if not project and isinstance(body.metadata, dict):
            project = budget_scope.clean_scope_id(str(body.metadata.get("project") or ""))
        # metadata.task_id는 agent/user 양쪽 permission 평가의 task scope
        # 힌트 — agent_id 유무와 무관하게 한 번만 추출한다.
        _perm_task_id = ""
        if isinstance(body.metadata, dict):
            _perm_task_id = str(body.metadata.get("task_id") or "")
        # spec 3차 D — agent_id 태깅된 run 생성은 해당 agent의 run:execute
        # capability를 서버가 재검증한다(버튼 숨김이 아니라 강제). agent_id
        # 없는 run은 agent 행위자가 아니므로 agent permission을 적용하지
        # 않는다 — 사람/시스템 run은 기존 lead/예산 게이트가 담당한다.
        # L5 결정: 이 경로는 run 수준 검사(run:execute)만 한다. metadata에
        # task_id가 있어도 task 생명주기 capability(task:assign/update)는
        # 검사하지 않는다 — task 측 강제는 task 엔드포인트
        # (checkout/assign/patch)와 wake 경로에서 수행된다(docs §29 참조).
        # project 태그는 이름 문자열일 수 있으므로 permission scope_id
        # (canonical project uuid)로 해석한다 — id 또는 이름(ci) 모두 허용.
        # 해석 실패 시 ''를 넘겨 task_id 유도 project가 대신 쓰이게 한다.
        if agent_id:
            _perm_project_id = ""
            if project:
                _proj_row = store.get_project(project) or store.find_project_by_name(project)
                if _proj_row is not None:
                    _perm_project_id = str(_proj_row.get("id") or "")
            try:
                perm_svc.require_permission(
                    store,
                    agent_id,
                    AgentCapability.RUN_EXECUTE.value,
                    project_id=_perm_project_id,
                    task_id=_perm_task_id,
                    actor=_actor_label(request),
                )
            except mgmt.ManagementError as e:
                return err(e.status_code, e.code, e.message, request)
        input_data: dict[str, Any] = {"task": body.task}
        if project:
            input_data["project"] = project
        if body.metadata:
            input_data["meta"] = body.metadata
        # agent_id를 넘겨 agent 스코프 정책/agents.monthly_budget_usd도
        # 함께 심사한다.
        blocks = budget_scope.evaluate_admission(store, body.workflow, project, agent_id=agent_id)
        if blocks:
            _audit_api(
                request,
                "run_budget_exceeded",
                body.workflow,
                {"blocks": blocks[:5]},
            )
            # spec 004 US1 — budget.exceeded webhook (분 단위 dedupe).
            try:
                from ...services import webhooks as _webhooks

                _webhooks.emit_event(
                    store,
                    "budget.exceeded",
                    f"run-budget:{body.workflow}:{blocks[0]['scope']}:"
                    f"{datetime.now(UTC):%Y%m%d%H%M}",
                    {
                        "workflow": body.workflow,
                        "scope": str(blocks[0].get("scope") or ""),
                        "project": project,
                        "source": "api",
                    },
                )
            except Exception:
                logger.warning("webhook emit failed budget deny", exc_info=True)
            logger.warning(
                "run rejected: scoped budget exceeded workflow=%s blocks=%s",
                body.workflow,
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
        # spec 003 US4 — 세션 사용자의 run:execute user-permission 강제.
        # project는 이름 문자열일 수 있어 agent permission 경로와 동일하게
        # canonical project id로 해석한다. 머신 키/로컬 오픈은 헬퍼가 건너뛴다.
        _sess_project_id = ""
        if project:
            _sess_proj = store.get_project(project) or store.find_project_by_name(project)
            if _sess_proj is not None:
                _sess_project_id = str(_sess_proj.get("id") or "")
        denied = _require_session_capability(
            request,
            "run:execute",
            task_id=_perm_task_id,
            project_id=_sess_project_id,
            actor=_actor_label(request),
        )
        if denied is not None:
            return denied
        res = wakeups.admit_run(
            store,
            workflow_key=body.workflow,
            input_data=input_data,
            source="api",
            idempotency_key=body.idempotency_key,
            force_new=body.force_new,
            created_by=_actor_label(request),
            version=wf_spec.version,
            job_payload={"task": body.task},
            agent_id=agent_id,
            goal_id=goal_id,
        )
        action = str(res.get("action") or "")
        if action == "invalid_goal":
            # admit_run의 goal 검증 — 명시 goal_id는 라우트에서 이미 검증했으므로
            # 이 경로는 input_data.goal_id 등 비정규 경로용 방어선.
            return err(
                422,
                "validation",
                str(res.get("error") or "invalid goal"),
                request,
            )
        if action == "conflict":
            # 동일 idempotency_key가 다른 입력으로 이미 사용됨
            return err(
                409,
                "idempotency_conflict",
                "idempotency_key already used with a different request",
                request,
            )
        if action == "created":
            run = res["run"]
            # spec 006 US2 FR-008 — require_approval 정책으로 budget 승인
            # 대기(waiting_approval)로 생성된 run은 execute_run을 예약하지
            # 않는다. 승인(POST /runs/{id}/approve) 또는 예산 상향 재개 전까지
            # jobs._execute_run_job 가드가 실행을 차단한다.
            if run.status == RunStatus.WAITING_APPROVAL:
                logger.info(
                    "run created id=%s workflow=%s status=waiting_approval(budget)",
                    run.id,
                    body.workflow,
                )
                _audit_api(
                    request,
                    "run_budget_approval_wait",
                    run.id,
                    {"workflow": body.workflow},
                )
                return JSONResponse(
                    status_code=202,
                    content={
                        "id": run.id,
                        "status": run.status.value,
                        "reason": "budget_approval",
                    },
                )
            logger.info("run created id=%s workflow=%s", run.id, body.workflow)
            _schedule_run_jobs(
                [(run.id, body.workflow, {"task": body.task}, None)], background_tasks
            )
            return JSONResponse(status_code=202, content={"id": run.id, "status": run.status.value})
        if action in ("coalesced", "replayed"):
            run = res.get("run")
            status = run.status.value if run is not None else "unknown"
            logger.info("run %s workflow=%s wakeup=%s", action, body.workflow, res.get("wakeup_id"))
            return JSONResponse(
                status_code=202,
                content={
                    "id": str(res.get("run_id") or ""),
                    "status": status,
                    "action": action,
                },
            )
        # deferred — 동일 scope 활성 run 종료 후 promote
        return JSONResponse(
            status_code=202,
            content={
                "id": "",
                "status": "deferred",
                "action": "deferred",
                "wakeup_id": str(res.get("wakeup_id") or ""),
            },
        )

    @app.get("/api/v1/runs")
    def list_runs(
        request: Request,
        limit: int = 50,
        workflow: str | None = None,
        status: str | None = None,
        days: int | None = None,
        q: str | None = None,
        agent: str | None = None,
    ) -> Any:
        # spec 012 US3 — status/days/q/agent는 기존 workflow/limit과 AND.
        # days는 형제 엔드포인트와 동일하게 1~365 클램프.
        # q는 인덱스 불가 LIKE 스캔이라 member DoS 표면 — lead로 제한.
        if q and q.strip():
            gate = _lead_gate(request)
            if gate is not None:
                return gate
        clamped_days = max(1, min(int(days), 365)) if days is not None else None
        runs = store.list_runs(
            max(1, min(limit, 500)),
            workflow=workflow,
            status=status,
            days=clamped_days,
            q=q,
            agent=agent,
        )
        coalesced = store.coalesced_counts_for_runs([r.id for r in runs])
        return [
            {
                "id": r.id,
                "workflow": r.workflow_key,
                "status": r.status.value,
                "project": r.project,
                "goal_id": r.goal_id,
                "coalesced_count": coalesced.get(r.id, 0),
                "created_at": r.created_at.isoformat(),
            }
            for r in runs
        ]

    @app.get("/api/v1/runs/stats")
    def run_stats(workflow: str | None = None, days: int = 30) -> dict[str, Any]:
        """workflow별 run 성공/실패/토큰/단계 실패율 집계 (최대 365일)."""
        return store.run_stats(workflow, max(1, min(days, 365)))

    @app.get("/api/v1/llm-monitor/stats")
    def llm_monitor_stats(days: int = 30) -> JSONResponse:
        """LLM 모니터 대시보드 — 지연시간 백분위/토큰/모델/프롬프트 버전/
        stage 호출·실패율/승인 백로그/일별 시계열/최근 실패 run + LangSmith 상태."""
        data = store.llm_stats(max(1, min(days, 365)), monthly_budget=settings.budget_max_tokens)
        # 월 토큰 예산 80% 초과 시 인박스 알림 (dedupe_key로 중복 방지)
        budget = data.get("budget")
        if budget and budget.get("alert"):
            store.upsert_inbox_item(
                kind="budget_alert",
                title=f"LLM 토큰 예산 {budget['used_pct']}% 사용 ({budget['month']})",
                body=(
                    f"이번 달 stage 토큰 {budget['monthly_tokens']:,}"
                    f" / 예산 {budget['budget_max']:,}"
                ),
                link="?view=monitor",
                dedupe_key=f"llm_budget_{budget['month']}",
            )
        tracing = os.environ.get("LANGCHAIN_TRACING_V2") == "true" and bool(
            os.environ.get("LANGCHAIN_API_KEY")
        )
        data["langsmith"] = {
            "enabled": tracing,
            "project": os.environ.get("LANGCHAIN_PROJECT", ""),
            "sample_rate": os.environ.get("JOBMATE_LANGSMITH_SAMPLE_RATE", "1.0"),
        }
        return JSONResponse(content=data)

    @app.get("/api/v1/wakeups")
    def list_wakeups(
        request: Request,
        run_id: str | None = None,
        status: str | None = None,
        source: str | None = None,
        limit: int = 50,
    ) -> JSONResponse:
        """wakeup_requests 감사 조회 — run 생성 코얼레싱/디퍼 이력."""
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        items = store.list_wakeups(limit=limit, run_id=run_id, status=status, source=source)
        return JSONResponse(content={"items": items})

    # spec 006 US4 FR-017 — run 비교 뷰. /runs/{run_id} 보다 먼저 등록해야
    # 'compare'가 run_id로 잡히지 않는다(stats와 같은 이유).
    @app.get("/api/v1/runs/compare")
    def compare_runs(request: Request, run_a: str = "", run_b: str = "") -> JSONResponse:
        """두 run의 단계별 status·입출력 요약·diff 파일·소요 시간 비교.

        org 스코핑은 store.get_run의 _org_bind에 위임 — 보이지 않는 run은
        404로 수렴한다(GET /runs/{id}와 동일 게이트)."""
        a, b = run_a.strip(), run_b.strip()
        if not a or not b:
            return err(422, "validation", "run_a and run_b are required", request)
        data = run_analysis.compare_runs(store, a, b)
        if data is None:
            return err(404, "not_found", "run not found", request)
        return JSONResponse(content=data)

    @app.get("/api/v1/runs/{run_id}")
    def get_run(run_id: str, request: Request) -> JSONResponse:
        d = run_detail(run_id)
        if d is None:
            return err(404, "not_found", "run not found", request)
        return JSONResponse(content=d)

    # ---- SSE 실시간 이벤트 스트림 (/api/v1/stream) ----
    # 인증은 /api/* 미들웨어와 동일 — EventSource는 헤더를 못 쓰므로
    # 세션 쿠키(동일 출처) 또는 단기 SSE 토큰(?token=)을 사용한다.
    # 토큰은 POST /api/v1/stream/token으로 발급(헤더/쿠키 인증 필요).

    @app.post("/api/v1/stream/token")
    def stream_token(request: Request) -> JSONResponse:
        """SSE 단기 토큰 발급 — 헤더/세션 인증은 /api/* 미들웨어가 처리.

        EventSource는 Authorization 헤더를 설정할 수 없으므로, API 키
        인증 클라이언트가 이 엔드포인트로 5분짜리 토큰을 받아
        /api/v1/stream?token= 으로 접속한다. 장기 키는 URL에 넣지 않는다.
        """
        from ...security import issue_stream_token

        token, ttl = issue_stream_token()
        return JSONResponse(content={"token": token, "expires_in": ttl})

    @app.get("/api/v1/stream")
    async def event_stream(request: Request) -> Any:
        with _sse_lock:
            if _sse["count"] >= _SSE_MAX_CONNECTIONS:
                return err(503, "too_many_streams", "SSE 연결 수 제한을 초과했습니다", request)
            _sse["count"] += 1
        seq = 0

        async def gen() -> AsyncIterator[str]:
            nonlocal seq
            # 테이블별 워터마크 — connect 시각 이후 행만 이벤트화.
            # 경계(ts == wm)의 중복은 seen 집합으로 제거한다.
            started = datetime.now(UTC).isoformat()
            wm = {"runs": started, "stages": started, "sessions": started}
            seen: dict[str, set[str]] = {"runs": set(), "stages": set(), "sessions": set()}
            beats = 0
            try:
                snap = json.dumps(_sse_snapshot(), ensure_ascii=False)
                yield f"event: snapshot\ndata: {snap}\n\n"
                logger.debug("sse client connected total=%d", _sse["count"])
                while True:
                    if await request.is_disconnected():
                        break
                    await asyncio.sleep(_SSE_POLL_SECONDS)
                    if await request.is_disconnected():
                        break
                    try:
                        data = await run_in_threadpool(store.activity_since, min(wm.values()))
                    except Exception:
                        logger.warning("sse poll failed", exc_info=True)
                        continue
                    chunks: list[str] = []
                    for kind, event_name in (
                        ("runs", "run"),
                        ("stages", "stage"),
                        ("sessions", "session"),
                    ):
                        rows = data.get(kind) or []
                        max_ts = wm[kind]
                        emitted_at: dict[str, set[str]] = {}
                        for row in rows:
                            ts = str(row.get("ts") or "")
                            rid = str(row.get("id") or "")
                            if ts < wm[kind]:
                                continue
                            if ts == wm[kind] and rid in seen[kind]:
                                continue
                            payload = {k: v for k, v in row.items() if k != "ts"}
                            seq += 1
                            chunks.append(
                                f"id: {seq}\nevent: {event_name}\n"
                                f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                            )
                            emitted_at.setdefault(ts, set()).add(rid)
                            if ts > max_ts:
                                max_ts = ts
                        if max_ts > wm[kind]:
                            wm[kind] = max_ts
                            seen[kind] = emitted_at.get(max_ts, set())
                        else:
                            seen[kind] |= emitted_at.get(wm[kind], set())
                    if chunks:
                        beats = 0
                        yield "".join(chunks)
                    else:
                        beats += 1
                        if beats >= _SSE_HEARTBEAT_LOOPS:
                            beats = 0
                            yield ": heartbeat\n\n"
            except (asyncio.CancelledError, GeneratorExit):
                raise
            except Exception:
                logger.warning("sse stream error", exc_info=True)
            finally:
                with _sse_lock:
                    _sse["count"] = max(0, _sse["count"] - 1)
                logger.debug("sse client disconnected total=%d", _sse["count"])

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    @app.post("/api/v1/runs/{run_id}/approve")
    def approve(
        run_id: str, body: ApprovalRequest, request: Request, background_tasks: BackgroundTasks
    ) -> JSONResponse:
        # 승인/거절은 사이드이펙트를 재개하므로 승인 대기열(/approvals)과
        # 동일하게 lead 이상만 허용한다. 메신저 커맨드 경로는 _do_approve를
        # 직접 호출하므로 영향 없음.
        gated = _lead_gate(request)
        if gated is not None:
            return gated
        # spec 003 US4 — 세션 사용자의 run:approve user-permission 강제.
        # run의 project 태그(이름 문자열 가능)와 input.meta.task_id를
        # permission scope로 해석한다 — POST /runs의 run:execute와 동일 규칙.
        _appr_task_id = ""
        _appr_project_id = ""
        _appr_run = store.get_run(run_id)
        if _appr_run is not None:
            _appr_meta = _appr_run.input.get("meta") if isinstance(_appr_run.input, dict) else None
            if isinstance(_appr_meta, dict):
                _appr_task_id = str(_appr_meta.get("task_id") or "")
            if _appr_run.project:
                _appr_proj = store.get_project(_appr_run.project) or store.find_project_by_name(
                    _appr_run.project
                )
                if _appr_proj is not None:
                    _appr_project_id = str(_appr_proj.get("id") or "")
        denied = _require_session_capability(
            request,
            "run:approve",
            task_id=_appr_task_id,
            project_id=_appr_project_id,
            actor=_actor_label(request),
        )
        if denied is not None:
            return denied
        # spec 006 US2 FR-008 — budget 승인 대기 run은 graph interrupt가
        # 없으므로 _do_approve(resume) 경로가 아니라 예산 재심사 후
        # 처음부터 execute_run으로 재개한다.
        if _appr_run is not None and store.run_budget_approval_state(run_id) == "waiting":
            return _budget_approve(run_id, body, request, background_tasks)
        result = _do_approve(
            run_id,
            body.decision,
            body.comment,
            actor=_actor_label(request),
            background_tasks=background_tasks,
        )
        if not result["ok"]:
            code = (
                "not_found"
                if result["status"] == 404
                else ("invalid_state" if result["status"] == 409 else "resume_failed")
            )
            return err(result["status"], code, str(result["error"]), request)
        return JSONResponse(content=result["run"])

    def _budget_approve(
        run_id: str,
        body: ApprovalRequest,
        request: Request,
        background_tasks: BackgroundTasks,
    ) -> JSONResponse:
        """spec 006 US2 — budget 승인 대기 run의 승인/거절.

        승인 시 evaluate_admission + pending_budget_approvals를 재실행해
        여전히 임계 초과면 대기를 유지한다(409 budget_still_exceeded).
        통과하면 approved→queued로 전이한다. 실행 발사는 단일 경로로
        수렴한다(H2): 여기서 approved→dispatched를 원자 claim에 성공한
        경우에만 즉시 스케줄하고, claim 실패(경합·이미 dispatched)면
        jobs._process_budget_approvals가 다음 automation cycle에 집는다 —
        어느 경로든 dispatched 마커가 중복 runner.invoke를 막는다.
        run은 처음부터 실행되므로 graph resume이 아니라 신규 invoke다."""
        run = store.get_run(run_id)
        if run is None:
            return err(404, "not_found", "run not found", request)
        actor = _actor_label(request)
        if body.decision == "reject":
            if not store.reject_run_budget_approval(run_id):
                return err(
                    409,
                    "invalid_state",
                    "run is no longer waiting for budget approval",
                    request,
                )
            store.audit(run_id, actor, "run_budget_approval_reject", run_id, {})
            _audit_api(request, "run_budget_approval_reject", run_id, {})
            logger.info("run %s budget approval rejected actor=%s", run_id, actor)
            d = run_detail(run_id)
            return JSONResponse(content=d or {"ok": True})
        # 승인 — 예산 재심사(승인 시점 윈도우/지출 기준)
        task_id = ""
        meta = run.input.get("meta") if isinstance(run.input, dict) else None
        if isinstance(meta, dict):
            task_id = str(meta.get("task_id") or "")
        agent_id = str(run.agent_id or "")
        project = str(run.project or "")
        blocks = budget_scope.evaluate_admission(
            store, run.workflow_key, project, agent_id=agent_id, task_id=task_id
        )
        pending = budget_scope.pending_budget_approvals(
            store, run.workflow_key, project, agent_id=agent_id, task_id=task_id
        )
        if blocks or pending:
            _audit_api(
                request,
                "run_budget_approval_still_exceeded",
                run_id,
                {"blocks": len(blocks), "pending": len(pending)},
            )
            return err(
                409,
                "budget_still_exceeded",
                "budget threshold still exceeded; run remains waiting for approval",
                request,
            )
        if not store.approve_run_budget_approval(run_id):
            return err(
                409,
                "invalid_state",
                "run is no longer waiting for budget approval",
                request,
            )
        store.audit(run_id, actor, "run_budget_approval_approve", run_id, {})
        _audit_api(request, "run_budget_approval_approve", run_id, {})
        # approved→dispatched 원자 claim — 성공 시에만 즉시 스케줄한다.
        # 실패 시 run은 approved+queued로 남아 _process_budget_approvals가
        # 다음 사이클(~worker_interval_seconds)에 발사하므로 지연만 있고
        # 중복 실행은 없다. 응답은 어느 쪽이든 202 queued로 동일하다.
        if store.mark_budget_approval_dispatched(run_id):
            payload = {"task": str((run.input or {}).get("task") or "")}
            _schedule_run_jobs([(run.id, run.workflow_key, payload, None)], background_tasks)
            logger.info("run %s budget approval dispatched actor=%s", run_id, actor)
        else:
            logger.info(
                "run %s budget approval approved; dispatch deferred to automation cycle",
                run_id,
            )
        return JSONResponse(
            status_code=202,
            content={"id": run.id, "status": "queued", "budget_approved": True},
        )

    @app.get("/api/v1/runs/{run_id}/diff")
    def run_diff(run_id: str, request: Request) -> JSONResponse:
        """implement 단계 최신 산출물의 unified diff를 파일 단위로 집계."""
        if store.get_run(run_id) is None:
            return err(404, "not_found", "run not found", request)
        files_by_path: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        summary = ""
        for sr in store.get_stage_runs(run_id):
            if sr.stage_key != "implement" or sr.status != StageStatus.SUCCEEDED:
                continue
            out = sr.output or {}
            summary = str(out.get("summary") or summary)
            for f in _parse_diff_files(str(out.get("diff") or "")):
                if f["path"] not in files_by_path:
                    order.append(f["path"])
                files_by_path[f["path"]] = f
        return JSONResponse(
            content={"files": [files_by_path[p] for p in order], "summary": summary}
        )

    @app.get("/api/v1/runs/{run_id}/audit")
    def run_audit(run_id: str, request: Request) -> JSONResponse:
        """run의 audit_events — 최신순, 최대 200건 (lead 이상)."""
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        if store.get_run(run_id) is None:
            return err(404, "not_found", "run not found", request)
        events = [
            {
                "id": e.id,
                "actor": e.actor,
                "action": e.action,
                "object_ref": e.object_ref,
                "detail": e.detail,
                "created_at": e.created_at.isoformat(),
            }
            for e in store.get_audit(run_id)[-200:][::-1]
        ]
        return JSONResponse(content=events)

    @app.patch("/api/v1/runs/{run_id}")
    def patch_run(run_id: str, body: RunPatchRequest, request: Request) -> JSONResponse:
        """run의 project/goal 태그 설정/해제 (null/빈 문자열 → 해제). lead 이상."""
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        run = store.get_run(run_id)
        if run is None:
            return err(404, "not_found", "run not found", request)
        fields_set = body.model_fields_set
        if not fields_set:
            return err(422, "validation", "no fields to update", request)
        # 모든 검증을 먼저 끝낸 뒤 mutate — goal_id 422가 project 변경을
        # 부분 적용하는 것을 막는다(M4).
        new_project: str | None = None
        if "project" in fields_set:
            new_project = "" if body.project is None else budget_scope.clean_scope_id(body.project)
        new_goal: str | None = None
        if "goal_id" in fields_set:
            gid = (body.goal_id or "").strip()
            if gid:
                goal_row = store.get_goal(gid)
                if goal_row is None:
                    return err(422, "validation", "goal not found", request)
                if str(goal_row.get("status") or "") == "cancelled":
                    return err(422, "validation", "goal is cancelled", request)
            new_goal = gid
        if new_project is not None:
            if not store.set_run_project(run_id, new_project):
                return err(404, "not_found", "run not found", request)
            _audit_api(request, "run_project_set", run_id, {"project": new_project})
            logger.info("run %s project set to %r", run_id, new_project)
        if new_goal is not None:
            if not store.set_run_goal(run_id, new_goal):
                return err(404, "not_found", "run not found", request)
            _audit_api(request, "run_goal_set", run_id, {"goal_id": new_goal})
            logger.info("run %s goal set to %r", run_id, new_goal)
        d = run_detail(run_id)
        return JSONResponse(content=d or {"ok": True})

    # ---- spec 006 US4 FR-018/019 — retry-from-stage ----
    @app.post("/api/v1/runs/{run_id}/retry")
    def retry_run(
        run_id: str,
        request: Request,
        background_tasks: BackgroundTasks,
        from_stage: str = "",
    ) -> JSONResponse:
        """from_stage 이후를 pending으로 되돌리고 해당 단계부터 재실행.

        실행 경로는 _execute_run_job → runner.invoke(retry_from=...) —
        approve resume과 동일한 "checkpoint 이후만 실행" 의미론이며,
        succeeded 선행 단계는 stage_outputs 시드로 건너뛴다(FR-019).
        running/queued/waiting_* 상태는 409 invalid_state."""
        # 재실행은 LLM 비용을 다시 태우므로 run 생성과 같은 버스트 제한.
        if _run_rate_limited(_actor_label(request)):
            return err(429, "rate_limited", "run 재실행 요청이 너무 많습니다", request)
        run = store.get_run(run_id)
        if run is None:
            return err(404, "not_found", "run not found", request)
        plan = run_analysis.retry_plan(store, run, from_stage)
        if plan.get("error"):
            err_kind = str(plan.get("error") or "validation")
            if err_kind == "invalid_state":
                return err(409, "invalid_state", str(plan.get("message") or ""), request)
            if err_kind == "unknown_workflow":
                return err(422, "unknown_workflow", str(plan.get("message") or ""), request)
            return err(422, "validation", str(plan.get("message") or ""), request)
        stage_key = str(plan["from_stage"])
        # 사이드이펙트 워크플로 재실행은 run 생성과 동일하게 lead 이상만.
        if _workflow_needs_lead(store, run.workflow_key):
            gate = _lead_gate(request)
            if gate is not None:
                return gate
        # run:execute user-permission — POST /runs와 동일 scope 해석
        # (meta.task_id + project 태그 → canonical project id).
        _rt_task_id = ""
        _rt_project_id = ""
        _rt_meta = run.input.get("meta") if isinstance(run.input, dict) else None
        if isinstance(_rt_meta, dict):
            _rt_task_id = str(_rt_meta.get("task_id") or "")
        if run.project:
            _rt_proj = store.get_project(run.project) or store.find_project_by_name(run.project)
            if _rt_proj is not None:
                _rt_project_id = str(_rt_proj.get("id") or "")
        denied = _require_session_capability(
            request,
            "run:execute",
            task_id=_rt_task_id,
            project_id=_rt_project_id,
            actor=_actor_label(request),
        )
        if denied is not None:
            return denied
        # spec 006 US2 — retry도 run 생성(POST /runs)과 동일한 예산 게이트를
        # 통과한다(H1). 우회하면 pause/require_approval 정책이 재실행 비용을
        # 막지 못한다. 판정 순서도 create_run과 동일: 레거시 월간 → 스코프드
        # admission(차단 시 429) → require_approval 마킹(reset 이후).
        retry_project = budget_scope.clean_scope_id(run.project or "")
        retry_agent_id = str(run.agent_id or "")
        budget = _monthly_budget_usd()
        if budget is not None:
            mtd = _mtd_llm_cost_usd()
            if mtd > budget:
                _audit_api(
                    request,
                    "run_budget_exceeded",
                    run.workflow_key,
                    {"budget_usd": budget, "mtd_cost_usd": round(mtd, 6), "retry": True},
                )
                logger.warning(
                    "run retry rejected: monthly budget exceeded run=%s mtd=%.4f budget=%.4f",
                    run_id,
                    mtd,
                    budget,
                )
                return err(
                    429,
                    "budget_exceeded",
                    "monthly LLM budget exceeded",
                    request,
                )
        blocks = budget_scope.evaluate_admission(
            store,
            run.workflow_key,
            retry_project,
            agent_id=retry_agent_id,
            task_id=_rt_task_id,
        )
        if blocks:
            _audit_api(
                request,
                "run_budget_exceeded",
                run.workflow_key,
                {"blocks": blocks[:5], "retry": True},
            )
            logger.warning(
                "run retry rejected: scoped budget exceeded run=%s blocks=%s",
                run_id,
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
        # 원자 상태 전이 — 검증~전이 사이 상태가 바뀌었으면 rowcount 0 → 409.
        if not store.reset_run_for_retry(run_id, stage_key, actor=_actor_label(request)):
            return err(409, "invalid_state", "run is not in a retryable state", request)
        # require_approval 재판정 — reset이 status=queued로 전이했으므로
        # create_run이 쓰는 것과 동일한 마킹 경로를 재사용한다. 마킹되면
        # 실행을 예약하지 않고 waiting_approval을 반환한다 — 이후 승인은
        # POST /runs/{id}/approve, 실행 차단은 _execute_run_job 가드가 담당.
        # 실패해도 재시도 자체는 유지한다(create_run과 동일한 fail-open).
        try:
            run_oid = row_org_id(store, "runs", run_id)
            store._maybe_mark_budget_approval(  # noqa: SLF001 - create_run과 동일 마킹 경로 재사용
                run, dict(run.input or {}), retry_agent_id, run_oid
            )
        except Exception:
            logger.warning("budget approval recheck failed on retry run=%s", run_id, exc_info=True)
        if run.status == RunStatus.WAITING_APPROVAL:
            _audit_api(
                request,
                "run_budget_approval_wait",
                run_id,
                {"workflow": run.workflow_key, "retry": True},
            )
            logger.info(
                "run %s retry held for budget approval from_stage=%s",
                run_id,
                stage_key,
            )
            return JSONResponse(
                status_code=202,
                content={
                    "id": run_id,
                    "status": "waiting_approval",
                    "reason": "budget_approval",
                    "from_stage": stage_key,
                },
            )
        task = str((run.input or {}).get("task") or "") if isinstance(run.input, dict) else ""
        _schedule_run_jobs(
            [
                (
                    run_id,
                    run.workflow_key,
                    {"task": task},
                    {"_retry_from": stage_key},
                )
            ],
            background_tasks,
        )
        logger.info(
            "run %s retry scheduled from_stage=%s actor=%s",
            run_id,
            stage_key,
            _actor_label(request),
        )
        return JSONResponse(
            status_code=202,
            content={"id": run_id, "status": "queued", "from_stage": stage_key},
        )
