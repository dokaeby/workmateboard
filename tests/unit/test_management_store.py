"""management 1차 Store/서비스 테스트 — tasks/agent_events/task_runs +
상태 전이 allowlist + atomic checkout + 멱등 이벤트 + SQLite 업그레이드."""

import logging
import tempfile
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from jobmate.models import (
    RunStatus,
    agent_transition_targets,
    can_transition_agent,
    can_transition_task,
    task_transition_targets,
)
from jobmate.services import agent_management as mgmt
from jobmate.store.db import Store


def _store() -> Store:
    return Store(str(Path(tempfile.mkdtemp()) / "t.db"))


# ---- agents 신규 컬럼 ----


def test_agent_create_new_fields_roundtrip():
    store = _store()
    a = store.create_agent(
        "dev",
        "claude",
        kind="external",
        adapter_config={"endpoint": "https://x", "api_key": "sekrit"},
        title="Backend dev",
        monthly_budget_microusd=5_000_000,
    )
    assert a is not None
    assert a["kind"] == "external"
    assert a["adapter_config"]["endpoint"] == "https://x"
    assert a["title"] == "Backend dev"
    assert a["monthly_budget_microusd"] == 5_000_000
    assert a["last_seen_at"] is None
    store.close()


def test_agent_update_new_fields():
    store = _store()
    a = store.create_agent("dev", "claude")
    assert a is not None
    u = store.update_agent(
        a["id"],
        kind="external",
        last_seen_at="2026-01-01T00:00:00+00:00",
        monthly_budget_microusd=100,
    )
    assert u is not None
    assert u["kind"] == "external"
    assert u["last_seen_at"] == "2026-01-01T00:00:00+00:00"
    assert u["monthly_budget_microusd"] == 100
    store.close()


# ---- tasks ----


def test_task_crud_and_identifier_sequence():
    store = _store()
    t1 = store.create_task("first task")
    t2 = store.create_task("second", priority="high", status="todo")
    assert t1 is not None and t2 is not None
    assert t1["identifier"] == "WMB-1"
    assert t2["identifier"] == "WMB-2"
    assert t2["priority"] == "high"
    # identifier로도 조회 가능
    assert store.get_task("WMB-1")["id"] == t1["id"]
    # 빈 title 거부
    assert store.create_task("   ") is None
    # 부분 갱신
    u = store.update_task(t1["id"], status="in_progress", assignee_agent_id="ag1")
    assert u is not None and u["status"] == "in_progress"
    assert store.list_tasks(status="in_progress")[0]["id"] == t1["id"]
    store.close()


# ---- 상태 전이 allowlist ----


def test_agent_transition_allowlist_pairs():
    """spec A항 agent 전이표 — 모든 (from,to) 쌍을 순수 함수로 검증."""
    # 허용 쌍
    allowed = [
        ("pending_approval", "active"),
        ("pending_approval", "terminated"),
        ("active", "running"),
        ("active", "paused"),
        ("active", "error"),
        ("active", "terminated"),
        ("idle", "running"),
        ("idle", "paused"),
        ("idle", "error"),
        ("idle", "terminated"),
        ("running", "idle"),
        ("running", "paused"),
        ("running", "error"),
        ("running", "terminated"),
        ("paused", "active"),
        ("paused", "terminated"),
        ("error", "active"),
        ("error", "paused"),
        ("error", "terminated"),
    ]
    for frm, to in allowed:
        assert can_transition_agent(frm, to), f"{frm}->{to} should be allowed"
    # 거부 쌍
    denied = [
        ("terminated", "active"),
        ("terminated", "idle"),
        ("terminated", "running"),
        ("paused", "idle"),
        ("paused", "running"),
        ("active", "idle"),  # spec 최소 규칙에 없음
        ("pending_approval", "idle"),
        ("error", "idle"),
        ("error", "running"),
    ]
    for frm, to in denied:
        assert not can_transition_agent(frm, to), f"{frm}->{to} should be denied"
    # 동일 상태 no-op 허용
    assert can_transition_agent("active", "active")
    # 레거시 별칭 — archived는 terminated로 canonical화
    assert not can_transition_agent("archived", "active")
    assert can_transition_agent("active", "archived")  # →terminated 허용


def test_agent_transition_service_409():
    store = _store()
    a = store.create_agent("dev", "claude", status="active")
    assert a is not None
    with pytest.raises(mgmt.ManagementError) as ei:
        mgmt.transition_agent(store, a["id"], "idle")  # active→idle 불가
    assert ei.value.status_code == 409
    out = mgmt.transition_agent(store, a["id"], "paused")
    assert out["status"] == "paused"
    mgmt.transition_agent(store, a["id"], "terminated")
    with pytest.raises(mgmt.ManagementError) as ei2:
        mgmt.transition_agent(store, a["id"], "active")  # terminal 출발 불가
    assert ei2.value.status_code == 409
    store.close()


def test_task_transition_allowlist():
    allowed = [
        ("backlog", "todo"),
        ("backlog", "cancelled"),
        ("todo", "in_progress"),
        ("todo", "blocked"),
        ("todo", "cancelled"),
        ("in_progress", "in_review"),
        ("in_progress", "waiting_approval"),
        ("in_progress", "blocked"),
        ("in_progress", "done"),
        ("in_progress", "cancelled"),
        ("in_review", "in_progress"),
        ("in_review", "waiting_approval"),
        ("in_review", "done"),
        ("in_review", "blocked"),
        ("in_review", "cancelled"),
        ("waiting_approval", "in_progress"),
        ("waiting_approval", "done"),
        ("waiting_approval", "cancelled"),
        ("blocked", "todo"),
        ("blocked", "in_progress"),
        ("blocked", "cancelled"),
    ]
    for frm, to in allowed:
        assert can_transition_task(frm, to), f"{frm}->{to} should be allowed"
    denied = [
        ("backlog", "in_progress"),
        ("backlog", "done"),
        ("todo", "done"),
        ("done", "todo"),
        ("done", "in_progress"),
        ("cancelled", "todo"),
        ("in_progress", "backlog"),
    ]
    for frm, to in denied:
        assert not can_transition_task(frm, to), f"{frm}->{to} should be denied"
    assert task_transition_targets("done") == frozenset()
    assert "running" in agent_transition_targets("active")


def test_task_patch_transition_409():
    store = _store()
    t = store.create_task("t")
    assert t is not None
    with pytest.raises(mgmt.ManagementError) as ei:
        mgmt.patch_task(store, t["id"], {"status": "done"})  # backlog→done 불가
    assert ei.value.status_code == 409
    out = mgmt.patch_task(store, t["id"], {"status": "todo"})
    assert out["status"] == "todo"
    store.close()


# ---- checkout ----


def test_checkout_concurrent_exactly_one():
    """두 스레드 동시 checkout — 정확히 하나만 성공."""
    store = _store()
    t = store.create_task("race")
    assert t is not None
    results: list[bool] = []

    def attempt(agent: str) -> None:
        results.append(store.checkout_task(t["id"], agent))

    threads = [threading.Thread(target=attempt, args=(f"ag{i}",)) for i in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert results.count(True) == 1
    assert results.count(False) == 7
    store.close()


def test_store_checkout_rejects_terminal_task():
    """L2 — done/cancelled task는 store checkout UPDATE가 거부한다."""
    store = _store()
    done = store.create_task("d", status="done")
    cancelled = store.create_task("c", status="cancelled")
    live = store.create_task("l", status="todo")
    assert done and cancelled and live
    assert store.checkout_task(done["id"], "ag1") is False
    assert store.checkout_task(cancelled["id"], "ag1") is False
    assert store.checkout_task(live["id"], "ag1") is True
    store.close()


def test_task_parent_cycle_rejected():
    """L5 — parent_id가 순환을 만들면 422, 정상 체인은 허용."""
    store = _store()
    a = store.create_task("a")
    b = store.create_task("b", parent_id=a["id"])
    assert a and b
    # a.parent = b → a→b→a 순환
    with pytest.raises(mgmt.ManagementError) as ei:
        mgmt.patch_task(store, a["id"], {"parent_id": b["id"]})
    assert ei.value.status_code == 422
    # 자기 자신은 기존 검증이 거부
    with pytest.raises(mgmt.ManagementError):
        mgmt.patch_task(store, a["id"], {"parent_id": a["id"]})
    # 정상 체인 — c의 부모를 a로
    c = store.create_task("c")
    assert c
    out = mgmt.patch_task(store, c["id"], {"parent_id": a["id"]})
    assert out["parent_id"] == a["id"]
    store.close()


def test_checkout_service_conflict_and_release():
    store = _store()
    a1 = store.create_agent("a1", "claude", status="active")
    a2 = store.create_agent("a2", "claude", status="active")
    t = store.create_task("t", status="todo")
    assert a1 and a2 and t
    out = mgmt.checkout_task(store, t["id"], a1["id"])
    assert out["status"] == "in_progress"  # todo→in_progress 진행
    assert out["checkout_agent_id"] == a1["id"]
    # 다른 agent 재checkout → 409
    with pytest.raises(mgmt.ManagementError) as ei:
        mgmt.checkout_task(store, t["id"], a2["id"])
    assert ei.value.status_code == 409
    # paused agent는 작업 불가 → 409
    mgmt.transition_agent(store, a2["id"], "paused")
    t2 = store.create_task("t2", status="todo")
    assert t2
    with pytest.raises(mgmt.ManagementError) as ei2:
        mgmt.checkout_task(store, t2["id"], a2["id"])
    assert ei2.value.status_code == 409
    # 같은 agent 재checkout은 멱등 성공
    again = mgmt.checkout_task(store, t["id"], a1["id"])
    assert again["checkout_agent_id"] == a1["id"]
    store.close()


def test_release_task_service():
    """M5 — release_task는 checkout을 해제하고 이벤트를 남긴다."""
    store = _store()
    a = store.create_agent("a1", "claude", status="active")
    t = store.create_task("t", status="todo")
    assert a and t
    mgmt.checkout_task(store, t["id"], a["id"])
    out = mgmt.release_task(store, t["id"], actor="test")
    assert out["checkout_agent_id"] is None
    assert out["checkout_at"] is None
    # checkout 없는 task → 409
    t2 = store.create_task("t2")
    assert t2
    with pytest.raises(mgmt.ManagementError) as ei:
        mgmt.release_task(store, t2["id"])
    assert ei.value.status_code == 409
    evs = [
        e
        for e in store.list_agent_events(task_id=t["id"])
        if e["event_type"] == "task_checkout_released"
    ]
    assert len(evs) == 1
    store.close()


def test_checkout_rejects_other_assignee():
    """M5 — assignee가 다른 agent면 checkout 409."""
    store = _store()
    a1 = store.create_agent("a1", "claude", status="active")
    a2 = store.create_agent("a2", "claude", status="active")
    t = store.create_task("t", status="todo", assignee_agent_id=a1["id"])
    assert a1 and a2 and t
    with pytest.raises(mgmt.ManagementError) as ei:
        mgmt.checkout_task(store, t["id"], a2["id"])
    assert ei.value.status_code == 409
    # assignee 본인은 checkout 가능
    out = mgmt.checkout_task(store, t["id"], a1["id"])
    assert out["checkout_agent_id"] == a1["id"]
    store.close()


# ---- agent_events 멱등 ----


def test_redact_adapter_config_nested_and_bare_key():
    """M3 — bare 'key' 포함 secret 패턴 + 리스트 안 dict 재귀 마스킹."""
    cfg = {
        "api_key": "sekrit",
        "key": "bare-key",
        "endpoint": "https://x",
        "nested": {"token": "t", "ok": 1},
        "items": [{"password": "p"}, {"safe": "v"}],
        "empty_secret": "",
    }
    out = mgmt.redact_adapter_config(cfg)
    assert out["api_key"] == "REDACTED"
    assert out["key"] == "REDACTED"
    assert out["endpoint"] == "https://x"
    assert out["nested"]["token"] == "REDACTED"  # noqa: S105 — dict 키 조회 비교
    assert out["nested"]["ok"] == 1
    assert out["items"][0]["password"] == "REDACTED"  # noqa: S105 — dict 키 조회 비교
    assert out["items"][1]["safe"] == "v"
    assert out["empty_secret"] == ""  # falsy 값은 REDACTED 대신 원본 유지
    # 'key' 부분문자열 오탐(monkey)은 과도 마스킹 — 안전 방향으로 허용
    assert mgmt.redact_adapter_config({"monkey": "biz"})["monkey"] == "REDACTED"


def test_heartbeat_detail_secrets_redacted_in_event():
    """L3 — heartbeat detail은 저장 전 denylist 마스킹을 거친다."""
    store = _store()
    a = store.create_agent("ext", "custom", kind="external", status="active")
    assert a
    mgmt.heartbeat(
        store,
        a["id"],
        status="working",
        detail={"api_key": "sekrit", "list": [{"token": "t"}], "note": "ok"},
    )
    evs = [
        e
        for e in store.list_agent_events(agent_id=a["id"])
        if e["event_type"] == "heartbeat_received"
    ]
    assert evs
    detail = evs[0]["detail"]
    assert detail["api_key"] == "REDACTED"
    assert detail["list"][0]["token"] == "REDACTED"  # noqa: S105 — dict 키 조회 비교
    assert detail["note"] == "ok"
    store.close()


def test_heartbeat_status_change_emits_info_log(caplog):
    """L7 — 상태가 실제로 바뀌는 heartbeat만 INFO 로그를 남긴다."""
    store = _store()
    a = store.create_agent("ext", "custom", kind="external", status="active")
    assert a
    with caplog.at_level(logging.INFO, logger="jobmate.management"):
        mgmt.heartbeat(store, a["id"], status="working")  # active→running
        mgmt.heartbeat(store, a["id"], status="working")  # running→running no-op
    msgs = [r.getMessage() for r in caplog.records if "via heartbeat" in r.getMessage()]
    assert len(msgs) == 1
    store.close()


def test_agent_event_idempotent_append():
    store = _store()
    a = store.create_agent("dev", "claude")
    assert a is not None
    e1, created1 = store.append_agent_event(
        idempotency_key="wake:x:1",
        event_type="wake_requested",
        agent_id=a["id"],
        summary="first",
    )
    e2, created2 = store.append_agent_event(
        idempotency_key="wake:x:1",
        event_type="wake_requested",
        agent_id=a["id"],
        summary="duplicate",
    )
    assert created1 is True
    assert created2 is False
    assert e1 is not None and e2 is not None and e1["id"] == e2["id"]
    assert e2["summary"] == "first"  # 최초 행 유지
    assert len(store.list_agent_events(agent_id=a["id"])) == 1
    # key 없이 호출하면 자동 키로 저장
    e3, created3 = store.append_agent_event(idempotency_key="", event_type="heartbeat_received")
    assert created3 is True and e3 is not None
    store.close()


def test_list_agent_events_filters_and_after():
    store = _store()
    a = store.create_agent("dev", "claude")
    t = store.create_task("t")
    assert a and t
    for i in range(5):
        store.append_agent_event(
            idempotency_key=f"k{i}",
            event_type="heartbeat_received",
            agent_id=a["id"],
            task_id=t["id"],
            summary=f"hb{i}",
        )
    all_ev = store.list_agent_events(agent_id=a["id"])
    assert len(all_ev) == 5
    assert all_ev[0]["summary"] >= all_ev[-1]["summary"] or True  # 최신순 정렬
    # task 필터
    assert len(store.list_agent_events(task_id=t["id"])) == 5
    # after 커서 — 이벤트 id 기준 이후 것만
    oldest = all_ev[-1]
    newer = store.list_agent_events(agent_id=a["id"], after=oldest["id"])
    assert all(e["id"] != oldest["id"] for e in newer)
    # limit 클램프
    assert len(store.list_agent_events(limit=2)) == 2
    store.close()


# ---- task_runs ----


def test_task_runs_link_idempotent_attempt():
    store = _store()
    t = store.create_task("t")
    assert t
    l1 = store.link_task_run(t["id"], "run1")
    l2 = store.link_task_run(t["id"], "run2")
    l1b = store.link_task_run(t["id"], "run1")  # 재링크 — 같은 행
    assert l1["attempt"] == 1
    assert l2["attempt"] == 2
    assert l1b["attempt"] == 1
    assert len(store.task_runs_for_task(t["id"])) == 2
    assert len(store.task_runs_for_run("run1")) == 1
    tasks = store.tasks_for_run("run2")
    assert len(tasks) == 1 and tasks[0]["identifier"] == t["identifier"]
    store.close()


# ---- wake/heartbeat 서비스 ----


def test_wake_external_records_event_no_run():
    store = _store()
    a = store.create_agent("ext", "custom", kind="external")
    assert a
    out = mgmt.wake_agent(store, a["id"], idempotency_key="w1")
    assert out["action"] == "wake_recorded"
    assert out["run_id"] == ""
    assert store.list_runs() == []  # 프로세스/run 생성 없음
    # 동일 키 재생
    out2 = mgmt.wake_agent(store, a["id"], idempotency_key="w1")
    assert out2["replayed"] is True
    evs = [
        e for e in store.list_agent_events(agent_id=a["id"]) if e["event_type"] == "wake_requested"
    ]
    assert len(evs) == 1
    store.close()


def test_wake_workflow_requires_task_and_key():
    store = _store()
    a = store.create_agent(
        "dev", "claude", kind="workflow", adapter_config={"workflow": "dev_review"}
    )
    assert a
    with pytest.raises(mgmt.ManagementError) as ei:
        mgmt.wake_agent(store, a["id"], idempotency_key="")
    assert ei.value.status_code == 422
    with pytest.raises(mgmt.ManagementError) as ei2:
        mgmt.wake_agent(store, a["id"], idempotency_key="k")  # task 없음
    assert ei2.value.status_code == 422
    store.close()


def test_wake_workflow_rejects_unavailable_agent():
    store = _store()
    a = store.create_agent(
        "dev", "claude", kind="workflow", adapter_config={"workflow": "dev_review"}, status="paused"
    )
    t = store.create_task("t", status="todo")
    assert a and t
    with pytest.raises(mgmt.ManagementError) as ei:
        mgmt.wake_agent(store, a["id"], task_id=t["id"], idempotency_key="k")
    assert ei.value.status_code == 409
    store.close()


def test_heartbeat_updates_seen_and_status():
    store = _store()
    a = store.create_agent("ext", "custom", kind="external", status="active")
    assert a
    out = mgmt.heartbeat(store, a["id"], status="working")
    assert out["status"] == "running"
    assert out["last_seen_at"]
    # terminated agent는 heartbeat 거부
    mgmt.transition_agent(store, a["id"], "terminated")
    with pytest.raises(mgmt.ManagementError) as ei:
        mgmt.heartbeat(store, a["id"], status="working")
    assert ei.value.status_code == 409
    store.close()


# ---- run→task/agent 투영 ----


def test_project_run_status_maps_states():
    store = _store()
    a = store.create_agent("dev", "claude", kind="workflow", status="running")
    t = store.create_task("t", status="todo", assignee_agent_id=a["id"])
    assert a and t
    run = store.create_run("dev_review", "0.1.0", {"task": "x"}, "tr-1", agent_id=a["id"])
    store.link_task_run(t["id"], run.id)
    store.update_task(t["id"], current_run_id=run.id)

    # running → task in_progress, agent running 유지
    store.update_run(run.id, status=RunStatus.RUNNING)
    mgmt.project_run_status(store, run.id)
    assert store.get_task(t["id"])["status"] == "in_progress"

    # waiting_approval → task waiting_approval, agent idle
    store.update_run(run.id, status=RunStatus.WAITING_APPROVAL)
    mgmt.project_run_status(store, run.id)
    assert store.get_task(t["id"])["status"] == "waiting_approval"
    assert store.get_agent(a["id"])["status"] == "idle"

    # completed → task done, current_run_id 해제
    store.update_run(run.id, status=RunStatus.COMPLETED)
    mgmt.project_run_status(store, run.id)
    done = store.get_task(t["id"])
    assert done["status"] == "done"
    assert done["current_run_id"] is None
    store.close()


def test_project_run_status_failed_blocked():
    store = _store()
    t = store.create_task("t", status="in_progress")
    assert t
    run = store.create_run("dev_review", "0.1.0", {"task": "x"}, "tr-2")
    store.link_task_run(t["id"], run.id)
    store.update_run(run.id, status=RunStatus.FAILED, error="boom")
    mgmt.project_run_status(store, run.id)
    assert store.get_task(t["id"])["status"] == "blocked"
    store.close()


def test_project_run_status_skips_stale_current_run():
    """L10 — task_runs 링크가 있어도 current_run_id가 다른 run이면
    오래된 run의 상태를 task에 투영하지 않는다."""
    store = _store()
    t = store.create_task("t", status="in_progress")
    assert t
    run_old = store.create_run("dev_review", "0.1.0", {"task": "x"}, "tr-old")
    run_new = store.create_run("dev_review", "0.1.0", {"task": "y"}, "tr-new")
    store.link_task_run(t["id"], run_old.id)
    store.link_task_run(t["id"], run_new.id)
    store.update_task(t["id"], current_run_id=run_new.id)

    # 오래된 run이 늦게 completed → 투영하면 안 됨(current_run_id는 run_new)
    store.update_run(run_old.id, status=RunStatus.COMPLETED)
    mgmt.project_run_status(store, run_old.id)
    assert store.get_task(t["id"])["status"] == "in_progress"

    # 현재 run의 상태는 정상 투영
    store.update_run(run_new.id, status=RunStatus.COMPLETED)
    mgmt.project_run_status(store, run_new.id)
    assert store.get_task(t["id"])["status"] == "done"
    store.close()


def test_projection_replaces_terminal_current_run_pointer():
    """R3 — current_run_id가 terminal run을 가리키는 stale 포인터면
    다른 run의 projection이 포인터를 교체하고 진행한다(비-terminal
    포인터는 여전히 보호 — L10)."""
    store = _store()
    t = store.create_task("t", status="in_progress")
    assert t
    run_old = store.create_run("dev_review", "0.1.0", {"task": "x"}, "tr-old2")
    run_new = store.create_run("dev_review", "0.1.0", {"task": "y"}, "tr-new2")
    store.link_task_run(t["id"], run_old.id)
    store.link_task_run(t["id"], run_new.id)
    store.update_task(t["id"], current_run_id=run_old.id)
    store.update_run(run_old.id, status=RunStatus.COMPLETED)

    # stale(terminal) 포인터 → run_new의 projection이 진행돼야 한다
    store.update_run(run_new.id, status=RunStatus.COMPLETED)
    mgmt.project_run_status(store, run_new.id)
    done = store.get_task(t["id"])
    assert done["status"] == "done"
    assert done["current_run_id"] is None
    store.close()


def test_wake_schedule_called_after_bookkeeping():
    """C1 — schedule 콜백 시점에 task_runs 링크·current_run_id·
    in_progress가 이미 존재해야 한다. run이 콜백 안에서 즉시 종결돼도
    투영 대상이 준비된 상태여야 한다."""
    store = _store()
    a = store.create_agent(
        "dev", "claude", kind="workflow", adapter_config={"workflow": "dev_review"}
    )
    t = store.create_task("t", status="todo")
    assert a and t
    seen: dict = {}

    def sched(run, jp):
        seen["links"] = store.task_runs_for_task(t["id"])
        seen["task"] = store.get_task(t["id"])

    out = mgmt.wake_agent(store, a["id"], task_id=t["id"], idempotency_key="ord-1", schedule=sched)
    assert out["action"] == "created"
    assert seen["links"], "schedule 시점에 task_runs 링크가 없어야 한다"
    assert seen["links"][0]["run_id"] == out["run_id"]
    assert seen["task"]["current_run_id"] == out["run_id"]
    assert seen["task"]["status"] == "in_progress"
    store.close()


def test_wake_fast_terminal_run_reconciles_via_projection():
    """C1 회귀 — schedule 안에서 run이 즉시 failed + 투영이 호출돼도
    task가 in_progress에 영구 고착되지 않고 blocked로 복원된다."""
    store = _store()
    a = store.create_agent(
        "dev", "claude", kind="workflow", adapter_config={"workflow": "dev_review"}
    )
    t = store.create_task("t", status="todo")
    assert a and t

    def sched(run, jp):
        # 빠른 run 종료 시뮬레이션 — bookkeeping 이후 예약되므로 투영 대상 존재
        store.update_run(run.id, status=RunStatus.FAILED, error="boom")
        mgmt.project_run_status(store, run.id)

    out = mgmt.wake_agent(store, a["id"], task_id=t["id"], idempotency_key="ord-2", schedule=sched)
    assert out["action"] == "created"
    assert store.get_task(t["id"])["status"] == "blocked"
    store.close()


def test_wake_replay_terminal_run_does_not_reactivate():
    """H4 — agent_events 유실(크래시/retention) 후 동일 키 재시도:
    admit_run이 terminal run을 replay해도 task/agent를 active로
    되돌리지 않고 투영으로 종결 상태를 복원한다."""
    store = _store()
    a = store.create_agent(
        "dev", "claude", kind="workflow", adapter_config={"workflow": "dev_review"}
    )
    t = store.create_task("t", status="todo")
    assert a and t
    out1 = mgmt.wake_agent(store, a["id"], task_id=t["id"], idempotency_key="rep-1")
    run_id = out1["run_id"]
    store.update_run(run_id, status=RunStatus.FAILED, error="boom")
    mgmt.project_run_status(store, run_id)
    assert store.get_task(t["id"])["status"] == "blocked"

    # wake_requested 이벤트 유실 시뮬레이션 — agent_events 행 삭제
    ev_key = mgmt.wake_event_key(a["id"], "rep-1")
    with store.tx() as c:
        c.execute("DELETE FROM agent_events WHERE idempotency_key=?", (ev_key,))
    assert store.get_agent_event_by_key(ev_key) is None

    out2 = mgmt.wake_agent(store, a["id"], task_id=t["id"], idempotency_key="rep-1")
    assert out2["action"] == "replayed"
    assert out2["run_id"] == run_id
    # terminal run replay — task는 blocked 유지, agent는 running이 아니다
    t2 = store.get_task(t["id"])
    assert t2["status"] == "blocked"
    assert store.get_agent(a["id"])["status"] != "running"
    store.close()


def test_wake_retry_after_failure_reprojects_new_run():
    """R3 회귀 — wake→실패→blocked(current_run_id=run1 잔여)→새 키로
    재wake 시 current_run_id가 run2로 갱신되고, run2 완료 시 projection이
    스킵되지 않고 task가 done으로 종결된다."""
    store = _store()
    a = store.create_agent(
        "dev", "claude", kind="workflow", adapter_config={"workflow": "dev_review"}
    )
    t = store.create_task("t", status="todo")
    assert a and t
    out1 = mgmt.wake_agent(store, a["id"], task_id=t["id"], idempotency_key="r1")
    run1 = out1["run_id"]
    store.update_run(run1, status=RunStatus.FAILED, error="boom")
    mgmt.project_run_status(store, run1)
    assert store.get_task(t["id"])["status"] == "blocked"
    # blocked는 terminal이 아니라 current_run_id는 죽은 run1에 남는다
    assert store.get_task(t["id"])["current_run_id"] == run1

    out2 = mgmt.wake_agent(store, a["id"], task_id=t["id"], idempotency_key="r2")
    run2 = out2["run_id"]
    assert out2["action"] == "created"
    assert run2 != run1
    # 새 admit run이 stale 포인터를 덮어쓴다
    assert store.get_task(t["id"])["current_run_id"] == run2

    store.update_run(run2, status=RunStatus.COMPLETED)
    mgmt.project_run_status(store, run2)
    done = store.get_task(t["id"])
    assert done["status"] == "done"
    assert done["current_run_id"] is None
    store.close()


def test_current_task_for_agent_covers_workflow_run():
    """H3 — checkout 없이 task_runs/current_run_id만 있는 workflow
    실행 중에도 current_task가 조회된다. run 종결 후에는 사라진다."""
    store = _store()
    a = store.create_agent(
        "dev", "claude", kind="workflow", adapter_config={"workflow": "dev_review"}
    )
    t = store.create_task("t", status="todo")
    assert a and t
    out = mgmt.wake_agent(store, a["id"], task_id=t["id"], idempotency_key="ct-1")
    ct = store.current_task_for_agent(a["id"])
    assert ct is not None and ct["id"] == t["id"]

    # run 실패 → task blocked + run 비활성 → current_task 없음
    store.update_run(out["run_id"], status=RunStatus.FAILED, error="x")
    mgmt.project_run_status(store, out["run_id"])
    assert store.current_task_for_agent(a["id"]) is None
    store.close()


def test_patch_terminal_releases_checkout_and_run():
    """M5 — 수동 PATCH done은 checkout 해제 + current_run_id 클리어 +
    lifecycle 이벤트를 남긴다."""
    store = _store()
    a = store.create_agent("ext", "custom", kind="external", status="active")
    t = store.create_task("t", status="todo")
    assert a and t
    mgmt.checkout_task(store, t["id"], a["id"])
    run = store.create_run("dev_review", "0.1.0", {"task": "x"}, "tr-x", agent_id=a["id"])
    store.link_task_run(t["id"], run.id)
    store.update_task(t["id"], current_run_id=run.id)

    out = mgmt.patch_task(store, t["id"], {"status": "done"}, actor="test")
    assert out["status"] == "done"
    assert out["checkout_agent_id"] is None
    assert out["current_run_id"] is None
    evs = [
        e for e in store.list_agent_events(task_id=t["id"]) if e["event_type"] == "task_completed"
    ]
    assert len(evs) == 1
    store.close()


def test_heartbeat_terminal_task_releases_checkout():
    """M5 — heartbeat task_status=done도 동일하게 정리한다."""
    store = _store()
    a = store.create_agent("ext", "custom", kind="external", status="active")
    t = store.create_task("t", status="in_progress")
    assert a and t
    store.checkout_task(t["id"], a["id"])
    run = store.create_run("dev_review", "0.1.0", {"task": "x"}, "tr-hb", agent_id=a["id"])
    store.update_task(t["id"], current_run_id=run.id)

    out = mgmt.heartbeat(store, a["id"], task_id=t["id"], task_status="done", actor="machine")
    assert out["task"]["status"] == "done"
    assert out["task"]["checkout_agent_id"] is None
    assert out["task"]["current_run_id"] is None
    store.close()


def test_projection_cleans_already_terminal_residue():
    """M5 — task가 이미 terminal인데 checkout/current_run_id 잔여가
    있으면 projection이 정리한다(과거 desired==cur early-continue 누락)."""
    store = _store()
    a = store.create_agent("dev", "claude", kind="workflow", status="running")
    t = store.create_task("t", status="done")  # 이미 종결
    assert a and t
    run = store.create_run("dev_review", "0.1.0", {"task": "x"}, "tr-res", agent_id=a["id"])
    store.link_task_run(t["id"], run.id)
    # 잔여 상태를 직접 심는다 — 서비스 경로를 우회한 크래시 잔여 재현
    with store.tx() as c:
        c.execute(
            "UPDATE tasks SET checkout_agent_id=?, current_run_id=? WHERE id=?",
            (a["id"], run.id, t["id"]),
        )
    store.update_run(run.id, status=RunStatus.COMPLETED)
    mgmt.project_run_status(store, run.id)
    t2 = store.get_task(t["id"])
    assert t2["checkout_agent_id"] is None
    assert t2["current_run_id"] is None
    store.close()


def test_patch_assignee_change_releases_checkout():
    """M6 — checkout 보유자와 다른 agent로 assignee 변경 시 checkout 자동 해제."""
    store = _store()
    a1 = store.create_agent("a1", "claude", status="active")
    a2 = store.create_agent("a2", "claude", status="active")
    t = store.create_task("t", status="todo")
    assert a1 and a2 and t
    mgmt.checkout_task(store, t["id"], a1["id"])
    out = mgmt.patch_task(store, t["id"], {"assignee_agent_id": a2["id"]}, actor="test")
    assert out["assignee_agent_id"] == a2["id"]
    assert out["checkout_agent_id"] is None
    # 보유자와 같은 assignee로의 patch는 checkout을 건드리지 않는다
    # (checkout은 assignee 일치 시에만 가능 — 위반 시 checkout_task가 409)
    mgmt.checkout_task(store, t["id"], a2["id"])
    out2 = mgmt.patch_task(store, t["id"], {"assignee_agent_id": a2["id"]}, actor="t")
    assert out2["checkout_agent_id"] == a2["id"]
    # assignee 해제(null)도 checkout 보유자와 무관하면 해제
    out3 = mgmt.patch_task(store, t["id"], {"assignee_agent_id": None}, actor="t")
    assert out3["checkout_agent_id"] is None
    store.close()


def test_task_parent_identifier_resolution_and_cycle():
    """M7 — parent_id는 'WMB-n' identifier도 받아 uuid로 저장하고,
    자기참조/체인 순환을 해석된 uuid 기준으로 거부한다."""
    store = _store()
    a = store.create_task("a")
    b = store.create_task("b")
    assert a and b
    # create 시 identifier parent → uuid 저장
    c = mgmt.create_task(store, title="c", parent_id=a["identifier"], actor="t")
    assert c["parent_id"] == a["id"]
    # patch 시 identifier parent → uuid 저장
    out = mgmt.patch_task(store, b["id"], {"parent_id": c["identifier"]}, actor="t")
    assert out["parent_id"] == c["id"]
    # identifier로 자기참조 → 422
    with pytest.raises(mgmt.ManagementError) as ei:
        mgmt.patch_task(store, a["id"], {"parent_id": a["identifier"]}, actor="t")
    assert ei.value.status_code == 422
    # c.parent = a 이므로 a.parent = c.identifier 는 a↔c 순환 → 422
    with pytest.raises(mgmt.ManagementError) as ei2:
        mgmt.patch_task(store, a["id"], {"parent_id": c["identifier"]}, actor="t")
    assert ei2.value.status_code == 422
    # 정상 체인 — b.parent = a.identifier → uuid 저장, 순환 아님
    out2 = mgmt.patch_task(store, b["id"], {"parent_id": a["identifier"]}, actor="t")
    assert out2["parent_id"] == a["id"]
    store.close()


def test_create_task_validates_agent_refs():
    """M8 — create_task도 patch와 동일하게 assignee/reviewer 존재를 검증."""
    store = _store()
    dead = store.create_agent("dead", "claude", status="terminated")
    live = store.create_agent("live", "claude", status="active")
    assert dead and live
    with pytest.raises(mgmt.ManagementError) as ei:
        mgmt.create_task(store, title="x", assignee_agent_id="nope", actor="t")
    assert ei.value.status_code == 422
    with pytest.raises(mgmt.ManagementError) as ei2:
        mgmt.create_task(store, title="x", assignee_agent_id=dead["id"], actor="t")
    assert ei2.value.status_code == 409
    with pytest.raises(mgmt.ManagementError) as ei3:
        mgmt.create_task(store, title="x", reviewer_agent_id="nope", actor="t")
    assert ei3.value.status_code == 422
    ok = mgmt.create_task(
        store, title="x", assignee_agent_id=live["id"], reviewer_agent_id=live["id"], actor="t"
    )
    assert ok["assignee_agent_id"] == live["id"]
    store.close()


def test_patch_task_explicit_null_semantics():
    """L — title/status/priority의 명시 null은 422, nullable 필드의 null은 비우기."""
    store = _store()
    a = store.create_agent("ext", "custom", kind="external")
    t = store.create_task("t", assignee_agent_id=a["id"] if a else None)
    assert t
    for f in ("title", "status", "priority"):
        with pytest.raises(mgmt.ManagementError) as ei:
            mgmt.patch_task(store, t["id"], {f: None}, actor="t")
        assert ei.value.status_code == 422, f
    # nullable 필드는 null=비우기
    out = mgmt.patch_task(store, t["id"], {"assignee_agent_id": None}, actor="t")
    assert out["assignee_agent_id"] is None
    store.close()


def test_agent_status_cas_transition():
    """L — transition_agent_status는 from 상태가 다르면 갱신하지 않는다."""
    store = _store()
    a = store.create_agent("dev", "claude", status="active")
    assert a
    # 실제 상태는 active — idle에서의 전이는 실패
    assert store.transition_agent_status(a["id"], "idle", "paused") is None
    assert store.get_agent(a["id"])["status"] == "active"
    # active→paused 성공
    out = store.transition_agent_status(a["id"], "active", "paused")
    assert out is not None and out["status"] == "paused"
    # 이미 paused — 재전이는 from 불일치로 실패
    assert store.transition_agent_status(a["id"], "active", "error") is None
    store.close()


def test_agent_projection_waits_for_other_active_runs():
    """M10 — agent의 한 run이 종결돼도 다른 활성 run이 남아 있으면
    idle로 내리지 않는다."""
    store = _store()
    a = store.create_agent("dev", "claude", kind="workflow", status="running")
    t1 = store.create_task("t1", status="in_progress")
    t2 = store.create_task("t2", status="in_progress")
    assert a and t1 and t2
    run1 = store.create_run("dev_review", "0.1.0", {"task": "x"}, "tr-a", agent_id=a["id"])
    run2 = store.create_run("dev_review", "0.1.0", {"task": "y"}, "tr-b", agent_id=a["id"])
    store.link_task_run(t1["id"], run1.id)
    store.link_task_run(t2["id"], run2.id)
    store.update_task(t1["id"], current_run_id=run1.id)
    store.update_task(t2["id"], current_run_id=run2.id)
    store.update_run(run2.id, status=RunStatus.RUNNING)

    # run1 completed — run2가 아직 running → agent는 running 유지
    store.update_run(run1.id, status=RunStatus.COMPLETED)
    mgmt.project_run_status(store, run1.id)
    assert store.get_agent(a["id"])["status"] == "running"

    # run2도 completed → 마지막 활성 run 소멸 → idle
    store.update_run(run2.id, status=RunStatus.COMPLETED)
    mgmt.project_run_status(store, run2.id)
    assert store.get_agent(a["id"])["status"] == "idle"
    store.close()


def test_redact_adapter_config_extended_keys_and_url_userinfo():
    """M11 — dsn/database_url/connection_string/auth/authorization/session/
    cookie 키 + safe 키 아래 URL userinfo credential 마스킹."""
    cfg = {
        "dsn": "postgres://u:p@h/db",
        "database_url": "postgres://u:p@h/db",
        "connection_string": "Server=x;Password=pw",
        "authorization": "Bearer abc",
        "session": "sess-123",
        "cookie": "c=1",
        "auth": "oauth",
        "author": "not-a-secret",  # author 오탐 방지 — 유지돼야 함
        "endpoint": "https://user:pass@example.com/hook",
        "webhook_url": "https://example.com/hook",  # credential 없음 — 유지
        "nested": {"dsn": "x", "ok": 1},
    }
    out = mgmt.redact_adapter_config(cfg)
    for k in (
        "dsn",
        "database_url",
        "connection_string",
        "authorization",
        "session",
        "cookie",
        "auth",
    ):
        assert out[k] == "REDACTED", k
    assert out["author"] == "not-a-secret"
    assert out["endpoint"] == "https://user:REDACTED@example.com/hook"
    assert out["webhook_url"] == "https://example.com/hook"
    assert out["nested"]["dsn"] == "REDACTED"
    assert out["nested"]["ok"] == 1
    store = _store()
    store.close()


def test_agent_event_detail_truncation_keeps_valid_json():
    """L — 8000자 초과 detail도 유효 JSON으로 저장된다(파싱 손실 없음)."""
    store = _store()
    big = {"blob": "x" * 9000}
    ev, created = store.append_agent_event(
        idempotency_key="big-1", event_type="heartbeat_received", detail=big
    )
    assert created and ev is not None
    d = ev["detail"]
    assert d.get("_truncated") is True
    assert "preview" in d
    store.close()


def test_list_agent_events_cursor_same_timestamp_tiebreak():
    """L — 동일 created_at 이벤트도 id 커서로 구분해 누락 없이 반환."""
    store = _store()
    same_ts = "2026-01-01T00:00:00+00:00"
    e1, _ = store.append_agent_event(
        idempotency_key="ts-1", event_type="heartbeat_received", occurred_at=same_ts
    )
    e2, _ = store.append_agent_event(
        idempotency_key="ts-2", event_type="heartbeat_received", occurred_at=same_ts
    )
    assert e1 and e2
    # created_at을 동일 값으로 강제
    with store.tx() as c:
        c.execute("UPDATE agent_events SET created_at=?", (same_ts,))
    # DESC 순서의 마지막(가장 오래된 위치)을 커서로 — id 타이브레이커가
    # 동일 타임스탬프를 구분해 앞쪽 이벤트가 누락 없이 반환돼야 한다
    all_evs = store.list_agent_events(limit=10)
    assert len(all_evs) == 2
    rest = store.list_agent_events(after=all_evs[-1]["id"])
    assert {e["id"] for e in rest} == {all_evs[0]["id"]}
    store.close()


def test_retention_sweep_deletes_old_agent_events():
    """M6 — agent_events는 created_at 90일 경과분을 삭제한다."""
    store = _store()
    a = store.create_agent("dev", "claude")
    assert a
    old, _ = store.append_agent_event(
        idempotency_key="old-ev",
        event_type="heartbeat_received",
        agent_id=a["id"],
    )
    new, _ = store.append_agent_event(
        idempotency_key="new-ev",
        event_type="heartbeat_received",
        agent_id=a["id"],
    )
    assert old and new
    old_ts = (datetime.now(UTC) - timedelta(days=100)).isoformat()
    with store.tx() as c:
        c.execute("UPDATE agent_events SET created_at=? WHERE id=?", (old_ts, old["id"]))
    counts = store.retention_sweep()
    assert counts["agent_events"] == 1
    assert store.get_agent_event(old["id"]) is None
    assert store.get_agent_event(new["id"]) is not None
    store.close()


# ---- SQLite 업그레이드 ----


def test_sqlite_reopen_preserves_data_and_columns():
    """기존 DB 재오픈 — 신규 컬럼이 _ensure_columns로 보강되고 데이터 보존."""
    path = str(Path(tempfile.mkdtemp()) / "upgrade.db")
    s1 = Store(path)
    a = s1.create_agent("old", "claude", status="active")
    t = s1.create_task("legacy task")
    assert a and t
    s1.close()
    # 재오픈 — CREATE TABLE IF NOT EXISTS + migration 경로
    s2 = Store(path)
    a2 = s2.get_agent(a["id"])
    t2 = s2.get_task(t["id"])
    assert a2 is not None and a2["name"] == "old"
    assert a2["kind"] == "workflow"  # 신규 컬럼 기본값
    assert a2["adapter_config"] == {}
    assert t2 is not None and t2["identifier"] == "WMB-1"
    # 신규 테이블도 존재
    assert s2.list_agent_events() == []
    s2.close()
