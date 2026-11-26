"""approval_requests 서비스 테스트 — paperclip spec 2차 D.

- 생성/결정 CAS(단 한 번), 만료 lazy-expiry
- safe_payload secret-like key 거부
- 액션별 부수효과: run_resume/budget_raise/agent_terminate/recovery
- 부수효과 실패 시 승인 기록 유지 + 에러 감사
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from jobmate import budget_scope
from jobmate.services import agent_management as mgmt
from jobmate.services import approvals
from jobmate.store.db import Store
from jobmate.store.util import _iso


def _store() -> Store:
    return Store(str(Path(tempfile.mkdtemp()) / "t.db"))


def _mk(store: Store, **kw) -> dict:
    row, created = approvals.request_approval(store, **kw)
    assert created and row is not None
    return row


def _run(store: Store):
    """실제 run — request_approval이 run_id 존재를 검증한다(L3)."""
    return store.create_run("dev_review", "1", {"task": "t"}, trace_id="x")


# ---------------- 생성/결정 ----------------


def test_create_and_decide_approve():
    s = _store()
    row = _mk(
        s,
        action_type="deploy",
        risk_level="low",
        summary="ship it",
        resource_type="service",
        resource_id="web",
    )
    assert row["status"] == "pending"
    out = approvals.decide_approval(s, row["id"], "approve", decided_by="lead@x", actor="lead@x")
    assert out["status"] == "approved"
    assert out["decided_by"] == "lead@x"
    assert out["decided_at"]
    s.close()


def test_decide_reject_and_double_decision_blocked():
    s = _store()
    row = _mk(s, action_type="deploy", summary="x")
    approvals.decide_approval(s, row["id"], "reject", decided_by="u", actor="u")
    # 두 번째 결정 — CAS rowcount 0 → 409
    with pytest.raises(mgmt.ManagementError) as ei:
        approvals.decide_approval(s, row["id"], "approve", decided_by="u2", actor="u2")
    assert ei.value.status_code == 409
    fresh = s.get_approval_request(row["id"])
    assert fresh is not None and fresh["status"] == "rejected"
    s.close()


def test_concurrent_decisions_only_one_wins():
    """동일 pending에 approve/reject를 연달아 — 첫 CAS만 성공."""
    s = _store()
    row = _mk(s, action_type="deploy", summary="x")
    first = approvals.decide_approval(s, row["id"], "approve", decided_by="a", actor="a")
    assert first["status"] == "approved"
    with pytest.raises(mgmt.ManagementError):
        approvals.decide_approval(s, row["id"], "reject", decided_by="b", actor="b")
    s.close()


def test_decide_invalid_and_missing():
    s = _store()
    with pytest.raises(mgmt.ManagementError) as ei:
        approvals.decide_approval(s, "nope", "approve")
    assert ei.value.status_code == 404
    row = _mk(s, action_type="deploy")
    with pytest.raises(mgmt.ManagementError) as ei2:
        approvals.decide_approval(s, row["id"], "maybe")
    assert ei2.value.status_code == 422
    s.close()


def test_expired_pending_cannot_be_decided():
    s = _store()
    past = _iso(datetime.now(UTC) - timedelta(hours=1))
    row = _mk(
        s,
        action_type="deploy",
        expires_at=past,
    )
    fresh = s.get_approval_request(row["id"])
    assert fresh is not None and fresh["status"] == "expired"
    with pytest.raises(mgmt.ManagementError) as ei:
        approvals.decide_approval(s, row["id"], "approve", decided_by="u", actor="u")
    assert ei.value.status_code == 409
    s.close()


def test_invalid_action_type_and_risk_rejected():
    s = _store()
    with pytest.raises(mgmt.ManagementError) as ei:
        approvals.request_approval(s, action_type="bogus")
    assert ei.value.status_code == 422
    with pytest.raises(mgmt.ManagementError):
        approvals.request_approval(s, action_type="deploy", risk_level="extreme")
    s.close()


# ---------------- safe_payload secret 거부 ----------------


@pytest.mark.parametrize(
    "payload",
    [
        {"api_key": "x"},
        {"nested": {"password": "x"}},
        {"Authorization": "Bearer x"},
        {"dsn": "postgres://u:p@h/db"},
        {"items": [{"secret_token": "x"}]},
        {"monkey": {"business_key": "v"}},  # 'key' 부분문자열 — 보수적 거부
    ],
)
def test_safe_payload_secret_keys_rejected(payload):
    s = _store()
    with pytest.raises(mgmt.ManagementError) as ei:
        approvals.request_approval(s, action_type="deploy", safe_payload=payload)
    assert ei.value.status_code == 422
    assert ei.value.code == "secret_payload"
    # 거부된 요청은 저장되지 않는다
    assert s.list_approval_requests() == []
    s.close()


def test_safe_payload_clean_and_non_dict():
    s = _store()
    row = _mk(
        s,
        action_type="deploy",
        safe_payload={"endpoint": "https://x", "replicas": 2},
    )
    assert row["safe_payload"] == {"endpoint": "https://x", "replicas": 2}
    with pytest.raises(mgmt.ManagementError):
        approvals.request_approval(s, action_type="deploy", safe_payload=["x"])  # type: ignore[arg-type]
    s.close()


# ---------------- 부수효과 ----------------


def test_run_resume_approve_calls_resume_fn():
    s = _store()
    calls: list[tuple] = []

    def fake_resume(run_id, decision, comment, actor):
        calls.append((run_id, decision, comment, actor))
        return {"ok": True}

    run = _run(s)
    row = _mk(
        s,
        action_type="run_resume",
        run_id=run.id,
        resource_type="run",
        resource_id=run.id,
    )
    approvals.decide_approval(
        s, row["id"], "approve", decided_by="u", actor="u", resume_fn=fake_resume
    )
    assert calls == [(run.id, "approve", "", "u")]
    s.close()


def test_run_resume_reject_calls_resume_fn_with_reject():
    s = _store()
    calls: list[tuple] = []

    def fake_resume(run_id, decision, comment, actor):
        calls.append((run_id, decision))
        return {"ok": True}

    run = _run(s)
    row = _mk(s, action_type="run_resume", run_id=run.id)
    approvals.decide_approval(
        s, row["id"], "reject", decided_by="u", actor="u", resume_fn=fake_resume
    )
    assert calls == [(run.id, "reject")]
    s.close()


def test_run_resume_failure_keeps_approval_decided():
    """spec D — resume 실패 시 승인 기록을 되돌리지 않고 error 감사만."""
    s = _store()

    def bad_resume(run_id, decision, comment, actor):
        return {"ok": False, "error": "boom"}

    run = _run(s)
    row = _mk(s, action_type="run_resume", run_id=run.id)
    out = approvals.decide_approval(
        s, row["id"], "approve", decided_by="u", actor="u", resume_fn=bad_resume
    )
    assert out["status"] == "approved"  # 결정은 유지
    audits = [a for a in s.get_audit(run.id) if a.action == "approval_side_effect_error"]
    assert audits, "side-effect failure must be audited"
    s.close()


def test_budget_raise_updates_policy():
    s = _store()
    budget_scope.upsert_policy(s, "global", "", amount_microusd=1_000_000)
    row = _mk(
        s,
        action_type="budget_raise",
        safe_payload={"scope_type": "global", "scope_id": "", "amount_microusd": 5_000_000},
        resource_type="budget_policy",
        resource_id="global",
    )
    approvals.decide_approval(s, row["id"], "approve", decided_by="u", actor="u")
    pol = s.get_budget_policy("global", "")
    assert pol is not None and int(pol["amount_microusd"]) == 5_000_000
    s.close()


def test_budget_raise_via_incident():
    s = _store()
    pol = budget_scope.upsert_policy(s, "global", "", amount_microusd=1_000_000)
    assert pol is not None
    inc_id = s.create_budget_incident(
        str(pol["id"]),
        "global",
        "",
        "2025-01",
        "hard_stop",
        1.0,
        2.0,
        observed_spend_microusd=2_000_000,
    )
    assert inc_id is not None
    row = _mk(
        s,
        action_type="budget_raise",
        safe_payload={"incident_id": inc_id, "amount_microusd": 3_000_000},
    )
    approvals.decide_approval(s, row["id"], "approve", decided_by="u", actor="u")
    inc2 = s.get_budget_incident(inc_id)
    assert inc2 is not None and inc2["status"] == "resolved"
    s.close()


def test_agent_terminate_side_effect_and_guard():
    s = _store()
    a = s.create_agent("dev", "claude", status="active")
    assert a is not None
    # 승인 없이 terminated 전이 → 409 approval_required
    with pytest.raises(mgmt.ManagementError) as ei:
        mgmt.transition_agent(s, a["id"], "terminated")
    assert ei.value.status_code == 409
    assert ei.value.code == "approval_required"
    # 승인 → 부수효과로 terminated
    row = _mk(
        s,
        action_type="agent_terminate",
        risk_level="high",
        resource_type="agent",
        resource_id=a["id"],
        safe_payload={"agent_id": a["id"]},
    )
    approvals.decide_approval(s, row["id"], "approve", decided_by="u", actor="u")
    assert s.get_agent(a["id"])["status"] == "terminated"
    s.close()


def test_recovery_approval_resolves_action():
    s = _store()
    rec, created = s.create_recovery_action(source_type="run", source_id="r1", kind="stuck_run")
    assert created and rec is not None
    row = _mk(
        s,
        action_type="recovery",
        safe_payload={"recovery_id": rec["id"]},
        resource_type="recovery_action",
        resource_id=rec["id"],
    )
    approvals.decide_approval(s, row["id"], "approve", decided_by="u", actor="u")
    fresh = s.get_recovery_action(rec["id"])
    assert fresh is not None and fresh["status"] == "resolved"
    s.close()


def test_approval_audit_and_listing():
    s = _store()
    run = _run(s)
    row = _mk(s, action_type="deploy", run_id=run.id, summary="x")
    approvals.decide_approval(s, row["id"], "approve", decided_by="u", actor="u")
    audits = [a for a in s.get_audit(run.id) if a.action == "approval_approve"]
    assert audits and audits[0].object_ref == row["id"]
    pending = s.list_approval_requests(status="pending")
    assert all(p["id"] != row["id"] for p in pending)
    by_run = s.list_approval_requests(run_id=run.id)
    assert len(by_run) == 1
    s.close()


# ---------------- 보안 리뷰 2차: H1/H2/L3 ----------------


def test_dangling_refs_rejected_at_creation():
    """L3 — 존재하지 않는 agent/task/run 참조는 422."""
    s = _store()
    with pytest.raises(mgmt.ManagementError) as e1:
        approvals.request_approval(s, action_type="deploy", run_id="ghost-run")
    assert e1.value.status_code == 422
    with pytest.raises(mgmt.ManagementError):
        approvals.request_approval(s, action_type="deploy", requested_by_agent_id="ghost-agent")
    s.close()


def test_agent_terminate_mismatch_blocked_at_decision():
    """H1 — 생성 검증을 우회해 저장된 mismatch row도 결정 전에 422로
    차단되고 승인은 pending으로 남는다(결정+부수효과 원자성)."""
    s = _store()
    a = s.create_agent("va", "claude", status="active")
    b = s.create_agent("vb", "claude", status="active")
    assert a is not None and b is not None
    # store 직접 insert — 서비스 계층 검증을 거치지 않은 row
    row = s.create_approval_request(
        action_type="agent_terminate",
        risk_level="high",
        resource_type="agent",
        resource_id=a["id"],
        safe_payload={"agent_id": b["id"]},
    )
    assert row is not None
    with pytest.raises(mgmt.ManagementError) as ei:
        approvals.decide_approval(s, row["id"], "approve", decided_by="u", actor="u")
    assert ei.value.status_code == 422
    fresh = s.get_approval_request(row["id"])
    assert fresh is not None and fresh["status"] == "pending"
    # 어느 agent도 terminate되지 않음
    assert s.get_agent(a["id"])["status"] == "active"
    assert s.get_agent(b["id"])["status"] == "active"
    s.close()


def test_min_risk_floor_matrix():
    """C1 — action_type별 최소 risk가 요청값을 상향 보정한다."""
    s = _store()
    row, _ = approvals.request_approval(
        s,
        action_type="agent_terminate",
        risk_level="low",
        resource_type="agent",
        resource_id="x",
    )
    # agent_terminate는 resource_id 실존 검증은 없지만 risk 보정만 확인 —
    # resource_id='x'는 agent이 아니므로 get_agent 검증은 없다.
    assert row is not None and row["risk_level"] == "high"
    row2, _ = approvals.request_approval(
        s,
        action_type="recovery",
        risk_level="low",
        safe_payload={"recovery_id": "r1"},
        resource_id="r1",
    )
    assert row2 is not None and row2["risk_level"] == "low"
    s.close()
