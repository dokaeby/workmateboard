"""agent-sessions 인사이트 — 전이 이벤트/이력/통계 API + 어텐션 푸시 + 종료 산출물."""

import sqlite3
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jobmate.api.app import _check_waiting_approvals, create_app
from jobmate.config import Settings
from jobmate.models import RunStatus
from jobmate.store.db import Store


def _client(api_key: str | None = "secret-key", adapters: dict | None = None):
    db = str(Path(tempfile.mkdtemp()) / "t.db")
    settings = Settings(db_path=db, budget_max_tokens=100_000)
    store = Store(db)
    app = create_app(settings=settings, store=store, llm=None, adapters=adapters or {})
    return TestClient(app), store


def _headers(key: str = "secret-key") -> dict[str, str]:
    return {"X-API-Key": key}


class FakePush:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send(self, *, title: str, body: str, source_url: str) -> None:
        self.sent.append({"title": title, "body": body, "source_url": source_url})


# ---- Store: 전이 이벤트 ----


def test_upsert_records_transition_events():
    store = Store(str(Path(tempfile.mkdtemp()) / "t.db"))
    store.upsert_agent_session("claude", "s1", "working", doing="edit a.py")
    store.upsert_agent_session("claude", "s1", "decision", ask="approve?")
    store.upsert_agent_session("claude", "s1", "working", doing="edit b.py")
    hist = store.agent_session_history("claude", "s1")
    assert [e["status"] for e in hist] == ["working", "decision", "working"]
    assert hist[1]["ask"] == "approve?"
    store.close()


def test_upsert_identical_repeat_adds_no_event():
    store = Store(str(Path(tempfile.mkdtemp()) / "t.db"))
    store.upsert_agent_session("codex", "s1", "working", doing="x")
    store.upsert_agent_session("codex", "s1", "working", doing="x")
    assert len(store.agent_session_history("codex", "s1")) == 1
    store.close()


def test_upsert_partial_none_keeps_values_in_event():
    store = Store(str(Path(tempfile.mkdtemp()) / "t.db"))
    store.upsert_agent_session("claude", "s1", "working", project="proj", doing="edit")
    # doing=None → COALESCE로 기존 값 유지, status만 변경 → 이벤트에도 doing 유지
    store.upsert_agent_session("claude", "s1", "rest")
    hist = store.agent_session_history("claude", "s1")
    assert hist[-1]["status"] == "rest"
    assert hist[-1]["doing"] == "edit"
    assert hist[-1]["project"] == "proj"
    store.close()


def test_ended_fields_stored_and_listed():
    store = Store(str(Path(tempfile.mkdtemp()) / "t.db"))
    store.upsert_agent_session(
        "devin",
        "s1",
        "ended",
        project="repo",
        ended_summary="HEAD abc1234 fix bug",
        artifacts=["commit abc1234 fix bug", "uncommitted 2"],
    )
    row = store.get_agent_session("devin", "s1")
    assert row is not None
    assert row["ended_summary"] == "HEAD abc1234 fix bug"
    listed = store.list_agent_sessions(include_ended=True)
    assert listed[0]["ended_summary"] == "HEAD abc1234 fix bug"
    store.close()


def test_migration_adds_columns_to_old_db(tmp_path):
    """컬럼 없는 구 스키마 DB를 열어도 ALTER로 보강된다."""
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE agent_sessions ("
        "id TEXT PRIMARY KEY, tool TEXT NOT NULL, session_key TEXT NOT NULL,"
        " project TEXT, title TEXT, status TEXT NOT NULL, doing TEXT, ask TEXT,"
        " last_event_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,"
        " UNIQUE(tool, session_key))"
    )
    con.commit()
    con.close()
    store = Store(str(db))
    store.upsert_agent_session("claude", "s1", "ended", ended_summary="sum", artifacts=["a1"])
    row = store.get_agent_session("claude", "s1")
    assert row is not None and row["ended_summary"] == "sum"
    store.close()


# ---- API: history / stats ----


def test_history_endpoint_chronological(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("JOBMATE_API_KEY", "secret-key")
    client, _ = _client()
    for status, ask in (("working", None), ("decision", "ok?"), ("ended", None)):
        client.post(
            "/api/v1/agent-sessions",
            json={"tool": "claude", "session_key": "s1", "status": status, "ask": ask},
            headers=_headers(),
        )
    r = client.get("/api/v1/agent-sessions/claude/s1/history", headers=_headers())
    assert r.status_code == 200
    events = r.json()["events"]
    assert [e["status"] for e in events] == ["working", "decision", "ended"]
    # 인증 필요
    assert client.get("/api/v1/agent-sessions/claude/s1/history").status_code == 401


def test_stats_endpoint(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("JOBMATE_API_KEY", "secret-key")
    client, _ = _client()
    client.post(
        "/api/v1/agent-sessions",
        json={"tool": "claude", "session_key": "s1", "status": "working"},
        headers=_headers(),
    )
    client.post(
        "/api/v1/agent-sessions",
        json={"tool": "claude", "session_key": "s1", "status": "decision", "ask": "?"},
        headers=_headers(),
    )
    client.post(
        "/api/v1/agent-sessions",
        json={"tool": "claude", "session_key": "s1", "status": "ended"},
        headers=_headers(),
    )
    client.post(
        "/api/v1/agent-sessions",
        json={"tool": "codex", "session_key": "c1", "status": "working"},
        headers=_headers(),
    )
    r = client.get("/api/v1/agent-sessions/stats", headers=_headers())
    assert r.status_code == 200
    tools = r.json()["tools"]
    assert tools["claude"]["sessions"] == 1
    assert tools["claude"]["decisions"] == 1
    assert tools["claude"]["ended"] == 1
    assert tools["claude"]["working_seconds"] >= 0
    assert tools["codex"]["sessions"] == 1
    assert tools["claude"]["last_active"]
    # since 필터 — 미래 날짜면 이벤트 집계 0
    r2 = client.get("/api/v1/agent-sessions/stats?since=2999-01-01", headers=_headers())
    assert r2.json()["tools"]["claude"]["decisions"] == 0


def test_list_include_ended_param(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("JOBMATE_API_KEY", "secret-key")
    client, _ = _client()
    client.post(
        "/api/v1/agent-sessions",
        json={"tool": "claude", "session_key": "s1", "status": "ended"},
        headers=_headers(),
    )
    assert client.get("/api/v1/agent-sessions", headers=_headers()).json() == []
    rows = client.get("/api/v1/agent-sessions?include_ended=true", headers=_headers()).json()
    assert len(rows) == 1 and rows[0]["status"] == "ended"


# ---- API: 어텐션 푸시 (decision/ask 전이) ----


def test_attention_push_on_decision_transition(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("JOBMATE_API_KEY", "secret-key")
    push = FakePush()
    client, _ = _client(adapters={"webpush": push})
    body = {"tool": "claude", "session_key": "s1", "status": "working"}
    client.post("/api/v1/agent-sessions", json=body, headers=_headers())
    assert push.sent == []
    # decision 전이 → 1회 발송
    client.post(
        "/api/v1/agent-sessions",
        json={"tool": "claude", "session_key": "s1", "status": "decision", "ask": "ok?"},
        headers=_headers(),
    )
    assert len(push.sent) == 1
    assert push.sent[0]["source_url"] == "/?view=office"
    # 동일 상태 반복 poll → 추가 발송 없음
    client.post(
        "/api/v1/agent-sessions",
        json={"tool": "claude", "session_key": "s1", "status": "decision", "ask": "ok?"},
        headers=_headers(),
    )
    assert len(push.sent) == 1
    # working → decision 재전이도 10분 레이트리밋으로 억제
    client.post("/api/v1/agent-sessions", json=body, headers=_headers())
    client.post(
        "/api/v1/agent-sessions",
        json={"tool": "claude", "session_key": "s1", "status": "decision", "ask": "again?"},
        headers=_headers(),
    )
    assert len(push.sent) == 1


def test_no_attention_push_for_ended_or_rest(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("JOBMATE_API_KEY", "secret-key")
    push = FakePush()
    client, _ = _client(adapters={"webpush": push})
    for status in ("rest", "ended"):
        client.post(
            "/api/v1/agent-sessions",
            json={"tool": "gemini", "session_key": f"g-{status}", "status": status},
            headers=_headers(),
        )
    assert push.sent == []


# ---- waiting_approval 워처 ----


def test_waiting_approval_push_only_on_transition():
    store = Store(str(Path(tempfile.mkdtemp()) / "t.db"))
    push = FakePush()
    run = store.create_run("dev_review", "1", {"task": "fix bug"}, trace_id="t1")
    store.update_run(run.id, status=RunStatus.WAITING_APPROVAL)
    _check_waiting_approvals(store, {"webpush": push})
    _check_waiting_approvals(store, {"webpush": push})  # 동일 대기 → 재발송 없음
    assert len(push.sent) == 1
    assert push.sent[0]["source_url"] == "/?view=office"
    assert "fix bug" in push.sent[0]["body"]
    # 승인 후 다시 대기 전이 → 새 지문이므로 재발송
    store.update_run(run.id, status=RunStatus.RUNNING)
    store.update_run(run.id, status=RunStatus.WAITING_APPROVAL)
    _check_waiting_approvals(store, {"webpush": push})
    assert len(push.sent) == 2
    store.close()
