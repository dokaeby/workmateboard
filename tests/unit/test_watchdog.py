"""watchdog 서비스 테스트 — paperclip spec 2차 E.

- 5개 detector: stale_heartbeat / stranded_task / stuck_run /
  missing_cost / failed_resume
- source당 open recovery 1건(dedupe), batch 상한
- 안전 복구만 자동 시도, max_attempts 후 escalated + 알림
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from jobmate.models import RunStatus, TaskStatus
from jobmate.services import watchdog
from jobmate.store.db import Store
from jobmate.store.util import _iso


def _store() -> Store:
    return Store(str(Path(tempfile.mkdtemp()) / "t.db"))


def _hours_ago(h: float) -> str:
    return _iso(datetime.now(UTC) - timedelta(hours=h)) or ""


def _external_agent(store: Store, name: str = "ext", last_seen: str | None = None):
    a = store.create_agent(name, "custom", kind="external", status="active")
    assert a is not None
    if last_seen is not None:
        store.update_agent(a["id"], last_seen_at=last_seen)
    return a


def _run(store: Store, status: RunStatus = RunStatus.QUEUED, age_hours: float = 0):
    run = store.create_run("dev_review", "1", {"task": "t"}, trace_id="x")
    store.update_run(run.id, status=status)
    if age_hours:
        old = _hours_ago(age_hours)
        with store.tx() as c:
            c.execute("UPDATE runs SET updated_at=? WHERE id=?", (old, run.id))
    return store.get_run(run.id)


# ---------------- detector: stale_heartbeat ----------------


def test_stale_heartbeat_detected_and_deduped():
    s = _store()
    a = _external_agent(s, last_seen=_hours_ago(2))
    counts = watchdog.run_cycle(s)
    assert counts["created"] >= 1
    recs = s.list_recovery_actions(kind="stale_heartbeat")
    assert len(recs) == 1
    assert recs[0]["source_type"] == "agent" and recs[0]["source_id"] == a["id"]
    # 같은 cycle 재실행 — open recovery가 있으므로 중복 생성 없음
    counts2 = watchdog.run_cycle(s)
    assert counts2["created"] == 0
    assert len(s.list_recovery_actions(kind="stale_heartbeat")) == 1
    s.close()


def test_stale_heartbeat_threshold_and_workflow_exclusion():
    s = _store()
    # workflow kind는 heartbeat 대상이 아니다(서버 heartbeat 없음)
    w = s.create_agent("wf", "claude", kind="workflow", status="active")
    assert w is not None
    store_update = s.update_agent(w["id"], last_seen_at=_hours_ago(100))
    assert store_update is not None
    # external이지만 최근 heartbeat → 미검출
    _external_agent(s, "fresh", last_seen=_iso(datetime.now(UTC)))
    # external + stale → 검출
    stale = _external_agent(s, "stale", last_seen=_hours_ago(3))
    watchdog.run_cycle(s)
    recs = s.list_recovery_actions(kind="stale_heartbeat")
    sources = {r["source_id"] for r in recs}
    assert stale["id"] in sources
    assert w["id"] not in sources
    s.close()


def test_stale_heartbeat_recovers_on_new_heartbeat():
    s = _store()
    a = _external_agent(s, last_seen=_hours_ago(2))
    watchdog.run_cycle(s)
    rec = s.list_recovery_actions(kind="stale_heartbeat")[0]
    assert rec["status"] == "active"
    # heartbeat 재개 → 다음 cycle에 resolved
    s.update_agent(a["id"], last_seen_at=_iso(datetime.now(UTC)))
    counts = watchdog.run_cycle(s)
    assert counts["resolved"] >= 1
    assert s.get_recovery_action(rec["id"])["status"] == "resolved"
    s.close()


# ---------------- detector: stranded_task ----------------


def test_stranded_task_detected():
    s = _store()
    t = s.create_task("orphan", status="in_progress")
    assert t is not None
    watchdog.run_cycle(s)
    recs = s.list_recovery_actions(kind="stranded_task")
    assert len(recs) == 1 and recs[0]["source_id"] == t["id"]
    s.close()


def test_stranded_task_with_active_run_not_flagged():
    s = _store()
    run = _run(s, status=RunStatus.RUNNING)
    t = s.create_task("working", status="in_progress")
    assert t is not None
    s.update_task(t["id"], current_run_id=run.id)
    watchdog.run_cycle(s)
    assert s.list_recovery_actions(kind="stranded_task") == []
    s.close()


def test_stranded_task_checkout_owner_not_flagged():
    s = _store()
    a = s.create_agent("w", "claude", status="active")
    assert a is not None
    t = s.create_task("checked", status="in_progress")
    assert t is not None
    assert s.checkout_task(t["id"], a["id"])
    watchdog.run_cycle(s)
    assert s.list_recovery_actions(kind="stranded_task") == []
    s.close()


# ---------------- detector: stuck_run / missing_cost / failed_resume ----------------


def test_stuck_run_detected_after_threshold():
    s = _store()
    # waiting_approval + 오래된 updated_at → stuck
    old = _run(s, status=RunStatus.WAITING_APPROVAL, age_hours=30)
    # 최근 waiting_approval → 미검출
    _run(s, status=RunStatus.WAITING_APPROVAL, age_hours=0)
    watchdog.run_cycle(s, batch_limit=50)
    recs = s.list_recovery_actions(kind="stuck_run")
    assert len(recs) == 1 and recs[0]["source_id"] == old.id
    s.close()


def test_stuck_run_terminal_not_flagged():
    s = _store()
    _run(s, status=RunStatus.COMPLETED, age_hours=100)
    watchdog.run_cycle(s)
    assert s.list_recovery_actions(kind="stuck_run") == []
    s.close()


def test_missing_cost_detected_and_backfilled():
    s = _store()
    run = _run(s, status=RunStatus.COMPLETED, age_hours=1)
    sr = s.start_stage(run.id, "llm", 1, {})
    s.finish_stage(sr.id, status="succeeded", output={}, tokens_in=100, tokens_out=50)
    watchdog.run_cycle(s)
    recs = s.list_recovery_actions(kind="missing_cost")
    assert len(recs) == 1 and recs[0]["source_id"] == run.id
    # 첫 cycle의 attempt가 cost backfill을 실행 — cost_events 생성 확인
    assert s.list_cost_events(run_id=run.id)
    # 다음 cycle 건강 재검사 → resolved
    counts = watchdog.run_cycle(s)
    assert counts["resolved"] >= 1
    assert s.get_recovery_action(recs[0]["id"])["status"] == "resolved"
    s.close()


def test_failed_resume_creates_run_resume_approval():
    s = _store()
    run = _run(s, status=RunStatus.WAITING_APPROVAL, age_hours=1)
    s.audit(run.id, "system", "run_resume_error", run.id, {"error": "x"})
    watchdog.run_cycle(s)
    recs = s.list_recovery_actions(kind="failed_resume")
    assert len(recs) == 1
    # attempt가 run_resume approval_request를 생성한다(dedupe)
    approvals = s.list_approval_requests(action_type="run_resume", run_id=run.id)
    assert len(approvals) == 1 and approvals[0]["status"] == "pending"
    # 두 번째 cycle — approval이 pending이면 중복 생성 없음
    watchdog.run_cycle(s)
    assert len(s.list_approval_requests(action_type="run_resume", run_id=run.id)) == 1
    s.close()


# ---------------- 복구 시도/에스컬레이션 ----------------


def test_max_attempts_escalates_with_inbox_and_blocked_task():
    s = _store()
    t = s.create_task("doomed", status="in_progress")
    assert t is not None
    rec, created = s.create_recovery_action(
        source_type="task",
        source_id=t["id"],
        kind="stranded_task",
        max_attempts=1,
    )
    assert created and rec is not None
    # attempt 1회 → attempts=1 >= max=1 → escalated
    watchdog.run_cycle(s)
    fresh = s.get_recovery_action(rec["id"])
    assert fresh is not None and fresh["status"] == "escalated"
    assert fresh["owner_type"] == "human"
    # inbox 알림 + task blocked
    inbox = s.list_inbox()
    assert any(i["kind"] == "recovery" for i in inbox)
    assert s.get_task(t["id"])["status"] == TaskStatus.BLOCKED.value
    s.close()


def test_escalated_not_retried_but_can_resolve():
    s = _store()
    a = _external_agent(s, last_seen=_hours_ago(2))
    rec, _ = s.create_recovery_action(
        source_type="agent", source_id=a["id"], kind="stale_heartbeat", max_attempts=1
    )
    assert rec is not None
    watchdog.run_cycle(s)
    assert s.get_recovery_action(rec["id"])["status"] == "escalated"
    # escalated는 자동 재시도하지 않는다 — attempt_count가 늘지 않는다
    before = s.get_recovery_action(rec["id"])["attempt_count"]
    watchdog.run_cycle(s)
    after = s.get_recovery_action(rec["id"])
    assert after["attempt_count"] == before
    # 수동 resolve 가능
    assert s.set_recovery_status(rec["id"], "resolved", resolution_note="fixed")
    assert s.get_recovery_action(rec["id"])["status"] == "resolved"
    s.close()


def test_recovery_attempt_backoff_delays_next_attempt():
    s = _store()
    a = _external_agent(s, last_seen=_hours_ago(2))
    rec, _ = s.create_recovery_action(
        source_type="agent", source_id=a["id"], kind="stale_heartbeat", max_attempts=5
    )
    assert rec is not None
    watchdog.run_cycle(s)
    fresh = s.get_recovery_action(rec["id"])
    assert fresh is not None and fresh["attempt_count"] == 1
    assert fresh["next_attempt_at"]  # 백오프 예약됨
    # next_attempt_at이 미래 → 즉시 재실행해도 재시도 안 함
    counts = watchdog.run_cycle(s)
    assert counts["retried"] == 0
    s.close()


def test_open_recovery_dedupe_via_unique_guard():
    """같은 source에 active|escalated recovery는 1건만."""
    s = _store()
    rec1, c1 = s.create_recovery_action(source_type="run", source_id="r", kind="stuck_run")
    rec2, c2 = s.create_recovery_action(source_type="run", source_id="r", kind="stuck_run")
    assert c1 and not c2
    assert rec1 is not None and rec2 is not None and rec1["id"] == rec2["id"]
    # resolved 후에는 새 recovery 생성 가능
    s.set_recovery_status(rec1["id"], "resolved")
    rec3, c3 = s.create_recovery_action(source_type="run", source_id="r", kind="stuck_run")
    assert c3 and rec3 is not None and rec3["id"] != rec1["id"]
    s.close()


def test_watchdog_batch_limit():
    s = _store()
    for i in range(5):
        _external_agent(s, f"ext{i}", last_seen=_hours_ago(2))
    counts = watchdog.run_cycle(s, batch_limit=2)
    assert counts["created"] <= 2
    assert len(s.list_recovery_actions()) <= 2
    s.close()


def test_watchdog_run_cycle_returns_counts():
    s = _store()
    _external_agent(s, last_seen=_hours_ago(2))
    counts = watchdog.run_cycle(s)
    for key in ("scanned", "detected", "created", "retried", "escalated", "resolved"):
        assert key in counts
    s.close()


# ---------------- BLOCK-2: detection budget starvation ----------------


def test_detection_dedupe_does_not_starve_retry_phase():
    """만성(dedupe) 소스가 batch를 소비하면 안 된다 — 건강 재검사/재시도
    단계는 detection backlog와 무관하게 항상 실행된다."""
    s = _store()
    # 30개의 만성 stale agent — 이미 open recovery가 있는 상태
    for i in range(30):
        a = _external_agent(s, f"chronic{i}", last_seen=_hours_ago(2))
        rec, created = s.create_recovery_action(
            source_type="agent",
            source_id=a["id"],
            kind="stale_heartbeat",
            max_attempts=5,
        )
        assert created and rec is not None
    # stuck run — detection 후보
    stuck = _run(s, status=RunStatus.WAITING_APPROVAL, age_hours=30)
    # 재시도 대상 open recovery(next_attempt_at 과거)
    a2 = _external_agent(s, "retryme", last_seen=_hours_ago(2))
    rec2, _ = s.create_recovery_action(
        source_type="agent",
        source_id=a2["id"],
        kind="stale_heartbeat",
        max_attempts=5,
    )
    assert rec2 is not None
    with s.tx() as c:
        c.execute(
            "UPDATE recovery_actions SET next_attempt_at=? WHERE id=?",
            (_hours_ago(1), rec2["id"]),
        )
    counts = watchdog.run_cycle(s, batch_limit=5)
    # 만성 31개는 dedupe — 신규 생성은 stuck_run 1건뿐
    assert counts["created"] == 1
    kinds = {r["kind"] for r in s.list_recovery_actions(kind="stuck_run")}
    assert "stuck_run" in kinds
    stuck_recs = [
        r for r in s.list_recovery_actions(kind="stuck_run") if r["source_id"] == stuck.id
    ]
    assert stuck_recs
    # retry 대상 recovery는 detection backlog와 무관하게 attempt됐다
    retried = s.get_recovery_action(rec2["id"])
    assert retried is not None and retried["attempt_count"] >= 1
    assert counts["retried"] >= 1
    s.close()


def test_detection_per_kind_quota_no_detector_starvation():
    """한 detector의 신규 소스가 폭주해도 다른 detector 몫은 남는다."""
    s = _store()
    # stale heartbeat 신규 소스 30개 — per-kind 쿼터를 넘는다
    for i in range(30):
        _external_agent(s, f"flood{i}", last_seen=_hours_ago(2))
    stuck = _run(s, status=RunStatus.WAITING_APPROVAL, age_hours=30)
    counts = watchdog.run_cycle(s, batch_limit=10)
    # 총 생성은 batch 상한 이내
    assert counts["created"] <= 10
    # stuck_run detector도 자기 몫을 받아 recovery가 생성된다
    assert any(r["source_id"] == stuck.id for r in s.list_recovery_actions(kind="stuck_run"))
    s.close()


# ---------------- M-1: missing_cost stage_run-tagged coverage ----------------


def test_missing_cost_stage_run_tagged_event_covers():
    """stage_run_id만 연결된 cost event(run_id NULL)도 커버로 인정 —
    외부 보고 경로의 정상 이벤트가 missing_cost 오탐을 만들지 않는다."""
    s = _store()
    run = _run(s, status=RunStatus.COMPLETED, age_hours=1)
    sr = s.start_stage(run.id, "llm", 1, {})
    s.finish_stage(sr.id, status="succeeded", output={}, tokens_in=100, tokens_out=50)
    # 외부 보고 형식: stage_run_id만, run_id NULL
    row, created = s.record_cost_event(
        idempotency_key="ext:evt1",
        stage_run_id=sr.id,
        cost_status="reported",
        cost_microusd=1234,
    )
    assert created and row is not None
    watchdog.run_cycle(s)
    assert s.list_recovery_actions(kind="missing_cost") == []
    s.close()


def test_missing_cost_uncovered_stage_run_detected():
    """run-level cost event가 있어도 개별 stage_run이 미커버면 검출 —
    stage_run_id 커버리지가 우선이다."""
    s = _store()
    run = _run(s, status=RunStatus.COMPLETED, age_hours=1)
    s1 = s.start_stage(run.id, "llm", 1, {})
    s.finish_stage(s1.id, status="succeeded", output={}, tokens_in=100, tokens_out=50)
    s2 = s.start_stage(run.id, "llm", 2, {})
    s.finish_stage(s2.id, status="succeeded", output={}, tokens_in=100, tokens_out=50)
    # s1만 stage_run 태그 — s2는 미커버
    s.record_cost_event(
        idempotency_key="ext:only-s1",
        stage_run_id=s1.id,
        cost_status="reported",
        cost_microusd=100,
    )
    watchdog.run_cycle(s)
    recs = s.list_recovery_actions(kind="missing_cost")
    assert len(recs) == 1 and recs[0]["source_id"] == run.id
    s.close()


def test_missing_cost_health_check_stage_run_coverage():
    """_source_recovered도 stage_run 커버리지를 본다 — stage_run 태그
    이벤트만 생겨도 recovery가 resolved된다."""
    s = _store()
    run = _run(s, status=RunStatus.COMPLETED, age_hours=1)
    sr = s.start_stage(run.id, "llm", 1, {})
    s.finish_stage(sr.id, status="succeeded", output={}, tokens_in=100, tokens_out=50)
    rec, created = s.create_recovery_action(
        source_type="run", source_id=run.id, kind="missing_cost"
    )
    assert created and rec is not None
    # 첫 cycle: attempt(backfill — 토큰 stage를 estimated로 기록할 수 있음)
    # 또는 그대로 두고 stage_run 태그 이벤트를 직접 기록
    s.record_cost_event(
        idempotency_key="ext:cover",
        stage_run_id=sr.id,
        cost_status="reported",
        cost_microusd=100,
    )
    watchdog.run_cycle(s)
    fresh = s.get_recovery_action(rec["id"])
    assert fresh is not None and fresh["status"] == "resolved"
    s.close()
