"""spec 005 US2 — 동의 이력·재동의·계정 삭제 매트릭스·export 확장 테스트.

커버: FR-011 가입 동의 기록, FR-012 /auth/config 버전, FR-013 /auth/me
consent_required + POST /auth/consent + consent_block_response 게이트,
FR-014~016 삭제 매트릭스·영수증, FR-017 export 확장, FR-018 auth_consent 감사.
"""

import hashlib
import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from starlette.requests import Request

from jobmate.api.app import create_app
from jobmate.api.appctx import build_app_context
from jobmate.config import Settings
from jobmate.security import SESSION_COOKIE, hash_password, hash_token
from jobmate.store.db import Store
from tests.integration.helpers import default_outputs, scripted_llm

_PW = "Str0ng#Passw0rd!"


def _client(monkeypatch=None, **settings_kw):
    """JOBMATE_API_KEY 설정 → _email_verification_required()=True 경로
    (가입에 agree_terms 필수, 이메일 인증 플로우 활성)."""
    if monkeypatch is not None:
        monkeypatch.setenv("JOBMATE_API_KEY", "test-api-key")
        for v in (
            "JOBMATE_ALLOWED_EMAILS",
            "VERCEL",
            "JOBMATE_MODE",
            "JOBMATE_ENCRYPTION_KEY",
            "JOBMATE_PUBLIC_HOSTS",
            "JOBMATE_ADMIN_EMAILS",
            "JOBMATE_LEAD_EMAILS",
            "JOBMATE_CONSENT_ENFORCE",
            "JOBMATE_PII_MASK",
            "JOBMATE_TERMS_VERSION",
            "JOBMATE_PRIVACY_VERSION",
            "JOBMATE_PRIVACY_POLICY_URL",
        ):
            monkeypatch.delenv(v, raising=False)
    db = str(Path(tempfile.mkdtemp()) / "t.db")
    kw = {"db_path": db, "budget_max_tokens": 100_000}
    kw.update(settings_kw)
    settings = Settings(**kw)
    store = Store(db)
    app = create_app(settings=settings, store=store, llm=scripted_llm(default_outputs()))
    return TestClient(app), store, settings


def _mkuser(store, email="u@b.com", password=_PW):
    return store.create_user(email, hash_password(password), "U", "", True)


def _login(client, email="u@b.com", password=_PW):
    return client.post("/api/v1/auth/login", json={"email": email, "password": password})


def _signup(client, email="s@b.com", password=_PW, agree=True):
    return client.post(
        "/api/v1/auth/signup",
        json={
            "email": email,
            "password": password,
            "display_name": "S",
            "agree_terms": agree,
        },
        headers={"user-agent": "pytest-agent/1.0"},
    )


def _audit_actions(store, action):
    rows = store._conn.execute("SELECT * FROM audit_events WHERE action=?", (action,)).fetchall()
    return [dict(r) for r in rows]


# ---- FR-011/012: 가입 동의 기록 + /auth/config ----


def test_signup_records_consent_and_audit(monkeypatch):
    client, store, settings = _client(monkeypatch)
    r = _signup(client)
    assert r.status_code == 201
    user = store.get_user_by_email("s@b.com")
    assert user is not None
    rows = store.list_consents_for_user(user["id"])
    assert len(rows) == 1
    row = rows[0]
    assert row["terms_version"] == settings.terms_version
    assert row["privacy_version"] == settings.privacy_version
    assert row["agreed_at"] and row["created_at"]
    assert row["ip"]  # testclient host
    assert row["user_agent"] == "pytest-agent/1.0"
    # FR-011 — auth_signup audit detail에 terms_version 포함
    audits = _audit_actions(store, "auth_signup")
    assert audits, "auth_signup audit missing"
    detail = json.loads(audits[-1]["detail"])
    assert detail["terms_agreed"] is True
    assert detail["terms_version"] == settings.terms_version
    assert detail["privacy_version"] == settings.privacy_version


def test_signup_without_terms_still_blocked_in_verify_mode(monkeypatch):
    client, store, _ = _client(monkeypatch)
    r = _signup(client, agree=False)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "terms_required"
    assert store.get_user_by_email("s@b.com") is None


def test_auth_config_returns_versions(monkeypatch):
    client, _, _ = _client(
        monkeypatch,
        privacy_policy_url="https://example.com/privacy",
    )
    r = client.get("/api/v1/auth/config")
    assert r.status_code == 200
    body = r.json()
    assert body["terms_version"] == "v1.0"
    assert body["privacy_version"] == "v1.0"
    assert body["privacy_policy_url"] == "https://example.com/privacy"
    assert "google_enabled" in body


# ---- FR-013: /auth/me 재동의 표시 + POST /auth/consent ----


def test_me_reports_missing_consent_then_reconsent(monkeypatch):
    client, store, settings = _client(monkeypatch)
    user = _mkuser(store)
    assert _login(client).status_code == 200

    me = client.get("/api/v1/auth/me")
    assert me.status_code == 200
    body = me.json()
    # 동의 행이 없는 기존 계정 — 두 버전 모두 부족
    assert body["consent_required"] is True
    assert sorted(body["consent_missing"]) == [
        f"privacy:{settings.privacy_version}",
        f"terms:{settings.terms_version}",
    ]

    r = client.post("/api/v1/auth/consent")
    assert r.status_code == 200
    assert r.json()["status"] == "consented"

    me2 = client.get("/api/v1/auth/me")
    assert me2.json()["consent_required"] is False
    assert me2.json()["consent_missing"] == []

    # FR-018 — auth_consent 감사 기록
    audits = _audit_actions(store, "auth_consent")
    assert len(audits) == 1
    assert audits[0]["actor"] == "u@b.com"
    detail = json.loads(audits[0]["detail"])
    assert detail["terms_version"] == settings.terms_version
    assert len(store.list_consents_for_user(user["id"])) == 1


def test_me_partial_version_mismatch(monkeypatch):
    client, store, settings = _client(monkeypatch)
    user = _mkuser(store)
    # terms만 구버전 동의 — privacy는 현행
    store.record_consent(user["id"], "v0.9", settings.privacy_version)
    assert _login(client).status_code == 200
    body = client.get("/api/v1/auth/me").json()
    assert body["consent_required"] is True
    assert body["consent_missing"] == [f"terms:{settings.terms_version}"]


def test_consent_requires_session(monkeypatch):
    client, _, _ = _client(monkeypatch)
    r = client.post("/api/v1/auth/consent")
    assert r.status_code == 401


# ---- FR-013: consent_block_response 게이트 (미들웨어 로직 단위 테스트) ----


def _fake_request(method: str, path: str, session_token: str = "") -> Request:
    headers = []
    if session_token:
        headers.append((b"cookie", f"{SESSION_COOKIE}={session_token}".encode()))
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers,
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 12345),
    }
    return Request(scope)


def _ctx_for(store, settings):
    return build_app_context(
        store=store,
        settings=settings,
        llm=scripted_llm(default_outputs()),
        adapters={},
    )


def _session_token(store, user_id: str) -> str:
    token = "raw-session-token"  # noqa: S105 — 테스트용 세션 토큰 평문
    store.create_session(user_id, hash_token(token), ttl_days=1)
    return token


def test_consent_block_off_by_default(monkeypatch):
    client, store, settings = _client(monkeypatch)  # consent_enforce 기본 off
    user = _mkuser(store)
    tok = _session_token(store, user["id"])
    ctx = _ctx_for(store, settings)
    assert ctx.consent_block_response(_fake_request("POST", "/api/v1/runs", tok)) is None


def test_consent_block_read_only(monkeypatch):
    client, store, settings = _client(monkeypatch, consent_enforce="read_only")
    user = _mkuser(store)  # 동의 행 없음 → 재동의 필요
    tok = _session_token(store, user["id"])
    ctx = _ctx_for(store, settings)
    # GET은 허용
    assert ctx.consent_block_response(_fake_request("GET", "/api/v1/runs", tok)) is None
    # 쓰기는 403 consent_required
    resp = ctx.consent_block_response(_fake_request("POST", "/api/v1/runs", tok))
    assert resp is not None
    assert resp.status_code == 403
    # 동의 후에는 쓰기도 통과
    store.record_consent(user["id"], settings.terms_version, settings.privacy_version)
    assert ctx.consent_block_response(_fake_request("POST", "/api/v1/runs", tok)) is None


def test_consent_block_block_mode_exemptions(monkeypatch):
    client, store, settings = _client(monkeypatch, consent_enforce="block")
    user = _mkuser(store)
    tok = _session_token(store, user["id"])
    ctx = _ctx_for(store, settings)
    # block 모드 — GET도 차단
    resp = ctx.consent_block_response(_fake_request("GET", "/api/v1/runs", tok))
    assert resp is not None and resp.status_code == 403
    # 면제 경로는 허용 — 데이터 주체 권리(export/account) 포함
    for path in (
        "/api/v1/auth/me",
        "/api/v1/auth/config",
        "/api/v1/auth/consent",
        "/api/v1/auth/logout",
        "/api/v1/auth/export",
        "/api/v1/auth/account",
    ):
        assert ctx.consent_block_response(_fake_request("GET", path, tok)) is None, path
    assert ctx.consent_block_response(_fake_request("POST", "/api/v1/auth/consent", tok)) is None
    assert ctx.consent_block_response(_fake_request("DELETE", "/api/v1/auth/account", tok)) is None
    # trailing slash 변형도 면제 정규화로 통과(fail-closed 우회 방지)
    assert ctx.consent_block_response(_fake_request("GET", "/api/v1/auth/export/", tok)) is None
    # 세션 없는 요청(머신 키 경로)은 대상 아님
    assert ctx.consent_block_response(_fake_request("GET", "/api/v1/runs")) is None


# ---- FR-013: consent 게이트 e2e (실제 미들웨어 경유) ----


def test_consent_block_mode_e2e_via_middleware(monkeypatch):
    """block 모드 — 동의 없는 세션 사용자는 일반 API 403, 데이터 주체
    권리(export)와 재동의(consent)는 면제로 통과한다."""
    client, store, _ = _client(monkeypatch, consent_enforce="block")
    _mkuser(store, email="gate@b.com")  # 동의 행 없음 → 재동의 필요
    assert _login(client, email="gate@b.com").status_code == 200

    # 일반 API는 403 consent_required
    r = client.get("/api/v1/runs")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "consent_required"

    # 데이터 주체 권리·상태 조회는 면제 — block 모드에서도 통과
    assert client.get("/api/v1/auth/me").status_code == 200
    assert client.get("/api/v1/auth/export").status_code == 200

    # 재동의하면 차단이 풀린다
    r = client.post("/api/v1/auth/consent")
    assert r.status_code == 200
    assert client.get("/api/v1/runs").status_code == 200


def test_consent_block_mode_account_delete_exempt(monkeypatch):
    """block 모드 — 계정 삭제(DELETE /auth/account)도 면제 경로라
    재동의 전에도 실행 가능해야 한다(파기권 보장)."""
    client, store, _ = _client(monkeypatch, consent_enforce="block")
    user = _mkuser(store, email="del-gate@b.com")
    assert _login(client, email="del-gate@b.com").status_code == 200
    # 대조군 — 일반 쓰기는 막힌다
    assert client.post("/api/v1/cards", json={}).status_code == 403
    r = client.request("DELETE", "/api/v1/auth/account", json={"password": _PW})
    assert r.status_code == 200
    assert r.json()["status"] == "deleted"
    assert store.get_user_by_id(user["id"]) is None


def test_consent_read_only_mode_e2e_via_middleware(monkeypatch):
    """read_only 모드 — GET은 허용, 비-GET은 403 consent_required."""
    client, store, _ = _client(monkeypatch, consent_enforce="read_only")
    _mkuser(store, email="ro@b.com")
    assert _login(client, email="ro@b.com").status_code == 200

    assert client.get("/api/v1/runs").status_code == 200
    r = client.post("/api/v1/cards", json={})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "consent_required"
    # 재동의(POST지만 면제 경로)는 read_only에서도 가능 — 이후 쓰기 제한 해제
    assert client.post("/api/v1/auth/consent").status_code == 200
    # 동의 후 POST는 consent 게이트를 통과 — 422(본문 검증) 등 라우트 단계 응답
    r2 = client.post("/api/v1/cards", json={})
    assert r2.status_code != 403 or r2.json().get("error", {}).get("code") != "consent_required"


# ---- FR-014~016: 계정 삭제 매트릭스 + 영수증 ----


def _seed_user_data(store, user):
    """삭제 대상/익명화 대상을 골고루 심는다."""
    uid, email = user["id"], user["email"]
    store.record_consent(uid, "v1.0", "v1.0")
    store.create_session(uid, hash_token("sess-tok"), ttl_days=1)
    # 인증/보안 부속 테이블 — cascade가 함께 정리해야 한다
    store.create_email_verification(email)
    store.create_password_reset(uid, email)
    store.create_api_key(uid, "test-key", "keyhash-1", "jm_test", [])
    now = datetime.now(UTC).isoformat()
    store.create_share_link(
        "share-tok-1",
        "run",
        "r1",
        {"x": 1},
        email,
        (datetime.now(UTC) + timedelta(days=30)).isoformat(),
    )
    store.create_card("card", "https://example.com/feed.json", created_by=email)
    now = datetime.now(UTC).isoformat()
    with store.tx() as c:
        c.execute(
            "INSERT INTO runs(id,workflow_key,workflow_version,status,trace_id,"
            "created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            ("run-del-1", "wf", "1", "completed", "tr", email, now, now),
        )
        c.execute(
            "INSERT INTO agent_events(id,idempotency_key,event_type,agent_id,task_id,"
            "run_id,actor,summary,detail,occurred_at,created_at,org_id)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "ae1",
                "idem-1",
                "note",
                None,
                None,
                "run-del-1",
                email,
                "s",
                "{}",
                now,
                now,
                "",
            ),
        )
        c.execute(
            "INSERT INTO run_comments(id,run_id,user_id,email,body,created_at) VALUES(?,?,?,?,?,?)",
            ("rc1", "run-del-1", uid, email, "hi", now),
        )
        # 비종결(진행 중) run — 삭제 후에도 행은 남고 created_by만 익명화
        c.execute(
            "INSERT INTO runs(id,workflow_key,workflow_version,status,trace_id,"
            "created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            ("run-live-1", "wf", "1", "running", "tr2", email, now, now),
        )
        # MFA·권한 행 — 계정 삭제 시 함께 정리되어야 한다
        c.execute(
            "INSERT INTO mfa_secrets(user_id,secret_enc,status,created_at) VALUES(?,?,?,?)",
            (uid, "enc:v1:test", "pending", now),  # pending — 로그인을 MFA 챌린지로 보내지 않음
        )
        c.execute(
            "INSERT INTO mfa_backup_codes(id,user_id,code_hash,created_at) VALUES(?,?,?,?)",
            ("bc1", uid, "h1", now),
        )
        c.execute(
            "INSERT INTO mfa_tickets(id,user_id,ticket_hash,expires_at,created_at)"
            " VALUES(?,?,?,?,?)",
            ("mt1", uid, "th1", now, now),
        )
        c.execute(
            "INSERT INTO user_permissions(id,user_id,capability,effect,"
            "scope_type,scope_id,created_at,org_id) VALUES(?,?,?,?,?,?,?,?)",
            ("up1", uid, "runs:write", "allow", "global", "", now, ""),
        )
        # 다른 사용자의 데이터 — 삭제되면 안 됨
        c.execute(
            "INSERT INTO share_links(token,kind,ref_id,payload,created_by,"
            "expires_at,created_at,org_id) VALUES(?,?,?,?,?,?,?,?)",
            (
                "share-other",
                "run",
                "r2",
                "{}",
                "other@b.com",
                (datetime.now(UTC) + timedelta(days=30)).isoformat(),
                now,
                "",
            ),
        )


def test_delete_account_full_matrix_and_receipt(monkeypatch):
    client, store, _ = _client(monkeypatch)
    user = _mkuser(store, email="del@b.com")
    _seed_user_data(store, user)
    assert _login(client, email="del@b.com").status_code == 200

    r = client.request("DELETE", "/api/v1/auth/account", json={"password": _PW})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "deleted"
    receipt_id = body["receipt_id"]
    assert receipt_id

    uid, email = user["id"], user["email"]
    # hard delete 검증
    assert store.get_user_by_id(uid) is None
    assert store.list_user_sessions(uid) == []
    assert store.list_consents_for_user(uid) == []
    assert store.get_share_link("share-tok-1") is None
    assert store.list_cards() == []  # 본인 생성 카드 삭제(테이블에 1건뿐)
    # 타인 share_link는 보존
    assert store.get_share_link("share-other") is not None
    # anonymize 검증 — 행은 남고 created_by/actor만 'deleted_user'
    run = store._conn.execute("SELECT created_by FROM runs WHERE id='run-del-1'").fetchone()
    assert run["created_by"] == "deleted_user"
    ev = store._conn.execute("SELECT actor FROM agent_events WHERE id='ae1'").fetchone()
    assert ev["actor"] == "deleted_user"
    audit = store._conn.execute(
        "SELECT actor FROM audit_events WHERE action='auth_login' ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    assert audit is not None and audit["actor"] == "deleted_user"
    # run_comments 삭제
    assert store._conn.execute("SELECT COUNT(*) AS n FROM run_comments").fetchone()["n"] == 0
    # 인증/보안 부속 테이블 정리 — email/password-reset/mfa/api_keys/user_permissions
    assert (
        store._conn.execute(
            "SELECT COUNT(*) AS n FROM email_verifications WHERE email=?", (email,)
        ).fetchone()["n"]
        == 0
    )
    assert (
        store._conn.execute(
            "SELECT COUNT(*) AS n FROM password_reset_tokens WHERE user_id=?", (uid,)
        ).fetchone()["n"]
        == 0
    )
    for table in ("mfa_secrets", "mfa_backup_codes", "mfa_tickets"):
        # noqa: S608 — table은 고정 리터럴 튜플에서만 온다
        sql = f"SELECT COUNT(*) AS n FROM {table} WHERE user_id=?"  # noqa: S608
        assert store._conn.execute(sql, (uid,)).fetchone()["n"] == 0, table
    assert (
        store._conn.execute(
            "SELECT COUNT(*) AS n FROM api_keys WHERE owner_user_id=?", (uid,)
        ).fetchone()["n"]
        == 0
    )
    assert (
        store._conn.execute(
            "SELECT COUNT(*) AS n FROM user_permissions WHERE user_id=?", (uid,)
        ).fetchone()["n"]
        == 0
    )
    # 비종결 run — 행은 보존(업무 이력), created_by만 'deleted_user'로 익명화
    live = store._conn.execute(
        "SELECT status, created_by FROM runs WHERE id='run-live-1'"
    ).fetchone()
    assert live is not None
    assert live["status"] == "running"
    assert live["created_by"] == "deleted_user"

    # FR-015/016 — 영수증: 해시+건수만, PII 없음
    receipt = store.get_deletion_receipt(receipt_id)
    assert receipt is not None
    assert receipt["user_id_hash"] == hashlib.sha256(uid.encode()).hexdigest()
    assert receipt["status"] == "completed"
    assert receipt["counts"].get("users") == 1
    assert receipt["counts"].get("consent_records") == 1
    assert receipt["counts"].get("share_links") == 1
    receipt_blob = json.dumps(receipt)
    assert email not in receipt_blob
    assert uid not in receipt_blob
    # auth_account_delete 감사 + 영수증 id 연결
    audits = _audit_actions(store, "auth_account_delete")
    assert audits
    assert json.loads(audits[-1]["detail"]).get("receipt_id") == receipt_id

    # 세션 폐기 — export/추가 API는 401
    assert client.get("/api/v1/auth/export").status_code == 401


def test_delete_account_last_admin_still_blocked(monkeypatch):
    client, store, _ = _client(monkeypatch)
    # 유일 사용자 + admin 강제 — last_admin 보호가 삭제 매트릭스보다 먼저
    user = _mkuser(store, email="admin@b.com")
    store.set_user_role(user["id"], "admin")
    assert _login(client, email="admin@b.com").status_code == 200
    r = client.request("DELETE", "/api/v1/auth/account", json={"password": _PW})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "last_admin"
    assert store.get_user_by_id(user["id"]) is not None
    # 차단 시 영수증도 없어야 한다
    assert store._conn.execute("SELECT COUNT(*) AS n FROM deletion_receipts").fetchone()["n"] == 0


# ---- FR-017: export 확장 ----


def test_export_includes_consent_runs_share_links(monkeypatch):
    client, store, _ = _client(monkeypatch)
    user = _mkuser(store, email="exp@b.com")
    uid, email = user["id"], user["email"]
    store.record_consent(uid, "v1.0", "v1.0", ip="1.2.3.4", user_agent="ua")
    now = datetime.now(UTC).isoformat()
    store.create_share_link(
        "exp-share",
        "run",
        "r9",
        {"secret": "payload-body"},
        email,
        (datetime.now(UTC) + timedelta(days=30)).isoformat(),
    )
    with store.tx() as c:
        c.execute(
            "INSERT INTO runs(id,workflow_key,workflow_version,status,trace_id,"
            "created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            ("run-exp-1", "wf", "1", "completed", "tr", email, now, now),
        )
    assert _login(client, email="exp@b.com").status_code == 200
    r = client.get("/api/v1/auth/export")
    assert r.status_code == 200
    data = r.json()

    # 비밀값 제외 — password_hash/token_hash/endpoint 원문 없음
    blob = json.dumps(data)
    assert "password_hash" not in blob
    assert "token_hash" not in blob

    assert len(data["consent_records"]) == 1
    cr = data["consent_records"][0]
    assert cr["terms_version"] == "v1.0"
    assert cr["ip"] == "1.2.3.4"

    assert len(data["runs"]) == 1
    assert data["runs"][0]["id"] == "run-exp-1"
    assert data["runs"][0]["workflow_key"] == "wf"
    # runs 항목에 input/metadata/created_by 원문이 들어가지 않는다
    assert "input" not in data["runs"][0]
    assert "created_by" not in data["runs"][0]

    assert len(data["share_links"]) == 1
    sl = data["share_links"][0]
    # spec 005 리뷰 — share_links.token은 export하지 않는다(본인 링크라도
    # 토큰 원문은 열람권 응답에 포함하지 않는 것으로 확정).
    assert "token" not in sl
    assert "payload" not in sl  # 공유 스냅샷 본문은 export하지 않는다

    # FR-018 — auth_data_export 감사
    audits = _audit_actions(store, "auth_data_export")
    assert audits
