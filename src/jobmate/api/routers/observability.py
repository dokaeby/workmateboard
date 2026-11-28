"""spec 007 — 관측성 플랫폼 라우트.

GET /api/v1/runs/{id}/timeline — stage+cost 워터폴 프레임 데이터 (US1)
GET /api/v1/observability/{costs,usage,errors,approvals,anomalies} —
대시보드 집계 (US2/US3/US5/US7).

모든 엔드포인트 lead 이상(_lead_gate) — stage 입출력·비용·승인 대기
같은 운영 민감 데이터라 member에게는 열지 않는다. org 스코프는
store 계층의 orgctx 필터가 강제한다.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ...store.otlp_ingest import external_span_json

logger = logging.getLogger("jobmate.api")


def register(app: FastAPI, ctx: Any) -> None:
    store = ctx.store
    err = ctx.err
    _lead_gate = ctx._lead_gate

    @app.get("/api/v1/runs/{run_id}/timeline")
    def run_timeline(run_id: str, request: Request) -> JSONResponse:
        """run 1건의 stage 타임라인 — stage_runs + stage별 cost 합산."""
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        # get_run은 org 바인딩 조회 — 다른 조직/미존재 run은 404로 수렴.
        if store.get_run(run_id) is None:
            return err(404, "not_found", "run not found", request)
        stages = store.run_timeline(run_id)
        # spec 008 US1 — jobmate.run_id로 링크된 외부 OTLP 스팬을 타임라인에 병합.
        # 믹스인 미배선(Store에 external_spans_for_run 없음) 환경에서는 빈 배열.
        _spans_for_run = getattr(store, "external_spans_for_run", None)
        ext = (
            [external_span_json(r) for r in _spans_for_run(run_id)]
            if _spans_for_run is not None
            else []
        )
        return JSONResponse(content={"run_id": run_id, "stages": stages, "external_spans": ext})

    @app.get("/api/v1/observability/costs")
    def observability_costs(
        request: Request, days: int = 30, group_by: str = "model"
    ) -> JSONResponse:
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        data = store.observability_costs(days, group_by)
        return JSONResponse(content=data)

    @app.get("/api/v1/observability/usage")
    def observability_usage(request: Request, days: int = 30) -> JSONResponse:
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        return JSONResponse(content=store.observability_usage(days))

    @app.get("/api/v1/observability/errors")
    def observability_errors(request: Request, days: int = 30) -> JSONResponse:
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        return JSONResponse(content=store.observability_errors(days))

    @app.get("/api/v1/observability/approvals")
    def observability_approvals(request: Request) -> JSONResponse:
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        return JSONResponse(content=store.observability_approvals())

    @app.get("/api/v1/observability/anomalies")
    def observability_anomalies(request: Request, days: int = 30) -> JSONResponse:
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        items = store.list_anomaly_events(days=days)
        return JSONResponse(content={"days": max(1, min(int(days), 365)), "items": items})

    # ---- spec 011 US1 — 관제센터 모듈 집계 ----

    @app.get("/api/v1/observability/notifications")
    def observability_notifications(request: Request, days: int = 7) -> JSONResponse:
        """알림함 집계 — 총량/open/resolved + 일별 시계열 + 최근 알림."""
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        return JSONResponse(content=store.observability_notifications(days))

    @app.get("/api/v1/observability/funnel")
    def observability_funnel(request: Request, days: int = 7, workflow: str = "") -> JSONResponse:
        """실행 퍼널 — received→planned→assigned→executed 단계별 도달 수."""
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        return JSONResponse(content=store.observability_funnel(days, workflow))

    @app.get("/api/v1/observability/agents/live")
    def observability_agents_live(request: Request) -> JSONResponse:
        """활성 에이전트/세션 카드 — agent_sessions 최신 활동 순."""
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        return JSONResponse(content=store.observability_agents_live())

    # ---- spec 012 US2: Tool Call 통계 ----

    @app.get("/api/v1/observability/tool-stats")
    def observability_tool_stats(request: Request, days: int = 7) -> JSONResponse:
        """external_spans 기반 tool/LLM 호출 통계 — by_name/by_kind 집계."""
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        return JSONResponse(content=store.observability_tool_stats(days))

    @app.get("/api/v1/observability/swarm")
    def observability_swarm(request: Request, run_id: str = "", days: int = 7) -> JSONResponse:
        """노드/엣지 그래프 — run_id 지정 시 해당 run, 아니면 최근 활성 run."""
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        # get_run은 org 바인딩 조회 — 다른 조직/미존재 run은 404로 수렴.
        if run_id and store.get_run(run_id) is None:
            return err(404, "not_found", "run not found", request)
        return JSONResponse(content=store.observability_swarm(run_id, days))
