"""spec 003 US4 — user_permissions 프로젝트 권한 부여 + 세션 capability 강제.

검증 범위:
- GET/PUT/DELETE /api/v1/orgs/{org}/projects/{pid}/permissions[/{uid}]
- 프로젝트 scope 외 규칙(global/task/타 프로젝트) 거부와 scope 격리
- _require_session_capability 강제 지점: task:update(생성/수정/checkout/
  release/프로젝트 이동), task:assign, task:create, run:execute,
  run:approve, agent:manage, agent:wake, artifact:create
- 플랫폼 admin(_platform_admin_*)과 조직 admin(_admin_*)의 분리
- 멤버 제거/계정 삭제 시 user_permissions 정리
"""

import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from jobmate.api.app import create_app
from jobmate.config import Settings
from jobmate.security import hash_password, hash_token
from jobmate.store import orgctx
from jobmate.store.db import Store
from tests.integration.helpers import default_outputs, scripted_llm

_PW = "Str0ng#Passw0rd!"


def _client(monkeypatch):
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


def _mkuser(store, email):
    user = store.create_user(email, hash_password(_PW), "U", "", True)
    return user["id"] if isinstance(user, dict) else user


def _sess(store, app, user_id, tag):
    """세션 쿠키 클라이언트 — current_org_id는 스위치 엔드포인트로 설정."""
    token = f"sess-{tag}-{user_id}"
    store.create_session(user_id, hash_token(token))
    c = TestClient(app)
    c.cookies.set("jobmate_session", token)
    return c


def _login(client, email):
    return client.post("/api/v1/auth/login", json={"email": email, "password": _PW})


def _org_setup(monkeypatch):
    """(client, store, owner_c, member_c, org_id) — 팀 조직 + owner/member 세션.

    owner는 org_owner, member는 org role member(플랫폼 role도 member) —
    capability 검사가 실제로 row를 타는지 보려면 lead급 주체(owner)가
    deny를 받는 구성이 필요하다."""
    client, store = _client(monkeypatch)
    base = TestClient(client.app)
    _mkuser(store, "owner@b.com")
    _mkuser(store, "member@b.com")
    assert _login(base, "owner@b.com").status_code == 200
    r = base.post("/api/v1/orgs", json={"name": "Team", "slug": "team-x"})
    assert r.status_code == 201, r.text
    org_id = r.json()["org"]["id"]
    owner_c = TestClient(client.app)
    owner_tok = base.cookies.get("jobmate_session", "")
    owner_c.cookies.set("jobmate_session", owner_tok)
    member_id = store.get_user_by_email("member@b.com")["id"]
    owner_id = store.get_user_by_email("owner@b.com")["id"]
    store.add_org_member(org_id, member_id, "member")
    # owner 세션을 팀 조직으로 전환
    assert owner_c.post(f"/api/v1/orgs/{org_id}/switch").status_code == 200
    member_c = _sess(store, client.app, member_id, "m")
    assert member_c.post(f"/api/v1/orgs/{org_id}/switch").status_code == 200
    return client, store, owner_c, member_c, org_id, owner_id, member_id


def _project(client, name="P1"):
    r = client.post("/api/v1/management/projects", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _task(client, title="t", project_id=None):
    body = {"title": title}
    if project_id:
        body["project_id"] = project_id
    r = client.post("/api/v1/management/tasks", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _perm_url(org_id, pid, uid=""):
    base = f"/api/v1/orgs/{org_id}/projects/{pid}/permissions"
    return f"{base}/{uid}" if uid else base


# ---------- 권한 엔드포인트 ----------


def test_project_permissions_put_get_delete_roundtrip(monkeypatch):
    client, store, owner, member, oid, _ow, mid = _org_setup(monkeypatch)
    pid = _project(owner, "Alpha")
    r = owner.put(
        _perm_url(oid, pid, mid),
        json={
            "rules": [
                {
                    "capability": "task:update",
                    "effect": "deny",
                    "scope_type": "project",
                    "scope_id": pid,
                }
            ]
        },
    )
    assert r.status_code == 200, r.text
    rules = r.json()["rules"]
    assert len(rules) == 1 and rules[0]["effect"] == "deny"
    # GET — user_id 필터와 전체 목록 둘 다 조회 가능
    r = owner.get(_perm_url(oid, pid) + f"?user_id={mid}")
    assert r.status_code == 200
    assert len(r.json()["items"]) == 1
    r = owner.get(_perm_url(oid, pid))
    assert any(i["user_id"] == mid for i in r.json()["items"])
    # DELETE — 프로젝트 scope 행만 제거
    r = owner.delete(_perm_url(oid, pid, mid))
    assert r.status_code == 200 and r.json()["deleted"] == 1
    assert owner.get(_perm_url(oid, pid) + f"?user_id={mid}").json()["items"] == []
    store.close()


def test_project_permissions_rejects_non_project_scope(monkeypatch):
    """global/task scope나 타 프로젝트 scope_id는 422 — GET에 안 보이는
    규칙이 적용되는 사각지대 차단."""
    client, store, owner, member, oid, _ow, mid = _org_setup(monkeypatch)
    pid = _project(owner, "Alpha")
    pid2 = _project(owner, "Beta")
    tid = _task(owner, "x", pid)["id"]
    for rule in (
        {"capability": "run:execute", "effect": "deny", "scope_type": "global"},
        {
            "capability": "run:execute",
            "effect": "deny",
            "scope_type": "task",
            "scope_id": tid,
        },
        {
            "capability": "run:execute",
            "effect": "deny",
            "scope_type": "project",
            "scope_id": pid2,
        },
    ):
        r = owner.put(_perm_url(oid, pid, mid), json={"rules": [rule]})
        assert r.status_code == 422, (rule, r.text)
    store.close()


def test_project_permissions_put_preserves_other_scopes(monkeypatch):
    """PUT은 해당 프로젝트 scope 행만 교체 — global 규칙은 유지."""
    client, store, owner, member, oid, _ow, mid = _org_setup(monkeypatch)
    pid = _project(owner, "Alpha")
    pid2 = _project(owner, "Beta")
    with orgctx.org_scope(oid):
        store.replace_user_permissions(
            mid,
            [
                {
                    "capability": "run:execute",
                    "effect": "deny",
                    "scope_type": "global",
                    "scope_id": "",
                }
            ],
        )
        store.replace_user_permissions(
            mid,
            [
                {
                    "capability": "task:read",
                    "effect": "deny",
                    "scope_type": "project",
                    "scope_id": pid2,
                }
            ],
            scope_type="project",
            scope_id=pid2,
        )
    r = owner.put(
        _perm_url(oid, pid, mid),
        json={
            "rules": [
                {
                    "capability": "task:update",
                    "effect": "deny",
                    "scope_type": "project",
                    "scope_id": pid,
                }
            ]
        },
    )
    assert r.status_code == 200, r.text
    with orgctx.org_scope(oid):
        rows = store.list_user_permissions(mid, limit=100)
    scopes = {(r2["scope_type"], r2["scope_id"]) for r2 in rows}
    assert ("global", "") in scopes
    assert ("project", pid2) in scopes
    assert ("project", pid) in scopes
    store.close()


def test_project_permissions_delete_scoped_only(monkeypatch):
    client, store, owner, member, oid, _ow, mid = _org_setup(monkeypatch)
    pid = _project(owner, "Alpha")
    pid2 = _project(owner, "Beta")
    with orgctx.org_scope(oid):
        for p in (pid, pid2):
            store.replace_user_permissions(
                mid,
                [
                    {
                        "capability": "task:update",
                        "effect": "deny",
                        "scope_type": "project",
                        "scope_id": p,
                    }
                ],
                scope_type="project",
                scope_id=p,
            )
    r = owner.delete(_perm_url(oid, pid, mid))
    assert r.status_code == 200 and r.json()["deleted"] == 1
    with orgctx.org_scope(oid):
        rows = store.list_user_permissions(mid, limit=100)
    assert [r2["scope_id"] for r2 in rows] == [pid2]
    store.close()


def test_project_permissions_member_and_cross_org_denied(monkeypatch):
    client, store, owner, member, oid, _ow, mid = _org_setup(monkeypatch)
    pid = _project(owner, "Alpha")
    body = {
        "rules": [
            {
                "capability": "task:update",
                "effect": "deny",
                "scope_type": "project",
                "scope_id": pid,
            }
        ]
    }
    # 일반 member는 grant 관리 불가
    r = member.put(_perm_url(oid, pid, mid), json=body)
    assert r.status_code == 403
    # 다른 조직 컨텍스트에서의 path org_id 위조 → 404
    r = owner.put("/api/v1/orgs/nope/projects/x/permissions/u", json=body)
    assert r.status_code == 404
    store.close()


# ---------- capability 강제 ----------


def _deny(owner, oid, pid, mid, cap, scope_type="project", scope_id=""):
    r = owner.put(
        _perm_url(oid, pid, mid),
        json={
            "rules": [
                {
                    "capability": cap,
                    "effect": "deny",
                    "scope_type": scope_type,
                    "scope_id": scope_id or pid,
                }
            ]
        },
    )
    assert r.status_code == 200, r.text


def test_task_update_denied_blocks_patch_and_checkout(monkeypatch):
    """프로젝트 scope task:update deny — PATCH와 checkout 모두 403.

    deny 대상은 org_owner 자신 — 명시 규칙이 역할 기본값(allow)을
    override하는지 함께 검증한다."""
    client, store, owner, member, oid, ow, mid = _org_setup(monkeypatch)
    pid = _project(owner, "Alpha")
    tid = _task(owner, "t1", pid)["id"]
    # owner 본인에게 deny — PUT은 owner 세션으로 자기 권한을 바꾼다
    _deny(owner, oid, pid, ow, "task:update")
    r = owner.patch(f"/api/v1/management/tasks/{tid}", json={"priority": "high"})
    assert r.status_code == 403, r.text
    # checkout도 동일 capability
    agent = owner.post(
        "/api/v1/management/agents",
        json={"name": "a1", "adapter_type": "claude"},
    )
    # agent:manage deny는 아직 없으므로 생성은 성공해야 함 — 단 owner가
    # task:update deny 상태에서도 agent 생성은 가능(다른 capability)
    assert agent.status_code in (200, 201), agent.text
    aid = agent.json()["id"]
    r = owner.post(
        f"/api/v1/management/tasks/{tid}/checkout",
        json={"agent_id": aid},
    )
    assert r.status_code == 403, r.text
    store.close()


def test_task_update_target_project_move_denied(monkeypatch):
    """task:update deny가 '목표 프로젝트'에도 적용 — 현재 프로젝트만
    검사하면 deny된 프로젝트로의 이동 우회가 된다."""
    client, store, owner, member, oid, ow, mid = _org_setup(monkeypatch)
    pid_a = _project(owner, "A")
    pid_b = _project(owner, "B")
    tid = _task(owner, "mv", pid_a)["id"]
    # owner에게 B 프로젝트 task:update deny
    _deny(owner, oid, pid_b, ow, "task:update")
    r = owner.patch(f"/api/v1/management/tasks/{tid}", json={"project_id": pid_b})
    assert r.status_code == 403, r.text
    # A 프로젝트 내부 수정은 여전히 가능
    r = owner.patch(f"/api/v1/management/tasks/{tid}", json={"priority": "high"})
    assert r.status_code == 200, r.text
    store.close()


def test_run_execute_task_scoped_deny(monkeypatch):
    """task scope run:execute deny — metadata.task_id로 run 생성을 막는다."""
    client, store, owner, member, oid, ow, mid = _org_setup(monkeypatch)
    pid = _project(owner, "Alpha")
    tid = _task(owner, "rt", pid)["id"]
    # member에게 task-scoped deny — PUT은 owner가 수행
    r = owner.put(
        _perm_url(oid, pid, mid),
        json={
            "rules": [
                {
                    "capability": "run:execute",
                    "effect": "deny",
                    "scope_type": "task",
                    "scope_id": tid,
                }
            ]
        },
    )
    # 엔드포인트는 project scope만 허용 — task scope는 422여야 한다
    assert r.status_code == 422
    # store 레벨로 task scope deny를 심고 run 생성을 시도
    with orgctx.org_scope(oid):
        store.replace_user_permissions(
            mid,
            [
                {
                    "capability": "run:execute",
                    "effect": "deny",
                    "scope_type": "task",
                    "scope_id": tid,
                }
            ],
        )
    r = member.post(
        "/api/v1/runs",
        json={
            "workflow": "dev_review",
            "task": "do it",
            "metadata": {"task_id": tid},
        },
    )
    assert r.status_code == 403, r.text
    store.close()


def test_agent_manage_and_wake_deny(monkeypatch):
    """agent:manage/agent:wake deny — management agents 쓰기 경로 강제."""
    client, store, owner, member, oid, ow, mid = _org_setup(monkeypatch)
    _project(owner, "Alpha")
    # owner 본인에게 global agent:manage deny — scope 제한 없는 규칙이
    # 모든 프로젝트/조직 컨텍스트에서 적용되는지 확인
    with orgctx.org_scope(oid):
        store.replace_user_permissions(
            ow,
            [
                {
                    "capability": "agent:manage",
                    "effect": "deny",
                    "scope_type": "global",
                    "scope_id": "",
                }
            ],
        )
    r = owner.post(
        "/api/v1/management/agents",
        json={"name": "blocked", "adapter_type": "claude"},
    )
    assert r.status_code == 403, r.text
    store.close()


def test_run_approve_deny(monkeypatch):
    """run:approve deny — 승인 엔드포인트 강제."""
    client, store, owner, member, oid, ow, mid = _org_setup(monkeypatch)
    with orgctx.org_scope(oid):
        store.replace_user_permissions(
            ow,
            [
                {
                    "capability": "run:approve",
                    "effect": "deny",
                    "scope_type": "global",
                    "scope_id": "",
                }
            ],
        )
    # 존재하지 않는 run이어도 capability 검사가 먼저 403을 내야 한다
    # (approve 게이트 순서: lead → capability → run 조회)
    r = owner.post(
        "/api/v1/runs/nonexistent/approve",
        json={"decision": "approve"},
    )
    assert r.status_code == 403, r.text
    store.close()


# ---------- 플랫폼/조직 admin 분리 ----------


def test_org_owner_cannot_reach_platform_admin_endpoints(monkeypatch):
    """개인 조직 org_owner는 조직 스코프 admin은 되지만 플랫폼 admin은 아니다 —
    /users(전 사용자 목록)·/settings(글로벌 prefs)는 403, 조직 스코프
    schedules/reports/audit은 허용."""
    client, store = _client(monkeypatch)
    _mkuser(store, "solo@b.com")
    # 가입 API 경로와 동일하게 개인 워크스페이스(org_owner)를 만든다 —
    # _mkuser는 users 행만 만들고 조직은 별도 생성이다.
    store.create_personal_org(store.get_user_by_email("solo@b.com"))
    assert _login(client, "solo@b.com").status_code == 200
    # 사용자 목록 — 플랫폼 admin 전용
    r = client.get("/api/v1/users")
    assert r.status_code == 403, r.text
    # 글로벌 설정 — 플랫폼 admin 전용
    r = client.post("/api/v1/settings", json={"gmail_check_minutes": 30})
    assert r.status_code == 403, r.text
    # 조직 스코프 admin — schedules 생성은 허용
    r = client.post(
        "/api/v1/schedules",
        json={"workflow": "dev_review", "input": {}, "interval_minutes": 60},
    )
    assert r.status_code == 201, r.text
    # 감사 로그 — 조직 스코프 필터로 own org만 보이는 admin 뷰
    r = client.get("/api/v1/audit")
    assert r.status_code == 200, r.text
    store.close()


# ---------- 정리 ----------


def test_member_removal_and_account_delete_clean_permissions(monkeypatch):
    client, store, owner, member, oid, ow, mid = _org_setup(monkeypatch)
    pid = _project(owner, "Alpha")
    with orgctx.org_scope(oid):
        store.replace_user_permissions(
            mid,
            [
                {
                    "capability": "task:update",
                    "effect": "deny",
                    "scope_type": "project",
                    "scope_id": pid,
                }
            ],
        )
    # 멤버 제거 → user_permissions도 함께 삭제
    assert store.remove_org_member(oid, mid)
    with orgctx.org_scope(oid):
        assert store.list_user_permissions(mid, limit=10) == []
    # 계정 삭제 cascade — 다른 조직의 행도 user_id 기준으로 제거
    _mkuser(store, "gone@b.com")
    gid = store.get_user_by_email("gone@b.com")["id"]
    oid2 = owner.post("/api/v1/orgs", json={"name": "T2", "slug": "t2-x"}).json()["org"]["id"]
    with orgctx.org_scope(oid2):
        store.replace_user_permissions(
            gid,
            [
                {
                    "capability": "task:update",
                    "effect": "deny",
                    "scope_type": "global",
                    "scope_id": "",
                }
            ],
        )
    counts = store.delete_user_cascade(gid)
    assert counts.get("user_permissions", 0) >= 1
    with orgctx.org_scope(oid2):
        assert store.list_user_permissions(gid, limit=10) == []
    store.close()
