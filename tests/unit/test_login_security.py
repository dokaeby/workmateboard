"""로그인 보안 정책 테스트 — spec 002 US3.

비밀번호 정책(validate_password/HIBP), 세션 유휴 만료·동시 세션 상한,
신규 기기 알림(auth_login_new_device + last_login_* 메타)을 검증한다.
"""

import hashlib
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from jobmate.api.app import create_app
from jobmate.config import Settings
from jobmate.security import validate_password
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
        "JOBMATE_SESSION_IDLE_DAYS",
        "JOBMATE_MAX_SESSIONS",
        "JOBMATE_PWNED_CHECK",
        "JOBMATE_PWNED_FAIL_MODE",
        "JOBMATE_PASSWORD_MIN_LEN",
    ):
        monkeypatch.delenv(v, raising=False)
    db = str(Path(tempfile.mkdtemp()) / "t.db")
    settings = Settings(db_path=db, budget_max_tokens=100_000)
    store = Store(db)
    app = create_app(settings=settings, store=store, llm=scripted_llm(default_outputs()))
    return TestClient(app), store


def _mkuser(store, email="m@b.com", password=_PW):
    from jobmate.security import hash_password

    return store.create_user(email, hash_password(password), "U", "", True)


def _login(client, email="m@b.com", password=_PW, **headers):
    return client.post("/api/v1/auth/login", json={"email": email, "password": password}, **headers)


def _user_id(store, email="m@b.com"):
    return store.get_user_by_email(email)["id"]


# ---- validate_password 단위 테스트 ----


def test_validate_password_rules():
    assert validate_password("short", "a@b.com") == "too_short"
    assert validate_password("password123", "a@b.com") == "common_password"
    assert validate_password("qwerty123", "a@b.com") == "common_password"
    assert validate_password("m@b.com-pass", "m@b.com") == "contains_email"
    assert validate_password("mymail-secret1", "mymail@b.com") == "contains_email"
    assert validate_password("Xy9!kQ2#mZ", "m@b.com") is None
    assert validate_password("13579135", "a@b.com") == "numeric_only"
    assert validate_password("aaaaaaaa", "a@b.com") == "repeated_chars"
    assert validate_password("abcdefgh", "a@b.com") == "sequential_chars"
    assert validate_password("zyxwvuts", "a@b.com") == "sequential_chars"
    assert validate_password(_PW, "a@b.com") is None


def test_validate_password_min_len_env(monkeypatch):
    monkeypatch.setenv("JOBMATE_PASSWORD_MIN_LEN", "12")
    assert validate_password("Str0ng!Pass", "a@b.com") == "too_short"
    assert validate_password("Str0ng!Passw0rd", "a@b.com") is None


# ---- signup/reset/change 3경로 정책 적용 ----


def test_signup_rejects_common_password(monkeypatch):
    client, store = _client(monkeypatch)
    r = client.post(
        "/api/v1/auth/signup",
        json={"email": "n@b.com", "password": "password123", "agree_terms": True},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "weak_password"
    store.close()


def test_signup_rejects_email_in_password(monkeypatch):
    client, store = _client(monkeypatch)
    r = client.post(
        "/api/v1/auth/signup",
        json={"email": "n@b.com", "password": "n@b.com-x9!", "agree_terms": True},
    )
    assert r.status_code == 400
    assert "이메일" in r.json()["error"]["message"]
    store.close()


def test_change_password_policy_enforced(monkeypatch):
    client, store = _client(monkeypatch)
    _mkuser(store)
    _login(client)
    r = client.post(
        "/api/v1/auth/password",
        json={"current_password": _PW, "new_password": "password123"},
    )
    assert r.status_code == 400
    # 정책 위반은 비밀번호를 바꾸지 않는다
    assert _login(client).status_code == 200
    store.close()


# ---- HIBP k-anonymity ----


def _pwned_range_response(password: str, found: bool) -> httpx.Response:
    sha1 = hashlib.sha1(password.encode()).hexdigest().upper()  # noqa: S324 — HIBP 규약 테스트용
    suffix = sha1[5:]
    body = f"{suffix}:12345\nDEADBEEF:1\n" if found else "DEADBEEF:1\n"
    return httpx.Response(
        200, text=body, request=httpx.Request("GET", "https://api.pwnedpasswords.com/range/x")
    )


def test_weak_password_rejection_audited(monkeypatch):
    """US3 AS1 — 비밀번호 정책 거부 사유가 감사에 남는다(원문 금지)."""
    client, store = _client(monkeypatch)
    r = client.post(
        "/api/v1/auth/signup",
        json={"email": "w@b.com", "password": "password123", "agree_terms": True},
    )
    assert r.status_code == 400
    hits = [a for a in store.list_audit(limit=500) if a["action"] == "auth_password_rejected"]
    assert hits, "거부 감사 이벤트가 없다"
    assert "password123" not in str(hits[0].get("detail", ""))
    store.close()


def test_pwned_password_rejected_when_enabled(monkeypatch):
    client, store = _client(monkeypatch)
    monkeypatch.setenv("JOBMATE_PWNED_CHECK", "1")
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _pwned_range_response(_PW, found=True))
    r = client.post(
        "/api/v1/auth/signup",
        json={"email": "p@b.com", "password": _PW, "agree_terms": True},
    )
    assert r.status_code == 400
    assert "유출" in r.json()["error"]["message"]
    store.close()


def test_pwned_check_disabled_by_default(monkeypatch):
    """JOBMATE_PWNED_CHECK 미설정이면 네트워크 호출 없이 통과."""

    def _boom(*a, **kw):
        raise AssertionError("HIBP should not be called")

    monkeypatch.setattr(httpx, "get", _boom)
    client, store = _client(monkeypatch)
    r = client.post(
        "/api/v1/auth/signup",
        json={"email": "p2@b.com", "password": _PW, "agree_terms": True},
    )
    assert r.status_code == 201
    store.close()


def test_pwned_check_fail_open_default(monkeypatch):
    """네트워크 실패 + fail_mode=open(기본)이면 가입 통과."""
    client, store = _client(monkeypatch)
    monkeypatch.setenv("JOBMATE_PWNED_CHECK", "1")

    def _fail(*a, **kw):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx, "get", _fail)
    r = client.post(
        "/api/v1/auth/signup",
        json={"email": "p3@b.com", "password": _PW, "agree_terms": True},
    )
    assert r.status_code == 201
    store.close()


def test_pwned_check_fail_closed(monkeypatch):
    """fail_mode=closed면 네트워크 실패도 거부."""
    client, store = _client(monkeypatch)
    monkeypatch.setenv("JOBMATE_PWNED_CHECK", "1")
    monkeypatch.setenv("JOBMATE_PWNED_FAIL_MODE", "closed")

    def _fail(*a, **kw):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx, "get", _fail)
    r = client.post(
        "/api/v1/auth/signup",
        json={"email": "p4@b.com", "password": _PW, "agree_terms": True},
    )
    assert r.status_code == 400
    store.close()


# ---- 세션 유휴 만료 ----


def _age_session(store, user_id: str, days: int) -> None:
    old = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    with store.tx() as c:
        c.execute("UPDATE auth_sessions SET last_seen_at=? WHERE user_id=?", (old, user_id))


def test_session_idle_expiry(monkeypatch):
    """기본 7일 유휴 — last_seen_at이 8일 전이면 세션 무효."""
    client, store = _client(monkeypatch)
    _mkuser(store)
    assert _login(client).status_code == 200
    assert client.get("/api/v1/auth/me").status_code == 200
    _age_session(store, _user_id(store), days=8)
    assert client.get("/api/v1/auth/me").status_code == 401
    store.close()


def test_session_idle_expiry_disabled(monkeypatch):
    """JOBMATE_SESSION_IDLE_DAYS=0이면 유휴 만료 비활성."""
    client, store = _client(monkeypatch)
    monkeypatch.setenv("JOBMATE_SESSION_IDLE_DAYS", "0")
    _mkuser(store)
    assert _login(client).status_code == 200
    _age_session(store, _user_id(store), days=30)
    assert client.get("/api/v1/auth/me").status_code == 200
    store.close()


# ---- 동시 세션 상한 ----


def test_session_limit_evicts_oldest(monkeypatch):
    """JOBMATE_MAX_SESSIONS=2 — 3번째 로그인 시 가장 오래된 세션 폐기 + 감사."""
    client, store = _client(monkeypatch)
    monkeypatch.setenv("JOBMATE_MAX_SESSIONS", "2")
    _mkuser(store)
    uid = _user_id(store)
    for _ in range(3):
        assert _login(client).status_code == 200
    sessions = store.list_user_sessions(uid)
    assert len(sessions) == 2
    # 가장 오래된 세션만 revoked — 최신 2개는 생존
    rows = store._conn.execute(
        "SELECT id,created_at,revoked_at FROM auth_sessions WHERE user_id=?"
        " ORDER BY created_at ASC",
        (uid,),
    ).fetchall()
    assert len(rows) == 3
    assert rows[0]["revoked_at"], "oldest session must be revoked"
    assert rows[1]["revoked_at"] is None and rows[2]["revoked_at"] is None
    actions = [r["action"] for r in store.list_audit(limit=500)]
    assert "auth_session_evicted" in actions
    store.close()


def test_session_touch_throttled(monkeypatch):
    """last_seen_at은 5분 스로틀 — 활동 시각만 갱신하고 매 요청 쓰지 않는다."""
    client, store = _client(monkeypatch)
    _mkuser(store)
    _login(client)
    uid = _user_id(store)
    # last_seen_at을 6분 전으로 — 다음 요청이 갱신해야 함
    old = (datetime.now(UTC) - timedelta(minutes=6)).isoformat()
    with store.tx() as c:
        c.execute("UPDATE auth_sessions SET last_seen_at=? WHERE user_id=?", (old, uid))
    assert client.get("/api/v1/auth/me").status_code == 200
    r = store._conn.execute(
        "SELECT last_seen_at FROM auth_sessions WHERE user_id=?", (uid,)
    ).fetchone()
    t1 = r["last_seen_at"]
    assert t1 != old
    # 즉시 재요청 → 5분 스로틀로 갱신되지 않는다
    assert client.get("/api/v1/auth/me").status_code == 200
    r2 = store._conn.execute(
        "SELECT last_seen_at FROM auth_sessions WHERE user_id=?", (uid,)
    ).fetchone()
    assert r2["last_seen_at"] == t1
    store.close()


def test_session_activity_resets_idle_clock(monkeypatch):
    """유휴 6일 세션도 요청이 오면 last_seen_at이 갱신돼 만료가 연기된다."""
    client, store = _client(monkeypatch)
    _mkuser(store)
    _login(client)
    uid = _user_id(store)
    _age_session(store, uid, days=6)
    assert client.get("/api/v1/auth/me").status_code == 200
    r = store._conn.execute(
        "SELECT last_seen_at FROM auth_sessions WHERE user_id=?", (uid,)
    ).fetchone()
    # 갱신된 last_seen_at은 최근이어야 한다 — 다시 6일 전으로 돌려도 통과
    assert _dt_recent(r["last_seen_at"])
    store.close()


def _dt_recent(iso: str) -> bool:
    return (datetime.now(UTC) - datetime.fromisoformat(iso)).total_seconds() < 60


def test_session_limit_unlimited(monkeypatch):
    """JOBMATE_MAX_SESSIONS=0이면 상한 없음."""
    client, store = _client(monkeypatch)
    monkeypatch.setenv("JOBMATE_MAX_SESSIONS", "0")
    _mkuser(store)
    uid = _user_id(store)
    for _ in range(5):
        assert _login(client).status_code == 200
    assert len(store.list_user_sessions(uid)) == 5
    store.close()


# ---- 신규 기기 알림 ----


def test_new_device_notification_first_login(monkeypatch):
    """첫 로그인 → auth_login_new_device 감사 + users.last_login_* 기록."""
    client, store = _client(monkeypatch)
    _mkuser(store)
    uid = _user_id(store)
    r = _login(client, headers={"user-agent": "DeviceA/1.0"})
    assert r.status_code == 200
    actions = [a["action"] for a in store.list_audit(limit=500)]
    assert "auth_login_new_device" in actions
    row = store._conn.execute(
        "SELECT last_login_ip, last_login_fingerprint, last_login_at FROM users WHERE id=?",
        (uid,),
    ).fetchone()
    assert row["last_login_at"]
    assert row["last_login_fingerprint"]
    store.close()


def test_new_device_notification_only_on_change(monkeypatch):
    """같은 UA 재로그인은 알림 없음, UA 변경 시 다시 알림."""
    client, store = _client(monkeypatch)
    _mkuser(store)
    _login(client, headers={"user-agent": "DeviceA/1.0"})
    _login(client, headers={"user-agent": "DeviceA/1.0"})
    n1 = sum(1 for a in store.list_audit(limit=500) if a["action"] == "auth_login_new_device")
    assert n1 == 1
    _login(client, headers={"user-agent": "DeviceB/2.0"})
    n2 = sum(1 for a in store.list_audit(limit=500) if a["action"] == "auth_login_new_device")
    assert n2 == 2
    store.close()


def test_login_alert_email_sent(monkeypatch):
    """알림 메일이 백그라운드 태스크로 발송된다(TestClient가 완료까지 대기)."""
    sent = []
    import jobmate.mailer as mailer_mod

    monkeypatch.setattr(
        mailer_mod,
        "send_login_alert_email",
        lambda to, ip, ua_summary, when, gmail=None: sent.append((to, {"ip": ip})) or True,
    )
    client, store = _client(monkeypatch)
    _mkuser(store, email="alert@b.com")
    assert _login(client, email="alert@b.com").status_code == 200
    assert len(sent) == 1
    assert sent[0][0] == "alert@b.com"
    assert sent[0][1]["ip"]
    store.close()
