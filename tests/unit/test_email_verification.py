"""이메일 인증 테스트 — 가입→미인증 로그인 차단→토큰 인증→로그인 성공,
재발송 rate limit, mailer 미설정 graceful."""

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from jobmate import mailer
from jobmate.api.app import create_app
from jobmate.config import Settings
from jobmate.security import hash_token
from jobmate.store.db import Store
from tests.integration.helpers import default_outputs, scripted_llm


def _client(monkeypatch=None, require_verify=True):
    """require_verify=True면 JOBMATE_API_KEY를 설정해 인증 모드를 활성화
    (로컬 개발 모드는 email_verified 검사를 건너뛴다)."""
    if require_verify and monkeypatch is not None:
        monkeypatch.setenv("JOBMATE_API_KEY", "test-api-key")
    db = str(Path(tempfile.mkdtemp()) / "t.db")
    settings = Settings(db_path=db, budget_max_tokens=100_000)
    store = Store(db)
    app = create_app(settings=settings, store=store, llm=scripted_llm(default_outputs()))
    return TestClient(app), store


@pytest.fixture
def sent_mails(monkeypatch):
    """mailer.send_verification_email을 가로채 발송 URL을 수집."""
    mails: list[tuple[str, str]] = []

    def fake_send(to: str, verify_url: str, gmail=None) -> bool:
        mails.append((to, verify_url))
        return True

    monkeypatch.setattr(mailer, "send_verification_email", fake_send)
    return mails


_PW = "password123"


def _signup(client, email="v@b.com", password=_PW):
    return client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": password, "display_name": "V"},
    )


def _login(client, email="v@b.com", password=_PW):
    return client.post("/api/v1/auth/login", json={"email": email, "password": password})


def test_signup_verify_login_flow(monkeypatch, sent_mails):
    client, store = _client(monkeypatch)
    r = _signup(client)
    assert r.status_code == 201
    body = r.json()
    assert body["verify_email_sent"] is True
    assert body["email_verification_pending"] is True
    # 인증 전에는 세션 쿠키가 발급되지 않는다
    assert "jobmate_session" not in r.headers.get("set-cookie", "")
    assert client.get("/api/v1/auth/me").status_code == 401

    # 미인증 상태로는 로그인 불가
    bad = _login(client)
    assert bad.status_code == 403
    assert bad.json()["error"]["code"] == "email_not_verified"

    # 발송된 URL에서 토큰 추출 → 인증 링크 호출
    assert len(sent_mails) == 1
    to, url = sent_mails[0]
    assert to == "v@b.com"
    token = parse_qs(urlparse(url).query)["token"][0]

    vr = client.get(f"/api/v1/auth/verify?token={token}")
    assert vr.status_code == 200
    assert "이메일 인증 완료" in vr.text
    assert "jobmate_session" in vr.headers.get("set-cookie", "")

    user = store.get_user_by_email("v@b.com")
    assert user is not None and user["email_verified"] is True

    # 인증 후 로그인 성공
    ok = _login(client)
    assert ok.status_code == 200
    assert ok.json()["user"]["email"] == "v@b.com"

    # 사용된 토큰 재사용 불가
    again = client.get(f"/api/v1/auth/verify?token={token}")
    assert again.status_code == 400


def test_verify_invalid_token(monkeypatch):
    client, _ = _client(monkeypatch)
    r = client.get("/api/v1/auth/verify?token=bogus-token")
    assert r.status_code == 400
    assert "인증 실패" in r.text


def test_verify_token_hash_only_in_db(monkeypatch, sent_mails):
    """DB에는 토큰 원문이 아니라 SHA-256 해시만 저장된다."""
    client, store = _client(monkeypatch)
    _signup(client)
    token = parse_qs(urlparse(sent_mails[0][1]).query)["token"][0]
    assert token not in str(store._conn.execute("SELECT * FROM email_verifications").fetchall())
    assert hash_token(token) in {
        r["token_hash"]
        for r in store._conn.execute("SELECT token_hash FROM email_verifications").fetchall()
    }


def test_resend_rate_limit(monkeypatch, sent_mails):
    client, store = _client(monkeypatch)
    _signup(client, email="r@b.com")
    assert len(sent_mails) == 1

    # 가입 직후 재발송 → rate limit (10분에 1회)
    r = client.post("/api/v1/auth/verify/resend", json={"email": "r@b.com"})
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "resend_too_soon"
    assert len(sent_mails) == 1

    # 마지막 발급 시각을 11분 전으로 되돌리면 재발송 가능
    old = (datetime.now(UTC) - timedelta(minutes=11)).isoformat()
    with store.tx() as c:
        c.execute(
            "UPDATE email_verifications SET created_at=? WHERE email=?",
            (old, "r@b.com"),
        )
    r2 = client.post("/api/v1/auth/verify/resend", json={"email": "r@b.com"})
    assert r2.status_code == 200
    assert r2.json()["sent"] is True
    assert len(sent_mails) == 2


def test_resend_unknown_or_verified_email(monkeypatch, sent_mails):
    client, _ = _client(monkeypatch)
    # 미가입 이메일 — 계정 열거 방지를 위해 동일 형태의 응답
    r = client.post("/api/v1/auth/verify/resend", json={"email": "nobody@b.com"})
    assert r.status_code == 200
    assert r.json() == {"sent": False}
    assert sent_mails == []

    # 잘못된 이메일 형식
    bad = client.post("/api/v1/auth/verify/resend", json={"email": "not-an-email"})
    assert bad.status_code == 400


def test_signup_mail_failure_still_succeeds(monkeypatch):
    """SMTP 미설정 시 발송 실패여도 가입은 성공 — verify_email_sent=False."""
    client, _ = _client(monkeypatch)
    # JOBMATE_SMTP_HOST 미설정 → mailer가 False 반환
    r = _signup(client, email="f@b.com")
    assert r.status_code == 201
    assert r.json()["verify_email_sent"] is False
    assert r.json()["email_verification_pending"] is True


def test_mailer_unconfigured_returns_false(monkeypatch):
    monkeypatch.delenv("JOBMATE_SMTP_HOST", raising=False)
    assert mailer.send_verification_email("a@b.com", "http://x/verify?token=t") is False


def test_mailer_gmail_backend(monkeypatch):
    """SMTP 미설정 + gmail sender 주입 → Gmail API로 발송."""
    monkeypatch.delenv("JOBMATE_SMTP_HOST", raising=False)
    sent = []

    class FakeGmail:
        def send_message(self, to, subject, body):
            sent.append((to, subject, body))
            return "msg-1"

    assert (
        mailer.send_verification_email("a@b.com", "http://x/verify?token=t", gmail=FakeGmail())
        is True
    )
    assert sent and sent[0][0] == "a@b.com" and "verify?token=t" in sent[0][2]


def test_mailer_gmail_failure_returns_false(monkeypatch):
    monkeypatch.delenv("JOBMATE_SMTP_HOST", raising=False)

    class BoomGmail:
        def send_message(self, to, subject, body):
            raise RuntimeError("gmail api down")

    assert (
        mailer.send_verification_email("a@b.com", "http://x/verify?token=t", gmail=BoomGmail())
        is False
    )


def test_mailer_smtp_takes_priority_over_gmail(monkeypatch):
    """SMTP_HOST 설정 시 gmail 백엔드보다 SMTP 우선 — gmail은 호출되지 않음."""
    monkeypatch.setenv("JOBMATE_SMTP_HOST", "smtp.invalid")
    called = []

    class FakeGmail:
        def send_message(self, to, subject, body):
            called.append(to)

    # SMTP 연결 실패로 False지만 gmail fallback은 시도하지 않음
    assert (
        mailer.send_verification_email("a@b.com", "http://x/verify?token=t", gmail=FakeGmail())
        is False
    )
    assert called == []


def test_delete_expired_verifications():
    """만료·사용 완료 토큰 정리 — 유효 미사용 행은 유지."""
    db = str(Path(tempfile.mkdtemp()) / "t.db")
    store = Store(db)

    def _count() -> int:
        return store._conn.execute("SELECT COUNT(*) AS c FROM email_verifications").fetchone()["c"]

    # 만료 행 생성
    tok_exp = store.create_email_verification("exp@b.com", ttl_hours=24)
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    with store.tx() as c:
        c.execute(
            "UPDATE email_verifications SET expires_at=? WHERE token_hash=?",
            (past, hash_token(tok_exp)),
        )
    # 사용 완료 행 생성
    tok_used = store.create_email_verification("used@b.com")
    store.mark_verification_used(hash_token(tok_used))
    # 유효 행 생성 — 생성 시점 opportunistic 삭제로 exp/used 행이 이미 지워진다
    store.create_email_verification("ok@b.com")
    assert _count() == 1

    # 명시 호출 — 유효 행만 남아 삭제 0
    assert store.delete_expired_verifications() == 0
    assert _count() == 1

    # now 파라미터로 미래 기준 삭제도 가능
    future = datetime.now(UTC) + timedelta(hours=48)
    assert store.delete_expired_verifications(now=future) == 1
    assert _count() == 0


def test_create_verification_opportunistic_cleanup():
    """create_email_verification 호출 시 만료/사용 행이 함께 삭제된다."""
    db = str(Path(tempfile.mkdtemp()) / "t.db")
    store = Store(db)
    tok = store.create_email_verification("old@b.com")
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    with store.tx() as c:
        c.execute(
            "UPDATE email_verifications SET expires_at=? WHERE token_hash=?",
            (past, hash_token(tok)),
        )
    store.create_email_verification("new@b.com")
    rows = store._conn.execute("SELECT email FROM email_verifications").fetchall()
    assert [r["email"] for r in rows] == ["new@b.com"]


def test_signup_ip_throttle(monkeypatch, sent_mails):
    """같은 IP에서 10분에 5회까지만 가입 가능 — 6회째 429."""
    client, store = _client(monkeypatch)
    for i in range(5):
        r = _signup(client, email=f"u{i}@b.com")
        assert r.status_code == 201, i
    r = _signup(client, email="u5@b.com")
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "too_many_attempts"
    actions = [a["action"] for a in store.list_audit()]
    assert "auth_signup" in actions
    assert "auth_signup_throttled" in actions


def test_auth_audit_events(monkeypatch, sent_mails):
    """verify 성공/실패·resend·signup이 감사 로그에 기록되고 토큰 원문은 없다."""
    client, store = _client(monkeypatch)
    _signup(client, email="aud@b.com")
    token = parse_qs(urlparse(sent_mails[0][1]).query)["token"][0]

    # 실패 토큰 → auth_email_verify_failed
    client.get("/api/v1/auth/verify?token=bogus-token")
    # 성공 토큰 → auth_email_verified
    client.get(f"/api/v1/auth/verify?token={token}")

    # 미인증 사용자 resend — 직후라 throttle, 감사 기록 확인
    _signup(client, email="aud2@b.com")
    client.post("/api/v1/auth/verify/resend", json={"email": "aud2@b.com"})

    actions = {a["action"] for a in store.list_audit()}
    assert {
        "auth_signup",
        "auth_email_verified",
        "auth_email_verify_failed",
        "auth_verify_resend_throttled",
    } <= actions
    # 토큰 원문·비밀번호가 감사 detail에 기록되지 않는다
    import json as _json

    for a in store.list_audit():
        blob = _json.dumps(a["detail"])
        assert token not in blob and _PW not in blob


def test_local_dev_mode_skips_verification():
    """인증 비활성 로컬 모드(키·허용목록·VERCEL 없음)에서는 인증 없이 로그인."""
    client, _ = _client(require_verify=False)
    r = _signup(client, email="local@b.com")
    assert r.status_code == 201
    assert r.json()["email_verification_pending"] is False
    assert "jobmate_session" in r.headers.get("set-cookie", "")
    client.cookies.clear()
    assert _login(client, email="local@b.com").status_code == 200


def test_mailer_local_log_masks_verification_token(monkeypatch, caplog):
    """로컬 개발 로그의 인증 URL에서 token 등 민감 쿼리 값이 마스킹된다
    (로그에 토큰 원문 금지 원칙 — CLAUDE.md)."""
    import logging

    monkeypatch.delenv("JOBMATE_SMTP_HOST", raising=False)
    monkeypatch.delenv("VERCEL", raising=False)
    monkeypatch.delenv("JOBMATE_SMTP_USER", raising=False)
    with caplog.at_level(logging.INFO, logger="jobmate.mailer"):
        assert (
            mailer.send_verification_email(
                "a@b.com",
                "http://localhost:8000/api/v1/auth/verify?token=SECRET_TOKEN_123&next=/x",
            )
            is False
        )
    assert "SECRET_TOKEN_123" not in caplog.text
    assert "token=***" in caplog.text
    # 도메인+경로는 남아 로컬 개발 편의 유지
    assert "localhost:8000/api/v1/auth/verify" in caplog.text
