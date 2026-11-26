"""management 1차 API 테스트 — /api/v1/management 엔드포인트 + 머신 인증."""

import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from jobmate.api.app import create_app
from jobmate.config import Settings
from jobmate.security import hash_token
from jobmate.store.db import Store
from tests.integration.helpers import default_outputs, scripted_llm

API_KEY = "mgmt-test-key"


def _client(api_key: str | None = None, with_llm: bool = False):
    db = str(Path(tempfile.mkdtemp()) / "t.db")
    settings = Settings(db_path=db, budget_max_tokens=100_000)
    store = Store(db)
    llm = scripted_llm(default_outputs()) if with_llm else None
    app = create_app(settings=settings, store=store, llm=llm, adapters={})
    return TestClient(app), store


def _machine_headers() -> dict[str, str]:
    return {"X-API-Key": API_KEY}


def _session_cookie(client: TestClient, store: Store, email: str = "u@b.com") -> None:
    """유효 세션 쿠키를 직접 발급 — 머신 전용 엔드포인트의 세션 거부 검증용."""
    user = store.create_user(email, password_hash="x" * 32, email_verified=True)
    assert user is not None
    token = f"sess-{email}"
    store.create_session(user["id"], hash_token(token))
    client.cookies.set("jobmate_session", token)


def _mk_agent(client: TestClient, headers: dict[str, str] | None = None, **kw) -> dict:
    body = {"name": kw.pop("name", "dev"), "adapter_type": "claude", **kw}
    r = client.post("/api/v1/management/agents", json=body, headers=headers or {})
    assert r.status_code == 201, r.text
    return r.json()


def _mk_task(
    client: TestClient, title: str = "task", headers: dict[str, str] | None = None, **kw
) -> dict:
    r = client.post("/api/v1/management/tasks", json={"title": title, **kw}, headers=headers or {})
    assert r.status_code == 201, r.text
    return r.json()


# ---- 인증 ----


def test_heartbeat_rejects_unauthenticated(monkeypatch):
    """인증 없는 machine heartbeat → 401 (미들웨어), 세션 쿠키만 → 403."""
    monkeypatch.setenv("JOBMATE_API_KEY", API_KEY)
    client, store = _client()
    agent = _mk_agent(
        client,
        headers=_machine_headers(),
        name="ext",
        kind="external",
        adapter_type="custom",
    )
    # API 키 없이 → 401
    r = client.post(f"/api/v1/management/agents/{agent['id']}/heartbeat", json={})
    assert r.status_code == 401
    # 세션 쿠키만 → 미들웨어 통과 후 machine gate 403
    _session_cookie(client, store)
    r2 = client.post(
        f"/api/v1/management/agents/{agent['id']}/heartbeat",
        json={"status": "working"},
    )
    assert r2.status_code == 403
    # 유효 API 키 → 200
    r3 = client.post(
        f"/api/v1/management/agents/{agent['id']}/heartbeat",
        json={"status": "working"},
        headers=_machine_headers(),
    )
    assert r3.status_code == 200
    assert r3.json()["status"] == "running"
    assert r3.json()["last_seen_at"]


def test_external_checkout_requires_machine_key(monkeypatch):
    monkeypatch.setenv("JOBMATE_API_KEY", API_KEY)
    client, store = _client()
    agent = _mk_agent(
        client,
        headers=_machine_headers(),
        name="ext",
        kind="external",
        adapter_type="custom",
    )
    task = _mk_task(
        client, headers=_machine_headers(), assignee_agent_id=agent["id"], status="todo"
    )
    # 세션 쿠키만 → 403
    _session_cookie(client, store)
    r = client.post(
        f"/api/v1/management/tasks/{task['id']}/checkout",
        json={"agent_id": agent["id"]},
    )
    assert r.status_code == 403
    # API 키 → 200
    r2 = client.post(
        f"/api/v1/management/tasks/{task['id']}/checkout",
        json={"agent_id": agent["id"]},
        headers=_machine_headers(),
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["checkout_agent_id"] == agent["id"]


# ---- agent CRUD/transition ----


def test_agent_crud_and_transition():
    client, store = _client()
    agent = _mk_agent(
        client,
        name="dev",
        kind="workflow",
        adapter_config={"workflow": "dev_review", "api_key": "sekrit"},
        title="Backend",
    )
    assert agent["status"] == "active"
    assert agent["kind"] == "workflow"
    # secret-like 키는 응답에서 마스킹
    assert agent["adapter_config"]["api_key"] == "REDACTED"
    assert agent["adapter_config"]["workflow"] == "dev_review"

    # 목록/단건 조회
    lst = client.get("/api/v1/management/agents")
    assert lst.status_code == 200
    assert any(a["id"] == agent["id"] for a in lst.json()["items"])
    one = client.get(f"/api/v1/management/agents/{agent['id']}")
    assert one.status_code == 200
    assert one.json()["current_task"] is None

    # PATCH — status는 transition 규칙 적용
    p = client.patch(f"/api/v1/management/agents/{agent['id']}", json={"role": "engineer"})
    assert p.status_code == 200 and p.json()["role"] == "engineer"
    # active→idle는 spec상 불가 → 409
    bad = client.patch(f"/api/v1/management/agents/{agent['id']}", json={"status": "idle"})
    assert bad.status_code == 409
    # transition 엔드포인트 — 허용 경로
    tr = client.post(
        f"/api/v1/management/agents/{agent['id']}/transition",
        json={"status": "running"},
    )
    assert tr.status_code == 200 and tr.json()["status"] == "running"
    # running→idle 허용
    tr2 = client.post(
        f"/api/v1/management/agents/{agent['id']}/transition",
        json={"status": "idle"},
    )
    assert tr2.status_code == 200
    # 존재하지 않는 agent → 404
    nf = client.post("/api/v1/management/agents/nope/transition", json={"status": "active"})
    assert nf.status_code == 404


def test_agent_create_validation():
    client, _ = _client()
    r = client.post(
        "/api/v1/management/agents",
        json={"name": "x", "adapter_type": "claude", "kind": "bogus"},
    )
    assert r.status_code == 422
    r2 = client.post(
        "/api/v1/management/agents",
        json={"name": "x", "adapter_type": "claude", "adapter_config": "nope"},
    )
    assert r2.status_code == 422


# ---- task CRUD/assign/checkout ----


def test_task_crud_assign_checkout():
    client, store = _client()
    agent = _mk_agent(client, name="dev")
    agent2 = _mk_agent(client, name="dev2")
    task = _mk_task(client, title="do thing", priority="high")
    assert task["identifier"] == "WMB-1"
    assert task["status"] == "backlog"

    # GET 목록/단건
    assert client.get("/api/v1/management/tasks").status_code == 200
    got = client.get(f"/api/v1/management/tasks/{task['id']}")
    assert got.status_code == 200
    # identifier로도 조회
    got2 = client.get("/api/v1/management/tasks/WMB-1")
    assert got2.status_code == 200

    # assign
    a = client.post(
        f"/api/v1/management/tasks/{task['id']}/assign",
        json={"agent_id": agent["id"]},
    )
    assert a.status_code == 200
    assert a.json()["assignee_agent_id"] == agent["id"]

    # checkout → in_progress 진행 + checkout_agent_id 기록
    c = client.post(
        f"/api/v1/management/tasks/{task['id']}/checkout",
        json={"agent_id": agent["id"]},
    )
    assert c.status_code == 200, c.text
    assert c.json()["status"] == "in_progress"
    assert c.json()["checkout_agent_id"] == agent["id"]

    # 다른 agent checkout → 409
    c2 = client.post(
        f"/api/v1/management/tasks/{task['id']}/checkout",
        json={"agent_id": agent2["id"]},
    )
    assert c2.status_code == 409

    # PATCH status — in_progress→done는 allowlist 허용 → 200
    ok = client.patch(f"/api/v1/management/tasks/{task['id']}", json={"status": "done"})
    assert ok.status_code == 200
    assert ok.json()["status"] == "done"


def test_task_patch_transition_rules():
    client, _ = _client()
    task = _mk_task(client, title="t")
    # backlog→done 불가 → 409
    r = client.patch(f"/api/v1/management/tasks/{task['id']}", json={"status": "done"})
    assert r.status_code == 409
    # backlog→todo→in_progress→done 경로
    assert (
        client.patch(f"/api/v1/management/tasks/{task['id']}", json={"status": "todo"}).status_code
        == 200
    )
    assert (
        client.patch(
            f"/api/v1/management/tasks/{task['id']}", json={"status": "in_progress"}
        ).status_code
        == 200
    )
    assert (
        client.patch(f"/api/v1/management/tasks/{task['id']}", json={"status": "done"}).status_code
        == 200
    )
    # terminal → 어떤 전이도 409
    r2 = client.patch(f"/api/v1/management/tasks/{task['id']}", json={"status": "todo"})
    assert r2.status_code == 409


# ---- wake ----


def test_workflow_wake_creates_run_once_and_links():
    client, store = _client(with_llm=True)
    agent = _mk_agent(
        client,
        name="dev",
        kind="workflow",
        adapter_config={"workflow": "dev_review"},
    )
    task = _mk_task(client, title="fix the bug", status="todo")
    client.post(
        f"/api/v1/management/tasks/{task['id']}/assign",
        json={"agent_id": agent["id"]},
    )
    # idempotency key 없으면 422
    r0 = client.post(
        f"/api/v1/management/agents/{agent['id']}/wake",
        json={"task_id": task["id"]},
    )
    assert r0.status_code == 422

    r1 = client.post(
        f"/api/v1/management/agents/{agent['id']}/wake",
        json={"task_id": task["id"], "idempotency_key": "wake-key-1"},
    )
    assert r1.status_code == 202, r1.text
    body = r1.json()
    assert body["action"] == "created"
    run_id = body["run_id"]
    assert run_id

    # 동일 키 재전송 — 같은 run, 신규 run 없음
    r2 = client.post(
        f"/api/v1/management/agents/{agent['id']}/wake",
        json={"task_id": task["id"], "idempotency_key": "wake-key-1"},
    )
    assert r2.status_code == 202
    assert r2.json()["run_id"] == run_id
    assert r2.json().get("replayed") is True
    assert len(store.list_runs()) == 1

    # task_runs 링크 + task 상태
    links = store.task_runs_for_task(task["id"])
    assert len(links) == 1 and links[0]["run_id"] == run_id
    t = store.get_task(task["id"])
    assert t["status"] == "in_progress"
    assert t["current_run_id"] == run_id
    run = store.get_run(run_id)
    assert run is not None and run.agent_id == agent["id"]

    # 이벤트 기록
    evs = client.get(f"/api/v1/management/events?task_id={task['id']}").json()["items"]
    types = {e["event_type"] for e in evs}
    assert "wake_requested" in types
    assert "task_assigned" in types


def test_external_wake_202_no_process():
    client, store = _client()
    agent = _mk_agent(client, name="ext", kind="external", adapter_type="custom")
    task = _mk_task(client, title="external job", status="todo")
    r = client.post(
        f"/api/v1/management/agents/{agent['id']}/wake",
        json={"task_id": task["id"], "idempotency_key": "ext-wake-1"},
        headers={"Idempotency-Key": "ext-wake-1"},  # 헤더 경로도 허용
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["action"] == "wake_recorded"
    assert body["run_id"] == ""
    assert store.list_runs() == []  # 프로세스/run 생성 없음
    # 이벤트는 기록됨
    evs = store.list_agent_events(agent_id=agent["id"])
    assert any(e["event_type"] == "wake_requested" for e in evs)


def test_wake_rejects_assigned_other_agent():
    client, _ = _client()
    a1 = _mk_agent(client, name="a1")
    a2 = _mk_agent(client, name="a2")
    task = _mk_task(client, title="t", assignee_agent_id=a1["id"], status="todo")
    r = client.post(
        f"/api/v1/management/agents/{a2['id']}/wake",
        json={"task_id": task["id"], "idempotency_key": "k"},
    )
    assert r.status_code == 409


# ---- events ----


def test_events_filters():
    client, store = _client()
    agent = _mk_agent(client, name="dev")
    task = _mk_task(client, title="t")
    client.post(
        f"/api/v1/management/tasks/{task['id']}/assign",
        json={"agent_id": agent["id"]},
    )
    r = client.get(f"/api/v1/management/events?agent_id={agent['id']}")
    assert r.status_code == 200
    items = r.json()["items"]
    assert any(e["event_type"] == "task_assigned" for e in items)
    # task 필터
    r2 = client.get(f"/api/v1/management/events?task_id={task['id']}")
    assert all(e["task_id"] == task["id"] for e in r2.json()["items"])


def test_events_requires_lead_but_tasks_member_readable(monkeypatch):
    """M2 — events는 heartbeat detail 등 임의 JSON을 포함하므로 lead 전용.
    tasks 목록/단건은 업무 항목이라 member 읽기를 유지한다(제품 결정)."""
    monkeypatch.setenv("JOBMATE_API_KEY", API_KEY)
    client, store = _client()
    _mk_agent(client, headers=_machine_headers(), name="dev")
    task = _mk_task(client, headers=_machine_headers(), title="t")

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

    # events — member 403, lead 200
    assert member.get("/api/v1/management/events").status_code == 403
    assert lead.get("/api/v1/management/events").status_code == 200
    # tasks — member도 읽기 가능(워크 아이템)
    assert member.get("/api/v1/management/tasks").status_code == 200
    assert member.get(f"/api/v1/management/tasks/{task['id']}").status_code == 200


# ---- wake 예산 방어선 (H1) ----


def test_workflow_wake_blocked_by_hard_stop_budget():
    """H1 — hard-stop 스코프 예산 초과 시 wake는 429, run 미생성."""
    from jobmate import budget_scope

    budget_scope._spend_cache.clear()
    try:
        client, store = _client()
        agent = _mk_agent(
            client,
            name="dev",
            kind="workflow",
            adapter_config={"workflow": "dev_review"},
        )
        task = _mk_task(client, title="costly", status="todo")
        # 월간 지출 생성 — claude 1M output tokens ≈ $15
        spend_run = store.create_run("dev_review", "0.1.0", {"task": "x"}, "tr-spend")
        sr = store.start_stage(spend_run.id, "llm", 1, {})
        store.finish_stage(sr.id, status="succeeded", output={}, tokens_out=1_000_000)
        budget_scope.upsert_policy(store, "global", "", amount_usd=1.0, hard_stop_enabled=True)
        before = len(store.list_runs())

        r = client.post(
            f"/api/v1/management/agents/{agent['id']}/wake",
            json={"task_id": task["id"], "idempotency_key": "budget-k1"},
        )
        assert r.status_code == 429, r.text
        assert r.json()["error"]["code"] == "budget_exceeded"
        assert len(store.list_runs()) == before  # run 미생성
        # task 상태도 그대로
        assert store.get_task(task["id"])["status"] == "todo"
    finally:
        budget_scope._spend_cache.clear()


def test_workflow_wake_blocked_by_agent_monthly_budget():
    """H1 — agents.monthly_budget_usd 암묵 상한도 wake를 차단한다."""
    from jobmate import budget_scope

    budget_scope._spend_cache.clear()
    try:
        client, store = _client()
        agent = _mk_agent(
            client,
            name="dev",
            kind="workflow",
            adapter_config={"workflow": "dev_review"},
        )
        # agent 스코프 지출 생성 — run.agent_id 태그 필요
        spend_run = store.create_run(
            "dev_review", "0.1.0", {"task": "x"}, "tr-spend2", agent_id=agent["id"]
        )
        sr = store.start_stage(spend_run.id, "llm", 1, {})
        store.finish_stage(sr.id, status="succeeded", output={}, tokens_out=1_000_000)
        store.update_agent(agent["id"], monthly_budget_usd=1.0)
        task = _mk_task(client, title="costly2", status="todo")

        r = client.post(
            f"/api/v1/management/agents/{agent['id']}/wake",
            json={"task_id": task["id"], "idempotency_key": "budget-k2"},
        )
        assert r.status_code == 429, r.text
        assert r.json()["error"]["code"] == "budget_exceeded"
    finally:
        budget_scope._spend_cache.clear()


def test_external_wake_not_budget_blocked():
    """H1 — external wake는 run을 만들지 않으므로 예산 차단 대상이 아니다."""
    client, store = _client()
    agent = _mk_agent(client, name="ext", kind="external", adapter_type="custom")
    task = _mk_task(client, title="ext job", status="todo")
    r = client.post(
        f"/api/v1/management/agents/{agent['id']}/wake",
        json={"task_id": task["id"], "idempotency_key": "ext-budget-1"},
    )
    assert r.status_code == 202, r.text


# ---- wake 멱등 replay task 검증 (H3) ----


def test_wake_replay_same_key_different_task_conflicts():
    """H3 — 동일 idempotency key + 다른 task_id는 replay가 아니라 409."""
    client, store = _client()
    agent = _mk_agent(client, name="ext", kind="external", adapter_type="custom")
    t1 = _mk_task(client, title="job1", status="todo")
    t2 = _mk_task(client, title="job2", status="todo")

    r1 = client.post(
        f"/api/v1/management/agents/{agent['id']}/wake",
        json={"task_id": t1["id"], "idempotency_key": "shared-key"},
    )
    assert r1.status_code == 202
    # 동일 키 + 동일 task → replay
    r2 = client.post(
        f"/api/v1/management/agents/{agent['id']}/wake",
        json={"task_id": t1["id"], "idempotency_key": "shared-key"},
    )
    assert r2.status_code == 202 and r2.json().get("replayed") is True
    # 동일 키 + 다른 task → 409 idempotency_conflict
    r3 = client.post(
        f"/api/v1/management/agents/{agent['id']}/wake",
        json={"task_id": t2["id"], "idempotency_key": "shared-key"},
    )
    assert r3.status_code == 409, r3.text
    assert r3.json()["error"]["code"] == "idempotency_conflict"


def test_wake_replay_identifier_task_id_matches():
    """H3 — task_id를 identifier('WMB-1')로내도 저장된 uuid와 비교가
    일치해 replay가 정상 동작한다."""
    client, store = _client()
    agent = _mk_agent(client, name="ext2", kind="external", adapter_type="custom")
    t1 = _mk_task(client, title="job", status="todo")
    r1 = client.post(
        f"/api/v1/management/agents/{agent['id']}/wake",
        json={"task_id": t1["identifier"], "idempotency_key": "ident-key"},
    )
    assert r1.status_code == 202
    r2 = client.post(
        f"/api/v1/management/agents/{agent['id']}/wake",
        json={"task_id": t1["id"], "idempotency_key": "ident-key"},
    )
    assert r2.status_code == 202 and r2.json().get("replayed") is True


# ---- checkout assignee + release (M5) ----


def test_checkout_rejects_assignee_mismatch():
    """M5 — 다른 agent에게 배정된 task는 checkout 불가(409)."""
    client, _ = _client()
    a1 = _mk_agent(client, name="a1")
    a2 = _mk_agent(client, name="a2", kind="external", adapter_type="custom")
    task = _mk_task(client, title="assigned", assignee_agent_id=a1["id"], status="todo")
    r = client.post(
        f"/api/v1/management/tasks/{task['id']}/checkout",
        json={"agent_id": a2["id"]},
    )
    assert r.status_code == 409, r.text


def test_release_task_clears_checkout():
    """M5 — release는 checkout을 해제하고 재checkout을 가능하게 한다."""
    client, store = _client()
    agent = _mk_agent(client, name="ext", kind="external", adapter_type="custom")
    task = _mk_task(client, title="job", status="todo")
    c = client.post(
        f"/api/v1/management/tasks/{task['id']}/checkout",
        json={"agent_id": agent["id"]},
    )
    assert c.status_code == 200
    assert c.json()["checkout_agent_id"] == agent["id"]

    # checkout 없는 task release → 409
    t2 = _mk_task(client, title="free", status="todo")
    r0 = client.post(f"/api/v1/management/tasks/{t2['id']}/release", json={})
    assert r0.status_code == 409

    rel = client.post(f"/api/v1/management/tasks/{task['id']}/release", json={})
    assert rel.status_code == 200, rel.text
    assert rel.json()["checkout_agent_id"] is None
    assert rel.json()["checkout_at"] is None
    # 재checkout 가능
    c2 = client.post(
        f"/api/v1/management/tasks/{task['id']}/checkout",
        json={"agent_id": agent["id"]},
    )
    assert c2.status_code == 200


def test_release_task_machine_owner_only(monkeypatch):
    """M5 — 비-lead(머신 키)는 checkout 소유 agent_id 일치 시에만 release."""
    monkeypatch.setenv("JOBMATE_API_KEY", API_KEY)
    client, store = _client()
    agent = _mk_agent(
        client, headers=_machine_headers(), name="ext", kind="external", adapter_type="custom"
    )
    other = _mk_agent(
        client,
        headers=_machine_headers(),
        name="ext2",
        kind="external",
        adapter_type="custom",
    )
    task = _mk_task(client, headers=_machine_headers(), title="job", status="todo")
    client.post(
        f"/api/v1/management/tasks/{task['id']}/checkout",
        json={"agent_id": agent["id"]},
        headers=_machine_headers(),
    )
    # 세션 쿠키만 → 403
    _session_cookie(client, store)
    r = client.post(f"/api/v1/management/tasks/{task['id']}/release", json={})
    assert r.status_code == 403
    # 머신 키 + 다른 agent_id → 403
    r2 = client.post(
        f"/api/v1/management/tasks/{task['id']}/release",
        json={"agent_id": other["id"]},
        headers=_machine_headers(),
    )
    assert r2.status_code == 403
    # 머신 키 + 소유 agent_id → 200
    r3 = client.post(
        f"/api/v1/management/tasks/{task['id']}/release",
        json={"agent_id": agent["id"]},
        headers=_machine_headers(),
    )
    assert r3.status_code == 200, r3.text
    assert r3.json()["checkout_agent_id"] is None


def test_release_task_machine_key_requires_owner_even_admin_equivalent(monkeypatch):
    """H2 회귀 — 머신 API 키는 _lead_user에서 admin으로 취급되지만,
    release의 holder 검사는 여전히 적용된다: agent_id 미제시/불일치 → 403,
    소유 agent_id 일치 → 200. 세션 lead는 agent_id 없이 해제 가능."""
    monkeypatch.setenv("JOBMATE_API_KEY", API_KEY)
    client, store = _client()
    agent = _mk_agent(
        client, headers=_machine_headers(), name="ext", kind="external", adapter_type="custom"
    )
    other = _mk_agent(
        client,
        headers=_machine_headers(),
        name="ext2",
        kind="external",
        adapter_type="custom",
    )
    task = _mk_task(client, headers=_machine_headers(), title="job", status="todo")
    client.post(
        f"/api/v1/management/tasks/{task['id']}/checkout",
        json={"agent_id": agent["id"]},
        headers=_machine_headers(),
    )
    # 머신 키 + agent_id 없음 → 403 (구버그: admin 취급으로 holder 검사 생략)
    r0 = client.post(
        f"/api/v1/management/tasks/{task['id']}/release", json={}, headers=_machine_headers()
    )
    assert r0.status_code == 403, r0.text
    # 머신 키 + 다른 agent → 403
    r1 = client.post(
        f"/api/v1/management/tasks/{task['id']}/release",
        json={"agent_id": other["id"]},
        headers=_machine_headers(),
    )
    assert r1.status_code == 403
    # 세션 lead(명시적) → agent_id 없이 200
    user = store.create_user("lead@b.com", password_hash="x" * 32, email_verified=True)
    assert user is not None
    store.set_user_role(user["id"], "lead")
    token = f"sess-lead-release-{user['id'][:8]}"
    store.create_session(user["id"], hash_token(token))
    lead_client = TestClient(client.app)
    lead_client.cookies.set("jobmate_session", token)
    r2 = lead_client.post(f"/api/v1/management/tasks/{task['id']}/release", json={})
    assert r2.status_code == 200, r2.text
    assert r2.json()["checkout_agent_id"] is None


def test_wake_replay_skips_budget_checks(monkeypatch):
    """H2(예산) 회귀 — 기록된 wake 결과의 replay는 예산 소진 후에도
    429가 아니라 202 replayed를 반환한다."""
    from jobmate import budget_scope

    budget_scope._spend_cache.clear()
    try:
        client, store = _client()
        agent = _mk_agent(
            client,
            name="dev",
            kind="workflow",
            adapter_config={"workflow": "dev_review"},
        )
        task = _mk_task(client, title="costly retry", status="todo")
        r1 = client.post(
            f"/api/v1/management/agents/{agent['id']}/wake",
            json={"task_id": task["id"], "idempotency_key": "retry-key"},
        )
        assert r1.status_code == 202, r1.text
        run_id = r1.json()["run_id"]

        # 예산 소진 상태 만들기 — hard stop global 정책 + 대량 지출
        spend_run = store.create_run("dev_review", "0.1.0", {"task": "x"}, "tr-spend3")
        sr = store.start_stage(spend_run.id, "llm", 1, {})
        store.finish_stage(sr.id, status="succeeded", output={}, tokens_out=1_000_000)
        budget_scope.upsert_policy(store, "global", "", amount_usd=1.0, hard_stop_enabled=True)

        # 동일 키 재시도 — replay이므로 429가 아니라 202
        r2 = client.post(
            f"/api/v1/management/agents/{agent['id']}/wake",
            json={"task_id": task["id"], "idempotency_key": "retry-key"},
        )
        assert r2.status_code == 202, r2.text
        assert r2.json()["run_id"] == run_id
        assert r2.json().get("replayed") is True
        # 신규 run 없음
        assert len(store.list_runs()) == 2  # wake run + spend run
    finally:
        budget_scope._spend_cache.clear()


def test_mgmt_list_agents_rejects_bogus_filters():
    """L — status/kind bogus 값은 빈 목록이 아니라 422."""
    client, _ = _client()
    r1 = client.get("/api/v1/management/agents?status=bogus")
    assert r1.status_code == 422
    r2 = client.get("/api/v1/management/agents?kind=bogus")
    assert r2.status_code == 422
    # 유효 값은 그대로 동작
    r3 = client.get("/api/v1/management/agents?status=active&kind=workflow")
    assert r3.status_code == 200


def test_patch_task_explicit_null_returns_422():
    """L — title/status/priority 명시 null은 422로 정규화."""
    client, _ = _client()
    task = _mk_task(client, title="t")
    for f in ("title", "status", "priority"):
        r = client.patch(f"/api/v1/management/tasks/{task['id']}", json={f: None})
        assert r.status_code == 422, (f, r.text)
    # nullable 필드 null은 비우기로 동작
    agent = _mk_agent(client, name="a1")
    client.post(f"/api/v1/management/tasks/{task['id']}/assign", json={"agent_id": agent["id"]})
    r2 = client.patch(f"/api/v1/management/tasks/{task['id']}", json={"assignee_agent_id": None})
    assert r2.status_code == 200
    assert r2.json()["assignee_agent_id"] is None


# ---- management PATCH 원자성 (M4) ----


def test_mgmt_patch_agent_validates_transition_before_writes():
    """M4 — status 전이 불가 시 비-status 필드도 커밋되지 않는다."""
    client, store = _client()
    agent = _mk_agent(client, name="dev")  # status=active
    # active→idle는 allowlist 위반 — role 변경도 롤백돼야 한다
    r = client.patch(
        f"/api/v1/management/agents/{agent['id']}",
        json={"role": "should-not-stick", "status": "idle"},
    )
    assert r.status_code == 409, r.text
    got = store.get_agent(agent["id"])
    assert got["role"] != "should-not-stick"
    assert got["status"] == "active"
    # 필드만 → 정상
    ok = client.patch(f"/api/v1/management/agents/{agent['id']}", json={"role": "eng"})
    assert ok.status_code == 200


# ---- heartbeat detail 마스킹 (L3) ----


def test_heartbeat_detail_secrets_redacted(monkeypatch):
    """L3 — heartbeat detail의 secret-like 키는 저장 전 마스킹된다."""
    monkeypatch.setenv("JOBMATE_API_KEY", API_KEY)
    client, store = _client()
    agent = _mk_agent(
        client, headers=_machine_headers(), name="ext", kind="external", adapter_type="custom"
    )
    r = client.post(
        f"/api/v1/management/agents/{agent['id']}/heartbeat",
        json={
            "status": "working",
            "detail": {"api_key": "sekrit", "nested": {"token": "abc"}, "note": "ok"},
        },
        headers=_machine_headers(),
    )
    assert r.status_code == 200
    evs = [
        e
        for e in store.list_agent_events(agent_id=agent["id"])
        if e["event_type"] == "heartbeat_received"
    ]
    assert evs
    detail = evs[0]["detail"]
    assert detail["api_key"] == "REDACTED"
    assert detail["nested"]["token"] == "REDACTED"  # noqa: S105 — dict 키 조회 비교
    assert detail["note"] == "ok"
