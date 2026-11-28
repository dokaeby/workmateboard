"""spec 003 US5 — _org_access_block 면제 경로 + org_delete TOCTOU 회귀 테스트.

suspended/deleted 조직 컨텍스트에서도 탈출·공개 경로는 열려 있어야 한다:
- GET/POST /api/v1/orgs (정확 경로 포함) — 조직 목록·생성으로 탈출
- /api/v1/invitations/* — 타깃 조직 상태는 라우트가 자체 검증
- GET /api/v1/share/{token} — 토큰 기반 공개 스냅샷(세션 유무 무관)
"""

import secrets
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from jobmate.api.app import create_app
from jobmate.config import Settings
from jobmate.security import hash_password, hash_token
from jobmate.store.db import Store
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
    """test_org_lifecycle._team_org와 동일 패턴 — (oid, owner_c, member_c, ow, mm)."""
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


def _platform_admin(store, app, email="admin@b.com"):
    aid = _mkuser(store, email, role="admin")
    return _sess(store, app, aid, "adm")


def _capture_invites(monkeypatch):
    """mailer.send_org_invite_email을 가로채 초대 URL(토큰 포함)을 수집."""
    sent: list[str] = []

    def fake(to, org_name, role, invite_url, gmail=None):
        sent.append(invite_url)
        return True

    monkeypatch.setattr("jobmate.mailer.send_org_invite_email", fake)
    return sent


def _token_of(url: str) -> str:
    return url.split("token=", 1)[-1]


# ---------- /api/v1/orgs 정확 경로 면제 ----------


def test_suspended_org_user_can_list_and_create_orgs(monkeypatch):
    """suspended 조직 ctx에서도 GET/POST /api/v1/orgs는 열려 있어야 한다 —
    조직 목록 조회·새 조직 생성이 유일한 탈출 경로다."""
    client, store, _s, _l = _client(monkeypatch)
    oid, owner, member, _ow, _mm = _team_org(client, store)
    adm = _platform_admin(store, client.app)
    assert adm.post(f"/api/v1/orgs/{oid}/suspend").status_code == 200

    # owner — suspended ctx에서 조직 목록 조회 + 새 조직 생성 가능
    r = owner.get("/api/v1/orgs")
    assert r.status_code == 200, r.text
    assert any(o["id"] == oid for o in r.json()["orgs"])
    r = owner.post("/api/v1/orgs", json={"name": "Escape", "slug": "escape-org"})
    assert r.status_code == 201, r.text
    new_oid = r.json()["org"]["id"]
    assert new_oid != oid

    # member — 전면 차단 대상이지만 orgs 목록/생성은 탈출 경로로 허용
    r = member.get("/api/v1/orgs")
    assert r.status_code == 200, r.text
    r = member.post("/api/v1/orgs", json={"name": "Escape2", "slug": "escape2-org"})
    assert r.status_code == 201, r.text

    # 대조 — 면제가 아닌 경로는 여전히 차단
    assert member.get("/api/v1/management/tasks").status_code == 403
    assert owner.post("/api/v1/management/tasks", json={"title": "x"}).status_code == 403
    store.close()


# ---------- /api/v1/invitations/* 면제 ----------


def test_invite_accept_reaches_route_under_suspended_ctx(monkeypatch):
    """현재 org(A)가 suspended여도 타깃 조직(B)의 초대 수락은 라우트 로직까지
    도달해야 한다 — invite_accept가 타깃 조직 상태를 자체 검증한다."""
    client, store, _s, _l = _client(monkeypatch)
    sent = _capture_invites(monkeypatch)

    # 수신자 X — 조직 A를 만들어 소유하고 ctx를 A로 둔다
    xid = _mkuser(store, "invitee@b.com")
    xc = _sess(store, client.app, xid, "x")
    r = xc.post("/api/v1/orgs", json={"name": "OrgA", "slug": "org-a-x"})
    assert r.status_code == 201, r.text
    oid_a = r.json()["org"]["id"]
    assert xc.post(f"/api/v1/orgs/{oid_a}/switch").status_code == 200

    # 초대자 Y — 조직 B에서 X 이메일로 초대 발송
    yid = _mkuser(store, "inviter@b.com")
    yc = _sess(store, client.app, yid, "y")
    r = yc.post("/api/v1/orgs", json={"name": "OrgB", "slug": "org-b-y"})
    assert r.status_code == 201, r.text
    oid_b = r.json()["org"]["id"]
    assert yc.post(f"/api/v1/orgs/{oid_b}/switch").status_code == 200
    r = yc.post(
        f"/api/v1/orgs/{oid_b}/invitations",
        json={"email": "invitee@b.com", "role": "member"},
    )
    assert r.status_code == 201, r.text
    assert sent, "invite email was not captured"
    token = _token_of(sent[-1])

    # A 정지 — X의 세션 ctx는 suspended 조직 A
    adm = _platform_admin(store, client.app)
    assert adm.post(f"/api/v1/orgs/{oid_a}/suspend").status_code == 200
    # 대조 — 면제 아닌 쓰기 경로는 여전히 차단(X는 A의 owner라 읽기는 허용)
    r = xc.post("/api/v1/management/tasks", json={"title": "x"})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "org_suspended"

    # 초대 정보 조회 + 수락 — 타깃 조직 B는 active라 성공해야 한다
    r = xc.get(f"/api/v1/invitations/info?token={token}")
    assert r.status_code == 200, r.text
    assert r.json()["org"]["id"] == oid_b
    r = xc.post(f"/api/v1/invitations/accept?token={token}")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "accepted"
    assert r.json()["org_id"] == oid_b
    assert store.get_org_membership(oid_b, xid) is not None
    store.close()


# ---------- project permissions GET suspended 읽기 규칙 ----------


def test_project_permissions_get_suspended_read_rule(monkeypatch):
    """suspended 조직에서 project permissions GET은 org_owner만 허용 —
    org_admin 멤버는 403 org_suspended (PUT/DELETE는 전면 403)."""
    client, store, _s, _l = _client(monkeypatch)
    oid, owner, member, ow, mm = _team_org(client, store)

    # org_admin 멤버 추가 — guard(org_admin+)는 통과하지만 suspended 읽기는
    # owner만 허용해야 하므로 owner와 결과가 갈려야 한다
    adm_uid = _mkuser(store, "orgadm@b.com")
    store.add_org_member(oid, adm_uid, "org_admin")
    adm_c = _sess(store, client.app, adm_uid, "oa")
    assert adm_c.post(f"/api/v1/orgs/{oid}/switch").status_code == 200

    # 프로젝트 생성(active 상태에서)
    r = owner.post("/api/v1/management/projects", json={"name": "PP"})
    assert r.status_code == 201, r.text
    pid = r.json()["id"]

    adm = _platform_admin(store, client.app)
    assert adm.post(f"/api/v1/orgs/{oid}/suspend").status_code == 200

    url = f"/api/v1/orgs/{oid}/projects/{pid}/permissions"
    # owner — suspended 읽기 허용
    r = owner.get(url)
    assert r.status_code == 200, r.text
    assert "items" in r.json()
    # org_admin 멤버 — 비owner라 403 org_suspended
    r = adm_c.get(url)
    assert r.status_code == 403, r.text
    assert r.json()["error"]["code"] == "org_suspended"
    # 일반 member — guard 단계에서 이미 403
    r = member.get(url)
    assert r.status_code == 403
    # 쓰기 sibling은 suspended에서 전면 차단(owner도)
    r = owner.put(f"{url}/{ow}", json={"rules": []})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "org_suspended"
    store.close()


# ---------- 공개 share GET 일관성 ----------


def test_share_get_consistent_for_suspended_session_user(monkeypatch):
    """공개 공유 스냅샷 GET은 토큰 capability — anonymous와 suspended 조직
    세션 사용자에게 동일하게 200이어야 한다(미들웨어 인증 예외와 정합)."""
    client, store, _s, _l = _client(monkeypatch)
    oid, owner, _member, _ow, _mm = _team_org(client, store)

    token = secrets.token_urlsafe(24)
    expires = (datetime.now(UTC) + timedelta(days=30)).isoformat()
    store.create_share_link(
        token,
        "report",
        "",
        {"summary": "ok"},
        "tester",
        expires,
    )
    url = f"/api/v1/share/{token}"

    # anonymous — 인증 없이 200
    anon = TestClient(client.app)
    r = anon.get(url)
    assert r.status_code == 200, r.text
    assert r.json()["payload"]["summary"] == "ok"

    # suspended 조직 세션 owner — 동일하게 200이어야 한다
    adm = _platform_admin(store, client.app)
    assert adm.post(f"/api/v1/orgs/{oid}/suspend").status_code == 200
    r = owner.get(url)
    assert r.status_code == 200, r.text
    assert r.json()["payload"]["summary"] == "ok"

    # 대조 — 면제가 아닌 쓰기 경로는 여전히 차단
    r = owner.post("/api/v1/management/tasks", json={"title": "x"})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "org_suspended"
    store.close()
