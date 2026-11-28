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
from ...models import AgentCapability, StageStatus, canonical_agent_status
from ...services import agent_management as mgmt
from ...services import permissions as perm_svc
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
            _perm_task_id = ""
            if isinstance(body.metadata, dict):
                _perm_task_id = str(body.metadata.get("task_id") or "")
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
    def list_runs(limit: int = 50, workflow: str | None = None) -> list[dict[str, Any]]:
        runs = store.list_runs(max(1, min(limit, 500)), workflow=workflow)
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
