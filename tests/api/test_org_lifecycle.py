"""spec 003 US5 — 조직 생명주기: suspended/deleted 접근 차단, 삭제 차단
조건, purge 잡, 플랫폼 admin suspend/restore (FR-028/031/033).
"""

import tempfile
from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from jobmate.api import jobs
from jobmate.api.app import create_app
from jobmate.config import Settings
from jobmate.security import hash_password, hash_token
from jobmate.store import orgctx
from jobmate.store.db import Store
from jobmate.store.util import _iso, _now
from tests.integration.helpers import default_outputs, scripted_llm

_PW = "Str0ng#Passw0rd!"


def _env(monkeypatch):
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


def _client(monkeypatch):
    _env(monkeypatch)
    db = str(Path(tempfile.mkdtemp()) / "t.db")
    settings = Settings(db_path=db, budget_max_tokens=100_000)
    store = Store(db)
    llm = scripted_llm(default_outputs())
    app = create_app(settings=settings, store=store, llm=llm)
    return TestClient(app), store, settings, llm


def _mkuser(store, email, role=""):
    user = store.create_user(email, hash_password(_PW), "U", "", True)
    uid = user["id"] if isinstance(user, dict) else user
    if role:
        store.set_user_role(uid, role)
    return uid


def _login(client, email):
    return client.post("/api/v1/auth/login", json={"email": email, "password": _PW})


def _sess(store, app, user_id, tag):
    token = f"sess-{tag}-{user_id}"
    store.create_session(user_id, hash_token(token))
    c = TestClient(app)
    c.cookies.set("jobmate_session", token)
    return c


def _team_org(client, store, owner_email="own@b.com", member_email="mem@b.com"):
    """팀 조직 + owner/member 세션 클라이언트 쌍. 반환: (oid, owner_c, member_c, ids)."""
    base = TestClient(client.app)
    ow = _mkuser(store, owner_email)
    mm = _mkuser(store, member_email)
    assert _login(base, owner_email).status_code == 200
    r = base.post("/api/v1/orgs", json={"name": "Team", "slug": f"team-{ow[:6]}"})
    assert r.status_code == 201, r.text
    oid = r.json()["org"]["id"]
    owner_c = TestClient(client.app)
    owner_c.cookies.set("jobmate_session", base.cookies.get("jobmate_session", ""))
    assert owner_c.post(f"/api/v1/orgs/{oid}/switch").status_code == 200
    store.add_org_member(oid, mm, "member")
    member_c = _sess(store, client.app, mm, "m")
    assert member_c.post(f"/api/v1/orgs/{oid}/switch").status_code == 200
    return oid, owner_c, member_c, ow, mm


def _wait_run(store, run_id, timeout=5.0):
    """백그라운드 run 스레드가 store.close() 이후에도 살아있지 않도록
    종결 상태까지 폴링한다(test_org_isolation과 동일 패턴)."""
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        run = store.get_run(run_id)
        if run is not None and str(run.status) in {
            "completed",
            "failed",
            "cancelled",
            "rejected",
            "waiting_approval",
            "waiting_review",
            "needs_human",
            "budget_exhausted",
        }:
            return
        time.sleep(0.05)


def _platform_admin(store, app, email="admin@b.com"):
    aid = _mkuser(store, email, role="admin")
    return _sess(store, app, aid, "adm")


# ---------- suspend / restore ----------


def test_suspend_requires_platform_admin(monkeypatch):
    client, store, _s, _l = _client(monkeypatch)
    oid, owner, member, _ow, _mm = _team_org(client, store)
    # 조직 owner/member는 suspend 불가 — 플랫폼 admin만
    assert owner.post(f"/api/v1/orgs/{oid}/suspend").status_code == 403
    assert member.post(f"/api/v1/orgs/{oid}/suspend").status_code == 403
    adm = _platform_admin(store, client.app)
    r = adm.post(f"/api/v1/orgs/{oid}/suspend")
    assert r.status_code == 200 and r.json()["status"] == "suspended"
    assert store.get_organization(oid)["status"] == "suspended"
    store.close()


def test_suspend_write_matrix(monkeypatch):
    """FR-031 — suspended 조직: 멤버 전면 403, owner 읽기 전용."""
    client, store, _s, _l = _client(monkeypatch)
    oid, owner, member, _ow, _mm = _team_org(client, store)
    adm = _platform_admin(store, client.app)
    assert adm.post(f"/api/v1/orgs/{oid}/suspend").status_code == 200
    # 멤버 — 읽기도 쓰기도 403 org_suspended (orgs/* 경로 제외)
    r = member.get("/api/v1/management/tasks")
    assert r.status_code == 403 and r.json()["error"]["code"] == "org_suspended"
    r = member.post("/api/v1/management/tasks", json={"title": "x"})
    assert r.status_code == 403
    # owner — GET은 허용, 쓰기는 403
    r = owner.get("/api/v1/management/tasks")
    assert r.status_code == 200, r.text
    r = owner.post("/api/v1/management/tasks", json={"title": "x"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "org_suspended"
    # orgs/* 경로는 자체 규칙 — owner는 조직 조회 가능, 멤버는 403
    assert owner.get(f"/api/v1/orgs/{oid}").status_code == 200
    assert member.get(f"/api/v1/orgs/{oid}").status_code == 403
    # run 생성도 차단
    r = owner.post("/api/v1/runs", json={"workflow": "dev_review", "task": "x"})
    assert r.status_code == 403
    store.close()


def test_restore_reactivates(monkeypatch):
    client, store, _s, _l = _client(monkeypatch)
    oid, owner, _member, _ow, _mm = _team_org(client, store)
    adm = _platform_admin(store, client.app)
    adm.post(f"/api/v1/orgs/{oid}/suspend")
    # restore는 suspended 상태에서만
    r = adm.post(f"/api/v1/orgs/{oid}/restore")
    assert r.status_code == 200 and r.json()["status"] == "active"
    assert owner.post("/api/v1/management/tasks", json={"title": "ok"}).status_code == 201
    # active 조직 restore는 409
    assert adm.post(f"/api/v1/orgs/{oid}/restore").status_code == 409
    store.close()


def test_suspend_audit_events(monkeypatch):
    client, store, _s, _l = _client(monkeypatch)
    oid, _o, _m, _ow, _mm = _team_org(client, store)
    adm = _platform_admin(store, client.app)
    adm.post(f"/api/v1/orgs/{oid}/suspend")
    adm.post(f"/api/v1/orgs/{oid}/restore")
    with orgctx.org_scope(oid):
        actions = {e["action"] for e in store.list_audit(limit=200)}
    assert "org_suspended" in actions and "org_restored" in actions
    store.close()


# ---------- 삭제 차단 / 접근 차단 ----------


def test_delete_blocked_by_active_run(monkeypatch):
    """FR-028 — 활성 run이 있으면 409 org_has_active_runs + 건수."""
    client, store, _s, _l = _client(monkeypatch)
    base = TestClient(client.app)
    _mkuser(store, "del@b.com")
    assert _login(base, "del@b.com").status_code == 200
    oid = base.post("/api/v1/orgs", json={"name": "D", "slug": "del-org"}).json()["org"]["id"]
    owner = TestClient(client.app)
    owner.cookies.set("jobmate_session", base.cookies.get("jobmate_session", ""))
    assert owner.post(f"/api/v1/orgs/{oid}/switch").status_code == 200
    r = owner.post("/api/v1/runs", json={"workflow": "dev_review", "task": "x"})
    assert r.status_code == 202, r.text
    run_id = r.json().get("id") or r.json().get("run_id")
    r = owner.delete(f"/api/v1/orgs/{oid}")
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "org_has_active_runs"
    if run_id:
        _wait_run(store, run_id)
    store.close()


def test_deleted_org_blocks_member_access(monkeypatch):
    """FR-028 — 삭제 즉시 멤버 접근 차단 + orgs 조회 404."""
    client, store, _s, _l = _client(monkeypatch)
    base = TestClient(client.app)
    _mkuser(store, "del2@b.com")
    mm = _mkuser(store, "del2m@b.com")
    assert _login(base, "del2@b.com").status_code == 200
    oid = base.post("/api/v1/orgs", json={"name": "D2", "slug": "del2-org"}).json()["org"]["id"]
    owner = TestClient(client.app)
    owner.cookies.set("jobmate_session", base.cookies.get("jobmate_session", ""))
    assert owner.post(f"/api/v1/orgs/{oid}/switch").status_code == 200
    # 멤버 제거 후 삭제(멤버 1명 조건) — member는 탈퇴시켜 둔다
    store.add_org_member(oid, mm, "member")
    member = _sess(store, client.app, mm, "dm")
    assert member.post(f"/api/v1/orgs/{oid}/switch").status_code == 200
    tid = owner.post("/api/v1/management/tasks", json={"title": "t"}).json()["id"]
    # 멤버를 제거하면 org는 owner 1명 — 삭제 가능
    r = owner.delete(f"/api/v1/orgs/{oid}/members/{mm}")
    assert r.status_code == 200, r.text
    r = owner.delete(f"/api/v1/orgs/{oid}")
    assert r.status_code == 200, r.text
    # 삭제된 조직 — orgs 조회 404
    assert owner.get(f"/api/v1/orgs/{oid}").status_code == 404
    # 멤버(제거된)의 삭제 조직 데이터 접근 — ctx가 기본 조직으로 복귀해 404
    assert member.get(f"/api/v1/management/tasks/{tid}").status_code == 404
    store.close()


# ---------- purge 잡 ----------


def test_purge_deleted_organizations(monkeypatch):
    """FR-028 — 삭제 30일 경과 조직의 org 스코프 행 삭제, audit 보존."""
    client, store, _s, _l = _client(monkeypatch)
    base = TestClient(client.app)
    _mkuser(store, "pg@b.com")
    assert _login(base, "pg@b.com").status_code == 200
    oid = base.post("/api/v1/orgs", json={"name": "P", "slug": "pg-org"}).json()["org"]["id"]
    owner = TestClient(client.app)
    owner.cookies.set("jobmate_session", base.cookies.get("jobmate_session", ""))
    assert owner.post(f"/api/v1/orgs/{oid}/switch").status_code == 200
    owner.post("/api/v1/management/tasks", json={"title": "pt"})
    owner.post("/api/v1/management/projects", json={"name": "pp"})
    assert owner.delete(f"/api/v1/orgs/{oid}").status_code == 200
    # retention 기간 미경과 — purge 대상 아님
    counts = store.purge_deleted_organizations()
    assert counts["purged_organizations"] == 0
    assert store.get_organization(oid) is not None
    # 31일 경과로 시뮬레이션 — updated_at을 과거로 되돌린다
    old = _iso(_now() - timedelta(days=31))
    store._conn.execute("UPDATE organizations SET updated_at=? WHERE id=?", (old, oid))
    store._conn.commit()
    counts = store.purge_deleted_organizations()
    assert counts["purged_organizations"] == 1
    assert store.get_organization(oid) is None
    # org 스코프 행 삭제 확인
    with orgctx.org_scope(oid):
        assert store.list_tasks(limit=10) == []
        assert store.list_projects() == []
    # audit_events는 보존
    with orgctx.org_scope(oid):
        audits = store.list_audit(limit=100)
    assert any(e["action"] == "org_deleted" for e in audits)
    store.close()


# ---------- 스케줄/루틴 발사 차단 ----------


def test_due_schedule_skipped_when_org_suspended(monkeypatch):
    """FR-031 — suspended 조직의 due 스케줄은 run을 만들지 않는다."""
    client, store, settings, llm = _client(monkeypatch)
    oid, owner, _m, _ow, _mm = _team_org(client, store)
    r = owner.post(
        "/api/v1/schedules",
        json={"workflow": "dev_review", "input": {}, "interval_minutes": 60},
    )
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    # next_run_at을 과거로 — due 상태로 만든다
    store._conn.execute(
        "UPDATE workflow_schedules SET next_run_at=? WHERE id=?",
        (_iso(_now() - timedelta(minutes=5)), sid),
    )
    store._conn.commit()
    # suspended 전 — 발사 확인
    jobs._process_due_schedules(store, settings, llm, {}, defer=[])
    with orgctx.org_scope(oid):
        assert len(store.list_runs(limit=10)) == 1
    # 정지 후 — due여도 발사 안 함(claim도 안 하므로 next_run_at 유지)
    store.set_organization_status(oid, "suspended")
    store._conn.execute(
        "UPDATE workflow_schedules SET next_run_at=? WHERE id=?",
        (_iso(_now() - timedelta(minutes=5)), sid),
    )
    store._conn.commit()
    jobs._process_due_schedules(store, settings, llm, {}, defer=[])
    with orgctx.org_scope(oid):
        assert len(store.list_runs(limit=10)) == 1
    store.close()
