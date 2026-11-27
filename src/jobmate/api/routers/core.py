"""Core routes — index pages, health probes, workflow catalog.

원래 app.py create_app 본문에 있던 라우트를 routers 컨벤션(register(app, ctx))으로
옮긴 것. 다른 라우터보다 먼저 register된다(원래 라우터 include 이전에 정의됐던
경로들과 동일한 등록 순서 유지).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from ...workflows import WORKFLOWS
from ..support import _REAPER_STATE, _last_cron_tick_at

STATIC_DIR = Path(__file__).parent.parent / "static"


def register(app: FastAPI, ctx: Any) -> None:
    store = ctx.store
    _admin_gate = ctx._admin_gate
    # React 프론트엔드(v2) — 저장소 루트 v2/dist 빌드 산출물. 존재할 때만 마운트.
    _v2_dist = STATIC_DIR.parents[3] / "v2" / "dist"

    def _legacy_index_html() -> HTMLResponse:
        # 로그인제가 도입되어 API 키는 더 이상 페이지에 주입하지 않는다.
        # ?key= 파라미터나 localStorage 키는 관리자/머신용 폴백으로 유지.
        # static/legacy/ — v2 React 전환 후 유지보수 중인 레거시 UI.
        # 신규 기능은 v2(v2/src/features)에만 추가하고, 이 파일은 제거 예정.
        html = (STATIC_DIR / "legacy" / "index.html").read_text(encoding="utf-8")
        # no-cache — 배포 후 heuristic 캐시가 구버전 HTML을 붙잡지 않게
        return HTMLResponse(
            html.replace("__JOBMATE_API_KEY__", ""),
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/")
    def index(request: Request) -> Response:
        # v2(React) 빌드가 있으면 기본 진입점으로 리다이렉트 — 없으면 레거시.
        # 쿼리스트링 보존: 푸시 알림 딥링크(?view=..)·OAuth 에러(?auth_error=..)가
        # 리다이렉트에서 유실되지 않게 한다.
        if _v2_dist.is_dir():
            qs = f"?{request.url.query}" if request.url.query else ""
            return RedirectResponse(
                f"/app/{qs}",
                status_code=302,
                headers={"Cache-Control": "no-cache"},
            )
        return _legacy_index_html()

    @app.get("/legacy")
    def legacy_index() -> HTMLResponse:
        # v2 전환 기간 동안 레거시 대시보드 유지 경로.
        return _legacy_index_html()

    @app.get("/sw.js")
    def service_worker() -> FileResponse:
        # 루트 스코프로 서빙해야 push 수신 후 openWindow 등이 동작한다.
        # no-cache — 구버전 SW가 heuristic 캐시로 잔존해 업데이트를 막지 않게.
        return FileResponse(
            STATIC_DIR / "sw.js",
            media_type="application/javascript",
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/api/v1/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/v1/health/detail")
    def health_detail(request: Request) -> JSONResponse:
        """헬스 상세 — DB 경로·머신명 등 내부 정보 포함이므로 admin 전용."""
        gate = _admin_gate(request)
        if gate is not None:
            return gate
        started = float(getattr(app.state, "started_at", time.time()))
        bridge = store.agent_sessions_bridge_info()
        return JSONResponse(
            content={
                "api": {"uptime_s": round(time.time() - started, 1)},
                "db": store.db_info(),
                "bridge": bridge,
                "cron": {
                    "last_tick_at": _last_cron_tick_at(store),
                    "reaper": dict(_REAPER_STATE),
                },
                "version": "0.1.0",
            }
        )

    @app.get("/api/v1/workflows")
    def list_workflows() -> list[dict[str, Any]]:
        return [
            {
                "key": w.key,
                "version": w.version,
                "description": w.description,
                "stages": [
                    {
                        "key": s.key,
                        "kind": s.kind,
                        "description": s.description,
                        "agent": s.agent,
                        "function": s.function,
                    }
                    for s in w.stages
                ],
            }
            for w in WORKFLOWS.values()
        ]
