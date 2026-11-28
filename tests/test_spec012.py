"""spec 012 — 세션 필드 확장 + Tool 통계 + 조회 필터 백엔드 테스트.

- US1: agent_sessions 신규 컬럼(model/progress_pct/cost_usd/tool_calls/
  sub_agents) — 스키마/마이그레이션, upsert COALESCE, push 엔드포인트,
  /observability/agents/live 노출.
- US2: GET /api/v1/observability/tool-stats — external_spans 집계 shape.
- US3: GET /api/v1/runs 필터(status/days/q/agent),
  GET /api/v1/observability/external-logs q 파라미터.
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jobmate.api.app import create_app
from jobmate.config import Settings
from jobmate.models import RunStatus
from jobmate.store import orgctx
from jobmate.store.db import Store
from tests.integration.helpers import default_outputs, scripted_llm

_KEY = "secret-key"
_HEADERS = {"X-API-Key": _KEY}

_NEW_COLS = {"model", "progress_pct", "cost_usd", "tool_calls", "sub_agents"}


def _env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("JOBMATE_API_KEY", _KEY)
    db = str(Path(tempfile.mkdtemp()) / "t.db")
    settings = Settings(db_path=db, budget_max_tokens=100_000)
    store = Store(db)
    llm = scripted_llm(default_outputs())
    app = create_app(settings=settings, store=store, llm=llm, adapters={})
    return TestClient(app), store


def _oid(store: Store) -> str:
    return str(store.list_organizations()[0]["id"])


def _session_cols(store: Store) -> set[str]:
    rows = store._conn.execute("PRAGMA table_info(agent_sessions)").fetchall()
    return {str(r["name"]) for r in rows}


# ---------------- US1: 스키마/마이그레이션 ----------------


def test_session_new_columns_on_fresh_db():
    with tempfile.TemporaryDirectory() as td:
        store = Store(Path(td) / "t.db")
        try:
            assert _NEW_COLS <= _session_cols(store)
        finally:
            store.close()


def test_session_columns_migrated_on_existing_db():
    """spec 012 이전 형태의 agent_sessions만 있는 기존 DB → Store 기동 시
    _ensure_columns가 신규 컬럼을 ALTER로 보강한다."""
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "old.db"
        conn = sqlite3.connect(path)
        # 신규 컬럼이 없는 구 스키마 형태(UNIQUE(org_id,...) — 리스코프 불필요).
        conn.executescript(
            """
            CREATE TABLE agent_sessions (
              id TEXT PRIMARY KEY,
              tool TEXT NOT NULL,
              session_key TEXT NOT NULL,
              project TEXT,
              title TEXT,
              status TEXT NOT NULL,
              doing TEXT,
              ask TEXT,
              ended_summary TEXT,
              artifacts TEXT,
              machine TEXT,
              tokens_in INTEGER,
              tokens_out INTEGER,
              last_event_at TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              org_id TEXT NOT NULL DEFAULT '',
              UNIQUE(org_id, tool, session_key)
            );
            """
        )
        conn.commit()
        conn.close()
        store = Store(path)
        try:
            cols = _session_cols(store)
            assert _NEW_COLS <= cols
            # 마이그레이션 후 upsert가 신규 컬럼에 쓸 수 있어야 한다
            with orgctx.org_scope(_oid(store)):
                store.upsert_agent_session(
                    "claude", "old-1", "working", model="m1", progress_pct=42.5
                )
            row = store.get_agent_session("claude", "old-1")
            assert row is not None and row["model"] == "m1"
        finally:
            store.close()


# ---------------- US1: upsert / 뷰 ----------------


def test_upsert_session_fields_stored_and_coalesced(monkeypatch: pytest.MonkeyPatch):
    _client, store = _env(monkeypatch)
    oid = _oid(store)
    with orgctx.org_scope(oid):
        store.upsert_agent_session(
            "claude",
            "sess-f",
            "working",
            model="claude-opus-4",
            progress_pct=35.0,
            cost_usd=1.25,
            tool_calls=7,
            sub_agents=2,
        )
        row = store.get_agent_session("claude", "sess-f")
        assert row is not None
        assert row["model"] == "claude-opus-4"
        assert row["progress_pct"] == pytest.approx(35.0)
        assert row["cost_usd"] == pytest.approx(1.25)
        assert row["tool_calls"] == 7
        assert row["sub_agents"] == 2

        # None 필드는 기존 값을 유지한다(COALESCE)
        store.upsert_agent_session("claude", "sess-f", "working", doing="still going")
        row = store.get_agent_session("claude", "sess-f")
        assert row is not None
        assert row["model"] == "claude-opus-4"
        assert row["progress_pct"] == pytest.approx(35.0)
        assert row["tool_calls"] == 7

        # 제공된 값은 최신 상태로 덮어쓴다(MAX 누적이 아니다)
        store.upsert_agent_session("claude", "sess-f", "working", progress_pct=80.0, tool_calls=3)
        row = store.get_agent_session("claude", "sess-f")
        assert row is not None
        assert row["progress_pct"] == pytest.approx(80.0)
        assert row["tool_calls"] == 3  # 7 → 3 감소도 반영
        assert row["cost_usd"] == pytest.approx(1.25)  # 미제공은 유지

        # 미전송 세션은 전부 null — 추정 금지
        store.upsert_agent_session("codex", "sess-n", "rest")
        row = store.get_agent_session("codex", "sess-n")
        assert row is not None
        for col in _NEW_COLS:
            assert row[col] is None


def test_push_agent_session_accepts_new_fields(monkeypatch: pytest.MonkeyPatch):
    client, store = _env(monkeypatch)
    payload = {
        "tool": "claude",
        "session_key": "push-1",
        "status": "working",
        "title": "t",
        "model": "claude-sonnet-4",
        "progress_pct": 55.5,
        "cost_usd": 0.5,
        "tool_calls": 12,
        "sub_agents": 1,
    }
    r = client.post("/api/v1/agent-sessions", json=payload, headers=_HEADERS)
    assert r.status_code == 200, r.text

    # GET /agent-sessions 뷰에도 필드가 노출된다
    r = client.get("/api/v1/agent-sessions?include_ended=true", headers=_HEADERS)
    assert r.status_code == 200, r.text
    items = {i["session_key"]: i for i in r.json()}
    s = items["push-1"]
    assert s["model"] == "claude-sonnet-4"
    assert s["progress_pct"] == pytest.approx(55.5)
    assert s["cost_usd"] == pytest.approx(0.5)
    assert s["tool_calls"] == 12
    assert s["sub_agents"] == 1

    # /observability/agents/live 카드에도 실제 컬럼 값이 나온다
    r = client.get("/api/v1/observability/agents/live", headers=_HEADERS)
    assert r.status_code == 200, r.text
    live = {i["id"]: i for i in r.json()["items"]}
    card = live["claude:push-1"]
    assert card["model"] == "claude-sonnet-4"
    assert card["progress_pct"] == pytest.approx(55.5)
    assert card["cost_usd"] == pytest.approx(0.5)
    assert card["tool_calls"] == 12
    assert card["sub_agents"] == 1


def test_push_agent_session_validation(monkeypatch: pytest.MonkeyPatch):
    client, _store = _env(monkeypatch)
    base = {"tool": "claude", "session_key": "v-1", "status": "working"}
    # progress_pct 범위 밖 / 음수 카운터는 422
    for bad in (
        {"progress_pct": 101},
        {"progress_pct": -1},
        {"tool_calls": -1},
        {"sub_agents": -2},
        {"cost_usd": -0.1},
    ):
        r = client.post("/api/v1/agent-sessions", json={**base, **bad}, headers=_HEADERS)
        assert r.status_code == 422, (bad, r.text)


# ---------------- US2: tool-stats ----------------


def _seed_spans(store: Store, oid: str) -> None:
    """tool-stats 윈도우(days=7) 안에 들어오도록 현재 시각 기준 타임스탬프."""
    from datetime import UTC, datetime, timedelta

    base = datetime.now(UTC)

    def _ts(sec: float) -> str:
        return (base - timedelta(seconds=sec)).isoformat()

    with orgctx.org_scope(oid):
        store.insert_external_spans(
            [
                {
                    "trace_id": "t1",
                    "span_id": "s1",
                    "name": "file.read",
                    "kind": "tool",
                    "started_at": _ts(40),
                    "ended_at": _ts(39.9),  # 100ms
                    "attributes": {},
                },
                {
                    "trace_id": "t1",
                    "span_id": "s2",
                    "name": "file.read",
                    "kind": "tool",
                    "started_at": _ts(30),
                    "ended_at": _ts(29.7),  # 300ms
                    "attributes": {"error": "ENOENT"},
                },
                {
                    "trace_id": "t1",
                    "span_id": "s3",
                    "name": "llm.chat",
                    "kind": "llm",
                    "started_at": _ts(20),
                    "ended_at": _ts(18),  # 2000ms
                    "attributes": {},
                },
                {
                    "trace_id": "t1",
                    "span_id": "s4",
                    "name": "mystery",
                    "kind": "",
                    "started_at": _ts(10),
                    "ended_at": None,
                    "attributes": {"exception.type": "Timeout"},
                },
            ]
        )


def test_tool_stats_aggregate_shape(monkeypatch: pytest.MonkeyPatch):
    client, store = _env(monkeypatch)
    oid = _oid(store)
    _seed_spans(store, oid)

    r = client.get("/api/v1/observability/tool-stats?days=7", headers=_HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["days"] == 7
    assert body["total_calls"] == 4

    by_name = {i["name"]: i for i in body["by_name"] if i["name"] != "기타"}
    fr = by_name["file.read"]
    assert fr["kind"] == "tool"
    assert fr["count"] == 2
    assert fr["error_count"] == 1  # attributes.error=ENOENT
    # (100ms + 300ms)/2 = 200ms
    assert fr["avg_duration_ms"] == pytest.approx(200.0)
    llm = by_name["llm.chat"]
    assert llm["kind"] == "llm"
    assert llm["avg_duration_ms"] == pytest.approx(2000.0)
    # kind 없는 스팬은 '' 그룹 — 추정하지 않는다
    mystery = by_name["mystery"]
    assert mystery["kind"] == ""
    assert mystery["error_count"] == 1  # exception.type
    assert mystery["avg_duration_ms"] is None  # ended_at 없음

    by_kind = {i["kind"]: i["count"] for i in body["by_kind"]}
    assert by_kind == {"tool": 2, "llm": 1, "": 1}
    assert body["error_rate"] == pytest.approx(0.5)


def test_tool_stats_empty_and_lead_gate(monkeypatch: pytest.MonkeyPatch):
    client, _store = _env(monkeypatch)
    r = client.get("/api/v1/observability/tool-stats?days=7", headers=_HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_calls"] == 0
    assert body["by_name"] == []
    assert body["by_kind"] == []
    assert body["error_rate"] == 0.0
    # 인증 없음 → 401, member → lead 게이트
    assert client.get("/api/v1/observability/tool-stats").status_code == 401


# ---------------- US3: runs 필터 ----------------


def _seed_runs(store: Store, oid: str) -> dict[str, str]:
    ids: dict[str, str] = {}
    with orgctx.org_scope(oid):
        agent = store.create_agent("builder-bot", "script")
        assert agent is not None
        r1 = store.create_run(
            "dev_review",
            "0.1.0",
            {"task": "alpha task", "project": "projA"},
            trace_id="r1",
            agent_id=agent["id"],
        )
        store.update_run(r1.id, status=RunStatus.COMPLETED)
        ids["done"] = r1.id
        r2 = store.create_run(
            "report_gen",
            "0.1.0",
            {"task": "beta task", "project": "projB"},
            trace_id="r2",
        )
        store.update_run(r2.id, status=RunStatus.FAILED, error="boom")
        ids["failed"] = r2.id
        r3 = store.create_run(
            "dev_review",
            "0.1.0",
            {"task": "gamma task", "project": "projA"},
            trace_id="r3",
            agent_id=agent["id"],
        )
        store.update_run(r3.id, status=RunStatus.RUNNING)
        ids["running"] = r3.id
        # r3을 10일 전으로 — days 필터 검증용
        with store.tx() as c:
            c.execute(
                "UPDATE runs SET created_at='2020-01-01T00:00:00+00:00' WHERE id=?",
                (r3.id,),
            )
    return ids


def test_runs_filters(monkeypatch: pytest.MonkeyPatch):
    client, store = _env(monkeypatch)
    oid = _oid(store)
    ids = _seed_runs(store, oid)

    def _ids(params: str) -> set[str]:
        r = client.get(f"/api/v1/runs?{params}", headers=_HEADERS)
        assert r.status_code == 200, r.text
        return {i["id"] for i in r.json()}

    assert _ids("") == {ids["done"], ids["failed"], ids["running"]}
    # status — 정확 일치
    assert _ids("status=completed") == {ids["done"]}
    assert _ids("status=failed") == {ids["failed"]}
    # days — 10일 전(실제로는 2020년) run은 365일 클램프 상한에서도 제외
    assert _ids("days=7") == {ids["done"], ids["failed"]}
    assert _ids("days=3650") == {ids["done"], ids["failed"]}  # 365로 클램프
    # q — workflow_key/project/input 부분일치 (runs에 title 컬럼은 없음)
    assert _ids("q=dev_review") == {ids["done"], ids["running"]}
    assert _ids("q=projB") == {ids["failed"]}
    assert _ids("q=beta%20task") == {ids["failed"]}
    assert _ids("q=nope") == set()
    # agent — agent_id 또는 agents.name
    agent_rows = [a for a in store.list_agents() if a.get("name") == "builder-bot"]
    assert agent_rows, "seed agent missing"
    agent_id = agent_rows[0]["id"]
    assert _ids(f"agent={agent_id}") == {ids["done"], ids["running"]}
    assert _ids("agent=builder-bot") == {ids["done"], ids["running"]}
    assert _ids("agent=other-bot") == set()
    # AND 결합 — status+workflow+days+q+agent
    assert _ids("status=completed&workflow=dev_review&agent=builder-bot&days=7") == {ids["done"]}
    assert _ids("status=completed&workflow=report_gen") == set()
    # 기존 workflow/limit 동작 유지
    assert _ids("workflow=dev_review") == {ids["done"], ids["running"]}


# ---------------- US3: external-logs q ----------------


def test_external_logs_q_filter(monkeypatch: pytest.MonkeyPatch):
    from datetime import UTC, datetime, timedelta

    client, store = _env(monkeypatch)
    oid = _oid(store)
    base = datetime.now(UTC)

    def _ts(sec: float) -> str:
        return (base - timedelta(seconds=sec)).isoformat()

    with orgctx.org_scope(oid):
        store.insert_external_logs(
            [
                {
                    "trace_id": "lt1",
                    "span_id": "a",
                    "severity": "ERROR",
                    "body": "disk full on node-1",
                    "attributes": {"severityText": "DiskFull", "k": "v"},
                    "timestamp": _ts(30),
                },
                {
                    "trace_id": "lt1",
                    "span_id": "b",
                    "severity": "INFO",
                    "body": "routine heartbeat ok",
                    "attributes": {"severityText": "Info"},
                    "timestamp": _ts(20),
                },
                {
                    "trace_id": "lt1",
                    "span_id": "c",
                    "severity": "WARN",
                    "body": "100% cpu spike",
                    "attributes": {},
                    "timestamp": _ts(10),
                },
            ]
        )

    def _bodies(params: str) -> set[str]:
        r = client.get(f"/api/v1/observability/external-logs?{params}", headers=_HEADERS)
        assert r.status_code == 200, r.text
        return {i["body"] for i in r.json()["items"]}

    # body 부분일치
    assert _bodies("q=disk%20full") == {"disk full on node-1"}
    # severityText(attributes JSON 내부) 부분일치
    assert _bodies("q=DiskFull") == {"disk full on node-1"}
    # LIKE 특수문자 %는 리터럴로 — '100% cpu' 본문만 매칭
    assert _bodies("q=100%25") == {"100% cpu spike"}
    # 매칭 없음
    assert _bodies("q=nomatchxyz") == set()
    # q + 기존 필터 AND
    assert _bodies("q=cpu&severity=WARN") == {"100% cpu spike"}
    assert _bodies("q=cpu&severity=ERROR") == set()
