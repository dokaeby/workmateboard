"""spec 007 US3 — runner._finalize 실패성 종결의 run.error/error_class 기록.

escalate/거절 경로가 update_run(status=...)만 호출해 runs.error/error_class가
비어 /observability/errors와 error.error_class 알림 조건에서 누락되던
회귀 테스트. _finalize를 그래프 없이 직접 호출한다.
"""

from __future__ import annotations

from jobmate.config import Settings
from jobmate.engine.registry import Registry
from jobmate.engine.runner import WorkflowRunner
from jobmate.llm import FakeLLM
from jobmate.models import AgentSpec, ModelConfig, StageSpec, WorkflowSpec
from jobmate.models.enums import StageStatus
from jobmate.store.db import Store


def _make(tmp_path):
    db = str(tmp_path / "t.db")
    settings = Settings(db_path=db)
    store = Store(db)
    spec = WorkflowSpec(
        key="wf",
        version="0",
        entry="s1",
        stages=[StageSpec(key="s1", kind="llm", agent="a")],
    )
    reg = Registry()
    reg.register_agent(
        AgentSpec(
            key="a",
            role="analysis",
            prompt="p",
            output_schema="AnalysisResult",
            model=ModelConfig(model="m"),
        )
    )
    runner = WorkflowRunner(
        spec=spec,
        registry=reg,
        store=store,
        llm=FakeLLM(),
        tools=None,
        settings=settings,
    )
    return runner, store


def _run_row(store: Store, run_id: str):
    return store._conn.execute(  # noqa: SLF001 - 에러 컬럼 직접 검증
        "SELECT status,error,error_class FROM runs WHERE id=?", (run_id,)
    ).fetchone()


def _mk_run(store: Store):
    return store.create_run("wf", "0", {"task": "t"}, trace_id="t")


def test_finalize_budget_exhausted_records_budget_error_class(tmp_path):
    """ESCALATE_BUDGET → budget_exhausted + error_class='budget' +
    observability_errors에 집계된다."""
    runner, store = _make(tmp_path)
    run = _mk_run(store)
    runner._finalize({"run_id": run.id, "escalate": "budget_exhausted"})
    row = _run_row(store, run.id)
    assert row["status"] == "budget_exhausted"
    assert row["error"]
    assert row["error_class"] == "budget"
    errs = store.observability_errors(30)
    assert errs["total"] >= 1
    assert any(b["error_class"] == "budget" for b in errs["by_class"])


def test_finalize_failed_prefers_stage_error(tmp_path):
    """실패 stage_run 원문이 있으면 run.error로 승계 — 실제 원인이
    error_class에 정확히 분류된다."""
    runner, store = _make(tmp_path)
    run = _mk_run(store)
    sr = store.start_stage(run.id, "s1", 1, {})
    store.finish_stage(sr.id, status=StageStatus.FAILED, error="connection timed out")
    runner._finalize({"run_id": run.id, "escalate": "failed"})
    row = _run_row(store, run.id)
    assert row["status"] == "failed"
    assert "timed out" in row["error"]
    assert row["error_class"] == "timeout"


def test_finalize_needs_human_records_error(tmp_path):
    runner, store = _make(tmp_path)
    run = _mk_run(store)
    runner._finalize({"run_id": run.id, "escalate": "needs_human"})
    row = _run_row(store, run.id)
    assert row["status"] == "needs_human"
    assert row["error"]


def test_finalize_rejected_records_error(tmp_path):
    runner, store = _make(tmp_path)
    run = _mk_run(store)
    runner._finalize({"run_id": run.id, "approval": {"decision": "reject"}})
    row = _run_row(store, run.id)
    assert row["status"] == "rejected"
    assert row["error"]


def test_finalize_does_not_overwrite_existing_error(tmp_path):
    """이미 기록된 더 구체적인 error는 finalize가 덮어쓰지 않는다."""
    runner, store = _make(tmp_path)
    run = _mk_run(store)
    store.update_run(run.id, error="original specific failure")
    runner._finalize({"run_id": run.id, "escalate": "failed"})
    row = _run_row(store, run.id)
    assert row["error"] == "original specific failure"


def test_finalize_completed_writes_no_error(tmp_path):
    runner, store = _make(tmp_path)
    run = _mk_run(store)
    runner._finalize({"run_id": run.id, "approval": {"decision": "approve"}})
    row = _run_row(store, run.id)
    assert row["status"] == "completed"
    assert row["error"] == ""
    assert row["error_class"] == ""
