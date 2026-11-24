"""agent-sessions — Store upsert/list + push/pull API 인증."""

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jobmate.api.app import create_app
from jobmate.config import Settings
from jobmate.store.db import Store


def _client(api_key: str | None = None):
    db = str(Path(tempfile.mkdtemp()) / "t.db")
    settings = Settings(db_path=db, budget_max_tokens=100_000)
    store = Store(db)
    app = create_app(settings=settings, store=store, llm=None, adapters={})
    return TestClient(app), store


def _headers(key: str = "secret-key") -> dict[str, str]:
    return {"X-API-Key": key}


# ---- Store 레벨 ----


def test_upsert_and_list():
    store = Store(str(Path(tempfile.mkdtemp()) / "t.db"))
    store.upsert_agent_session(
        "claude",
        "sess-1",
        "working",
        project="workmateboard",
        title="fix bug",
        doing="editing db.py",
        event_at="2026-11-22T01:00:00+00:00",
    )
    rows = store.list_agent_sessions()
    assert len(rows) == 1
    row = rows[0]
    assert row["tool"] == "claude"
    assert row["session_key"] == "sess-1"
    assert row["status"] == "working"
    assert row["project"] == "workmateboard"
    assert row["doing"] == "editing db.py"
    assert row["last_event_at"] == "2026-11-22T01:00:00+00:00"
    store.close()


def test_upsert_same_key_updates_row():
    store = Store(str(Path(tempfile.mkdtemp()) / "t.db"))
    store.upsert_agent_session("codex", "s1", "working", doing="first")
    store.upsert_agent_session(
        "codex", "s1", "decision", ask="run tests?", event_at="2026-11-22T02:00:00+00:00"
    )
    # 다른 tool/session_key 조합은 별도 행
    store.upsert_agent_session("gemini", "s1", "rest")
    rows = store.list_agent_sessions()
    assert len(rows) == 2
    codex = next(r for r in rows if r["tool"] == "codex")
    assert codex["status"] == "decision"
    assert codex["ask"] == "run tests?"
    # None 필드는 기존 값 유지 (COALESCE)
    assert codex["doing"] == "first"
    store.close()


def test_list_excludes_ended_by_default():
    store = Store(str(Path(tempfile.mkdtemp()) / "t.db"))
    store.upsert_agent_session("claude", "a", "working")
    store.upsert_agent_session("claude", "b", "ended")
    assert [r["session_key"] for r in store.list_agent_sessions()] == ["a"]
    assert len(store.list_agent_sessions(include_ended=True)) == 2
    store.close()


def test_list_orders_by_last_event_at_desc():
    store = Store(str(Path(tempfile.mkdtemp()) / "t.db"))
    store.upsert_agent_session("claude", "old", "working", event_at="2026-11-22T01:00:00")
    store.upsert_agent_session("claude", "new", "working", event_at="2026-11-22T03:00:00")
    store.upsert_agent_session("claude", "mid", "working", event_at="2026-11-22T02:00:00")
    keys = [r["session_key"] for r in store.list_agent_sessions()]
    assert keys == ["new", "mid", "old"]
    store.close()


# ---- API 레벨 ----


def test_post_requires_api_key(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("JOBMATE_API_KEY", "secret-key")
    client, _ = _client()
    body = {"tool": "claude", "session_key": "s1", "status": "working"}
    # 키 없음 → 미들웨어 401
    assert client.post("/api/v1/agent-sessions", json=body).status_code == 401
    # 잘못된 키 → 401
    assert (
        client.post("/api/v1/agent-sessions", json=body, headers=_headers("wrong")).status_code
        == 401
    )
    # 정상 키 → 200
    r = client.post("/api/v1/agent-sessions", json=body, headers=_headers())
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def _verify_session(client: TestClient, store: Store, email: str) -> None:
    """이메일 인증 강제 모드에서 signup 후 세션 쿠키를 발급받는다."""
    token = store.create_email_verification(email)
    assert client.get(f"/api/v1/auth/verify?token={token}").status_code == 200


def test_post_rejects_session_cookie_only(monkeypatch: pytest.MonkeyPatch):
    """세션 쿠키만으로는 머신 push 불가 — API 키 필수."""
    monkeypatch.setenv("JOBMATE_API_KEY", "secret-key")
    client, store = _client()
    client.post(
        "/api/v1/auth/signup",
        json={"email": "a@b.com", "password": "password123"},
        headers=_headers(),
    )
    _verify_session(client, store, "a@b.com")
    # 세션 쿠키로 일반 GET은 통과하지만 POST push는 403
    assert client.get("/api/v1/agent-sessions").status_code == 200
    r = client.post(
        "/api/v1/agent-sessions",
        json={"tool": "claude", "session_key": "s1", "status": "working"},
    )
    assert r.status_code == 403


def test_post_validation(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("JOBMATE_API_KEY", "secret-key")
    client, _ = _client()
    # status 화이트리스트 외 → 422
    r = client.post(
        "/api/v1/agent-sessions",
        json={"tool": "claude", "session_key": "s1", "status": "bogus"},
        headers=_headers(),
    )
    assert r.status_code == 422
    # tool/session_key 누락 → 422
    r = client.post(
        "/api/v1/agent-sessions",
        json={"tool": "claude", "status": "working"},
        headers=_headers(),
    )
    assert r.status_code == 422


def test_post_get_roundtrip(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("JOBMATE_API_KEY", "secret-key")
    client, _ = _client()
    client.post(
        "/api/v1/agent-sessions",
        json={
            "tool": "  codex  ",
            "session_key": "sess-9",
            "status": "decision",
            "project": "proj",
            "ask": "approve?",
            "event_at": "2026-11-22T05:00:00+00:00",
        },
        headers=_headers(),
    )
    client.post(
        "/api/v1/agent-sessions",
        json={"tool": "claude", "session_key": "sess-10", "status": "ended"},
        headers=_headers(),
    )
    rows = client.get("/api/v1/agent-sessions", headers=_headers()).json()
    assert len(rows) == 1
    row = rows[0]
    assert row["tool"] == "codex"  # strip 적용
    assert row["session_key"] == "sess-9"
    assert row["status"] == "decision"
    assert row["ask"] == "approve?"


def test_get_auth_policy_same_as_dashboard(monkeypatch: pytest.MonkeyPatch):
    """GET은 기존 /api/* 정책과 동일 — 키 없으면 401, 세션 쿠키/키면 200."""
    monkeypatch.setenv("JOBMATE_API_KEY", "secret-key")
    client, store = _client()
    assert client.get("/api/v1/agent-sessions").status_code == 401
    assert client.get("/api/v1/agent-sessions", headers=_headers()).status_code == 200
    # 세션 쿠키 인증도 허용 (대시보드와 동일)
    client.post(
        "/api/v1/auth/signup",
        json={"email": "g@b.com", "password": "password123"},
        headers=_headers(),
    )
    _verify_session(client, store, "g@b.com")
    assert client.get("/api/v1/agent-sessions").status_code == 200


def test_post_without_api_key_configured(monkeypatch: pytest.MonkeyPatch):
    """JOBMATE_API_KEY 미설정(로컬 개발)이면 인증 비활성 — push 허용."""
    monkeypatch.delenv("JOBMATE_API_KEY", raising=False)
    client, _ = _client()
    r = client.post(
        "/api/v1/agent-sessions",
        json={"tool": "devin", "session_key": "d1", "status": "rest"},
    )
    assert r.status_code == 200
