"""FastAPI 제공층 — 임시. 상용 단계 Django 전환 예정.

도메인 엔진(jobmate.engine/workflows)은 HTTP 프레임워크에 의존하지 않는다.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import messenger
from ..adapters import LoggingNotifier, WebPushNotifier
from ..config import (
    Settings,
    configure_langsmith,
    configure_logging,
    database_dsn,
    get_settings,
)
from ..llm import CompositeLLM, MeteredLLM
from ..models import RunStatus, StageStatus
from ..pricing import cost_usd
from ..radar import GitHubAPI
from ..sensors import (
    allowed_identifier,
    build_gmail_url,
    is_safe_source_url,
    verify_slack_signature,
    verify_telegram_secret,
)
from ..store.db import Store
from ..workflows import WORKFLOWS
from ..workflows.registry_builder import build_runner

STATIC_DIR = Path(__file__).parent / "static"

logger = logging.getLogger("jobmate.api")
worker_logger = logging.getLogger("jobmate.worker")


def _endpoint_fp(endpoint: str) -> str:
    """push endpoint 비식별 지문 — 원문 대신 SHA256 앞 12자."""
    import hashlib

    return hashlib.sha256(endpoint.encode()).hexdigest()[:12]


# 세션 토큰 누적치 → USD 추정치 (1M 토큰당). tool별 오버라이드, 없으면 default.
_SESSION_TOKEN_PRICES: dict[str, tuple[float, float]] = {
    "default": (3.0, 15.0),
    "claude": (3.0, 15.0),
    "codex": (1.5, 6.0),
    "gemini": (1.0, 4.0),
}

_SESSION_STALE_SECONDS = 300.0
_SESSION_LONG_IDLE_SECONDS = 43200.0


def _parse_session_ts(value: Any) -> datetime | None:
    """ISO 타임스탬프 파싱 — 실패/naive는 UTC 간주."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _session_health(row: dict[str, Any]) -> str:
    """세션 상태 건강도 — ended/stale/long_idle/active (Python 계산, SQL 아님)."""
    status = str(row.get("status") or "")
    if status == "ended":
        return "ended"
    ts = _parse_session_ts(row.get("last_event_at")) or _parse_session_ts(row.get("updated_at"))
    age = (datetime.now(UTC) - ts).total_seconds() if ts is not None else 0.0
    if status in ("working", "decision"):
        return "stale" if age > _SESSION_STALE_SECONDS else "active"
    if status == "rest":
        return "long_idle" if age > _SESSION_LONG_IDLE_SECONDS else "active"
    return "active"


def _session_est_cost_usd(row: dict[str, Any]) -> float | None:
    """tokens_in/out 누적치 → USD 추정. 둘 다 없으면 None."""
    tin = row.get("tokens_in")
    tout = row.get("tokens_out")
    if tin is None and tout is None:
        return None
    price_in, price_out = _SESSION_TOKEN_PRICES.get(
        str(row.get("tool") or ""), _SESSION_TOKEN_PRICES["default"]
    )
    return round(((tin or 0) * price_in + (tout or 0) * price_out) / 1_000_000, 6)


def _session_view(row: dict[str, Any]) -> dict[str, Any]:
    return {**row, "health": _session_health(row), "est_cost_usd": _session_est_cost_usd(row)}


# 마지막 자동화 틱 시각 — /health/detail의 cron.last_tick_at (모듈 공유)
_LAST_CRON_TICK_AT: str | None = None


def _mark_cron_tick() -> None:
    global _LAST_CRON_TICK_AT
    _LAST_CRON_TICK_AT = datetime.now(UTC).isoformat()


def _last_cron_tick_at() -> str | None:
    return _LAST_CRON_TICK_AT


class RunCreateRequest(BaseModel):
    workflow: str = "dev_review"
    task: str = Field(min_length=1, max_length=20_000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ApprovalRequest(BaseModel):
    decision: str = Field(pattern="^(approve|reject)$")
    comment: str = ""


class ScheduleCreateRequest(BaseModel):
    workflow: str
    input: dict[str, Any] = Field(default_factory=dict)
    interval_minutes: int = Field(ge=5)


class ScheduleToggleRequest(BaseModel):
    enabled: bool


class EventReviewRequest(BaseModel):
    action: str = Field(pattern="^(approve|reject)$")
    title: str | None = None
    start_at: str | None = None
    end_at: str | None = None
    timezone: str | None = None
    location: str | None = None


class AgentSessionRequest(BaseModel):
    """로컬 에이전트 브릿지 push — tool은 알려진 값 외에도 허용(미래 도구 대비)."""

    tool: str = Field(min_length=1, max_length=64)
    session_key: str = Field(min_length=1, max_length=512)
    status: Literal["working", "decision", "rest", "ended"]
    project: str | None = Field(default=None, max_length=512)
    title: str | None = Field(default=None, max_length=500)
    doing: str | None = Field(default=None, max_length=500)
    ask: str | None = Field(default=None, max_length=500)
    event_at: str | None = Field(default=None, max_length=64)
    ended_summary: str | None = Field(default=None, max_length=500)
    artifacts: list[Annotated[str, Field(max_length=300)]] | None = Field(
        default=None, max_length=10
    )
    machine: str | None = Field(default=None, max_length=128)
    tokens_in: int | None = Field(default=None, ge=0)
    tokens_out: int | None = Field(default=None, ge=0)


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


class ReportGenerateRequest(BaseModel):
    kind: Literal["daily", "weekly"] = "daily"


def _token_path_for(kind: str, settings: Settings) -> str | None:
    """Google 토큰 파일 경로 해석.

    서버리스(Vercel)에는 토큰 파일이 없으므로 JOBMATE_{KIND}_TOKEN_JSON env의
    토큰 JSON을 임시 파일(0600)로 materialize한다. 로컬은 설정된 경로가
    실제 존재할 때만 사용 — 경로만 있고 파일이 없으면 어댑터 생성을 건너뛴다.
    """
    import tempfile

    env_json = os.environ.get(f"JOBMATE_{kind.upper()}_TOKEN_JSON", "")
    if env_json:
        try:
            json.loads(env_json)
        except ValueError:
            logger.warning("%s token json env is invalid JSON — adapter disabled", kind)
        else:
            fd, path = tempfile.mkstemp(prefix=f"jobmate-{kind}-token-", suffix=".json")
            with os.fdopen(fd, "w") as f:
                f.write(env_json)
            os.chmod(path, 0o600)
            logger.info("%s token materialized from env to temp file", kind)
            return path
    configured = getattr(settings, f"{kind}_token_path", None)
    if configured:
        p = Path(configured).expanduser()
        if p.is_file():
            return str(p)
        logger.warning("%s token path configured but file missing: %s", kind, p)
    return None


def _default_adapters(settings: Settings, store: Store) -> dict[str, Any]:
    from ..adapters.google import GoogleCalendar, GoogleGmail

    adapters: dict[str, Any] = {
        "notifier": LoggingNotifier(),
        "github": GitHubAPI(token=os.environ.get(settings.github_token_env, "")),
        "webpush": WebPushNotifier(
            store,
            private_key=os.environ.get("VAPID_PRIVATE_KEY", ""),
            subject=os.environ.get("VAPID_SUBJECT", "mailto:admin@localhost"),
        ),
    }
    # 실제 Google 연동 — 토큰 파일/env JSON이 확인된 경우에만 주입
    gmail_path = _token_path_for("gmail", settings)
    if gmail_path:
        adapters["gmail"] = GoogleGmail(gmail_path)
    cal_path = _token_path_for("calendar", settings)
    if cal_path:
        adapters["calendar"] = GoogleCalendar(cal_path)
    return adapters


def create_app(
    settings: Settings | None = None,
    store: Store | None = None,
    llm: Any | None = None,
    adapters: dict[str, Any] | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)
    configure_langsmith(settings)
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
    stop_event = threading.Event()

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

    @app.middleware("http")
    async def security_middleware(request: Request, call_next: Any) -> Any:
        from ..security import authorized

        path = request.url.path
        # API 키 인증: /api/* 전체. health 체크와 외부 webhook(자체 서명 검증),
        # cron tick(자체 시크릿), 계정 인증 엔드포인트는 제외
        if path.startswith("/api/") and not path.startswith(
            (
                "/api/v1/health",
                "/api/v1/webhooks/",
                "/api/v1/cron/tick",
                "/api/v1/auth/",
            )
        ):
            if not (authorized(request) or _session_user(request)):
                logger.warning("api auth failed: %s", path)
                return err(401, "unauthorized", "invalid or missing API key", request)
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

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    def err(status: int, code: str, message: str, request: Request) -> JSONResponse:
        return JSONResponse(
            status_code=status,
            content={
                "error": {
                    "code": code,
                    "message": message,
                    "request_id": request.headers.get("x-request-id", uuid.uuid4().hex[:12]),
                }
            },
        )

    def run_detail(run_id: str) -> dict[str, Any] | None:
        run = store.get_run(run_id)
        if run is None:
            return None
        return {
            "id": run.id,
            "workflow": run.workflow_key,
            "version": run.workflow_version,
            "status": run.status.value,
            "trace_id": run.trace_id,
            "error": run.error,
            "input": run.input,
            "metadata": run.metadata,
            "created_at": run.created_at.isoformat(),
            "updated_at": run.updated_at.isoformat(),
            "stages": [s.model_dump(mode="json") for s in store.get_stage_runs(run_id)],
            "approvals": [a.model_dump(mode="json") for a in store.get_approvals(run_id)],
            "audit": [a.model_dump(mode="json") for a in store.get_audit(run_id)],
        }

    def execute_run(
        run_id: str, workflow: str, payload: dict[str, Any], ctx: dict[str, Any] | None = None
    ) -> None:
        _execute_run_job(store, settings, llm, adapters, run_id, workflow, payload, ctx)

    def _run_jobs_parallel(
        jobs: list[tuple[str, str, dict[str, Any], dict[str, Any] | None]],
    ) -> None:
        """서버리스 waitUntil 구간에서 run들을 병렬 실행 (daemon thread 대체)."""
        if not jobs:
            return
        with ThreadPoolExecutor(max_workers=min(len(jobs), 4)) as pool:
            list(pool.map(lambda j: execute_run(*j), jobs))

    def _schedule_run_jobs(
        jobs: list[tuple[str, str, dict[str, Any], dict[str, Any] | None]],
        background_tasks: BackgroundTasks | None,
    ) -> None:
        """VERCEL에서는 FastAPI BackgroundTasks로 응답 후에도 함수 수명을 연장해
        run이 완료까지 실행되게 한다. 그 외 환경은 기존 daemon thread 방식."""
        if not jobs:
            return
        if os.environ.get("VERCEL") and background_tasks is not None:
            background_tasks.add_task(_run_jobs_parallel, jobs)
            return
        for job in jobs:
            threading.Thread(target=execute_run, args=job, daemon=True).start()

    def submit_run(
        workflow: str,
        task: str,
        ctx: dict[str, Any] | None = None,
        defer: list[tuple[str, str, dict[str, Any], dict[str, Any] | None]] | None = None,
    ) -> str:
        run = store.create_run(
            workflow, WORKFLOWS[workflow].version, {"task": task}, trace_id=uuid.uuid4().hex
        )
        job = (run.id, workflow, {"task": task}, ctx)
        if defer is not None:
            defer.append(job)
        else:
            threading.Thread(target=execute_run, args=job, daemon=True).start()
        return run.id

    def _session_user(request: Request) -> dict[str, Any] | None:
        """세션 쿠키 → 유효 사용자. 만료/무효 시 None."""
        from ..security import SESSION_COOKIE, email_allowed, hash_token

        token = request.cookies.get(SESSION_COOKIE, "")
        if not token:
            return None
        user = store.get_session_user(hash_token(token))
        return user if user and email_allowed(user["email"]) else None

    def _effective_role(user: dict[str, Any]) -> str:
        """저장된 role + JOBMATE_ADMIN_EMAILS env 오버라이드."""
        from ..security import admin_emails

        if str(user.get("role") or "user") == "admin":
            return "admin"
        if (user.get("email") or "").strip().lower() in admin_emails():
            return "admin"
        return "user"

    def _admin_user(request: Request) -> dict[str, Any] | None:
        """관리자 판정 — 세션 사용자 role=admin, 유효 API 키(머신), 또는
        인증이 비활성인 로컬 개발 모드(키·허용목록·VERCEL 모두 없음)일 때 허용."""
        from ..security import allowed_emails, api_key, authorized

        user = _session_user(request)
        if user is not None:
            if _effective_role(user) == "admin":
                return {**user, "role": "admin"}
            return None
        if api_key() and authorized(request):
            return {"id": "api-key", "email": "", "role": "admin"}
        if not api_key() and not allowed_emails() and not os.environ.get("VERCEL"):
            return {"id": "local", "email": "", "role": "admin"}
        return None

    @app.get("/")
    def index() -> HTMLResponse:
        # 로그인제가 도입되어 API 키는 더 이상 페이지에 주입하지 않는다.
        # ?key= 파라미터나 localStorage 키는 관리자/머신용 폴백으로 유지.
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(html.replace("__JOBMATE_API_KEY__", ""))

    @app.get("/sw.js")
    def service_worker() -> FileResponse:
        # 루트 스코프로 서빙해야 push 수신 후 openWindow 등이 동작한다
        return FileResponse(STATIC_DIR / "sw.js", media_type="application/javascript")

    @app.get("/api/v1/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/v1/health/detail")
    def health_detail() -> dict[str, Any]:
        """헬스 상세 — /health와 동일하게 인증 예외 경로(공개 수준 유지)."""
        started = float(getattr(app.state, "started_at", time.time()))
        bridge = store.agent_sessions_bridge_info()
        return {
            "api": {"uptime_s": round(time.time() - started, 1)},
            "db": store.db_info(),
            "bridge": bridge,
            "cron": {"last_tick_at": _last_cron_tick_at()},
            "version": "0.1.0",
        }

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

    @app.post("/api/v1/runs", status_code=202)
    def create_run(
        body: RunCreateRequest, request: Request, background_tasks: BackgroundTasks
    ) -> JSONResponse:
        if body.workflow not in WORKFLOWS:
            return err(404, "unknown_workflow", f"workflow '{body.workflow}' not found", request)
        run = store.create_run(
            body.workflow,
            WORKFLOWS[body.workflow].version,
            {"task": body.task, **({"meta": body.metadata} if body.metadata else {})},
            trace_id=uuid.uuid4().hex,
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
                "created_at": r.created_at.isoformat(),
            }
            for r in store.list_runs(limit, workflow=workflow)
        ]

    @app.get("/api/v1/runs/stats")
    def run_stats(workflow: str | None = None, days: int = 30) -> dict[str, Any]:
        """workflow별 run 성공/실패/토큰/단계 실패율 집계 (최대 365일)."""
        return store.run_stats(workflow, max(1, min(days, 365)))

    @app.get("/api/v1/runs/{run_id}")
    def get_run(run_id: str, request: Request) -> JSONResponse:
        d = run_detail(run_id)
        if d is None:
            return err(404, "not_found", "run not found", request)
        return JSONResponse(content=d)

    def _do_approve(run_id: str, decision: str, comment: str) -> dict[str, Any]:
        """승인/거절 공통 경로 — API 엔드포인트와 메신저 커맨드가 공유.
        반환: {"ok": True, "run": detail} 또는 {"ok": False, "status": int, "error": str}."""
        run = store.get_run(run_id)
        if run is None:
            return {"ok": False, "status": 404, "error": "run not found"}
        if run.status != RunStatus.WAITING_APPROVAL:
            return {
                "ok": False,
                "status": 409,
                "error": f"run status is {run.status.value}, not waiting_approval",
            }
        runner = build_runner(
            run.workflow_key, store=store, llm=llm, settings=settings, adapters=adapters
        )
        try:
            runner.resume(run_id, {"decision": decision, "comment": comment})
        except Exception as e:
            logger.warning("run %s resume failed: %s", run_id, str(e)[:200])
            return {"ok": False, "status": 500, "error": str(e)[:300]}
        store.mark_inbox_read_dedupe(f"approval:{run_id}")
        logger.info("run %s resume decision=%s", run_id, decision)
        return {"ok": True, "run": run_detail(run_id) or {}}

    @app.post("/api/v1/runs/{run_id}/approve")
    def approve(run_id: str, body: ApprovalRequest, request: Request) -> JSONResponse:
        result = _do_approve(run_id, body.decision, body.comment)
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
        """run의 audit_events — 최신순, 최대 200건."""
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

    # ---- workflow schedules (주기적 run 자동 생성) ----
    @app.get("/api/v1/schedules")
    def list_schedules() -> list[dict[str, Any]]:
        return store.list_schedules()

    @app.post("/api/v1/schedules", status_code=201)
    def create_schedule(body: ScheduleCreateRequest, request: Request) -> JSONResponse:
        if _admin_user(request) is None:
            return err(403, "forbidden", "admin role required", request)
        if body.workflow not in WORKFLOWS:
            return err(404, "unknown_workflow", f"workflow '{body.workflow}' not found", request)
        sched = store.create_schedule(body.workflow, body.input, body.interval_minutes)
        store.audit(
            "",
            "api",
            "schedule_create",
            sched["id"],
            {"workflow": body.workflow, "interval_minutes": body.interval_minutes},
        )
        logger.info(
            "schedule created id=%s workflow=%s interval=%dm",
            sched["id"],
            body.workflow,
            body.interval_minutes,
        )
        return JSONResponse(status_code=201, content=sched)

    @app.post("/api/v1/schedules/{schedule_id}/toggle")
    def toggle_schedule(
        schedule_id: str, body: ScheduleToggleRequest, request: Request
    ) -> JSONResponse:
        if _admin_user(request) is None:
            return err(403, "forbidden", "admin role required", request)
        sched = store.set_schedule_enabled(schedule_id, body.enabled)
        if sched is None:
            return err(404, "not_found", "schedule not found", request)
        store.audit("", "api", "schedule_toggle", schedule_id, {"enabled": body.enabled})
        logger.info("schedule %s toggled enabled=%s", schedule_id, body.enabled)
        return JSONResponse(content=sched)

    @app.delete("/api/v1/schedules/{schedule_id}")
    def delete_schedule(schedule_id: str, request: Request) -> JSONResponse:
        if _admin_user(request) is None:
            return err(403, "forbidden", "admin role required", request)
        if not store.delete_schedule(schedule_id):
            return err(404, "not_found", "schedule not found", request)
        store.audit("", "api", "schedule_delete", schedule_id, {})
        logger.info("schedule %s deleted", schedule_id)
        return JSONResponse(content={"ok": True})

    @app.get("/api/v1/notifications")
    def notifications(limit: int = 50) -> list[dict[str, Any]]:
        return [n.model_dump(mode="json") for n in store.list_notifications(limit)]

    # ---- 통합 인박스 (알림 센터) ----
    # notifications 테이블은 별도 유지 — GET /inbox 호출 시 dedupe_key=notif:{id}로
    # inbox_items에 지연 미러링한다(멱등). 나머지 kind는 이벤트 발생 시 직접 insert.
    @app.get("/api/v1/inbox")
    def inbox(limit: int = 50, unread_only: bool = False) -> list[dict[str, Any]]:
        store.sync_notifications_to_inbox()
        return store.list_inbox(limit=min(max(limit, 1), 200), unread_only=unread_only)

    @app.post("/api/v1/inbox/{item_id}/read")
    def inbox_read(item_id: str, request: Request) -> JSONResponse:
        if not store.mark_inbox_read(item_id):
            return err(404, "not_found", "inbox item not found", request)
        return JSONResponse(content={"ok": True})

    @app.post("/api/v1/inbox/read-all")
    def inbox_read_all() -> dict[str, Any]:
        return {"ok": True, "updated": store.mark_all_inbox_read()}

    @app.get("/api/v1/inbox/unread-count")
    def inbox_unread_count() -> dict[str, int]:
        return {"count": store.inbox_unread_count()}

    # ---- 비용 대시보드 ----
    @app.get("/api/v1/usage/summary")
    def usage_summary(days: int = 30) -> dict[str, Any]:
        """llm_usage 기반 비용/토큰 요약 + 에이전트 세션 추정 비용 top + 월 예산."""
        days = max(1, min(days, 365))
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        rows = store.llm_usage_rows(cutoff)

        by_model: dict[str, dict[str, Any]] = {}
        by_day: dict[str, dict[str, Any]] = {}
        total_cost = 0.0
        total_in_i = total_out_i = 0
        for r in rows:
            model = str(r.get("model") or "")
            tin = int(r.get("tokens_in") or 0)
            tout = int(r.get("tokens_out") or 0)
            cost = cost_usd(model, tin, tout)
            total_cost += cost
            total_in_i += tin
            total_out_i += tout
            m = by_model.setdefault(model, {"model": model, "cost_usd": 0.0, "tokens": 0})
            m["cost_usd"] += cost
            m["tokens"] += tin + tout
            day = (str(r.get("created_at") or ""))[:10]
            if day:
                d = by_day.setdefault(day, {"date": day, "cost_usd": 0.0, "tokens": 0})
                d["cost_usd"] += cost
                d["tokens"] += tin + tout

        # 에이전트 세션 추정 비용 — 상위 10개 (토큰 없으면 스킵)
        top_sessions: list[dict[str, Any]] = []
        for s in store.list_agent_sessions(limit=200, include_ended=True):
            est = _session_est_cost_usd(s)
            if est is None:
                continue
            top_sessions.append(
                {
                    "tool": s.get("tool") or "",
                    "session_key": s.get("session_key") or "",
                    "cost_usd": est,
                }
            )
        top_sessions.sort(key=lambda x: x["cost_usd"], reverse=True)

        prefs = _prefs()
        budget_raw = prefs.get("monthly_budget_usd")
        try:
            budget = float(budget_raw) if budget_raw is not None else None
        except (TypeError, ValueError):
            budget = None
        # 월초(UTC) 이후 llm_usage 비용 = month-to-date
        month_start = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        mtd_cost = 0.0
        for r in store.llm_usage_rows(month_start.isoformat()):
            mtd_cost += cost_usd(
                str(r.get("model") or ""),
                int(r.get("tokens_in") or 0),
                int(r.get("tokens_out") or 0),
            )
        budget_exceeded = budget is not None and mtd_cost > budget

        return {
            "total_cost_usd": round(total_cost, 6),
            "total_tokens_in": total_in_i,
            "total_tokens_out": total_out_i,
            "by_model": [
                {"model": m["model"], "cost_usd": round(m["cost_usd"], 6), "tokens": m["tokens"]}
                for m in sorted(by_model.values(), key=lambda x: -x["cost_usd"])
            ],
            "by_day": [
                {"date": d["date"], "cost_usd": round(d["cost_usd"], 6), "tokens": d["tokens"]}
                for d in sorted(by_day.values(), key=lambda x: x["date"])
            ],
            "top_sessions": top_sessions[:10],
            "budget_usd": budget,
            "budget_exceeded": budget_exceeded,
        }

    # ---- 자연어 쿼리 (ask) ----
    _ask_daily: dict[str, tuple[str, int]] = {}
    _ASK_DAILY_LIMIT = 20
    _ASK_TIMEOUT_S = 30.0

    def _ask_key(request: Request) -> str:
        user = _session_user(request)
        if user and user.get("id"):
            return f"u:{user['id']}"
        return f"ip:{request.client.host if request.client else '-'}"

    @app.post("/api/v1/ask")
    def ask(body: AskRequest, request: Request) -> JSONResponse:
        """자연어 질문 → LLM 답변(한국어). LLM 실패/타임아웃 시 503 + error 필드.
        사용자/IP당 하루 20회 인메모리 레이트리밋."""
        from concurrent.futures import TimeoutError as FutureTimeout

        question = body.question.strip()
        if not question:
            return err(422, "validation_error", "question must not be blank", request)
        key = _ask_key(request)
        today = datetime.now(UTC).date().isoformat()
        day, count = _ask_daily.get(key, ("", 0))
        if day != today:
            day, count = today, 0
        if count >= _ASK_DAILY_LIMIT:
            return err(429, "rate_limited", "daily ask limit reached", request)
        _ask_daily[key] = (day, count + 1)
        if len(_ask_daily) > 1000:
            _ask_daily.clear()

        # 컴팩트 컨텍스트 — 비밀값/프롬프트 원문 없이 집계만
        try:
            stats = store.run_stats(None, 7)
            sessions = store.agent_session_stats(
                since=(datetime.now(UTC) - timedelta(days=7)).isoformat()
            )
            usage = store.llm_usage_summary(days=7)
            context = {
                "runs_7d": {
                    k: stats.get(k)
                    for k in ("runs", "succeeded", "failed", "running", "waiting_approval")
                },
                "tokens_7d": {
                    "in": stats.get("total_tokens_in"),
                    "out": stats.get("total_tokens_out"),
                },
                "stage_failures": stats.get("stage_failures"),
                "sessions": sessions,
                "llm_models": usage.get("models"),
            }
        except Exception as e:
            logger.warning("ask context build failed: %s", str(e)[:200])
            context = {}
        ctx_text = json.dumps(context, ensure_ascii=False, default=str)[:6000]
        system = (
            "너는 jobMate 대시보드의 운영 어시스턴트다. 제공된 집계 컨텍스트만 근거로 "
            "한국어로 간결하게 답한다(3~6문장). 모르는 내용은 추측하지 말고 "
            "데이터가 없다고 답한다. 비밀번호·토큰 등 비밀값은 절대 언급하지 않는다."
        )
        model = settings.model_for("_ask", "default")
        pool = ThreadPoolExecutor(max_workers=1)
        try:
            fut = pool.submit(
                llm.complete,
                model=model,
                system=system,
                user=f"컨텍스트:\n{ctx_text}\n\n질문: {question}",
            )
            out, _usage = fut.result(timeout=_ASK_TIMEOUT_S)
        except FutureTimeout:
            logger.warning("ask llm timeout")
            return JSONResponse(status_code=503, content={"answer": "", "error": "llm_unavailable"})
        except Exception as e:
            logger.warning("ask llm failed: %s", str(e)[:200])
            return JSONResponse(status_code=503, content={"answer": "", "error": "llm_unavailable"})
        finally:
            pool.shutdown(wait=False)
        answer = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
        return JSONResponse(content={"answer": answer})

    # ---- 리포트 ----
    from ..reports import build_report

    @app.get("/api/v1/reports")
    def get_report(kind: str = "daily") -> JSONResponse:
        """온디맨드 리포트 — 저장 없이 즉시 생성."""
        if kind not in ("daily", "weekly"):
            return JSONResponse(
                status_code=422,
                content={"error": {"code": "validation", "message": "kind must be daily|weekly"}},
            )
        try:
            content = build_report(store, kind, settings)
        except Exception as e:
            logger.warning("report build failed kind=%s: %s", kind, str(e)[:200])
            return JSONResponse(
                status_code=500,
                content={"error": {"code": "report_failed", "message": str(e)[:200]}},
            )
        return JSONResponse(content=content)

    @app.get("/api/v1/reports/latest")
    def latest_report(request: Request, kind: str = "daily") -> JSONResponse:
        """저장된 최신 리포트. 없으면 온디맨드 생성 결과를 반환(404 없음)."""
        if kind not in ("daily", "weekly"):
            return err(422, "validation", "kind must be daily|weekly", request)
        rep = store.latest_report(kind)
        if rep is None:
            try:
                rep = {
                    "id": "",
                    "kind": kind,
                    "content": build_report(store, kind, settings),
                    "created_at": datetime.now(UTC).isoformat(),
                }
            except Exception as e:
                logger.warning("report build failed kind=%s: %s", kind, str(e)[:200])
                return err(500, "report_failed", str(e)[:200], request)
        return JSONResponse(content=rep)

    @app.post("/api/v1/reports/generate", status_code=201)
    def generate_report(body: ReportGenerateRequest, request: Request) -> JSONResponse:
        """리포트 생성·저장 + 인박스 알림 + 메신저 알림 (관리자 전용)."""
        if _admin_user(request) is None:
            return err(403, "forbidden", "admin role required", request)
        try:
            content = build_report(store, body.kind, settings)
        except Exception as e:
            logger.warning("report build failed kind=%s: %s", body.kind, str(e)[:200])
            return err(500, "report_failed", str(e)[:200], request)
        rid = store.save_report(body.kind, content)
        store.upsert_inbox_item(
            "report",
            f"{body.kind} 리포트 생성됨",
            body=f"runs={content['runs']['total']} failed={content['runs']['failed']}",
            link="/?view=reports",
            dedupe_key=f"report:{rid}",
        )
        store.audit("", "api", "report_generate", rid, {"kind": body.kind})
        logger.info("report generated id=%s kind=%s", rid, body.kind)
        try:
            messenger.notify(
                f"jobMate {body.kind} 리포트: run {content['runs']['total']}건 "
                f"(실패 {content['runs']['failed']}), 승인 대기 {content['approvals']['pending']}"
            )
        except Exception:
            logger.debug("messenger notify after report failed", exc_info=True)
        return JSONResponse(
            status_code=201,
            content={"id": rid, "kind": body.kind, "content": content},
        )

    # ---- PWA 뱃지 ----
    @app.get("/api/v1/badge/count")
    def badge_count() -> dict[str, int]:
        """앱 뱃지 = 미읽음 인박스 + 승인 대기 run."""
        waiting = sum(
            1 for r in store.list_runs(limit=100) if r.status == RunStatus.WAITING_APPROVAL
        )
        return {"count": store.inbox_unread_count() + waiting}

    # ---- 멀티유저 역할 ----
    @app.get("/api/v1/users")
    def list_users(request: Request) -> JSONResponse:
        """사용자 목록 (관리자 전용) — 비밀번호 해시 제외."""
        admin = _admin_user(request)
        if admin is None:
            return err(403, "forbidden", "admin role required", request)
        users = []
        for u in store.list_users():
            users.append(
                {
                    "id": u["id"],
                    "email": u["email"],
                    "display_name": u.get("display_name", ""),
                    "role": _effective_role(u),
                    "created_at": u.get("created_at"),
                }
            )
        return JSONResponse(content={"users": users})

    @app.get("/api/v1/event-links")
    def event_links(limit: int = 50) -> list[dict[str, Any]]:
        return [x.model_dump(mode="json") for x in store.list_links(limit)]

    # ---- agent-sessions: 로컬 에이전트 브릿지 push/pull ----
    _attention_ts: dict[str, float] = {}

    def _push_attention(key: str, title: str, body: str) -> None:
        """어텐션 웹 푸시 — 키(세션/런)당 10분 레이트리밋."""
        now = time.time()
        if now - _attention_ts.get(key, 0.0) < 600:
            return
        if len(_attention_ts) > 1000:
            _attention_ts.clear()
        _attention_ts[key] = now
        from ..engine.functions import send_web_push_text

        send_web_push_text(adapters, title, body, "/?view=office")
        logger.info("attention push sent key=%s", key)

    def _session_push_allowed(tool: str, project: str | None, status: str, ask: str | None) -> bool:
        """prefs.notify_rules 기반 세션 어텐션 푸시 필터.

        규칙 없음/빈 리스트 → 전부 허용(기존 동작). 규칙이 있으면
        tool/project null-or-일치 AND statuses에 유효 상태(ask 우선, 없으면
        status)가 포함된 규칙이 하나라도 있을 때만 허용."""
        rules = _prefs().get("notify_rules")
        if not isinstance(rules, list) or not rules:
            return True
        effective = "ask" if (ask or "").strip() else status
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            rtool = rule.get("tool")
            if rtool and rtool != tool:
                continue
            rproject = rule.get("project")
            if rproject and rproject != project:
                continue
            statuses = rule.get("statuses")
            if not isinstance(statuses, list):
                continue
            if status in statuses or effective in statuses:
                return True
        return False

    @app.post("/api/v1/agent-sessions")
    def push_agent_session(body: AgentSessionRequest, request: Request) -> JSONResponse:
        """머신 push 전용 — API 키 인증만 허용. 세션 쿠키만으로는 거부."""
        from ..security import authorized

        if not authorized(request):
            logger.warning("agent-sessions push rejected: session-only auth")
            return err(403, "forbidden", "machine API key required", request)
        tool = body.tool.strip()
        session_key = body.session_key.strip()
        if not tool or not session_key:
            return err(422, "validation_error", "tool/session_key must not be blank", request)
        prev = store.get_agent_session(tool, session_key)
        store.upsert_agent_session(
            tool,
            session_key,
            body.status,
            project=body.project,
            title=body.title,
            doing=body.doing,
            ask=body.ask,
            event_at=body.event_at,
            ended_summary=body.ended_summary,
            artifacts=body.artifacts,
            machine=body.machine,
            tokens_in=body.tokens_in,
            tokens_out=body.tokens_out,
        )
        # 활동 녹화 — status/doing 변경 또는 60초 경과 시에만 insert(스토어 스로틀)
        try:
            store.record_session_activity(
                tool, session_key, body.status, doing=body.doing, project=body.project
            )
        except Exception as e:
            logger.warning("session activity record failed: %s", str(e)[:200])
        # decision 진입(또는 ask 설정) '전이' 시에만 푸시 — 반복 poll은 무시
        attention = body.status == "decision" or bool((body.ask or "").strip())
        prev_attention = prev is not None and (
            prev.get("status") == "decision" or bool((prev.get("ask") or "").strip())
        )
        if attention and not prev_attention:
            # 통합 인박스 — 세션당 1건 dedupe
            try:
                store.upsert_inbox_item(
                    "session_attention",
                    f"승인 필요: {tool} · {body.project or session_key[:12]}",
                    body=((body.ask or body.doing or body.title or "").strip())[:300],
                    link="/?view=office",
                    dedupe_key=f"session:{tool}:{session_key}",
                )
            except Exception as e:
                logger.warning("inbox session_attention failed: %s", str(e)[:200])
            if _session_push_allowed(tool, body.project, body.status, body.ask):
                who = body.project or session_key[:12]
                detail = (body.ask or body.doing or body.title or "").strip()[:200]
                _push_attention(
                    f"agent:{tool}:{session_key}",
                    f"승인 필요: {tool} · {who}",
                    detail,
                )
            else:
                logger.debug("agent-session attention skipped by notify_rules tool=%s", tool)
        return JSONResponse(content={"ok": True})

    @app.get("/api/v1/agent-sessions")
    def list_agent_sessions(
        limit: int = 50,
        include_ended: bool = False,
        machine: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = store.list_agent_sessions(limit=limit, include_ended=include_ended, machine=machine)
        return [_session_view(r) for r in rows]

    @app.get("/api/v1/agent-sessions/stats")
    def agent_session_stats(request: Request, since: str | None = None) -> Any:
        """tool별 세션 통계 + since(YYYY-MM-DD, 기본 오늘)의 시간대별 working 초."""
        day = since or datetime.now().astimezone().date().isoformat()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            return err(422, "validation_error", "since must be YYYY-MM-DD", request)
        try:
            hourly = store.agent_session_hourly(day)
        except ValueError:
            return err(422, "validation_error", "invalid since date", request)
        return {"tools": store.agent_session_stats(since=since), "hourly": hourly}

    @app.get("/api/v1/agent-sessions/{tool}/{session_key}/history")
    def agent_session_history(tool: str, session_key: str, limit: int = 100) -> dict[str, Any]:
        """세션 상태 전이 이력 (시간순)."""
        return {
            "events": store.agent_session_history(tool, session_key, limit=min(max(limit, 1), 500))
        }

    @app.get("/api/v1/agent-sessions/{tool}/{session_key}/activity")
    def agent_session_activity(tool: str, session_key: str, limit: int = 200) -> dict[str, Any]:
        """세션 활동 스냅샷 (시간순, 최대 200) — doing/status/프로젝트."""
        return {
            "activity": store.session_activity(tool, session_key, limit=min(max(limit, 1), 500))
        }

    @app.get("/api/v1/usage/llm")
    def llm_usage(days: int = 7) -> dict[str, Any]:
        """LLM 토큰 사용량 요약 — 모델별 합계 + 일별 시계열."""
        days = max(1, min(days, 90))
        return {"days": days, **store.llm_usage_summary(days=days)}

    @app.get("/api/v1/gmail-calendar")
    def gmail_status() -> dict[str, Any]:
        auto = store.get_automation()
        return {
            "enabled": auto.get("enabled", False),
            "last_poll_at": auto.get("last_poll_at"),
            "last_poll_result": auto.get("last_poll_result", ""),
        }

    @app.post("/api/v1/gmail-calendar/toggle")
    def gmail_toggle(body: dict[str, Any]) -> dict[str, Any]:
        auto = store.get_automation()
        auto["enabled"] = bool(body.get("enabled", not auto.get("enabled", False)))
        store.set_automation(auto)
        return {"enabled": auto["enabled"]}

    @app.post("/api/v1/gmail-calendar/poll")
    def gmail_poll(request: Request) -> JSONResponse:
        gmail = adapters.get("gmail")
        if gmail is None:
            return JSONResponse(
                status_code=202,
                content={"status": "accepted", "note": "poll requires configured gmail adapter"},
            )
        ingested = _poll_gmail(store, settings, gmail)
        return JSONResponse(status_code=202, content={"status": "accepted", "ingested": ingested})

    # ---- 메신저 커맨드 (Slack/Telegram 양방향) ----
    def _status_summary_text() -> str:
        """현황 커맨드용 컴팩트 요약 (한국어)."""
        try:
            stats = store.run_stats(None, 7)
            sess = store.agent_session_stats()
        except Exception as e:
            logger.warning("status summary failed: %s", str(e)[:200])
            return "jobMate 현황을 가져오지 못했습니다."
        lines = [
            f"최근 7일 run: 총 {stats.get('runs', 0)}건 "
            f"(성공 {stats.get('succeeded', 0)}, 실패 {stats.get('failed', 0)}, "
            f"승인 대기 {stats.get('waiting_approval', 0)})"
        ]
        if sess:
            top = sorted(sess.items(), key=lambda kv: -int(kv[1].get("working_seconds") or 0))[:3]
            lines.append(
                "세션: "
                + ", ".join(
                    f"{t}(세션 {v.get('sessions', 0)}, "
                    f"작업 {int(v.get('working_seconds') or 0) // 60}분)"
                    for t, v in top
                )
            )
        return "\n".join(lines)

    def _messenger_reply(provider: str, chat_id: str, text: str) -> None:
        """수신 채널로 답장 — best-effort, 실패는 debug 로그만."""
        try:
            if provider == "slack":
                url = os.environ.get("JOBMATE_SLACK_WEBHOOK_URL", "")
                if url:
                    messenger.send_slack(url, text)
            elif provider == "telegram":
                token = os.environ.get("JOBMATE_TELEGRAM_BOT_TOKEN", "")
                if token:
                    messenger.send_telegram(token, chat_id, text)
        except Exception:
            logger.debug("messenger reply failed provider=%s", provider, exc_info=True)

    def _messenger_handle(
        provider: str,
        chat_id: str,
        text: str,
        background_tasks: BackgroundTasks,
    ) -> bool:
        """수신 텍스트를 커맨드로 파싱·실행 후 답장. 인식 못 하면 False."""
        cmd = messenger.parse_command(text)
        if cmd is None:
            return False
        action = str(cmd.get("action") or "")
        if action == "help":
            _messenger_reply(provider, chat_id, messenger.HELP_TEXT)
        elif action == "status":
            _messenger_reply(provider, chat_id, _status_summary_text())
        elif action == "approve":
            target = str(cmd.get("run_id") or "")
            if not target:
                for r in store.list_runs(limit=100):
                    if r.status == RunStatus.WAITING_APPROVAL:
                        target = r.id
                        break
            if not target:
                _messenger_reply(provider, chat_id, "승인 대기 중인 run이 없습니다.")
                return True
            res = _do_approve(target, "approve", "messenger")
            if res["ok"]:
                _messenger_reply(provider, chat_id, f"run {target} 승인을 재개했습니다.")
            else:
                _messenger_reply(
                    provider, chat_id, f"승인 실패({res['status']}): {str(res['error'])[:200]}"
                )
        elif action == "start":
            wf = str(cmd.get("workflow") or "")
            task = str(cmd.get("task") or "")
            if wf not in WORKFLOWS:
                _messenger_reply(
                    provider,
                    chat_id,
                    f"알 수 없는 workflow: {wf}\n사용 가능: {', '.join(sorted(WORKFLOWS))}",
                )
                return True
            jobs: list[tuple[str, str, dict[str, Any], dict[str, Any] | None]] = []
            rid = submit_run(wf, task, defer=jobs)
            _schedule_run_jobs(jobs, background_tasks)
            _messenger_reply(provider, chat_id, f"run 생성: {rid} ({wf})")
            logger.info("messenger start run=%s workflow=%s provider=%s", rid, wf, provider)
        return True

    # ---- calendar-agent: 멀티채널 웹훅·수신함·이벤트·알림예약 ----
    @app.post("/api/v1/webhooks/slack")
    async def slack_webhook(request: Request, background_tasks: BackgroundTasks) -> JSONResponse:
        body = await request.body()
        if not verify_slack_signature(
            body,
            request.headers.get("X-Slack-Request-Timestamp", ""),
            request.headers.get("X-Slack-Signature", ""),
            settings.slack_signing_secret,
        ):
            logger.warning("slack webhook rejected: bad signature")
            return err(401, "invalid_signature", "bad slack signature", request)
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return err(400, "bad_json", "invalid JSON", request)
        if payload.get("type") == "url_verification":
            return JSONResponse(content={"challenge": payload.get("challenge", "")})
        event = payload.get("event") or {}
        if (
            payload.get("type") != "event_callback"
            or event.get("type") != "message"
            or event.get("bot_id")
            or event.get("subtype") not in (None, "file_share")
            or not (event.get("text") or "").strip()
        ):
            return JSONResponse(content={"status": "ignored"})
        channel = event.get("channel", "")
        if not allowed_identifier(channel, settings.slack_channel_ids):
            return JSONResponse(content={"status": "ignored"})
        team = payload.get("team_id", "")
        ts = event.get("ts", "")
        source_id = f"{team}:{channel}:{ts}"
        text = event.get("text", "")
        # 메신저 커맨드 — 인식되면 답장만 하고 캘린더 수신함 처리로는 넘기지 않는다
        if _messenger_handle("slack", channel, text, background_tasks):
            return JSONResponse(content={"status": "command"})
        created = store.save_message_extraction(
            "slack", source_id, event.get("thread_ts") or ts, {}, "pending", source_text=text[:8000]
        )
        return JSONResponse(
            content={"status": "processed" if created else "duplicate", "source_id": source_id}
        )

    @app.post("/api/v1/webhooks/telegram")
    async def telegram_webhook(request: Request, background_tasks: BackgroundTasks) -> JSONResponse:
        if not verify_telegram_secret(
            request.headers.get("X-Telegram-Bot-Api-Secret-Token", ""),
            settings.telegram_webhook_secret,
        ):
            logger.warning("telegram webhook rejected: bad secret")
            return err(401, "invalid_secret", "bad telegram secret", request)
        try:
            update = json.loads(await request.body())
        except json.JSONDecodeError:
            return err(400, "bad_json", "invalid JSON", request)
        msg = update.get("message") or update.get("channel_post") or {}
        sender = msg.get("from") or {}
        if sender.get("is_bot"):
            return JSONResponse(content={"status": "ignored"})
        text = msg.get("text") or msg.get("caption") or ""
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        if not text.strip() or not allowed_identifier(chat_id, settings.telegram_chat_ids):
            return JSONResponse(content={"status": "ignored"})
        source_id = f"{chat_id}:{msg.get('message_id', '')}"
        # 메신저 커맨드 — 인식되면 답장만 하고 캘린더 수신함 처리로는 넘기지 않는다
        if _messenger_handle("telegram", chat_id, text, background_tasks):
            return JSONResponse(content={"status": "command"})
        created = store.save_message_extraction(
            "telegram",
            source_id,
            str(msg.get("message_thread_id") or msg.get("message_id", "")),
            {},
            "pending",
            source_text=text[:8000],
        )
        return JSONResponse(
            content={"status": "processed" if created else "duplicate", "source_id": source_id}
        )

    @app.get("/api/v1/messages")
    def list_messages(provider: str | None = None, limit: int = 20) -> dict[str, Any]:
        return {"messages": store.list_message_extractions(provider, limit)}

    @app.post("/api/v1/gmail/digest")
    def gmail_digest(body: dict[str, Any] | None = None) -> JSONResponse:
        """최근 Gmail 메시지들을 LLM으로 요약. 실패 시 빈 summary + error."""
        limit = int((body or {}).get("limit", 20))
        msgs = [
            m
            for m in store.list_message_extractions("gmail", limit)
            if (m.get("source_text") or "").strip()
        ]
        if not msgs:
            return JSONResponse(content={"summary": "", "count": 0})
        lines = []
        for m in msgs[:15]:
            text = (m.get("source_text") or "").replace("\n", " ")[:300]
            lines.append(f"- [{m.get('processing_status')}] {text}")
        system = (
            "너는 이메일 비서다. 다음 Gmail 메시지 목록을 한국어로 "
            "3~5개 bullet로 요약해. 각 bullet은 '- '로 시작하고, "
            "일정/행동 필요한 내용 중심으로 작성해."
        )
        try:
            model = settings.model_for("_digest", "default")
            out, _usage = llm.complete(model=model, system=system, user="\n".join(lines))
            summary = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
        except Exception as e:
            logger.warning("gmail digest failed: %s", str(e)[:200])
            return JSONResponse(content={"summary": "", "count": len(msgs), "error": str(e)[:200]})
        return JSONResponse(content={"summary": summary, "count": len(msgs)})

    @app.api_route("/api/v1/cron/tick", methods=["GET", "POST"])
    def cron_tick(request: Request, background_tasks: BackgroundTasks) -> JSONResponse:
        """서버리스 환경용 1회 자동화 사이클 (Vercel Cron 등에서 호출)."""
        secret = os.environ.get("CRON_SECRET") or os.environ.get("JOBMATE_CRON_SECRET", "")
        from ..security import api_key, authorized

        # Vercel Cron requires its dedicated secret; other hosts also accept a machine key.
        if os.environ.get("VERCEL"):
            valid = bool(secret) and request.headers.get("authorization") == f"Bearer {secret}"
        else:
            valid = (
                (bool(secret) and request.headers.get("authorization") == f"Bearer {secret}")
                or (not secret and authorized(request))
                or (bool(api_key()) and authorized(request))
            )
        if not valid:
            logger.warning("cron tick rejected: invalid credentials")
            return err(401, "unauthorized", "invalid cron credentials", request)
        _mark_cron_tick()
        _process_pending_messages(store, settings, llm, adapters)
        _process_due_notification_jobs(store, adapters)
        schedule_jobs: list = []
        _process_due_schedules(store, settings, llm, adapters, defer=schedule_jobs)
        _schedule_run_jobs(schedule_jobs, background_tasks)
        _check_waiting_approvals(store, adapters)
        _maybe_poll_gmail(store, settings, adapters)
        radar_jobs: list = []
        _maybe_daily_radar(store, settings, llm, adapters, defer=radar_jobs)
        _schedule_run_jobs(radar_jobs, background_tasks)
        _maybe_generate_reports(store, settings, adapters)
        logger.info("cron tick cycle completed")
        return JSONResponse(content={"ok": True})

    def _cron_authorized(request: Request) -> bool:
        secret = os.environ.get("CRON_SECRET") or os.environ.get("JOBMATE_CRON_SECRET", "")
        if not secret:
            return True
        if (
            request.headers.get("authorization") == f"Bearer {secret}"
            or request.query_params.get("secret") == secret
        ):
            return True
        # cron-job.org 등 API 키 인증 경로 — 미들웨어에서 이미 검증된 키만 도달
        from ..security import authorized

        return bool(os.environ.get("JOBMATE_API_KEY")) and authorized(request)

    def _summarize_email(msg: dict[str, Any]) -> str:
        body = (msg.get("body_text") or "").strip()
        fallback = "\n".join(ln for ln in body.splitlines() if ln.strip())[:300]
        try:
            model = settings.model_for("_digest", "default")
            out, _u = llm.complete(
                model=model,
                system=(
                    "너는 이메일 비서다. 주어진 이메일을 정확히 3줄로 "
                    "요약해. 각 줄은 '- '로 시작하지 말고 간결한 문장으로."
                ),
                user=(
                    f"제목: {msg.get('subject', '')}\n"
                    f"보낸사람: {msg.get('from_addr', '')}\n"
                    f"본문:\n{body[:3000] or '(본문 없음)'}"
                ),
            )
            if isinstance(out, str):
                lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
                if lines:
                    return "\n".join(lines[:3])
        except Exception as e:
            logger.warning("email summarize failed: %s", e)
        return fallback

    @app.post("/api/v1/email/check")
    def email_check(request: Request, body: dict[str, Any] | None = None) -> JSONResponse:
        """Gmail 최근 메일을 읽어 3줄 요약 카드를 생성·저장.
        cron-job.org 등 외부 스케줄러가 30분마다 POST로 호출."""
        if not _cron_authorized(request):
            logger.warning("email check rejected: invalid cron secret")
            return err(401, "unauthorized", "invalid cron secret", request)
        gmail = adapters.get("gmail")
        if gmail is None:
            return err(503, "no_gmail_adapter", "gmail adapter not configured", request)
        payload = body or {}
        limit = min(int(payload.get("limit", 5)), 20)
        query = str(payload.get("query", "in:inbox"))
        cards: list[dict[str, Any]] = []
        errors: list[str] = []
        try:
            listing = gmail.list_messages(query=query, max_results=limit)
        except Exception as e:
            logger.warning("email check gmail list failed: %s", str(e)[:200])
            return JSONResponse(
                status_code=502, content={"ok": False, "error": str(e)[:300], "cards": []}
            )
        for m in listing:
            mid = m["id"]
            try:
                msg = gmail.get_message(mid)
            except Exception as e:
                errors.append(f"{mid}: {str(e)[:120]}")
                logger.warning("email check get_message failed id=%s: %s", mid, str(e)[:120])
                continue
            summary = _summarize_email(msg)
            ts = msg.get("internal_date", "")
            message_at = ""
            if ts.isdigit():
                message_at = datetime.fromtimestamp(int(ts) / 1000, UTC).isoformat()
            store.save_email_summary(
                "gmail",
                mid,
                subject=msg.get("subject", ""),
                sender=msg.get("from_addr", ""),
                summary=summary,
                message_at=message_at,
            )
            cards.append(
                {
                    "source_id": mid,
                    "subject": msg.get("subject", ""),
                    "sender": msg.get("from_addr", ""),
                    "summary": summary,
                    "message_at": message_at,
                    "url": f"https://mail.google.com/mail/u/0/#all/{mid}",
                }
            )
        if errors:
            logger.warning("email check partial failures: %d/%d", len(errors), len(listing))
        logger.info("email check done cards=%d errors=%d", len(cards), len(errors))
        return JSONResponse(
            content={"ok": True, "checked": len(cards), "cards": cards, "errors": errors}
        )

    @app.get("/api/v1/email/summaries")
    def email_summaries(limit: int = 30) -> dict[str, Any]:
        cards = store.list_email_summaries("gmail", min(limit, 100))
        for c in cards:
            c["url"] = f"https://mail.google.com/mail/u/0/#all/{c['source_id']}"
        return {"cards": cards}

    @app.post("/api/v1/email/purge-undecryptable")
    def purge_undecryptable(request: Request) -> JSONResponse:
        """복호화 불가한 email_summaries 행 삭제 (키 로테이션 복구용)."""
        removed = store.purge_undecryptable_email_summaries("gmail")
        logger.info("purged %d undecryptable email_summaries rows", removed)
        store.audit("", "api", "email_purge_undecryptable", "", {"removed": removed})
        return JSONResponse(content={"ok": True, "removed": removed})

    # ---------- settings (대시보드 설정) ----------
    PREFS_KEY = "prefs"

    def _prefs() -> dict[str, Any]:
        return store.get_state(PREFS_KEY, {})

    @app.get("/api/v1/settings")
    def get_prefs() -> dict[str, Any]:
        p = _prefs()
        return {
            "gmail_check_minutes": p.get("gmail_check_minutes", 30),
            "radar_interval": p.get("radar_interval", "daily"),
            "notify_rules": p.get("notify_rules", []),
            "monthly_budget_usd": p.get("monthly_budget_usd"),
            "dashboard_widgets": p.get("dashboard_widgets", []),
        }

    @app.post("/api/v1/settings")
    def set_prefs(body: dict[str, Any], request: Request) -> JSONResponse:
        if _admin_user(request) is None:
            return err(403, "forbidden", "admin role required", request)
        p = _prefs()
        minutes = int(body.get("gmail_check_minutes", p.get("gmail_check_minutes", 30)))
        if minutes not in (30, 60):
            return err(422, "validation", "gmail_check_minutes must be 30 or 60", request)
        interval = str(body.get("radar_interval", p.get("radar_interval", "daily")))
        if interval not in ("daily", "weekly"):
            return err(422, "validation", "radar_interval must be daily or weekly", request)
        p.update(
            {
                "gmail_check_minutes": minutes,
                "radar_interval": interval,
            }
        )
        if "notify_rules" in body:
            rules = body.get("notify_rules")
            if not isinstance(rules, list) or len(rules) > 100:
                return err(422, "validation", "notify_rules must be a list (max 100)", request)
            cleaned: list[dict[str, Any]] = []
            for rule in rules:
                if not isinstance(rule, dict):
                    return err(422, "validation", "notify_rules items must be objects", request)
                statuses = rule.get("statuses")
                if not isinstance(statuses, list) or not all(isinstance(s, str) for s in statuses):
                    return err(
                        422, "validation", "notify_rules[].statuses must be a string list", request
                    )
                tool = rule.get("tool")
                project = rule.get("project")
                cleaned.append(
                    {
                        "tool": str(tool)[:64] if tool else None,
                        "project": str(project)[:512] if project else None,
                        "statuses": [str(s)[:32] for s in statuses][:16],
                    }
                )
            p["notify_rules"] = cleaned
        if "monthly_budget_usd" in body:
            raw_budget = body.get("monthly_budget_usd")
            if raw_budget is None:
                p["monthly_budget_usd"] = None
            else:
                try:
                    budget_val = float(raw_budget)
                except (TypeError, ValueError):
                    return err(422, "validation", "monthly_budget_usd must be a number", request)
                if budget_val < 0:
                    return err(422, "validation", "monthly_budget_usd must be >= 0", request)
                p["monthly_budget_usd"] = budget_val
        if "dashboard_widgets" in body:
            widgets = body.get("dashboard_widgets")
            if not isinstance(widgets, list) or len(widgets) > 50:
                return err(422, "validation", "dashboard_widgets must be a list (max 50)", request)
            cleaned_widgets: list[dict[str, Any]] = []
            for w in widgets:
                if not isinstance(w, dict):
                    return err(
                        422,
                        "validation",
                        "dashboard_widgets items must be objects",
                        request,
                    )
                cleaned_widgets.append(
                    {
                        "id": str(w.get("id", ""))[:64],
                        "visible": bool(w.get("visible", True)),
                        "order": int(w.get("order", 0) or 0),
                    }
                )
            p["dashboard_widgets"] = cleaned_widgets
        store.set_state(PREFS_KEY, p)
        cronjob = _sync_cronjob(settings, store, p)
        return JSONResponse(content={"ok": True, "prefs": p, "cronjob": cronjob})

    @app.post("/api/v1/messages/{provider}/{source_id}/retry")
    def retry_message(provider: str, source_id: str, request: Request) -> JSONResponse:
        if provider not in ("gmail", "slack", "telegram"):
            return err(400, "bad_provider", "unknown provider", request)
        if not store.has_message(provider, source_id):
            return err(404, "not_found", "message not found", request)
        store.finish_message(provider, source_id, "pending")
        return JSONResponse(content={"status": "pending"})

    @app.get("/api/v1/listeners/status")
    def listeners_status() -> dict[str, Any]:
        auto = store.get_automation()
        return {
            "auto_register": settings.auto_register,
            "gmail_enabled": bool(adapters.get("gmail")),
            "gmail_last_error": auto.get("last_gmail_error", ""),
            "slack_configured": bool(settings.slack_signing_secret),
            "telegram_configured": bool(settings.telegram_webhook_secret),
        }

    @app.get("/api/v1/events")
    def list_events(status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        return store.list_scheduled_events(status, limit)

    @app.get("/api/v1/notification-jobs")
    def notification_jobs(limit: int = 50) -> list[dict[str, Any]]:
        return store.list_notification_jobs(limit)

    @app.post("/api/v1/events/{event_id}/review")
    def review_event(event_id: str, body: EventReviewRequest, request: Request) -> JSONResponse:
        """needs_review/needs_confirmation/candidate 일정의 사람 검토.

        approve: 필드 수정분을 반영해 재검증 → Calendar 등록까지 수행.
        reject: 상태만 rejected로 전이.
        """
        from ..models import ExtractedEvent
        from ..sensors.event_validation import validate_event

        ev_row = store.get_scheduled_event(event_id)
        if ev_row is None:
            return err(404, "not_found", "scheduled event not found", request)
        if ev_row["status"] not in ("needs_review", "needs_confirmation", "candidate"):
            return err(
                409,
                "invalid_state",
                f"event status is {ev_row['status']}, not reviewable",
                request,
            )
        if body.action == "reject":
            store.update_scheduled_event(event_id, status="rejected", reason="rejected_by_user")
            store.audit("", "api", "event_reject", event_id)
            logger.info("scheduled event %s rejected via api", event_id)
            return JSONResponse(content={"ok": True, "status": "rejected"})

        merged = {
            "title": body.title if body.title is not None else ev_row["title"],
            "start_at": body.start_at if body.start_at is not None else ev_row["start_at"],
            "end_at": body.end_at if body.end_at is not None else ev_row["end_at"],
            "timezone": body.timezone if body.timezone is not None else ev_row["timezone"],
            "location": body.location if body.location is not None else ev_row["location"],
            "summary": ev_row.get("summary") or "",
            "confidence": max(float(ev_row.get("confidence") or 0.0), 0.9),
            "is_ambiguous": False,
            "is_recurring": False,
        }
        try:
            cand = ExtractedEvent.model_validate(merged)
        except Exception as e:
            return err(422, "validation", f"invalid event fields: {str(e)[:200]}", request)
        ok, reason = validate_event(cand)
        if not ok:
            store.update_scheduled_event(
                event_id,
                title=cand.title,
                start_at=cand.start_at,
                end_at=cand.end_at,
                timezone=cand.timezone,
                location=cand.location,
                reason=reason,
            )
            return err(422, "validation", f"still invalid: {reason}", request)

        cal = adapters.get("calendar")
        if cal is None or not hasattr(cal, "create_event"):
            return err(503, "no_adapter", "calendar adapter not configured", request)
        marker = f"review:{event_id}"
        try:
            existing = cal.find_by_marker(marker) if hasattr(cal, "find_by_marker") else None
            if existing:
                cal_event_id = existing
            else:
                cal_event_id = cal.create_event(
                    title=cand.title,
                    start_at=cand.start_at,
                    end_at=cand.end_at,
                    timezone=cand.timezone,
                    location=cand.location,
                    marker=marker,
                )
        except Exception as e:
            store.audit("", "api", "event_approve_error", event_id, {"error": str(e)[:200]})
            logger.warning("scheduled event %s approve failed: %s", event_id, str(e)[:200])
            return err(502, "calendar_error", str(e)[:300], request)
        store.update_scheduled_event(
            event_id,
            title=cand.title,
            start_at=cand.start_at,
            end_at=cand.end_at,
            timezone=cand.timezone,
            location=cand.location,
            status="registered",
            reason="",
            calendar_event_id=cal_event_id,
        )
        store.audit(
            "",
            "api",
            "event_approve",
            event_id,
            {"calendar_event_id": cal_event_id},
        )
        logger.info("scheduled event %s approved -> calendar_event=%s", event_id, cal_event_id)
        return JSONResponse(
            content={"ok": True, "status": "registered", "calendar_event_id": cal_event_id}
        )

    # ---- skillRadar ----
    @app.get("/api/v1/radar/trending")
    def radar_trending(
        platform: str = "claude", date: str | None = None, lang: str = "ko"
    ) -> JSONResponse:
        date_str = date or datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat()
        snap = store.radar_snapshot(platform, date_str)
        if snap is None:
            # 최신 스냅샷으로 폴백
            dates = store.radar_dates(platform)
            if not dates:
                return JSONResponse(
                    content={
                        "platform": platform,
                        "date": date_str,
                        "lang": lang,
                        "status": "no_snapshot",
                        "items": [],
                    }
                )
            date_str = dates[0]
            snap = store.radar_snapshot(platform, date_str)
        if snap is None:
            return JSONResponse(content={"items": [], "date": date_str})
        items = store.radar_items(int(snap["id"]))
        lang_key = {"ko": "summary_ko", "en": "summary_en", "zh": "summary_zh"}.get(
            lang, "summary_ko"
        )
        return JSONResponse(
            content={
                "platform": platform,
                "date": date_str,
                "lang": lang,
                "items": [
                    {
                        "rank": i["rank"],
                        "repo": i["repo_full_name"],
                        "url": i["repo_url"],
                        "description": i["description"],
                        "stars": i["stars"],
                        "stars_today": i["stars_today"],
                        "language": i["language"],
                        "license": i["license"],
                        "taxonomy": i["taxonomy"],
                        "summary": (i.get(lang_key) or i.get("summary_ko", "")).splitlines(),
                    }
                    for i in items
                ],
            }
        )

    @app.get("/api/v1/radar/dates")
    def radar_dates(platform: str = "claude") -> dict[str, Any]:
        return {"platform": platform, "dates": store.radar_dates(platform)}

    # ---- skillRadar 포팅: 토픽/랭킹/상세 조회 (원본 routes.py 대응) ----
    _RADAR_TOPIC_WINDOW_DAYS = 30
    _RADAR_TOPIC_MAX = 50
    _RADAR_RANKING_PERIODS = {"week": 7, "month": 30}
    _RADAR_LANGS = ("ko", "en", "zh")

    def _radar_since(days: int) -> str:
        return (datetime.now(ZoneInfo("Asia/Seoul")).date() - timedelta(days=days)).isoformat()

    def _radar_recent(days: int = _RADAR_TOPIC_WINDOW_DAYS) -> list[dict[str, Any]]:
        return store.radar_items_since(_radar_since(days))

    def _radar_item_json(row: dict[str, Any], lang: str) -> dict[str, Any]:
        lang_key = {"ko": "summary_ko", "en": "summary_en", "zh": "summary_zh"}.get(
            lang, "summary_ko"
        )
        return {
            "rank": row.get("rank"),
            "repo": row.get("repo_full_name"),
            "repo_full_name": row.get("repo_full_name"),
            "url": row.get("repo_url"),
            "repo_url": row.get("repo_url"),
            "appearances": row.get("appearances") or 1,
            "description": row.get("description"),
            "stars": row.get("stars"),
            "stars_today": row.get("stars_today"),
            "language": row.get("language"),
            "license": row.get("license"),
            "topics": json.loads(row.get("topics_json") or "[]"),
            "taxonomy": row.get("taxonomy"),
            "rank_score": row.get("rank_score"),
            "summary_status": row.get("summary_status"),
            "platform": row.get("platform"),
            "summary": (row.get(lang_key) or "").splitlines(),
        }

    @app.get("/api/v1/radar/topics")
    def radar_topics() -> dict[str, Any]:
        counts: dict[str, int] = {}
        for row in _radar_recent():
            for t in set(json.loads(row.get("topics_json") or "[]")):
                counts[t] = counts.get(t, 0) + 1
        top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:_RADAR_TOPIC_MAX]
        return {"topics": [{"topic": t, "count": c} for t, c in top]}

    @app.get("/api/v1/radar/topic-repos")
    def radar_topic_repos(
        request: Request,
        topic: str,
        platform: str = "all",
        lang: str = "ko",
        limit: int = 10,
    ) -> JSONResponse:
        if not topic or len(topic) > 100:
            return err(400, "validation", "topic 파라미터가 필요합니다", request)
        if platform != "all" and platform not in settings.radar_platforms:
            return err(400, "validation", "platform은 all 또는 radar_platforms 중 하나", request)
        if lang not in _RADAR_LANGS:
            return err(400, "validation", "lang은 ko|en|zh 중 하나", request)
        limit = max(1, min(limit, 50))
        best: dict[str, dict[str, Any]] = {}
        for row in _radar_recent():
            if topic not in json.loads(row.get("topics_json") or "[]"):
                continue
            if platform != "all" and row.get("platform") != platform:
                continue
            prev = best.get(row["repo_full_name"])
            if prev is None or (row.get("stars") or 0) > (prev.get("stars") or 0):
                best[row["repo_full_name"]] = row
        ranked = sorted(
            best.values(),
            key=lambda r: (
                r.get("rank_score") if r.get("rank_score") is not None else float("-inf"),
                r.get("stars") or 0,
            ),
            reverse=True,
        )[:limit]
        return JSONResponse(
            content={
                "platform": platform,
                "date": datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat(),
                "lang": lang,
                "items": [_radar_item_json(r, lang) for r in ranked],
            }
        )

    @app.get("/api/v1/radar/related-topics")
    def radar_related_topics(request: Request, topic: str) -> JSONResponse:
        if not topic or len(topic) > 100:
            return err(400, "validation", "topic 파라미터가 필요합니다", request)
        co: dict[str, int] = {}
        for row in _radar_recent():
            topics = set(json.loads(row.get("topics_json") or "[]"))
            if topic not in topics:
                continue
            for other in topics - {topic}:
                co[other] = co.get(other, 0) + 1
        top = sorted(co.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
        return JSONResponse(
            content={"topic": topic, "related": [{"topic": t, "score": c} for t, c in top]}
        )

    @app.get("/api/v1/radar/ranking")
    def radar_ranking(
        request: Request, period: str = "week", platform: str = "all", lang: str = "ko"
    ) -> JSONResponse:
        days = _RADAR_RANKING_PERIODS.get(period)
        if days is None:
            return err(400, "validation", "period은 week|month 중 하나", request)
        if platform != "all" and platform not in settings.radar_platforms:
            return err(400, "validation", "platform은 all 또는 radar_platforms 중 하나", request)
        if lang not in _RADAR_LANGS:
            return err(400, "validation", "lang은 ko|en|zh 중 하나", request)
        agg: dict[str, dict[str, Any]] = {}
        for row in _radar_recent(days):
            if platform != "all" and row.get("platform") != platform:
                continue
            key = row["repo_full_name"]
            cur = agg.get(key)
            if cur is None:
                agg[key] = {
                    "row": row,
                    "appearances": 1,
                    "gain": int(row.get("stars_today") or 0),
                }
                continue
            cur["appearances"] += 1
            cur["gain"] += int(row.get("stars_today") or 0)
            if (row.get("collected_date") or "") > (cur["row"].get("collected_date") or ""):
                cur["row"] = row
        ranked = sorted(
            agg.values(),
            key=lambda a: (a["gain"], a["appearances"], a["row"].get("stars") or 0),
            reverse=True,
        )[:30]
        items = []
        for idx, a in enumerate(ranked, 1):
            payload = _radar_item_json(a["row"], lang)
            payload["rank"] = idx
            payload["appearances"] = a["appearances"]
            items.append(payload)
        return JSONResponse(
            content={
                "platform": platform,
                "date": datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat(),
                "lang": lang,
                "items": items,
            }
        )

    @app.get("/api/v1/radar/repo/{full_name:path}")
    def radar_repo_detail(request: Request, full_name: str, lang: str = "ko") -> JSONResponse:
        if lang not in _RADAR_LANGS:
            return err(400, "validation", "lang은 ko|en|zh 중 하나", request)
        row = store.radar_repo_latest(full_name)
        if row is None:
            return err(404, "not_found", "수집된 저장소가 아닙니다", request)
        topics = set(json.loads(row.get("topics_json") or "[]"))
        co: dict[str, int] = {}
        for other_row in _radar_recent():
            other_topics = set(json.loads(other_row.get("topics_json") or "[]"))
            if not (topics & other_topics):
                continue
            for t in other_topics - topics:
                co[t] = co.get(t, 0) + 1
        related = [t for t, _c in sorted(co.items(), key=lambda kv: (-kv[1], kv[0]))[:8]]
        payload = _radar_item_json(row, lang)
        payload.update(
            {
                "readme_excerpt": row.get("readme_excerpt") or "",
                "summaries": {
                    "en": (row.get("summary_en") or "").splitlines(),
                    "zh": (row.get("summary_zh") or "").splitlines(),
                    "ko": (row.get("summary_ko") or "").splitlines(),
                },
                "related_topics": related,
                "collected_date": row.get("collected_date"),
                "snapshot_platform": row.get("platform"),
            }
        )
        return JSONResponse(content={"item": payload})

    @app.post("/api/v1/radar/refresh", status_code=202)
    def radar_refresh(
        background_tasks: BackgroundTasks, body: dict[str, Any] | None = None
    ) -> JSONResponse:
        body = body or {}
        platforms = body.get("platforms") or settings.radar_platforms
        force = bool(body.get("force", True))
        auto_approve = bool(body.get("auto_approve", False))
        jobs: list = []
        ids = []
        for p in platforms:
            rid = submit_run(
                "skill_radar",
                f"radar collect {p}",
                {"platform": p, "force": force, "auto_approve": auto_approve},
                defer=jobs,
            )
            ids.append(rid)
        _schedule_run_jobs(jobs, background_tasks)
        return JSONResponse(status_code=202, content={"run_ids": ids, "platforms": platforms})

    # ---- Web Push 구독 ----
    @app.get("/api/v1/push/vapid-key")
    def vapid_key() -> dict[str, str]:
        return {"public_key": os.environ.get("VAPID_PUBLIC_KEY", "")}

    @app.post("/api/v1/push/subscribe")
    def push_subscribe(body: dict[str, Any], request: Request) -> Any:
        from ..adapters.push import valid_push_subscription

        endpoint = body.get("endpoint", "")
        keys = body.get("keys") or {}
        if not valid_push_subscription(endpoint, keys):
            return err(422, "invalid_subscription", "invalid push subscription", request)
        store.save_push_subscription(endpoint, keys)
        logger.info("push subscribed: sub=%s", _endpoint_fp(endpoint))
        return {"status": "subscribed"}

    @app.post("/api/v1/push/unsubscribe")
    def push_unsubscribe(body: dict[str, Any]) -> dict[str, str]:
        endpoint = body.get("endpoint", "")
        store.delete_push_subscription(endpoint)
        if endpoint:
            logger.info("push unsubscribed: sub=%s", _endpoint_fp(endpoint))
        return {"status": "unsubscribed"}

    @app.post("/api/v1/push/test")
    def push_test() -> dict[str, Any]:
        """구독된 모든 디바이스로 테스트 푸시 발송 — 연결 검증용."""
        subs = store.list_push_subscriptions()
        if not subs:
            return {"status": "no_subscriptions", "sent": 0}
        from ..engine.functions import send_web_push_text

        send_web_push_text(adapters, "jobMate 테스트 알림", "Web Push 연결이 정상입니다.", "/")
        logger.info("push test sent: subs=%d", len(subs))
        return {"status": "sent", "sent": len(subs)}

    # ---- 계정 인증 (이메일+비밀번호 / Google OAuth) ----
    from urllib.parse import urlencode

    import httpx

    from ..security import (
        OAUTH_STATE_COOKIE,
        SESSION_COOKIE,
        SESSION_TTL_DAYS,
        allowed_emails,
        api_key,
        authorized,
        email_allowed,
        hash_password,
        hash_token,
        new_session_token,
        verify_password,
    )

    _EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    _login_fails: dict[str, list[float]] = {}
    _LOGIN_WINDOW = 600.0
    _LOGIN_MAX_FAILS = 10

    def _pub_user(u: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": u["id"],
            "email": u["email"],
            "display_name": u["display_name"],
            "role": _effective_role(u),
        }

    def _maybe_first_user_admin(user: dict[str, Any]) -> None:
        """첫 번째 가입 사용자는 자동 admin — 이후 관리자는 role/env로 지정."""
        try:
            if len(store.list_users()) == 1:
                store.set_user_role(user["id"], "admin")
        except Exception as e:
            logger.warning("first-user admin promotion failed: %s", str(e)[:200])

    def _set_session(response: Any, request: Request, user_id: str) -> None:
        token = new_session_token()
        store.create_session(user_id, hash_token(token), SESSION_TTL_DAYS)
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=SESSION_TTL_DAYS * 86400,
            httponly=True,
            samesite="lax",
            secure=request.url.scheme == "https",
            path="/",
        )

    def _login_key(request: Request, email: str) -> str:
        ip = request.client.host if request.client else "-"
        return f"{ip}:{email.strip().lower()}"

    def _login_throttled(key: str) -> bool:
        now = time.time()
        fails = [t for t in _login_fails.get(key, []) if now - t < _LOGIN_WINDOW]
        _login_fails[key] = fails
        return len(fails) >= _LOGIN_MAX_FAILS

    def _login_record(key: str, ok: bool) -> None:
        if ok:
            _login_fails.pop(key, None)
        else:
            _login_fails.setdefault(key, []).append(time.time())

    @app.post("/api/v1/auth/signup")
    def auth_signup(body: dict[str, Any], request: Request) -> JSONResponse:
        # 허용 목록이 설정된 경우에만 비밀번호 가입을 제한한다 — 비밀번호는
        # 이메일 소유권을 증명할 수 없어 허용 목록 이메일을 임의로 선점할 수 있다.
        if allowed_emails():
            if not api_key() or not authorized(request):
                logger.warning("signup rejected: administrator API key required")
                return err(403, "signup_restricted", "관리자 API 키가 필요합니다", request)
        email = str(body.get("email", "")).strip().lower()
        password = str(body.get("password", ""))
        name = str(body.get("display_name", "")).strip()[:80]
        if not _EMAIL_RE.match(email):
            return err(400, "invalid_email", "이메일 형식이 올바르지 않습니다", request)
        if len(password) < 8:
            return err(400, "weak_password", "비밀번호는 8자 이상이어야 합니다", request)
        if not email_allowed(email):
            logger.warning("signup denied (not allowed): %s", email)
            return err(403, "email_not_allowed", "허용되지 않은 이메일입니다", request)
        user = store.create_user(email, hash_password(password), name)
        if user is None:
            return err(409, "email_taken", "이미 가입된 이메일입니다", request)
        _maybe_first_user_admin(user)
        logger.info("auth signup: user=%s", user["id"])
        resp = JSONResponse({"user": _pub_user(user)}, status_code=201)
        _set_session(resp, request, user["id"])
        return resp

    @app.post("/api/v1/auth/login")
    def auth_login(body: dict[str, Any], request: Request) -> JSONResponse:
        email = str(body.get("email", "")).strip().lower()
        password = str(body.get("password", ""))
        key = _login_key(request, email)
        if _login_throttled(key):
            logger.warning("login throttled: %s", key)
            return err(
                429, "too_many_attempts", "시도 횟수 초과 — 잠시 후 다시 시도하세요", request
            )
        row = store.get_user_password_hash(email)
        if row is None or not row[1] or not verify_password(password, row[1]):
            _login_record(key, ok=False)
            logger.info("login failed: %s", email)
            return err(
                401, "invalid_credentials", "이메일 또는 비밀번호가 올바르지 않습니다", request
            )
        if not email_allowed(email):
            return err(403, "email_not_allowed", "허용되지 않은 이메일입니다", request)
        _login_record(key, ok=True)
        user = store.get_user_by_id(row[0])
        logger.info("auth login: user=%s", row[0])
        resp = JSONResponse({"user": _pub_user(user) if user else {}})
        _set_session(resp, request, row[0])
        return resp

    @app.post("/api/v1/auth/logout")
    def auth_logout(request: Request) -> Any:
        resp = JSONResponse({"status": "logged_out"})
        token = request.cookies.get(SESSION_COOKIE, "")
        if token:
            store.revoke_session(hash_token(token))
        resp.delete_cookie(SESSION_COOKIE, path="/")
        return resp

    @app.get("/api/v1/auth/config")
    def auth_config() -> dict[str, bool]:
        """로그인 화면용 공개 설정 — 외부 노출 정보 없음."""
        return {"google_enabled": bool(os.environ.get("GOOGLE_CLIENT_ID", "").strip())}

    @app.get("/api/v1/auth/me")
    def auth_me(request: Request) -> JSONResponse:
        user = _session_user(request)
        if user is None:
            return err(401, "unauthorized", "로그인이 필요합니다", request)
        return JSONResponse({"user": _pub_user(user)})

    def _google_redirect_uri(request: Request) -> str:
        return os.environ.get("GOOGLE_REDIRECT_URI", "").strip() or (
            str(request.base_url).rstrip("/") + "/api/v1/auth/google/callback"
        )

    @app.get("/api/v1/auth/google")
    def auth_google(request: Request) -> Any:
        client_id = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
        if not client_id:
            return err(501, "not_configured", "Google 로그인이 설정되지 않았습니다", request)
        state = new_session_token()
        url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(
            {
                "client_id": client_id,
                "redirect_uri": _google_redirect_uri(request),
                "response_type": "code",
                "scope": "openid email profile",
                "state": state,
                "prompt": "select_account",
            }
        )
        resp = RedirectResponse(url, status_code=302)
        resp.set_cookie(
            OAUTH_STATE_COOKIE,
            state,
            max_age=600,
            httponly=True,
            samesite="lax",
            secure=request.url.scheme == "https",
            path="/",
        )
        return resp

    @app.get("/api/v1/auth/google/callback")
    def auth_google_callback(
        request: Request, code: str = "", state: str = "", error: str = ""
    ) -> Any:
        def back(q: str) -> RedirectResponse:
            return RedirectResponse(f"/?auth_error={q}", status_code=302)

        if error:
            return back("google_denied")
        expected = request.cookies.get(OAUTH_STATE_COOKIE, "")
        if not state or not expected or state != expected:
            logger.warning("google oauth state mismatch")
            return err(400, "bad_state", "OAuth state 불일치", request)
        client_id = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
        client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
        if not client_id or not client_secret:
            return err(501, "not_configured", "Google 로그인이 설정되지 않았습니다", request)
        try:
            token_resp = httpx.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "code": code,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uri": _google_redirect_uri(request),
                    "grant_type": "authorization_code",
                },
                timeout=15,
            )
            token_resp.raise_for_status()
            access_token = token_resp.json().get("access_token", "")
            info_resp = httpx.get(
                "https://openidconnect.googleapis.com/v1/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=15,
            )
            info_resp.raise_for_status()
            info = info_resp.json()
        except Exception as e:
            logger.warning("google oauth exchange failed: %s", str(e)[:200])
            return back("google_exchange_failed")
        gsub = str(info.get("sub", ""))
        email = str(info.get("email", "")).strip().lower()
        name = str(info.get("name", "")).strip()[:80]
        if not gsub or not _EMAIL_RE.match(email):
            return back("google_no_email")
        if info.get("email_verified") is not True:
            logger.warning("google login rejected: unverified email")
            return back("google_unverified_email")
        if not email_allowed(email):
            logger.warning("google login denied (not allowed): %s", email)
            return back("email_not_allowed")
        user = store.get_user_by_google_sub(gsub)
        if user is None:
            user = store.get_user_by_email(email)
            if user is not None and user.get("google_sub") not in ("", gsub):
                return back("google_account_mismatch")
            if user is not None and not user.get("google_sub"):
                store.link_google_sub(user["id"], gsub)
        if user is None:
            user = store.create_user(email, "", name, gsub)
            if user is None:
                return back("signup_failed")
            _maybe_first_user_admin(user)
            logger.info("auth signup via google: user=%s", user["id"])
        logger.info("auth login via google: user=%s", user["id"])
        resp = RedirectResponse("/", status_code=302)
        _set_session(resp, request, user["id"])
        resp.delete_cookie(OAUTH_STATE_COOKIE, path="/")
        return resp

    # ---- 사이드바 뷰 집계 (읽기 전용) — 인증은 /api/* 미들웨어와 동일 ----
    from ..insights import (
        archive_search,
        automation_overview,
        developer_status,
        insights_summary,
    )

    @app.get("/api/v1/insights")
    def insights(days: int = 30) -> dict[str, Any]:
        """인사이트 뷰 — 세션/런/비용/실패 집계 (최대 365일)."""
        return insights_summary(store, sorted(WORKFLOWS), max(1, min(days, 365)))

    @app.get("/api/v1/automation/overview")
    def automation_overview_endpoint() -> dict[str, Any]:
        """자동화 허브 뷰 — 스케줄/알림 규칙/cron/메신저/리포트 상태."""
        return automation_overview(store, _prefs().get("notify_rules", []), _last_cron_tick_at())

    @app.get("/api/v1/archive")
    def archive(request: Request, q: str = "", kind: str = "", limit: int = 50) -> Any:
        """지식/아카이브 검색 — 리포트·run·종료 세션 통합."""
        if kind and kind not in ("report", "run", "session"):
            return err(422, "validation_error", "kind must be report|run|session", request)
        return archive_search(store, q, kind, limit)

    @app.get("/api/v1/developer/status")
    def developer_status_endpoint() -> dict[str, Any]:
        """개발자 뷰 — 연동/인증 구성 상태 (값이 아닌 bool/노트만)."""
        return developer_status(store, settings, adapters)

    return app


# ---------- automation worker ----------

# run_id → waiting_approval 진입 시점의 updated_at 지문 (재전이 감지용)
_WAITING_APPROVAL_FP: dict[str, str] = {}


def _execute_run_job(
    store: Store,
    settings: Settings,
    llm: Any,
    adapters: dict[str, Any],
    run_id: str,
    workflow: str,
    payload: dict[str, Any],
    ctx: dict[str, Any] | None = None,
) -> None:
    """run 1건을 동기 실행 — automation loop/스레드/cron tick 공용."""
    try:
        runner = build_runner(workflow, store=store, llm=llm, settings=settings, adapters=adapters)
        runner.invoke(run_id, payload, gmail_ctx=ctx)
    except Exception as e:
        store.update_run(run_id, status=RunStatus.FAILED, error=str(e)[:500])
        store.audit(run_id, "system", "runner_error", "", {"error": str(e)[:300]})
        logger.warning("run %s failed: %s", run_id, str(e)[:200])
        return
    run = store.get_run(run_id)
    logger.info("run %s finished status=%s", run_id, getattr(run, "status", "?"))


def _process_due_schedules(
    store: Store,
    settings: Settings,
    llm: Any,
    adapters: dict[str, Any],
    defer: list[tuple[str, str, dict[str, Any], dict[str, Any] | None]] | None = None,
) -> None:
    """workflow_schedules 중 next_run_at이 지난 enabled 스케줄을 run으로 발사.

    defer가 주어지면 job을 defer에 추가(호출자가 _schedule_run_jobs로 실행),
    None이면 daemon thread로 즉시 실행. 발사 시각/다음 실행 시각을 갱신하고
    run의 audit에 scheduled_run 이벤트를 남긴다."""
    for sched in store.due_schedules():
        sid = str(sched.get("id") or "")
        workflow = str(sched.get("workflow") or "")
        try:
            interval = max(int(sched.get("interval_minutes") or 0), 1)
            fired = datetime.now(UTC)
            nxt = fired + timedelta(minutes=interval)
            if workflow not in WORKFLOWS:
                worker_logger.warning("schedule %s skipped: unknown workflow %s", sid, workflow)
                store.mark_schedule_fired(sid, fired.isoformat(), nxt.isoformat())
                continue
            payload = dict(sched.get("input") or {})
            payload.setdefault("task", f"scheduled {workflow}")
            run = store.create_run(
                workflow, WORKFLOWS[workflow].version, payload, trace_id=uuid.uuid4().hex
            )
            store.mark_schedule_fired(sid, fired.isoformat(), nxt.isoformat())
            store.audit(
                run.id,
                "system",
                "scheduled_run",
                sid,
                {"interval_minutes": interval},
            )
            # 통합 인박스 — 스케줄 발사/실패 알림
            try:
                store.upsert_inbox_item(
                    "schedule",
                    f"스케줄 실행: {workflow}",
                    body=str(payload.get("task") or "")[:300],
                    link="/?view=office",
                    dedupe_key=f"schedule:{run.id}",
                )
            except Exception as e:
                worker_logger.warning("inbox schedule item failed: %s", str(e)[:200])
            job = (run.id, workflow, payload, None)
            if defer is not None:
                defer.append(job)
            else:
                threading.Thread(
                    target=_execute_run_job,
                    args=(store, settings, llm, adapters, *job),
                    daemon=True,
                ).start()
            worker_logger.info("schedule %s fired run=%s workflow=%s", sid, run.id, workflow)
        except Exception as e:
            worker_logger.warning("schedule %s fire failed: %s", sid, str(e)[:200])
            try:
                store.upsert_inbox_item(
                    "schedule",
                    f"스케줄 실패: {workflow}",
                    body=str(e)[:300],
                    link="/?view=office",
                    dedupe_key=f"schedule-fail:{sid}:{datetime.now(UTC).isoformat()}",
                )
            except Exception as inbox_err:
                worker_logger.debug("inbox schedule-fail item failed: %s", str(inbox_err)[:200])


def _parse_diff_files(diff_text: str) -> list[dict[str, Any]]:
    """unified diff 텍스트를 파일 단위 [{path,status,additions,deletions,diff}]로 분해."""
    files: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            if cur is not None:
                files.append(cur)
            cur = {
                "path": "",
                "status": "modified",
                "additions": 0,
                "deletions": 0,
                "_lines": [line],
            }
            # "diff --git a/<old> b/<new>" — best-effort 경로 (마커 라인이 보정)
            rest = line[len("diff --git ") :]
            if " b/" in rest:
                cur["path"] = rest.rsplit(" b/", 1)[1].strip().strip('"')
            continue
        if cur is None:
            continue
        cur["_lines"].append(line)
        if line.startswith("new file mode"):
            cur["status"] = "added"
        elif line.startswith("deleted file mode"):
            cur["status"] = "deleted"
        elif line.startswith("rename from ") or line.startswith("rename to "):
            cur["status"] = "renamed"
        elif line.startswith("+++ ") or line.startswith("--- "):
            marker = line[4:].strip()
            if marker != "/dev/null":
                path = marker[2:] if marker[:2] in ("a/", "b/") else marker
                path = path.strip('"')
                if line.startswith("+++ ") or not cur["path"]:
                    cur["path"] = path
        elif line.startswith("+"):
            cur["additions"] += 1
        elif line.startswith("-"):
            cur["deletions"] += 1
    if cur is not None:
        files.append(cur)
    return [
        {
            "path": f["path"] or "unknown",
            "status": f["status"],
            "additions": f["additions"],
            "deletions": f["deletions"],
            "diff": "\n".join(f["_lines"]),
        }
        for f in files
    ]


def _check_waiting_approvals(store: Store, adapters: dict[str, Any]) -> None:
    """dev_review run이 waiting_approval에 '전이'할 때만 웹 푸시.

    updated_at 지문으로 동일 대기 상태의 반복 감지를 막고, 승인을 거쳐
    다시 대기에 들어오면(지문 변경) 새로 알린다."""
    from ..engine.functions import send_web_push_text

    waiting: dict[str, Any] = {}
    for run in store.list_runs(limit=100):
        if run.status == RunStatus.WAITING_APPROVAL:
            waiting[run.id] = run
    for rid in list(_WAITING_APPROVAL_FP):
        if rid not in waiting:
            _WAITING_APPROVAL_FP.pop(rid, None)
    for run in waiting.values():
        fp = run.updated_at.isoformat() if run.updated_at else run.id
        if _WAITING_APPROVAL_FP.get(run.id) == fp:
            continue
        _WAITING_APPROVAL_FP[run.id] = fp
        task = ""
        if isinstance(run.input, dict):
            task = str(run.input.get("task", ""))[:200]
        send_web_push_text(adapters, f"승인 대기: {run.workflow_key}", task, "/?view=office")
        # 통합 인박스 — run별 dedupe, 재대기 시 미읽음으로 리셋
        try:
            store.upsert_inbox_item(
                "approval",
                f"승인 대기: {run.workflow_key}",
                body=task,
                link="/?view=office",
                dedupe_key=f"approval:{run.id}",
                reset_read=True,
            )
        except Exception as e:
            logger.warning("inbox approval item failed run=%s: %s", run.id, str(e)[:200])
        try:
            messenger.notify(f"승인 대기: {run.workflow_key} — {task or run.id}")
        except Exception:
            logger.debug("messenger notify failed", exc_info=True)
        logger.info("waiting_approval push sent run=%s", run.id)


def _sync_cronjob(settings: Settings, store: Store, prefs: dict[str, Any]) -> dict[str, str]:
    """prefs를 cron-job.org 실제 스케줄에 반영. job id가 없고 API 키+
    public_base_url이 설정돼 있으면 job을 자동 생성하고 prefs에 id를 저장한다."""
    import json
    import os

    from ..adapters.cronjob import (
        CronJobOrgClient,
        create_job,
        daily_at,
        interval_schedule,
        weekly_at,
    )

    api_key = os.environ.get(settings.cronjob_api_key_env, "")
    out: dict[str, str] = {}
    # 공개 URL: 명시 설정 → Vercel 배포 URL 순. 둘 다 없으면 자동 생성 불가.
    public_base = settings.public_base_url or (
        f"https://{os.environ['VERCEL_URL']}" if os.environ.get("VERCEL_URL") else ""
    )

    def ensure_job(
        job_id: str,
        pref_key: str,
        title: str,
        path: str,
        body: str,
        schedule: dict[str, Any],
        label: str,
    ) -> str:
        """job id 확보(기존 → env → 자동 생성) 후 스케줄 갱신."""
        job_id = job_id or str(prefs.get(pref_key) or "")
        # 요청 헤더/URL은 생성·갱신 모두에 사용 — API 키 로테이션 시
        # 기존 job의 stale 헤더를 함께 갱신하기 위함.
        req_headers: dict[str, str] = {"Content-Type": "application/json"}
        jm_key = os.environ.get("JOBMATE_API_KEY", "")
        if jm_key:
            req_headers["X-API-Key"] = jm_key
        req_url = (public_base.rstrip("/") + path) if public_base else ""
        extended = {"headers": req_headers, "body": body} if (req_headers or body) else None
        if not job_id:
            if not api_key or not public_base:
                out[label] = "skipped(no job id)"
                return ""
            try:
                job_id = str(
                    create_job(
                        api_key,
                        base_url=settings.cronjob_base_url,
                        title=title,
                        url=req_url,
                        schedule=schedule,
                        headers=req_headers,
                        body=body,
                    )
                )
                prefs[pref_key] = job_id
                store.set_state("prefs", prefs)
                out[label] = f"created({job_id})"
                logger.info("cronjob %s auto-created job_id=%s", label, job_id)
            except Exception as e:
                out[label] = f"error(create): {str(e)[:120]}"
                logger.warning("cronjob %s create failed: %s", label, str(e)[:200])
                return ""
        if not api_key:
            out[label] = "skipped(no api key)"
            return ""
        try:
            CronJobOrgClient(api_key, job_id, settings.cronjob_base_url).update_schedule(
                schedule, url=req_url or None, extended_data=extended
            )
            out[label] = "updated" if "created" not in out.get(label, "") else out[label]
            logger.info("cronjob %s schedule updated job_id=%s", label, job_id)
        except Exception as e:
            out[label] = f"error: {str(e)[:120]}"
            logger.warning("cronjob %s update failed job_id=%s: %s", label, job_id, str(e)[:200])
        return job_id

    ensure_job(
        settings.cronjob_job_id,
        "cronjob_email_job_id",
        "jobMate email check",
        "/api/v1/email/check",
        json.dumps({"limit": 5}),
        interval_schedule(int(prefs.get("gmail_check_minutes", 30))),
        "email",
    )
    radar_schedule = (
        weekly_at(6, 0, wday=1) if prefs.get("radar_interval") == "weekly" else daily_at(6, 0)
    )
    ensure_job(
        settings.cronjob_radar_job_id,
        "cronjob_radar_job_id",
        "jobMate radar refresh",
        "/api/v1/radar/refresh",
        # auto_approve: 무인 cron 수집은 승인 없이 publish+푸시까지 진행
        json.dumps({"force": True, "auto_approve": True}),
        radar_schedule,
        "radar",
    )
    return out


def _automation_loop(
    store: Store, settings: Settings, llm: Any, adapters: dict[str, Any], stop: threading.Event
) -> None:
    """calendar-agent worker 포팅: pending 메시지 처리 + 알림 예약 발송 +
    gmail 폴링 + radar 일일 수집."""
    while not stop.is_set():
        try:
            _mark_cron_tick()
            _process_pending_messages(store, settings, llm, adapters)
            _process_due_notification_jobs(store, adapters)
            _process_due_schedules(store, settings, llm, adapters)
            _check_waiting_approvals(store, adapters)
            _maybe_poll_gmail(store, settings, adapters)
            _maybe_daily_radar(store, settings, llm, adapters)
            _maybe_generate_reports(store, settings, adapters)
        except Exception:
            worker_logger.exception("automation loop error")
        stop.wait(settings.worker_interval_seconds)


def _process_pending_messages(
    store: Store, settings: Settings, llm: Any, adapters: dict[str, Any]
) -> None:
    for row in store.pending_messages():
        provider = row["source_provider"]
        source_id = row["source_id"]
        attempts = int(row.get("attempts", 0)) + 1
        try:
            if attempts > 5:
                store.finish_message(
                    provider, source_id, "failed", "max attempts", attempts=attempts
                )
                worker_logger.warning(
                    "message %s:%s failed permanently after %s attempts",
                    provider,
                    source_id,
                    attempts,
                )
                continue
            ctx = {
                "source_provider": provider,
                "source_id": source_id,
                "thread_id": row.get("thread_id", ""),
                "message_text": row.get("source_text", ""),
            }
            if provider == "gmail":
                ctx["gmail_message_id"] = source_id
            run_id = _create_and_run(
                store, settings, llm, adapters, "gmail_calendar", "message intake", ctx
            )
            store.finish_message(
                provider, source_id, "processed", f"run:{run_id}", attempts=attempts
            )
        except Exception as e:
            worker_logger.warning(
                "message %s:%s processing failed (attempt %s): %s",
                provider,
                source_id,
                attempts,
                str(e)[:200],
            )
            retry_at = datetime.now(UTC) + timedelta(seconds=min(300, 2**attempts))
            store.finish_message(
                provider,
                source_id,
                "pending",
                str(e)[:200],
                retry_at=retry_at.isoformat(),
                attempts=attempts,
            )


def _create_and_run(
    store: Store,
    settings: Settings,
    llm: Any,
    adapters: dict[str, Any],
    workflow: str,
    task: str,
    ctx: dict[str, Any] | None = None,
) -> str:
    run = store.create_run(
        workflow, WORKFLOWS[workflow].version, {"task": task}, trace_id=uuid.uuid4().hex
    )
    runner = build_runner(workflow, store=store, llm=llm, settings=settings, adapters=adapters)
    runner.invoke(run.id, {"task": task}, gmail_ctx=ctx)
    done = store.get_run(run.id)
    logger.info("run %s finished status=%s", run.id, getattr(done, "status", "?"))
    return run.id


def _process_due_notification_jobs(store: Store, adapters: dict[str, Any]) -> None:
    notifier = adapters.get("notifier")
    if notifier is None:
        return
    for job in store.due_notification_jobs():
        try:
            # MVP: 본문은 스케줄 이벤트 제목으로 구성
            ev_rows = store.list_scheduled_events(limit=200)
            title = "일정 알림"
            source_url = ""
            for ev in ev_rows:
                if f"{ev['source_provider']}:{ev['source_id']}" == job["scheduled_event_id"]:
                    title = f"곧 시작: {ev['title']}"
                    # gmail 유래 이벤트는 원본 메일 링크를 푸시에 포함
                    if ev["source_provider"] == "gmail" and ev["source_id"]:
                        try:
                            url = build_gmail_url(ev["source_id"])
                            source_url = url if is_safe_source_url(url) else ""
                        except ValueError:
                            pass
                    break
            notifier.send(title=title, body="", source_url=source_url)
            # notifier는 로컬 로그용 — 실제 디바이스 푸시는 webpush 채널로도 발송
            from ..engine.functions import send_web_push_text

            send_web_push_text(adapters, title, "", source_url)
            store.mark_job(job["id"], "delivered")
            logger.debug("notification job %s delivered", job["id"])
        except Exception as e:
            store.mark_job(job["id"], "failed", str(e)[:200])
            worker_logger.warning(
                "notification job %s delivery failed: %s", job["id"], str(e)[:200]
            )


def _maybe_poll_gmail(store: Store, settings: Settings, adapters: dict[str, Any]) -> None:
    auto = store.get_automation()
    if not auto.get("enabled"):
        return
    last = auto.get("last_gmail_poll_at")
    prefs = store.get_state("prefs", {})
    minutes = int(prefs.get("gmail_check_minutes") or 0)
    interval_secs = minutes * 60 if minutes in (30, 60) else settings.gmail_poll_interval_seconds
    if last:
        try:
            prev = datetime.fromisoformat(last)
            if datetime.now(UTC) - prev < timedelta(seconds=interval_secs):
                return
        except ValueError:
            pass
    gmail = adapters.get("gmail")
    if gmail is None:
        return
    try:
        ingested = _poll_gmail(store, settings, gmail)
        auto["last_gmail_poll_at"] = datetime.now(UTC).isoformat()
        auto["last_poll_result"] = f"ingested:{ingested}"
        auto["last_gmail_error"] = ""
        if ingested:
            logger.info("gmail poll ingested %d new messages", ingested)
    except Exception as e:
        auto["last_gmail_error"] = type(e).__name__
        worker_logger.warning("gmail poll failed: %s", type(e).__name__)
    store.set_automation(auto)


def _poll_gmail(store: Store, settings: Settings, gmail: Any) -> int:
    """Gmail adapter로 새 메시지를 수신함에 적재. 반환: 신규 건수."""
    ingested = 0
    for ref in gmail.list_messages(settings.gmail_query, 50):
        msg_id = ref.get("id", "")
        if not msg_id or store.has_message("gmail", msg_id):
            continue
        msg = gmail.get_message(msg_id)
        text = f"{msg.get('subject', '')}\n{msg.get('body_text', '')}"
        if store.save_message_extraction(
            "gmail", msg_id, msg.get("thread_id", ""), {}, "pending", source_text=text[:8000]
        ):
            ingested += 1
    return ingested


def _maybe_daily_radar(
    store: Store,
    settings: Settings,
    llm: Any,
    adapters: dict[str, Any],
    defer: list[tuple[str, str, dict[str, Any], dict[str, Any] | None]] | None = None,
) -> None:
    """KST 06:00 이후 최초 1회 radar 자동 수집 (자동화 플래그)."""
    auto = store.get_automation()
    if not auto.get("radar_enabled"):
        return
    now_kst = datetime.now(ZoneInfo("Asia/Seoul"))
    today = now_kst.date().isoformat()
    if now_kst.hour < 6:
        return
    prefs = store.get_state("prefs", {})
    last_radar = auto.get("last_radar_date")
    if last_radar == today:
        return
    if prefs.get("radar_interval") == "weekly" and last_radar:
        try:
            if (now_kst.date() - datetime.fromisoformat(last_radar).date()).days < 7:
                return
        except ValueError:
            pass
    if adapters.get("github") is None:
        return
    scheduled = []
    for p in settings.radar_platforms:
        if not store.radar_snapshot_exists(p, today):
            task = f"radar daily {p}"
            ctx = {"platform": p, "force": False, "auto_approve": True}
            if defer is not None:
                run = store.create_run(
                    "skill_radar",
                    WORKFLOWS["skill_radar"].version,
                    {"task": task},
                    trace_id=uuid.uuid4().hex,
                )
                defer.append((run.id, "skill_radar", {"task": task}, ctx))
            else:
                _create_and_run(store, settings, llm, adapters, "skill_radar", task, ctx)
            scheduled.append(p)
    auto["last_radar_date"] = today
    store.set_automation(auto)
    if scheduled:
        logger.info("radar daily collect scheduled: %s", ",".join(scheduled))


_REPORT_MAX_AGE = {"daily": timedelta(hours=20), "weekly": timedelta(days=7)}


def _report_is_stale(store: Store, kind: str) -> bool:
    """해당 kind의 최신 저장 리포트가 없거나 최대 주기보다 오래됐으면 True."""
    rep = store.latest_report(kind)
    if rep is None or not rep.get("created_at"):
        return True
    try:
        created = datetime.fromisoformat(str(rep["created_at"]).replace("Z", "+00:00"))
    except ValueError:
        return True
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return datetime.now(UTC) - created > _REPORT_MAX_AGE[kind]


def _maybe_generate_reports(store: Store, settings: Settings, adapters: dict[str, Any]) -> None:
    """daily 리포트 20시간/weekly 리포트 7일 주기로 자동 생성.
    생성 시 reports 테이블 저장 + 인박스 kind='report' + 메신저 알림(best-effort)."""
    from ..reports import build_report

    for kind in ("daily", "weekly"):
        try:
            if not _report_is_stale(store, kind):
                continue
            content = build_report(store, kind, settings)
            rid = store.save_report(kind, content)
            store.upsert_inbox_item(
                "report",
                f"{kind} 리포트 생성됨",
                body=(
                    f"runs={content['runs']['total']} failed={content['runs']['failed']} "
                    f"pending={content['approvals']['pending']}"
                ),
                link="/?view=reports",
                dedupe_key=f"report:{rid}",
            )
            store.audit("", "system", "report_auto_generate", rid, {"kind": kind})
            worker_logger.info("auto report generated id=%s kind=%s", rid, kind)
            try:
                messenger.notify(
                    f"jobMate {kind} 리포트: run {content['runs']['total']}건 "
                    f"(실패 {content['runs']['failed']}), "
                    f"승인 대기 {content['approvals']['pending']}"
                )
            except Exception:
                worker_logger.debug("messenger notify after auto report failed", exc_info=True)
        except Exception as e:
            worker_logger.warning("auto report %s failed: %s", kind, str(e)[:200])


app = create_app()
