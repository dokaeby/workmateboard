"""LLM 모니터 — store.llm_stats 집계 + /api/v1/llm-monitor/stats 엔드포인트."""

from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from jobmate.api.app import create_app
from jobmate.config import Settings
from jobmate.store.db import RunStatus, StageStatus, Store
from tests.integration.helpers import default_outputs, scripted_llm


def _client():
    db = str(Path(tempfile.mkdtemp()) / "t.db")
    settings = Settings(db_path=db, budget_max_tokens=100_000)
    store = Store(db)
    llm = scripted_llm(default_outputs())
    app = create_app(settings=settings, store=store, llm=llm)
    return TestClient(app), store


def _seed(store: Store) -> str:
    """completed run 1개(스테이지 2, 토큰/모델/프롬프트버전) + failed run 1개."""
    r1 = store.create_run("dev_review", "v1", {"project": "p1"}, "trace-1")
    s1 = store.start_stage(r1.id, "analyze", 1, {}, model="gpt-x", prompt_version="pv1")
    store.finish_stage(s1.id, status=StageStatus.SUCCEEDED, tokens_in=100, tokens_out=50)
    s2 = store.start_stage(r1.id, "implement", 1, {}, model="gpt-x", prompt_version="pv1")
    store.finish_stage(s2.id, status=StageStatus.FAILED, error="boom", tokens_in=10, tokens_out=0)
    store.update_run(r1.id, status=RunStatus.COMPLETED)

    r2 = store.create_run("dev_review", "v1", {}, "trace-2")
    store.update_run(r2.id, status=RunStatus.FAILED, error="bad input")
    return r1.id


def test_llm_stats_aggregates():
    _, store = _client()
    _seed(store)
    st = store.llm_stats(30)
    assert st["runs_total"] == 2
    assert st["by_status"]["completed"] == 1
    assert st["failed_runs"] == 1
    assert st["terminal_runs"] == 2
    assert st["error_rate_pct"] == 50.0
    assert st["stage_calls"] == 2
    assert st["stage_failures"] == 1
    assert st["stage_failure_rate_pct"] == 50.0
    assert st["tokens"]["total_in"] == 110
    assert st["tokens"]["total_out"] == 50
    assert st["tokens"]["total"] == 160
    assert st["models"][0]["model"] == "gpt-x"
    assert st["prompt_versions"] == ["pv1"]
    assert st["latency_s"]["samples"] == 2  # terminal run(completed+failed) 모두 지연 표본
    assert st["latency_s"]["p95"] is not None
    assert st["daily"] and len(st["daily"]) == 30
    assert len(st["recent_failures"]) == 1
    assert st["recent_failures"][0]["status"] == "failed"
    # 신규 집계 필드
    assert st["by_stage"] and st["by_stage"][0]["stage_key"] in {"analyze", "implement"}
    assert st["by_workflow"][0]["workflow"] == "dev_review"
    assert st["by_workflow"][0]["avg_tokens_per_run"] == 80.0  # 160토큰/2run
    assert st["retry"]["calls"] == 0
    assert st["retry"]["attempts_hist"] == {"1": 2}
    assert st["tokens_ratio"]["input_pct"] == 68.8  # 110/160
    assert st["stage_model_matrix"]
    assert st["prompt_version_stats"][0]["version"] == "pv1"
    assert st["approval_backlog"]["total_wait_s"] is None
    assert st["budget"] is None  # monthly_budget 미전달


def test_llm_stats_budget_and_retry():
    _, store = _client()
    r = store.create_run("dev_review", "v1", {}, "t")
    # attempt 2 재시도 스테이지 (토큰 낭비 집계용)
    store.start_stage(r.id, "analyze", 2, {}, model="m", prompt_version="pv")
    sr = store.latest_stage(r.id, "analyze")
    store.finish_stage(sr.id, status=StageStatus.SUCCEEDED, tokens_in=50, tokens_out=50)
    store.update_run(r.id, status=RunStatus.WAITING_APPROVAL)
    st = store.llm_stats(30, monthly_budget=1000)
    assert st["budget"]["monthly_tokens"] == 100
    assert st["budget"]["used_pct"] == 10.0
    assert st["budget"]["alert"] is False
    assert st["retry"]["calls"] == 1
    assert st["retry"]["tokens"] == 100
    assert st["retry"]["attempts_hist"] == {"2": 1}
    assert st["approval_backlog"]["waiting"] == 1
    assert st["approval_backlog"]["total_wait_s"] is not None


def test_llm_monitor_endpoint():
    client, store = _client()
    _seed(store)
    r = client.get("/api/v1/llm-monitor/stats?days=7")
    assert r.status_code == 200
    body = r.json()
    assert body["runs_total"] == 2
    assert body["days"] == 7
    assert body["langsmith"]["enabled"] is False
    assert "sample_rate" in body["langsmith"]
