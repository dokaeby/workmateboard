"""app.py에서 분리한 automation worker 잡 함수들."""

from __future__ import annotations

import logging
import os
import threading
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .. import budget_scope, cost_ledger, messenger, wakeups
from ..config import Settings
from ..engine.trace import job_traceable
from ..models import RunStatus
from ..sensors import build_gmail_url, is_safe_source_url
from ..store import orgctx
from ..store.db import Store
from ..workflows import WORKFLOWS
from ..workflows.registry_builder import build_runner
from ..workflows.resolver import resolve_workflow_spec
from .support import (
    _acquire_run_slot,
    _mark_cron_tick,
    _reap_stale_runs,
    _retention_sweep,
    _run_slots,
)
from .triggers import _maybe_check_triggers

logger = logging.getLogger("jobmate.api")
worker_logger = logging.getLogger("jobmate.worker")


# run.status → 아웃바운드 웹훅 이벤트 타입 (spec 004 US1, FR-003).
# waiting_approval도 여기서 emit한다 — event_id에 updated_at 지문을 넣어
# _check_waiting_approvals의 재대기 감지와 (subscription_id,event_id)
# UNIQUE로 상호 멱등된다.
_RUN_WEBHOOK_EVENTS = {
    "completed": "run.completed",
    "failed": "run.failed",
    "needs_human": "run.needs_human",
    "waiting_approval": "run.waiting_approval",
}


def _emit_run_webhook(store: Store, run_id: str) -> None:
    """run 상태 → webhook 이벤트 emit — 실패가 run 경로를 깨지 않게 전부 삼킨다."""
    try:
        from ..services import webhooks as _webhooks

        run = store.get_run(run_id)
        if run is None:
            return
        status = str(getattr(run.status, "value", run.status) or "")
        event_type = _RUN_WEBHOOK_EVENTS.get(status)
        if event_type is None:
            return
        updated = run.updated_at.isoformat() if run.updated_at else ""
        data: dict[str, Any] = {
            "run_id": run.id,
            "workflow": run.workflow_key,
            "status": status,
        }
        if run.error:
            data["error"] = str(run.error)[:200]
        _webhooks.emit_event(store, event_type, f"run:{run.id}:{status}:{updated}", data)
    except Exception:
        worker_logger.warning("webhook emit failed run=%s", run_id, exc_info=True)


def _emit_run_alert_event(store: Store, run_id: str) -> None:
    """spec 006 US1 FR-003 — run 상태 → alert 규칙 평가 (비동기, non-blocking).

    실패가 run 경로를 깨지 않게 전부 삼킨다 — 실제 평가는 daemon thread가
    수행하고 (rule_id, event_fingerprint) dedup이 재평가 중복을 막는다."""
    try:
        from ..services import alert_rules as _alerts

        _alerts.emit_run_status_event_async(store, run_id)
    except Exception:
        worker_logger.warning("alert emit failed run=%s", run_id, exc_info=True)


# spec 006 US1 — alert sweep가 평가하는 run 상태 (종결 + 승인 대기).
_ALERT_SWEEP_STATUSES = {
    "completed",
    "failed",
    "rejected",
    "cancelled",
    "budget_exhausted",
    "needs_human",
    "waiting_approval",
}


def _alert_rule_sweep(store: Store) -> None:
    """alert 규칙 평가 백스톱 — 직접 emit을 놓친 경로(승인 resume·메신저
    커맨드·stale reaper)를 automation cycle에서 보완한다.

    updated_at 워터마크 이후 변경된 run/stage_run을 스캔해 emit 함수에
    넘긴다. fingerprint dedup이 중복 발송을 막으므로 직접 emit과 겹쳐도
    안전하다. 규칙이 하나도 없으면 조회 없이 종료한다."""
    try:
        rules = store.list_alert_rules(enabled_only=True)
    except Exception:
        return
    if not rules:
        return
    from ..services import alert_rules as _alerts

    now = datetime.now(UTC)
    wm_state = store.get_state("alert_sweep_ts", {})
    wm = str((wm_state or {}).get("ts") or "")
    try:
        since_dt = datetime.fromisoformat(wm) if wm else now - timedelta(minutes=15)
    except ValueError:
        since_dt = now - timedelta(minutes=15)
    # 워터마크-2분 오버랩 — 시계/커밋 경계 누락 방지. dedup이 중복을 걸러준다.
    since = (since_dt - timedelta(minutes=2)).isoformat()
    try:
        rows = store._conn.execute(  # noqa: SLF001 - 서비스 계층 store 내부 조회
            "SELECT id,status,updated_at FROM runs WHERE updated_at >= ?"
            " ORDER BY updated_at LIMIT 300",
            (since,),
        ).fetchall()
        for r in rows:
            if str(r["status"]) in _ALERT_SWEEP_STATUSES:
                _alerts.emit_run_status_event_async(store, str(r["id"]))
        srows = store._conn.execute(  # noqa: SLF001
            "SELECT id,run_id FROM stage_runs WHERE status='failed'"
            " AND finished_at >= ? ORDER BY finished_at LIMIT 300",
            (since,),
        ).fetchall()
        for r in srows:
            _alerts.emit_stage_failed_event(store, str(r["run_id"]), str(r["id"]))
        scan_ok = True
    except Exception:
        worker_logger.warning("alert rule sweep failed", exc_info=True)
        scan_ok = False
    if scan_ok:
        try:
            store.set_state("alert_sweep_ts", {"ts": now.isoformat()})
        except Exception:
            worker_logger.debug("alert sweep watermark update failed", exc_info=True)
    # spec 007 US4 — 주기 집계 규칙(error.rate_pct/session.stale_count/
    # cost.delta_pct)은 이벤트가 없는 조용한 시스템에서도 평가돼야 한다.
    # emit 경로가 없는 상태에서는 아무 emit도 호출되지 않으므로 스윕 끝에서
    # 무조건 평가한다 — evaluate_periodic_rules가 _PERIODIC_MIN_INTERVAL로
    # 스로틀하고 org_scope를 내부에서 재적용한다. 규칙이 있는 org별로 호출.
    try:
        org_ids = sorted({str(r.get("org_id") or "") for r in rules})
        for oid in org_ids:
            _alerts.evaluate_periodic_rules(store, org_id=oid)
    except Exception:
        worker_logger.warning("periodic alert eval failed", exc_info=True)


def _anomaly_sweep(store: Store) -> None:
    """spec 007 US7 — 비용/에러 급증 이상 탐지 (시간 게이트는 서비스 내부).

    실패가 automation cycle을 깨지 않게 전부 삼킨다."""
    try:
        from ..services import anomaly as _anomaly

        _anomaly.run_anomaly_sweep(store)
    except Exception as exc:
        worker_logger.warning("anomaly sweep failed", exc_info=True)
        try:
            from ..services import sentry as _sentry

            _sentry.capture_exception(exc, tags={"component": "anomaly"})
        except Exception:
            worker_logger.debug("sentry capture failed", exc_info=True)


def _process_budget_approvals(
    store: Store,
    settings: Settings,
    llm: Any,
    adapters: dict[str, Any],
    schedule: Any = None,
) -> None:
    """spec 006 US2 — budget 승인 대기 run의 TTL 만료 + 승인분 실행 발사.

    1. state=waiting + TTL 초과 → cancelled(FR-009).
    2. state=approved + status=queued → dispatched 마킹 후 execute_run 예약.
    schedule 콜백이 있으면(cron tick) BackgroundTasks로, 없으면 daemon
    thread로 실행한다 — _promote_deferred_runs의 _sched와 동일 규칙."""
    try:
        ttl_state = store.get_state("budget_approval_ttl", {})
        ttl_raw = (ttl_state or {}).get("hours", 72)
        try:
            ttl = float(ttl_raw)
        except (TypeError, ValueError):
            ttl = 72.0
    except (TypeError, ValueError):
        ttl = 72.0
    try:
        expired = store.expire_budget_approval_waits(ttl)
    except Exception:
        worker_logger.warning("budget approval TTL sweep failed", exc_info=True)
        expired = []
    for rid in expired:
        worker_logger.info("run %s budget approval expired ttl=%sh", rid, ttl)
        _emit_run_alert_event(store, rid)
    try:
        approved = store.list_budget_approval_approved()
    except Exception:
        worker_logger.warning("budget approval dispatch scan failed", exc_info=True)
        return
    for row in approved:
        try:
            run = store._row_to_run(row)  # noqa: SLF001 - 내부 조회
        except Exception:
            worker_logger.warning("budget approval row parse failed", exc_info=True)
            continue
        # approved→dispatched 원자 claim — loop/tick 중복 발사 방지
        try:
            if not store.mark_budget_approval_dispatched(run.id):
                continue
        except Exception:
            worker_logger.warning(
                "budget approval dispatch claim failed run=%s", run.id, exc_info=True
            )
            continue
        payload = {"task": str((run.input or {}).get("task") or "")}
        worker_logger.info("run %s budget approval dispatched", run.id)
        if schedule is not None:
            try:
                schedule(run, payload)
                continue
            except Exception:
                worker_logger.warning(
                    "budget approval schedule failed run=%s", run.id, exc_info=True
                )
        threading.Thread(
            target=_execute_run_job,
            args=(store, settings, llm, adapters, run.id, run.workflow_key, payload, None),
            daemon=True,
        ).start()


def _emit_event_webhook(store: Store, event_type: str, event_id: str, data: dict[str, Any]) -> None:
    """범용 webhook 이벤트 emit — 실패가 호출 경로를 깨지 않게 삼킨다."""
    try:
        from ..services import webhooks as _webhooks

        _webhooks.emit_event(store, event_type, event_id, data)
    except Exception:
        worker_logger.warning(
            "webhook emit failed event=%s id=%s", event_type, event_id, exc_info=True
        )


def _process_webhook_deliveries(store: Store, settings: Settings) -> None:
    """spec 004 US1 — due webhook delivery 배치 전송 (FR-008).

    건당 timeout(webhook_timeout_seconds)·배치 상한(webhook_batch)·
    구독당 in-flight 1건으로 사이클 지연이 제한된다. HTTP 전송은 빠른
    외부 호출이라 run job defer 큐를 타지 않고 사이클 안에서 처리한다
    (cron tick 경로에서도 동일하게 동작 — FR-036)."""
    try:
        from ..services import webhooks as _webhooks

        _webhooks.process_webhook_deliveries(store, settings)
    except Exception as exc:
        worker_logger.warning("webhook delivery cycle failed", exc_info=True)
        # spec 004 US4/FR-026 — Sentry 캡처(미설정 시 no-op).
        try:
            from ..services import sentry as _sentry

            _sentry.capture_exception(exc, tags={"component": "webhook_deliveries"})
        except Exception:
            worker_logger.debug("sentry capture failed", exc_info=True)


def _project_management(store: Store, run_id: str) -> None:
    """run→task/agent 상태 투영 (management 1차 E항).

    task_runs에 링크된 run의 상태 변경을 task/agent에 반영한다.
    투영 실패는 run 결과를 유실시키지 않는다 — 내부에서도 삼키지만
    import/호출 자체의 예외도 여기서 한 번 더 방어한다."""
    try:
        from ..services import agent_management as _mgmt

        _mgmt.project_run_status(store, run_id)
    except Exception:
        worker_logger.warning("management projection failed run=%s", run_id, exc_info=True)


class BudgetExceededError(RuntimeError):
    """스코프드 예산 hard_stop으로 run 생성이 차단됨.

    월간 지출 윈도우가 리셋되기 전까지 재시도해도 같은 결과이므로
    일시적 오류와 구분한다 — 메시지 intake 재시도 소진/ radar 사이클
    중단을 막기 위해 호출자가 이 타입을 잡아 terminal 처리한다."""


def _default_org_id(store: Store) -> str:
    """조직 컨텍스트가 없는 경로의 기본 조직 — 첫 활성 조직(spec 003 FR-015 백필 조직).

    gmail 폴링·radar·자동 리포트처럼 행 단위 org_id가 없는 전역 자동화가
    inbox/report/run을 만들 때 사용한다. 멀티 조직 배포에서는 이들 기능이
    기본 조직에 귀속된다는 점을 전제로 한다."""
    try:
        orgs = store.list_organizations()
    except Exception:
        return "__no_org__"
    # FR-031 — suspended 조직은 "활성"이 아니므로 건너뛴다. 활성 조직이
    # 하나도 없으면 '__no_org__' 센티널 — ''는 무필터 ctx라 쓰기가
    # org_id='' 레거시 행에 떨어지고 읽기는 전 조직을 노출한다.
    for o in orgs:
        if str(o.get("status") or "active") == "active":
            return str(o["id"])
    return "__no_org__"


def _org_inactive(store: Store, oid: str) -> bool:
    """org 행 상태 — suspended/deleted 조직의 자동화 run 생성 차단(FR-031).

    실제 판정은 Store.org_inactive 단일 경로 — 스케줄/루틴/메시지/트리거/
    wakeup promote 등 모든 자동화 경로가 같은 규칙을 공유한다."""
    return store.org_inactive(oid)


def _run_org_id(store: Store, run_id: str) -> str:
    """run 행의 org_id — org 필터와 무관하게 직접 조회한다.

    _execute_run_job/_promote_deferred_runs는 daemon thread·BackgroundTasks·
    cron tick 등 요청 컨텍스트가 없거나 다른 컨텍스트에서 호출되므로
    contextvar에 의존하지 않고 행에서 org를 읽어 org_scope로 재적용한다."""
    try:
        from ..services.management_common import row_org_id

        return row_org_id(store, "runs", run_id)
    except Exception:
        return ""


# ---------- automation worker ----------


def run_automation_cycle(
    store: Store,
    settings: Settings,
    llm: Any,
    adapters: dict[str, Any],
    *,
    promote_schedule: Any = None,
    flush_jobs: Any = None,
    watchdog_log: Any = worker_logger,
    watchdog_message: str = "watchdog cycle failed",
) -> None:
    """자동화 1회 사이클 — automation loop 스레드와 /api/v1/cron/tick의 공용 본체.

    스케줄러 진입점은 두 곳이다:
    1. _automation_loop — 비서버리스 환경의 상주 daemon thread
       (lifespan에서 시작, worker_interval_seconds 간격).
    2. POST/GET /api/v1/cron/tick — 서버리스(Vercel)에서 외부 cron이 호출하는
       HTTP 핸들러. 상주 스레드가 없으므로 이 경로가 유일한 구동 신호다.

    두 경로가 실행하는 _process_*/_maybe_* 단위 작업과 순서는 여기서 단일
    기준으로 유지한다. 차이는 run 실행 방식뿐이다:
    - loop: defer 없이 호출 → 각 단계가 daemon thread로 즉시 실행.
    - tick: flush_jobs(=BackgroundTasks 래퍼)와 promote_schedule을 넘겨
      run 실행과 deferred wakeup promote를 응답 후로 미룬다.
    """
    _reap_stale_runs(store, settings, llm, adapters, schedule=promote_schedule)
    _retention_sweep(store)
    _process_pending_messages(store, settings, llm, adapters)
    _process_due_notification_jobs(store, adapters)
    # schedules+routines는 하나의 defer 목록에 모아 한 번에 flush —
    # tick에서도 기존처럼 schedules 작업이 routines 작업보다 먼저 실행된다.
    jobs: list = []
    defer = jobs if flush_jobs is not None else None
    _process_due_schedules(store, settings, llm, adapters, defer=defer)
    _process_due_routines(store, settings, llm, adapters, defer=defer)
    if flush_jobs is not None:
        flush_jobs(jobs)
    _check_waiting_approvals(store, adapters)
    # spec 006 US1/US2 — alert 규칙 백스톱 스윕 + budget 승인 TTL/재개.
    # 직접 emit을 놓친 경로(승인 resume·메신저·reaper)를 보완하고,
    # require_approval 대기 run의 만료·승인분 실행을 처리한다.
    _alert_rule_sweep(store)
    # spec 007 US7 — 비용/에러 급증 이상 탐지 (1h 게이트, dedupe fingerprint).
    _anomaly_sweep(store)
    _process_budget_approvals(store, settings, llm, adapters, schedule=promote_schedule)
    # spec 004 US1 — due webhook delivery 배치 (FR-008). 서버리스 cron tick
    # 경로에서도 동일하게 동작해야 하므로 사이클 본체에 포함한다.
    _process_webhook_deliveries(store, settings)
    # spec 004 US3 — 백업 스케줄·복원 검증 (주기 게이트는 서비스 내부).
    try:
        from ..services import backup as _backup

        _backup.maybe_run_backup(store, settings)
        _backup.maybe_verify_backup(store, settings)
    except Exception as exc:
        worker_logger.warning("backup cycle failed", exc_info=True)
        # spec 004 US4/FR-026 — Sentry 캡처(미설정 시 no-op).
        try:
            from ..services import sentry as _sentry

            _sentry.capture_exception(exc, tags={"component": "backup"})
        except Exception:
            worker_logger.debug("sentry capture failed", exc_info=True)
    # spec 004 US2 — 헬스 체크 + 상태 전이 알림.
    try:
        from ..services import health as _health

        _health.run_health_cycle(store, settings)
    except Exception as exc:
        worker_logger.warning("health cycle failed", exc_info=True)
        # spec 004 US4/FR-026 — Sentry 캡처(미설정 시 no-op).
        try:
            from ..services import sentry as _sentry

            _sentry.capture_exception(exc, tags={"component": "health"})
        except Exception:
            worker_logger.debug("sentry capture failed", exc_info=True)
    # watchdog 1 cycle — spec 2차 E. 감지/복구/escalation은 내부에서
    # batch 상한(JOBMATE_WATCHDOG_BATCH)과 중복 방지를 적용한다.
    try:
        from ..services import watchdog as _watchdog

        _watchdog.run_cycle(store)
    except Exception as exc:
        watchdog_log.warning(watchdog_message, exc_info=True)
        try:
            from ..services import sentry as _sentry

            _sentry.capture_exception(exc, tags={"component": "watchdog"})
        except Exception:
            worker_logger.debug("sentry capture failed", exc_info=True)
    # 전역 자동화(gmail 폴링·radar·자동 리포트)는 행 단위 org_id가 없으므로
    # 기본 조직으로 스코프한다 — 생성되는 message/run/report/inbox가
    # default 조직에 귀속된다(spec 003 FR-019, FR-015 백필 조직).
    sys_org = orgctx.current_org_id() or _default_org_id(store)
    with orgctx.org_scope(sys_org):
        _maybe_poll_gmail(store, settings, adapters)
    radar_jobs: list = []
    with orgctx.org_scope(sys_org):
        _maybe_daily_radar(
            store,
            settings,
            llm,
            adapters,
            defer=radar_jobs if flush_jobs is not None else None,
            schedule=promote_schedule,
        )
    if flush_jobs is not None:
        flush_jobs(radar_jobs)
    trigger_jobs: list = []
    _maybe_check_triggers(
        store,
        settings,
        llm,
        adapters,
        defer=trigger_jobs if flush_jobs is not None else None,
    )
    if flush_jobs is not None:
        flush_jobs(trigger_jobs)
    with orgctx.org_scope(sys_org):
        _maybe_generate_reports(store, settings, adapters)


def _promote_deferred_runs(
    store: Store,
    settings: Settings,
    llm: Any,
    adapters: dict[str, Any],
    run_id: str,
    schedule: Any = None,
) -> None:
    """run 종결 후 wakeup 마감 + deferred promote — 실패해도 run 결과에 영향 없음.

    schedule(run, job_payload) 콜백이 주어지면 promote된 run의 실행을
    그쪽에 위임한다 — cron tick 같은 HTTP 핸들러는 FastAPI
    BackgroundTasks를 넘겨 서버리스 maxDuration 안에서 수 분짜리 run이
    요청 처리 중 동기 실행되지 않게 한다. 미지정 시 VERCEL에서는
    post-response 컨텍스트(background task/worker thread) 안에서만
    호출된다는 전제로 동기 실행하고, 그 외는 daemon thread로 실행한다."""
    # 조직 컨텍스트 재적용 — reaper/radar cleanup/cron tick 등 ctx=''
    # 경로에서 호출돼도 finishing run의 org로 wakeup 마감·deferred
    # promote(→ create_run)를 수행해 새 run이 같은 org를 물려받게 한다.
    oid = _run_org_id(store, run_id)
    if oid and orgctx.current_org_id() != oid:
        with orgctx.org_scope(oid):
            return _promote_deferred_runs(store, settings, llm, adapters, run_id, schedule=schedule)

    def _sched(new_run: Any, job_payload: dict[str, Any]) -> None:
        if schedule is not None:
            schedule(new_run, job_payload)
            return
        if os.environ.get("VERCEL"):
            # 서버리스 — 응답 후 daemon thread는 freeze돼 실행되지 않을 수
            # 있으므로 현재 job 컨텍스트(background task/틱) 안에서 동기
            # 실행한다. 호출 체인(_execute_run_job → promote → _execute_run_job)
            # 이 중첩되지만 deferred 체인은 단일 비행이라 깊이가 제한적이다.
            # HTTP 요청 핸들러 안에서 이 경로에 도달하면 안 된다 — 그런
            # 호출자는 반드시 schedule을 넘겨야 한다.
            _execute_run_job(
                store,
                settings,
                llm,
                adapters,
                new_run.id,
                new_run.workflow_key,
                job_payload,
                None,
            )
            return
        threading.Thread(
            target=_execute_run_job,
            args=(
                store,
                settings,
                llm,
                adapters,
                new_run.id,
                new_run.workflow_key,
                job_payload,
                None,
            ),
            daemon=True,
        ).start()

    try:
        wakeups.on_run_finished(store, run_id, schedule=_sched)
    except Exception:
        worker_logger.warning("wakeup finalize failed run=%s", run_id, exc_info=True)


def _execute_run_job(
    store: Store,
    settings: Settings,
    llm: Any,
    adapters: dict[str, Any],
    run_id: str,
    workflow: str,
    payload: dict[str, Any],
    ctx: dict[str, Any] | None = None,
    schedule: Any = None,
) -> None:
    """run 1건을 동기 실행 — automation loop/스레드/cron tick 공용.

    schedule은 _promote_deferred_runs로 전달 — HTTP 핸들러가 이 run을
    동기 실행할 때(현재는 없음) chained promote도 요청 밖으로 빼야 한다."""
    # 조직 컨텍스트 재적용 — daemon thread/BackgroundTasks/cron tick 등
    # contextvar이 전파되지 않는 경로에서 run 행의 org_id를 읽어 scope한다
    # (spec 003 FR-019). 이미 같은 org로 호출된 경우(동기 실행·request ctx)
    # 재진입하지 않는다. org_id='' 행은 ambient ctx를 유지한다.
    oid = _run_org_id(store, run_id)
    if oid and orgctx.current_org_id() != oid:
        with orgctx.org_scope(oid):
            return _execute_run_job(
                store, settings, llm, adapters, run_id, workflow, payload, ctx, schedule
            )
    # spec 006 US2 FR-008 — budget 승인 대기(run.metadata.budget_approval
    # state=waiting) run은 승인/예산 재개 전까지 실행하지 않는다. 생성 경로가
    # 스케줄을 건너뛰지만, deferred promote·스케줄 발사·submit_run 등 다른
    # 진입점이 실수로 잡을 수 있어 실행 경계에서 한 번 더 차단한다.
    try:
        if store.run_budget_approval_state(run_id) == "waiting":
            worker_logger.info("run %s skipped: waiting budget approval", run_id)
            return
    except Exception:
        worker_logger.debug("budget approval state check failed run=%s", run_id, exc_info=True)
    # 동시 run 세마포어 — 슬롯 대기 후 실행. 타임아웃 시 run을 failed로 마감.
    if not _acquire_run_slot(run_id):
        store.update_run(run_id, status=RunStatus.FAILED, error="concurrency_timeout")
        store.audit(run_id, "system", "run_queue_timeout", run_id, {})
        _project_management(store, run_id)
        _emit_run_webhook(store, run_id)
        _emit_run_alert_event(store, run_id)
        _promote_deferred_runs(store, settings, llm, adapters, run_id, schedule=schedule)
        return
    try:
        # retry-from-stage 지시(spec 006 US4) — 라우터가 ctx에 실어 보낸
        # _retry_from을 state gmail_ctx로 새지 않게 꺼내 invoke에 전달한다.
        retry_from = ""
        if isinstance(ctx, dict):
            retry_from = str(ctx.pop("_retry_from", "") or "")
        try:
            runner = build_runner(
                workflow, store=store, llm=llm, settings=settings, adapters=adapters
            )
            runner.invoke(run_id, payload, gmail_ctx=ctx, retry_from=retry_from or None)
        except Exception as e:
            store.update_run(run_id, status=RunStatus.FAILED, error=str(e)[:500])
            store.audit(run_id, "system", "runner_error", "", {"error": str(e)[:300]})
            logger.warning("run %s failed: %s", run_id, str(e)[:200])
            # promote는 슬롯 해제 후로 미룬다 — finally에서 release된 뒤
            # 아래 공통 경로가 수행. 여기서 호출하면 중첩 실행이 추가 슬롯을
            # 필요로 해 체인 깊이만큼 세마포어를 잠식한다.
    finally:
        _run_slots().release()
    run = store.get_run(run_id)
    logger.info("run %s finished status=%s", run_id, getattr(run, "status", "?"))
    _project_management(store, run_id)
    # spec 004 US1 — 종결/대기 상태 webhook 이벤트 (실패는 emit 내부에서 삼킴)
    _emit_run_webhook(store, run_id)
    # spec 006 US1 FR-003 — run 상태 변경을 alert 규칙으로 평가(비동기).
    _emit_run_alert_event(store, run_id)
    _promote_deferred_runs(store, settings, llm, adapters, run_id, schedule=schedule)


@job_traceable("jobmate.process_due_schedules")
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
        # spec 003 FR-019 — 스케줄 행의 org로 발사 전체를 스코프한다.
        # run 생성(admit_run→create_run)·inbox·audit·예산 평가가 모두
        # 이 조직 컨텍스트를 상속해 run.org_id = schedule.org_id가 된다.
        org_tok = orgctx.set_org_context(str(sched.get("org_id") or ""))
        try:
            # FR-031 — suspended/deleted 조직의 스케줄 실행 차단.
            # claim 전에 건너뛰어 정지 해제 시 다음 due에서 재시도된다.
            if _org_inactive(store, str(sched.get("org_id") or "")):
                worker_logger.info("schedule %s skipped: org %s inactive", sid, sched.get("org_id"))
                continue
            interval = max(int(sched.get("interval_minutes") or 0), 1)
            fired = datetime.now(UTC)
            nxt = fired + timedelta(minutes=interval)
            expected_next = str(sched.get("next_run_at") or "")
            # 원자 claim — next_run_at compare-and-swap으로 automation loop과
            # cron tick(또는 다중 인스턴스)의 중복 발사를 막는다
            if not store.claim_schedule_fire(
                sid, expected_next, fired.isoformat(), nxt.isoformat()
            ):
                continue
            # 스케줄 행의 org 컨텍스트(위 set_org_context)로 spec 해석 —
            # 조직별 동명 커스텀 spec이 섞이지 않는다(spec 003 US3).
            wf_spec = resolve_workflow_spec(store, workflow)
            if wf_spec is None:
                worker_logger.warning("schedule %s skipped: unknown workflow %s", sid, workflow)
                continue
            payload = dict(sched.get("input") or {})
            payload.setdefault("task", f"scheduled {workflow}")
            # 스코프드 예산 — hard_stop 초과 시 이번 발사만 건너뛴다
            # (스케줄 자체는 claim돼 next_run_at이 진행됨).
            blocks = budget_scope.evaluate_admission(
                store, workflow, str(payload.get("project") or "")
            )
            if blocks:
                store.audit(
                    "",
                    "system",
                    "schedule_budget_exceeded",
                    sid,
                    {"workflow": workflow, "scope": blocks[0]["scope"]},
                )
                # spec 004 US1 — budget.exceeded webhook (월 1회 dedupe).
                _emit_event_webhook(
                    store,
                    "budget.exceeded",
                    f"schedule-budget:{sid}:{fired:%Y-%m}",
                    {
                        "schedule_id": sid,
                        "workflow": workflow,
                        "scope": str(blocks[0].get("scope") or ""),
                        "source": "schedule",
                    },
                )
                worker_logger.warning(
                    "schedule %s skipped: budget exceeded scope=%s", sid, blocks[0]["scope"]
                )
                try:
                    store.upsert_inbox_item(
                        "budget",
                        f"예산 초과로 스케줄 건너뜀: {workflow}",
                        body=(
                            f"scope={blocks[0]['scope']} spend=${blocks[0]['month_spend_usd']:.4f}"
                        ),
                        link="/?view=cost",
                        dedupe_key=f"schedule-budget:{sid}:{datetime.now(UTC):%Y-%m}",
                    )
                except Exception as e:
                    worker_logger.debug("inbox schedule-budget item failed: %s", str(e)[:200])
                continue
            res = wakeups.admit_run(
                store,
                workflow_key=workflow,
                input_data=payload,
                source="schedule",
                created_by=f"schedule:{sid}",
                version=wf_spec.version,
                job_payload=payload,
            )
            action = str(res.get("action") or "")
            if action == "invalid_goal":
                worker_logger.warning("schedule %s skipped: invalid goal %s", sid, res.get("error"))
                continue
            if action != "created":
                # 동일 scope 활성 run에 합류 — 스케줄은 claim됐으므로 재시도 없음
                worker_logger.info(
                    "schedule %s %s into run=%s", sid, action, res.get("run_id") or "-"
                )
                continue
            run = res["run"]
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
        finally:
            orgctx.reset_org_context(org_tok)


@job_traceable("jobmate.process_due_routines")
def _process_due_routines(
    store: Store,
    settings: Settings,
    llm: Any,
    adapters: dict[str, Any],
    defer: list[tuple[str, str, dict[str, Any], dict[str, Any] | None]] | None = None,
) -> None:
    """routines 중 next_run_at이 지난 enabled 루틴을 발사 (paperclip spec 3차 C).

    _process_due_schedules와 동일 구조 — defer가 주어지면 run job을 defer에
    추가(호출자가 _schedule_run_jobs로 실행), None이면 daemon thread로 즉시
    실행. 발사 자체(task 생성 + wake)는 services/routines.fire_due_routine이
    담당하고, claim CAS + routine_runs idempotency가 중복을 막는다.
    routine 1건의 실패는 루프를 죽이지 않는다."""
    from ..services import routines as routines_svc

    try:
        batch = max(1, int(os.environ.get("JOBMATE_ROUTINE_BATCH", "10")))
    except (TypeError, ValueError):
        batch = 10

    def _schedule_run(run: Any, job_payload: dict[str, Any]) -> None:
        """wake_agent의 schedule 콜백 — 생성된 run을 defer 또는 thread로 실행."""
        if run is None:
            return
        job = (str(run.id), str(run.workflow_key), dict(job_payload or {}), None)
        if defer is not None:
            defer.append(job)
        else:
            threading.Thread(
                target=_execute_run_job,
                args=(store, settings, llm, adapters, *job),
                daemon=True,
            ).start()

    fired = skipped = failed = 0
    # R2-5 — claim 후 크래시/마감 실패로 pending에 남은 routine_run 회수.
    # 저비용 UPDATE 1건 — 매 due-routine 처리 주기마다 실행한다.
    try:
        swept = routines_svc.sweep_stale_pending_runs(store)
        if swept:
            worker_logger.info("swept %d stale pending routine_runs", swept)
    except Exception as e:
        worker_logger.warning("stale routine_run sweep failed: %s", str(e)[:200])
    for routine in store.due_routines(limit=batch):
        rid = str(routine.get("id") or "")
        # spec 003 FR-019 — routine 행의 org로 발사를 스코프한다. 생성되는
        # task/routine_run/run·audit·inbox가 모두 이 조직을 상속한다.
        org_tok = orgctx.set_org_context(str(routine.get("org_id") or ""))
        try:
            # FR-031 — suspended/deleted 조직의 루틴 발사 차단.
            if _org_inactive(store, str(routine.get("org_id") or "")):
                worker_logger.info(
                    "routine %s skipped: org %s inactive", rid, routine.get("org_id")
                )
                continue
            res = routines_svc.fire_due_routine(
                store, routine, schedule_run=_schedule_run, actor="routine"
            )
            action = str(res.get("action") or "")
            if action == "claimed":
                fired += 1
                worker_logger.info(
                    "routine %s fired task=%s scheduled_for=%s",
                    rid,
                    res.get("task_id") or "-",
                    res.get("scheduled_for") or "-",
                )
            elif action == "skipped":
                skipped += 1
            elif action == "failed":
                failed += 1
                worker_logger.info("routine %s failed", rid)
        except Exception as e:
            failed += 1
            worker_logger.warning("routine %s fire failed: %s", rid, str(e)[:200])
            try:
                store.upsert_inbox_item(
                    "routine",
                    f"루틴 실패: {routine.get('name') or rid}",
                    body=str(e)[:300],
                    link="/?view=office",
                    dedupe_key=f"routine-fail:{rid}:{datetime.now(UTC).isoformat()}",
                )
            except Exception as inbox_err:
                worker_logger.debug("inbox routine-fail item failed: %s", str(inbox_err)[:200])
        finally:
            orgctx.reset_org_context(org_tok)
    if fired or skipped or failed:
        # routine cycle counts — spec 3차 G 로깅 요구.
        worker_logger.info(
            "routine cycle done fired=%d skipped=%d failed=%d", fired, skipped, failed
        )


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


@job_traceable("jobmate.check_waiting_approvals")
def _check_waiting_approvals(store: Store, adapters: dict[str, Any]) -> None:
    """dev_review run이 waiting_approval에 '전이'할 때만 웹 푸시.

    updated_at 지문으로 동일 대기 상태의 반복 감지를 막고, 승인을 거쳐
    다시 대기에 들어오면(지문 변경) 새로 알린다. 지문은 DB state에 저장 —
    서버리스에서는 인메모리 dict가 매 호출마다 초기화돼 매분 푸시가 반복됐다."""
    from ..engine.functions import send_web_push_text

    waiting: dict[str, Any] = {}
    for run in store.list_runs(limit=100):
        if run.status == RunStatus.WAITING_APPROVAL:
            waiting[run.id] = run
    fp_map = store.get_state("approval_wait_fp", {})
    if not isinstance(fp_map, dict):
        fp_map = {}
    changed = False
    for rid in list(fp_map):
        if rid not in waiting:
            fp_map.pop(rid, None)
            changed = True
    for run in waiting.values():
        fp = run.updated_at.isoformat() if run.updated_at else run.id
        if fp_map.get(run.id) == fp:
            continue
        # spec 003 FR-019 — run별 org로 inbox 쓰기를 스코프한다.
        org_tok = orgctx.set_org_context(_run_org_id(store, run.id))
        try:
            fp_map[run.id] = fp
            changed = True
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
                messenger.notify(f"승인 대기: {run.workflow_key} ({task or run.id})")
            except Exception:
                logger.debug("messenger notify failed", exc_info=True)
            logger.info("waiting_approval push sent run=%s", run.id)
            # spec 004 US1 — 승인 대기 진입 webhook. run.waiting_approval은
            # _execute_run_job과 event_id(상태+updated_at 지문)가 같아
            # UNIQUE로 중복 전송되지 않고, approval.requested는 지문을
            # 포함해 재대기 시 새 이벤트로 발행된다.
            _emit_run_webhook(store, run.id)
            _emit_event_webhook(
                store,
                "approval.requested",
                f"approval:{run.id}:requested:{fp}",
                {
                    "run_id": run.id,
                    "workflow": run.workflow_key,
                    "status": "waiting_approval",
                },
            )
        finally:
            orgctx.reset_org_context(org_tok)
    if changed:
        store.set_state("approval_wait_fp", fp_map)


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
        make_schedule,
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
        bearer: str = "",
    ) -> str:
        """job id 확보(기존 → env → 자동 생성) 후 스케줄 갱신.
        bearer 지정 시 X-API-Key 대신 Authorization: Bearer 헤더 사용
        (cron/tick은 Vercel에서 CRON_SECRET Bearer만 허용)."""
        job_id = job_id or str(prefs.get(pref_key) or "")
        # 요청 헤더/URL은 생성·갱신 모두에 사용 — API 키 로테이션 시
        # 기존 job의 stale 헤더를 함께 갱신하기 위함.
        req_headers: dict[str, str] = {"Content-Type": "application/json"}
        if bearer:
            req_headers["Authorization"] = f"Bearer {bearer}"
        else:
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
    # 자동화 tick — 스케줄/메시지/리포트/트리거/알림의 유일한 구동 신호.
    # 매분 호출(cron-job.org 최소 주기). Vercel의 tick 인증은 CRON_SECRET
    # Bearer만 허용하므로 bearer로 전달한다.
    cron_secret = os.environ.get("CRON_SECRET") or os.environ.get("JOBMATE_CRON_SECRET", "")
    if cron_secret:
        ensure_job(
            "",
            "cronjob_tick_job_id",
            "jobMate automation tick",
            "/api/v1/cron/tick",
            "",
            make_schedule(minutes=[-1]),
            "tick",
            bearer=cron_secret,
        )
    else:
        out["tick"] = "skipped(no cron secret)"
    return out


def _automation_loop(
    store: Store, settings: Settings, llm: Any, adapters: dict[str, Any], stop: threading.Event
) -> None:
    """calendar-agent worker 포팅: pending 메시지 처리 + 알림 예약 발송 +
    gmail 폴링 + radar 일일 수집.

    1회 사이클의 작업 순서는 run_automation_cycle이 단일 기준 — 서버리스
    환경에서는 /api/v1/cron/tick이 같은 사이클을 BackgroundTasks 실행으로
    대체한다(두 진입점 설명은 run_automation_cycle docstring 참고)."""
    while not stop.is_set():
        try:
            _mark_cron_tick(store)
            # 비용 원장 1회성 백필 — automation_state 가드로 매 tick 1쿼리 조기 반환.
            # 배치 상한이 있어 대량 DB에서도 tick을 막지 않는다(2차 C).
            cost_ledger.maybe_backfill_cost_events(store)
            run_automation_cycle(store, settings, llm, adapters)
        except Exception as exc:
            worker_logger.exception("automation loop error")
            # spec 004 US4/FR-026 — Sentry 캡처(미설정 시 no-op).
            try:
                from ..services import sentry as _sentry

                _sentry.capture_exception(exc, tags={"component": "automation_loop"})
            except Exception:
                worker_logger.debug("sentry capture failed", exc_info=True)
        stop.wait(settings.worker_interval_seconds)


def _process_pending_messages(
    store: Store, settings: Settings, llm: Any, adapters: dict[str, Any]
) -> None:
    """pending 메시지를 claim한 뒤 메시지당 worker thread로 디스패치.

    _create_and_run은 runner.invoke가 끝날 때까지 블로킹하므로 automation
    loop/cron tick에서 동기 호출하면 루프 전체가 메시지 1건 처리 시간만큼
    멈춘다. claim은 루프에서 원자적으로 수행하고 실제 처리는 스레드로 넘겨
    다른 메시지/스케줄/폴링이 계속 진행되게 한다. 실패 시 기존
    attempts/backoff(finish_message pending+retry_at) 의미는 그대로 유지."""
    for row in store.pending_messages():
        provider = row["source_provider"]
        source_id = row["source_id"]
        # 원자 claim — automation loop과 cron tick이 같은 메시지를 동시에
        # 처리해 run을 중복 생성하는 것을 막는다 (리스 만료 시 재claim 가능)
        # spec 003 US3 — 복합 PK라 같은 (provider,source_id)가 조직별로
        # 존재하므로 행의 org_id를 명시한다. ctx='' 배치 루프에서 무필터
        # claim하면 타 조직 행까지 processing으로 뒤집힌다('' org 행도
        # org_id='' 조건으로 정확히 스코프된다).
        row_org = str(row.get("org_id") or "")
        if not store.claim_message(provider, source_id, org_id=row_org):
            continue
        threading.Thread(
            target=_process_message_job,
            args=(store, settings, llm, adapters, dict(row)),
            daemon=True,
        ).start()


@job_traceable("jobmate.process_message")
def _process_message_job(
    store: Store,
    settings: Settings,
    llm: Any,
    adapters: dict[str, Any],
    row: dict[str, Any],
) -> None:
    """claim된 메시지 1건의 intake run 실행 — worker thread에서 호출."""
    # 조직 컨텍스트 재적용 — daemon thread는 contextvar을 물려받지 못하므로
    # 메시지 행의 org_id로 scope한다(spec 003 FR-019). intake run의
    # org_id는 이 메시지의 org를 따른다.
    oid = str(row.get("org_id") or "")
    if oid and orgctx.current_org_id() != oid:
        with orgctx.org_scope(oid):
            return _process_message_job(store, settings, llm, adapters, row)
    provider = row["source_provider"]
    source_id = row["source_id"]
    attempts = int(row.get("attempts", 0)) + 1
    # FR-031 — suspended/deleted 조직의 메시지는 새 run을 만들지 않는다.
    # 'failed'는 terminal이라 pending_messages(pending/processing만 수집)가
    # 재수집하지 않아 조직 정지 동안 무한 재시도하지 않는다. 메시지 행은
    # 1회성이므로 조직 복귀 후 자동 재처리되지 않는다 — 필요 시 재투입.
    if _org_inactive(store, oid):
        try:
            store.finish_message(
                provider,
                source_id,
                "failed",
                "org_inactive",
                attempts=attempts,
                org_id=oid,
            )
        except Exception as e:
            worker_logger.warning(
                "message %s:%s org_inactive finish failed: %s",
                provider,
                source_id,
                str(e)[:200],
            )
        worker_logger.info("message %s:%s skipped: org %s inactive", provider, source_id, oid)
        return
    try:
        if attempts > 5:
            store.finish_message(
                provider, source_id, "failed", "max attempts", attempts=attempts, org_id=oid
            )
            worker_logger.warning(
                "message %s:%s failed permanently after %s attempts",
                provider,
                source_id,
                attempts,
            )
            return
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
            provider, source_id, "processed", f"run:{run_id}", attempts=attempts, org_id=oid
        )
    except BudgetExceededError as e:
        # 스코프드 예산 hard_stop — 월간 윈도우 리셋 전 재시도는 무의미한
        # 비일시 오류. attempts를 소진해 'max attempts'로 끝나는 대신
        # 사유가 명확한 budget_exceeded로 terminal failed 마감한다.
        worker_logger.warning(
            "message %s:%s blocked by budget: %s", provider, source_id, str(e)[:200]
        )
        store.finish_message(
            provider, source_id, "failed", str(e)[:200], attempts=attempts, org_id=oid
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
            org_id=oid,
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
    # 스코프드 예산 — 차단 시 예외를 던져 호출자(메시지 재시도/ radar 로그) 경로에 맡긴다
    # 코얼레싱 scope input에 메시지 식별자를 포함한다 — task가 상수
    # ("message intake" 등)인 경로에서 서로 다른 메시지가 하나의 run으로
    # 합쳐져 후속 메시지가 처리 없이 processed 마킹되는 데이터 유실을 막는다.
    input_data: dict[str, Any] = {"task": task}
    if ctx:
        for k in ("source_provider", "source_id", "gmail_message_id", "platform", "project"):
            v = ctx.get(k)
            if v:
                input_data[k] = str(v)[:200]
    blocks = budget_scope.evaluate_admission(store, workflow, str(input_data.get("project") or ""))
    if blocks:
        raise BudgetExceededError(f"budget_exceeded: {blocks[0]['scope']}")
    wf_spec = resolve_workflow_spec(store, workflow)
    res = wakeups.admit_run(
        store,
        workflow_key=workflow,
        input_data=input_data,
        source="automation",
        created_by="system:intake",
        version=wf_spec.version if wf_spec is not None else "",
        job_payload={"task": task},
    )
    if res["action"] == "invalid_goal":
        # 방어선 — 이 경로의 input_data는 goal_id를 복사하지 않으므로
        # 실질 도달 불가. 도달 시 호출자의 실패/재시도 경로로 전파한다.
        raise RuntimeError(f"invalid goal: {res.get('error')}")
    if res["action"] != "created":
        # 동일 scope 활성 run에 합류 — 새 실행 없이 기존 run id 반환
        return str(res.get("run_id") or "")
    run = res["run"]
    # 동시 run 세마포어 — 메시지 폭주 시 LLM 동시 호출 상한.
    # 타임아웃 시 run을 failed로 마감하고 호출자(_process_message_job)의
    # 재시도 경로로 예외를 전파한다.
    if not _acquire_run_slot(run.id):
        try:
            store.update_run(run.id, status=RunStatus.FAILED, error="concurrency_timeout")
        except Exception:
            logger.warning("run %s FAILED 마킹 실패", run.id, exc_info=True)
        _project_management(store, run.id)
        _emit_run_webhook(store, run.id)
        _promote_deferred_runs(store, settings, llm, adapters, run.id)
        raise RuntimeError(f"run {run.id} concurrency slot timeout")
    invoke_error: Exception | None = None
    try:
        try:
            # build_runner 실패도 run을 pending/running에 고착시키지 않도록 try 안에서 호출
            runner = build_runner(
                workflow, store=store, llm=llm, settings=settings, adapters=adapters
            )
            runner.invoke(run.id, {"task": task}, gmail_ctx=ctx)
        except Exception as e:
            # invoke가 예외로 죽으면 run이 running에 영구 고착된다 — FAILED로 마감 후 재전파
            try:
                store.update_run(run.id, status=RunStatus.FAILED, error=str(e)[:500])
            except Exception:
                logger.warning("run %s FAILED 마킹 실패", run.id, exc_info=True)
            logger.warning("run %s invoke failed: %s", run.id, str(e)[:200])
            # promote는 슬롯 해제 후 공통 경로로 미룬다 — 슬롯 보유 중
            # 중첩 promote하면 체인 깊이만큼 세마포어를 잠식한다.
            invoke_error = e
    finally:
        _run_slots().release()
    done = store.get_run(run.id)
    logger.info("run %s finished status=%s", run.id, getattr(done, "status", "?"))
    _project_management(store, run.id)
    # spec 004 US1 — 메시지 intake 등 _execute_run_job을 거치지 않는
    # run 종결 경로도 동일 이벤트를 emit한다(event_id 멱등).
    _emit_run_webhook(store, run.id)
    _promote_deferred_runs(store, settings, llm, adapters, run.id)
    if invoke_error is not None:
        raise invoke_error
    return run.id


@job_traceable("jobmate.process_due_notification_jobs")
def _process_due_notification_jobs(store: Store, adapters: dict[str, Any]) -> None:
    notifier = adapters.get("notifier")
    if notifier is None:
        return
    for job in store.due_notification_jobs():
        # 원자 claim — 조회~발송 사이에 다른 tick이 같은 job을 집어
        # 알림이 중복 발송되는 레이스를 막는다.
        if not store.claim_job(job["id"]):
            continue
        # spec 003 FR-019 — notification job 행의 org로 발송 블록을 스코프.
        # scheduled_events 조회·mark_job이 이 조직 컨텍스트를 따른다.
        org_tok = orgctx.set_org_context(str(job.get("org_id") or ""))
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
        finally:
            orgctx.reset_org_context(org_tok)


@job_traceable("jobmate.poll_gmail")
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
    # 연속 실패 시 지수 백오프 — 실패할 때마다 매 틱 재시도해 Gmail API를 두드리는 것 방지
    backoff_until = str(auto.get("gmail_backoff_until") or "")
    if backoff_until:
        try:
            if datetime.now(UTC) < datetime.fromisoformat(backoff_until):
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
        auto["gmail_fail_count"] = 0
        auto["gmail_backoff_until"] = ""
        if ingested:
            logger.info("gmail poll ingested %d new messages", ingested)
    except Exception as e:
        auto["last_gmail_error"] = type(e).__name__
        fails = int(auto.get("gmail_fail_count") or 0) + 1
        auto["gmail_fail_count"] = fails
        delay = min(interval_secs * (2 ** min(fails, 4)), 3600)
        auto["gmail_backoff_until"] = (datetime.now(UTC) + timedelta(seconds=delay)).isoformat()
        auto["last_gmail_poll_at"] = datetime.now(UTC).isoformat()
        worker_logger.warning(
            "gmail poll failed (attempt %s, backoff %ss): %s",
            fails,
            delay,
            type(e).__name__,
        )
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


@job_traceable("jobmate.daily_radar")
def _maybe_daily_radar(
    store: Store,
    settings: Settings,
    llm: Any,
    adapters: dict[str, Any],
    defer: list[tuple[str, str, dict[str, Any], dict[str, Any] | None]] | None = None,
    schedule: Any = None,
) -> None:
    """KST 06:00 이후 최초 1회 radar 자동 수집 (자동화 플래그).

    schedule은 _promote_deferred_runs로 전달 — cron tick HTTP 핸들러가
    호출할 때 deferred promote 실행을 BackgroundTasks로 위임하기 위함."""
    # radar는 항상 auto_approve — 과거 잔여 waiting_approval run/인박스 알림 정리.
    # 취소된 run의 wakeup finalize도 여기서 수행해야 deferred promote가 굶지 않는다.
    radar_reaped: list[str] = []
    cleaned = store.cleanup_radar_waits(reaped_out=radar_reaped)
    if cleaned:
        logger.info("radar stale waiting_approval cleaned: %d", cleaned)
        for rid in radar_reaped:
            try:
                _promote_deferred_runs(store, settings, llm, adapters, rid, schedule=schedule)
            except Exception:
                worker_logger.warning(
                    "wakeup finalize after radar cleanup failed run=%s", rid, exc_info=True
                )
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
            try:
                if defer is not None:
                    # defer 경로는 job을 직접 예약하므로 여기서 admit —
                    # _create_and_run은 내부에서 다시 admit하므로 호출하지 않는다.
                    # 스코프드 예산도 여기서 검사한다(_create_and_run 우회 경로).
                    blocks = budget_scope.evaluate_admission(
                        store, "skill_radar", str(ctx.get("project") or "")
                    )
                    if blocks:
                        worker_logger.warning(
                            "radar %s deferred: budget exceeded scope=%s",
                            p,
                            blocks[0]["scope"],
                        )
                        continue
                    res = wakeups.admit_run(
                        store,
                        workflow_key="skill_radar",
                        input_data={"task": task},
                        source="automation",
                        created_by="radar",
                        version=WORKFLOWS["skill_radar"].version,
                        job_payload={"task": task},
                    )
                    if res["action"] == "created":
                        defer.append((res["run"].id, "skill_radar", {"task": task}, ctx))
                    else:
                        worker_logger.info(
                            "radar %s %s into run=%s", p, res["action"], res.get("run_id") or "-"
                        )
                else:
                    _create_and_run(store, settings, llm, adapters, "skill_radar", task, ctx)
            except Exception as e:
                # 플랫폼 1개의 실패(예산 차단 BudgetExceededError 포함)가
                # 나머지 플랫폼 수집과 last_radar_date 기록을 막지 않게 한다.
                worker_logger.warning("radar %s collect failed: %s", p, str(e)[:200])
                continue
            scheduled.append(p)
    auto["last_radar_date"] = today
    store.set_automation(auto)
    if scheduled:
        logger.info("radar daily collect scheduled: %s", ",".join(scheduled))


_REPORT_MAX_AGE = {
    "daily": timedelta(hours=20),
    "exec_daily": timedelta(hours=20),
    "weekly": timedelta(days=7),
    "exec_weekly": timedelta(days=7),
}


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


@job_traceable("jobmate.generate_reports")
def _maybe_generate_reports(store: Store, settings: Settings, adapters: dict[str, Any]) -> None:
    """daily/exec_daily 20시간·weekly/exec_weekly 7일 주기로 자동 생성.
    생성 시 reports 테이블 저장 + 인박스 kind='report' + 메신저 알림(best-effort)."""
    from ..reports import REPORT_KIND_LABELS, build_report

    for kind in ("daily", "exec_daily", "weekly", "exec_weekly"):
        try:
            if not _report_is_stale(store, kind):
                continue
            content = build_report(store, kind, settings)
            rid = store.save_report(kind, content)
            store.upsert_inbox_item(
                "report",
                f"{REPORT_KIND_LABELS.get(kind, kind)} 리포트 생성됨",
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
                kind_label = REPORT_KIND_LABELS.get(kind, kind)
                messenger.notify(
                    f"jobMate {kind_label} 리포트: run {content['runs']['total']}건 "
                    f"(실패 {content['runs']['failed']}), "
                    f"승인 대기 {content['approvals']['pending']}"
                )
            except Exception:
                worker_logger.debug("messenger notify after auto report failed", exc_info=True)
        except Exception as e:
            worker_logger.warning("auto report %s failed: %s", kind, str(e)[:200])
