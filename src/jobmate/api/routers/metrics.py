"""Prometheus metrics endpoint — spec 004 US5 (FR-029~FR-032).

GET /api/v1/metrics — text exposition 0.0.4. 인증은 보안 미들웨어의
/api/* 규칙(API 키/세션) 또는 JOBMATE_METRICS_TOKEN bearer(FR-032).
고빈도 경로라 store.audit은 남기지 않고 접근 로그만 기존 미들웨어가 기록한다.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI
from fastapi.responses import Response

from ...services import metrics as metrics_svc

logger = logging.getLogger("jobmate.api")


def register(app: FastAPI, ctx: Any) -> None:
    store = ctx.store
    settings = ctx.settings

    @app.get("/api/v1/metrics")
    def metrics_endpoint() -> Response:
        try:
            body = metrics_svc.render_metrics(store, settings)
        except Exception:
            # 메트릭 실패가 모니터링 경로 자체를 죽이면 안 된다 — 최소 exposition.
            logger.warning("metrics render failed", exc_info=True)
            body = "# jobMate metrics render failed\n"
        return Response(
            content=body,
            media_type="text/plain; version=0.0.4; charset=utf-8",
            headers={"Cache-Control": "no-store"},
        )
