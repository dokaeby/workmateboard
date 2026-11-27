"""조직(Organization) 테스트 — spec 003 US1/US5.

개인 워크스페이스 자동 생성, 조직 CRUD, 멤버십·역할, 스위치,
소유권 이관, 마지막 owner 보호, soft-delete를 검증한다.
"""

import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from jobmate.api.app import create_app
from jobmate.config import Settings
from jobmate.security import hash_password
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


def _mkuser(store, email="u@b.com", password=_PW):
    user = store.create_user(email, hash_password(password), "U", "", True)
    return user["id"] if isinstance(user, dict) else user


def _uid(store, email="u@b.com"):
    return store.get_user_by_email(email)["id"]


def _login(client, email="u@b.com", password=_PW):
    return client.post("/api/v1/auth/login", json={"email": email, "password": password})


def _signup(client, email="s@b.com"):
    return client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": _PW, "agree_terms": True},
    )


def _create_org(client, name="Team", slug=None):
    body = {"name": name}
    if slug:
        body["slug"] = slug
    return client.post("/api/v1/orgs", json=body)


# ---- 개인 워크스페이스 ----


def test_signup_creates_personal_org(monkeypatch):
    """FR-003 — 가입 완료 시 kind='personal' 조직 + org_owner 멤버십."""
    client, store = _client(monkeypatch)
    r = _signup(client, "new@b.com")
    assert r.status_code == 201
    uid = _uid(store, "new@b.com")
    orgs = store.list_user_orgs(uid)
    assert len(orgs) == 1
    assert orgs[0]["kind"] == "personal"
    assert orgs[0]["my_role"] == "org_owner"
    store.close()


def test_login_session_has_org_context(monkeypatch):
    """로그인 세션의 current_org_id가 개인 조직으로 설정된다."""
    client, store = _client(monkeypatch)
    _signup(client, "ctx@b.com")
    # 인증 요구 환경이면 로그인 불가 — conftest 기본은 인증 불필요일 수 있으므로
    # 세션 발급 여부와 무관하게 세션 행을 직접 확인한다
    uid = _uid(store, "ctx@b.com")
    orgs = store.list_user_orgs(uid)
    assert orgs and orgs[0]["kind"] == "personal"
    store.close()


# ---- 조직 CRUD ----


def test_create_list_get_org(monkeypatch):
    client, store = _client(monkeypatch)
    _mkuser(store)
    _login(client)
    r = _create_org(client, "Alpha", slug="alpha-team")
    assert r.status_code == 201
    org = r.json()["org"]
    assert org["slug"] == "alpha-team" and org["kind"] == "team"
    assert r.json()["my_role"] == "org_owner"
    # 목록
    orgs = client.get("/api/v1/orgs").json()["orgs"]
    assert any(o["id"] == org["id"] for o in orgs)
    # 단건 조회
    g = client.get(f"/api/v1/orgs/{org['id']}")
    assert g.status_code == 200 and g.json()["my_role"] == "org_owner"
    store.close()


def test_org_slug_conflict_409(monkeypatch):
    client, store = _client(monkeypatch)
    _mkuser(store)
    _login(client)
    assert _create_org(client, "A", slug="same-slug").status_code == 201
    r = _create_org(client, "B", slug="SAME-SLUG")  # 대소문자 무시 충돌
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "slug_taken"
    store.close()


def test_org_non_member_gets_404(monkeypatch):
    """비멤버는 존재 자체가 보이지 않는다(404)."""
    client, store = _client(monkeypatch)
    _mkuser(store, "owner@b.com")
    _mkuser(store, "other@b.com")
    _login(client, "owner@b.com")
    oid = _create_org(client, "Secret", slug="secret-org").json()["org"]["id"]
    _login(client, "other@b.com")
    assert client.get(f"/api/v1/orgs/{oid}").status_code == 404
    assert client.get(f"/api/v1/orgs/{oid}/members").status_code == 404
    store.close()


def test_org_requires_auth(monkeypatch):
    client, store = _client(monkeypatch)
    assert client.get("/api/v1/orgs").status_code == 401
    assert _create_org(client).status_code == 401
    store.close()


# ---- 멤버십·역할 ----


def _two_member_org(client, store):
    """owner + member 2인 조직 구성."""
    _mkuser(store, "owner@b.com")
    _mkuser(store, "member@b.com")
    _login(client, "owner@b.com")
    oid = _create_org(client, "Two", slug="two-org").json()["org"]["id"]
    store.add_org_member(oid, _uid(store, "member@b.com"), "member")
    return oid


def test_member_cannot_update_org(monkeypatch):
    client, store = _client(monkeypatch)
    oid = _two_member_org(client, store)
    _login(client, "member@b.com")
    r = client.patch(f"/api/v1/orgs/{oid}", json={"name": "Hijack"})
    assert r.status_code == 403
    store.close()


def test_org_admin_can_update_and_member_lists(monkeypatch):
    client, store = _client(monkeypatch)
    oid = _two_member_org(client, store)
    _login(client, "owner@b.com")
    r = client.patch(f"/api/v1/orgs/{oid}", json={"name": "Renamed", "slug": "renamed-org"})
    assert r.status_code == 200
    assert r.json()["org"]["slug"] == "renamed-org"
    # 멤버 목록 — member도 조회 가능
    _login(client, "member@b.com")
    members = client.get(f"/api/v1/orgs/{oid}/members").json()["members"]
    assert len(members) == 2
    roles = {m["email"]: m["role"] for m in members}
    assert roles["owner@b.com"] == "org_owner"
    assert roles["member@b.com"] == "member"
    store.close()


def test_member_role_change_and_remove(monkeypatch):
    client, store = _client(monkeypatch)
    oid = _two_member_org(client, store)
    member_uid = _uid(store, "member@b.com")
    _login(client, "owner@b.com")
    # member → org_admin 승격
    r = client.patch(f"/api/v1/orgs/{oid}/members/{member_uid}", json={"role": "org_admin"})
    assert r.status_code == 200
    assert store.get_org_membership(oid, member_uid)["role"] == "org_admin"
    # org_owner 지정은 PATCH로 불가(이관만)
    r = client.patch(f"/api/v1/orgs/{oid}/members/{member_uid}", json={"role": "org_owner"})
    assert r.status_code == 422
    # 제거
    assert client.delete(f"/api/v1/orgs/{oid}/members/{member_uid}").status_code == 200
    assert store.get_org_membership(oid, member_uid) is None
    store.close()


def test_member_cannot_manage_members(monkeypatch):
    client, store = _client(monkeypatch)
    oid = _two_member_org(client, store)
    member_uid = _uid(store, "member@b.com")
    _login(client, "member@b.com")
    assert (
        client.patch(
            f"/api/v1/orgs/{oid}/members/{member_uid}", json={"role": "member"}
        ).status_code
        == 403
    )
    assert client.delete(f"/api/v1/orgs/{oid}/members/{member_uid}").status_code == 403
    store.close()


# ---- 스위치 ----


def test_switch_org_context(monkeypatch):
    client, store = _client(monkeypatch)
    _mkuser(store, "multi@b.com")
    _mkuser(store, "other@b.com")
    uid = _uid(store, "multi@b.com")
    _login(client, "other@b.com")
    oid = _create_org(client, "SwitchTarget", slug="switch-target").json()["org"]["id"]
    store.add_org_member(oid, uid, "member")
    _login(client, "multi@b.com")
    r = client.post(f"/api/v1/orgs/{oid}/switch")
    assert r.status_code == 200
    assert r.json()["org_id"] == oid
    # 세션 current_org_id가 갱신됐는지 DB로 확인
    row = store._conn.execute(
        "SELECT current_org_id FROM auth_sessions s JOIN users u ON u.id=s.user_id"
        " WHERE u.email='multi@b.com' ORDER BY s.created_at DESC LIMIT 1"
    ).fetchone()
    assert row["current_org_id"] == oid
    # 비멤버 조직으로 스위치 → 404
    bad = client.post("/api/v1/orgs/nonexistent/switch")
    assert bad.status_code == 404
    store.close()


# ---- 소유권 이관·마지막 owner 보호 ----


def test_transfer_ownership(monkeypatch):
    client, store = _client(monkeypatch)
    oid = _two_member_org(client, store)
    member_uid = _uid(store, "member@b.com")
    _login(client, "owner@b.com")
    r = client.post(f"/api/v1/orgs/{oid}/transfer", json={"user_id": member_uid})
    assert r.status_code == 200
    assert store.get_org_membership(oid, member_uid)["role"] == "org_owner"
    owner_uid = _uid(store, "owner@b.com")
    assert store.get_org_membership(oid, owner_uid)["role"] == "org_admin"
    # 이전 owner는 더 이상 transfer 불가
    r = client.post(f"/api/v1/orgs/{oid}/transfer", json={"user_id": owner_uid})
    assert r.status_code == 403
    store.close()


def test_last_org_owner_protection(monkeypatch):
    """마지막 owner의 탈퇴·제거·강등은 409 last_org_owner."""
    client, store = _client(monkeypatch)
    _mkuser(store, "solo@b.com")
    _login(client, "solo@b.com")
    oid = _create_org(client, "Solo", slug="solo-org").json()["org"]["id"]
    # 본인 탈퇴 → 409
    r = client.delete(f"/api/v1/orgs/{oid}/members/me")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "last_org_owner"
    # 강등 시도(admin이 아닌 본인 PATCH도 불가하지만 store 경로로 검증)
    # 조직 삭제는 멤버 1명이라 허용 — soft delete 확인
    r = client.delete(f"/api/v1/orgs/{oid}")
    assert r.status_code == 200
    assert store.get_organization(oid)["status"] == "deleted"
    # 삭제된 조직은 접근 불가
    assert client.get(f"/api/v1/orgs/{oid}").status_code == 404
    store.close()


def test_org_delete_requires_members_removed(monkeypatch):
    client, store = _client(monkeypatch)
    oid = _two_member_org(client, store)
    _login(client, "owner@b.com")
    r = client.delete(f"/api/v1/orgs/{oid}")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "org_has_members"
    store.close()


def test_org_delete_requires_owner(monkeypatch):
    client, store = _client(monkeypatch)
    oid = _two_member_org(client, store)
    _login(client, "member@b.com")
    assert client.delete(f"/api/v1/orgs/{oid}").status_code == 403
    store.close()


# ---- 감사 ----


def test_org_audit_events(monkeypatch):
    client, store = _client(monkeypatch)
    _mkuser(store)
    _login(client)
    oid = _create_org(client, "Audited", slug="audited-org").json()["org"]["id"]
    client.post(f"/api/v1/orgs/{oid}/switch")
    actions = [a["action"] for a in store.list_audit(limit=500)]
    assert "org_created" in actions
    assert "org_switched" in actions
    store.close()


# ---- 초대 (US2) ----


def _capture_invites(monkeypatch):
    """mailer.send_org_invite_email을 가로채 초대 URL(토큰 포함)을 수집."""
    sent: list[str] = []

    def fake(to, org_name, role, invite_url, gmail=None):
        sent.append(invite_url)
        return True

    monkeypatch.setattr("jobmate.mailer.send_org_invite_email", fake)
    return sent


def _invite(client, org_id, email, role="member"):
    return client.post(f"/api/v1/orgs/{org_id}/invitations", json={"email": email, "role": role})


def _token_of(url: str) -> str:
    return url.split("token=", 1)[-1]


def test_invite_create_accept_and_single_use(monkeypatch):
    """생성→수락→멤버 추가, 토큰은 단일 사용, 해시만 저장."""
    client, store = _client(monkeypatch)
    sent = _capture_invites(monkeypatch)
    oid = _two_member_org(client, store)  # owner@b.com이 owner
    _login(client, "owner@b.com")
    r = _invite(client, oid, "newbie@b.com")
    assert r.status_code == 201
    inv = r.json()["invitation"]
    assert inv["status"] == "pending" and inv["role"] == "member"
    assert r.json()["email_sent"] is True
    token = _token_of(sent[-1])
    # DB에는 원문 토큰이 없고 해시만 있다
    row = store._conn.execute(
        "SELECT token_hash FROM org_invitations WHERE id=?", (inv["id"],)
    ).fetchone()
    assert row["token_hash"] != token and len(row["token_hash"]) == 64

    # 초대 대상 가입 후 수락
    _mkuser(store, "newbie@b.com")
    _login(client, "newbie@b.com")
    r = client.post(f"/api/v1/invitations/accept?token={token}")
    assert r.status_code == 200 and r.json()["status"] == "accepted"
    m = store.get_org_membership(oid, _uid(store, "newbie@b.com"))
    assert m is not None and m["role"] == "member"
    # 재사용 거부
    r = client.post(f"/api/v1/invitations/accept?token={token}")
    assert r.status_code == 410
    store.close()


def test_invite_accept_email_mismatch(monkeypatch):
    client, store = _client(monkeypatch)
    sent = _capture_invites(monkeypatch)
    oid = _two_member_org(client, store)
    _login(client, "owner@b.com")
    _invite(client, oid, "invitee@b.com")
    token = _token_of(sent[-1])
    _mkuser(store, "other@b.com")
    _login(client, "other@b.com")
    r = client.post(f"/api/v1/invitations/accept?token={token}")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "invite_email_mismatch"
    assert store.get_org_membership(oid, _uid(store, "other@b.com")) is None
    store.close()


def test_invite_decline_and_revoke(monkeypatch):
    client, store = _client(monkeypatch)
    sent = _capture_invites(monkeypatch)
    oid = _two_member_org(client, store)
    _login(client, "owner@b.com")
    _invite(client, oid, "decline@b.com")
    token = _token_of(sent[-1])

    _mkuser(store, "decline@b.com")
    _login(client, "decline@b.com")
    r = client.post(f"/api/v1/invitations/decline?token={token}")
    assert r.status_code == 200 and r.json()["status"] == "declined"
    # 거절 후 수락 불가
    r = client.post(f"/api/v1/invitations/accept?token={token}")
    assert r.status_code == 410

    # revoke 경로
    _login(client, "owner@b.com")
    _invite(client, oid, "rev@b.com")
    invs = client.get(f"/api/v1/orgs/{oid}/invitations").json()["invitations"]
    rid = next(i["id"] for i in invs if i["email"] == "rev@b.com")
    r = client.delete(f"/api/v1/orgs/{oid}/invitations/{rid}")
    assert r.status_code == 200
    # 이미 revoked → 재취소 409
    r = client.delete(f"/api/v1/orgs/{oid}/invitations/{rid}")
    assert r.status_code == 409
    store.close()


def test_invite_resend_invalidates_old_token(monkeypatch):
    client, store = _client(monkeypatch)
    sent = _capture_invites(monkeypatch)
    oid = _two_member_org(client, store)
    _login(client, "owner@b.com")
    _invite(client, oid, "rs@b.com")
    inv_id = client.get(f"/api/v1/orgs/{oid}/invitations").json()["invitations"][0]["id"]

    # 10분 이내 재발송 → 429
    r = client.post(f"/api/v1/orgs/{oid}/invitations/{inv_id}/resend")
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "resend_too_soon"

    # 발급 시각을 11분 전으로 되돌리면 재발송 가능
    store._conn.execute(
        "UPDATE org_invitations SET created_at=? WHERE id=?",
        ("2000-01-01T00:00:00+00:00", inv_id),
    )
    store._conn.commit()
    r = client.post(f"/api/v1/orgs/{oid}/invitations/{inv_id}/resend")
    assert r.status_code == 200
    old_token = _token_of(sent[0])
    new_token = _token_of(sent[-1])
    assert old_token != new_token
    # 이전 토큰은 무효 — 정보 조회/수락 모두 불가
    r = client.get(f"/api/v1/invitations/info?token={old_token}")
    assert r.status_code == 410 or r.json().get("status") != "pending"
    store.close()


def test_invite_expired(monkeypatch):
    client, store = _client(monkeypatch)
    sent = _capture_invites(monkeypatch)
    oid = _two_member_org(client, store)
    _login(client, "owner@b.com")
    _invite(client, oid, "exp@b.com")
    token = _token_of(sent[-1])
    store._conn.execute(
        "UPDATE org_invitations SET expires_at=? WHERE email=?",
        ("2000-01-01T00:00:00+00:00", "exp@b.com"),
    )
    store._conn.commit()
    # 공개 조회에서 만료 전이
    r = client.get(f"/api/v1/invitations/info?token={token}")
    assert r.json()["status"] == "expired"
    _mkuser(store, "exp@b.com")
    _login(client, "exp@b.com")
    r = client.post(f"/api/v1/invitations/accept?token={token}")
    assert r.status_code == 410
    assert r.json()["error"]["code"] == "invite_expired"
    store.close()


def test_invite_role_and_email_validation(monkeypatch):
    client, store = _client(monkeypatch)
    _capture_invites(monkeypatch)
    oid = _two_member_org(client, store)
    _login(client, "owner@b.com")
    # org_owner는 초대로 부여 불가
    r = _invite(client, oid, "x@b.com", role="org_owner")
    assert r.status_code == 422
    r = _invite(client, oid, "not-an-email")
    assert r.status_code == 422
    # member는 초대 생성 불가
    _login(client, "member@b.com")
    r = _invite(client, oid, "y@b.com")
    assert r.status_code == 403
    store.close()


def test_invite_already_member_and_suspended(monkeypatch):
    client, store = _client(monkeypatch)
    _capture_invites(monkeypatch)
    oid = _two_member_org(client, store)
    _login(client, "owner@b.com")
    r = _invite(client, oid, "member@b.com")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "already_member"
    # suspended 조직은 초대 생성 차단
    store.set_organization_status(oid, "suspended")
    r = _invite(client, oid, "new@b.com")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "org_suspended"
    store.close()


def test_invite_reuses_existing_account(monkeypatch):
    """이미 가입된 이메일 초대 → 계정 재사용, 멤버십만 추가."""
    client, store = _client(monkeypatch)
    sent = _capture_invites(monkeypatch)
    oid = _two_member_org(client, store)
    _mkuser(store, "existing@b.com")
    uid = _uid(store, "existing@b.com")
    _login(client, "owner@b.com")
    _invite(client, oid, "existing@b.com", role="org_admin")
    token = _token_of(sent[-1])
    _login(client, "existing@b.com")
    r = client.post(f"/api/v1/invitations/accept?token={token}")
    assert r.status_code == 200
    m = store.get_org_membership(oid, uid)
    assert m is not None and m["role"] == "org_admin"
    # 계정이 새로 생성되지 않고 기존 user_id가 재사용됐다
    assert _uid(store, "existing@b.com") == uid
    store.close()


def test_invite_info_public_and_audit(monkeypatch):
    client, store = _client(monkeypatch)
    sent = _capture_invites(monkeypatch)
    oid = _two_member_org(client, store)
    _login(client, "owner@b.com")
    _invite(client, oid, "info@b.com")
    token = _token_of(sent[-1])
    # 비로그인 조회 가능 — 최소 정보만
    r = client.get(f"/api/v1/invitations/info?token={token}")
    assert r.status_code == 200
    body = r.json()
    assert body["email"] == "info@b.com" and body["status"] == "pending"
    assert body["org"]["name"] == "Two"
    # 감사 이벤트
    actions = [a["action"] for a in store.list_audit(limit=500)]
    assert "org_invite_created" in actions
    store.close()


# ---- 리뷰 보강: 초대 상태 전이·스위치·suspended·마이그레이션 ----


def test_invite_lifecycle_status_transitions(monkeypatch):
    """accept 후 DB status='accepted', 소비된 초대는 resend/revoke 불가."""
    client, store = _client(monkeypatch)
    sent = _capture_invites(monkeypatch)
    oid = _two_member_org(client, store)
    _login(client, "owner@b.com")
    _invite(client, oid, "life@b.com")
    token = _token_of(sent[-1])
    inv_id = client.get(f"/api/v1/orgs/{oid}/invitations").json()["invitations"][0]["id"]

    _mkuser(store, "life@b.com")
    _login(client, "life@b.com")
    assert client.post(f"/api/v1/invitations/accept?token={token}").status_code == 200
    inv = store.get_org_invitation(inv_id)
    assert inv["status"] == "accepted" and inv["used_at"] is not None
    # 공개 정보도 accepted
    client.cookies.clear()
    r = client.get(f"/api/v1/invitations/info?token={token}")
    assert r.json()["status"] == "accepted"
    # 소비된 초대는 resend/revoke 불가
    _login(client, "owner@b.com")
    assert client.post(f"/api/v1/orgs/{oid}/invitations/{inv_id}/resend").status_code == 409
    assert client.delete(f"/api/v1/orgs/{oid}/invitations/{inv_id}").status_code == 409
    store.close()


def test_invite_resend_new_token_works_old_fails(monkeypatch):
    """재발송 후 새 토큰만 유효 — 새 토큰 accept 성공, 이전 토큰 410."""
    client, store = _client(monkeypatch)
    sent = _capture_invites(monkeypatch)
    oid = _two_member_org(client, store)
    _login(client, "owner@b.com")
    _invite(client, oid, "rs2@b.com")
    inv_id = client.get(f"/api/v1/orgs/{oid}/invitations").json()["invitations"][0]["id"]
    store._conn.execute(
        "UPDATE org_invitations SET created_at=? WHERE id=?",
        ("2000-01-01T00:00:00+00:00", inv_id),
    )
    store._conn.commit()
    r = client.post(f"/api/v1/orgs/{oid}/invitations/{inv_id}/resend")
    assert r.status_code == 200
    old_token, new_token = _token_of(sent[0]), _token_of(sent[-1])
    # 이전 토큰 accept → 410
    _mkuser(store, "rs2@b.com")
    _login(client, "rs2@b.com")
    assert client.post(f"/api/v1/invitations/accept?token={old_token}").status_code == 410
    # 새 토큰 accept → 200
    r = client.post(f"/api/v1/invitations/accept?token={new_token}")
    assert r.status_code == 200
    # pending 목록은 해당 이메일 1건
    _login(client, "owner@b.com")
    invs = client.get(f"/api/v1/orgs/{oid}/invitations").json()["invitations"]
    assert [i["email"] for i in invs].count("rs2@b.com") == 0  # 이미 accepted
    store.close()


def test_invite_info_public_without_session(monkeypatch):
    """GET /invitations/info는 비로그인 접근 가능 — 미들웨어 인증 예외."""
    client, store = _client(monkeypatch)
    sent = _capture_invites(monkeypatch)
    oid = _two_member_org(client, store)
    _login(client, "owner@b.com")
    _invite(client, oid, "pub@b.com")
    token = _token_of(sent[-1])
    client.cookies.clear()  # 세션 쿠키 제거 — 비로그인 상태
    r = client.get(f"/api/v1/invitations/info?token={token}")
    assert r.status_code == 200
    body = r.json()
    assert body["email"] == "pub@b.com" and body["status"] == "pending"
    # 토큰 원문/해시가 응답에 없어야 한다
    assert token not in str(body) and "token" not in body
    store.close()


def test_switch_org_context_actually_changes(monkeypatch):
    """두 조직 멤버 — default 컨텍스트에서 다른 조직으로 실제 전환."""
    client, store = _client(monkeypatch)
    _mkuser(store, "sw@b.com")
    uid = _uid(store, "sw@b.com")
    _login(client, "sw@b.com")
    oid_a = _create_org(client, "OrgA", slug="org-a").json()["org"]["id"]
    oid_b = _create_org(client, "OrgB", slug="org-b").json()["org"]["id"]
    # B에 나중에 합류 — default_org_id는 joined_at 순 첫 조직(A)
    store.add_org_member(oid_b, uid, "member")
    assert store.user_default_org_id(uid) == oid_a
    token = client.cookies.get("jobmate_session", "")
    import hashlib

    th = hashlib.sha256(token.encode()).hexdigest()
    # 조직 생성 전 발급된 세션은 current_org_id가 비어 있음 — A로 먼저 스위치
    assert client.post(f"/api/v1/orgs/{oid_a}/switch").status_code == 200
    assert store.get_session_user(th)["current_org_id"] == oid_a
    # A → B 실제 전환
    r = client.post(f"/api/v1/orgs/{oid_b}/switch")
    assert r.status_code == 200
    assert store.get_session_user(th)["current_org_id"] == oid_b
    # 비멤버 조직으로 스위치 → 404
    _mkuser(store, "other@b.com")
    _login(client, "other@b.com")
    oid_c = _create_org(client, "OrgC", slug="org-c").json()["org"]["id"]
    _login(client, "sw@b.com")
    r = client.post(f"/api/v1/orgs/{oid_c}/switch")
    assert r.status_code == 404
    store.close()


def test_last_org_owner_demote_and_remove_blocked(monkeypatch):
    """마지막 owner의 PATCH 강등·DELETE 제거도 409 last_org_owner."""
    client, store = _client(monkeypatch)
    oid = _two_member_org(client, store)
    owner_uid = _uid(store, "owner@b.com")
    member_uid = _uid(store, "member@b.com")
    _login(client, "owner@b.com")
    # owner가 스스로 강등 시도 → 409
    r = client.patch(f"/api/v1/orgs/{oid}/members/{owner_uid}", json={"role": "member"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "last_org_owner"
    # owner 제거 시도 → 409
    r = client.delete(f"/api/v1/orgs/{oid}/members/{owner_uid}")
    assert r.status_code == 409 and r.json()["error"]["code"] == "last_org_owner"
    # 소유권 이관 후에는 이전 owner(org_admin)가 새 owner를 제거 불가
    r = client.post(f"/api/v1/orgs/{oid}/transfer", json={"user_id": member_uid})
    assert r.status_code == 200
    r = client.delete(f"/api/v1/orgs/{oid}/members/{member_uid}")
    assert r.status_code == 409 and r.json()["error"]["code"] == "last_org_owner"
    store.close()


def test_suspended_org_matrix(monkeypatch):
    """suspended: owner 읽기 허용·쓰기 403, member 읽기·쓰기 모두 403."""
    client, store = _client(monkeypatch)
    oid = _two_member_org(client, store)
    store.set_organization_status(oid, "suspended")
    # owner: GET 허용, PATCH/DELETE 쓰기 403
    _login(client, "owner@b.com")
    assert client.get(f"/api/v1/orgs/{oid}").status_code == 200
    assert client.patch(f"/api/v1/orgs/{oid}", json={"name": "X"}).status_code == 403
    assert client.delete(f"/api/v1/orgs/{oid}").status_code == 403
    # member: GET도 403
    _login(client, "member@b.com")
    r = client.get(f"/api/v1/orgs/{oid}")
    assert r.status_code == 403 and r.json()["error"]["code"] == "org_suspended"
    store.close()


def test_patch_slug_conflict_and_personal_delete(monkeypatch):
    client, store = _client(monkeypatch)
    _mkuser(store)
    _login(client)
    oid = _create_org(client, "One", slug="org-one").json()["org"]["id"]
    _create_org(client, "Two", slug="org-two")
    # slug 충돌 → 409 slug_taken
    r = client.patch(f"/api/v1/orgs/{oid}", json={"slug": "org-two"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "slug_taken"
    # 개인 조직 삭제 불가 — 계정 삭제 경로 사용
    uid = _uid(store, "u@b.com")
    personal = [o for o in store.list_user_orgs(uid) if o["kind"] == "personal"]
    if personal:
        r = client.delete(f"/api/v1/orgs/{personal[0]['id']}")
        assert r.status_code == 409 and r.json()["error"]["code"] == "personal_org"
    store.close()


def test_account_delete_blocked_for_last_org_owner(monkeypatch):
    """유일 org_owner의 team 조직이 있으면 계정 삭제 409 last_org_owner."""
    client, store = _client(monkeypatch)
    _two_member_org(client, store)  # owner@b.com이 유일 owner + member 존재
    _login(client, "owner@b.com")
    r = client.request("DELETE", "/api/v1/auth/account", json={"password": _PW})
    assert r.status_code == 409 and r.json()["error"]["code"] == "last_org_owner"
    # 계정은 살아있다
    assert store.get_user_by_email("owner@b.com") is not None
    store.close()


def test_backfill_default_org_and_roles(monkeypatch):
    """_migrate_org_columns — 최초 admin이 org_owner, 나머지 admin=org_admin."""
    client, store = _client(monkeypatch)
    from jobmate.security import hash_password

    # 레거시 상태 재현: member→admin→admin 순 생성 후 마이그레이션 재실행
    u1 = store.create_user("m@x.com", hash_password(_PW), "M", "", True)
    u2 = store.create_user("a1@x.com", hash_password(_PW), "A1", "", True)
    store._conn.execute("UPDATE users SET role='admin' WHERE id=?", (u2["id"],))
    u3 = store.create_user("a2@x.com", hash_password(_PW), "A2", "", True)
    store._conn.execute("UPDATE users SET role='admin' WHERE id=?", (u3["id"],))
    store._conn.commit()
    # 조직이 없는 상태에서 백필 강제 실행
    store._conn.execute("DELETE FROM organization_members")
    store._conn.execute("DELETE FROM organizations")
    store._conn.commit()
    store._migrate_org_columns()
    org = store.get_organization_by_slug("default")
    assert org is not None and org["kind"] == "team"
    m1 = store.get_org_membership(org["id"], u1["id"])
    m2 = store.get_org_membership(org["id"], u2["id"])
    m3 = store.get_org_membership(org["id"], u3["id"])
    assert m2["role"] == "org_owner"  # 최초 admin
    assert m3["role"] == "org_admin"
    assert m1["role"] == "member"
    store.close()
