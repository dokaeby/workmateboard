"""커스텀 워크플로 조직 격리 회귀 테스트 — spec 003 US3.

배경 버그: _sync_workflow_registry가 조직 필터된 custom_workflows를 전역
WORKFLOWS dict에 섞어 넣어, (name, org_id) PK를 공유하는 두 조직의 동명
spec이 서로를 덮어쓰고(hijack) 한 조직의 동기화가 다른 조직 항목을
evict했다. 수정 후 spec은 조회 시점에 조직 컨텍스트로 해석된다.

검증:
- 두 조직이 같은 name의 커스텀 워크플로를 소유해도 spec/version이 독립
- POST /runs·/api/v1/workflows·build_runner가 각자 조직의 spec을 사용
- 조직 A의 삭제/갱신이 조직 B 항목을 evict하지 않음
- ctx='' 쓰기가 다른 조직의 동명 행을 덮어쓰지 않음
"""

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jobmate.api.app import create_app
from jobmate.config import Settings
from jobmate.store import orgctx
from jobmate.store.db import Store
from jobmate.workflows.registry_builder import build_runner
from jobmate.workflows.resolver import resolve_workflow_spec
from tests.integration.helpers import default_outputs, scripted_llm

_PW = "Str0ng#Passw0rd!"


def _spec(key: str, version: str, stage_key: str) -> dict:
    return {
        "key": key,
        "version": version,
        "description": f"{key} {version}",
        "entry": stage_key,
        "stages": [{"key": stage_key, "kind": "human", "description": "d"}],
    }


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.setenv("JOBMATE_API_KEY", "test-api-key")
    # 두 사용자 모두 admin — 커스텀 워크플로 API(lead 전용)를 양쪽에서 사용.
    monkeypatch.setenv("JOBMATE_ADMIN_EMAILS", "a@x.com,b@x.com")
    for v in (
        "JOBMATE_ALLOWED_EMAILS",
        "VERCEL",
        "JOBMATE_MODE",
        "JOBMATE_ENCRYPTION_KEY",
        "JOBMATE_PUBLIC_HOSTS",
        "JOBMATE_LEAD_EMAILS",
    ):
        monkeypatch.delenv(v, raising=False)
    db = str(Path(tempfile.mkdtemp()) / "t.db")
    settings = Settings(db_path=db, budget_max_tokens=100_000)
    store = Store(db)
    llm = scripted_llm(default_outputs())
    app = create_app(settings=settings, store=store, llm=llm, adapters={})
    client = TestClient(app)
    yield client, store, settings, llm
    store.close()


def _signup(client, store, email):
    r = client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": _PW, "agree_terms": True},
    )
    assert r.status_code == 201, r.text
    with store.tx() as c:
        c.execute("UPDATE users SET email_verified=1 WHERE email=?", (email,))


def _login(client, email):
    r = client.post("/api/v1/auth/login", json={"email": email, "password": _PW})
    assert r.status_code == 200, r.text


def _org_of(store, email):
    uid = store.get_user_by_email(email)["id"]
    orgs = store.list_user_orgs(uid)
    assert orgs, f"{email} has no org"
    return str(orgs[0]["id"])


def _wait_run(store, run_id, timeout=5.0):
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


def test_same_name_custom_workflow_isolated_per_org(env):
    client, store, settings, llm = env
    _signup(client, store, "a@x.com")
    _signup(client, store, "b@x.com")
    org_a = _org_of(store, "a@x.com")
    org_b = _org_of(store, "b@x.com")
    assert org_a != org_b

    spec_a = _spec("sharedflow", "1.0.0", "step_a")
    spec_b = _spec("sharedflow", "2.0.0", "step_b")

    _login(client, "a@x.com")
    r = client.post("/api/v1/workflows/custom", json={"name": "sharedflow", "spec": spec_a})
    assert r.status_code == 201, r.text
    _login(client, "b@x.com")
    r = client.post("/api/v1/workflows/custom", json={"name": "sharedflow", "spec": spec_b})
    assert r.status_code == 201, r.text

    # 조직별 목록/카탈로그는 각자 spec만 본다
    _login(client, "a@x.com")
    items_a = client.get("/api/v1/workflows/custom").json()["items"]
    assert [i["name"] for i in items_a] == ["sharedflow"]
    assert items_a[0]["spec"]["version"] == "1.0.0"
    wf_a = {w["key"]: w for w in client.get("/api/v1/workflows").json()}
    assert wf_a["sharedflow"]["version"] == "1.0.0"

    _login(client, "b@x.com")
    items_b = client.get("/api/v1/workflows/custom").json()["items"]
    assert items_b[0]["spec"]["version"] == "2.0.0"
    wf_b = {w["key"]: w for w in client.get("/api/v1/workflows").json()}
    assert wf_b["sharedflow"]["version"] == "2.0.0"

    # resolver — org_scope별로 각자 spec (hijack 불가)
    with orgctx.org_scope(org_a):
        ra = resolve_workflow_spec(store, "sharedflow")
    with orgctx.org_scope(org_b):
        rb = resolve_workflow_spec(store, "sharedflow")
    assert ra is not None and ra.version == "1.0.0"
    assert rb is not None and rb.version == "2.0.0"

    # build_runner — 실행 시점에도 run/org 컨텍스트의 spec으로 조립된다
    with orgctx.org_scope(org_a):
        runner_a = build_runner("sharedflow", store=store, llm=llm, settings=settings, adapters={})
    with orgctx.org_scope(org_b):
        runner_b = build_runner("sharedflow", store=store, llm=llm, settings=settings, adapters={})
    assert runner_a.spec.version == "1.0.0"
    assert runner_a.spec.stages[0].key == "step_a"
    assert runner_b.spec.version == "2.0.0"
    assert runner_b.spec.stages[0].key == "step_b"

    # POST /runs — 각 조직이 자기 version의 run을 만든다
    _login(client, "a@x.com")
    r = client.post("/api/v1/runs", json={"workflow": "sharedflow", "task": "go"})
    assert r.status_code == 202, r.text
    run_a = store.get_run(r.json()["id"])
    assert run_a is not None and run_a.workflow_version == "1.0.0"
    _wait_run(store, run_a.id)

    _login(client, "b@x.com")
    r = client.post("/api/v1/runs", json={"workflow": "sharedflow", "task": "go"})
    assert r.status_code == 202, r.text
    run_b = store.get_run(r.json()["id"])
    assert run_b is not None and run_b.workflow_version == "2.0.0"
    _wait_run(store, run_b.id)


def test_org_delete_does_not_evict_other_org(env):
    client, store, _settings, _llm = env
    _signup(client, store, "a@x.com")
    _signup(client, store, "b@x.com")
    org_b = _org_of(store, "b@x.com")

    for email, ver in (("a@x.com", "1.0.0"), ("b@x.com", "2.0.0")):
        _login(client, email)
        r = client.post(
            "/api/v1/workflows/custom",
            json={"name": "sharedflow", "spec": _spec("sharedflow", ver, "s")},
        )
        assert r.status_code == 201, r.text

    # 조직 A가 삭제 — 조직 B의 spec은 그대로 해석/실행 가능
    _login(client, "a@x.com")
    assert client.delete("/api/v1/workflows/custom/sharedflow").status_code == 200
    r_gone = client.post("/api/v1/runs", json={"workflow": "sharedflow", "task": "x"})
    assert r_gone.status_code == 404

    with orgctx.org_scope(org_b):
        rb = resolve_workflow_spec(store, "sharedflow")
    assert rb is not None and rb.version == "2.0.0"

    _login(client, "b@x.com")
    r = client.post("/api/v1/runs", json={"workflow": "sharedflow", "task": "go"})
    assert r.status_code == 202, r.text
    run_b = store.get_run(r.json()["id"])
    assert run_b is not None and run_b.workflow_version == "2.0.0"
    _wait_run(store, run_b.id)


def test_unscoped_write_does_not_overwrite_other_org(env):
    """ctx='' 쓰기는 기본 조직(defaultorg01)에 귀속 — 다른 조직 동명 행 불침범."""
    client, store, _s, _l = env
    _signup(client, store, "a@x.com")
    _signup(client, store, "b@x.com")
    org_b = _org_of(store, "b@x.com")
    default_org = str(store.list_organizations()[0]["id"])

    # 조직 B 소유 동명 워크플로
    spec_b = _spec("sharedflow", "2.0.0", "step_b")
    with orgctx.org_scope(org_b):
        store.save_custom_workflow("sharedflow", spec_b)

    # ctx='' 쓰기 — 기본 조직(defaultorg01)에 귀속되고 org_b는 무사.
    spec_sys = _spec("sharedflow", "9.9.9", "step_sys")
    store.save_custom_workflow("sharedflow", spec_sys)

    with orgctx.org_scope(org_b):
        rb = resolve_workflow_spec(store, "sharedflow")
    assert rb is not None and rb.version == "2.0.0"

    with orgctx.org_scope(default_org):
        rd = resolve_workflow_spec(store, "sharedflow")
    assert rd is not None and rd.version == "9.9.9"

    # (name, org_id) PK로 두 조직 행이 공존 — org_b spec 원문 유지
    rows = store._conn.execute(
        "SELECT org_id, spec FROM custom_workflows WHERE name='sharedflow'"
    ).fetchall()
    by_org = {str(r["org_id"]): r["spec"] for r in rows}
    assert set(by_org) == {default_org, org_b}
    assert '"version": "2.0.0"' in by_org[org_b] or '"version":"2.0.0"' in by_org[org_b]
