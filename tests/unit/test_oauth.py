"""Google OAuth 로그인 테스트 — 설정 별칭, 콜백 플로우, CSRF state, 역할."""

import tempfile
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from jobmate.api.app import create_app
from jobmate.config import Settings
from jobmate.store.db import Store
from tests.integration.helpers import default_outputs, scripted_llm

_OAUTH_ENVS = (
    "GOOGLE_OAUTH_CLIENT_ID",
    "GOOGLE_OAUTH_CLIENT_SECRET",
    "GOOGLE_OAUTH_REDIRECT_URI",
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
    "GOOGLE_REDIRECT_URI",
)


def _client():
    db = str(Path(tempfile.mkdtemp()) / "t.db")
    settings = Settings(db_path=db, budget_max_tokens=100_000)
    store = Store(db)
    app = create_app(settings=settings, store=store, llm=scripted_llm(default_outputs()))
    return TestClient(app), store


@pytest.fixture
def clean_oauth_env(monkeypatch):
    for name in _OAUTH_ENVS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def _mock_google(monkeypatch, *, sub="gsub-1", email="user@example.com", verified=True):
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *a, **kw: httpx.Response(
            200,
            json={"access_token": "tok"},
            request=httpx.Request("POST", "https://oauth2.googleapis.com/token"),
        ),
    )
    monkeypatch.setattr(
        httpx,
        "get",
        lambda *a, **kw: httpx.Response(
            200,
            json={"sub": sub, "email": email, "name": "User", "email_verified": verified},
            request=httpx.Request("GET", "https://openidconnect.googleapis.com/v1/userinfo"),
        ),
    )


def test_google_login_disabled(clean_oauth_env):
    client, _ = _client()
    assert client.get("/api/v1/auth/config").json()["google_enabled"] is False
    r = client.get("/api/v1/auth/google", follow_redirects=False)
    assert r.status_code == 501
    client.cookies.set("jobmate_oauth_state", "s")
    r = client.get("/api/v1/auth/google/callback?code=c&state=s", follow_redirects=False)
    assert r.status_code == 501


def test_google_login_redirect(clean_oauth_env):
    clean_oauth_env.setenv("GOOGLE_OAUTH_CLIENT_ID", "cid")
    clean_oauth_env.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "sec")
    client, _ = _client()
    assert client.get("/api/v1/auth/config").json()["google_enabled"] is True
    r = client.get("/api/v1/auth/google", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"].startswith("https://accounts.google.com/")
    assert "client_id=cid" in r.headers["location"]
    assert "jobmate_oauth_state" in r.headers.get("set-cookie", "")


def test_google_callback_creates_user_and_session(clean_oauth_env):
    clean_oauth_env.setenv("GOOGLE_OAUTH_CLIENT_ID", "cid")
    clean_oauth_env.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "sec")
    _mock_google(clean_oauth_env)
    client, store = _client()
    client.cookies.set("jobmate_oauth_state", "state")
    r = client.get(
        "/api/v1/auth/google/callback?code=auth-code&state=state", follow_redirects=False
    )
    assert r.status_code == 302
    assert r.headers["location"].endswith("/")
    assert "jobmate_session" in r.headers.get("set-cookie", "")
    user = store.get_user_by_email("user@example.com")
    assert user is not None
    assert user["google_sub"] == "gsub-1"
    assert client.get("/api/v1/auth/me").status_code == 200


def test_google_callback_existing_user_no_duplicate(clean_oauth_env):
    """Google-only 계정(password_hash 빈 값)은 Google sub 자동 연결 허용."""
    clean_oauth_env.setenv("GOOGLE_OAUTH_CLIENT_ID", "cid")
    clean_oauth_env.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "sec")
    _mock_google(clean_oauth_env, sub="gsub-9", email="existing@example.com")
    client, store = _client()
    store.create_user("existing@example.com", "", "Existing")  # Google-only 계정
    before = len(store.list_users())
    client.cookies.set("jobmate_oauth_state", "state")
    r = client.get("/api/v1/auth/google/callback?code=c&state=state", follow_redirects=False)
    assert r.status_code == 302
    assert len(store.list_users()) == before
    user = store.get_user_by_email("existing@example.com")
    assert user["google_sub"] == "gsub-9"  # 기존 계정에 sub 연결


def test_google_callback_password_account_link_refused(clean_oauth_env):
    """Pre-hijacking 방지: 인증된 비밀번호 계정에는 Google sub 자동 연결 거부.

    공격자가 피해자 이메일로 비밀번호 가입을 선점해두면 피해자의 Google
    로그인이 공격자 계정에 연결되어 탈취될 수 있다 — 인증 완료된
    password_hash 계정은 명시적 연결 플로우 전까지 google_account_conflict로 거부."""
    clean_oauth_env.setenv("GOOGLE_OAUTH_CLIENT_ID", "cid")
    clean_oauth_env.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "sec")
    _mock_google(clean_oauth_env, sub="gsub-evil", email="victim@example.com")
    client, store = _client()
    store.create_user("victim@example.com", "scrypt$hash", "Victim", email_verified=True)
    client.cookies.set("jobmate_oauth_state", "state")
    r = client.get("/api/v1/auth/google/callback?code=c&state=state", follow_redirects=False)
    assert r.status_code == 302
    assert "auth_error=google_account_conflict" in r.headers["location"]
    assert "jobmate_session" not in r.headers.get("set-cookie", "")
    user = store.get_user_by_email("victim@example.com")
    assert user["google_sub"] in ("", None)  # sub 미연결
    # 별도 계정도 생성되지 않음
    assert len(store.list_users()) == 1


def test_google_callback_unverified_account_claimed(clean_oauth_env):
    """미인증 비밀번호 계정은 Google 소유권 증명으로 클레임 — 사전가입 DoS 방지.

    공격자가 피해자 이메일로 미인증 비밀번호 가입을 심어둬도, 피해자의
    Google 로그인(email_verified=True)이 오면 공격자 해시를 비우고 Google
    sub를 연결한다 — 그렇지 않으면 cred[1] 존재만으로 영구 차단된다."""
    clean_oauth_env.setenv("GOOGLE_OAUTH_CLIENT_ID", "cid")
    clean_oauth_env.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "sec")
    _mock_google(clean_oauth_env, sub="gsub-victim", email="victim@example.com")
    client, store = _client()
    store.create_user("victim@example.com", "scrypt$attacker-hash", "Victim")
    client.cookies.set("jobmate_oauth_state", "state")
    r = client.get("/api/v1/auth/google/callback?code=c&state=state", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"].endswith("/")
    user = store.get_user_by_email("victim@example.com")
    assert user is not None
    assert user["google_sub"] == "gsub-victim"
    assert user["email_verified"] is True
    # 공격자가 심어둔 비밀번호 해시가 비워졌는지 확인
    cred = store.get_user_password_hash("victim@example.com")
    assert cred is not None and cred[1] == ""
    assert len(store.list_users()) == 1


def test_google_callback_state_mismatch(clean_oauth_env):
    clean_oauth_env.setenv("GOOGLE_OAUTH_CLIENT_ID", "cid")
    clean_oauth_env.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "sec")
    _mock_google(clean_oauth_env)
    client, store = _client()
    client.cookies.set("jobmate_oauth_state", "expected-state")
    r = client.get("/api/v1/auth/google/callback?code=c&state=wrong", follow_redirects=False)
    assert r.status_code == 400
    assert store.get_user_by_email("user@example.com") is None
    assert "jobmate_session" not in r.headers.get("set-cookie", "")


def test_google_callback_legacy_env_names(clean_oauth_env):
    """레거시 GOOGLE_CLIENT_ID/SECRET도 그대로 동작."""
    clean_oauth_env.setenv("GOOGLE_CLIENT_ID", "legacy-cid")
    clean_oauth_env.setenv("GOOGLE_CLIENT_SECRET", "legacy-sec")
    _mock_google(clean_oauth_env)
    client, store = _client()
    client.cookies.set("jobmate_oauth_state", "state")
    r = client.get("/api/v1/auth/google/callback?code=c&state=state", follow_redirects=False)
    assert r.status_code == 302
    assert store.get_user_by_email("user@example.com") is not None


def test_google_login_admin_email_role(clean_oauth_env):
    clean_oauth_env.setenv("GOOGLE_OAUTH_CLIENT_ID", "cid")
    clean_oauth_env.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "sec")
    clean_oauth_env.setenv("JOBMATE_ADMIN_EMAILS", "user@example.com")
    _mock_google(clean_oauth_env, email="user@example.com")
    client, _ = _client()
    client.cookies.set("jobmate_oauth_state", "state")
    client.get("/api/v1/auth/google/callback?code=c&state=state", follow_redirects=False)
    me = client.get("/api/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["role"] == "admin"
