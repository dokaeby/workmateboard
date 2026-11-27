"""FastAPI 제공층 — 임시. 상용 단계 Django 전환 예정.

도메인 엔진(jobmate.engine/workflows)은 HTTP 프레임워크에 의존하지 않는다.

구조:
- create_app: 설정/Store/LLM/어댑터 조립, lifespan, 마운트, 미들웨어,
  예외 핸들러, 라우터 등록만 담당하는 composition root.
- appctx.build_app_context: 라우터에 주입되는 ctx 헬퍼(인증 게이트,
  run 실행/승인, 레이트리밋, 메신저 커맨드 등) 생성.
- routers/*: register(app, ctx)로 엔드포인트 정의. core.py가
  인덱스/헬스/워크플로 목록을, 나머지는 도메인별 모듈이 담당.
- jobs.py/support.py: automation loop·cron tick이 공유하는 백그라운드 작업.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from ..config import (
    Settings,
    configure_langsmith,
    configure_logging,
    database_dsn,
    get_settings,
)
from ..llm import CompositeLLM, MeteredLLM
from ..store.db import Store
from .appctx import build_app_context
from .jobs import _automation_loop
from .support import _default_adapters, _sync_workflow_registry

STATIC_DIR = Path(__file__).parent / "static"

logger = logging.getLogger("jobmate.api")


def create_app(
    settings: Settings | None = None,
    store: Store | None = None,
    llm: Any | None = None,
    adapters: dict[str, Any] | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)
    configure_langsmith(settings)
    from ..security import allowed_emails, api_key

    if not api_key() and not allowed_emails() and not os.environ.get("VERCEL"):
        logger.warning(
            "authentication disabled (local dev mode): only loopback clients are authorized — "
            "set JOBMATE_API_KEY or JOBMATE_ALLOWED_EMAILS before exposing this instance"
        )
    if store is None:
        try:
            store = Store(database_dsn(settings))
        except Exception:
            logger.exception(
                "database connect failed db=%s",
                "postgres" if settings.database_url else settings.db_path,
            )
            raise
    if os.environ.get("VERCEL") and not (
        settings.database_url
        or os.environ.get("POSTGRES_URL")
        or os.environ.get("POSTGRES_PRISMA_URL")
    ):
        logger.warning(
            "VERCEL without postgres DSN — sqlite db is ephemeral per-invocation; "
            "set JOBMATE_DATABASE_URL or POSTGRES_URL"
        )
    llm = MeteredLLM(llm or CompositeLLM(), store)
    adapters = adapters if adapters is not None else _default_adapters(settings, store)
    # custom_workflows 테이블 → WORKFLOWS 동기화 (재시작 후에도 /runs에서 해석 가능)
    _sync_workflow_registry(store)
    stop_event = threading.Event()
    # 라우터에 주입할 요청 컨텍스트 — 인스턴스별 상태(레이트리밋/SSE/세마포어)는
    # 여기서 생성돼 create_app 호출마다 격리된다.
    ctx = build_app_context(store=store, settings=settings, llm=llm, adapters=adapters)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logger.info(
            "jobmate api starting mode=%s db=%s serverless=%s",
            settings.mode,
            "postgres"
            if database_dsn(settings).startswith(("postgres://", "postgresql://"))
            else "sqlite",
            bool(os.environ.get("VERCEL")),
        )
        app.state.store = store
        app.state.settings = settings
        app.state.llm = llm
        app.state.adapters = adapters
        app.state.started_at = time.time()
        worker = None
        if not os.environ.get("VERCEL"):
            # 서버리스(Vercel)에서는 상주 스레드가 없으므로 cron tick 엔드포인트 사용
            worker = threading.Thread(
                target=_automation_loop,
                args=(store, settings, llm, adapters, stop_event),
                daemon=True,
            )
            worker.start()
            logger.info("automation worker started")
        yield
        stop_event.set()
        store.close()

    app = FastAPI(title="jobMate Harness", version="0.1.0", lifespan=lifespan)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # React 프론트엔드(v2) — 저장소 루트 v2/dist 빌드 산출물. 존재할 때만 마운트.
    _v2_dist = STATIC_DIR.parents[3] / "v2" / "dist"
    if _v2_dist.is_dir():
        app.mount("/app", StaticFiles(directory=_v2_dist, html=True), name="app_v2")

    @app.middleware("http")
    async def security_middleware(request: Request, call_next: Any) -> Any:
        from ..security import authorized

        path = request.url.path
        # API 키 인증: /api/* 전체. health 체크와 외부 webhook(자체 서명 검증),
        # cron tick(자체 시크릿), 계정 인증 엔드포인트는 제외
        # /api/v1/health만 공개 — /health/detail은 DB 경로·머신명 등 내부 정보가
        # 포함되므로 인증이 필요하다 (세션/API 키).
        if (
            path.startswith("/api/")
            and path != "/api/v1/health"
            and not path.startswith(
                (
                    "/api/v1/webhooks/",
                    "/api/v1/cron/tick",
                    "/api/v1/auth/",
                    # 초대 토큰은 bearer secret — 세션 없이도 정보 조회 가능,
                    # accept/decline은 라우트가 세션을 자체 검증한다
                    "/api/v1/invitations/",
                )
            )
        ):
            # 공개 공유 스냅샷 — GET만 인증 예외(POST /share는 세션 필요)
            public_share = path.startswith("/api/v1/share/") and request.method == "GET"
            if not public_share and not (
                authorized(request) or ctx._session_user(request) or ctx._db_api_key(request)
            ):
                logger.warning("api auth failed: %s", path)
                return ctx.err(401, "unauthorized", "invalid or missing API key", request)
            # DB 스코프드 키 — deny-by-default 스코프 검사(env 키·세션은 대상 아님)
            # 공개 공유 GET은 인증 예외이므로 스코프 검사도 적용하지 않는다
            if not public_share and not ctx._api_scope_allows(request):
                logger.warning("api scope denied: %s %s", request.method, path)
                return ctx.err(
                    403, "insufficient_scope", "API key scope does not allow this endpoint", request
                )
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )
        return response

    @app.middleware("http")
    async def access_log_middleware(request: Request, call_next: Any) -> Any:
        """접근 로그 — IP/메서드/경로/상태/크기/소요시간/UA/요청ID.
        쿼리스트링(secret 파라미터 포함 가능)과 요청 본문은 기록하지 않는다."""
        started = time.perf_counter()
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        # err() 등 하위 계층이 같은 request_id를 재사용하도록 state에 심는다 —
        # 접근 로그의 rid와 에러 응답의 request_id가 항상 일치해야 한다.
        request.state.request_id = request_id
        response = await call_next(request)
        elapsed_ms = (time.perf_counter() - started) * 1000
        response.headers.setdefault("X-Request-ID", request_id)
        path = request.url.path
        client = request.client.host if request.client else "-"
        ua = (request.headers.get("user-agent") or "-")[:80]
        size = response.headers.get("content-length", "-")
        msg = (
            f"access {request.method} {path} -> {response.status_code} "
            f'{size}B {elapsed_ms:.0f}ms ip={client} ua="{ua}" rid={request_id}'
        )
        if path.startswith("/api/") and path != "/api/v1/health":
            logger.info(msg)
        else:
            logger.debug(msg)
        return response

    @app.exception_handler(Exception)
    async def _unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
        """최상위 예외 → 500 JSON. 스택/내부 정보는 응답에 노출하지 않고
        logger.exception에 request_id를 포함해 접근 로그와 상관시킨다."""
        logger.exception(
            "unhandled exception rid=%s method=%s path=%s",
            getattr(request.state, "request_id", ""),
            request.method,
            request.url.path,
        )
        return ctx.err(500, "internal_error", "internal server error", request)

    from .routers import agents as _r_agents
    from .routers import auth as _r_auth
    from .routers import budgets as _r_budgets
    from .routers import builder as _r_builder
    from .routers import core as _r_core
    from .routers import evals as _r_evals
    from .routers import events as _r_events
    from .routers import goals as _r_goals
    from .routers import management as _r_management
    from .routers import messaging as _r_messaging
    from .routers import misc as _r_misc
    from .routers import ops as _r_ops
    from .routers import orgs as _r_orgs
    from .routers import paperclip as _r_paperclip
    from .routers import permissions as _r_permissions
    from .routers import push as _r_push
    from .routers import radar as _r_radar
    from .routers import routines as _r_routines
    from .routers import runs as _r_runs
    from .routers import sessions as _r_sessions
    from .routers import task_collab as _r_task_collab

    # core(페이지/헬스/워크플로 목록)는 원래 앱 내장 라우트 — 다른 라우터보다 먼저.
    _r_core.register(app, ctx)
    _r_runs.register(app, ctx)
    _r_ops.register(app, ctx)
    _r_sessions.register(app, ctx)
    _r_messaging.register(app, ctx)
    _r_events.register(app, ctx)
    _r_radar.register(app, ctx)
    _r_push.register(app, ctx)
    _r_auth.register(app, ctx)
    _r_misc.register(app, ctx)
    _r_builder.register(app, ctx)
    _r_budgets.register(app, ctx)
    _r_agents.register(app, ctx)
    _r_management.register(app, ctx)
    _r_goals.register(app, ctx)
    _r_evals.register(app, ctx)
    _r_task_collab.register(app, ctx)
    _r_routines.register(app, ctx)
    _r_permissions.register(app, ctx)
    _r_orgs.register(app, ctx)
    _r_paperclip.register(app, ctx)
    return app


app = create_app()
