"""governance 2차 API 테스트 — /api/v1/management/approvals,
recovery-actions, watchdog/run (paperclip spec D/E/F)."""

import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from jobmate.api.app import create_app
from jobmate.config import Settings
from jobmate.models import RunStatus
from jobmate.security import hash_token
from jobmate.store.db import Store

API_KEY = "gov-test-key"


def _client():
    db = str(Path(tempfile.mkdtemp()) / "t.db")
    settings = Settings(db_path=db, budget_max_tokens=100_000)
    store = Store(db)
    app = create_app(settings=settings, store=store, llm=None, adapters={})
    return TestClient(app), store


def _machine() -> dict[str, str]:
    return {"X-API-Key": API_KEY}


def _session(client: TestClient, store: Store, email: str, role: str) -> None:
    user = store.create_user(email, password_hash="x" * 32, email_verified=True)
    assert user is not None
    store.set_user_role(user["id"], role)
    token = f"sess-{email}"
    store.create_session(user["id"], hash_token(token))
    client.cookies.set("jobmate_session", token)


# ---------------- approvals CRUD/decision ----------------


def test_approval_create_list_decide():
    client, store = _client()
    r = client.post(
        "/api/v1/management/approvals",
        json={
            "action_type": "deploy",
            "risk_level": "low",
            "summary": "ship v1",
            "resource_type": "service",
            "resource_id": "web",
        },
    )
    assert r.status_code == 201, r.text
    row = r.json()
    assert row["status"] == "pending" and row["action_type"] == "deploy"

    lst = client.get("/api/v1/management/approvals?status=pending")
    assert lst.status_code == 200
    assert any(i["id"] == row["id"] for i in lst.json()["items"])

    d = client.post(
        f"/api/v1/management/approvals/{row['id']}/decision",
        json={"decision": "approve", "comment": "ok"},
    )
    assert d.status_code == 200, d.text
    assert d.json()["status"] == "approved"
    # 두 번째 결정 → 409
    d2 = client.post(
        f"/api/v1/management/approvals/{row['id']}/decision",
        json={"decision": "reject"},
    )
    assert d2.status_code == 409


def test_approval_create_validation():
    client, _ = _client()
    # 잘못된 action_type → 422
    r = client.post("/api/v1/management/approvals", json={"action_type": "bogus"})
    assert r.status_code == 422
    # secret-like safe_payload key → 422
    r2 = client.post(
        "/api/v1/management/approvals",
        json={
            "action_type": "deploy",
            "safe_payload": {"api_key": "sekrit"},
        },
    )
    assert r2.status_code == 422
    assert r2.json()["error"]["code"] == "secret_payload"


def test_high_risk_decision_requires_explicit_user(monkeypatch):
    """spec D — high/critical은 API 키(머신)만으로 결정 불가."""
    monkeypatch.setenv("JOBMATE_API_KEY", API_KEY)
    client, store = _client()
    agent = store.create_agent("term-me", "claude", status="active")
    assert agent is not None
    # 머신 키로 high-risk 요청 생성은 가능(lead gate는 api key를 admin 취급)
    # — agent_terminate는 resource 바인딩이 필수다(H1).
    r = client.post(
        "/api/v1/management/approvals",
        json={
            "action_type": "agent_terminate",
            "risk_level": "high",
            "resource_type": "agent",
            "resource_id": agent["id"],
            "safe_payload": {"agent_id": agent["id"]},
        },
        headers=_machine(),
    )
    assert r.status_code == 201, r.text
    aid = r.json()["id"]
    # 머신 키만으로 결정 → 403 explicit_lead_required
    d = client.post(
        f"/api/v1/management/approvals/{aid}/decision",
        json={"decision": "approve"},
        headers=_machine(),
    )
    assert d.status_code == 403
    assert d.json()["error"]["code"] == "explicit_lead_required"
    # 세션 lead로 결정 → 200 (세션이 있으면 머신 헤더 없어도 됨)
    _session(client, store, "lead@b.com", "lead")
    d2 = client.post(
        f"/api/v1/management/approvals/{aid}/decision",
        json={"decision": "approve"},
    )
    assert d2.status_code == 200, d2.text
    # member 세션은 lead gate에서 403
    client2, store2 = _client()
    _session(client2, store2, "m@b.com", "member")
    r3 = client2.post("/api/v1/management/approvals", json={"action_type": "deploy"})
    assert r3.status_code == 403


def test_low_risk_machine_decision_allowed(monkeypatch):
    """low/medium은 머신(lead급) 결정 허용 — spec은 high/critical만 제한.
    deploy는 action_type 최소 risk가 high(C1)라 머신 결정이 불가하므로
    medium floor의 run_resume으로 검증한다."""
    monkeypatch.setenv("JOBMATE_API_KEY", API_KEY)
    client, store = _client()
    run = store.create_run("dev_review", "1", {"task": "t"}, trace_id="x")
    r = client.post(
        "/api/v1/management/approvals",
        json={
            "action_type": "run_resume",
            "risk_level": "low",  # → medium으로 보정
            "run_id": run.id,
        },
        headers=_machine(),
    )
    assert r.status_code == 201
    assert r.json()["risk_level"] == "medium"
    d = client.post(
        f"/api/v1/management/approvals/{r.json()['id']}/decision",
        json={"decision": "approve"},
        headers=_machine(),
    )
    assert d.status_code == 200


def test_approval_run_resume_uses_do_approve():
    """run_resume 승인은 ctx._do_approve로 resume 경로를 탄다 —
    waiting_approval이 아닌 run은 resume 실패로 기록되고 승인은 유지."""
    client, store = _client()
    run = store.create_run("dev_review", "1", {"task": "t"}, trace_id="x")
    store.update_run(run.id, status=RunStatus.QUEUED)
    r = client.post(
        "/api/v1/management/approvals",
        json={
            "action_type": "run_resume",
            "run_id": run.id,
            "resource_type": "run",
            "resource_id": run.id,
        },
    )
    assert r.status_code == 201
    d = client.post(
        f"/api/v1/management/approvals/{r.json()['id']}/decision",
        json={"decision": "approve"},
    )
    assert d.status_code == 200
    # run이 waiting_approval이 아니므로 resume은 실패하지만 승인은 approved 유지
    assert d.json()["status"] == "approved"


# ---------------- recovery-actions ----------------


def test_recovery_actions_list_and_resolve():
    client, store = _client()
    rec, created = store.create_recovery_action(source_type="run", source_id="r1", kind="stuck_run")
    assert created and rec is not None
    lst = client.get("/api/v1/management/recovery-actions?status=active")
    assert lst.status_code == 200
    assert any(i["id"] == rec["id"] for i in lst.json()["items"])
    # 존재하지 않는 id → 404
    nf = client.post(
        "/api/v1/management/recovery-actions/nope/resolve", json={"status": "resolved"}
    )
    assert nf.status_code == 404
    ok = client.post(
        f"/api/v1/management/recovery-actions/{rec['id']}/resolve",
        json={"status": "resolved", "note": "fixed manually"},
    )
    assert ok.status_code == 200
    assert ok.json()["status"] == "resolved"
    # 이미 종결 → 409
    again = client.post(
        f"/api/v1/management/recovery-actions/{rec['id']}/resolve",
        json={"status": "cancelled"},
    )
    assert again.status_code == 409


# ---------------- watchdog/run ----------------


def test_watchdog_run_endpoint():
    client, store = _client()
    # 로컬 개발 모드(인증 미설정) → admin 취급
    r = client.post("/api/v1/management/watchdog/run")
    assert r.status_code == 200, r.text
    body = r.json()
    for key in ("scanned", "detected", "created", "retried", "escalated", "resolved"):
        assert key in body


def test_watchdog_run_requires_admin(monkeypatch):
    """admin이 아닌 세션(member) → 403."""
    monkeypatch.setenv("JOBMATE_API_KEY", API_KEY)
    client, store = _client()
    _session(client, store, "m@b.com", "member")
    r = client.post("/api/v1/management/watchdog/run")
    assert r.status_code == 403


# ---------------- BLOCK-1: DELETE /agents도 terminate 승인 필요 ----------------


def test_delete_agent_requires_terminate_approval():
    """DELETE /agents/{id}는 archived(=canonical terminated)로 전이 —
    승인된 agent_terminate approval_request 없이 삭제 불가."""
    client, store = _client()
    a = store.create_agent("deleteme", "claude", status="active")
    assert a is not None
    r = client.delete(f"/api/v1/agents/{a['id']}")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "approval_required"
    # agent는 그대로 active
    assert store.get_agent(a["id"])["status"] == "active"

    # 승인 생성 → approve → 부수효과로 terminated → DELETE 허용(이미 terminal)
    ar = client.post(
        "/api/v1/management/approvals",
        json={
            "action_type": "agent_terminate",
            "risk_level": "medium",
            "resource_type": "agent",
            "resource_id": a["id"],
            "safe_payload": {"agent_id": a["id"]},
        },
    )
    assert ar.status_code == 201
    d = client.post(
        f"/api/v1/management/approvals/{ar.json()['id']}/decision",
        json={"decision": "approve"},
    )
    assert d.status_code == 200
    assert store.get_agent(a["id"])["status"] == "terminated"
    ok = client.delete(f"/api/v1/agents/{a['id']}")
    assert ok.status_code == 200
    assert store.get_agent(a["id"])["status"] == "archived"


def test_delete_agent_emits_status_event():
    """archive도 agent_status_changed 이벤트를 남긴다 — transition과 동일 흔적."""
    client, store = _client()
    a = store.create_agent("evt", "claude", status="active")
    assert a is not None
    # 승인 후 DELETE — 이벤트 확인
    ar = client.post(
        "/api/v1/management/approvals",
        json={
            "action_type": "agent_terminate",
            "resource_type": "agent",
            "resource_id": a["id"],
            "safe_payload": {"agent_id": a["id"]},
        },
    )
    assert ar.status_code == 201
    client.post(
        f"/api/v1/management/approvals/{ar.json()['id']}/decision",
        json={"decision": "approve"},
    )
    assert client.delete(f"/api/v1/agents/{a['id']}").status_code == 200
    evs = [
        e
        for e in store.list_agent_events(agent_id=a["id"])
        if e["event_type"] == "agent_status_changed"
    ]
    assert evs, "archive must emit agent_status_changed"


# ---------------- M-2: expires_at 검증 ----------------


def test_approval_expires_at_validation():
    client, _ = _client()
    # malformed → 422
    r = client.post(
        "/api/v1/management/approvals",
        json={"action_type": "deploy", "expires_at": "garbage"},
    )
    assert r.status_code == 422
    # '9999'도 ISO가 아니므로 거부 — 사전식 비교로 영구 미만료되는 것 방지
    r2 = client.post(
        "/api/v1/management/approvals",
        json={"action_type": "deploy", "expires_at": "9999"},
    )
    assert r2.status_code == 422
    # naive datetime → UTC로 정규화돼 저장
    r3 = client.post(
        "/api/v1/management/approvals",
        json={"action_type": "deploy", "expires_at": "2030-06-01 12:00:00"},
    )
    assert r3.status_code == 201, r3.text
    assert "+00:00" in r3.json()["expires_at"]
    # 과거 시각은 허용 — 생성 즉시 expired로 읽힌다
    r4 = client.post(
        "/api/v1/management/approvals",
        json={"action_type": "deploy", "expires_at": "2020-01-01T00:00:00Z"},
    )
    assert r4.status_code == 201
    got = client.get("/api/v1/management/approvals")
    past = [i for i in got.json()["items"] if i["id"] == r4.json()["id"]]
    assert past and past[0]["status"] == "expired"


# ---------------- 보안 리뷰 2차: C1/H1/H2/M2 ----------------


def test_action_min_risk_coerces_and_blocks_machine_decision(monkeypatch):
    """C1 — agent_terminate를 risk_level=low로 생성해도 high로 보정돼
    머신 키는 결정할 수 없다(사람 전용 게이트 유지)."""
    monkeypatch.setenv("JOBMATE_API_KEY", API_KEY)
    client, store = _client()
    a = store.create_agent("c1-victim", "claude", status="active")
    assert a is not None
    r = client.post(
        "/api/v1/management/approvals",
        json={
            "action_type": "agent_terminate",
            "risk_level": "low",
            "resource_type": "agent",
            "resource_id": a["id"],
            "safe_payload": {"agent_id": a["id"]},
        },
        headers=_machine(),
    )
    assert r.status_code == 201, r.text
    assert r.json()["risk_level"] == "high"  # low 요청 → high 보정
    d = client.post(
        f"/api/v1/management/approvals/{r.json()['id']}/decision",
        json={"decision": "approve"},
        headers=_machine(),
    )
    assert d.status_code == 403
    assert d.json()["error"]["code"] == "explicit_lead_required"
    # 부수효과 미실행 — agent는 여전히 active
    assert store.get_agent(a["id"])["status"] == "active"


def test_agent_terminate_payload_resource_mismatch_rejected():
    """H1 — resource_id와 safe_payload.agent_id가 다르면 생성 거부.
    한 승인이 A에 묶이면서 B를 terminate하는 경로를 차단한다."""
    client, store = _client()
    a = store.create_agent("h1-a", "claude", status="active")
    b = store.create_agent("h1-b", "claude", status="active")
    assert a is not None and b is not None
    r = client.post(
        "/api/v1/management/approvals",
        json={
            "action_type": "agent_terminate",
            "resource_type": "agent",
            "resource_id": a["id"],
            "safe_payload": {"agent_id": b["id"]},
        },
    )
    assert r.status_code == 422
    # resource 바인딩 없는 agent_terminate도 거부
    r2 = client.post(
        "/api/v1/management/approvals",
        json={"action_type": "agent_terminate"},
    )
    assert r2.status_code == 422


def test_decision_prevalidation_keeps_pending_on_missing_target():
    """H2 — 부수효과 대상이 없는 승인은 CAS 전에 실패해 pending 유지.
    store 직접 insert로 서비스 검증을 우회한 레거시/변조 row를 시뮬레이션."""
    client, store = _client()
    row = store.create_approval_request(
        action_type="agent_terminate",
        risk_level="high",
        resource_type="agent",
        resource_id="ghost-agent",
    )
    assert row is not None
    d = client.post(
        f"/api/v1/management/approvals/{row['id']}/decision",
        json={"decision": "approve"},
    )
    assert d.status_code == 404
    fresh = store.get_approval_request(row["id"])
    assert fresh is not None and fresh["status"] == "pending"


def test_safe_payload_oversize_rejected_not_truncated():
    """M2 — 8000자 초과 safe_payload는 422. 잘린 JSON 저장 금지."""
    client, _ = _client()
    r = client.post(
        "/api/v1/management/approvals",
        json={
            "action_type": "deploy",
            "safe_payload": {"blob": "x" * 9000},
        },
    )
    assert r.status_code == 422
