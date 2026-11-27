"""비밀번호 재설정 테스트 — forgot→메일 링크→reset→세션 폐기,
토큰 1회성/만료, 재발송 throttle, 원격 오픈 모드 차단."""

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from jobmate import mailer
from jobmate.api.app import create_app
from jobmate.config import Settings
from jobmate.security import hash_password, hash_token
from jobmate.store.db import Store
from tests.integration.helpers import default_outputs, scripted_llm


def _client(monkeypatch=None):
    """인증 모드(JOBMATE_API_KEY)로 앱 기동 — 이메일 인증 강제 경로와 동일."""
    monkeypatch.setenv("JOBMATE_API_KEY", "test-api-key")
    db = str(Path(tempfile.mkdtemp()) / "t.db")
    settings = Settings(db_path=db, budget_max_tokens=100_000)
    store = Store(db)
    app = create_app(settings=settings, store=store, llm=scripted_llm(default_outputs()))
    return TestClient(app), store


@pytest.fixture
def sent_resets(monkeypatch):
    """mailer.send_password_reset_email을 가로채 발송 URL을 수집."""
    mails: list[tuple[str, str]] = []

    def fake_send(to: str, reset_url: str, gmail=None) -> bool:
        mails.append((to, reset_url))
        return True

    monkeypatch.setattr(mailer, "send_password_reset_email", fake_send)
    return mails


_PW = "password123"
_NEW_PW = "newpassword456"


def _mkuser(store, email="r@b.com", password=_PW, verified=True):
    return store.create_user(email, hash_password(password), "R", "", verified)


def _login(client, email="r@b.com", password=_PW):
    return client.post("/api/v1/auth/login", json={"email": email, "password": password})


def _reset_token_from(mails, email="r@b.com") -> str:
    for to, url in mails:
        if to == email:
            return parse_qs(urlparse(url).query)["token"][0]
    raise AssertionError("reset mail not sent")


def test_forgot_sends_reset_email(monkeypatch, sent_resets):
    client, store = _client(monkeypatch)
    _mkuser(store)
    r = client.post("/api/v1/auth/password/forgot", json={"email": "r@b.com"})
    assert r.status_code == 200
    assert r.json()["sent"] is True
    assert len(sent_resets) == 1
    to, url = sent_resets[0]
    assert to == "r@b.com"
    assert "/api/v1/auth/password/reset" in url
    assert "token=" in url
    store.close()


def test_forgot_unknown_email_same_response(monkeypatch, sent_resets):
    """미가입 이메일에도 동일 응답 — 계정 열거 방지."""
    client, store = _client(monkeypatch)
    r = client.post("/api/v1/auth/password/forgot", json={"email": "nobody@b.com"})
    assert r.status_code == 200
    assert r.json()["sent"] is True
    assert sent_resets == []
    store.close()


def test_forgot_google_only_no_mail(monkeypatch, sent_resets):
    """비밀번호 없는(Google-only) 계정은 발송하지 않지만 응답은 동일."""
    client, store = _client(monkeypatch)
    store.create_user("g@b.com", "", "G", "sub123", True)
    r = client.post("/api/v1/auth/password/forgot", json={"email": "g@b.com"})
    assert r.status_code == 200
    assert r.json()["sent"] is True
    assert sent_resets == []
    store.close()


def test_reset_changes_password_and_revokes_sessions(monkeypatch, sent_resets):
    client, store = _client(monkeypatch)
    _mkuser(store)
    # 기존 세션 확보
    assert _login(client).status_code == 200
    assert client.get("/api/v1/auth/me").status_code == 200

    client.post("/api/v1/auth/password/forgot", json={"email": "r@b.com"})
    token = _reset_token_from(sent_resets)
    r = client.post(
        "/api/v1/auth/password/reset",
        json={"token": token, "new_password": _NEW_PW},
    )
    assert r.status_code == 200

    # 구 세션 전부 폐기 — 기존 쿠키로 인증 API 호출 시 401
    assert client.get("/api/v1/auth/me").status_code == 401
    # 구 비밀번호 불가, 신 비밀번호로만 로그인 가능
    assert _login(client).status_code == 401
    assert _login(client, password=_NEW_PW).status_code == 200
    store.close()


def test_reset_token_single_use(monkeypatch, sent_resets):
    client, store = _client(monkeypatch)
    _mkuser(store)
    client.post("/api/v1/auth/password/forgot", json={"email": "r@b.com"})
    token = _reset_token_from(sent_resets)
    assert (
        client.post(
            "/api/v1/auth/password/reset", json={"token": token, "new_password": _NEW_PW}
        ).status_code
        == 200
    )
    r = client.post(
        "/api/v1/auth/password/reset", json={"token": token, "new_password": "other12345"}
    )
    assert r.status_code in (400, 410)
    # 두 번째 시도는 비밀번호를 바꾸지 않는다
    assert _login(client, password=_NEW_PW).status_code == 200
    store.close()


def test_reset_expired_token_fails(monkeypatch, sent_resets):
    client, store = _client(monkeypatch)
    _mkuser(store)
    client.post("/api/v1/auth/password/forgot", json={"email": "r@b.com"})
    token = _reset_token_from(sent_resets)
    # 토큰을 과거로 이동시켜 만료시킨다
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    with store.tx() as c:
        c.execute(
            "UPDATE password_reset_tokens SET expires_at=? WHERE token_hash=?",
            (past, hash_token(token)),
        )
    r = client.post("/api/v1/auth/password/reset", json={"token": token, "new_password": _NEW_PW})
    assert r.status_code in (400, 410)
    assert _login(client).status_code == 200  # 비밀번호 그대로
    store.close()


def test_reset_invalid_token_fails(monkeypatch):
    client, store = _client(monkeypatch)
    _mkuser(store)
    r = client.post(
        "/api/v1/auth/password/reset",
        json={"token": "bogus-token", "new_password": _NEW_PW},
    )
    assert r.status_code in (400, 410)
    store.close()


def test_forgot_throttled_within_interval(monkeypatch, sent_resets):
    """같은 이메일 재요청은 10분에 1회 발송만 — 두 번째도 동일 응답(계정 열거 방지)."""
    client, store = _client(monkeypatch)
    _mkuser(store)
    r1 = client.post("/api/v1/auth/password/forgot", json={"email": "r@b.com"})
    assert r1.status_code == 200
    assert r1.json()["sent"] is True
    r2 = client.post("/api/v1/auth/password/forgot", json={"email": "r@b.com"})
    # 429가 아니라 동일 응답 — 상태 코드 차이로 계정 존재가 노출되면 안 된다
    assert r2.status_code == 200
    assert r2.json()["sent"] is True
    assert len(sent_resets) == 1
    store.close()


def test_forgot_failed_send_does_not_burn_throttle(monkeypatch):
    """메일 발송 실패 시 토큰 행이 삭제돼 재시도가 throttle에 막히지 않는다."""
    calls: list[str] = []

    def flaky_send(to: str, reset_url: str, gmail=None) -> bool:
        calls.append(to)
        return len(calls) > 1  # 첫 발송 실패, 이후 성공

    monkeypatch.setattr(mailer, "send_password_reset_email", flaky_send)
    client, store = _client(monkeypatch)
    _mkuser(store)
    assert client.post("/api/v1/auth/password/forgot", json={"email": "r@b.com"}).status_code == 200
    # 실패로 토큰 행 삭제 → 즉시 재요청해도 새 토큰 발급 + 재발송 시도
    assert client.post("/api/v1/auth/password/forgot", json={"email": "r@b.com"}).status_code == 200
    assert len(calls) == 2
    store.close()


def test_reset_weak_password_rejected(monkeypatch, sent_resets):
    client, store = _client(monkeypatch)
    _mkuser(store)
    client.post("/api/v1/auth/password/forgot", json={"email": "r@b.com"})
    token = _reset_token_from(sent_resets)
    r = client.post("/api/v1/auth/password/reset", json={"token": token, "new_password": "short"})
    assert r.status_code == 400
    # 토큰은 소비되지 않아야 한다(비밀번호 검증 실패가 consume보다 먼저)
    r2 = client.post("/api/v1/auth/password/reset", json={"token": token, "new_password": _NEW_PW})
    assert r2.status_code == 200
    store.close()


def test_reset_page_returns_form(monkeypatch, sent_resets):
    """GET reset 링크는 비밀번호 입력 폼 HTML을 반환한다(토큰 미소비)."""
    client, store = _client(monkeypatch)
    _mkuser(store)
    client.post("/api/v1/auth/password/forgot", json={"email": "r@b.com"})
    token = _reset_token_from(sent_resets)
    r = client.get(f"/api/v1/auth/password/reset?token={token}")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    # GET은 토큰을 소비하지 않는다 — 이후 POST가 성공해야 한다
    r2 = client.post("/api/v1/auth/password/reset", json={"token": token, "new_password": _NEW_PW})
    assert r2.status_code == 200
    store.close()


def test_remote_forgot_reset_denied_in_open_mode(monkeypatch):
    """무키 원격 오픈 모드에서는 재설정 발급·소비 경로가 차단된다."""
    for v in ("JOBMATE_API_KEY", "JOBMATE_ALLOWED_EMAILS", "VERCEL"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr("jobmate.security.is_loopback_request", lambda request: False)
    db = str(Path(tempfile.mkdtemp()) / "t.db")
    settings = Settings(db_path=db, budget_max_tokens=100_000)
    store = Store(db)
    app = create_app(settings=settings, store=store, llm=scripted_llm(default_outputs()))
    client = TestClient(app)
    store.create_user("r@b.com", hash_password(_PW), "R", "", True)

    r = client.post("/api/v1/auth/password/forgot", json={"email": "r@b.com"})
    assert r.status_code == 403
    r = client.post("/api/v1/auth/password/reset", json={"token": "x", "new_password": _NEW_PW})
    assert r.status_code == 403
    r = client.get("/api/v1/auth/password/reset?token=x")
    assert r.status_code == 403
    store.close()
