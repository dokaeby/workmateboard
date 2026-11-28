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

import hmac
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
from ..services import metrics as _metrics
from ..services import sentry as _sentry
from ..store.db import Store
from .appctx import build_app_context
from .jobs import _automation_loop
from .support import _default_adapters

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
    # spec 004 US4 — Sentry는 DSN 설정 시에만 초기화, 실패해도 기동 비차단
    _sentry.configure_sentry(settings)
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
    store._activity_retention_days = max(1, settings.session_activity_retention_days)
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
    # 커스텀 워크플로는 더 이상 전역 WORKFLOWS에 동기화하지 않는다 —
    # spec 003 US3: 조회 시점에 조직 컨텍스트로 custom_workflows 테이블에서
    # 해석한다(workflows.resolver). 재시작 후에도 별도 복원 없이 해석된다.
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

    # spec 006 US7 FR-024 — 프로덕션(release/VERCEL)에서는 API 스키마·Swagger
    # UI를 비활성화해 내부 엔드포인트 구조가 외부에 노출되지 않게 한다.
    # 개발(debug) 환경에서는 기존대로 공개.
    _prod_docs_off = settings.mode == "release" or bool(os.environ.get("VERCEL"))
    app = FastAPI(
        title="jobMate Harness",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None if _prod_docs_off else "/docs",
        redoc_url=None if _prod_docs_off else "/redoc",
        openapi_url=None if _prod_docs_off else "/openapi.json",
    )

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
                    "/api/v1/health/public",
                )
            )
        ):
            # 공개 공유 스냅샷·공유 템플릿 — GET만 인증 예외
            # (POST /share는 세션 필요)
            public_share = (
                path.startswith("/api/v1/share/") or path.startswith("/api/v1/templates/shared/")
            ) and request.method == "GET"
            # spec 004 US5 FR-032 — 스크래이퍼용 별도 bearer(JOBMATE_METRICS_TOKEN).
            # /api/v1/metrics GET에만 허용하고, 세션/API 키 인증은 그대로 요구된다.
            metrics_scrape = False
            _mt = settings.metrics_token
            if path == "/api/v1/metrics" and request.method == "GET" and _mt:
                _auth = request.headers.get("authorization", "")
                if _auth.lower().startswith("bearer ") and hmac.compare_digest(
                    _auth[7:].strip().encode(), _mt.encode()
                ):
                    metrics_scrape = True
            if (
                not public_share
                and not metrics_scrape
                and not (
                    authorized(request) or ctx._session_user(request) or ctx._db_api_key(request)
                )
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
        # spec 003 US3 — 요청 조직 컨텍스트. /api/* 요청에만 설정해
        # store 계층의 org 스탬프/필터가 동작하게 한다. 비인증·면제
        # 경로(webhook·공유·cron)는 ""가 돼 무필터+부모 상속 규칙 적용.
        org_tok = None
        if path.startswith("/api/"):
            from ..store import orgctx

            # 해석된 조직을 request.state에도 심는다 — 아래 finally가 org ctx를
            # 리셋한 뒤 _unhandled_exception이 emit 대상을 복원할 수 있게 하기
            # 위함이다(컨텍스트 소실 시 전 조직 팬아웃 방지).
            _req_org = ctx._request_org_id(request)
            request.state.org_id = _req_org
            org_tok = orgctx.set_org_context(_req_org)
        try:
            # FR-031/FR-028 — suspended 조직은 owner 읽기 전용·멤버 전면 차단,
            # deleted 조직은 멤버 전면 차단. 세션 사용자만 대상.
            block = ctx._org_access_block(request)
            if block is not None:
                return block
            # spec 005 FR-013 — 재동의 요구(consent_enforce=read_only|block)
            block = ctx.consent_block_response(request)
            if block is not None:
                return block
            response = await call_next(request)
        finally:
            if org_tok is not None:
                from ..store import orgctx

                orgctx.reset_org_context(org_tok)
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
        # spec 004 US5 — HTTP 메트릭. 미들웨어는 라우팅 전에 실행되므로
        # scope["route"]가 없으면 등록 패턴을 직접 매칭해 템플릿으로 정규화한다.
        # 메트릭 실패가 요청 처리를 막지 않도록 전체를 예외로 격리한다.
        try:
            _route = request.scope.get("route")
            _template = getattr(_route, "path", None)
            if not _template:
                _template = _metrics.route_template(app.routes, path)
            _metrics.observe_http_request(
                request.method,
                _template or "unmatched",
                response.status_code,
                time.perf_counter() - started,
            )
        except Exception:
            logger.debug("http metrics observe failed", exc_info=True)
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
        # spec 004 US4/US5 — path는 라우트 템플릿으로 정규화한다.
        # /api/v1/share/<token> 같은 bearer 경로의 원시 path가 Sentry 태그·
        # 웹훅 페이로드·event_id로 새는 것을 막는다.
        _path_tmpl = _metrics.route_template(app.routes, request.url.path)
        # spec 004 US4 FR-026 — Sentry 캡처(미설정 시 no-op).
        # 태그는 화이트리스트 키만 통과하며 이메일·본문·토큰은 제외된다.
        _sentry.capture_exception(
            exc,
            tags={
                "request_id": getattr(request.state, "request_id", ""),
                "path": _path_tmpl,
                "method": request.method,
            },
        )
        # spec 004 FR-003 — error.unhandled 웹훅 이벤트(현재 조직 구독 대상).
        # 보안 미들웨어 finally가 org ctx를 리셋하므로 request.state.org_id로만
        # 대상을 복원한다 — 비어 있거나 '__no_org__' 센티널이면 전 조직
        # 팬아웃을 피해 emit 자체를 생략한다. 알림 실패가 500 응답을 막지
        # 않도록 완전 격리한다.
        _oid = str(getattr(request.state, "org_id", "") or "")
        if _oid and _oid != "__no_org__":
            try:
                from ..services import webhooks as _webhooks
                from ..store import orgctx

                # event_id는 템플릿 + 서버 생성 suffix — 클라이언트가
                # x-request-id를 조작해 dedupe를 억제하지 못하게 한다.
                _event_id = (
                    f"error:{getattr(request.state, 'request_id', '')}"
                    f":{request.method}:{_path_tmpl}:{uuid.uuid4().hex[:8]}"
                )
                with orgctx.org_scope(_oid):
                    _webhooks.emit_event(
                        store,
                        "error.unhandled",
                        _event_id,
                        {
                            "method": request.method,
                            "path": _path_tmpl,
                            "error_type": type(exc).__name__,
                        },
                    )
            except Exception:
                logger.debug("error.unhandled webhook emit failed", exc_info=True)
        return ctx.err(500, "internal_error", "internal server error", request)

    from ..store.orgs import OrgInactiveError

    @app.exception_handler(OrgInactiveError)
    async def _org_inactive_exc(request: Request, exc: OrgInactiveError) -> JSONResponse:
        """FR-031 — suspended/deleted 조직 run 생성 시도(store.create_run 중앙 차단)."""
        return ctx.err(403, "org_inactive", "organization is not active", request)

    from .routers import agents as _r_agents
    from .routers import alerts as _r_alerts
    from .routers import auth as _r_auth
    from .routers import backups as _r_backups
    from .routers import budgets as _r_budgets
    from .routers import builder as _r_builder
    from .routers import core as _r_core
    from .routers import evals as _r_evals
    from .routers import events as _r_events
    from .routers import goals as _r_goals
    from .routers import health as _r_health
    from .routers import management as _r_management
    from .routers import me as _r_me
    from .routers import messaging as _r_messaging
    from .routers import metrics as _r_metrics
    from .routers import misc as _r_misc
    from .routers import observability as _r_observability
    from .routers import ops as _r_ops
    from .routers import orgs as _r_orgs
    from .routers import paperclip as _r_paperclip
    from .routers import permissions as _r_permissions
    from .routers import push as _r_push
    from .routers import radar as _r_radar
    from .routers import retention as _r_retention
    from .routers import routines as _r_routines
    from .routers import runs as _r_runs
    from .routers import sessions as _r_sessions
    from .routers import task_collab as _r_task_collab
    from .routers import templates as _r_templates
    from .routers import webhooks as _r_webhooks

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
    _r_backups.register(app, ctx)
    _r_health.register(app, ctx)
    _r_webhooks.register(app, ctx)
    _r_metrics.register(app, ctx)
    _r_retention.register(app, ctx)
    _r_me.register(app, ctx)
    _r_templates.register(app, ctx)
    _r_alerts.register(app, ctx)
    _r_observability.register(app, ctx)
    return app


app = create_app()
