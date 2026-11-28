"""조직 데이터 격리 테스트 — spec 003 US3 (FR-013/FR-016).

서로 다른 조직의 사용자가 runs 등 org-scoped 리소스를 상호
조회/수정/삭제할 수 없음을 검증한다. ctx='' 인 경로(API 키·스토어 직접
호출)는 의도적으로 무필터다.
"""

import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from jobmate.api.app import create_app
from jobmate.config import Settings
from jobmate.store.db import Store
from tests.integration.helpers import default_outputs, scripted_llm

_PW = "Str0ng#Passw0rd!"


def _client(monkeypatch=None):
    monkeypatch.setenv("JOBMATE_API_KEY", "test-api-key")
    for v in (
        "JOBMATE_ALLOWED_EMAILS",
        "VERCEL",
        "JOBMATE_MODE",
        "JOBMATE_ENCRYPTION_KEY",
        "JOBMATE_PUBLIC_HOSTS",
        "JOBMATE_ADMIN_EMAILS",
        "JOBMATE_LEAD_EMAILS",
    ):
        monkeypatch.delenv(v, raising=False)
    db = str(Path(tempfile.mkdtemp()) / "t.db")
    settings = Settings(db_path=db, budget_max_tokens=100_000)
    store = Store(db)
    app = create_app(settings=settings, store=store, llm=scripted_llm(default_outputs()))
    return TestClient(app), store


def _signup(client, store, email):
    r = client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": _PW, "agree_terms": True},
    )
    assert r.status_code == 201, r.text
    # 테스트는 메일 인증을 거치지 않으므로 직접 verified 처리
    with store.tx() as c:
        c.execute("UPDATE users SET email_verified=1 WHERE email=?", (email,))


def _login(client, email):
    r = client.post("/api/v1/auth/login", json={"email": email, "password": _PW})
    assert r.status_code == 200, r.text


def _create_run(client, task="do something"):
    return client.post("/api/v1/runs", json={"task": task})


def _wait_run(store, run_id, timeout=5.0):
    """백그라운드 run 스레드가 store.close() 이후에도 살아있지 않도록
    종결 상태까지 폴링한다."""
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        run = store.get_run(run_id)
        if run is not None and str(run.status) in {
            "done",
            "failed",
            "cancelled",
            "waiting_approval",
        }:
            return
        time.sleep(0.05)


def _org_of(store, email):
    uid = store.get_user_by_email(email)["id"]
    orgs = store.list_user_orgs(uid)
    assert orgs, f"{email} has no org"
    return orgs[0]["id"]


def test_run_isolated_between_personal_orgs(monkeypatch):
    """FR-013 — 조직 A의 run이 조직 B 목록/상세에 노출되지 않는다."""
    client, store = _client(monkeypatch)
    _signup(client, store, "a@x.com")
    _signup(client, store, "b@x.com")

    _login(client, "a@x.com")
    r = _create_run(client, "org A secret task")
    assert r.status_code == 202, r.text
    run_a = r.json()["id"]
    _wait_run(store, run_a)

    _login(client, "b@x.com")
    rb = _create_run(client, "org B task")
    assert rb.status_code == 202
    _wait_run(store, rb.json()["id"])

    # B 목록에는 A의 run이 없다
    runs_b = client.get("/api/v1/runs").json()
    ids_b = {r["id"] for r in (runs_b if isinstance(runs_b, list) else runs_b["items"])}
    assert run_a not in ids_b

    # B가 A의 run id를 직접 찌르면 404
    r = client.get(f"/api/v1/runs/{run_a}")
    assert r.status_code == 404

    # A는 자기 run을 본다
    _login(client, "a@x.com")
    runs_a = client.get("/api/v1/runs").json()
    ids_a = {r["id"] for r in (runs_a if isinstance(runs_a, list) else runs_a["items"])}
    assert run_a in ids_a
    assert client.get(f"/api/v1/runs/{run_a}").status_code == 200
    store.close()


def test_run_org_id_stamped_with_creator_org(monkeypatch):
    """생성된 run의 org_id가 요청 조직과 일치한다."""
    client, store = _client(monkeypatch)
    _signup(client, store, "a@x.com")
    _signup(client, store, "b@x.com")
    _login(client, "a@x.com")
    run_id = _create_run(client).json()["id"]
    _wait_run(store, run_id)
    row = store._conn.execute("SELECT org_id FROM runs WHERE id=?", (run_id,)).fetchone()
    assert row["org_id"] == _org_of(store, "a@x.com")
    assert row["org_id"] != _org_of(store, "b@x.com")
    store.close()


def test_cross_org_run_write_blocked(monkeypatch):
    """조직 B 세션이 조직 A run을 approve/수정할 수 없다(404)."""
    client, store = _client(monkeypatch)
    _signup(client, store, "a@x.com")
    _signup(client, store, "b@x.com")
    _login(client, "a@x.com")
    run_a = _create_run(client).json()["id"]
    _wait_run(store, run_a)
    _login(client, "b@x.com")
    r = client.post(f"/api/v1/runs/{run_a}/approve", json={"decision": "approve"})
    assert r.status_code in (404, 403), r.text
    store.close()


def test_api_key_acts_in_owner_default_org(monkeypatch):
    """FR-016 — DB API 키는 소유자 기본 조직 컨텍스트로 동작한다."""
    client, store = _client(monkeypatch)
    _signup(client, store, "a@x.com")
    uid = store.get_user_by_email("a@x.com")["id"]
    # 회원이 스코프드 키 발급 (run:read/write)
    from jobmate.security import new_api_key

    raw, prefix, khash = new_api_key()
    store.create_api_key(uid, "k1", khash, prefix, ["runs:read", "runs:write"])
    assert raw
    # A 조직 run 생성 (세션)
    _login(client, "a@x.com")
    run_a = _create_run(client).json()["id"]
    _wait_run(store, run_a)
    # 두 번째 사용자 조직의 run을 스토어로 직접 생성(org_id=B)
    _signup(client, store, "b@x.com")
    org_b = _org_of(store, "b@x.com")
    store._conn.execute(
        "INSERT INTO runs(id,workflow_key,workflow_version,input,status,trace_id,"
        "created_at,updated_at,org_id) VALUES(?,?,?,?,?,?,?,?,?)",
        ("runB01", "dev_review", "1", "{}", "done", "t", "2026-01-01", "2026-01-01", org_b),
    )
    store._conn.commit()

    # A 소유 키로 조회 → A 조직 run만 보여야 함
    r = client.get("/api/v1/runs", headers={"Authorization": f"Bearer {raw}"})
    assert r.status_code == 200, r.text
    body = r.json()
    ids = {x["id"] for x in (body if isinstance(body, list) else body["items"])}
    assert run_a in ids
    assert "runB01" not in ids
    # B 조직 run 직접 조회 → 404
    r = client.get("/api/v1/runs/runB01", headers={"Authorization": f"Bearer {raw}"})
    assert r.status_code == 404
    store.close()
