"""Routine 스케줄 순수 계산 — 검증, next-run 계산(DST 정책).

paperclip spec 3차 C/G. croniter 의존 없이 interval|cron_daily|cron_weekly
3종의 next-run을 계산한다. 모든 시각은 UTC ISO-8601로 저장하고, cron 계열은
routine.timezone(IANA) 기준 로컬 시각을 UTC로 변환한다.

이 모듈은 순수 함수만 담는다 — Store/DB 접근이 없으므로 services/routines.py의
발사·claim 오케스트레이션과 분리되어 있다.

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

import re
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..models import ScheduleKind

_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
# nonexistent 로컬 시각 forward-scan 상한 — 실제 DST gap은 보통 1~2시간.
_DST_SCAN_MINUTES = 180
# cron kind의 날짜 탐색 윈도우 — weekly 최대 7일 + DST 경계 여유.
_CRON_WINDOW_DAYS = 9

_SCHEDULE_KINDS = {k.value for k in ScheduleKind}


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
