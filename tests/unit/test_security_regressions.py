"""Regression coverage for the security audit; external calls are replaced with fakes."""

import httpx
import pytest
from fastapi.testclient import TestClient

import jobmate.adapters.push as push_mod
from jobmate.api.app import create_app
from jobmate.config import Settings
from jobmate.store.db import Store
from jobmate.tools import ToolDeniedError, ToolExecutor


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("JOBMATE_ALLOWED_EMAILS", raising=False)
    monkeypatch.delenv("VERCEL", raising=False)
    store = Store(str(tmp_path / "audit.db"))
    app = create_app(
        settings=Settings(db_path=tmp_path / "audit.db"), store=store, llm=None, adapters={}
    )
    yield TestClient(app), store
    store.close()


def test_signup_is_open_when_no_allowlist(client, monkeypatch):
    monkeypatch.setenv("JOBMATE_API_KEY", "admin-key")
    c, store = client
    r = c.post(
        "/api/v1/auth/signup",
        json={"email": "owner@example.com", "password": "password123", "agree_terms": True},
    )
    assert r.status_code == 201
    assert store.get_user_by_email("owner@example.com") is not None


def test_public_signup_without_admin_key_does_not_grant_admin(client, monkeypatch):
    """보호 모드(JOBMATE_API_KEY만) + 허용목록 없음 + 가입자가 admin 키로 프로비저닝 받지
    않은 경우 → 첫 사용자도 member로 승격되지 않는다(H-1). admin이 필요하면
    JOBMATE_ALLOWED_EMAILS 설정 또는 관리자 API 키 프로비저닝으로만 부여."""
    monkeypatch.setenv("JOBMATE_API_KEY", "admin-key")
    c, store = client
    r = c.post(
        "/api/v1/auth/signup",
        json={"email": "attacker@example.com", "password": "password123", "agree_terms": True},
    )
    assert r.status_code == 201
    token = store.create_email_verification("attacker@example.com")
    assert c.get(f"/api/v1/auth/verify?token={token}").status_code == 200
    me = c.get("/api/v1/auth/me").json()["user"]
    assert me["role"] == "member"


def test_admin_key_provisioned_first_signup_grants_admin(client, monkeypatch):
    """관리자 API 키로 프로비저닝한 첫 가입자만 자동 admin 승격된다."""
    monkeypatch.setenv("JOBMATE_API_KEY", "admin-key")
    c, store = client
    r = c.post(
        "/api/v1/auth/signup",
        json={"email": "owner@example.com", "password": "password123", "agree_terms": True},
        headers={"X-API-Key": "admin-key"},
    )
    assert r.status_code == 201
    assert r.json()["user"]["role"] == "admin"


def test_allowed_email_cannot_be_claimed_without_admin_key(client, monkeypatch):
    monkeypatch.setenv("JOBMATE_ALLOWED_EMAILS", "owner@example.com")
    c, _ = client
    assert (
        c.post(
            "/api/v1/auth/signup",
            json={"email": "owner@example.com", "password": "password123", "agree_terms": True},
        ).status_code
        == 403
    )


def test_removed_email_loses_session_access(client, monkeypatch):
    c, _ = client
    assert (
        c.post(
            "/api/v1/auth/signup",
            json={"email": "old@example.com", "password": "password123", "agree_terms": True},
        ).status_code
        == 201
    )
    monkeypatch.setenv("JOBMATE_API_KEY", "admin-key")
    monkeypatch.setenv("JOBMATE_ALLOWED_EMAILS", "new@example.com")
    assert c.get("/api/v1/settings").status_code == 401
    assert c.get("/api/v1/auth/me").status_code == 401


def test_vercel_without_api_key_keeps_machine_api_closed(client, monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    c, _ = client
    assert c.get("/api/v1/settings").status_code == 401
    assert (
        c.post(
            "/api/v1/auth/signup",
            json={"email": "any@example.com", "password": "password123", "agree_terms": True},
        ).status_code
        == 201
    )


@pytest.mark.parametrize("method", ["get", "post"])
def test_non_vercel_cron_requires_secret(client, monkeypatch, method):
    monkeypatch.setenv("CRON_SECRET", "cron-key")
    c, _ = client
    assert getattr(c, method)("/api/v1/cron/tick").status_code == 401


@pytest.mark.parametrize("verified", [False, None, "true"])
def test_google_requires_verified_email(client, monkeypatch, verified):
    c, store = client
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret")
    monkeypatch.setenv("JOBMATE_ALLOWED_EMAILS", "owner@example.com")
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *a, **kw: httpx.Response(
            200,
            json={"access_token": "token"},
            request=httpx.Request("POST", "https://oauth2.googleapis.com/token"),
        ),
    )
    monkeypatch.setattr(
        httpx,
        "get",
        lambda *a, **kw: httpx.Response(
            200,
            json={"sub": "google-sub", "email": "owner@example.com", "email_verified": verified},
            request=httpx.Request("GET", "https://openidconnect.googleapis.com/v1/userinfo"),
        ),
    )
    c.cookies.set("jobmate_oauth_state", "state")
    r = c.get("/api/v1/auth/google/callback?code=code&state=state", follow_redirects=False)
    assert "jobmate_session" not in r.headers.get("set-cookie", "")
    assert store.get_user_by_email("owner@example.com") is None


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://127.0.0.1/x",
        "https://127.0.0.1/x",
        "https://evil.example/x",
        "https://fcm.googleapis.com.evil.example/x",
        "https://fcm.googleapis.com@127.0.0.1/x",
        "https://fcm.googleapis.com:8443/x",
    ],
)
def test_push_rejects_untrusted_endpoint(client, endpoint):
    c, store = client
    r = c.post(
        "/api/v1/push/subscribe",
        json={"endpoint": endpoint, "keys": {"p256dh": "pk", "auth": "ak"}},
    )
    assert r.status_code == 422
    assert store.list_push_subscriptions() == []


def test_legacy_push_cannot_send_to_internal_host(client, monkeypatch):
    _, store = client
    store.save_push_subscription("https://127.0.0.1/private", {"p256dh": "pk", "auth": "ak"})
    calls = []
    monkeypatch.setattr(push_mod, "webpush", lambda **kw: calls.append(kw))
    push_mod.WebPushNotifier(store, private_key="key").send(title="t", body="b", source_url="/")
    assert calls == []


def test_read_file_cannot_escape_to_sibling_prefix(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    sibling = tmp_path / "repo-private"
    sibling.mkdir()
    (sibling / "secret.txt").write_text("secret", encoding="utf-8")
    executor = ToolExecutor(repo_root=str(root))
    with pytest.raises(ToolDeniedError):
        executor.call("read_file", ["read_file"], path="../repo-private/secret.txt")


def test_read_file_resolves_symlinks(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("secret", encoding="utf-8")
    try:
        (root / "link.txt").symlink_to(secret)
    except OSError:
        pytest.skip("symlinks require Windows developer mode or administrator rights")
    with pytest.raises(ToolDeniedError):
        ToolExecutor(repo_root=str(root)).call("read_file", ["read_file"], path="link.txt")


def test_read_file_still_reads_inside_repo(tmp_path):
    (tmp_path / "ok.txt").write_text("ok", encoding="utf-8")
    assert (
        ToolExecutor(repo_root=str(tmp_path)).call("read_file", ["read_file"], path="ok.txt")
        == "ok"
    )


@pytest.mark.parametrize(
    "host",
    [
        "fcm.googleapis.com",
        "updates.push.services.mozilla.com",
        "web.push.apple.com",
        "wns1.notify.windows.com",
    ],
)
def test_browser_push_providers_are_accepted(client, host):
    c, store = client
    endpoint = f"https://{host}/push/token"
    r = c.post(
        "/api/v1/push/subscribe",
        json={"endpoint": endpoint, "keys": {"p256dh": "pk", "auth": "ak"}},
    )
    assert r.status_code == 200
    assert store.list_push_subscriptions()[0]["endpoint"] == endpoint


@pytest.mark.parametrize("keys", [None, [], "bad", {"p256dh": [], "auth": "a"}, {"p256dh": "p"}])
def test_push_key_shape_is_checked(client, keys):
    c, _ = client
    assert (
        c.post(
            "/api/v1/push/subscribe",
            json={"endpoint": "https://fcm.googleapis.com/push/t", "keys": keys},
        ).status_code
        == 422
    )


def test_push_disables_redirects_and_limits_timeout(client, monkeypatch):
    _, store = client
    store.save_push_subscription(
        "https://fcm.googleapis.com/push/t", {"p256dh": "pk", "auth": "ak"}
    )
    calls = []
    monkeypatch.setattr(push_mod, "webpush", lambda **kw: calls.append(kw))
    push_mod.WebPushNotifier(store, private_key="key").send(title="t", body="b", source_url="/")
    assert calls[0]["requests_session"].max_redirects == 0
    assert calls[0]["timeout"] == 10


@pytest.mark.parametrize(
    "existing_sub,allowed,verified,expected",
    [
        ("", "owner@example.com", True, True),
        ("other-sub", "owner@example.com", True, False),
        ("", "", True, True),
        ("", "other@example.com", True, False),
    ],
)
def test_google_account_access_policy(
    client, monkeypatch, existing_sub, allowed, verified, expected
):
    c, store = client
    monkeypatch.setenv("JOBMATE_API_KEY", "admin-key")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret")
    monkeypatch.setenv("JOBMATE_ALLOWED_EMAILS", allowed)
    if existing_sub:
        store.create_user("owner@example.com", "", "Owner", existing_sub)
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *a, **kw: httpx.Response(
            200,
            json={"access_token": "token"},
            request=httpx.Request("POST", "https://oauth2.googleapis.com/token"),
        ),
    )
    monkeypatch.setattr(
        httpx,
        "get",
        lambda *a, **kw: httpx.Response(
            200,
            json={"sub": "google-sub", "email": "owner@example.com", "email_verified": verified},
            request=httpx.Request("GET", "https://openidconnect.googleapis.com/v1/userinfo"),
        ),
    )
    c.cookies.set("jobmate_oauth_state", "state")
    r = c.get("/api/v1/auth/google/callback?code=code&state=state", follow_redirects=False)
    assert ("jobmate_session" in r.headers.get("set-cookie", "")) is expected


def test_allowlist_alone_protects_api(client, monkeypatch):
    monkeypatch.setenv("JOBMATE_ALLOWED_EMAILS", "owner@example.com")
    c, _ = client
    assert c.get("/api/v1/settings").status_code == 401


def test_concurrent_store_reads_keep_user_identity(client):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    _, store = client
    emails = [f"reader{i}@example.com" for i in range(16)]
    for email in emails:
        store.create_user(email, "", email)
    barrier = Barrier(len(emails))

    def read_user(email):
        barrier.wait(timeout=10)
        for _ in range(200):
            user = store.get_user_by_email(email)
            assert user is not None and user["email"] == email

    with ThreadPoolExecutor(max_workers=len(emails)) as pool:
        list(pool.map(read_user, emails))
