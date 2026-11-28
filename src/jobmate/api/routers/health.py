"""Public health route — spec 004 US2 (FR-012).

GET /api/v1/health/public — 비인증 공개 상태 요약.
보안 미들웨어 인증 예외 목록에 등록되어 있으며, 응답은 화이트리스트
필드(status/version/uptime_s/components{name:status})만 포함한다.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ...services import health as health_svc

logger = logging.getLogger("jobmate.api")


def register(app: FastAPI, ctx: Any) -> None:
    store = ctx.store
    settings = ctx.settings

    @app.get("/api/v1/health/public")
    def health_public(request: Request) -> JSONResponse:
        try:
            started = float(getattr(request.app.state, "started_at", 0.0) or 0.0)
            body = health_svc.public_health(store, settings, started_at=started or None)
        except Exception as e:
            # 모니터링 경로 자체가 죽으면 안 된다 — 최소 응답으로 폴백
            logger.warning("public health check failed: %s", type(e).__name__)
            body = {"status": "down", "version": "0.1.0", "uptime_s": 0.0, "components": {}}
        return JSONResponse(
            content=body,
            headers={"Cache-Control": "no-store"},
        )
