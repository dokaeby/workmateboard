"""MFA/TOTP 테스트 — RFC 6238 벡터, setup→confirm→login 챌린지→verify,
백업 코드 1회성, 리플레이 거부, disable/재발급, 세션 폐기."""

import tempfile
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from jobmate.api.app import create_app
from jobmate.config import Settings
from jobmate.security import hash_password, totp_code
from jobmate.store.db import Store
from tests.integration.helpers import default_outputs, scripted_llm

_PW = "Str0ng#Passw0rd!"


def _client(monkeypatch=None):
    monkeypatch.setenv("JOBMATE_API_KEY", "test-api-key")
    # 환경 변수 누수 방지 — sibling 인증 테스트와 동일한 격리 수준
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


def _mkuser(store, email="m@b.com", password=_PW):
    return store.create_user(email, hash_password(password), "M", "", True)


def _login(client, email="m@b.com", password=_PW):
    return client.post("/api/v1/auth/login", json={"email": email, "password": password})


def _enable_mfa(client):
    """로그인 → setup → confirm까지 수행. (secret, backup_codes) 반환.
    confirm이 전체 세션을 폐기하므로 이후 요청은 재로그인 필요."""
    assert _login(client).status_code == 200
    r = client.post("/api/v1/auth/mfa/setup")
    assert r.status_code == 200
    secret = r.json()["secret"]
    assert "otpauth://totp/" in r.json()["otpauth_uri"]
    # otpauth URI 파라미터 검증 — 실제 인증 앱이 파싱하는 필수 값
    from urllib.parse import parse_qs, urlparse

    uri = urlparse(r.json()["otpauth_uri"])
    params = parse_qs(uri.query)
    assert params["secret"] == [secret]
    assert params["issuer"] == ["jobMate"]
    r2 = client.post("/api/v1/auth/mfa/confirm", json={"code": totp_code(secret)})
    assert r2.status_code == 200
    return secret, r2.json()["backup_codes"]


def _mfa_ticket(client):
    """비밀번호 로그인 → mfa_ticket 획득."""
    r = _login(client)
    assert r.status_code == 200
    assert r.json()["mfa_required"] is True
    return r.json()["mfa_ticket"]


# ---- TOTP 헬퍼 (RFC 6238 테스트 벡터) ----


def test_totp_rfc6238_vectors():
    # RFC 6238 부록 B — secret "12345678901234567890"의 base32
    b32 = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # noqa: S105 — RFC 공개 테스트 벡터
    assert totp_code(b32, at=datetime.fromtimestamp(59, UTC)) == "287082"
    assert totp_code(b32, at=datetime.fromtimestamp(1111111109, UTC)) == "081804"
    assert totp_code(b32, at=datetime.fromtimestamp(1111111111, UTC)) == "050471"


def test_totp_verify_window_and_replay_step():
    from jobmate.security import totp_verify_step

    b32 = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # noqa: S105 — RFC 공개 테스트 벡터
    at = datetime.fromtimestamp(59, UTC)
    code = totp_code(b32, at=at)
    step = totp_verify_step(b32, code, at=at)
    assert step == 1
    # ±1 윈도우: 한 스텝 미래 시각에서도 검증됨
    assert totp_verify_step(b32, code, at=datetime.fromtimestamp(60, UTC)) == 1
    # 2스텝 이상 벗어나면 실패
    assert totp_verify_step(b32, code, at=datetime.fromtimestamp(200, UTC)) is None
    # 형식 오류 거부
    assert totp_verify_step(b32, "abcdef", at=at) is None
    assert totp_verify_step(b32, "12345", at=at) is None


# ---- setup / confirm ----


def test_setup_requires_session(monkeypatch):
    client, store = _client(monkeypatch)
    _mkuser(store)
    r = client.post("/api/v1/auth/mfa/setup")
    assert r.status_code == 401
    store.close()


def test_setup_stores_encrypted_secret(monkeypatch):
    client, store = _client(monkeypatch)
    _mkuser(store)
    _login(client)
    r = client.post("/api/v1/auth/mfa/setup")
    secret = r.json()["secret"]
    row = store.get_mfa_secret(_user_id(store))
    assert row["status"] == "pending"
    # 평문 비밀키가 DB에 없어야 한다 — enc:v1: 접두사 또는 복호화 가능 값
    assert secret not in row["secret_enc"]
    assert row["secret_enc"].startswith("enc:v1:")
    store.close()


def _user_id(store):
    return store.get_user_by_email("m@b.com")["id"]


def test_confirm_wrong_code_fails(monkeypatch):
    client, store = _client(monkeypatch)
    _mkuser(store)
    _login(client)
    client.post("/api/v1/auth/mfa/setup")
    r = client.post("/api/v1/auth/mfa/confirm", json={"code": "000000"})
    assert r.status_code == 400
    assert store.get_mfa_secret(_user_id(store))["status"] == "pending"
    store.close()


def test_confirm_activates_and_issues_backup_codes(monkeypatch):
    client, store = _client(monkeypatch)
    _mkuser(store)
    _, codes = _enable_mfa(client)
    assert len(codes) == 10
    assert all(len(c) == 10 for c in codes)
    row = store.get_mfa_secret(_user_id(store))
    assert row["status"] == "active"
    assert row["confirmed_at"] is not None
    # 백업 코드는 해시만 저장 — 평문이 DB에 없어야 한다
    hashes = [
        r["code_hash"]
        for r in store._conn.execute("SELECT code_hash FROM mfa_backup_codes").fetchall()
    ]
    assert len(hashes) == 10
    for c in codes:
        assert c not in hashes
    store.close()


def test_setup_rejected_when_active(monkeypatch):
    client, store = _client(monkeypatch)
    _mkuser(store)
    secret, _ = _enable_mfa(client)
    _login_mfa(client, secret)
    r = client.post("/api/v1/auth/mfa/setup")
    assert r.status_code == 409
    store.close()


def _login_mfa(client, secret):
    """MFA 계정 로그인 → verify까지 완료해 세션 확보."""
    ticket = _mfa_ticket(client)
    code = totp_code(secret)
    r = client.post("/api/v1/auth/mfa/verify", json={"mfa_ticket": ticket, "code": code})
    assert r.status_code == 200
    return r


# ---- login challenge / verify ----


def test_login_returns_mfa_challenge_not_session(monkeypatch):
    client, store = _client(monkeypatch)
    _mkuser(store)
    secret, _ = _enable_mfa(client)
    r = _login(client)
    assert r.json()["mfa_required"] is True
    assert "mfa_ticket" in r.json()
    # 세션 쿠키가 발급되지 않는다 — 인증 API는 401
    assert client.get("/api/v1/auth/mfa/status").status_code == 401
    store.close()


def test_mfa_verify_totp_issues_session(monkeypatch):
    client, store = _client(monkeypatch)
    _mkuser(store)
    secret, _ = _enable_mfa(client)
    _login_mfa(client, secret)
    assert client.get("/api/v1/auth/mfa/status").status_code == 200
    st = client.get("/api/v1/auth/mfa/status").json()
    assert st["mfa_enabled"] is True
    assert st["backup_codes_remaining"] == 10
    store.close()


def test_mfa_verify_rejects_reused_totp_code(monkeypatch):
    """같은 TOTP 스텝의 코드는 두 번 쓸 수 없다 — 두 번째는 새 티켓으로도 실패."""
    client, store = _client(monkeypatch)
    _mkuser(store)
    secret, _ = _enable_mfa(client)
    ticket = _mfa_ticket(client)
    code = totp_code(secret)
    assert (
        client.post(
            "/api/v1/auth/mfa/verify", json={"mfa_ticket": ticket, "code": code}
        ).status_code
        == 200
    )
    # 새 티켓 + 같은 코드(같은 스텝) → 리플레이 거부
    ticket2 = _mfa_ticket(client)
    r = client.post("/api/v1/auth/mfa/verify", json={"mfa_ticket": ticket2, "code": code})
    assert r.status_code == 401
    store.close()


def test_mfa_verify_backup_code_single_use(monkeypatch):
    client, store = _client(monkeypatch)
    _mkuser(store)
    _, codes = _enable_mfa(client)
    backup = codes[0]
    ticket = _mfa_ticket(client)
    r = client.post("/api/v1/auth/mfa/verify", json={"mfa_ticket": ticket, "code": backup})
    assert r.status_code == 200
    # 같은 백업 코드는 재사용 불가 — 새 티켓으로도 실패
    ticket2 = _mfa_ticket(client)
    r2 = client.post("/api/v1/auth/mfa/verify", json={"mfa_ticket": ticket2, "code": backup})
    assert r2.status_code == 401
    store.close()


def test_mfa_verify_invalid_ticket_and_bad_code(monkeypatch):
    client, store = _client(monkeypatch)
    _mkuser(store)
    secret, _ = _enable_mfa(client)
    # 존재하지 않는 티켓
    r = client.post("/api/v1/auth/mfa/verify", json={"mfa_ticket": "bogus", "code": "123456"})
    assert r.status_code == 400
    # 유효 티켓 + 틀린 코드 → 티켓 소진, 재사용 불가
    ticket = _mfa_ticket(client)
    r = client.post("/api/v1/auth/mfa/verify", json={"mfa_ticket": ticket, "code": "000000"})
    assert r.status_code == 401
    r2 = client.post("/api/v1/auth/mfa/verify", json={"mfa_ticket": ticket, "code": "000000"})
    assert r2.status_code == 400  # ticket already consumed
    store.close()


def test_mfa_verify_expired_ticket(monkeypatch):
    client, store = _client(monkeypatch)
    _mkuser(store)
    secret, _ = _enable_mfa(client)
    ticket = _mfa_ticket(client)
    # 티켓 만료 처리
    from datetime import timedelta

    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    with store.tx() as c:
        c.execute("UPDATE mfa_tickets SET expires_at=?", (past,))
    r = client.post(
        "/api/v1/auth/mfa/verify", json={"mfa_ticket": ticket, "code": totp_code(secret)}
    )
    assert r.status_code == 400
    store.close()


def test_non_mfa_login_unchanged(monkeypatch):
    client, store = _client(monkeypatch)
    _mkuser(store)
    r = _login(client)
    assert r.status_code == 200
    assert "mfa_required" not in r.json()
    assert "set-cookie" in r.headers
    store.close()


# ---- status / disable / backup regen ----


def test_mfa_disable_requires_reauth_and_revokes_sessions(monkeypatch):
    client, store = _client(monkeypatch)
    _mkuser(store)
    secret, _ = _enable_mfa(client)
    _login_mfa(client, secret)
    # 재인증 없이 해제 시도 → 403
    r = client.post("/api/v1/auth/mfa/disable", json={})
    assert r.status_code == 403
    # 비밀번호 재인증으로 해제
    r = client.post("/api/v1/auth/mfa/disable", json={"password": _PW})
    assert r.status_code == 200
    assert store.get_mfa_secret(_user_id(store))["status"] == "disabled"
    # 모든 세션 폐기 — 이후 호출 401
    assert client.get("/api/v1/auth/mfa/status").status_code == 401
    # 백업 코드도 삭제됨
    assert store.count_mfa_backup_codes(_user_id(store)) == 0
    store.close()


def _next_step_code(secret: str) -> str:
    """다음 30초 스텝의 TOTP 코드 — 같은 스텝 재사용(리플레이 거부)과 충돌하지 않게."""
    import time

    step = int(time.time() // 30) + 1
    return totp_code(secret, at=datetime.fromtimestamp(step * 30 + 1, UTC))


def test_mfa_disable_with_totp_code(monkeypatch):
    client, store = _client(monkeypatch)
    _mkuser(store)
    secret, _ = _enable_mfa(client)
    _login_mfa(client, secret)
    # 직전 verify와 다른 스텝의 코드여야 한다(같은 스텝 재사용은 리플레이로 거부)
    code = _next_step_code(secret)
    r = client.post("/api/v1/auth/mfa/disable", json={"code": code})
    assert r.status_code == 200
    store.close()


def test_backup_codes_regen_invalidates_old(monkeypatch):
    client, store = _client(monkeypatch)
    _mkuser(store)
    secret, old_codes = _enable_mfa(client)
    _login_mfa(client, secret)
    r = client.post("/api/v1/auth/mfa/backup-codes", json={"password": _PW})
    assert r.status_code == 200
    new_codes = r.json()["backup_codes"]
    assert len(new_codes) == 10
    assert set(new_codes).isdisjoint(old_codes)
    # 구 코드는 소진 처리돼 verify에서 실패해야 한다
    ticket = _mfa_ticket(client)
    r2 = client.post(
        "/api/v1/auth/mfa/verify",
        json={"mfa_ticket": ticket, "code": old_codes[0]},
    )
    assert r2.status_code == 401
    store.close()


def test_mfa_setup_pending_does_not_challenge_login(monkeypatch):
    """setup만 하고 confirm 안 한 계정은 로그인 시 MFA를 요구하지 않는다."""
    client, store = _client(monkeypatch)
    _mkuser(store)
    _login(client)
    client.post("/api/v1/auth/mfa/setup")
    r = _login(client)
    assert r.status_code == 200
    assert "mfa_required" not in r.json()
    assert "set-cookie" in r.headers  # 세션은 정상 발급돼야 한다
    store.close()


# ---- 리뷰 보강: 감사·세션폐기·티켓 해시·재인증 실패·throttle ----


def test_mfa_audit_events_recorded(monkeypatch):
    """MFA 생명주기 감사 이벤트가 전부 기록되고 시크릿/코드 원문이 없다."""
    client, store = _client(monkeypatch)
    _mkuser(store)
    secret, codes = _enable_mfa(client)
    _login_mfa(client, secret)
    client.post("/api/v1/auth/mfa/backup-codes", json={"password": _PW})
    client.post("/api/v1/auth/mfa/disable", json={"password": _PW})

    actions = {r["action"] for r in store.list_audit(limit=500)}
    for expected in (
        "auth_mfa_setup",
        "auth_mfa_enabled",
        "auth_login_mfa_challenge",
        "auth_login_mfa",
        "auth_mfa_backup_regen",
        "auth_mfa_disabled",
    ):
        assert expected in actions, f"missing audit event: {expected}"
    # 감사 detail에 TOTP 비밀키·백업 코드 원문이 없어야 한다
    blob = str(store.list_audit(limit=500))
    assert secret not in blob
    for c in codes:
        assert c not in blob
    store.close()


def test_mfa_confirm_revokes_prior_sessions(monkeypatch):
    """confirm 완료 시 활성화 이전 세션이 전부 폐기된다."""
    client, store = _client(monkeypatch)
    _mkuser(store)
    _login(client)
    # confirm 전 세션으로 상태 조회 가능 확인
    assert client.get("/api/v1/auth/mfa/status").status_code == 200
    secret = client.post("/api/v1/auth/mfa/setup").json()["secret"]
    r = client.post("/api/v1/auth/mfa/confirm", json={"code": totp_code(secret)})
    assert r.status_code == 200
    # confirm이 모든 세션을 폐기 — 같은 쿠키로는 401
    assert client.get("/api/v1/auth/mfa/status").status_code == 401
    store.close()


def test_mfa_ticket_stored_as_hash_only(monkeypatch):
    """mfa_ticket 원문은 DB에 없고 SHA-256 해시만 저장된다."""
    client, store = _client(monkeypatch)
    _mkuser(store)
    secret, _ = _enable_mfa(client)
    ticket = _mfa_ticket(client)
    rows = store._conn.execute("SELECT ticket_hash FROM mfa_tickets").fetchall()
    assert len(rows) == 1
    assert rows[0]["ticket_hash"] != ticket
    assert len(rows[0]["ticket_hash"]) == 64  # sha256 hex
    store.close()


def test_mfa_verify_failure_lockout(monkeypatch):
    """사용자당 5회 실패 시 10분간 verify가 429로 잠긴다(티켓 재발급 무관)."""
    client, store = _client(monkeypatch)
    _mkuser(store)
    secret, _ = _enable_mfa(client)
    for _ in range(5):
        ticket = _mfa_ticket(client)
        r = client.post("/api/v1/auth/mfa/verify", json={"mfa_ticket": ticket, "code": "000001"})
        assert r.status_code == 401
    # 6번째 — 올바른 코드여도 잠금
    ticket = _mfa_ticket(client)
    r = client.post(
        "/api/v1/auth/mfa/verify",
        json={"mfa_ticket": ticket, "code": totp_code(secret)},
    )
    assert r.status_code == 429
    store.close()


def test_mfa_reauth_throttled_after_failures(monkeypatch):
    """reauth TOTP 실패도 같은 카운터 — 5회 후 올바른 코드도 거부."""
    client, store = _client(monkeypatch)
    _mkuser(store)
    secret, _ = _enable_mfa(client)
    _login_mfa(client, secret)
    for _ in range(5):
        r = client.post("/api/v1/auth/mfa/disable", json={"code": "000001"})
        assert r.status_code == 403
    # 잠금 후 올바른 다음 스텝 코드도 403
    r = client.post("/api/v1/auth/mfa/disable", json={"code": _next_step_code(secret)})
    assert r.status_code == 403
    store.close()


def test_backup_codes_regen_requires_reauth(monkeypatch):
    """regen도 재인증 필수 — 없거나 틀린 비밀번호는 403."""
    client, store = _client(monkeypatch)
    _mkuser(store)
    secret, _ = _enable_mfa(client)
    _login_mfa(client, secret)
    assert client.post("/api/v1/auth/mfa/backup-codes", json={}).status_code == 403
    assert (
        client.post("/api/v1/auth/mfa/backup-codes", json={"password": "wrong"}).status_code == 403
    )
    # MFA는 여전히 활성 상태
    assert store.get_mfa_secret(_user_id(store))["status"] == "active"
    store.close()


def test_disable_wrong_password_rejected(monkeypatch):
    client, store = _client(monkeypatch)
    _mkuser(store)
    secret, _ = _enable_mfa(client)
    _login_mfa(client, secret)
    r = client.post("/api/v1/auth/mfa/disable", json={"password": "wrongpass1"})
    assert r.status_code == 403
    assert store.get_mfa_secret(_user_id(store))["status"] == "active"
    store.close()


def test_backup_code_use_decrements_remaining(monkeypatch):
    client, store = _client(monkeypatch)
    _mkuser(store)
    _, codes = _enable_mfa(client)
    ticket = _mfa_ticket(client)
    r = client.post("/api/v1/auth/mfa/verify", json={"mfa_ticket": ticket, "code": codes[0]})
    assert r.status_code == 200
    st = client.get("/api/v1/auth/mfa/status").json()
    assert st["backup_codes_remaining"] == 9
    store.close()


# ---- 재리뷰 보강: reauth 비밀번호 throttle·confirm throttle·Google MFA ----


def test_mfa_reauth_password_throttled(monkeypatch):
    """reauth 비밀번호 경로도 실패 카운터 공유 — 5회 후 올바른 비밀번호도 403.
    세션 탈취자의 /mfa/disable 비밀번호 무차별 대입 차단."""
    client, store = _client(monkeypatch)
    _mkuser(store)
    secret, _ = _enable_mfa(client)
    _login_mfa(client, secret)
    for _ in range(5):
        r = client.post("/api/v1/auth/mfa/disable", json={"password": "wrongpass1"})
        assert r.status_code == 403
    # 잠금 후 올바른 비밀번호도 거부 — MFA는 여전히 active
    r = client.post("/api/v1/auth/mfa/disable", json={"password": _PW})
    assert r.status_code == 403
    assert store.get_mfa_secret(_user_id(store))["status"] == "active"
    store.close()


def test_mfa_confirm_throttled(monkeypatch):
    """confirm의 TOTP 추측도 5회 실패 시 429로 잠긴다."""
    client, store = _client(monkeypatch)
    _mkuser(store)
    _login(client)
    secret = client.post("/api/v1/auth/mfa/setup").json()["secret"]
    for _ in range(5):
        r = client.post("/api/v1/auth/mfa/confirm", json={"code": "000001"})
        assert r.status_code == 400
    r = client.post("/api/v1/auth/mfa/confirm", json={"code": totp_code(secret)})
    assert r.status_code == 429
    store.close()


def test_mfa_disable_reenable_roundtrip(monkeypatch):
    """disable → 재setup → confirm → 로그인이 다시 MFA를 요구하는 전체 왕복.
    upsert의 last_totp_step 리셋 분기(status='disabled' → pending)를 검증."""
    client, store = _client(monkeypatch)
    _mkuser(store)
    secret, _ = _enable_mfa(client)
    _login_mfa(client, secret)
    r = client.post("/api/v1/auth/mfa/disable", json={"password": _PW})
    assert r.status_code == 200
    # 비활성화 직후 로그인은 세션만 발급
    _login(client)
    # 재등록
    r = client.post("/api/v1/auth/mfa/setup")
    secret2 = r.json()["secret"]
    r = client.post("/api/v1/auth/mfa/confirm", json={"code": totp_code(secret2)})
    assert r.status_code == 200
    # 재활성화 후 로그인은 다시 챌린지
    r = _login(client)
    assert r.json().get("mfa_required") is True
    ticket = r.json()["mfa_ticket"]
    r = client.post(
        "/api/v1/auth/mfa/verify",
        json={"mfa_ticket": ticket, "code": totp_code(secret2)},
    )
    assert r.status_code == 200
    store.close()


def _mock_google(monkeypatch, *, sub="gsub-1", email="m@b.com"):
    import httpx

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
            json={"sub": sub, "email": email, "name": "User", "email_verified": True},
            request=httpx.Request("GET", "https://openidconnect.googleapis.com/v1/userinfo"),
        ),
    )


def test_google_login_mfa_challenge(monkeypatch):
    """MFA 활성 계정의 Google 로그인은 세션 대신 챌린지 쿠키+페이지로 간다.
    챌린지 페이지 → /mfa/verify 성공 시에만 세션이 발급된다."""
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "sec")
    _mock_google(monkeypatch, sub="gsub-mfa", email="m@b.com")
    client, store = _client(monkeypatch)
    _mkuser(store)
    secret, _ = _enable_mfa(client)
    # Google sub를 계정에 연결
    store.link_google_sub(_user_id(store), "gsub-mfa")

    client.cookies.set("jobmate_oauth_state", "state")
    r = client.get("/api/v1/auth/google/callback?code=c&state=state", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"].endswith("/api/v1/auth/mfa/challenge")
    sc = r.headers.get("set-cookie", "")
    assert "jobmate_mfa_pending" in sc
    assert "jobmate_session" not in sc  # 세션은 아직 발급되지 않는다

    # 챌린지 페이지 — 티켓이 HTML에 심어진다
    r = client.get("/api/v1/auth/mfa/challenge")
    assert r.status_code == 200
    ticket = client.cookies.get("jobmate_mfa_pending")
    assert ticket and ticket in r.text

    # 올바른 TOTP → 세션 발급
    r = client.post(
        "/api/v1/auth/mfa/verify",
        json={"mfa_ticket": ticket, "code": totp_code(secret)},
    )
    assert r.status_code == 200
    assert client.get("/api/v1/auth/me").status_code == 200
    store.close()


def test_google_login_mfa_challenge_wrong_code_no_session(monkeypatch):
    """챌린지에서 코드가 틀리면 세션이 발급되지 않는다."""
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "sec")
    _mock_google(monkeypatch, sub="gsub-mfa2", email="m@b.com")
    client, store = _client(monkeypatch)
    _mkuser(store)
    secret, _ = _enable_mfa(client)
    store.link_google_sub(_user_id(store), "gsub-mfa2")
    client.cookies.set("jobmate_oauth_state", "state")
    client.get("/api/v1/auth/google/callback?code=c&state=state", follow_redirects=False)
    ticket = client.cookies.get("jobmate_mfa_pending")
    r = client.post(
        "/api/v1/auth/mfa/verify",
        json={"mfa_ticket": ticket, "code": "000001"},
    )
    assert r.status_code == 401
    assert client.get("/api/v1/auth/me").status_code == 401
    store.close()
