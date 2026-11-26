"""Routine 서비스 — 스케줄 검증, next-run 계산(DST 정책), 발사 로직.

paperclip spec 3차 C/G. croniter 의존 없이 interval|cron_daily|cron_weekly
3종의 next-run을 계산한다. 모든 시각은 UTC ISO-8601로 저장하고, cron 계열은
routine.timezone(IANA) 기준 로컬 시각을 UTC로 변환한다.

DST 정책 (명시):
- ambiguous(가을 fallback으로 같은 로컬 시각이 2번 발생): 첫 번째 발생
  (fold=0, 더 이른 UTC)을 선택한다.
- nonexistent(봄 forward로 로컬 시각이 존재하지 않음): 같은 날짜에서
  최대 180분 forward-scan해 처음 유효한 로컬 시각으로 이동한다
  (예: 02:30 → 03:00). 180분 내 유효 시각이 없으면 그 날은 발생 없음.
- interval은 timezone을 쓰지 않는다(anchor=next_run_at 격자).

missed schedule: 다음 발생 시각을 '지금 이전 가장 최근 1회'로만 발사하고
next_run_at은 '지금 이후 첫 발생'으로 진행한다 — catch-up 루프 없음.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..models import RoutineRunStatus, ScheduleKind, TaskPriority, TaskStatus
from ..store.db import Store
from ..store.util import _dt, _iso, _now
from . import agent_management as mgmt

logger = logging.getLogger(__name__)

_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
# nonexistent 로컬 시각 forward-scan 상한 — 실제 DST gap은 보통 1~2시간.
_DST_SCAN_MINUTES = 180
# cron kind의 날짜 탐색 윈도우 — weekly 최대 7일 + DST 경계 여유.
_CRON_WINDOW_DAYS = 9

_SCHEDULE_KINDS = {k.value for k in ScheduleKind}

# task_template에 허용하는 task 필드 — create_task 인자로 매핑된다.
_TEMPLATE_TASK_FIELDS = ("title", "description", "status", "priority")
# routine이 만드는 task의 초기 status는 비종결 상태만 허용 — 'done'/
# 'cancelled' 템플릿은 매 발사마다 종결 task를 양산한다.
_TEMPLATE_ALLOWED_STATUS = {
    TaskStatus.BACKLOG.value,
    TaskStatus.TODO.value,
    TaskStatus.IN_PROGRESS.value,
    TaskStatus.IN_REVIEW.value,
    TaskStatus.WAITING_APPROVAL.value,
    TaskStatus.BLOCKED.value,
}
_TEMPLATE_ALLOWED_PRIORITY = {p.value for p in TaskPriority}


class RoutineError(Exception):
    """routine 검증/충돌 오류 — 라우터가 status_code/code/message로 매핑."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def validate_timezone(tz: str) -> str:
    """IANA timezone 검증 — ZoneInfoNotFoundError 패턴(event_validation.py)."""
    name = (tz or "").strip()
    if not name:
        raise RoutineError(422, "validation", "timezone is required")
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise RoutineError(422, "validation", f"invalid timezone: {name}") from e
    return name


def _parse_hhmm(value: Any, field: str = "time") -> tuple[int, int]:
    s = str(value or "").strip()
    m = _HHMM_RE.match(s)
    if not m:
        raise RoutineError(422, "validation", f"schedule.{field} must be HH:MM (24h), got {s!r}")
    return int(m.group(1)), int(m.group(2))


def validate_schedule(schedule_kind: str, schedule: dict[str, Any] | None) -> dict[str, Any]:
    """schedule_kind별 schedule JSON 정규화 — 실패 시 RoutineError(422)."""
    kind = (schedule_kind or "").strip().lower()
    if kind not in _SCHEDULE_KINDS:
        raise RoutineError(
            422,
            "validation",
            f"schedule_kind must be one of {sorted(_SCHEDULE_KINDS)}",
        )
    sched = dict(schedule or {})
    if kind == ScheduleKind.INTERVAL.value:
        raw = sched.get("minutes", sched.get("interval_minutes"))
        if raw is None:
            raise RoutineError(422, "validation", "interval schedule requires integer 'minutes'")
        try:
            minutes = int(raw)
        except (TypeError, ValueError) as e:
            raise RoutineError(
                422, "validation", "interval schedule requires integer 'minutes'"
            ) from e
        if minutes < 1 or minutes > 525_600:  # 최대 1년
            raise RoutineError(422, "validation", "interval minutes must be 1..525600")
        return {"minutes": minutes}
    hh, mm = _parse_hhmm(sched.get("time"))
    if kind == ScheduleKind.CRON_DAILY.value:
        return {"time": f"{hh:02d}:{mm:02d}"}
    # cron_weekly — weekday 0=Monday..6=Sunday (date.weekday() 규약)
    raw_wd = sched.get("weekday", sched.get("wday"))
    if raw_wd is None:
        raise RoutineError(
            422, "validation", "weekly schedule requires integer 'weekday' (0=Mon..6=Sun)"
        )
    try:
        wd = int(raw_wd)
    except (TypeError, ValueError) as e:
        raise RoutineError(
            422, "validation", "weekly schedule requires integer 'weekday' (0=Mon..6=Sun)"
        ) from e
    if wd < 0 or wd > 6:
        raise RoutineError(422, "validation", "weekday must be 0..6")
    return {"weekday": wd, "time": f"{hh:02d}:{mm:02d}"}


# ---------- local→UTC 변환 (DST 정책 적용) ----------


def _utc_candidates(tz: ZoneInfo, naive: datetime) -> list[datetime]:
    """naive 로컬 시각에 대응하는 유효 UTC instant 목록(오름차순).

    fold=0/1 양쪽을 시도해 round-trip 검증한다 — 존재하지 않는 시각
    (spring-forward gap)은 어느 fold로도 복원되지 않아 후보가 0개,
    ambiguous(fall-back 중복)는 2개가 나온다."""
    cands: set[datetime] = set()
    for fold in (0, 1):
        aware = naive.replace(tzinfo=tz, fold=fold)
        back = aware.astimezone(UTC).astimezone(tz)
        if back.replace(tzinfo=None) == naive:
            cands.add(aware.astimezone(UTC))
    return sorted(cands)


def resolve_local(tz: ZoneInfo, naive: datetime) -> datetime | None:
    """DST 정책 적용 — naive 로컬 시각 → 단일 UTC instant.

    - ambiguous: 첫 번째 발생(fold=0 = 더 이른 UTC).
    - nonexistent: 같은 날 최대 180분 forward-scan해 첫 유효 시각.
    - 그래도 없으면 None(그 슬롯은 발생하지 않음)."""
    cands = _utc_candidates(tz, naive)
    if cands:
        return cands[0]
    # nonexistent — 같은 로컬 날짜 안에서만 forward-scan한다. 날짜를 넘는
    # 스캔은 '없는 슬롯'을 다음 날 슬롯으로 잘못 해석한다(예: 23:30 nonexistent
    # → 익일 00:30). cur가 다음 날로 넘어가면 즉시 중단한다.
    cur = naive
    for _ in range(_DST_SCAN_MINUTES):
        cur += timedelta(minutes=1)
        if cur.date() != naive.date():
            break
        cands = _utc_candidates(tz, cur)
        if cands:
            return cands[0]
    return None


def _cron_slot_instant(
    kind: str, hh: int, mm: int, weekday: int | None, tz: ZoneInfo, local_date: Any
) -> datetime | None:
    """주어진 로컬 날짜의 cron 슬롯 instant — kind/weekday 불일치 시 None."""
    if kind == ScheduleKind.CRON_WEEKLY.value and local_date.weekday() != weekday:
        return None
    naive = datetime(local_date.year, local_date.month, local_date.day, hh, mm)
    return resolve_local(tz, naive)


def _cron_instants(
    kind: str, sched: dict[str, Any], tz: ZoneInfo, anchor: datetime
) -> list[datetime]:
    """anchor 로컬 날짜 ±_CRON_WINDOW_DAYS 범위의 슬롯 instant 목록."""
    hh, mm = _parse_hhmm(sched.get("time"))
    weekday = int(sched["weekday"]) if kind == ScheduleKind.CRON_WEEKLY.value else None
    local_anchor = anchor.astimezone(tz).date()
    out: list[datetime] = []
    for delta in range(-_CRON_WINDOW_DAYS, _CRON_WINDOW_DAYS + 1):
        d = local_anchor + timedelta(days=delta)
        inst = _cron_slot_instant(kind, hh, mm, weekday, tz, d)
        if inst is not None:
            out.append(inst)
    return sorted(out)


# ---------- next-run 계산 ----------


def first_run_after(
    routine: dict[str, Any], after: datetime, *, anchor: datetime | None = None
) -> datetime | None:
    """after 이후 첫 발생 UTC instant. interval은 anchor 격자 기준."""
    kind = str(routine.get("schedule_kind") or ScheduleKind.INTERVAL.value)
    sched = routine.get("schedule") or {}
    tz = ZoneInfo(str(routine.get("timezone") or "UTC"))
    if kind == ScheduleKind.INTERVAL.value:
        minutes = int(sched.get("minutes") or 0)
        if minutes < 1:
            return None
        base = anchor
        if base is None:
            # anchor 없음(신규 생성/재활성화) — schedules와 동일하게
            # now+interval이 첫 발생이다.
            return after + timedelta(minutes=minutes)
        if after <= base:
            return base
        elapsed = (after - base).total_seconds()
        k = int(elapsed // (minutes * 60)) + 1
        return base + timedelta(minutes=minutes * k)
    instants = _cron_instants(kind, sched, tz, after)
    for inst in instants:
        if inst > after:
            return inst
    return None


def latest_on_or_before(
    routine: dict[str, Any], at: datetime, *, anchor: datetime | None = None
) -> datetime | None:
    """at 이전(포함) 가장 최근 발생 UTC instant — missed schedule 1회 정책용."""
    kind = str(routine.get("schedule_kind") or ScheduleKind.INTERVAL.value)
    sched = routine.get("schedule") or {}
    tz = ZoneInfo(str(routine.get("timezone") or "UTC"))
    if kind == ScheduleKind.INTERVAL.value:
        minutes = int(sched.get("minutes") or 0)
        base = anchor or at
        if minutes < 1 or at < base:
            return None
        k = int((at - base).total_seconds() // (minutes * 60))
        return base + timedelta(minutes=minutes * k)
    instants = _cron_instants(kind, sched, tz, at)
    latest: datetime | None = None
    for inst in instants:
        if inst <= at:
            latest = inst
        else:
            break
    return latest


# ---------- 서비스 진입점 ----------


def _validate_template(template: dict[str, Any] | None) -> dict[str, Any]:
    """task_template 화이트리스트 정규화 — 허용 키만 남기고 status/priority
    값도 어휘 검증한다(비종결 status만 — 종결 task 양산 방지)."""
    tmpl = dict(template or {})
    out: dict[str, Any] = {}
    for k in _TEMPLATE_TASK_FIELDS:
        v = tmpl.get(k)
        if v is None:
            continue
        out[k] = str(v).strip()[: 2000 if k == "description" else 200]
    if out.get("status") and out["status"] not in _TEMPLATE_ALLOWED_STATUS:
        raise RoutineError(
            422,
            "validation",
            f"task_template.status must be one of {sorted(_TEMPLATE_ALLOWED_STATUS)}",
        )
    if out.get("priority") and out["priority"] not in _TEMPLATE_ALLOWED_PRIORITY:
        raise RoutineError(
            422,
            "validation",
            f"task_template.priority must be one of {sorted(_TEMPLATE_ALLOWED_PRIORITY)}",
        )
    return out


def _validate_assignee(store: Store, assignee_agent_id: Any) -> str | None:
    aid = str(assignee_agent_id or "").strip()[:64]
    if not aid:
        return None
    agent = store.get_agent(aid)
    if agent is None:
        raise RoutineError(422, "validation", "assignee agent not found")
    return aid


def _validate_project(store: Store, project_id: Any) -> str | None:
    pid = str(project_id or "").strip()[:64]
    if not pid:
        return None
    if store.get_project(pid) is None:
        raise RoutineError(422, "validation", "project not found")
    return pid


def create_routine(
    store: Store,
    *,
    name: str,
    description: str = "",
    project_id: Any = None,
    task_template: dict[str, Any] | None = None,
    assignee_agent_id: Any = None,
    schedule_kind: str,
    schedule: dict[str, Any] | None,
    timezone: str = "UTC",
    max_concurrent: Any = 1,
    enabled: bool = True,
    created_by: str = "",
) -> dict[str, Any]:
    """routine 생성 — 검증 + 초기 next_run_at 계산 후 저장."""
    if not (name or "").strip():
        raise RoutineError(422, "validation", "name must not be empty")
    tz_name = validate_timezone(timezone)
    kind = (schedule_kind or "").strip().lower()
    sched = validate_schedule(kind, schedule)
    try:
        maxc = int(max_concurrent) if max_concurrent is not None else 1
    except (TypeError, ValueError) as e:
        raise RoutineError(422, "validation", "max_concurrent must be an integer") from e
    if maxc < 1:
        raise RoutineError(422, "validation", "max_concurrent must be >= 1")
    aid = _validate_assignee(store, assignee_agent_id)
    pid = _validate_project(store, project_id)
    tmpl = _validate_template(task_template)

    now = _now()
    routine_stub = {
        "schedule_kind": kind,
        "schedule": sched,
        "timezone": tz_name,
    }
    nxt = first_run_after(routine_stub, now) if enabled else None
    if enabled and nxt is None:
        raise RoutineError(422, "validation", "cannot compute next_run_at for schedule")
    return store.create_routine(
        name=name,
        description=description,
        project_id=pid,
        task_template=tmpl,
        assignee_agent_id=aid,
        schedule_kind=kind,
        schedule=sched,
        timezone=tz_name,
        max_concurrent=maxc,
        enabled=enabled,
        next_run_at=_iso(nxt) if nxt else None,
        created_by=created_by,
    )


def _merged_schedule_fields(
    routine: dict[str, Any], fields: dict[str, Any]
) -> tuple[str, dict[str, Any], str]:
    """patch 병합 — schedule 관련 필드가 바뀌면 next_run_at 재계산이 필요."""
    kind = str(fields.get("schedule_kind") or routine.get("schedule_kind") or "interval")
    sched_raw = fields.get("schedule")
    sched = sched_raw if isinstance(sched_raw, dict) else (routine.get("schedule") or {})
    tz = str(fields.get("timezone") or routine.get("timezone") or "UTC")
    return kind, dict(sched), tz


def update_routine(
    store: Store,
    routine_id: str,
    fields: dict[str, Any],
    *,
    actor: str = "",
) -> dict[str, Any]:
    """routine 부분 갱신 — schedule/timezone/kind/enabled 변경 시 next_run_at 재계산.

    비활성→활성 전환은 set_schedule_enabled와 동일하게 now 기준 첫 발생으로
    재무장한다. schedule 필드 변경은 merge 후 전체 재검증한다."""
    routine = store.get_routine(routine_id)
    if routine is None:
        raise RoutineError(404, "not_found", "routine not found")
    fields = dict(fields)
    # enabled의 명시 null은 '비우기'가 아니라 무시 — 실수로 routine이
    # disable되는 것을 막는다(다른 필수값 필드와 동일한 null 거부 취지).
    if "enabled" in fields and fields["enabled"] is None:
        del fields["enabled"]

    # 참조 검증 — 명시된 필드만 검사한다.
    if "assignee_agent_id" in fields and fields["assignee_agent_id"] is not None:
        _validate_assignee(store, fields["assignee_agent_id"])
    if "project_id" in fields and fields["project_id"] is not None:
        _validate_project(store, fields["project_id"])
    if "task_template" in fields and isinstance(fields["task_template"], dict):
        fields["task_template"] = _validate_template(fields["task_template"])
    if "max_concurrent" in fields:
        try:
            fields["max_concurrent"] = (
                int(fields["max_concurrent"]) if fields["max_concurrent"] is not None else 1
            )
        except (TypeError, ValueError) as e:
            raise RoutineError(422, "validation", "max_concurrent must be an integer") from e
        if int(fields["max_concurrent"]) < 1:
            raise RoutineError(422, "validation", "max_concurrent must be >= 1")

    schedule_touched = any(k in fields for k in ("schedule_kind", "schedule", "timezone"))
    enabled_touched = "enabled" in fields
    if schedule_touched:
        kind, sched_merged, tz = _merged_schedule_fields(routine, fields)
        tz_name = validate_timezone(tz)
        sched_norm = validate_schedule(kind, sched_merged)
        fields["schedule_kind"] = kind
        fields["schedule"] = sched_norm
        fields["timezone"] = tz_name

    # next_run_at 재계산 — schedule 변경 또는 비활성→활성 전환.
    new_enabled = bool(fields.get("enabled", routine.get("enabled")))
    was_enabled = bool(routine.get("enabled"))
    recompute_next = False
    if schedule_touched:
        recompute_next = True
    elif enabled_touched and new_enabled and not was_enabled:
        recompute_next = True
    if recompute_next:
        stub = {
            "schedule_kind": fields.get("schedule_kind") or routine.get("schedule_kind"),
            "schedule": fields.get("schedule") or routine.get("schedule") or {},
            "timezone": fields.get("timezone") or routine.get("timezone") or "UTC",
        }
        if new_enabled:
            nxt = first_run_after(stub, _now())
            if nxt is None:
                raise RoutineError(422, "validation", "cannot compute next_run_at")
            fields["next_run_at"] = _iso(nxt)
        else:
            fields["next_run_at"] = None
    elif enabled_touched and not new_enabled:
        # 명시적 비활성 — next_run_at은 유지하되 enabled=0이라 due 대상 아님.
        pass

    updated = store.update_routine(routine_id, **fields)
    if updated is None:
        raise RoutineError(404, "not_found", "routine not found")
    logger.info(
        "routine updated id=%s actor=%s fields=%s",
        routine_id,
        actor or "-",
        sorted(fields.keys()),
    )
    return updated


def set_routine_enabled(
    store: Store, routine_id: str, enabled: bool, *, actor: str = ""
) -> dict[str, Any]:
    """enable/disable — enable 시 next_run_at을 now 기준 첫 발생으로 재무장."""
    routine = store.get_routine(routine_id)
    if routine is None:
        raise RoutineError(404, "not_found", "routine not found")
    if enabled and not routine.get("enabled"):
        nxt = first_run_after(routine, _now())
        if nxt is None:
            raise RoutineError(422, "validation", "cannot compute next_run_at")
        updated = store.update_routine(routine_id, enabled=True, next_run_at=_iso(nxt))
    else:
        updated = store.update_routine(routine_id, enabled=enabled)
    if updated is None:
        raise RoutineError(404, "not_found", "routine not found")
    logger.info(
        "routine %s id=%s actor=%s",
        "enabled" if enabled else "disabled",
        routine_id,
        actor or "-",
    )
    return updated


# ---------- 발사 ----------


def _task_fields_from_template(routine: dict[str, Any]) -> dict[str, Any]:
    tmpl = routine.get("task_template") or {}
    return {
        "title": str(tmpl.get("title") or f"Routine: {routine.get('name') or 'routine'}"),
        "description": str(tmpl.get("description") or ""),
        "status": str(tmpl.get("status") or "todo"),
        "priority": str(tmpl.get("priority") or "medium"),
    }


def fire_due_routine(
    store: Store,
    routine: dict[str, Any],
    *,
    now: datetime | None = None,
    schedule_run: Any = None,
    actor: str = "routine",
) -> dict[str, Any]:
    """due routine 1건 발사 — claim → routine_run → task 생성 → wake.

    반환 action: claimed(정상 발사)/skipped(max_concurrent)/duplicate
    (idempotency)/claim_lost(CAS 실패 — 다른 tick이 선점).
    schedule_run은 (run, job_payload) → run 실행 예약 콜백 — automation
    loop은 daemon thread, cron tick은 BackgroundTasks defer로 전달된다.
    """
    now = now or _now()
    rid = str(routine.get("id") or "")
    if not rid:
        return {"action": "claim_lost", "reason": "no id"}
    expected_next = str(routine.get("next_run_at") or "")
    anchor = _dt(routine.get("next_run_at"))
    if anchor is None or not expected_next:
        # next_run_at이 손상된 행 — claim하면 next_run_at이 now로 굳어
        # 매 틱 재발사될 수 있으므로 이번 회차는 건너뛴다.
        return {"action": "claim_lost", "routine_id": rid, "reason": "bad next_run_at"}
    if anchor > now:
        # 아직 due가 아닌 routine — due_routines() 경로 외 직접 호출 방어.
        # 미래 발생을 scheduled_for로 잘못 기록하는 것을 막는다.
        return {"action": "not_due", "routine_id": rid}

    # missed schedule은 가장 최근 1회만 — scheduled_for는 now 이하 최신
    # 발생, next_run_at은 now 이후 첫 발생으로 진행한다(catch-up 없음).
    sched_for = latest_on_or_before(routine, now, anchor=anchor)
    if sched_for is None:
        sched_for = anchor or now
    new_next = first_run_after(routine, now, anchor=anchor)
    last_run = now.isoformat()
    next_run = new_next.isoformat() if new_next else None
    if not store.claim_routine_fire(rid, expected_next, last_run, next_run):
        return {"action": "claim_lost", "routine_id": rid}

    idem_key = f"{rid}:{_iso(sched_for)}"
    rr, created = store.create_routine_run(
        routine_id=rid,
        scheduled_for=_iso(sched_for) or "",
        idempotency_key=idem_key,
    )
    if not created or rr is None:
        # 동일 scheduled_for가 이미 기록됨 — 중복 발사 차단(두 번째 방어선).
        return {"action": "duplicate", "routine_id": rid, "routine_run": rr}

    maxc = int(routine.get("max_concurrent") or 1)
    if maxc >= 1 and store.routine_inflight_count(rid) >= maxc:
        store.mark_routine_run(
            str(rr["id"]),
            status=RoutineRunStatus.SKIPPED.value,
            error_code="max_concurrent",
        )
        store.audit(
            "",
            actor,
            "routine_skipped",
            rid,
            {"scheduled_for": _iso(sched_for), "reason": "max_concurrent"},
        )
        logger.info(
            "routine skipped id=%s scheduled_for=%s reason=max_concurrent",
            rid,
            _iso(sched_for),
        )
        return {"action": "skipped", "reason": "max_concurrent", "routine_run": rr}

    # task 생성 — task_template + routine.assignee_agent_id + project/goal
    # 태그. project_id/goal_id를 create_task에 직접 넘겨 scope 참조 검증과
    # assignee task:assign permission(project 스코프)이 발사 시점에 평가되게
    # 한다 — 생성 후 raw UPDATE로 태그를 찍으면 permission 재검사를 우회한다.
    tf = _task_fields_from_template(routine)
    pid = str(routine.get("project_id") or "")
    goal_id = ""
    if pid:
        try:
            proj = store.get_project(pid)
            goal_id = str((proj or {}).get("goal_id") or "")
        except Exception:
            goal_id = ""
    try:
        task = mgmt.create_task(
            store,
            title=tf["title"],
            description=tf["description"],
            status=tf["status"],
            priority=tf["priority"],
            assignee_agent_id=routine.get("assignee_agent_id"),
            project_id=pid or None,
            goal_id=goal_id or None,
            created_by=f"routine:{rid}",
            actor=actor,
        )
    except mgmt.ManagementError as e:
        store.mark_routine_run(
            str(rr["id"]),
            status=RoutineRunStatus.FAILED.value,
            error_code=f"task:{e.code}",
        )
        store.audit(
            "",
            actor,
            "routine_failed",
            rid,
            {"scheduled_for": _iso(sched_for), "error_code": f"task:{e.code}"},
        )
        logger.warning(
            "routine task create failed id=%s scheduled_for=%s code=%s",
            rid,
            _iso(sched_for),
            e.code,
        )
        return {"action": "failed", "reason": e.code, "routine_run": rr}
    except Exception as e:
        # ManagementError 외 예외(DB 오류 등)도 routine_run을 failed로
        # 마감한다 — pending으로 방치하면 routine_runs 이력이 영구
        # '진행 중'으로 남는다(claim은 이미 next_run_at을 전진시켰다).
        err_code = f"task:{type(e).__name__}"
        store.mark_routine_run(
            str(rr["id"]),
            status=RoutineRunStatus.FAILED.value,
            error_code=err_code[:200],
        )
        store.audit(
            "",
            actor,
            "routine_failed",
            rid,
            {"scheduled_for": _iso(sched_for), "error_code": err_code[:200]},
        )
        logger.warning(
            "routine task create crashed id=%s scheduled_for=%s err=%s",
            rid,
            _iso(sched_for),
            str(e)[:200],
        )
        return {"action": "failed", "reason": err_code, "routine_run": rr}

    task_id = str(task["id"])

    # assignee가 있으면 기존 wake 경로 재사용 — 예산 admission은
    # wakeups.admit_run 내부에서 수행된다. idempotency_key는 routine_run id
    # 기반이라 동일 run의 재시도/중복 tick이 run을 중복 생성하지 않는다.
    wake_error = ""
    aid = str(routine.get("assignee_agent_id") or "")
    if aid:
        try:
            mgmt.wake_agent(
                store,
                aid,
                task_id=task_id,
                idempotency_key=f"routine:{rr['id']}",
                actor=actor,
                schedule=schedule_run,
            )
        except Exception as e:
            # task는 생성됐으므로 run 상태는 created 유지 — wake 실패는
            # error_code에만 기록한다(재시도는 사람/다음 회차 담당).
            wake_error = f"wake:{getattr(e, 'code', type(e).__name__)}"
            logger.warning(
                "routine wake failed id=%s task=%s agent=%s err=%s",
                rid,
                task_id,
                aid,
                str(e)[:200],
            )

    store.mark_routine_run(
        str(rr["id"]),
        status=RoutineRunStatus.CREATED.value,
        task_id=task_id,
        error_code=wake_error,
    )
    store.audit(
        "",
        actor,
        "routine_fired",
        rid,
        {
            "scheduled_for": _iso(sched_for),
            "task_id": task_id,
            "routine_run_id": str(rr["id"]),
            "wake_error": wake_error,
        },
    )
    logger.info(
        "routine fired id=%s scheduled_for=%s task=%s run=%s",
        rid,
        _iso(sched_for),
        task_id,
        str(rr["id"]),
    )
    return {
        "action": "claimed",
        "routine_id": rid,
        "scheduled_for": _iso(sched_for),
        "task_id": task_id,
        "routine_run": rr,
        "wake_error": wake_error,
    }
