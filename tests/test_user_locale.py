"""spec 006 FR-028 — 사용자별 UI 언어(users.locale) 영속화 테스트.

PATCH /api/v1/me/locale 저장, /auth/me 의 locale 반영, 세션 사용자 격리,
검증(ko|en|''만 허용), 감사 이벤트를 검증한다.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from jobmate.api.app import create_app
from jobmate.config import Settings
from jobmate.store.db import Store
from tests.integration.helpers import default_outputs, scripted_llm

_PW = "Str0ng#Passw0rd!"


def _env():
    db = str(Path(tempfile.mkdtemp()) / "t.db")
    settings = Settings(db_path=db, budget_max_tokens=100_000)
    store = Store(db)
    llm = scripted_llm(default_outputs())
    app = create_app(settings=settings, store=store, llm=llm, adapters={})
    return TestClient(app), store


def _signup_login(client, store, email):
    r = client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": _PW, "agree_terms": True},
    )
    assert r.status_code == 201, r.text
    with store.tx() as c:
        c.execute("UPDATE users SET email_verified=1 WHERE email=?", (email,))
    r = client.post("/api/v1/auth/login", json={"email": email, "password": _PW})
    assert r.status_code == 200, r.text


def test_locale_patch_and_me_roundtrip():
    client, store = _env()
    _signup_login(client, store, "a@x.com")

    # 초기값은 ''(미설정)
    me = client.get("/api/v1/auth/me").json()["user"]
    assert me["locale"] == ""

    r = client.patch("/api/v1/me/locale", json={"locale": "en"})
    assert r.status_code == 200, r.text
    assert r.json()["locale"] == "en"

    me = client.get("/api/v1/auth/me").json()["user"]
    assert me["locale"] == "en"

    # ''로 되돌리면 자동 감지로 복귀
    r = client.patch("/api/v1/me/locale", json={"locale": ""})
    assert r.status_code == 200, r.text
    assert client.get("/api/v1/auth/me").json()["user"]["locale"] == ""


def test_locale_validation():
    client, store = _env()
    _signup_login(client, store, "a@x.com")

    for bad in ("fr", "KO", "en-US", None, 1, True):
        r = client.patch("/api/v1/me/locale", json={"locale": bad})
        assert r.status_code == 422, (bad, r.status_code, r.text)
    assert client.get("/api/v1/auth/me").json()["user"]["locale"] == ""


def test_locale_requires_session():
    client, _store = _env()
    assert client.patch("/api/v1/me/locale", json={"locale": "en"}).status_code == 401


def test_locale_is_per_user():
    client, store = _env()
    _signup_login(client, store, "a@x.com")
    client.patch("/api/v1/me/locale", json={"locale": "en"})
    client.post("/api/v1/auth/logout")

    _signup_login(client, store, "b@x.com")
    me = client.get("/api/v1/auth/me").json()["user"]
    assert me["locale"] == ""


def test_locale_audit_event():
    client, store = _env()
    _signup_login(client, store, "a@x.com")
    client.patch("/api/v1/me/locale", json={"locale": "ko"})

    rows = store.list_audit(limit=50, include_platform=True)
    hits = [r for r in rows if r.get("action") == "me_locale_update"]
    assert hits, "me_locale_update 감사 이벤트가 기록되어야 한다"
    assert hits[0]["target"]
