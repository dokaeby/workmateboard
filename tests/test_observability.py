"""spec 007 — 관측성 플랫폼 백엔드 테스트 (US1/US3/US7).

- GET /api/v1/runs/{id}/timeline — stage+cost 조인, error_class
- GET /api/v1/observability/{costs,usage,errors,approvals,anomalies}
- services/anomaly.py 스윕 — 비용/에러 스파이크 감지 + fingerprint dedupe
- store 계층 error_class 자동 기록(finish_stage/update_run)
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jobmate import cost_ledger
from jobmate.api.app import create_app
from jobmate.config import Settings
from jobmate.models import RunStatus
from jobmate.services import anomaly as anomaly_svc
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


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _seed_run(store: Store, oid: str) -> str:
    """stage 2개(성공+실패) + cost 이벤트 + run-level 에러를 가진 run 1건."""
    with orgctx.org_scope(oid):
        run = store.create_run(
            "dev_review",
            "0.1.0",
            {"task": "fix bug", "project": "projA"},
            trace_id="t1",
        )
        sr1 = store.start_stage(
            run.id, "analysis", 1, {"agent": "a"}, model="model-a", prompt_version="v1"
        )
        store.finish_stage(
            sr1.id,
            status="succeeded",
            output={"summary": "done"},
            tokens_in=100,
            tokens_out=50,
        )
        cost_ledger.emit_stage_cost_event(
            store,
            run_id=run.id,
            stage_run_id=sr1.id,
            model="model-a",
            provider="test",
            tokens_in=100,
            tokens_out=50,
        )
        sr2 = store.start_stage(run.id, "qa", 1, {}, model="model-b")
        store.finish_stage(
            sr2.id,
            status="failed",
            error="HTTP 429 rate limit exceeded",
            tokens_in=10,
            tokens_out=0,
        )
        cost_ledger.emit_stage_cost_event(
            store,
            run_id=run.id,
            stage_run_id=sr2.id,
            model="model-b",
            provider="test",
            tokens_in=10,
            tokens_out=0,
        )
        # run-level 에러 — timeout 분류 확인용
        run2 = store.create_run(
            "dev_review", "0.1.0", {"task": "x", "project": "projB"}, trace_id="t2"
        )
        store.update_run(run2.id, status=RunStatus.FAILED, error="connection timed out")
        return run.id


# ---------------- US1: timeline ----------------


def test_timeline_shape_and_cost_join(monkeypatch: pytest.MonkeyPatch):
    client, store = _env(monkeypatch)
    oid = _oid(store)
    run_id = _seed_run(store, oid)

    r = client.get(f"/api/v1/runs/{run_id}/timeline", headers=_HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["run_id"] == run_id
    stages = body["stages"]
    assert len(stages) == 2
    by_key = {s["stage_key"]: s for s in stages}

    ok = by_key["analysis"]
    assert ok["status"] == "succeeded"
    assert ok["model"] == "model-a"
    assert ok["prompt_version"] == "v1"
    assert ok["tokens_in"] == 100 and ok["tokens_out"] == 50
    assert ok["cost_usd"] is not None and ok["cost_usd"] > 0
    assert ok["duration_ms"] is not None and ok["duration_ms"] >= 0
    assert isinstance(ok["input_summary"], str)
    assert isinstance(ok["output_summary"], str)
    assert "done" in ok["output_summary"]

    bad = by_key["qa"]
    assert bad["status"] == "failed"
    assert bad["error_class"] == "rate_limit"
    assert "429" in bad["error"]


def test_timeline_run_error_class_and_404(monkeypatch: pytest.MonkeyPatch):
    client, store = _env(monkeypatch)
    oid = _oid(store)
    with orgctx.org_scope(oid):
        run = store.create_run("dev_review", "0.1.0", {"task": "t"}, trace_id="t3")
        store.update_run(run.id, status=RunStatus.FAILED, error="socket timed out")
        # stage 없이 run 에러만 — timeline은 빈 stages + run 자체는 조회 가능
        r = client.get(f"/api/v1/runs/{run.id}/timeline", headers=_HEADERS)
        assert r.status_code == 200
        assert r.json()["stages"] == []
        # error_class는 store.update_run이 기록 — runs 테이블 직접 확인
        row = store._conn.execute("SELECT error_class FROM runs WHERE id=?", (run.id,)).fetchone()
        assert row["error_class"] == "timeout"

    assert client.get("/api/v1/runs/nonexistent/timeline", headers=_HEADERS).status_code == 404


def test_timeline_requires_auth(monkeypatch: pytest.MonkeyPatch):
    """API 키 미설정 인증 없이는 401 — lead 게이트는 _lead_user가 강제."""
    client, store = _env(monkeypatch)
    oid = _oid(store)
    run_id = _seed_run(store, oid)
    # 헤더 없는 요청은 미들웨어에서 401
    assert client.get(f"/api/v1/runs/{run_id}/timeline").status_code == 401
    assert client.get("/api/v1/observability/costs").status_code == 401


# ---------------- US2: costs / usage ----------------


def test_costs_group_by_model_and_day(monkeypatch: pytest.MonkeyPatch):
    client, store = _env(monkeypatch)
    oid = _oid(store)
    _seed_run(store, oid)

    r = client.get("/api/v1/observability/costs?days=30&group_by=model", headers=_HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["group_by"] == "model" and body["days"] == 30
    keys = {i["key"]: i for i in body["items"]}
    assert "model-a" in keys and "model-b" in keys
    assert keys["model-a"]["calls"] >= 1
    assert keys["model-a"]["tokens_in"] == 100
    assert keys["model-a"]["runs"] >= 1
    assert keys["model-a"]["cost_usd"] > 0

    r2 = client.get("/api/v1/observability/costs?days=30&group_by=day", headers=_HEADERS)
    assert r2.status_code == 200
    day_items = r2.json()["items"]
    assert day_items and len(day_items[0]["key"]) == 10  # YYYY-MM-DD
    assert day_items[0]["cost_usd"] > 0

    # 알 수 없는 group_by는 model로 폴백
    r3 = client.get("/api/v1/observability/costs?group_by=bogus", headers=_HEADERS)
    assert r3.json()["group_by"] == "model"


def test_costs_group_by_project_and_agent(monkeypatch: pytest.MonkeyPatch):
    client, store = _env(monkeypatch)
    oid = _oid(store)
    _seed_run(store, oid)

    rp = client.get("/api/v1/observability/costs?days=30&group_by=project", headers=_HEADERS)
    pkeys = {i["key"] for i in rp.json()["items"]}
    # 비용 이벤트가 있는 run의 project만 집계된다 — projA는 stage cost 보유
    assert "projA" in pkeys

    ra = client.get("/api/v1/observability/costs?days=30&group_by=agent", headers=_HEADERS)
    assert ra.status_code == 200
    akeys = {i["key"] for i in ra.json()["items"]}
    assert "unknown" in akeys  # agent_id 미태깅 run → 'unknown' 폴백


def test_usage_endpoint(monkeypatch: pytest.MonkeyPatch):
    client, store = _env(monkeypatch)
    oid = _oid(store)
    with orgctx.org_scope(oid):
        store.record_llm_usage("model-a", 500, 200)
        store.record_llm_usage("model-b", 100, 40)
        # agent_sessions 1건 — 토큰 포함
        store.upsert_agent_session(
            "claude",
            "sess-1",
            status="working",
            tokens_in=300,
            tokens_out=150,
        )
    _seed_run(store, oid)

    r = client.get("/api/v1/observability/usage?days=30", headers=_HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["days"] == 30
    assert body["models"]["model-a"]["calls"] == 1
    assert body["models"]["model-a"]["tokens_in"] == 500
    assert body["daily"] and body["daily"][-1]["cost_usd"] > 0
    sess = body["sessions"]
    assert sess["total"] >= 1
    assert sess["by_tool"]["claude"]["sessions"] == 1
    assert sess["by_tool"]["claude"]["tokens_in"] == 300


# ---------------- US3: errors ----------------


def test_errors_aggregation(monkeypatch: pytest.MonkeyPatch):
    client, store = _env(monkeypatch)
    oid = _oid(store)
    _seed_run(store, oid)

    r = client.get("/api/v1/observability/errors?days=30", headers=_HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] >= 2  # stage 실패 1 + run 에러 1
    by_class = {c["error_class"]: c for c in body["by_class"]}
    assert "rate_limit" in by_class
    assert "timeout" in by_class
    rl = by_class["rate_limit"]
    assert rl["count"] >= 1 and rl["pct"] > 0 and rl["last_seen"]
    assert len(rl["samples"]) <= 5
    sample = rl["samples"][0]
    assert sample["run_id"] and sample["error_class"] == "rate_limit"
    # stage 실패 샘플은 stage_key를 가진다
    assert any(s["stage_key"] == "qa" for s in rl["samples"])


def test_errors_legacy_rows_classified_at_query(monkeypatch: pytest.MonkeyPatch):
    """error_class 컬럼이 비어 있는 레거시 행도 조회 시 분류된다."""
    client, store = _env(monkeypatch)
    oid = _oid(store)
    with orgctx.org_scope(oid):
        run = store.create_run("dev_review", "0.1.0", {"task": "t"}, trace_id="t9")
        sr = store.start_stage(run.id, "s", 1, {})
        # 컬럼을 직접 비워 레거시 상태를 재현
        store.finish_stage(sr.id, status="failed", error="boom tool failure")
        with store.tx() as c:
            c.execute("UPDATE stage_runs SET error_class='' WHERE id=?", (sr.id,))
    r = client.get("/api/v1/observability/errors?days=30", headers=_HEADERS)
    body = r.json()
    assert any(c["error_class"] == "tool_error" for c in body["by_class"])


# ---------------- US5: approvals ----------------


def test_approvals_waiting_and_stats(monkeypatch: pytest.MonkeyPatch):
    client, store = _env(monkeypatch)
    oid = _oid(store)
    with orgctx.org_scope(oid):
        run = store.create_run(
            "dev_review", "0.1.0", {"task": "t", "project": "projA"}, trace_id="ta"
        )
        # budget 승인 대기 마킹 — status=waiting_approval + requested_at
        assert store.mark_run_budget_approval(
            run.id,
            [{"policy_id": "p1", "scope_type": "global", "scope_id": "", "scope": "global"}],
        )
    r = client.get("/api/v1/observability/approvals", headers=_HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    waiting = body["waiting"]
    assert any(w["run_id"] == run.id for w in waiting)
    w = next(w for w in waiting if w["run_id"] == run.id)
    assert w["workflow"] == "dev_review"
    assert w["project"] == "projA"
    assert w["waited_s"] >= 0 and w["waiting_since"]
    stats = body["stats"]
    assert stats["waiting"] >= 1
    assert stats["oldest_wait_s"] >= 0 and stats["total_wait_s"] >= 0
    assert "decided_24h" in stats and "avg_decision_s" in stats


# ---------------- US7: anomalies ----------------


def test_anomaly_sweep_cost_spike_and_dedupe(monkeypatch: pytest.MonkeyPatch):
    _client, store = _env(monkeypatch)
    oid = _oid(store)
    with orgctx.org_scope(oid):
        # 오늘 비용 $10 (baseline=0 → value-baseline > $0.5 floor 충족)
        store.record_cost_event(
            idempotency_key="spike-1",
            cost_status="estimated",
            cost_microusd=10_000_000,
            occurred_at=_now_iso(),
        )
        # 실패 stage 4건 — error floor(3) 충족하려면 baseline 대비 2배+3 초과 필요.
        # baseline=0이면 today>3 필요 — 4건으로 error_spike도 발화시킨다.
        run = store.create_run("dev_review", "0.1.0", {"task": "t"}, trace_id="ts")
        for i in range(4):
            sr = store.start_stage(run.id, f"s{i}", 1, {})
            store.finish_stage(sr.id, status="failed", error="boom")

    res = anomaly_svc.sweep_anomalies(store)
    kinds = {i["kind"] for i in res["items"]}
    assert "cost_spike" in kinds
    assert "error_spike" in kinds

    # dedupe — 같은 fingerprint는 두 번째 스윕에서 기록되지 않는다
    res2 = anomaly_svc.sweep_anomalies(store)
    assert res2["detected"] == 0

    # anomaly_events 조회 + 인박스 알림 확인
    with orgctx.org_scope(oid):
        events = store.list_anomaly_events(days=7)
    assert len(events) >= 2
    assert {e["kind"] for e in events} >= {"cost_spike", "error_spike"}
    assert all(e["org_id"] == oid for e in events)
    with orgctx.org_scope(oid):
        inbox = store.list_inbox()
    assert any(i["kind"] == "anomaly" for i in inbox)


def test_anomaly_sweep_no_spike(monkeypatch: pytest.MonkeyPatch):
    _client, store = _env(monkeypatch)
    oid = _oid(store)
    with orgctx.org_scope(oid):
        store.record_cost_event(
            idempotency_key="small-1",
            cost_status="estimated",
            cost_microusd=100_000,  # $0.10 — floor 미만
            occurred_at=_now_iso(),
        )
    res = anomaly_svc.sweep_anomalies(store)
    assert res["detected"] == 0


def test_anomaly_endpoint(monkeypatch: pytest.MonkeyPatch):
    client, store = _env(monkeypatch)
    oid = _oid(store)
    with orgctx.org_scope(oid):
        store.record_anomaly_event(
            kind="cost_spike",
            metric="daily_cost_usd",
            value=12.5,
            baseline=1.0,
            message="test anomaly",
            fingerprint="cost_spike:daily_cost_usd:test",
            org_id=oid,
        )
    r = client.get("/api/v1/observability/anomalies?days=30", headers=_HEADERS)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert any(i["kind"] == "cost_spike" and i["value"] == 12.5 for i in items)


def test_run_anomaly_sweep_time_gate(monkeypatch: pytest.MonkeyPatch):
    """run_anomaly_sweep은 1h 게이트 — force=False 재호출은 skip."""
    _client, store = _env(monkeypatch)
    oid = _oid(store)
    with orgctx.org_scope(oid):
        store.record_cost_event(
            idempotency_key="gate-1",
            cost_status="estimated",
            cost_microusd=10_000_000,
            occurred_at=_now_iso(),
        )
    r1 = anomaly_svc.run_anomaly_sweep(store)
    assert r1.get("skipped") is not True
    r2 = anomaly_svc.run_anomaly_sweep(store)
    assert r2.get("skipped") is True
    r3 = anomaly_svc.run_anomaly_sweep(store, force=True)
    assert r3.get("skipped") is not True


def test_timeline_mixed_naive_aware_timestamps(monkeypatch: pytest.MonkeyPatch):
    """started_at naive + finished_at aware 혼합 행도 duration 계산이
    TypeError 없이 동작한다 — _aware 정규화 회귀 (spec 007)."""
    _client, store = _env(monkeypatch)
    oid = _oid(store)
    with orgctx.org_scope(oid):
        run = store.create_run("dev_review", "0", {"task": "t"}, trace_id="tz")
        sr = store.start_stage(run.id, "s1", 1, {})
        store.finish_stage(sr.id, status="succeeded")
    # 구 ISO 행 모사 — tz 없는 naive started_at + aware finished_at
    store._conn.execute(  # noqa: SLF001
        "UPDATE stage_runs SET started_at=?, finished_at=? WHERE id=?",
        ("2020-01-01T00:00:00", _now_iso(), sr.id),
    )
    store._conn.commit()
    with orgctx.org_scope(oid):
        stages = store.run_timeline(run.id)
    assert stages and stages[0]["duration_ms"] is not None
    assert stages[0]["duration_ms"] > 0
