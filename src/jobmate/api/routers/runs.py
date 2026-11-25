"""Runs/streaming/approval route handlers extracted from api/app.py."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, StreamingResponse

from ...models import StageStatus
from ...workflows import WORKFLOWS
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
        if body.workflow not in WORKFLOWS:
            return err(404, "unknown_workflow", f"workflow '{body.workflow}' not found", request)
        # 사이드이펙트 워크플로(gmail_calendar의 실제 캘린더 쓰기, 커스텀 spec의
        # 외부 부수효과 function stage)는 lead 이상만 실행 가능 — member는
        # dev_review* 등 읽기/검토성 워크플로만.
        if _workflow_needs_lead(body.workflow):
            gate = _lead_gate(request)
            if gate is not None:
                return gate
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
        run = store.create_run(
            body.workflow,
            WORKFLOWS[body.workflow].version,
            {"task": body.task, **({"meta": body.metadata} if body.metadata else {})},
            trace_id=uuid.uuid4().hex,
            created_by=_actor_label(request),
        )
        logger.info("run created id=%s workflow=%s", run.id, body.workflow)
        _schedule_run_jobs([(run.id, body.workflow, {"task": body.task}, None)], background_tasks)
        return JSONResponse(status_code=202, content={"id": run.id, "status": run.status.value})

    @app.get("/api/v1/runs")
    def list_runs(limit: int = 50, workflow: str | None = None) -> list[dict[str, Any]]:
        return [
            {
                "id": r.id,
                "workflow": r.workflow_key,
                "status": r.status.value,
                "project": r.project,
                "created_at": r.created_at.isoformat(),
            }
            for r in store.list_runs(max(1, min(limit, 500)), workflow=workflow)
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
    def approve(run_id: str, body: ApprovalRequest, request: Request) -> JSONResponse:
        # 승인/거절은 사이드이펙트를 재개하므로 승인 대기열(/approvals)과
        # 동일하게 lead 이상만 허용한다. 메신저 커맨드 경로는 _do_approve를
        # 직접 호출하므로 영향 없음.
        gated = _lead_gate(request)
        if gated is not None:
            return gated
        result = _do_approve(run_id, body.decision, body.comment, actor=_actor_label(request))
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
        """run의 project 태그 설정/해제 (null/빈 문자열 → 해제). lead 이상."""
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        run = store.get_run(run_id)
        if run is None:
            return err(404, "not_found", "run not found", request)
        project = "" if body.project is None else body.project.strip()[:200]
        if not store.set_run_project(run_id, project):
            return err(404, "not_found", "run not found", request)
        _audit_api(request, "run_project_set", run_id, {"project": project})
        logger.info("run %s project set to %r", run_id, project)
        d = run_detail(run_id)
        return JSONResponse(content=d or {"ok": True})
