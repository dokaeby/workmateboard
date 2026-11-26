"""routines 단위 테스트 — 스케줄 검증, next-run 계산(DST), 발사/멱등.

paperclip spec 3차 C/G. 외부 호출 없이 Store + services/routines만 사용.
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from jobmate.models import RoutineRunStatus
from jobmate.services import routines as svc
from jobmate.store.db import Store

NY = ZoneInfo("America/New_York")
SEOUL = ZoneInfo("Asia/Seoul")


def _store() -> Store:
    db = str(Path(tempfile.mkdtemp()) / "t.db")
    return Store(db)


def _routine(store: Store, **over) -> dict:
    fields = {
        "name": "daily standup",
        "schedule_kind": "interval",
        "schedule": {"minutes": 60},
        "timezone": "UTC",
    }
    fields.update(over)
    return svc.create_routine(store, **fields)


# ---------- schedule/timezone 검증 ----------


def test_validate_schedule_interval():
    assert svc.validate_schedule("interval", {"minutes": 30}) == {"minutes": 30}
    assert svc.validate_schedule("interval", {"interval_minutes": 5}) == {"minutes": 5}
    with pytest.raises(svc.RoutineError):
        svc.validate_schedule("interval", {})
    with pytest.raises(svc.RoutineError):
        svc.validate_schedule("interval", {"minutes": 0})
    with pytest.raises(svc.RoutineError):
        svc.validate_schedule("interval", {"minutes": "abc"})


def test_validate_schedule_cron_kinds():
    assert svc.validate_schedule("cron_daily", {"time": "09:30"}) == {"time": "09:30"}
    assert svc.validate_schedule("cron_weekly", {"weekday": 0, "time": "09:00"}) == {
        "weekday": 0,
        "time": "09:00",
    }
    with pytest.raises(svc.RoutineError):
        svc.validate_schedule("cron_daily", {"time": "25:00"})
    with pytest.raises(svc.RoutineError):
        svc.validate_schedule("cron_daily", {"time": "9am"})
    with pytest.raises(svc.RoutineError):
        svc.validate_schedule("cron_weekly", {"weekday": 7, "time": "09:00"})
    with pytest.raises(svc.RoutineError):
        svc.validate_schedule("cron_weekly", {"time": "09:00"})
    with pytest.raises(svc.RoutineError):
        svc.validate_schedule("bogus", {"minutes": 1})


def test_validate_timezone_iana():
    assert svc.validate_timezone("Asia/Seoul") == "Asia/Seoul"
    with pytest.raises(svc.RoutineError):
        svc.validate_timezone("Mars/Olympus")
    with pytest.raises(svc.RoutineError):
        svc.validate_timezone("")


def test_create_routine_rejects_bad_refs():
    store = _store()
    with pytest.raises(svc.RoutineError):
        svc.create_routine(
            store,
            name="x",
            schedule_kind="interval",
            schedule={"minutes": 1},
            assignee_agent_id="nope",
        )
    with pytest.raises(svc.RoutineError):
        svc.create_routine(
            store,
            name="x",
            schedule_kind="interval",
            schedule={"minutes": 1},
            project_id="nope",
        )
    with pytest.raises(svc.RoutineError):
        svc.create_routine(store, name="", schedule_kind="interval", schedule={"minutes": 1})
    with pytest.raises(svc.RoutineError):
        svc.create_routine(
            store,
            name="x",
            schedule_kind="interval",
            schedule={"minutes": 1},
            max_concurrent=0,
        )


# ---------- DST / next-run 계산 ----------


def test_interval_next_run():
    store = _store()
    r = _routine(store, schedule={"minutes": 60})
    nxt = datetime.fromisoformat(r["next_run_at"])
    created = datetime.fromisoformat(r["created_at"])
    assert nxt > created
    assert (nxt - created).total_seconds() <= 3600 + 1


def test_cron_daily_next_run_utc():
    """Asia/Seoul 09:00 daily → UTC 00:00 (KST=UTC+9)."""
    store = _store()
    r = _routine(
        store,
        schedule_kind="cron_daily",
        schedule={"time": "09:00"},
        timezone="Asia/Seoul",
    )
    nxt = datetime.fromisoformat(r["next_run_at"])
    local = nxt.astimezone(SEOUL)
    assert (local.hour, local.minute) == (9, 0)
    assert nxt.utcoffset() == timedelta(0)


def test_dst_spring_forward_nonexistent_skips_forward():
    """America/New_York 2025-03-09 02:30은 존재하지 않음 → 03:00(첫 유효)으로."""
    naive = datetime(2025, 3, 9, 2, 30)
    inst = svc.resolve_local(NY, naive)
    assert inst is not None
    back = inst.astimezone(NY)
    # gap(02:00→03:00) 직후 첫 유효 로컬 시각
    assert (back.hour, back.minute) == (3, 0)
    assert back.date().isoformat() == "2025-03-09"


def test_dst_fall_back_ambiguous_first_occurrence():
    """America/New_York 2025-11-02 01:30은 2번 발생 → 첫 번째(EDT, UTC-4)."""
    naive = datetime(2025, 11, 2, 1, 30)
    inst = svc.resolve_local(NY, naive)
    assert inst is not None
    back = inst.astimezone(NY)
    assert (back.hour, back.minute) == (1, 30)
    # 첫 발생 = EDT (UTC-4) → 05:30 UTC. 두 번째는 EST(UTC-5) → 06:30 UTC.
    assert inst == datetime(2025, 11, 2, 5, 30, tzinfo=UTC)


def test_cron_daily_across_dst_boundary():
    """daily 02:30 America/New_York — spring-forward 당일은 03:00으로 이동."""
    routine = {
        "schedule_kind": "cron_daily",
        "schedule": {"time": "02:30"},
        "timezone": "America/New_York",
    }
    # 2025-03-08 12:00 UTC = 03-08 07:00 EST — 다음 발생은 03-09.
    after = datetime(2025, 3, 8, 12, 0, tzinfo=UTC)
    nxt = svc.first_run_after(routine, after)
    assert nxt is not None
    local = nxt.astimezone(NY)
    assert local.date().isoformat() == "2025-03-09"
    assert (local.hour, local.minute) == (3, 0)  # nonexistent → skip forward


def test_cron_weekly_weekday():
    routine = {
        "schedule_kind": "cron_weekly",
        "schedule": {"weekday": 0, "time": "09:00"},  # Monday
        "timezone": "UTC",
    }
    # 2025-03-05는 수요일 — 다음 월요일은 03-10.
    after = datetime(2025, 3, 5, 12, 0, tzinfo=UTC)
    nxt = svc.first_run_after(routine, after)
    assert nxt is not None
    assert nxt.weekday() == 0
    assert nxt.date().isoformat() == "2025-03-10"
    assert (nxt.hour, nxt.minute) == (9, 0)


def test_missed_schedule_latest_once_only():
    """interval 60분, anchor가 3시간 전 — scheduled_for는 가장 최근 격자점 1개."""
    store = _store()
    r = _routine(store, schedule={"minutes": 60})
    # next_run_at을 3시간 전으로 강제 — 3회분 missed.
    old_next = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
    store.update_routine(r["id"], next_run_at=old_next)
    routine = store.get_routine(r["id"])
    assert routine is not None
    res = svc.fire_due_routine(store, routine, schedule_run=lambda r_, p: None)
    assert res["action"] == "claimed"
    sched_for = datetime.fromisoformat(res["scheduled_for"])
    # now 이전 가장 최근 60분 격자점 — anchor + 3*60min 지점이어야 한다.
    anchor = datetime.fromisoformat(old_next)
    assert sched_for == anchor + timedelta(hours=3)
    # routine_runs는 정확히 1건 — catch-up 없음.
    runs = store.list_routine_runs(r["id"])
    assert len(runs) == 1
    # next_run_at은 now 이후 — 과거 격자로 되돌아가지 않는다.
    updated = store.get_routine(r["id"])
    assert datetime.fromisoformat(updated["next_run_at"]) > datetime.now(UTC) - timedelta(seconds=5)


# ---------- store CRUD / CAS / idempotency ----------


def test_crud_and_filters():
    store = _store()
    r1 = _routine(store, name="a")
    r2 = _routine(store, name="b", enabled=False)
    assert store.get_routine(r1["id"])["name"] == "a"
    assert r1["status"] == "active" and r2["status"] == "paused"
    assert len(store.list_routines()) == 2
    assert [x["id"] for x in store.list_routines(enabled=True)] == [r1["id"]]
    assert store.update_routine(r1["id"], name="a2")["name"] == "a2"
    assert store.delete_routine(r2["id"]) is True
    assert store.get_routine(r2["id"]) is None


def test_claim_cas_blocks_double_fire():
    """automation loop과 cron tick이 동시에 due routine을 읽어도 claim은 1회."""
    store = _store()
    r = _routine(store)
    past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    store.update_routine(r["id"], next_run_at=past)
    nxt = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    # 두 번 claim 시도 — 두 번째는 expected_next_run_at 불일치로 실패.
    assert store.claim_routine_fire(r["id"], past, datetime.now(UTC).isoformat(), nxt)
    assert not store.claim_routine_fire(r["id"], past, datetime.now(UTC).isoformat(), nxt)


def test_routine_run_idempotency_dedupe():
    store = _store()
    r = _routine(store)
    rr1, c1 = store.create_routine_run(
        routine_id=r["id"],
        scheduled_for="2025-01-01T00:00:00+00:00",
        idempotency_key=f"{r['id']}:2025-01-01T00:00:00+00:00",
    )
    rr2, c2 = store.create_routine_run(
        routine_id=r["id"],
        scheduled_for="2025-01-01T00:00:00+00:00",
        idempotency_key=f"{r['id']}:2025-01-01T00:00:00+00:00",
    )
    assert c1 is True and c2 is False
    assert rr1["id"] == rr2["id"]


def test_mark_routine_run_statuses():
    store = _store()
    r = _routine(store)
    rr, _ = store.create_routine_run(routine_id=r["id"], scheduled_for="s", idempotency_key="k1")
    out = store.mark_routine_run(rr["id"], status=RoutineRunStatus.CREATED.value, task_id="t1")
    assert out["status"] == "created" and out["task_id"] == "t1"
    assert out["finished_at"] is not None
    # 잘못된 status는 거부
    assert store.mark_routine_run(rr["id"], status="bogus") is None


# ---------- 발사: task 생성 + wake + max_concurrent ----------


def _mk_agent(store: Store, name: str = "dev") -> dict:
    agent = store.create_agent(
        name,
        "claude",
        kind="workflow",
        adapter_config={"workflow": "dev_review"},
    )
    assert agent is not None
    return agent


def test_fire_creates_task_and_wake_once():
    """due routine 발사 → task 1개 생성 + assignee wake 1회(run 예약 콜백)."""
    store = _store()
    agent = _mk_agent(store)
    r = _routine(
        store,
        assignee_agent_id=agent["id"],
        task_template={"title": "standup", "priority": "high"},
        schedule={"minutes": 30},
    )
    # next_run_at을 과거로 — 즉시 due.
    past = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    store.update_routine(r["id"], next_run_at=past)
    routine = store.get_routine(r["id"])
    scheduled: list = []
    res = svc.fire_due_routine(
        store, routine, schedule_run=lambda run, payload: scheduled.append(run.id)
    )
    assert res["action"] == "claimed"
    task = store.get_task(res["task_id"])
    assert task is not None
    assert task["title"] == "standup"
    assert task["assignee_agent_id"] == agent["id"]
    assert task["project_id"] is None
    # wake → admit_run이 run을 만들고 schedule 콜백이 1회 호출돼야 한다.
    assert len(scheduled) == 1
    # routine_run은 created + task_id 연결.
    rr = store.list_routine_runs(r["id"])
    assert len(rr) == 1 and rr[0]["status"] == "created"
    assert rr[0]["task_id"] == task["id"]
    # 같은 routine 재발사 — next_run_at이 미래라 due 아니므로 직접 재호출해도
    # claim CAS가 막는다(expected_next 불일치).
    res2 = svc.fire_due_routine(store, store.get_routine(r["id"]), schedule_run=lambda a, b: None)
    assert res2["action"] == "not_due"
    assert len(store.list_routine_runs(r["id"])) == 1


def test_fire_idempotent_same_scheduled_for():
    """동일 routine/scheduled_for 재시도는 routine_run UNIQUE로 차단."""
    store = _store()
    r = _routine(store, schedule={"minutes": 30})
    past = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    store.update_routine(r["id"], next_run_at=past)
    routine = store.get_routine(r["id"])
    # 첫 발사 후 next_run_at을 다시 과거의 같은 scheduled_for가 되도록 강제할 수
    # 없으므로(격자가 진행), 직접 routine_run 중복 생성을 검증한다.
    res = svc.fire_due_routine(store, routine)
    assert res["action"] == "claimed"
    # 동일 idempotency key 재삽입 → created=False
    key = f"{r['id']}:{res['scheduled_for']}"
    _, created = store.create_routine_run(
        routine_id=r["id"], scheduled_for=res["scheduled_for"], idempotency_key=key
    )
    assert created is False


def test_max_concurrent_skips():
    """max_concurrent=1 — in-flight task가 있으면 다음 발사는 skipped."""
    store = _store()
    r = _routine(store, max_concurrent=1, schedule={"minutes": 30})
    past = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    store.update_routine(r["id"], next_run_at=past)
    res1 = svc.fire_due_routine(store, store.get_routine(r["id"]))
    assert res1["action"] == "claimed"
    # task가 backlog(비종결)이므로 in-flight=1 → 다음 회차 skipped.
    past2 = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    store.update_routine(r["id"], next_run_at=past2)
    res2 = svc.fire_due_routine(store, store.get_routine(r["id"]))
    assert res2["action"] == "skipped"
    assert res2["reason"] == "max_concurrent"
    runs = store.list_routine_runs(r["id"])
    assert runs[0]["status"] == "skipped"  # 최신순
    assert runs[1]["status"] == "created"
    # skipped여도 task는 추가 생성되지 않는다.
    assert len(runs) == 2


def test_inflight_count_terminal_task():
    """done/cancelled task는 in-flight에서 제외."""
    store = _store()
    r = _routine(store, max_concurrent=1, schedule={"minutes": 30})
    past = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    store.update_routine(r["id"], next_run_at=past)
    res = svc.fire_due_routine(store, store.get_routine(r["id"]))
    assert res["action"] == "claimed"
    task = store.get_task(res["task_id"])
    store.update_task(task["id"], status="done")
    assert store.routine_inflight_count(r["id"]) == 0


def test_fire_failure_marks_failed():
    """존재하지 않는 assignee로 갱신된 routine → task 생성 실패 → failed."""
    store = _store()
    r = _routine(store, schedule={"minutes": 30})
    store.update_routine(r["id"], assignee_agent_id="ghost")
    past = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    store.update_routine(r["id"], next_run_at=past)
    res = svc.fire_due_routine(store, store.get_routine(r["id"]))
    assert res["action"] == "failed"
    runs = store.list_routine_runs(r["id"])
    assert runs[0]["status"] == "failed"
    assert "task" in runs[0]["error_code"]


def test_project_tag_propagates_to_task():
    """routine.project_id → 생성 task의 project_id/goal_id 태그."""
    store = _store()
    proj = store.create_project("proj-x", color="#fff")
    assert proj is not None
    r = _routine(store, project_id=proj["id"], schedule={"minutes": 30})
    past = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    store.update_routine(r["id"], next_run_at=past)
    res = svc.fire_due_routine(store, store.get_routine(r["id"]))
    assert res["action"] == "claimed"
    task = store.get_task(res["task_id"])
    assert task["project_id"] == proj["id"]


# ---------- update / enable-disable ----------


def test_update_recomputes_next_run_on_schedule_change():
    store = _store()
    r = _routine(store, schedule={"minutes": 60})
    old_next = r["next_run_at"]
    out = svc.update_routine(store, r["id"], {"schedule": {"minutes": 5}}, actor="test")
    assert out["schedule"] == {"minutes": 5}
    assert out["next_run_at"] != old_next
    # 잘못된 schedule은 422
    with pytest.raises(svc.RoutineError):
        svc.update_routine(store, r["id"], {"schedule": {"minutes": -1}})


def test_disable_enable_rearms():
    store = _store()
    r = _routine(store, schedule={"minutes": 30})
    out = svc.set_routine_enabled(store, r["id"], False)
    assert out["enabled"] is False and out["status"] == "paused"
    # disabled는 due 대상이 아니다.
    assert store.due_routines() == []
    out2 = svc.set_routine_enabled(store, r["id"], True)
    assert out2["enabled"] is True
    nxt = datetime.fromisoformat(out2["next_run_at"])
    assert nxt > datetime.now(UTC)
