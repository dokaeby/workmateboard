"""spec 011 US1 — 관제센터 백엔드 집계 엔드포인트 테스트.

- GET /api/v1/observability/notifications — 알림함 집계 형태
- GET /api/v1/observability/funnel — 단계별 도달 run 수
- GET /api/v1/observability/agents/live — 세션 카드 shape + 상태 매핑
- GET /api/v1/observability/swarm — run 노드/엣지 그래프 + days 모드

모두 기존 observability 엔드포인트와 동일하게 lead 게이트(_lead_gate)를
거치며, 데이터는 store 계층을 orgctx.org_scope 아래에서 직접 시드한다.
"""

from __future__ import annotations

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


# ---------------- notifications ----------------


def test_notifications_aggregate_shape(monkeypatch: pytest.MonkeyPatch):
    client, store = _env(monkeypatch)
    oid = _oid(store)
    with orgctx.org_scope(oid):
        i1 = store.upsert_inbox_item("notification", "배포 완료", body="b1")
        store.upsert_inbox_item("notification", "승인 요청", body="b2")
        store.upsert_inbox_item("schedule", "다른 kind는 제외", body="b3")
        store.mark_inbox_read(i1)

    r = client.get("/api/v1/observability/notifications?days=7", headers=_HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["days"] == 7
    assert body["total"] == 2
    assert body["open"] == 1
    assert body["resolved"] == 1
    # 일별 시계열 — days 버킷, 오늘 버킷에 2건
    series = body["series"]
    assert len(series) == 7
    assert all(set(s) == {"t", "count"} for s in series)
    assert sum(s["count"] for s in series) == 2
    assert series[-1]["count"] == 2
    # 최근 알림 — status open/resolved 매핑
    recent = body["recent"]
    assert len(recent) == 2
    assert {i["status"] for i in recent} == {"open", "resolved"}
    assert all(set(i) >= {"id", "title", "created_at", "status"} for i in recent)
    assert next(i for i in recent if i["title"] == "배포 완료")["status"] == "resolved"


def test_notifications_requires_lead(monkeypatch: pytest.MonkeyPatch):
    client, store = _env(monkeypatch)
    # API 키 없는 요청은 미들웨어 401
    assert client.get("/api/v1/observability/notifications").status_code == 401


# ---------------- funnel ----------------


def _seed_funnel_runs(store: Store, oid: str) -> dict[str, str]:
    """dev_review(entry=analysis) 기준:
    - run_full: analysis(entry)+qa → planned/assigned, completed → executed
    - run_entry_only: analysis만 → received에만 카운트
    - run_failed_stage: qa failed → planned/assigned이나 미완료
    """
    ids: dict[str, str] = {}
    with orgctx.org_scope(oid):
        run_full = store.create_run("dev_review", "0.1.0", {"task": "full"}, trace_id="f1")
        sr = store.start_stage(run_full.id, "analysis", 1, {})
        store.finish_stage(sr.id, status="succeeded")
        sr2 = store.start_stage(run_full.id, "qa", 1, {})
        store.finish_stage(sr2.id, status="succeeded")
        store.update_run(run_full.id, status=RunStatus.COMPLETED)
        ids["full"] = run_full.id

        run_entry = store.create_run("dev_review", "0.1.0", {"task": "entry only"}, trace_id="f2")
        sr3 = store.start_stage(run_entry.id, "analysis", 1, {})
        store.finish_stage(sr3.id, status="succeeded")
        ids["entry_only"] = run_entry.id

        run_fail = store.create_run("dev_review", "0.1.0", {"task": "fail"}, trace_id="f3")
        sr4 = store.start_stage(run_fail.id, "analysis", 1, {})
        store.finish_stage(sr4.id, status="succeeded")
        sr5 = store.start_stage(run_fail.id, "qa", 1, {})
        store.finish_stage(sr5.id, status="failed", error="boom")
        ids["failed"] = run_fail.id
    return ids


def test_funnel_stage_counts(monkeypatch: pytest.MonkeyPatch):
    client, store = _env(monkeypatch)
    oid = _oid(store)
    _seed_funnel_runs(store, oid)

    r = client.get("/api/v1/observability/funnel?days=7", headers=_HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["days"] == 7
    stages = {s["key"]: s for s in body["stages"]}
    assert [s["key"] for s in body["stages"]] == [
        "received",
        "planned",
        "assigned",
        "executed",
    ]
    assert stages["received"]["count"] == 3
    # analysis만 있는 run은 planned/assigned/executed에서 제외
    assert stages["planned"]["count"] == 2
    assert stages["assigned"]["count"] == 2
    assert stages["executed"]["count"] == 1
    labels = {s["key"]: s["label"] for s in body["stages"]}
    assert labels["received"] == "요청 수신"

    # workflow 필터 — 다른 키는 0
    r2 = client.get("/api/v1/observability/funnel?days=7&workflow=nope", headers=_HEADERS)
    assert {s["count"] for s in r2.json()["stages"]} == {0}


# ---------------- agents/live ----------------


def test_agents_live_shape_and_status_map(monkeypatch: pytest.MonkeyPatch):
    client, store = _env(monkeypatch)
    oid = _oid(store)
    with orgctx.org_scope(oid):
        store.upsert_agent_session(
            "claude",
            "sess-1",
            status="working",
            title="fix login bug",
            doing="editing auth.py",
            project="projA",
            tokens_in=1000,
            tokens_out=200,
            event_at="2026-01-01T00:00:00+00:00",
        )
        store.upsert_agent_session("codex", "sess-2", status="ended")
        store.upsert_agent_session("gemini", "sess-3", status="decision", ask="approve?")

    r = client.get("/api/v1/observability/agents/live", headers=_HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 3
    by_key = {i["id"]: i for i in body["items"]}
    w = by_key["claude:sess-1"]
    assert w["name"] == "fix login bug"
    assert w["role"] == "claude"
    assert w["status"] == "running"  # working → running
    assert w["current_task"] == "editing auth.py"
    assert w["tokens"] == 1200
    assert w["started_at"]
    assert w["last_heartbeat_at"] == "2026-01-01T00:00:00+00:00"
    # 스키마에 없는 필드는 null — 추정 금지
    assert w["model"] is None
    assert w["progress_pct"] is None
    assert w["cost_usd"] is None
    assert w["tool_calls"] is None
    assert w["sub_agents"] is None
    assert by_key["codex:sess-2"]["status"] == "completed"  # ended → completed
    assert by_key["gemini:sess-3"]["status"] == "waiting"  # decision → waiting


# ---------------- swarm ----------------


def test_swarm_run_nodes_and_edges(monkeypatch: pytest.MonkeyPatch):
    client, store = _env(monkeypatch)
    oid = _oid(store)
    with orgctx.org_scope(oid):
        run = store.create_run("dev_review", "0.1.0", {"task": "swarm"}, trace_id="sw1")
        sr1 = store.start_stage(run.id, "analysis", 1, {})
        store.finish_stage(sr1.id, status="succeeded", tokens_in=10, tokens_out=5)
        sr2 = store.start_stage(run.id, "qa", 1, {})
        store.finish_stage(sr2.id, status="failed", error="boom", tokens_in=3, tokens_out=0)
    run_id = run.id

    r = client.get(f"/api/v1/observability/swarm?run_id={run_id}", headers=_HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    nodes = {n["id"]: n for n in body["nodes"]}
    edges = body["edges"]

    run_node = nodes[f"run:{run_id}"]
    assert run_node["kind"] == "run"
    assert run_node["label"] == "dev_review"

    stage_nodes = [n for n in body["nodes"] if n["kind"] == "stage"]
    assert len(stage_nodes) == 2
    by_key = {n["stage_key"]: n for n in stage_nodes}
    qa = by_key["qa"]
    assert qa["label"] == "qa"
    # dev_review spec의 qa stage agent 키
    assert qa["agent"] == "qa"
    assert qa["status"] == "failed"
    assert qa["error_count"] == 1
    assert qa["tokens"] == 3
    assert qa["latency_ms"] is not None and qa["latency_ms"] >= 0
    assert by_key["analysis"]["progress_pct"] == 100
    assert by_key["analysis"]["error_count"] == 0

    # run→각 stage_key 첫 실행 spawn 엣지 + stage 간 sequence 엣지
    spawn = [e for e in edges if e["kind"] == "spawn"]
    seq = [e for e in edges if e["kind"] == "sequence"]
    assert len(spawn) == 2  # analysis, qa 각 첫 실행
    assert all(e["source"] == f"run:{run_id}" for e in spawn)
    assert len(seq) == 1
    assert {seq[0]["source"], seq[0]["target"]} == {
        f"stage:{sr1.id}",
        f"stage:{sr2.id}",
    }

    # 존재하지 않는 run → 404, 다른 조직 run도 404 (get_run org 바인딩)
    assert (
        client.get("/api/v1/observability/swarm?run_id=nope", headers=_HEADERS).status_code == 404
    )


def test_swarm_days_mode(monkeypatch: pytest.MonkeyPatch):
    client, store = _env(monkeypatch)
    oid = _oid(store)
    with orgctx.org_scope(oid):
        for i in range(3):
            run = store.create_run("dev_review", "0.1.0", {"task": f"d{i}"}, trace_id=f"d{i}")
            sr = store.start_stage(run.id, "analysis", 1, {})
            store.finish_stage(sr.id, status="succeeded")
            sr2 = store.start_stage(run.id, "qa", 1, {})
            store.finish_stage(sr2.id, status="succeeded")

    r = client.get("/api/v1/observability/swarm?days=7", headers=_HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    kinds = {n["kind"] for n in body["nodes"]}
    assert "run" in kinds and "stage" in kinds
    # 3 run × (run 노드 1 + stage 노드 2) = 9 노드
    assert len(body["nodes"]) == 9
    # run당 stage 2개 → sequence 엣지 1개씩, spawn은 stage_key 첫 실행 2개씩
    assert sum(1 for e in body["edges"] if e["kind"] == "sequence") == 3
    assert sum(1 for e in body["edges"] if e["kind"] == "spawn") == 6
