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
    clean_oauth_env.setenv("GOOGLE_OAUTH_CLIENT_ID", "cid")
    clean_oauth_env.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "sec")
    _mock_google(clean_oauth_env, sub="gsub-9", email="existing@example.com")
    client, store = _client()
    store.create_user("existing@example.com", "scrypt$hash", "Existing")
    before = len(store.list_users())
    client.cookies.set("jobmate_oauth_state", "state")
    r = client.get("/api/v1/auth/google/callback?code=c&state=state", follow_redirects=False)
    assert r.status_code == 302
    assert len(store.list_users()) == before
    user = store.get_user_by_email("existing@example.com")
    assert user["google_sub"] == "gsub-9"  # 기존 계정에 sub 연결


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
