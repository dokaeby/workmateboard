"""spec 006 US2 — 예산 임계 액션(threshold_action) 테스트.

alert/pause/require_approval 3종 정책의 admission 평가, require_approval
run 대기 마킹, 승인 재심사·거절·TTL 만료, 승인 후 실행 발사를 검증한다.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from jobmate import budget_scope
from jobmate.api import jobs as api_jobs
from jobmate.models import RunStatus
from jobmate.store.db import Store


def _store() -> Store:
    return Store(str(Path(tempfile.mkdtemp()) / "t.db"))


@pytest.fixture(autouse=True)
def _clear():
    budget_scope._spend_cache.clear()
    yield
    budget_scope._spend_cache.clear()


def _add_spend(store: Store, run_id: str, tokens_out: int = 0) -> None:
    sr = store.start_stage(run_id, "llm", 1, {})
    store.finish_stage(sr.id, status="succeeded", output={}, tokens_out=tokens_out)


def _mk_run(store: Store, workflow="dev_review", project="") -> str:
    inp = {"task": "t"}
    if project:
        inp["project"] = project
    return store.create_run(workflow, "1", inp, trace_id="t").id


def _policy(store, scope_type="global", scope_id="", **kw):
    return budget_scope.upsert_policy(
        store, scope_type, scope_id, amount_usd=kw.pop("amount_usd", 10.0), **kw
    )


# ---- threshold_action admission ----


def test_threshold_action_pause_blocks_at_hard_limit():
    s = _store()
    r = _mk_run(s)
    _add_spend(s, r, tokens_out=1_000_000)  # $15 > $10
    _policy(s, threshold_action="pause")
    blocks = budget_scope.evaluate_admission(s, "dev_review", "")
    assert blocks


def test_threshold_action_pause_blocks_without_hard_stop_flag():
    """pause는 hard_stop_enabled와 무관하게 한도 도달 시 차단한다."""
    s = _store()
    r = _mk_run(s)
    _add_spend(s, r, tokens_out=1_000_000)
    _policy(s, threshold_action="pause", hard_stop_enabled=False)
    assert budget_scope.evaluate_admission(s, "dev_review", "")


def test_threshold_action_alert_preserves_legacy_block():
    """alert(기본)는 기존 동작 유지 — hard_stop_enabled면 한도 차단한다
    (기존 행의 alert 마이그레이션이 비용 보호를 끄지 않도록)."""
    s = _store()
    r = _mk_run(s)
    _add_spend(s, r, tokens_out=1_000_000)
    _policy(s, threshold_action="alert", hard_stop_enabled=True)
    assert budget_scope.evaluate_admission(s, "dev_review", "")


def test_threshold_action_alert_does_not_block():
    """alert + hard_stop_enabled=False → 인시던트/알림만, run 생성 허용."""
    s = _store()
    r = _mk_run(s)
    _add_spend(s, r, tokens_out=1_000_000)
    _policy(s, threshold_action="alert", hard_stop_enabled=False)
    assert budget_scope.evaluate_admission(s, "dev_review", "") == []


def test_threshold_action_require_approval_no_block_but_pending():
    s = _store()
    seed = _mk_run(s)
    _add_spend(s, seed, tokens_out=1_000_000)
    _policy(s, threshold_action="require_approval")
    # admission 자체는 차단하지 않는다 — 승인 대기로 전환된다
    assert budget_scope.evaluate_admission(s, "dev_review", "") == []
    pending = budget_scope.pending_budget_approvals(s, "dev_review", "")
    assert len(pending) >= 1
    assert pending[0]["scope_type"] == "global"


def test_unpriced_usage_block_still_blocks():
    s = _store()
    _policy(s, threshold_action="alert", unpriced_usage_policy="block")
    # 윈도우 내 unpriced cost_event → block 유지 (alert여도 차단)
    rid = _mk_run(s)
    s.record_cost_event(
        idempotency_key=f"unpriced-{rid}",
        run_id=rid,
        model="unknown-model-xyz",
        cost_status="unpriced",
        input_tokens=10,
    )
    blocks = budget_scope.evaluate_admission(s, "dev_review", "")
    assert blocks


# ---- require_approval run 대기 플로우 ----


def test_pending_marks_run_and_blocks_execute():
    """require_approval 정책 + 임계 초과 → run 생성 시 waiting 마킹."""
    s = _store()
    seed = _mk_run(s)
    _add_spend(s, seed, tokens_out=1_000_000)
    _policy(s, threshold_action="require_approval")

    rid = _mk_run(s)
    # 생성 훅(store/runs.py)이 pending을 보고 자동으로 waiting 마킹한다
    assert budget_scope.pending_budget_approvals(s, "dev_review", "")
    assert s.run_budget_approval_state(rid) == "waiting"
    assert s.get_run(rid).status.value == "waiting_approval"


def test_approve_clears_waiting_and_schedules():
    s = _store()
    seed = _mk_run(s)
    _add_spend(s, seed, tokens_out=1_000_000)
    _policy(s, threshold_action="require_approval")
    rid = _mk_run(s)
    assert s.run_budget_approval_state(rid) == "waiting"

    # 승인 — 예산이 여전히 초과면 approve_run_budget_approval 전에
    # 라우터가 재심사로 막는다. 여기서는 store 메서드 단위 검증:
    assert s.approve_run_budget_approval(rid)
    assert s.run_budget_approval_state(rid) == "approved"
    assert s.get_run(rid).status.value == "queued"
    # dispatched 마킹은 1회만 성공
    assert s.mark_budget_approval_dispatched(rid)
    assert not s.mark_budget_approval_dispatched(rid)


def test_reject_marks_cancelled():
    s = _store()
    seed = _mk_run(s)
    _add_spend(s, seed, tokens_out=1_000_000)
    _policy(s, threshold_action="require_approval")
    rid = _mk_run(s)
    assert s.run_budget_approval_state(rid) == "waiting"
    assert s.reject_run_budget_approval(rid)
    assert s.get_run(rid).status.value == "rejected"
    # approved 목록에 나오지 않는다
    assert all(r["id"] != rid for r in s.list_budget_approval_approved())


def test_ttl_expiry_cancels_waiting():
    s = _store()
    seed = _mk_run(s)
    _add_spend(s, seed, tokens_out=1_000_000)
    _policy(s, threshold_action="require_approval")
    rid = _mk_run(s)
    assert s.run_budget_approval_state(rid) == "waiting"
    # requested_at을 과거로 당겨 TTL(1h) 만료 대상이 되게 한다
    import json as _json
    from datetime import UTC, datetime, timedelta

    row = s._conn.execute("SELECT metadata FROM runs WHERE id=?", (rid,)).fetchone()
    meta = _json.loads(row["metadata"])
    meta["budget_approval"]["requested_at"] = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    s._conn.execute(
        "UPDATE runs SET metadata=? WHERE id=?",
        (_json.dumps(meta), rid),
    )
    s._conn.commit()
    expired = s.expire_budget_approval_waits(1)
    assert rid in expired
    assert s.get_run(rid).status.value == "cancelled"


def test_dispatch_cycle_executes_approved(monkeypatch):
    """automation cycle의 _process_budget_approvals가 approved run을 발사한다."""
    s = _store()
    seed = _mk_run(s)
    _add_spend(s, seed, tokens_out=1_000_000)
    _policy(s, threshold_action="require_approval")
    rid = _mk_run(s)
    assert s.run_budget_approval_state(rid) == "waiting"
    s.approve_run_budget_approval(rid)

    scheduled = []

    class _Settings:
        pass

    sched = lambda run, payload: scheduled.append(run.id)  # noqa: E731
    api_jobs._process_budget_approvals(s, _Settings(), llm=None, adapters={}, schedule=sched)
    assert scheduled == [rid]
    # 두 번째 호출은 dispatched라 재발사 없음
    api_jobs._process_budget_approvals(s, _Settings(), llm=None, adapters={}, schedule=sched)
    assert scheduled == [rid]


def test_execute_guard_skips_waiting(monkeypatch):
    """_execute_run_job이 budget waiting run을 실행하지 않는다."""
    s = _store()
    seed = _mk_run(s)
    _add_spend(s, seed, tokens_out=1_000_000)
    _policy(s, threshold_action="require_approval")
    rid = _mk_run(s)
    assert s.run_budget_approval_state(rid) == "waiting"

    called = []

    class _Runner:
        @staticmethod
        def invoke(*a, **kw):
            called.append(1)

    # org rescope 없이 직접 호출 — guard가 먼저 반환해야 한다
    api_jobs._execute_run_job(
        s,
        settings=None,
        llm=None,
        adapters={},
        run_id=rid,
        workflow="dev_review",
        payload={"task": "t"},
        ctx=None,
    )
    assert called == []
    assert s.get_run(rid).status.value == "waiting_approval"


# ---- 정책 upsert threshold_action ----


def test_upsert_policy_threshold_action_roundtrip():
    s = _store()
    p = _policy(s, threshold_action="require_approval")
    assert p["threshold_action"] == "require_approval"
    # 재upsert로 alert로 변경
    p2 = _policy(s, threshold_action="alert")
    assert p2["id"] == p["id"] and p2["threshold_action"] == "alert"


def test_list_policies_with_spend_reports_action():
    s = _store()
    _policy(s, threshold_action="pause")
    rows = budget_scope.list_policies_with_spend(s)
    assert any(r.get("threshold_action") == "pause" for r in rows)


# ---- API 수준 require_approval 플로우 ----


def _api_client(store, db_path, monkeypatch):
    from fastapi.testclient import TestClient

    from jobmate.api.app import create_app
    from jobmate.config import Settings
    from tests.integration.helpers import default_outputs, scripted_llm

    settings = Settings(db_path=db_path, budget_max_tokens=100_000)
    app = create_app(settings=settings, store=store, llm=scripted_llm(default_outputs()))
    client = TestClient(app)
    client.headers["X-API-Key"] = "budget-test-key"
    return client


def test_api_run_budget_approval_flow(monkeypatch):
    """POST /runs → waiting_approval(budget) → approve → queued 실행 예약,
    reject → rejected. API 계약 수준 검증."""
    db = str(Path(tempfile.mkdtemp()) / "t.db")
    s = Store(db)
    seed = s.create_run("dev_review", "1", {"task": "t"}, trace_id="seed").id
    sr = s.start_stage(seed, "llm", 1, {})
    s.finish_stage(sr.id, status="succeeded", output={}, tokens_out=1_000_000)
    budget_scope.upsert_policy(
        s, "global", "", amount_usd=10.0, threshold_action="require_approval"
    )
    client = _api_client(s, db, monkeypatch)

    r = client.post("/api/v1/runs", json={"workflow": "dev_review", "task": "hello"})
    assert r.status_code == 202, r.text
    body = r.json()
    assert body.get("reason") == "budget_approval"
    assert body.get("status") == "waiting_approval"
    rid = body["id"]
    assert s.run_budget_approval_state(rid) == "waiting"

    # 거절 경로 — 아직 임계 초과 상태에서 두 번째 run 생성 후 reject
    r3 = client.post("/api/v1/runs", json={"workflow": "dev_review", "task": "again"})
    assert r3.status_code == 202, r3.text
    rid2 = r3.json()["id"]
    r4 = client.post(f"/api/v1/runs/{rid2}/approve", json={"decision": "reject"})
    assert r4.status_code in (200, 202), r4.text
    assert s.get_run(rid2).status.value == "rejected"

    # 승인 시점 재심사 — 여전히 초과면 409 budget_still_exceeded로 재대기
    r5 = client.post(f"/api/v1/runs/{rid}/approve", json={"decision": "approve"})
    assert r5.status_code == 409, r5.text
    assert s.run_budget_approval_state(rid) == "waiting"

    # 예산 상향 후 승인 → queued 전이 + 실행 예약
    budget_scope.upsert_policy(
        s, "global", "", amount_usd=100.0, threshold_action="require_approval"
    )
    r6 = client.post(f"/api/v1/runs/{rid}/approve", json={"decision": "approve"})
    assert r6.status_code in (200, 202), r6.text
    assert r6.json().get("budget_approved") is True
    # _budget_approve가 approved→dispatched를 원자 claim하므로 승인 직후
    # 마커는 dispatched다 — _process_budget_approvals 재스캔이 재발사하지
    # 않는다는 뜻(H2 단일 발사). background 실행 완료 여부와 무관하게
    # 마커는 dispatched에 머문다.
    assert s.run_budget_approval_state(rid) == "dispatched"
    # run 자체는 승인 후 실행돼 dev_review의 human approval에서
    # waiting_approval에 도달할 수 있다 — budget 마커(dispatched)와
    # 구분되므로 상태값이 아니라 마커로 검증한다.


def test_api_run_budget_pause_still_429():
    """threshold_action=pause 정책은 기존대로 429 budget_exceeded."""
    db = str(Path(tempfile.mkdtemp()) / "t.db")
    s = Store(db)
    seed = s.create_run("dev_review", "1", {"task": "t"}, trace_id="seed").id
    sr = s.start_stage(seed, "llm", 1, {})
    s.finish_stage(sr.id, status="succeeded", output={}, tokens_out=1_000_000)
    budget_scope.upsert_policy(s, "global", "", amount_usd=10.0, threshold_action="pause")
    client = _api_client(s, db, monkeypatch=None)
    r = client.post("/api/v1/runs", json={"workflow": "dev_review", "task": "x"})
    assert r.status_code == 429, r.text
    assert r.json().get("error", {}).get("code") == "budget_exceeded" or "budget" in r.text


# ---- spec 006 US2 — retry budget enforcement / dispatch 단일화 / resume ----


def _seed_over_limit(store: Store) -> str:
    """전역 스코프 지출을 정책 한도 초과 상태로 만든다 — $15 지출."""
    seed = _mk_run(store)
    _add_spend(store, seed, tokens_out=1_000_000)
    return seed


def _terminal_run(store: Store, task: str = "retry-me"):
    """retry 가능한 terminal 상태의 run — 정책 적용 전에 생성한다."""
    run = store.create_run("dev_review", "1", {"task": task}, trace_id=f"t-{task}")
    store.update_run(run.id, status=RunStatus.COMPLETED)
    return run


def test_retry_blocked_by_pause_policy_429():
    """H1 — threshold_action=pause 정책 하에서 retry는 429 budget_exceeded.

    retry가 예산 게이트를 우회하면 pause 정책이 재실행 비용을 막지 못한다.
    거부 시 run은 terminal 상태 그대로여야 한다(reset 미실행)."""
    db = str(Path(tempfile.mkdtemp()) / "t.db")
    s = Store(db)
    _seed_over_limit(s)
    target = _terminal_run(s)
    budget_scope.upsert_policy(s, "global", "", amount_usd=10.0, threshold_action="pause")
    client = _api_client(s, db, monkeypatch=None)

    r = client.post(f"/api/v1/runs/{target.id}/retry?from_stage=qa")
    assert r.status_code == 429, r.text
    assert "budget" in r.text
    # 상태 전이 전에 거부 — completed 유지, 재실행 예약 없음
    assert s.get_run(target.id).status == RunStatus.COMPLETED


def test_retry_require_approval_marks_waiting():
    """H1 — require_approval 정책 하에서 retry는 승인 대기로 전이된다.

    reset(queued) 이후 create_run과 동일한 마킹 경로가 pending을 평가해
    waiting_approval + marker state=waiting으로 되돌린다. 실행은
    _execute_run_job 가드가 차단한다."""
    db = str(Path(tempfile.mkdtemp()) / "t.db")
    s = Store(db)
    _seed_over_limit(s)
    target = _terminal_run(s)
    budget_scope.upsert_policy(
        s, "global", "", amount_usd=10.0, threshold_action="require_approval"
    )
    client = _api_client(s, db, monkeypatch=None)

    r = client.post(f"/api/v1/runs/{target.id}/retry?from_stage=qa")
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "waiting_approval"
    assert body.get("reason") == "budget_approval"
    assert body.get("from_stage") == "qa"
    assert s.run_budget_approval_state(target.id) == "waiting"
    assert s.get_run(target.id).status == RunStatus.WAITING_APPROVAL


def test_retry_budget_clear_runs_normally():
    """예산이 충분하면 retry는 기존대로 queued + 실행 예약."""
    db = str(Path(tempfile.mkdtemp()) / "t.db")
    s = Store(db)
    target = _terminal_run(s)
    client = _api_client(s, db, monkeypatch=None)
    r = client.post(f"/api/v1/runs/{target.id}/retry?from_stage=qa")
    assert r.status_code == 202, r.text
    assert r.json()["status"] == "queued"
    assert s.run_budget_approval_state(target.id) == ""


def test_expired_budget_wait_retry_clears_marker():
    """M1 — TTL 만료(cancelled + stale waiting 마커) run의 retry는 마커를
    'cleared'로 정규화해 실행 차단/approve 409 브릭을 막는다."""
    s = _store()
    _seed_over_limit(s)
    _policy(s, threshold_action="require_approval")
    rid = _mk_run(s)
    assert s.run_budget_approval_state(rid) == "waiting"

    # requested_at을 과거로 당겨 TTL 만료시킨다
    import json as _json
    from datetime import UTC, datetime, timedelta

    row = s._conn.execute("SELECT metadata FROM runs WHERE id=?", (rid,)).fetchone()
    meta = _json.loads(row["metadata"])
    meta["budget_approval"]["requested_at"] = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    s._conn.execute("UPDATE runs SET metadata=? WHERE id=?", (_json.dumps(meta), rid))
    s._conn.commit()

    assert rid in s.expire_budget_approval_waits(1)
    assert s.get_run(rid).status == RunStatus.CANCELLED
    # stale waiting 마커가 남아있는 상태 — 이게 브릭의 원인
    assert s.run_budget_approval_state(rid) == "waiting"

    assert s.reset_run_for_retry(rid, "qa") is True
    assert s.get_run(rid).status == RunStatus.QUEUED
    assert s.run_budget_approval_state(rid) == "cleared"
    # _execute_run_job 가드가 더 이상 차단하지 않는다
    assert s.run_budget_approval_state(rid) != "waiting"


def test_reset_retry_does_not_touch_active_wait_marker():
    """M1 대조 — waiting_approval run은 retryable 상태가 아니므로
    reset_run_for_retry가 거부되고 마커도 그대로다."""
    s = _store()
    _seed_over_limit(s)
    _policy(s, threshold_action="require_approval")
    rid = _mk_run(s)
    assert s.run_budget_approval_state(rid) == "waiting"
    assert s.reset_run_for_retry(rid, "qa") is False
    assert s.run_budget_approval_state(rid) == "waiting"
    assert s.get_run(rid).status == RunStatus.WAITING_APPROVAL


def test_approved_run_not_rescheduled_by_cycle():
    """H2 — dispatched 마킹된 run은 _process_budget_approvals가 재발사하지
    않는다(승인 직후 이미 dispatched로 claim됐다는 전제)."""
    s = _store()
    _seed_over_limit(s)
    _policy(s, threshold_action="require_approval")
    rid = _mk_run(s)
    assert s.run_budget_approval_state(rid) == "waiting"

    # _budget_approve가 수행하는 전이 순서: approve → dispatched claim
    assert s.approve_run_budget_approval(rid)
    assert s.mark_budget_approval_dispatched(rid)

    scheduled: list[str] = []

    class _Settings:
        pass

    api_jobs._process_budget_approvals(
        s,
        _Settings(),
        llm=None,
        adapters={},
        schedule=lambda run, payload: scheduled.append(run.id),
    )
    assert scheduled == []
    assert s.run_budget_approval_state(rid) == "dispatched"


def test_raise_budget_and_resume_reapproves_waiting_run():
    """raise_budget_and_resume → 새 한도가 관측 지출보다 크면 budget 승인
    대기 run이 approved(queued)로 자동 재개된다."""
    s = _store()
    _seed_over_limit(s)  # $15 관측 지출
    _policy(s, threshold_action="require_approval")  # 한도 $10
    rid = _mk_run(s)
    assert s.run_budget_approval_state(rid) == "waiting"
    assert s.get_run(rid).status == RunStatus.WAITING_APPROVAL

    # 인시던트는 evaluate_admission의 임계 판정에서 생성된다
    budget_scope.evaluate_admission(s, "dev_review", "")
    inc = s.list_budget_incidents(status="open")
    assert inc, "expected an open budget incident"

    out = budget_scope.resolve_incident(
        s, inc[0]["id"], "raise_budget_and_resume", amount_usd=20.0, actor="lead@x"
    )
    assert out is not None and out["status"] == "resolved"
    # 새 한도 $20 > 지출 $15 → 재심사 통과 → approved + queued
    assert s.run_budget_approval_state(rid) == "approved"
    assert s.get_run(rid).status == RunStatus.QUEUED
