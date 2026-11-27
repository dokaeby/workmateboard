"""스코프드 API 키 테스트 — spec 002 US4.

키 발급/목록/폐기, 스코프 deny-by-default 인가, 만료·폐기 키 거부,
소유권 분리, 권한 상승 차단(FR-024), 관리 스코프(api_keys:manage)를 검증한다.
"""

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


def _mkuser(store, email="m@b.com", password=_PW, admin=False):
    user = store.create_user(email, hash_password(password), "U", "", True)
    uid = user["id"] if isinstance(user, dict) else user
    if admin:
        with store.tx() as c:
            c.execute("UPDATE users SET role='admin' WHERE id=?", (uid,))
    return uid


def _login(client, email="m@b.com", password=_PW):
    return client.post("/api/v1/auth/login", json={"email": email, "password": password})


def _user_id(store, email="m@b.com"):
    return store.get_user_by_email(email)["id"]


def _create_key(client, scopes=None, name="k", **extra):
    body = {"name": name, "scopes": scopes or []}
    body.update(extra)
    return client.post("/api/v1/auth/api-keys", json=body)


def _auth(key: str) -> dict[str, str]:
    return {"X-API-Key": key}


def _admin_client(client, store, email="admin@b.com"):
    """admin 계정 생성 + 로그인 — 키 발급은 admin 전용(FR-024)."""
    _mkuser(store, email, admin=True)
    _login(client, email)
    return email


# ---- 발급 ----


def test_create_key_returns_raw_once_and_stores_hash_only(monkeypatch):
    client, store = _client(monkeypatch)
    _admin_client(client, store)
    r = _create_key(client, scopes=["runs:read"], name="ci")
    assert r.status_code == 201
    body = r.json()
    assert body["key"].startswith("jm_")
    assert body["key_prefix"] == body["key"][:8]
    # DB에는 원문이 없고 해시+prefix만
    row = store._conn.execute("SELECT * FROM api_keys WHERE id=?", (body["id"],)).fetchone()
    assert row["key_hash"] == hash_token(body["key"])
    assert body["key"] not in row["key_hash"]
    assert row["scopes_json"] == '["runs:read"]'
    assert row["owner_user_id"] == _user_id(store, "admin@b.com")
    store.close()


def test_create_key_requires_auth(monkeypatch):
    client, store = _client(monkeypatch)
    r = _create_key(client)
    # /api/v1/auth/*는 미들웨어 면제 → 라우트 자체가 401을 반환해야 한다
    assert r.status_code == 401
    store.close()


def test_member_cannot_create_key(monkeypatch):
    """FR-024 — member 세션은 키 발급 불가(조회·폐기만)."""
    client, store = _client(monkeypatch)
    _mkuser(store, "m@b.com")
    _login(client, "m@b.com")
    r = _create_key(client, scopes=["runs:read"])
    assert r.status_code == 403
    store.close()


def test_member_cannot_mint_elevated_scope_via_admin_path(monkeypatch):
    """member가 admin인 척 owner_user_id를 넣어도 발급 자체가 403."""
    client, store = _client(monkeypatch)
    _mkuser(store, "m@b.com")
    _mkuser(store, "a@b.com", admin=True)
    _login(client, "m@b.com")
    r = _create_key(client, scopes=["admin"], owner_user_id=_user_id(store, "a@b.com"))
    assert r.status_code == 403
    store.close()


def test_manage_key_cannot_mint_elevated_scopes(monkeypatch):
    """api_keys:manage 키는 리소스 스코프만 발급 가능 —
    *, admin, api_keys:manage 발급은 403(권한 영속 확장 차단)."""
    client, store = _client(monkeypatch)
    _admin_client(client, store)
    manage = _create_key(client, scopes=["api_keys:manage"]).json()["key"]
    client.cookies.clear()
    # manage 키로 elevated 스코프 발급 시도 → 전부 403
    for scopes in (["*"], ["admin"], ["api_keys:manage"], ["runs:read", "admin"]):
        r = client.post(
            "/api/v1/auth/api-keys",
            headers=_auth(manage),
            json={"name": "esc", "scopes": scopes},
        )
        assert r.status_code == 403, scopes
    # manage 키로 리소스 스코프 발급은 가능
    r = client.post(
        "/api/v1/auth/api-keys",
        headers=_auth(manage),
        json={"name": "sub", "scopes": ["runs:read"]},
    )
    assert r.status_code == 201
    store.close()


def test_create_key_rejects_unknown_scope(monkeypatch):
    client, store = _client(monkeypatch)
    _admin_client(client, store)
    r = _create_key(client, scopes=["runs:read", "evil:everything"])
    assert r.status_code == 422
    assert "evil:everything" in r.json()["error"]["message"]
    store.close()


def test_create_key_rejects_non_list_scopes(monkeypatch):
    client, store = _client(monkeypatch)
    _admin_client(client, store)
    r = client.post("/api/v1/auth/api-keys", json={"scopes": "runs:read"})
    assert r.status_code == 422
    store.close()


def test_create_key_expiry_validation(monkeypatch):
    client, store = _client(monkeypatch)
    _admin_client(client, store)
    assert _create_key(client, expires_in_days=0).status_code == 422
    assert _create_key(client, expires_in_days=366).status_code == 422
    assert _create_key(client, expires_in_days=30).status_code == 201
    store.close()


def test_create_key_rate_limited(monkeypatch):
    """분당 10회 초과 발급은 429."""
    client, store = _client(monkeypatch)
    _admin_client(client, store)
    codes = [_create_key(client).status_code for _ in range(12)]
    assert 429 in codes
    assert codes.count(201) == 10
    store.close()


# ---- 목록 ----


def test_list_keys_member_sees_own_only_and_no_hash(monkeypatch):
    client, store = _client(monkeypatch)
    _mkuser(store, "a@b.com")
    _mkuser(store, "b@b.com")
    _admin_client(client, store, "admin@b.com")
    # admin이 a/b 각각 소유 키 발급
    ka = client.post(
        "/api/v1/auth/api-keys",
        json={"name": "ka", "scopes": [], "owner_user_id": _user_id(store, "a@b.com")},
    ).json()
    kb = client.post(
        "/api/v1/auth/api-keys",
        json={"name": "kb", "scopes": [], "owner_user_id": _user_id(store, "b@b.com")},
    ).json()
    # a로 로그인 → 본인 키 1개만
    _login(client, "a@b.com")
    keys = client.get("/api/v1/auth/api-keys").json()["keys"]
    assert [k["id"] for k in keys] == [ka["id"]]
    # b로 로그인 → 본인 키 1개만
    _login(client, "b@b.com")
    keys = client.get("/api/v1/auth/api-keys").json()["keys"]
    assert [k["id"] for k in keys] == [kb["id"]]
    # key_hash/원문이 응답에 없어야 한다
    for k in keys:
        assert "key_hash" not in k and "key" not in k
    store.close()


def test_list_keys_admin_sees_all(monkeypatch):
    client, store = _client(monkeypatch)
    _mkuser(store, "a@b.com")
    _mkuser(store, "b@b.com", admin=True)
    _login(client, "b@b.com")
    # b(admin)가 a 소유 키 발급
    r = client.post(
        "/api/v1/auth/api-keys",
        json={"name": "ka", "scopes": [], "owner_user_id": _user_id(store, "a@b.com")},
    )
    assert r.status_code == 201
    _create_key(client, scopes=[], name="kb")  # b 본인 키
    keys = client.get("/api/v1/auth/api-keys").json()["keys"]
    owners = {k["owner_user_id"] for k in keys}
    assert _user_id(store, "a@b.com") in owners and _user_id(store, "b@b.com") in owners
    store.close()


# ---- 스코프 인가 ----


def test_scoped_key_allows_matching_read_denies_write(monkeypatch):
    client, store = _client(monkeypatch)
    _admin_client(client, store)
    raw = _create_key(client, scopes=["runs:read"]).json()["key"]
    client.cookies.clear()  # 세션 없이 키만으로 인증
    # runs:read → GET 허용, POST 거부
    assert client.get("/api/v1/runs", headers=_auth(raw)).status_code == 200
    r = client.post(
        "/api/v1/runs",
        headers=_auth(raw),
        json={"goal": "x", "workflow": "dev_review"},
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "insufficient_scope"
    # runs:read만으로 다른 리소스도 거부
    assert client.get("/api/v1/usage/summary", headers=_auth(raw)).status_code == 403
    store.close()


def test_bearer_token_variant(monkeypatch):
    """Authorization: Bearer jm_… 도 X-API-Key와 동일하게 동작."""
    client, store = _client(monkeypatch)
    _admin_client(client, store)
    raw = _create_key(client, scopes=["runs:read"]).json()["key"]
    client.cookies.clear()
    h = {"Authorization": f"Bearer {raw}"}
    assert client.get("/api/v1/runs", headers=h).status_code == 200
    r = client.post("/api/v1/runs", headers=h, json={"goal": "x", "workflow": "dev_review"})
    assert r.status_code == 403
    store.close()


def test_empty_scope_key_denied_by_default(monkeypatch):
    """scopes=[] 키는 모든 /api/v1/* 쓰기·읽기 모두 403(deny-by-default)."""
    client, store = _client(monkeypatch)
    _admin_client(client, store)
    raw = _create_key(client, scopes=[]).json()["key"]
    client.cookies.clear()
    assert client.get("/api/v1/runs", headers=_auth(raw)).status_code == 403
    assert client.get("/api/v1/usage/summary", headers=_auth(raw)).status_code == 403
    store.close()


def test_cross_resource_write_denied(monkeypatch):
    """runs:write 키는 runs 외 리소스 쓰기도 403."""
    client, store = _client(monkeypatch)
    _admin_client(client, store)
    raw = _create_key(client, scopes=["runs:write"]).json()["key"]
    client.cookies.clear()
    # runs:write는 GET(read)이 없으므로 GET도 403
    assert client.get("/api/v1/runs", headers=_auth(raw)).status_code == 403
    # 다른 리소스 쓰기 — projects 등
    r = client.post(
        "/api/v1/projects",
        headers=_auth(raw),
        json={"name": "x"},
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "insufficient_scope"
    store.close()


def test_scoped_key_write_scope_allows_post(monkeypatch):
    client, store = _client(monkeypatch)
    _admin_client(client, store)
    raw = _create_key(client, scopes=["runs:write"]).json()["key"]
    client.cookies.clear()
    r = client.post(
        "/api/v1/runs",
        headers=_auth(raw),
        json={"goal": "x", "workflow": "dev_review"},
    )
    # 스코프 검사를 통과해 핸들러까지 도달해야 한다(403 insufficient_scope만 제외)
    assert r.status_code != 403 or r.json().get("error", {}).get("code") != "insufficient_scope"
    assert r.status_code in (200, 201, 202, 400, 404, 409, 422)
    store.close()


def test_star_scope_key_full_access(monkeypatch):
    client, store = _client(monkeypatch)
    _admin_client(client, store)
    raw = _create_key(client, scopes=["*"]).json()["key"]
    client.cookies.clear()
    assert client.get("/api/v1/runs", headers=_auth(raw)).status_code == 200
    assert client.get("/api/v1/usage/summary", headers=_auth(raw)).status_code == 200
    store.close()


def test_admin_scope_key_passes_admin_gate(monkeypatch):
    """admin 스코프 키는 _admin_user/_lead_user 게이트를 통과한다."""
    client, store = _client(monkeypatch)
    _admin_client(client, store)
    raw = _create_key(client, scopes=["admin"]).json()["key"]
    client.cookies.clear()
    # lead 이상 필요 엔드포인트 — audit 목록 등
    r = client.get("/api/v1/audit", headers=_auth(raw))
    assert r.status_code == 200
    store.close()


def test_scoped_key_cannot_manage_keys_without_manage_scope(monkeypatch):
    """일반 스코프 키는 /auth/api-keys 관리에 접근 불가."""
    client, store = _client(monkeypatch)
    _admin_client(client, store)
    # * 키(관리 스코프 아님)로 관리 시도 → 401
    raw = _create_key(client, scopes=["*"]).json()["key"]
    client.cookies.clear()
    rr = client.post("/api/v1/auth/api-keys", headers=_auth(raw), json={"scopes": []})
    assert rr.status_code == 401
    store.close()


def test_manage_scope_key_can_manage_keys(monkeypatch):
    client, store = _client(monkeypatch)
    _admin_client(client, store)
    raw = _create_key(client, scopes=["api_keys:manage"]).json()["key"]
    client.cookies.clear()
    r = client.post(
        "/api/v1/auth/api-keys",
        headers=_auth(raw),
        json={"name": "sub", "scopes": ["runs:read"]},
    )
    assert r.status_code == 201
    # 목록도 전체 조회 가능
    keys = client.get("/api/v1/auth/api-keys", headers=_auth(raw)).json()["keys"]
    assert any(k["key_prefix"] for k in keys)
    # manage 키로 타인 키 폐기도 가능
    victim = r.json()
    rd = client.delete(f"/api/v1/auth/api-keys/{victim['id']}", headers=_auth(raw))
    assert rd.status_code == 200
    store.close()


# ---- 폐기·만료 ----


def test_revoked_key_rejected(monkeypatch):
    client, store = _client(monkeypatch)
    _admin_client(client, store)
    # 폐기 전 원문은 발급 응답에만 있으므로 키를 발급받아 세션으로 폐기한다
    k2 = _create_key(client, scopes=["runs:read"]).json()
    # 폐기 전에는 동작 확인(전제조건)
    assert client.get("/api/v1/runs", headers=_auth(k2["key"])).status_code == 200
    assert client.delete(f"/api/v1/auth/api-keys/{k2['id']}").status_code == 200
    client.cookies.clear()
    assert client.get("/api/v1/runs", headers=_auth(k2["key"])).status_code == 401
    store.close()


def test_expired_key_rejected(monkeypatch):
    client, store = _client(monkeypatch)
    _admin_client(client, store)
    k = _create_key(client, scopes=["runs:read"], expires_in_days=1).json()
    # 폐기·만료 전에는 동작 확인(전제조건)
    assert client.get("/api/v1/runs", headers=_auth(k["key"])).status_code == 200
    # expires_at을 과거로 강제
    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    with store.tx() as c:
        c.execute("UPDATE api_keys SET expires_at=? WHERE id=?", (past, k["id"]))
    client.cookies.clear()
    assert client.get("/api/v1/runs", headers=_auth(k["key"])).status_code == 401
    store.close()


def test_revoke_requires_ownership_or_admin(monkeypatch):
    """member는 본인 키만 폐기 가능 — 타인 키는 404."""
    client, store = _client(monkeypatch)
    _mkuser(store, "a@b.com")
    _mkuser(store, "b@b.com")
    _admin_client(client, store, "admin@b.com")
    # admin이 a 소유 키 발급
    ka = client.post(
        "/api/v1/auth/api-keys",
        json={"name": "ka", "scopes": [], "owner_user_id": _user_id(store, "a@b.com")},
    ).json()
    # b가 a의 키 폐기 시도 → 404 (존재 자체를 숨김)
    _login(client, "b@b.com")
    r = client.delete(f"/api/v1/auth/api-keys/{ka['id']}")
    assert r.status_code == 404
    # a는 본인 키 폐기 가능
    _login(client, "a@b.com")
    assert client.delete(f"/api/v1/auth/api-keys/{ka['id']}").status_code == 200
    store.close()


def test_last_used_at_updated(monkeypatch):
    client, store = _client(monkeypatch)
    _admin_client(client, store)
    k = _create_key(client, scopes=["runs:read"]).json()
    client.cookies.clear()
    client.get("/api/v1/runs", headers=_auth(k["key"]))
    row = store._conn.execute("SELECT last_used_at FROM api_keys WHERE id=?", (k["id"],)).fetchone()
    assert row["last_used_at"]
    store.close()


def test_env_api_key_still_works(monkeypatch):
    """레거시 JOBMATE_API_KEY는 스코프 없이 전체 접근 유지."""
    client, store = _client(monkeypatch)
    assert client.get("/api/v1/runs", headers={"X-API-Key": "test-api-key"}).status_code == 200
    store.close()


def test_audit_events_for_key_lifecycle(monkeypatch):
    client, store = _client(monkeypatch)
    _admin_client(client, store)
    k = _create_key(client, scopes=["runs:read"]).json()
    client.delete(f"/api/v1/auth/api-keys/{k['id']}")
    actions = [a["action"] for a in store.list_audit(limit=500)]
    assert "auth_api_key_created" in actions
    assert "auth_api_key_revoked" in actions
    store.close()
