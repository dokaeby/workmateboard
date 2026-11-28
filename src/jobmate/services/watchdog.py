# mypy: disable-error-code="attr-defined"
"""Watchdog 서비스 — paperclip spec 2차 E.

cron tick/automation loop의 한 cycle에 bounded watchdog를 돌린다.
5개 detector(stale_heartbeat, stranded_task, stuck_run, missing_cost,
failed_resume)가 recovery_actions를 만들고, 매 cycle open recovery의
건강을 재검사해 회복됐으면 resolved, 아니면 제한된 안전 복구를 시도한다.
max_attempts 초과 시 escalated + 사람 알림(inbox) + source task blocked.

자동 retry는 명시적으로 안전한 경로만 — run 재개는 approval_request를
만들어 사람 결정을 기다리고, 외부 전송/파괴 작업은 자동 실행하지 않는다.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from .. import cost_ledger
from ..models import (
    ApprovalActionType,
    ApprovalRisk,
    RecoveryKind,
    RecoveryOwnerType,
    RecoveryStatus,
    RunStatus,
    TaskStatus,
)
from ..services import agent_management as mgmt
from ..services import approvals as approvals_svc
from ..store import orgctx
from ..store.db import Store
from ..store.util import _iso

logger = logging.getLogger("jobmate.watchdog")

# automation loop/cron tick/admin watchdog/run이 겹쳐도 cycle은 1개만 —
# 비차단 lock으로 후발 호출은 즉시 skipped를 반환한다.
_CYCLE_LOCK = threading.Lock()

# spec E: 한 cycle 처리 상한 — env로 조정 가능(기본 25).
_DEFAULT_BATCH = 25
# spec E 예시: heartbeat 임계 기본 30분.
_DEFAULT_HEARTBEAT_MINUTES = 30
# stuck_run은 queued/running의 30분 reaper와 별개로 더 긴 꼬리만 본다.
_DEFAULT_STUCK_HOURS = 24
# failed_resume 탐색 창 — 그보다 오래된 resume 실패는 사람이 이미 처리.
_FAILED_RESUME_WINDOW_HOURS = 72
# 복구 재시도 백오프 — attempt마다 지수 증가(상한 6h).
_RETRY_BASE_MINUTES = 15
_RETRY_MAX_MINUTES = 360
_ACTIVE_RUN_STATUSES = {
    RunStatus.QUEUED.value,
    RunStatus.RUNNING.value,
    RunStatus.WAITING_APPROVAL.value,
    RunStatus.WAITING_REVIEW.value,
}
_TERMINAL_RUN_STATUSES = {
    RunStatus.COMPLETED.value,
    RunStatus.FAILED.value,
    RunStatus.REJECTED.value,
    RunStatus.CANCELLED.value,
    RunStatus.BUDGET_EXHAUSTED.value,
    RunStatus.NEEDS_HUMAN.value,
}
# kind별 기본 max_attempts — 감지→escalation까지의 안전 재시도 횟수.
_KIND_MAX_ATTEMPTS = {
    RecoveryKind.STALE_HEARTBEAT.value: 3,
    RecoveryKind.STRANDED_TASK.value: 3,
    RecoveryKind.STUCK_RUN.value: 2,
    RecoveryKind.MISSING_COST.value: 3,
    RecoveryKind.FAILED_RESUME.value: 2,
}


def _env_int(name: str, default: int, *, lo: int = 1, hi: int = 100_000) -> int:
    try:
        v = int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def watchdog_batch_limit() -> int:
    return _env_int("JOBMATE_WATCHDOG_BATCH", _DEFAULT_BATCH)


def _heartbeat_cutoff(now: datetime) -> str:
    minutes = _env_int("JOBMATE_WATCHDOG_HEARTBEAT_MINUTES", _DEFAULT_HEARTBEAT_MINUTES)
    return _iso(now - timedelta(minutes=minutes)) or ""


def _stuck_cutoff(now: datetime) -> str:
    hours = _env_int("JOBMATE_WATCHDOG_STUCK_HOURS", _DEFAULT_STUCK_HOURS)
    return _iso(now - timedelta(hours=hours)) or ""


def _iso_dt(s: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


@contextmanager
def _rec_scope(rec: dict[str, Any]) -> Iterator[None]:
    """recovery_actions 행의 org로 스코프 — spec 003 FR-019.

    watchdog cycle은 ctx=''로 모든 조직의 recovery를 순회하므로
    행 단위 org_id를 읽어 inbox/event/approval 쓰기가 올바른 조직에
    귀속되게 한다. org_id='' 행은 ambient ctx를 유지한다."""
    oid = str(rec.get("org_id") or "")
    if oid and orgctx.current_org_id() != oid:
        with orgctx.org_scope(oid):
            yield
    else:
        yield


def _source_org_id(store: Store, source_type: str, source_id: str) -> str:
    """recovery source(agent/task/run)의 org_id — detector가 recovery를
    source와 같은 조직에 기록하기 위해 사용한다."""
    try:
        if source_type == "agent":
            row = store.get_agent(source_id)
            return str((row or {}).get("org_id") or "")
        if source_type == "task":
            row = store.get_task(source_id)
            return str((row or {}).get("org_id") or "")
        if source_type == "run":
            from .management_common import row_org_id

            return row_org_id(store, "runs", source_id)
    except Exception:
        return ""
    return ""


def _retry_delay_minutes(attempt_count: int) -> int:
    """지수 백오프 — 15m, 30m, 60m … 상한 6h."""
    return min(_RETRY_BASE_MINUTES * (2 ** max(0, attempt_count - 1)), _RETRY_MAX_MINUTES)


def _source_label(rec: dict[str, Any]) -> str:
    return f"{rec.get('source_type')}:{rec.get('source_id')}"


def _escalate(store: Store, rec: dict[str, Any], *, note: str, actor: str = "watchdog") -> None:
    """max_attempts 초과 — escalated + owner human + inbox 알림 + task blocked."""
    rid = str(rec.get("id") or "")
    if not store.set_recovery_status(
        rid,
        RecoveryStatus.ESCALATED.value,
        outcome="max_attempts_exceeded",
        resolution_note=note[:500],
        owner_type=RecoveryOwnerType.HUMAN.value,
    ):
        return
    st = str(rec.get("source_type") or "")
    sid = str(rec.get("source_id") or "")
    kind = str(rec.get("kind") or "")
    title = f"복구 에스컬레이션: {kind} ({st}:{sid})"
    body = (
        f"watchdog recovery {rid}가 max_attempts({rec.get('max_attempts')})를 "
        f"초과했습니다. kind={kind} source={st}:{sid}. {note[:200]}"
    )
    link = ""
    if st == "task":
        link = f"/api/v1/management/tasks/{sid}"
    elif st == "run":
        link = f"/runs/{sid}"
    elif st == "agent":
        link = f"/api/v1/management/agents/{sid}"
    try:
        store.upsert_inbox_item(
            "recovery",
            title,
            body,
            link=link,
            dedupe_key=f"recovery-escalated:{rid}",
            reset_read=True,
        )
    except Exception:
        logger.warning("recovery inbox notify failed id=%s", rid, exc_info=True)
    mgmt.emit_event(
        store,
        "error",
        agent_id=sid if st == "agent" else None,
        task_id=sid if st == "task" else None,
        run_id=sid if st == "run" else None,
        actor=actor,
        summary=f"recovery escalated: {kind} {_source_label(rec)}",
        detail={"recovery_id": rid, "kind": kind, "attempts": rec.get("attempt_count")},
        idempotency_key=f"recovery-escalated:{rid}",
    )
    # spec E: max attempts 후 task blocked — source가 task면 안전한
    # 상태 전이(in_progress→blocked)만 시도한다.
    if st == "task":
        try:
            task = store.get_task(sid)
            if task is not None and task.get("status") == TaskStatus.IN_PROGRESS.value:
                mgmt._advance_task_status(  # noqa: SLF001 — watchdog 복구 경로
                    store, sid, TaskStatus.BLOCKED.value
                )
        except Exception:
            logger.warning("recovery task block failed task=%s", sid, exc_info=True)
    logger.warning(
        "recovery escalated id=%s kind=%s source=%s attempts=%s",
        rid,
        kind,
        _source_label(rec),
        rec.get("attempt_count"),
    )


# ---------------- 건강 재검사 (source가 회복됐는가) ----------------


def _source_recovered(store: Store, rec: dict[str, Any], *, now: datetime) -> bool:
    """open recovery의 source가 더 이상 문제 상태가 아니면 True → resolved."""
    sid = str(rec.get("source_id") or "")
    kind = str(rec.get("kind") or "")
    try:
        if kind == RecoveryKind.STALE_HEARTBEAT.value:
            agent = store.get_agent(sid)
            if agent is None:
                return True  # terminated/삭제 — heartbeat 불필요
            lsa = _iso_dt(str(agent.get("last_seen_at") or ""))
            cutoff = _iso_dt(_heartbeat_cutoff(now))
            return lsa is not None and cutoff is not None and lsa >= cutoff
        if kind == RecoveryKind.STRANDED_TASK.value:
            task = store.get_task(sid)
            if task is None or task.get("status") != TaskStatus.IN_PROGRESS.value:
                return True
            if task.get("checkout_agent_id"):
                return True
            cur = str(task.get("current_run_id") or "")
            if cur:
                run = store.get_run(cur)
                if run is not None and str(run.status.value) in _ACTIVE_RUN_STATUSES:
                    return True
            # task_runs의 다른 활성 run도 확인 — stranded 판정과 동일 규칙.
            for tr in store.task_runs_for_task(sid):
                run = store.get_run(str(tr.get("run_id") or ""))
                if run is not None and str(run.status.value) in _ACTIVE_RUN_STATUSES:
                    return True
            return False
        if kind in (
            RecoveryKind.STUCK_RUN.value,
            RecoveryKind.FAILED_RESUME.value,
        ):
            run = store.get_run(sid)
            if run is None:
                return True
            return str(run.status.value) in _TERMINAL_RUN_STATUSES
        if kind == RecoveryKind.MISSING_COST.value:
            # stage_run 단위 커버리지 — stage_run_id 태그든 run_id 태그든
            # cost event가 없는 finished token stage_run이 남아 있으면
            # 미해결. detector SQL과 동일 규칙.
            return not store.run_has_uncovered_cost_stage(sid)
    except Exception:
        logger.warning("recovery health check failed %s", _source_label(rec), exc_info=True)
        return False
    return False


# ---------------- 복구 시도 (안전한 것만) ----------------


def _request_run_resume_approval(store: Store, run_id: str, *, actor: str) -> str:
    """run 재개는 사람 승인 경유 — run_resume approval_request 생성(dedupe)."""
    row, created = approvals_svc.request_approval(
        store,
        action_type=ApprovalActionType.RUN_RESUME.value,
        risk_level=ApprovalRisk.MEDIUM.value,
        summary=f"watchdog: resume run {run_id}",
        safe_payload={"run_id": run_id},
        requested_by=actor,
        run_id=run_id,
        resource_type="run",
        resource_id=run_id,
        dedupe=True,
    )
    return "approval_requested" if created else "approval_pending"


def _attempt_recovery(store: Store, rec: dict[str, Any], *, now: datetime) -> str:
    """open recovery 1건의 안전 복구 시도 — outcome 문자열 반환.

    destructive/external 자동 retry는 없다 — 사람 결정을 요청하거나
    관측/알림만 남긴다."""
    kind = str(rec.get("kind") or "")
    st = str(rec.get("source_type") or "")
    sid = str(rec.get("source_id") or "")
    actor = "watchdog"
    try:
        if kind == RecoveryKind.MISSING_COST.value:
            # 비용 백필은 멱등(backfill:stage:* 키) — 안전한 자동 복구.
            res = cost_ledger.backfill_cost_events(store)
            return f"backfill scanned={res.get('scanned', 0)} created={res.get('created', 0)}"
        if kind in (RecoveryKind.FAILED_RESUME.value, RecoveryKind.STUCK_RUN.value):
            if st != "run":
                return "no_safe_retry"
            run = store.get_run(sid)
            if run is None:
                return "source_missing"
            status = str(run.status.value)
            if status == RunStatus.WAITING_APPROVAL.value:
                return _request_run_resume_approval(store, sid, actor=actor)
            # waiting_review/needs_human/queued — 자동 경로 없음.
            # queued는 reaper가 담당하므로 여기서는 관측만 남긴다.
            return f"no_safe_retry status={status}"
        if kind == RecoveryKind.STRANDED_TASK.value:
            # 외부 agent는 pull 기반이라 서버가 강제 wake할 수 없고,
            # workflow agent wake는 schedule 콜백 없이는 run만 queued로
            # 남는다 — 안전한 자동 재시도 없이 알림만 남긴다.
            task = store.get_task(sid)
            assignee = str((task or {}).get("assignee_agent_id") or "")
            mgmt.emit_event(
                store,
                "error",
                task_id=sid,
                agent_id=assignee or None,
                actor=actor,
                summary=f"stranded task still in_progress: {sid}",
                detail={"recovery_id": rec.get("id"), "assignee": assignee},
                idempotency_key=f"recovery-alert:{rec.get('id')}:{rec.get('attempt_count')}",
            )
            return "alerted"
        if kind == RecoveryKind.STALE_HEARTBEAT.value:
            mgmt.emit_event(
                store,
                "error",
                agent_id=sid,
                actor=actor,
                summary=f"agent heartbeat stale: {sid}",
                detail={"recovery_id": rec.get("id")},
                idempotency_key=f"recovery-alert:{rec.get('id')}:{rec.get('attempt_count')}",
            )
            return "alerted"
        return "unknown_kind"
    except Exception as e:
        # spec H: recovery failure — recovery id, exception class, safe code.
        logger.warning(
            "recovery attempt failed id=%s kind=%s error=%s: %s",
            rec.get("id"),
            kind,
            type(e).__name__,
            str(e)[:200],
        )
        return f"error:{type(e).__name__}"


# ---------------- detector ----------------


def _detect(store: Store, *, now: datetime, budget: int) -> dict[str, int]:
    """5개 detector — 새 문제를 recovery_actions로 기록(dedupe 내장).

    예산 규칙(BLOCK-2 수정): 생성 시도는 was_created=True만 소비한다 —
    이미 open recovery가 있는 만성 소스의 dedupe SELECT는 무료라
    batch를 잠식하지 않는다. 또한 detector별 쿼터(ceil(budget/5))를 둬
    앞선 detector의 신규 소스 폭주가 뒤 detector를 굶기지 않게 한다.
    반환: {"detected": 평가한 후보 수, "created": 실제 신규 수}."""
    detected = 0
    created = 0
    created_by_kind: dict[str, int] = {}
    per_kind = max(1, -(-budget // len(_KIND_MAX_ATTEMPTS)))
    candidates: list[tuple[str, str, str, int]] = []

    hb_cutoff = _heartbeat_cutoff(now)
    # detector 조회 자체를 bound — stale agent가 수천 개여도 후보 리스트와
    # 쿼리 결과가 batch 크기에 비례하도록 (가장 오래된 것부터).
    for a in store.agents_with_stale_heartbeat(hb_cutoff, limit=max(budget * 4, 200)):
        candidates.append(
            (
                "agent",
                str(a.get("id") or ""),
                RecoveryKind.STALE_HEARTBEAT.value,
                _KIND_MAX_ATTEMPTS[RecoveryKind.STALE_HEARTBEAT.value],
            )
        )
    for t in store.stranded_in_progress_tasks():
        candidates.append(
            (
                "task",
                str(t.get("id") or ""),
                RecoveryKind.STRANDED_TASK.value,
                _KIND_MAX_ATTEMPTS[RecoveryKind.STRANDED_TASK.value],
            )
        )
    stuck_cutoff = _stuck_cutoff(now)
    for r in store.stuck_runs(stuck_cutoff):
        candidates.append(
            (
                "run",
                str(getattr(r, "id", "") or ""),
                RecoveryKind.STUCK_RUN.value,
                _KIND_MAX_ATTEMPTS[RecoveryKind.STUCK_RUN.value],
            )
        )
    for r in store.terminal_runs_missing_cost():
        candidates.append(
            (
                "run",
                str(getattr(r, "id", "") or ""),
                RecoveryKind.MISSING_COST.value,
                _KIND_MAX_ATTEMPTS[RecoveryKind.MISSING_COST.value],
            )
        )
    since = _iso(now - timedelta(hours=_FAILED_RESUME_WINDOW_HOURS)) or ""
    for r in store.runs_with_failed_resume(since):
        candidates.append(
            (
                "run",
                str(getattr(r, "id", "") or ""),
                RecoveryKind.FAILED_RESUME.value,
                _KIND_MAX_ATTEMPTS[RecoveryKind.FAILED_RESUME.value],
            )
        )

    for st, sid, kind, max_att in candidates:
        if not sid:
            continue
        detected += 1
        # dedupe/쿼터 초과 후보는 create를 호출하지 않는다 — open recovery
        # 조회는 어차피 매 cycle health 재검사가 담당한다.
        if created >= budget or created_by_kind.get(kind, 0) >= per_kind:
            continue
        # spec 003 FR-019 — recovery는 source와 같은 조직에 기록한다.
        # ctx='' cycle에서 source 조회·create 모두 source org로 스코프.
        src_oid = _source_org_id(store, st, sid)
        try:
            if src_oid and orgctx.current_org_id() != src_oid:
                with orgctx.org_scope(src_oid):
                    rec, was_created = store.create_recovery_action(
                        source_type=st,
                        source_id=sid,
                        kind=kind,
                        max_attempts=max_att,
                        owner_type=RecoveryOwnerType.SYSTEM.value,
                    )
            else:
                rec, was_created = store.create_recovery_action(
                    source_type=st,
                    source_id=sid,
                    kind=kind,
                    max_attempts=max_att,
                    owner_type=RecoveryOwnerType.SYSTEM.value,
                )
        except Exception:
            logger.warning("recovery create failed %s:%s", st, sid, exc_info=True)
            continue
        if was_created and rec is not None:
            created += 1
            created_by_kind[kind] = created_by_kind.get(kind, 0) + 1
            logger.info("recovery created id=%s kind=%s source=%s:%s", rec.get("id"), kind, st, sid)
    return {"detected": detected, "created": created}


def run_cycle(
    store: Store,
    *,
    batch_limit: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """watchdog 1 cycle — spec H 로그: scanned/detected/retried/escalated.

    반환 counts dict — API/테스트가 그대로 노출한다.

    중복 실행 방지: module-level non-blocking lock — automation loop과
    cron tick(서로 다른 스레드/프로세스 요청)이 겹쳐도 cycle은 1개만
    돈다. 후발 호출은 skipped=True로 즉시 반환한다."""
    if not _CYCLE_LOCK.acquire(blocking=False):
        logger.info("watchdog cycle skipped: already running")
        return {"skipped": True, "reason": "already_running"}
    try:
        return _run_cycle_locked(store, batch_limit=batch_limit, now=now)
    finally:
        _CYCLE_LOCK.release()


def _run_cycle_locked(
    store: Store,
    *,
    batch_limit: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    limit = batch_limit if batch_limit is not None else watchdog_batch_limit()
    limit = max(1, int(limit))
    counts = {
        "scanned": 0,
        "detected": 0,
        "created": 0,
        "retried": 0,
        "escalated": 0,
        "resolved": 0,
        "batch_limit": limit,
    }
    # 만료된 승인은 읽기 전 확정 — watchdog가 pending 부풀림을 막는다.
    try:
        counts["expired_approvals"] = approvals_svc.expire_pending(store)
    except Exception:
        logger.warning("approval expiry sweep failed", exc_info=True)

    # 1) detector — 새 문제를 recovery로 기록. dedupe는 budget을 소비하지
    #    않고 detector별 쿼터가 공정성을 보장한다(BLOCK-2).
    det = _detect(store, now=now, budget=limit)
    counts["detected"] += det["detected"]
    counts["created"] += det["created"]

    # 2) open recovery 건강 재검사 + due 복구 시도 — detection backlog과
    #    무관하게 항상 실행한다. attempt 수만 batch 상한을 공유한다.
    #
    #    2-pass 구조: pass 1은 모든 open recovery의 건강 재검사(회복 감지
    #    resolved는 attempt 예산과 무관). pass 2는 due 재시도만 attempt
    #    상한까지 — next_attempt_at이 잡힌 예약 재시도를 미시도(NULL)
    #    건보다 먼저 처리해 만성 backlog에 밀리지 않게 한다.
    try:
        open_recs = store.list_open_recovery_actions(limit=max(limit * 4, 100))
    except Exception:
        logger.warning("watchdog open recovery list failed", exc_info=True)
        open_recs = []
    counts["scanned"] += len(open_recs)

    due: list[tuple[datetime | None, dict[str, Any]]] = []
    for rec in open_recs:
        rid = str(rec.get("id") or "")
        # 건강 재검사 — 회복됐으면 resolved로 닫는다(attempt budget 무관,
        # resolved/cancelled/escalated 포함 모든 open 상태 대상).
        # spec 003 FR-019 — rec의 org로 source 조회를 스코프한다.
        with _rec_scope(rec):
            recovered = _source_recovered(store, rec, now=now)
        if recovered:
            if store.set_recovery_status(
                rid,
                RecoveryStatus.RESOLVED.value,
                resolution_note="source recovered",
            ):
                counts["resolved"] += 1
                logger.info("recovery resolved id=%s source=%s", rid, _source_label(rec))
            continue
        # escalated는 사람 결정 대기 — 자동 재시도하지 않는다(건강
        # 재검사로 resolved되는 것만 허용).
        if str(rec.get("status") or "") == RecoveryStatus.ESCALATED.value:
            continue
        # due 판정 — next_attempt_at이 미래면 이번 cycle은 건너뛴다.
        nat = _iso_dt(str(rec.get("next_attempt_at") or ""))
        if nat is not None and nat > now:
            continue
        due.append((nat, rec))

    # 예약된 재시도(next_attempt_at 과거)를 미시도 건보다 우선 —
    # key: (None 여부, 시각). None은 마지막.
    due.sort(key=lambda item: (item[0] is None, item[0] or now))

    attempts_left = limit
    for _nat, rec in due:
        if attempts_left <= 0:
            break
        rid = str(rec.get("id") or "")
        attempts_left -= 1
        # spec 003 FR-019 — 복구 시도가 만드는 approval/event/inbox/cost
        # 쓰기가 rec의 조직에 귀속되도록 스코프한다.
        with _rec_scope(rec):
            outcome = _attempt_recovery(store, rec, now=now)
        counts["retried"] += 1
        attempts = int(rec.get("attempt_count") or 0) + 1
        max_att = int(rec.get("max_attempts") or 3)
        # attempt 결과 기록 + 다음 시도 예약(지수 백오프).
        nxt = _iso(now + timedelta(minutes=_retry_delay_minutes(attempts)))
        store.record_recovery_attempt(
            rid,
            outcome=outcome[:200],
            next_attempt_at=nxt,
        )
        logger.info(
            "watchdog recovery attempt id=%s kind=%s outcome=%s attempts=%d/%d",
            rid,
            rec.get("kind"),
            outcome[:80],
            attempts,
            max_att,
        )
        if attempts >= max_att:
            with _rec_scope({**rec, "attempt_count": attempts}):
                _escalate(
                    store,
                    {**rec, "attempt_count": attempts},
                    note=f"max_attempts={max_att} 초과 — 마지막 outcome: {outcome[:120]}",
                )
            counts["escalated"] += 1

    logger.info("watchdog cycle done %s", counts)
    return counts
