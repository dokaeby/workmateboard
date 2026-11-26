"""app.py에서 분리한 automation worker 잡 함수들."""

from __future__ import annotations

import logging
import os
import threading
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .. import budget_scope, messenger, wakeups
from ..config import Settings
from ..engine.trace import job_traceable
from ..models import RunStatus
from ..sensors import build_gmail_url, is_safe_source_url
from ..store.db import Store
from ..workflows import WORKFLOWS
from ..workflows.registry_builder import build_runner
from .support import (
    _acquire_run_slot,
    _mark_cron_tick,
    _maybe_check_triggers,
    _reap_stale_runs,
    _retention_sweep,
    _run_slots,
)

logger = logging.getLogger("jobmate.api")
worker_logger = logging.getLogger("jobmate.worker")


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


# ---------- automation worker ----------


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
    # 동시 run 세마포어 — 슬롯 대기 후 실행. 타임아웃 시 run을 failed로 마감.
    if not _acquire_run_slot(run_id):
        store.update_run(run_id, status=RunStatus.FAILED, error="concurrency_timeout")
        store.audit(run_id, "system", "run_queue_timeout", run_id, {})
        _project_management(store, run_id)
        _promote_deferred_runs(store, settings, llm, adapters, run_id, schedule=schedule)
        return
    try:
        try:
            runner = build_runner(
                workflow, store=store, llm=llm, settings=settings, adapters=adapters
            )
            runner.invoke(run_id, payload, gmail_ctx=ctx)
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
        try:
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
            if workflow not in WORKFLOWS:
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
                version=WORKFLOWS[workflow].version,
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
    gmail 폴링 + radar 일일 수집."""
    while not stop.is_set():
        try:
            _mark_cron_tick(store)
            _reap_stale_runs(store, settings, llm, adapters)
            _retention_sweep(store)
            _process_pending_messages(store, settings, llm, adapters)
            _process_due_notification_jobs(store, adapters)
            _process_due_schedules(store, settings, llm, adapters)
            _check_waiting_approvals(store, adapters)
            _maybe_poll_gmail(store, settings, adapters)
            _maybe_daily_radar(store, settings, llm, adapters)
            _maybe_check_triggers(store, settings, llm, adapters)
            _maybe_generate_reports(store, settings, adapters)
        except Exception:
            worker_logger.exception("automation loop error")
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
        if not store.claim_message(provider, source_id):
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
    provider = row["source_provider"]
    source_id = row["source_id"]
    attempts = int(row.get("attempts", 0)) + 1
    try:
        if attempts > 5:
            store.finish_message(provider, source_id, "failed", "max attempts", attempts=attempts)
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
        store.finish_message(provider, source_id, "processed", f"run:{run_id}", attempts=attempts)
    except BudgetExceededError as e:
        # 스코프드 예산 hard_stop — 월간 윈도우 리셋 전 재시도는 무의미한
        # 비일시 오류. attempts를 소진해 'max attempts'로 끝나는 대신
        # 사유가 명확한 budget_exceeded로 terminal failed 마감한다.
        worker_logger.warning(
            "message %s:%s blocked by budget: %s", provider, source_id, str(e)[:200]
        )
        store.finish_message(provider, source_id, "failed", str(e)[:200], attempts=attempts)
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
    res = wakeups.admit_run(
        store,
        workflow_key=workflow,
        input_data=input_data,
        source="automation",
        created_by="system:intake",
        version=WORKFLOWS[workflow].version,
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
