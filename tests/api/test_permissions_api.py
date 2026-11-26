"""permissions API 테스트 — spec 3차 D/F.

GET/PUT /api/v1/management/agents/{id}/permissions + effective 판정 +
agent-actor 실행 경로(wake/assign/checkout/run create)의 서버 측 거부.
"""

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jobmate.api.app import create_app
from jobmate.config import Settings
from jobmate.security import hash_token
from jobmate.store.db import Store

API_KEY = "perm-test-key"


def _client(api_key: str | None = None):
    db = str(Path(tempfile.mkdtemp()) / "t.db")
    settings = Settings(db_path=db, budget_max_tokens=100_000)
    store = Store(db)
    app = create_app(settings=settings, store=store, llm=None, adapters={})
    return TestClient(app), store


def _machine() -> dict[str, str]:
    return {"X-API-Key": API_KEY}


def _mk_agent(client: TestClient, headers: dict[str, str] | None = None, **kw) -> dict:
    body = {"name": kw.pop("name", "dev"), "adapter_type": "claude", **kw}
    r = client.post("/api/v1/management/agents", json=body, headers=headers or {})
    assert r.status_code == 201, r.text
    return r.json()


def _mk_task(client: TestClient, headers: dict[str, str] | None = None, **kw) -> dict:
    r = client.post("/api/v1/management/tasks", json={"title": "t", **kw}, headers=headers or {})
    assert r.status_code == 201, r.text
    return r.json()


def _sessions(client: TestClient, store: Store) -> tuple[TestClient, TestClient]:
    """(member 클라이언트, lead 클라이언트) — JOBMATE_API_KEY 설정 하에서
    세션 쿠키 인증만으로 member+/lead+를 구분한다."""
    member = TestClient(client.app)
    lead = TestClient(client.app)
    um = store.create_user("m@b.com", password_hash="x" * 32, email_verified=True)
    ul = store.create_user("l@b.com", password_hash="x" * 32, email_verified=True)
    assert um is not None and ul is not None
    store.set_user_role(ul["id"], "lead")
    tm = f"sess-m-{um['id']}"
    tl = f"sess-l-{ul['id']}"
    store.create_session(um["id"], hash_token(tm))
    store.create_session(ul["id"], hash_token(tl))
    member.cookies.set("jobmate_session", tm)
    lead.cookies.set("jobmate_session", tl)
    return member, lead


def _put_rules(client: TestClient, agent_id: str, rules: list[dict], headers=None) -> object:
    return client.put(
        f"/api/v1/management/agents/{agent_id}/permissions",
        json={"permissions": rules},
        headers=headers or {},
    )


# ---------- GET/PUT 기본 ----------


def test_get_permissions_empty_and_member_read(monkeypatch):
    monkeypatch.setenv("JOBMATE_API_KEY", API_KEY)
    client, store = _client()
    agent = _mk_agent(client, headers=_machine(), name="dev")
    member, _lead = _sessions(client, store)
    r = member.get(f"/api/v1/management/agents/{agent['id']}/permissions")
    assert r.status_code == 200
    body = r.json()
    assert body["agent_id"] == agent["id"]
    assert body["permissions"] == []


def test_get_permissions_requires_auth(monkeypatch):
    monkeypatch.setenv("JOBMATE_API_KEY", API_KEY)
    client, _store = _client()
    agent = _mk_agent(client, headers=_machine(), name="dev")
    r = client.get(f"/api/v1/management/agents/{agent['id']}/permissions")
    assert r.status_code == 401


def test_put_permissions_lead_and_machine_ok_member_denied(monkeypatch):
    monkeypatch.setenv("JOBMATE_API_KEY", API_KEY)
    client, store = _client()
    agent = _mk_agent(client, headers=_machine(), name="dev")
    member, lead = _sessions(client, store)
    rules = [{"capability": "task:read", "effect": "allow", "scope_type": "global"}]
    # member → 403
    r = _put_rules(member, agent["id"], rules)
    assert r.status_code == 403
    # lead 세션 → 200
    r2 = _put_rules(lead, agent["id"], rules)
    assert r2.status_code == 200, r2.text
    assert r2.json()["permissions"][0]["capability"] == "task:read"
    # 머신 API 키 → 200 (lead 상당)
    r3 = _put_rules(client, agent["id"], rules, headers=_machine())
    assert r3.status_code == 200


def test_put_permissions_replace_semantics(monkeypatch):
    monkeypatch.setenv("JOBMATE_API_KEY", API_KEY)
    client, _store = _client()
    agent = _mk_agent(client, headers=_machine(), name="dev")
    h = _machine()
    _put_rules(
        client,
        agent["id"],
        [
            {"capability": "task:read", "effect": "allow"},
            {"capability": "agent:wake", "effect": "deny"},
        ],
        headers=h,
    )
    # 두 번째 PUT은 전체 교체 — task:read는 사라진다.
    r = _put_rules(
        client,
        agent["id"],
        [{"capability": "run:execute", "effect": "deny"}],
        headers=h,
    )
    assert r.status_code == 200
    caps = {p["capability"] for p in r.json()["permissions"]}
    assert caps == {"run:execute"}


# ---------- PUT 검증 ----------


@pytest.mark.parametrize(
    "rule",
    [
        {"capability": "bogus:cap", "effect": "allow"},
        {"capability": "task:read", "effect": "maybe"},
        {"capability": "task:read", "effect": "allow", "scope_type": "org"},
        {"capability": "task:read", "effect": "allow", "scope_type": "project"},
        {"capability": "task:read", "effect": "allow", "scope_type": "task", "scope_id": "nope"},
        {"capability": "task:read", "effect": "allow", "scope_type": "project", "scope_id": "nope"},
    ],
)
def test_put_permissions_validation_errors(monkeypatch, rule):
    monkeypatch.setenv("JOBMATE_API_KEY", API_KEY)
    client, _store = _client()
    agent = _mk_agent(client, headers=_machine(), name="dev")
    r = _put_rules(client, agent["id"], [rule], headers=_machine())
    assert r.status_code == 422, (rule, r.text)


def test_put_permissions_duplicate_rejected(monkeypatch):
    monkeypatch.setenv("JOBMATE_API_KEY", API_KEY)
    client, _store = _client()
    agent = _mk_agent(client, headers=_machine(), name="dev")
    rule = {"capability": "task:read", "effect": "allow", "scope_type": "global"}
    r = _put_rules(client, agent["id"], [rule, dict(rule)], headers=_machine())
    assert r.status_code == 422


def test_put_permissions_agent_not_found(monkeypatch):
    monkeypatch.setenv("JOBMATE_API_KEY", API_KEY)
    client, _store = _client()
    r = _put_rules(client, "no-such-agent", [], headers=_machine())
    assert r.status_code == 404


def test_put_permissions_scoped_ids_validated_and_normalized(monkeypatch):
    """project/task scope_id는 실존해야 하고 canonical id로 저장된다."""
    monkeypatch.setenv("JOBMATE_API_KEY", API_KEY)
    client, _store = _client()
    agent = _mk_agent(client, headers=_machine(), name="dev")
    h = _machine()
    # project 생성 — goals/projects 라우트 사용
    pr = client.post("/api/v1/projects", json={"name": "proj-x"}, headers=h)
    assert pr.status_code in (200, 201), pr.text
    pid = pr.json()["id"]
    task = _mk_task(client, headers=h, project_id=pid)
    r = _put_rules(
        client,
        agent["id"],
        [
            {"capability": "task:read", "effect": "deny", "scope_type": "project", "scope_id": pid},
            {
                "capability": "task:update",
                "effect": "allow",
                "scope_type": "task",
                "scope_id": task["identifier"],  # identifier도 canonical uuid로 정규화
            },
        ],
        headers=h,
    )
    assert r.status_code == 200, r.text
    saved = {p["capability"]: p for p in r.json()["permissions"]}
    assert saved["task:update"]["scope_id"] == task["id"]


# ---------- effective ----------


def test_effective_defaults_and_deny(monkeypatch):
    monkeypatch.setenv("JOBMATE_API_KEY", API_KEY)
    client, _store = _client()
    agent = _mk_agent(client, headers=_machine(), name="dev")
    h = _machine()
    base = f"/api/v1/management/agents/{agent['id']}/permissions/effective"
    # 규칙 없음 — 일반 capability는 allow, 위험 capability는 deny
    assert (
        client.get(base, params={"capability": "task:read"}, headers=h).json()["decision"]
        == "allow"
    )
    r = client.get(base, params={"capability": "external:send"}, headers=h)
    assert r.json()["decision"] == "deny"
    # deny 규칙 설정 후
    _put_rules(client, agent["id"], [{"capability": "task:read", "effect": "deny"}], headers=h)
    assert (
        client.get(base, params={"capability": "task:read"}, headers=h).json()["decision"] == "deny"
    )
    # 잘못된 capability → 422
    assert client.get(base, params={"capability": "bogus"}, headers=h).status_code == 422


def test_effective_needs_approval(monkeypatch):
    monkeypatch.setenv("JOBMATE_API_KEY", API_KEY)
    client, _store = _client()
    agent = _mk_agent(client, headers=_machine(), name="dev")
    h = _machine()
    _put_rules(
        client,
        agent["id"],
        [{"capability": "deploy:execute", "effect": "allow"}],
        headers=h,
    )
    base = f"/api/v1/management/agents/{agent['id']}/permissions/effective"
    body = client.get(base, params={"capability": "deploy:execute"}, headers=h).json()
    assert body["decision"] == "needs_approval"
    assert body["approval_action"] == "deploy"


# ---------- 서버 측 강제 (spec "서버가 반드시 다시 검증") ----------


def test_wake_denied_returns_403(monkeypatch):
    monkeypatch.setenv("JOBMATE_API_KEY", API_KEY)
    client, _store = _client()
    h = _machine()
    agent = _mk_agent(client, headers=h, name="w1", kind="external", adapter_type="custom")
    _put_rules(client, agent["id"], [{"capability": "agent:wake", "effect": "deny"}], headers=h)
    r = client.post(
        f"/api/v1/management/agents/{agent['id']}/wake",
        json={"idempotency_key": "wk-1"},
        headers=h,
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "permission_denied"


def test_run_create_denied_by_run_execute(monkeypatch):
    monkeypatch.setenv("JOBMATE_API_KEY", API_KEY)
    client, _store = _client()
    h = _machine()
    agent = _mk_agent(client, headers=h, name="r1")
    _put_rules(client, agent["id"], [{"capability": "run:execute", "effect": "deny"}], headers=h)
    r = client.post(
        "/api/v1/runs",
        json={"workflow": "dev_review", "task": "do it", "agent_id": agent["id"]},
        headers=h,
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "permission_denied"
    # agent_id 없는 run은 agent permission 비적용 — 생성 경로는 기존 게이트만
    r2 = client.post("/api/v1/runs", json={"workflow": "dev_review", "task": "do it"}, headers=h)
    assert r2.status_code == 202


def test_task_assign_denied_returns_403(monkeypatch):
    monkeypatch.setenv("JOBMATE_API_KEY", API_KEY)
    client, _store = _client()
    h = _machine()
    agent = _mk_agent(client, headers=h, name="t1")
    task = _mk_task(client, headers=h)
    _put_rules(client, agent["id"], [{"capability": "task:assign", "effect": "deny"}], headers=h)
    r = client.post(
        f"/api/v1/management/tasks/{task['id']}/assign",
        json={"agent_id": agent["id"]},
        headers=h,
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "permission_denied"


def test_checkout_denied_returns_403(monkeypatch):
    monkeypatch.setenv("JOBMATE_API_KEY", API_KEY)
    client, _store = _client()
    h = _machine()
    agent = _mk_agent(client, headers=h, name="c1", kind="external", adapter_type="custom")
    task = _mk_task(client, headers=h)
    _put_rules(
        client,
        agent["id"],
        [
            {
                "capability": "task:update",
                "effect": "deny",
                "scope_type": "task",
                "scope_id": task["id"],
            }
        ],
        headers=h,
    )
    r = client.post(
        f"/api/v1/management/tasks/{task['id']}/checkout",
        json={"agent_id": agent["id"]},
        headers=h,
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "permission_denied"


def test_allowed_paths_unaffected(monkeypatch):
    """deny 규칙이 없는 agent의 wake/assign/checkout/run은 기존과 동일."""
    monkeypatch.setenv("JOBMATE_API_KEY", API_KEY)
    client, _store = _client()
    h = _machine()
    agent = _mk_agent(client, headers=h, name="ok1", kind="external", adapter_type="custom")
    task = _mk_task(client, headers=h)
    wr = client.post(
        f"/api/v1/management/agents/{agent['id']}/wake",
        json={"idempotency_key": "wk-ok", "task_id": task["id"]},
        headers=h,
    )
    assert wr.status_code == 202, wr.text
    ar = client.post(
        f"/api/v1/management/tasks/{task['id']}/assign",
        json={"agent_id": agent["id"]},
        headers=h,
    )
    assert ar.status_code == 200, ar.text
    cr = client.post(
        f"/api/v1/management/tasks/{task['id']}/checkout",
        json={"agent_id": agent["id"]},
        headers=h,
    )
    assert cr.status_code == 200, cr.text
