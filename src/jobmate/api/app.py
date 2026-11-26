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
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import BackgroundTasks, FastAPI, Request, Response
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
)
from fastapi.staticfiles import StaticFiles

from .. import mailer, messenger
from ..config import (
    Settings,
    configure_langsmith,
    configure_logging,
    database_dsn,
    get_settings,
)
from ..engine.trace import graph_config
from ..llm import CompositeLLM, MeteredLLM
from ..models import RunStatus
from ..pricing import cost_usd
from ..store.db import Store
from ..workflows import WORKFLOWS
from ..workflows.registry_builder import build_runner
from .jobs import (
    _automation_loop,
    _execute_run_job,
)
from .schemas import (
    TriggerCreateRequest,
)
from .support import (
    _REAPER_STATE,
    _acquire_run_slot,
    _default_adapters,
    _last_cron_tick_at,
    _run_slots,
    _sync_workflow_registry,
)

STATIC_DIR = Path(__file__).parent / "static"

logger = logging.getLogger("jobmate.api")
worker_logger = logging.getLogger("jobmate.worker")


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
    # custom_workflows 테이블 → WORKFLOWS 동기화 (재시작 후에도 /runs에서 해석 가능)
    _sync_workflow_registry(store)
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

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # React 프론트엔드(v2) — 저장소 루트 v2/dist 빌드 산출물. 존재할 때만 마운트.
    _v2_dist = STATIC_DIR.parents[3] / "v2" / "dist"
    if _v2_dist.is_dir():
        app.mount("/app", StaticFiles(directory=_v2_dist, html=True), name="app_v2")

    def err(
        status: int,
        code: str,
        message: str,
        request: Request,
        extra: dict[str, Any] | None = None,
    ) -> JSONResponse:
        # request_id는 access_log_middleware가 심은 값을 재사용 — 별도 uuid를
        # 만들면 접근 로그의 rid와 응답의 request_id가 어긋난다.
        rid = (
            getattr(request.state, "request_id", "")
            or request.headers.get("x-request-id", "")
            or uuid.uuid4().hex[:12]
        )
        content: dict[str, Any] = {"error": {"code": code, "message": message, "request_id": rid}}
        # 계약상 최상위 추가 필드(current_revision 등) — error 객체 밖에 둔다.
        if extra:
            content.update(extra)
        return JSONResponse(status_code=status, content=content)

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
            "project": run.project,
            "agent_id": run.agent_id,
            "goal_id": run.goal_id,
            "coalesced_count": store.coalesced_counts_for_runs([run_id]).get(run_id, 0),
            "stages": [s.model_dump(mode="json") for s in store.get_stage_runs(run_id)],
            "approvals": [a.model_dump(mode="json") for a in store.get_approvals(run_id)],
            "audit": [a.model_dump(mode="json") for a in store.get_audit(run_id)],
            "wakeups": [
                {
                    "id": w["id"],
                    "source": w["source"],
                    "status": w["status"],
                    "coalesced_count": w["coalesced_count"],
                    "created_at": w["created_at"],
                }
                for w in store.list_wakeups_for_run(run_id)
            ],
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
        created_by: str = "",
    ) -> str:
        # wakeup coalescing — 동일 (workflow,input) 활성 run이 있으면
        # 새 run 없이 기존 run id를 반환한다(호출자는 job을 예약하지 않음).
        from .. import budget_scope
        from .. import wakeups as _wakeups

        # 스코프드 예산 — POST /runs·스케줄·트리거와 동일하게 admit 전에
        # hard_stop 초과를 검사한다. 차단 시 run을 만들지 않고 ""를 반환
        # (호출자: 메신저 start는 빈 id를 거절 메시지로 처리).
        project = str((ctx or {}).get("project") or "")
        blocks = budget_scope.evaluate_admission(store, workflow, project)
        if blocks:
            store.audit(
                "",
                created_by or "api",
                "run_budget_exceeded",
                workflow,
                {"blocks": blocks[:5], "source": "submit_run"},
            )
            logger.warning(
                "submit_run rejected: budget exceeded workflow=%s scope=%s",
                workflow,
                blocks[0]["scope"],
            )
            return ""
        # ctx.project는 예산 평가에만 쓰고 run input에는 기록되지 않아
        # project 스코프 지출 귀속에서 빠졌다 — input에도 동일하게 넣는다.
        input_data: dict[str, Any] = {"task": task}
        if project:
            input_data["project"] = project
        res = _wakeups.admit_run(
            store,
            workflow_key=workflow,
            input_data=input_data,
            source="api",
            created_by=created_by,
            version=WORKFLOWS[workflow].version,
            job_payload={"task": task},
        )
        if res["action"] == "invalid_goal":
            # input/ctx의 goal_id가 무효 — run 없이 거절(호출자는 ""로 처리)
            logger.warning(
                "submit_run rejected: invalid goal workflow=%s error=%s",
                workflow,
                res.get("error"),
            )
            return ""
        if res["action"] != "created":
            return str(res.get("run_id") or "")
        run = res["run"]
        job = (run.id, workflow, {"task": task}, ctx)
        if defer is not None:
            defer.append(job)
        else:
            threading.Thread(target=execute_run, args=job, daemon=True).start()
        return run.id

    def _session_user(request: Request) -> dict[str, Any] | None:
        """세션 쿠키 → 유효 사용자. 만료/무효 시 None."""
        from ..security import SESSION_COOKIE, hash_token

        token = request.cookies.get(SESSION_COOKIE, "")
        if not token:
            return None
        user = store.get_session_user(hash_token(token))
        return user if user and email_allowed(user["email"]) else None

    def _normalize_role(role: Any) -> str:
        """role 정규화 — member|lead|admin. 레거시 'user'는 member로 취급."""
        r = str(role or "").strip().lower()
        if r == "user":
            r = "member"
        return r if r in ("member", "lead", "admin") else "member"

    def _effective_role(user: dict[str, Any], role_override: str | None = None) -> str:
        """저장된 role(정규화) + JOBMATE_ADMIN_EMAILS/LEAD_EMAILS env 오버라이드.

        env admin > 저장 admin > env lead > 저장 lead > member 순."""
        from ..security import admin_emails, lead_emails

        stored = _normalize_role(role_override if role_override is not None else user.get("role"))
        email = (user.get("email") or "").strip().lower()
        if stored == "admin" or email in admin_emails():
            return "admin"
        if stored == "lead" or email in lead_emails():
            return "lead"
        return "member"

    def _admin_user(request: Request) -> dict[str, Any] | None:
        """관리자 판정 — 세션 사용자 role=admin, 유효 API 키(머신), 또는
        인증이 비활성인 로컬 개발 모드(키·허용목록·VERCEL 모두 없음)일 때 허용."""
        from ..security import allowed_emails, api_key

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

    def _lead_user(request: Request) -> dict[str, Any] | None:
        """팀장(lead) 이상 판정 — 세션 role이 lead/admin이거나,
        유효 API 키(머신), 또는 인증 비활성 로컬 개발 모드일 때 허용."""
        from ..security import allowed_emails, api_key

        user = _session_user(request)
        if user is not None:
            role = _effective_role(user)
            if role in ("lead", "admin"):
                return {**user, "role": role}
            return None
        if api_key() and authorized(request):
            return {"id": "api-key", "email": "", "role": "admin"}
        if not api_key() and not allowed_emails() and not os.environ.get("VERCEL"):
            return {"id": "local", "email": "", "role": "admin"}
        return None

    def _lead_gate(request: Request) -> JSONResponse | None:
        """lead 이상이 아니면 403 응답, 아니면 None — 라우트 첫 줄에서 호출."""
        if _lead_user(request) is None:
            return err(403, "forbidden", "lead role required", request)
        return None

    def _explicit_lead_user(request: Request) -> dict[str, Any] | None:
        """'명시적' lead/admin — 세션 사용자 role 또는 인증 비활성 로컬
        개발 모드만 인정한다. 유효 API 키(머신)는 _lead_user에서는 admin으로
        취급되지만 여기서는 제외 — 머신 키 하나가 임의 task의 checkout을
        해제하지 못하게 holder 검사가 필요한 경로(release)에서 구분용."""
        from ..security import allowed_emails, api_key

        user = _session_user(request)
        if user is not None:
            role = _effective_role(user)
            if role in ("lead", "admin"):
                return {**user, "role": role}
            return None
        if not api_key() and not allowed_emails() and not os.environ.get("VERCEL"):
            return {"id": "local", "email": "", "role": "admin"}
        return None

    def _admin_gate(request: Request) -> JSONResponse | None:
        """admin이 아니면 403 응답, 아니면 None — 라우트 첫 줄에서 호출."""
        if _admin_user(request) is None:
            return err(403, "forbidden", "admin role required", request)
        return None

    # ---- 인메모리 레이트리밋 공통 ----
    # 주의: create_app() 호출마다 초기화되는 인메모리 카운터 — 프로세스/
    # 인스턴스 단위 제한이며 수평 확장 시에는 외부 저장소가 필요하다.
    _RATE_MAX_KEYS = 10_000

    def _rate_hit(bucket: dict[str, list[float]], key: str, window_s: float, max_hits: int) -> bool:
        """슬라이딩 윈도우 카운터. True면 제한 초과."""
        now = time.time()
        hits = [t for t in bucket.get(key, []) if now - t < window_s]
        if len(hits) >= max_hits:
            bucket[key] = hits
            return True
        hits.append(now)
        bucket[key] = hits
        if len(bucket) > _RATE_MAX_KEYS:
            # 상한 초과 시 만료 항목 정리, 그래도 크면 전체 클리어(메모리 보호)
            for k in [k for k, v in bucket.items() if not v or now - v[-1] >= window_s]:
                bucket.pop(k, None)
            if len(bucket) > _RATE_MAX_KEYS:
                bucket.clear()
        return False

    _run_burst: dict[str, list[float]] = {}

    def _run_rate_limited(actor: str) -> bool:
        """POST /runs 버스트 제한 — actor(이메일/API 키/local)당 분당 10회."""
        return _rate_hit(_run_burst, actor or "-", 60.0, 10)

    _digest_daily: dict[str, tuple[str, int]] = {}
    _DIGEST_DAILY_LIMIT = 30

    def _digest_rate_limited(request: Request) -> bool:
        """POST /gmail/digest — _ask_daily와 같은 사용자/IP당 일일 상한."""
        user = _session_user(request)
        key = (
            f"u:{user['id']}"
            if user and user.get("id")
            else (f"ip:{request.client.host if request.client else '-'}")
        )
        today = datetime.now(UTC).date().isoformat()
        day, count = _digest_daily.get(key, ("", 0))
        if day != today:
            day, count = today, 0
        if count >= _DIGEST_DAILY_LIMIT:
            _digest_daily[key] = (day, count)
            return True
        _digest_daily[key] = (day, count + 1)
        if len(_digest_daily) > _RATE_MAX_KEYS:
            _digest_daily.clear()
        return False

    _email_check_last = {"at": 0.0}
    _EMAIL_CHECK_MIN_INTERVAL = 60.0

    def _email_check_rate_limited() -> bool:
        """POST /email/check — 외부 cron이 분당 수 회 이상 호출하지 못하게
        최소 간격을 둔다(인메모리, 인스턴스 단위)."""
        now = time.time()
        if now - _email_check_last["at"] < _EMAIL_CHECK_MIN_INTERVAL:
            return True
        _email_check_last["at"] = now
        return False

    # ---- 월간 LLM 예산 (POST /runs 집행용) ----
    _budget_cache = {"at": 0.0, "mtd": 0.0}
    _BUDGET_CACHE_TTL_S = 60.0

    def _monthly_budget_usd() -> float | None:
        """prefs.monthly_budget_usd — 미설정/파싱 실패 시 None(무제한)."""
        try:
            raw = _prefs().get("monthly_budget_usd")
            return float(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None

    def _mtd_llm_cost_usd() -> float:
        """월초(UTC) 이후 llm_usage 추정 비용 합계 — 60초 인메모리 캐시.

        usage/summary와 동일하게 cost_usd(모델별 토큰 단가)로 계산한다."""
        now = time.time()
        if now - _budget_cache["at"] < _BUDGET_CACHE_TTL_S:
            return float(_budget_cache["mtd"])
        month_start = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        mtd = 0.0
        for r in store.llm_usage_rows(month_start.isoformat()):
            mtd += cost_usd(
                str(r.get("model") or ""),
                int(r.get("tokens_in") or 0),
                int(r.get("tokens_out") or 0),
            )
        _budget_cache["at"] = now
        _budget_cache["mtd"] = mtd
        return mtd

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

    # ---- SSE 실시간 이벤트 스트림 (/api/v1/stream) ----
    # 인증은 /api/* 미들웨어와 동일 — EventSource는 헤더를 못 쓰므로
    # 세션 쿠키(동일 출처) 또는 단기 SSE 토큰(?token=)을 사용한다.
    # 토큰은 POST /api/v1/stream/token으로 발급(헤더/쿠키 인증 필요).
    _SSE_MAX_CONNECTIONS = 20
    _SSE_POLL_SECONDS = 1.5
    _SSE_HEARTBEAT_LOOPS = 10  # ~15초마다 주석 하트비트
    _sse = {"count": 0}
    _sse_lock = threading.Lock()

    def _sse_snapshot() -> dict[str, Any]:
        return {
            "runs": [
                {
                    "id": r.id,
                    "workflow": r.workflow_key,
                    "status": r.status.value,
                    "project": r.project,
                    "created_at": r.created_at.isoformat(),
                    "updated_at": r.updated_at.isoformat(),
                }
                for r in store.list_runs(30)
            ]
        }

    def _do_approve(
        run_id: str,
        decision: str,
        comment: str,
        actor: str = "api",
        background_tasks: BackgroundTasks | None = None,
    ) -> dict[str, Any]:
        """승인/거절 공통 경로 — API 엔드포인트와 메신저 커맨드가 공유.
        반환: {"ok": True, "run": detail} 또는 {"ok": False, "status": int, "error": str}."""
        run = store.get_run(run_id)
        if run is None:
            return {"ok": False, "status": 404, "error": "run not found"}
        if run.status != RunStatus.WAITING_APPROVAL:
            return {
                "ok": False,
                "status": 409,
                "error": f"run is {run.status.value}, expected waiting_approval",
            }
        # 동시 run 세마포어 — resume의 graph.invoke도 LLM을 태우므로
        # 신규 run과 같은 슬롯을 공유한다. claim 전에 획득해 슬롯 대기 중
        # running으로 고착된 claim이 생기지 않게 한다.
        if not _acquire_run_slot(run_id):
            return {"ok": False, "status": 503, "error": "run concurrency limit reached"}
        try:
            # 원자 claim — 동시 승인/거절(API+메신저 등)이 둘 다 resume하지 못하게
            # status를 running으로 조건부 전이한다. rowcount 0이면 이미 처리 중.
            if not store.claim_run_status(
                run_id, RunStatus.WAITING_APPROVAL.value, RunStatus.RUNNING.value
            ):
                return {
                    "ok": False,
                    "status": 409,
                    "error": "run is already being resumed by another request",
                }
            try:
                # build_runner 실패도 claim을 running에 고착시키지 않도록 try 안에서 호출
                runner = build_runner(
                    run.workflow_key,
                    store=store,
                    llm=llm,
                    settings=settings,
                    adapters=adapters,
                )
                # runner.resume()는 status==waiting_approval을 요구하지만 claim이 이미
                # running으로 전이했으므로, 동등한 graph 재개를 직접 수행한다
                # (claim 자체가 직렬화 지점 — resume()의 상태 재검사는 이중 방어).
                from langgraph.types import Command

                runner._graph.invoke(  # noqa: SLF001 — resume()의 claim-우회 경로
                    Command(resume={"decision": decision, "comment": comment}),
                    graph_config(run_id, workflow=run.workflow_key, run_id=run_id),
                )
            except Exception as e:
                logger.warning("run %s resume failed: %s", run_id, str(e)[:200])
                # resume 실패 시 대기 상태로 복구 — claim이 running으로 바꿔둔 것을
                # 되돌려 재시도/재승인이 가능하게 한다(이미 다른 상태면 그대로 둠).
                store.claim_run_status(
                    run_id, RunStatus.RUNNING.value, RunStatus.WAITING_APPROVAL.value
                )
                store.audit(run_id, actor, "run_resume_error", run_id, {"error": str(e)[:300]})
                return {"ok": False, "status": 500, "error": str(e)[:300]}
        finally:
            _run_slots().release()
        store.mark_inbox_read_dedupe(f"approval:{run_id}")
        store.audit(run_id, actor, f"run_{decision}", run_id, {"comment": comment[:200]})
        logger.info("run %s resume decision=%s actor=%s", run_id, decision, actor)
        # resume이 run을 종결시켰으면 wakeup 마감 + deferred promote
        try:
            from .. import wakeups as _wakeups

            def _sched_promoted(new_run: Any, job_payload: dict[str, Any]) -> None:
                if os.environ.get("VERCEL") and background_tasks is not None:
                    # 서버리스 — 응답 후 thread는 freeze되므로 background
                    # task로 실행해 함수 수명을 연장한다.
                    background_tasks.add_task(
                        execute_run, new_run.id, new_run.workflow_key, job_payload, None
                    )
                    return
                threading.Thread(
                    target=execute_run,
                    args=(new_run.id, new_run.workflow_key, job_payload, None),
                    daemon=True,
                ).start()

            _wakeups.on_run_finished(store, run_id, schedule=_sched_promoted)
        except Exception:
            logger.warning("wakeup finalize failed run=%s", run_id, exc_info=True)
        # resume이 run을 종결/재대기시켰으면 관리 task/agent 상태도 투영
        try:
            from ..services import agent_management as _mgmt

            _mgmt.project_run_status(store, run_id)
        except Exception:
            logger.warning("management projection failed run=%s", run_id, exc_info=True)
        return {"ok": True, "run": run_detail(run_id) or {}}

    # ---- workflow schedules (주기적 run 자동 생성) ----

    # ---- 통합 인박스 (알림 센터) ----
    # notifications 테이블은 별도 유지 — GET /inbox 호출 시 dedupe_key=notif:{id}로
    # inbox_items에 지연 미러링한다(멱등). 나머지 kind는 이벤트 발생 시 직접 insert.

    # ---- 비용 대시보드 ----

    # ---- 자연어 쿼리 (ask) ----
    _ask_daily: dict[str, tuple[str, int]] = {}
    _ASK_DAILY_LIMIT = 20
    _ASK_TIMEOUT_S = 30.0

    def _ask_key(request: Request) -> str:
        user = _session_user(request)
        if user and user.get("id"):
            return f"u:{user['id']}"
        return f"ip:{request.client.host if request.client else '-'}"

    # ---- 리포트 ----

    _REPORT_KINDS = ("daily", "weekly", "exec_daily", "exec_weekly")

    # ---- PWA 뱃지 ----

    # ---- 멀티유저 역할 ----

    # ---- agent-sessions: 로컬 에이전트 브릿지 push/pull ----
    def _push_attention(key: str, title: str, body: str) -> None:
        """어텐션 웹 푸시 — 키(세션/런)당 10분 레이트리밋.
        타임스탬프는 DB state에 저장 — 서버리스 인스턴스마다 인메모리가
        초기화돼 레이트리밋이 무력화되는 것을 막는다."""
        now = time.time()
        ts_map = store.get_state("attention_push_ts", {})
        if not isinstance(ts_map, dict):
            ts_map = {}
        if now - float(ts_map.get(key) or 0.0) < 600:
            return
        # 만료 항목 정리, 그래도 크면 전체 클리어(스토리지 보호)
        ts_map = {k: v for k, v in ts_map.items() if now - float(v or 0.0) < 600}
        if len(ts_map) > 1000:
            ts_map.clear()
        ts_map[key] = now
        store.set_state("attention_push_ts", ts_map)
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

    # ---- 프로젝트 마스터 + 태깅 (lead 이상) ----

    def _lead_or_403(request: Request) -> Any:
        """lead 이상이면 사용자 dict, 아니면 403 JSONResponse."""
        lead = _lead_user(request)
        if lead is None:
            return err(403, "forbidden", "lead role required", request)
        return lead

    # ---- 리드 대시보드: 리스크 / 승인 큐 / 성과 비교 ----

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

    _slack_reply_warned = {"done": False}

    def _messenger_reply(provider: str, chat_id: str, text: str) -> None:
        """수신 채널로 답장 — best-effort, 실패는 debug 로그만.

        Slack은 JOBMATE_SLACK_BOT_TOKEN+xoxb 채널 chat.postMessage를 우선 사용
        (webhook은 채널 고정이라 임의 chat_id로 답장 불가). 봇 토큰이 없거나
        실패하면 incoming webhook으로 폴백한다."""
        try:
            if provider == "slack":
                bot_token = os.environ.get("JOBMATE_SLACK_BOT_TOKEN", "")
                if bot_token and chat_id:
                    try:
                        import httpx as _httpx

                        resp = _httpx.post(
                            "https://slack.com/api/chat.postMessage",
                            headers={"Authorization": f"Bearer {bot_token}"},
                            json={"channel": chat_id, "text": text[:4000]},
                            timeout=10,
                        )
                        data = resp.json() if resp.content else {}
                        if data.get("ok"):
                            return
                        logger.warning(
                            "slack chat.postMessage failed: %s",
                            str(data.get("error") or resp.status_code)[:120],
                        )
                    except Exception as e:
                        logger.warning("slack bot reply failed: %s", type(e).__name__)
                url = os.environ.get("JOBMATE_SLACK_WEBHOOK_URL", "")
                if url:
                    messenger.send_slack(url, text)
                elif not _slack_reply_warned["done"]:
                    _slack_reply_warned["done"] = True
                    logger.warning(
                        "slack reply undeliverable: set JOBMATE_SLACK_BOT_TOKEN "
                        "or JOBMATE_SLACK_WEBHOOK_URL"
                    )
            elif provider == "telegram":
                token = os.environ.get("JOBMATE_TELEGRAM_BOT_TOKEN", "")
                if token:
                    messenger.send_telegram(token, chat_id, text)
        except Exception:
            logger.debug("messenger reply failed provider=%s", provider, exc_info=True)

    def _messenger_user_allowed(sender_id: str, action: str) -> bool:
        """JOBMATE_MESSENGER_ALLOWED_USERS per-user allowlist.

        쉼표 구분 Slack user ID(U…)/Telegram user ID. 설정 시 승인/시작
        같은 상태 변경 커맨드의 실행자 ID가 목록에 없으면 거부한다.
        미설정 시 채널 allowlist(settings.slack_channel_ids 등)만 적용
        (하위호환). help/status는 읽기 전용이라 제한하지 않는다."""
        if action not in ("approve", "start"):
            return True
        raw = os.environ.get("JOBMATE_MESSENGER_ALLOWED_USERS", "")
        allowed = {s.strip() for s in raw.split(",") if s.strip()}
        if not allowed:
            return True
        if sender_id and sender_id in allowed:
            return True
        logger.warning("messenger %s denied: sender not in JOBMATE_MESSENGER_ALLOWED_USERS", action)
        return False

    def _messenger_handle(
        provider: str,
        chat_id: str,
        text: str,
        background_tasks: BackgroundTasks,
        sender_id: str = "",
    ) -> bool:
        """수신 텍스트를 커맨드로 파싱·실행 후 답장. 인식 못 하면 False."""
        cmd = messenger.parse_command(text)
        if cmd is None:
            return False
        action = str(cmd.get("action") or "")
        if not _messenger_user_allowed(sender_id, action):
            _messenger_reply(provider, chat_id, "이 커맨드를 실행할 권한이 없습니다.")
            return True
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
            res = _do_approve(
                target,
                "approve",
                "messenger",
                actor="messenger",
                background_tasks=background_tasks,
            )
            if res["ok"]:
                _messenger_reply(provider, chat_id, f"run {target}을(를) 승인했습니다.")
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
            if not rid:
                # 스코프드 예산 hard_stop 등으로 run 생성이 거부됨
                _messenger_reply(provider, chat_id, "예산 한도 초과로 run 생성을 거부했습니다.")
                return True
            _schedule_run_jobs(jobs, background_tasks)
            _messenger_reply(provider, chat_id, f"run 생성: {rid} ({wf})")
            logger.info("messenger start run=%s workflow=%s provider=%s", rid, wf, provider)
        return True

    # ---- calendar-agent: 멀티채널 웹훅·수신함·이벤트·알림예약 ----

    def _cron_authorized(request: Request) -> bool:
        secret = os.environ.get("CRON_SECRET") or os.environ.get("JOBMATE_CRON_SECRET", "")
        if not secret:
            return True
        # 쿼리스트링 ?secret= 은 제거 — URL이 로그/프록시/브라우저 히스토리에
        # 남아 유출되므로 Authorization: Bearer 헤더만 허용한다.
        if request.headers.get("authorization") == f"Bearer {secret}":
            return True
        # cron-job.org 등 API 키 인증 경로 — 미들웨어에서 이미 검증된 키만 도달

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

    # ---------- settings (대시보드 설정) ----------
    PREFS_KEY = "prefs"

    def _prefs() -> dict[str, Any]:
        return store.get_state(PREFS_KEY, {})

    # ---- skillRadar ----

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

    # ---- Web Push 구독 ----

    # ---- 계정 인증 (이메일+비밀번호 / Google OAuth) ----

    from ..security import (
        SESSION_COOKIE,
        SESSION_TTL_DAYS,
        allowed_emails,
        api_key,
        authorized,
        email_allowed,
        hash_password,
        hash_token,
        new_session_token,
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
        """첫 번째 가입 사용자는 자동 admin — 이후 관리자는 role/env로 지정.

        count 조회 후 판정하는 비원자 경로는 동시 가입 레이스에서 둘 다
        admin이 될 수 있다 — store의 조건부 단일 UPDATE로 원자화한다."""
        try:
            if store.promote_to_admin_if_first(user["id"]):
                user["role"] = "admin"  # 응답용 dict도 동기화 (조회 시점 이후 승격)
                store.audit(
                    "",
                    "system",
                    "user_role",
                    user["id"],
                    {"role": "admin", "reason": "first_user"},
                )
        except Exception as e:
            logger.warning("first-user admin promotion failed: %s", str(e)[:200])

    def _cookie_secure(request: Request) -> bool:
        """세션 쿠키 Secure 판정.

        request.url.scheme만 보면 프록시(Vercel 등) 뒤에서 http로 보여
        Secure가 꺼진다. VERCEL 배포이거나 x-forwarded-proto/직접 scheme이
        https일 때만 Secure를 켠다 — plain http에 Secure를 붙이면 브라우저가
        쿠키를 저장하지 않아 로그인이 깨진다."""
        if os.environ.get("VERCEL"):
            return True
        if request.url.scheme == "https":
            return True
        proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
        return proto == "https"

    def _set_session(response: Any, request: Request, user_id: str) -> None:
        token = new_session_token()
        store.create_session(user_id, hash_token(token), SESSION_TTL_DAYS)
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=SESSION_TTL_DAYS * 86400,
            httponly=True,
            samesite="lax",
            secure=_cookie_secure(request),
            path="/",
        )

    def _login_key(request: Request, email: str) -> str:
        ip = request.client.host if request.client else "-"
        return f"{ip}:{email.strip().lower()}"

    def _prune_fail_bucket(bucket: dict[str, list[float]], window_s: float) -> None:
        """인메모리 실패 카운터 정리 — 만료 버킷 삭제 + 상한 초과 시 클리어."""
        now = time.time()
        for k in [k for k, v in bucket.items() if not v or now - v[-1] >= window_s]:
            bucket.pop(k, None)
        if len(bucket) > _RATE_MAX_KEYS:
            bucket.clear()

    def _login_throttled(key: str) -> bool:
        now = time.time()
        fails = [t for t in _login_fails.get(key, []) if now - t < _LOGIN_WINDOW]
        if fails:
            _login_fails[key] = fails
        else:
            _login_fails.pop(key, None)  # 만료 항목은 삭제해 dict가 무한 성장하지 않게
        return len(fails) >= _LOGIN_MAX_FAILS

    def _login_record(key: str, ok: bool) -> None:
        if ok:
            _login_fails.pop(key, None)
        else:
            _login_fails.setdefault(key, []).append(time.time())
            if len(_login_fails) > _RATE_MAX_KEYS:
                _prune_fail_bucket(_login_fails, _LOGIN_WINDOW)

    # signup은 login과 카운터를 공유하지 않는 별도 IP 기반 throttle
    _signup_attempts: dict[str, list[float]] = {}
    _SIGNUP_WINDOW = 600.0
    _SIGNUP_MAX_ATTEMPTS = 5

    def _signup_throttled(ip: str) -> bool:
        now = time.time()
        attempts = [t for t in _signup_attempts.get(ip, []) if now - t < _SIGNUP_WINDOW]
        if attempts:
            _signup_attempts[ip] = attempts
        else:
            _signup_attempts.pop(ip, None)
        return len(attempts) >= _SIGNUP_MAX_ATTEMPTS

    def _signup_record(ip: str) -> None:
        _signup_attempts.setdefault(ip, []).append(time.time())
        if len(_signup_attempts) > _RATE_MAX_KEYS:
            _prune_fail_bucket(_signup_attempts, _SIGNUP_WINDOW)

    # ---- 이메일 인증 (비밀번호 가입) ----
    _VERIFY_RESEND_MIN_INTERVAL = 600.0  # 같은 이메일 재발송 10분에 1회

    def _email_verification_required() -> bool:
        """이메일 인증 강제 여부 — 로컬 개발 모드(인증 비활성: api_key·
        allowed_emails·VERCEL 모두 없음)만 면제한다. VERCEL/키/허용목록이
        있으면 항상 적용."""
        return bool(api_key() or allowed_emails() or os.environ.get("VERCEL"))

    def _verify_url(request: Request, token: str) -> str:
        base = (settings.public_base_url or str(request.base_url)).rstrip("/")
        return f"{base}/api/v1/auth/verify?token={token}"

    def _public_host_allowed(request: Request) -> bool:
        """JOBMATE_PUBLIC_HOSTS(쉼표 구분 허용 호스트) 설정 시 요청 Host가
        목록에 없으면 인증 링크 발송을 거부한다 — Host 헤더 인젝션으로
        공격자 도메인의 피싱 인증 링크가 발송되는 것을 막는다.
        미설정 시 기존 동작(request.base_url 사용)을 유지한다."""
        raw = os.environ.get("JOBMATE_PUBLIC_HOSTS", "")
        allowed = {h.strip().lower() for h in raw.split(",") if h.strip()}
        if not allowed:
            return True
        host = (request.url.hostname or "").lower()
        # "example.com:8443" 형태 방어 — hostname은 이미 포트 제외
        return host in allowed

    def _send_verification(request: Request, email: str) -> bool:
        """인증 토큰 발급 + 메일 발송. 발송 실패해도 예외 없이 bool 반환."""
        if not _public_host_allowed(request):
            # 허용 목록에 없는 Host로의 요청 — 링크가 공격자 도메인을 가리키게 된다
            logger.warning("verification email refused: host not in JOBMATE_PUBLIC_HOSTS")
            try:
                store.audit("", email, "auth_verify_send_blocked", "", {})
            except Exception:
                logger.debug("verify-send-blocked audit failed", exc_info=True)
            return False
        try:
            token = store.create_email_verification(email)
            return mailer.send_verification_email(
                email, _verify_url(request, token), gmail=adapters.get("gmail")
            )
        except Exception as e:
            logger.warning("verification email setup failed: %s", str(e)[:200])
            return False

    _VERIFY_OK_HTML = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<title>이메일 인증 완료</title></head>
<body style="font-family:sans-serif;display:flex;align-items:center;
justify-content:center;min-height:100vh;margin:0;background:#f6f7f9">
<div style="background:#fff;border:1px solid #e3e6ea;border-radius:12px;
padding:32px;max-width:360px;text-align:center">
<h2 style="margin:0 0 8px">이메일 인증 완료</h2>
<p style="color:#555;margin:0">인증이 완료되었습니다. 잠시 후 로그인됩니다.</p>
</div></body></html>"""

    _VERIFY_FAIL_HTML = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<title>이메일 인증 실패</title></head>
<body style="font-family:sans-serif;display:flex;align-items:center;
justify-content:center;min-height:100vh;margin:0;background:#f6f7f9">
<div style="background:#fff;border:1px solid #e3e6ea;border-radius:12px;
padding:32px;max-width:360px;text-align:center">
<h2 style="margin:0 0 8px">이메일 인증 실패</h2>
<p style="color:#555;margin:0">링크가 만료되었거나 이미 사용되었습니다.
로그인 화면에서 인증 메일을 다시 요청하세요.</p>
</div></body></html>"""

    _dummy_pw_hash: dict[str, str] = {}

    def _dummy_password_hash() -> str:
        """타이밍 오라클 방지용 고정 scrypt 해시 — 실제 계정 검증에 쓰이지 않는다.
        첫 호출 시 1회만 생성해 scrypt 비용을 캐시한다."""
        if "h" not in _dummy_pw_hash:
            _dummy_pw_hash["h"] = hash_password("jobmate-dummy-timing-oracle")
        return _dummy_pw_hash["h"]

    def _google_oauth_env(suffix: str) -> str:
        """Google 로그인 OAuth 설정 — GOOGLE_OAUTH_<suffix> 우선,
        레거시 GOOGLE_<suffix>(GOOGLE_CLIENT_ID 등) 폴백."""
        return (
            os.environ.get(f"GOOGLE_OAUTH_{suffix}", "").strip()
            or os.environ.get(f"GOOGLE_{suffix}", "").strip()
        )

    def _google_redirect_uri(request: Request) -> str:
        return _google_oauth_env("REDIRECT_URI") or (
            str(request.base_url).rstrip("/") + "/api/v1/auth/google/callback"
        )

    # ---- 사이드바 뷰 집계 (읽기 전용) — 인증은 /api/* 미들웨어와 동일 ----

    # ==================== 대시보드 확장 기능 (playbooks/comments/connectors/
    # custom workflows/triggers/team/audit/export/share/cards) ====================

    def _actor_label(request: Request) -> str:
        """run.created_by / audit actor용 행위자 라벨 — 세션 email 우선."""
        from ..security import api_key

        user = _session_user(request)
        if user is not None:
            return str(user.get("email") or user.get("id") or "user")
        if api_key() and authorized(request):
            return "api-key"
        return "local"

    def _audit_api(
        request: Request, action: str, target: str = "", detail: dict[str, Any] | None = None
    ) -> None:
        """관리자/비즈니스 감사 이벤트 — detail에 비밀값 금지(호출자 책임)."""
        try:
            store.audit("", _actor_label(request), action, target, detail or {})
        except Exception:
            logger.debug("audit emit failed action=%s", action, exc_info=True)

    # ---- 1. PLAYBOOKS (플레이북 뷰는 lead 전용 — 목록 포함 전체 lead) ----

    # ---- 2. RUN COMMENTS ----

    # ---- 3. CONNECTORS (커넥터 뷰는 lead 전용 — config에 비밀값 포함) ----
    _CONNECTOR_TYPES = {"github", "jira", "notion", "linear", "webhook", "json"}

    # ---- 4. CUSTOM WORKFLOWS (빌더 뷰 lead 전용) ----

    # ---- 5. TRIGGERS (트리거 뷰 lead 전용 — run 자동 발사 설정) ----
    _TRIGGER_MATCH_KEYS = {"tool", "project", "state"}
    _TRIGGER_STATES = {"working", "decision", "rest", "ended"}
    _TRIGGER_ACTION_TYPES = {"run_workflow", "run_playbook", "report"}

    def _validate_trigger(body: TriggerCreateRequest, request: Request) -> JSONResponse | None:
        match = body.match or {}
        action = body.action or {}
        if not isinstance(match, dict) or not isinstance(action, dict):
            return err(422, "validation", "match/action must be objects", request)
        unknown = set(match) - _TRIGGER_MATCH_KEYS
        if unknown:
            return err(422, "validation", f"unknown match keys: {sorted(unknown)}", request)
        state = match.get("state")
        if state is not None and str(state) not in _TRIGGER_STATES:
            return err(
                422,
                "validation",
                f"state must be one of {sorted(_TRIGGER_STATES)}",
                request,
            )
        for k in ("tool", "project"):
            if match.get(k) is not None and not isinstance(match[k], str):
                return err(422, "validation", f"match.{k} must be a string", request)
        atype = str(action.get("type") or "")
        if atype not in _TRIGGER_ACTION_TYPES:
            return err(
                422,
                "validation",
                f"action.type must be one of {sorted(_TRIGGER_ACTION_TYPES)}",
                request,
            )
        if atype == "run_workflow" and str(action.get("workflow") or "") not in WORKFLOWS:
            return err(404, "unknown_workflow", "action.workflow not found", request)
        if (
            atype == "run_playbook"
            and store.get_playbook(str(action.get("playbook_id") or "")) is None
        ):
            return err(404, "not_found", "action.playbook_id not found", request)
        if atype == "report":
            rk = str(action.get("report_kind") or "daily")
            if rk not in ("daily", "weekly", "exec_daily", "exec_weekly"):
                return err(
                    422,
                    "validation",
                    "report_kind must be daily|weekly|exec_daily|exec_weekly",
                    request,
                )
        return None

    # ---- 6. TEAM VIEW (팀 뷰 lead 전용) ----

    # ---- 7. AUDIT LOG VIEW (admin only) ----

    # ---- 8. EXPORT & SHARE ----

    # ---- 9. CUSTOM DASHBOARD CARDS (inbox 뷰에서 조회하므로 GET은 무인증, 쓰기는 lead) ----

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
                )
            )
        ):
            # 공개 공유 스냅샷 — GET만 인증 예외(POST /share는 세션 필요)
            public_share = path.startswith("/api/v1/share/") and request.method == "GET"
            if not public_share and not (authorized(request) or _session_user(request)):
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
        return err(500, "internal_error", "internal server error", request)

    from types import SimpleNamespace

    from .routers import (
        agents as _r_agents,
    )
    from .routers import (
        auth as _r_auth,
    )
    from .routers import (
        budgets as _r_budgets,
    )
    from .routers import (
        builder as _r_builder,
    )
    from .routers import (
        evals as _r_evals,
    )
    from .routers import (
        events as _r_events,
    )
    from .routers import (
        goals as _r_goals,
    )
    from .routers import (
        management as _r_management,
    )
    from .routers import (
        messaging as _r_messaging,
    )
    from .routers import (
        misc as _r_misc,
    )
    from .routers import (
        ops as _r_ops,
    )
    from .routers import (
        push as _r_push,
    )
    from .routers import (
        radar as _r_radar,
    )
    from .routers import (
        runs as _r_runs,
    )
    from .routers import (
        sessions as _r_sessions,
    )

    ctx = SimpleNamespace(**{k: v for k, v in locals().items() if k != "ctx"})
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
    return app


app = create_app()
