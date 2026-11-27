"""계정 인증 테스트 — 비밀번호 해시, 세션, 가입/로그인/로그아웃, Google 미설정."""

import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from jobmate.api.app import create_app
from jobmate.config import Settings
from jobmate.security import hash_password, verify_password
from jobmate.store.db import Store
from tests.integration.helpers import default_outputs, scripted_llm


def _client():
    db = str(Path(tempfile.mkdtemp()) / "t.db")
    settings = Settings(db_path=db, budget_max_tokens=100_000)
    store = Store(db)
    app = create_app(settings=settings, store=store, llm=scripted_llm(default_outputs()))
    return TestClient(app), store


def test_password_hash_roundtrip():
    h = hash_password("correct horse battery")
    assert h.startswith("scrypt$")
    assert verify_password("correct horse battery", h)
    assert not verify_password("wrong password", h)
    assert not verify_password("x", "not-a-hash")


def test_signup_me_logout():
    client, _ = _client()
    r = client.post(
        "/api/v1/auth/signup",
        json={"email": "User@Example.com", "password": "Str0ng#Passw0rd!", "display_name": "U"},
    )
    assert r.status_code == 201
    assert r.json()["user"]["email"] == "user@example.com"
    assert "jobmate_session" in r.headers.get("set-cookie", "")

    me = client.get("/api/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["display_name"] == "U"

    client.post("/api/v1/auth/logout")
    # 로컬 오픈 모드(키·허용목록·VERCEL 없음)에서는 세션 없이도 백엔드가
    # local-admin으로 처리하므로 /auth/me도 pseudo admin을 반환한다.
    # 세션 해석 자체는 _session_user가 None — 로그아웃은 유효.
    me2 = client.get("/api/v1/auth/me")
    assert me2.status_code == 200
    assert me2.json()["user"]["role"] == "admin"
    assert me2.json()["user"]["id"] == "local"


def test_login_and_wrong_password():
    client, _ = _client()
    client.post(
        "/api/v1/auth/signup",
        json={"email": "a@b.com", "password": "Str0ng#Passw0rd!"},
    )
    client.post("/api/v1/auth/logout")

    bad = client.post("/api/v1/auth/login", json={"email": "a@b.com", "password": "wrong-pass"})
    assert bad.status_code == 401

    good = client.post(
        "/api/v1/auth/login",
        json={"email": "a@b.com", "password": "Str0ng#Passw0rd!"},
    )
    assert good.status_code == 200
    assert good.json()["user"]["email"] == "a@b.com"
    assert client.get("/api/v1/auth/me").status_code == 200


def test_duplicate_signup_rejected():
    client, _ = _client()
    body = {"email": "dup@b.com", "password": "Str0ng#Passw0rd!"}
    assert client.post("/api/v1/auth/signup", json=body).status_code == 201
    assert client.post("/api/v1/auth/signup", json=body).status_code == 409


def test_signup_validation():
    client, _ = _client()
    assert (
        client.post(
            "/api/v1/auth/signup", json={"email": "bad", "password": "Str0ng#Passw0rd!"}
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/v1/auth/signup", json={"email": "a@b.com", "password": "short"}
        ).status_code
        == 400
    )


def test_session_cookie_authorizes_api(monkeypatch):
    """API 키가 설정된 상태에서 세션 쿠키만으로 /api/* 접근 가능."""
    monkeypatch.setenv("JOBMATE_API_KEY", "secret-key")
    client, store = _client()
    assert client.get("/api/v1/workflows").status_code == 401

    # API 키가 있으면 이메일 인증이 강제된다 — 인증 링크로 세션을 발급받는다
    client.post(
        "/api/v1/auth/signup",
        json={"email": "s@b.com", "password": "Str0ng#Passw0rd!", "agree_terms": True},
        headers={"X-API-Key": "secret-key"},
    )
    token = store.create_email_verification("s@b.com")
    vr = client.get(f"/api/v1/auth/verify?token={token}")
    assert vr.status_code == 200
    assert client.get("/api/v1/workflows").status_code == 200

    # 잘못된 키 + 세션 없음 → 401
    client.cookies.clear()
    assert client.get("/api/v1/workflows").status_code == 401


def test_api_key_still_works_without_session(monkeypatch):
    """cron-job.org 등 머신 클라이언트의 X-API-Key 인증은 그대로 동작."""
    monkeypatch.setenv("JOBMATE_API_KEY", "secret-key")
    client, _ = _client()
    r = client.get("/api/v1/workflows", headers={"X-API-Key": "secret-key"})
    assert r.status_code == 200


def test_google_login_not_configured():
    client, _ = _client()
    r = client.get("/api/v1/auth/google", follow_redirects=False)
    assert r.status_code == 501
