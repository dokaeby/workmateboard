"""GET /api/v1/messages source_text 마스킹 — 모든 provider 원문은 lead 이상만."""

import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from jobmate.api.app import create_app
from jobmate.config import Settings
from jobmate.security import SESSION_COOKIE, hash_token, new_session_token
from jobmate.store.db import Store
from tests.integration.helpers import default_outputs, scripted_llm


def _client(monkeypatch):
    """인증 활성 모드(Vercel 시뮬레이션) — 세션 사용자로만 /api/* 접근."""
    monkeypatch.setenv("VERCEL", "1")
    db = str(Path(tempfile.mkdtemp()) / "t.db")
    settings = Settings(db_path=db, budget_max_tokens=100_000)
    store = Store(db)
    app = create_app(settings=settings, store=store, llm=scripted_llm(default_outputs()))
    return TestClient(app), store


def _session_client(client, store, email, role=None):
    user = store.create_user(email, "", email)
    if role:
        store.set_user_role(user["id"], role)
    token = new_session_token()
    store.create_session(user["id"], hash_token(token))
    client.cookies.set(SESSION_COOKIE, token)
    return user


def _seed(store):
    store.save_message_extraction(
        "gmail", "g1", "t", {}, "pending", source_text="gmail secret body"
    )
    store.save_message_extraction(
        "slack", "s1", "t", {}, "pending", source_text="slack secret body"
    )
    store.save_message_extraction(
        "telegram", "t1", "t", {}, "pending", source_text="telegram secret body"
    )


def test_member_sees_no_source_text(monkeypatch):
    client, store = _client(monkeypatch)
    _seed(store)
    _session_client(client, store, "member@b.com")

    r = client.get("/api/v1/messages")
    assert r.status_code == 200
    msgs = {m["source_provider"]: m for m in r.json()["messages"]}
    # gmail 포함 모든 provider의 원문은 member에게 마스킹된다
    assert msgs["gmail"]["source_text"] == ""
    assert msgs["slack"]["source_text"] == ""
    assert msgs["telegram"]["source_text"] == ""

    # provider=gmail 지정 조회도 동일
    r = client.get("/api/v1/messages?provider=gmail")
    assert r.json()["messages"][0]["source_text"] == ""

    # provider=slack 지정 조회도 member에게는 마스킹
    r = client.get("/api/v1/messages?provider=slack")
    assert r.json()["messages"][0]["source_text"] == ""


def test_lead_sees_all_source_text(monkeypatch):
    client, store = _client(monkeypatch)
    _seed(store)
    _session_client(client, store, "lead@b.com", role="lead")

    msgs = client.get("/api/v1/messages").json()["messages"]
    texts = {m["source_provider"]: m["source_text"] for m in msgs}
    assert texts["slack"] == "slack secret body"
    assert texts["telegram"] == "telegram secret body"
    assert texts["gmail"] == "gmail secret body"


def test_unauthenticated_rejected(monkeypatch):
    """VERCEL 모드에서 비로그인은 미들웨어가 401 — source_text 노출 없음."""
    client, store = _client(monkeypatch)
    _seed(store)
    assert client.get("/api/v1/messages").status_code == 401


def test_limit_clamped(monkeypatch):
    client, store = _client(monkeypatch)
    _session_client(client, store, "lead2@b.com", role="lead")
    for i in range(3):
        store.save_message_extraction("slack", f"s{i}", "t", {}, "pending", source_text="x")
    # limit=5000도 200으로 클램프 — 오류 없이 응답
    r = client.get("/api/v1/messages?limit=5000")
    assert r.status_code == 200
    assert len(r.json()["messages"]) == 3
