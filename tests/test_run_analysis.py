"""spec 006 US4 — 실행 분석: run compare, retry-from-stage, comment insights."""

import tempfile
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jobmate.api.app import create_app
from jobmate.config import Settings
from jobmate.models import RunStatus, StageStatus
from jobmate.services import run_analysis
from jobmate.store.db import Store
from tests.integration.helpers import VALID_DIFF, default_outputs, run_dev_review, scripted_llm


def _client():
    db = str(Path(tempfile.mkdtemp()) / "t.db")
    settings = Settings(db_path=db, budget_max_tokens=100_000)
    store = Store(db)
    llm = scripted_llm(default_outputs())
    app = create_app(settings=settings, store=store, llm=llm)
    return TestClient(app), store, settings, llm


def _wait_status(client: TestClient, run_id: str, statuses: set[str], timeout: float = 10.0):
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        d = client.get(f"/api/v1/runs/{run_id}").json()
        last = d.get("status", "")
        if last in statuses:
            return d
        time.sleep(0.1)
    raise AssertionError(f"run {run_id} stuck at {last}, wanted {statuses}")


def _mk_run(store: Store, task: str = "t", status: RunStatus = RunStatus.COMPLETED):
    run = store.create_run("dev_review", "0.1.0", {"task": task}, trace_id=f"t-{task}")
    store.update_run(run.id, status=status)
    return run


def _stage(
    store: Store,
    run_id: str,
    key: str,
    attempt: int,
    status: StageStatus,
    output: dict | None = None,
    tokens: tuple[int, int] = (0, 0),
):
    sr = store.start_stage(run_id, key, attempt, {"in": key})
    store.finish_stage(
        sr.id,
        status=status,
        output=output or {},
        tokens_in=tokens[0],
        tokens_out=tokens[1],
    )
    return sr


# ---------- FR-017 compare ----------


def test_compare_view_fields():
    client, store, _s, _l = _client()
    a = _mk_run(store, "task-a")
    b = _mk_run(store, "task-b", status=RunStatus.FAILED)

    _stage(store, a.id, "analysis", 1, StageStatus.SUCCEEDED, {"summary": "req ok"})
    _stage(
        store,
        a.id,
        "implement",
        1,
        StageStatus.SUCCEEDED,
        {"summary": "impl", "diff": VALID_DIFF},
        tokens=(10, 5),
    )
    _stage(store, a.id, "qa", 1, StageStatus.SUCCEEDED, {"decision": "pass"})
    _stage(store, b.id, "analysis", 1, StageStatus.SUCCEEDED, {"summary": "req ok"})
    _stage(store, b.id, "implement", 1, StageStatus.FAILED, {}, tokens=(3, 1))

    r = client.get(f"/api/v1/runs/compare?run_a={a.id}&run_b={b.id}")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["run_a"]["id"] == a.id
    assert data["run_b"]["status"] == "failed"

    by_key = {s["stage_key"]: s for s in data["stages"]}
    assert "analysis" in by_key and "implement" in by_key and "qa" in by_key

    impl = by_key["implement"]
    assert impl["a"]["status"] == "succeeded"
    assert impl["b"]["status"] == "failed"
    assert impl["status_changed"] is True
    assert impl["a"]["duration_ms"] is not None
    assert impl["b"]["duration_ms"] is not None
    assert impl["duration_delta_ms"] is not None
    # diff 파일 목록 — implement output.diff 파싱
    paths = [f["path"] for f in impl["a"]["diff_files"]]
    assert "x.txt" in paths
    # b의 implement는 실패 — diff 없음
    assert impl["b"]["diff_files"] == []
    # 입출력 요약
    assert "impl" in impl["a"]["output_summary"]

    # qa는 b에 없음 — 단방향
    assert by_key["qa"]["a"]["status"] == "succeeded"
    assert by_key["qa"]["b"] is None
    assert by_key["qa"]["status_changed"] is True

    # run 수준 필드
    assert data["run_a"]["succeeded_stages"] == ["analysis", "implement", "qa"]
    assert data["run_b"]["failed_stages"] == ["implement"]


def test_compare_missing_params_and_runs():
    client, store, _s, _l = _client()
    run = _mk_run(store, "only")
    assert client.get("/api/v1/runs/compare").status_code == 422
    assert client.get("/api/v1/runs/compare?run_a=x").status_code == 422
    r = client.get(f"/api/v1/runs/compare?run_a={run.id}&run_b=nope")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


# ---------- FR-020 comment insights ----------


def test_run_insight_includes_comments_meta():
    client, store, _s, _l = _client()
    run = _mk_run(store, "c1")
    _stage(store, run.id, "analysis", 1, StageStatus.SUCCEEDED, {"summary": "ok"})

    no_comments = run_analysis.run_insight(store, run)
    assert no_comments["meta"]["comments_included"] is False
    assert no_comments["meta"]["comment_count"] == 0
    assert "comments=0" not in no_comments["summary"]

    store.add_run_comment(run.id, "u1", "u@x.com", "이 요구사항은 이렇게 해석")
    store.add_run_comment(run.id, "u2", "v@x.com", "diff는 최소 변경으로")
    with_comments = run_analysis.run_insight(store, run)
    assert with_comments["meta"]["comments_included"] is True
    assert with_comments["meta"]["comment_count"] == 2
    assert len(with_comments["comments"]) == 2
    assert "comments=2 reflected" in with_comments["summary"]
    # 코멘트 유무로 결과가 구분된다
    assert no_comments["summary"] != with_comments["summary"]


def test_compare_embeds_insight_with_comment_meta():
    client, store, _s, _l = _client()
    a = _mk_run(store, "ia")
    b = _mk_run(store, "ib")
    store.add_run_comment(b.id, "u1", "u@x.com", "context note")
    r = client.get(f"/api/v1/runs/compare?run_a={a.id}&run_b={b.id}")
    assert r.status_code == 200
    data = r.json()
    assert data["run_a"]["insight"]["meta"]["comments_included"] is False
    assert data["run_b"]["insight"]["meta"]["comments_included"] is True
    assert data["run_b"]["insight"]["meta"]["comment_count"] == 1


# ---------- FR-018/019 retry-from-stage ----------


def test_retry_state_transition_store():
    _client_, store, _s, _l = _client()
    run = _mk_run(store, "rt", status=RunStatus.RUNNING)
    # running → 거부
    assert store.reset_run_for_retry(run.id, "qa") is False
    assert store.get_run(run.id).status == RunStatus.RUNNING

    store.update_run(run.id, status=RunStatus.COMPLETED)
    assert store.reset_run_for_retry(run.id, "qa", actor="tester") is True
    assert store.get_run(run.id).status == RunStatus.QUEUED
    # 감사 로그
    actions = [e.action for e in store.get_audit(run.id)]
    assert "run_retry" in actions
    # queued 상태에서는 재차 거부(진행 중)
    assert store.reset_run_for_retry(run.id, "qa") is False


def test_retry_409_on_active_run():
    client, store, _s, _l = _client()
    run = _mk_run(store, "active", status=RunStatus.RUNNING)
    r = client.post(f"/api/v1/runs/{run.id}/retry?from_stage=qa")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "invalid_state"

    run2 = _mk_run(store, "waiting", status=RunStatus.WAITING_APPROVAL)
    r2 = client.post(f"/api/v1/runs/{run2.id}/retry?from_stage=qa")
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "invalid_state"


def test_retry_unknown_stage_and_missing_run():
    client, store, _s, _l = _client()
    run = _mk_run(store, "badstage")
    r = client.post(f"/api/v1/runs/{run.id}/retry?from_stage=nope")
    assert r.status_code == 422
    r2 = client.post("/api/v1/runs/ghost/retry?from_stage=qa")
    assert r2.status_code == 404
    r3 = client.post(f"/api/v1/runs/{run.id}/retry")
    assert r3.status_code == 422


def test_retry_from_stage_skips_succeeded():
    """실제 dev_review 실행 → 완료 → qa부터 retry → 선행 단계 재실행 없음."""
    client, store, settings, llm = _client()
    run_id, runner, _state = run_dev_review(store, settings, llm, task="retry me")
    # waiting_approval → 승인 → completed
    d = client.get(f"/api/v1/runs/{run_id}").json()
    assert d["status"] == "waiting_approval"
    runner.resume(run_id, {"decision": "approve", "comment": "ok"})
    d = client.get(f"/api/v1/runs/{run_id}").json()
    assert d["status"] == "completed"

    def attempts(key: str) -> int:
        return sum(1 for s in store.get_stage_runs(run_id) if s.stage_key == key)

    stage_keys = (
        "analysis",
        "implement",
        "auto_checks",
        "security",
        "code_review",
        "qa",
        "approval",
    )
    before = {k: attempts(k) for k in stage_keys}
    assert all(v >= 1 for v in before.values())

    r = client.post(f"/api/v1/runs/{run_id}/retry?from_stage=qa")
    assert r.status_code == 202, r.text
    assert r.json()["from_stage"] == "qa"

    # qa(LLM) → approval(human interrupt)까지 재실행돼 다시 승인 대기
    _wait_status(client, run_id, {"waiting_approval", "completed", "failed", "needs_human"})
    d = client.get(f"/api/v1/runs/{run_id}").json()
    assert d["status"] == "waiting_approval", d["status"]

    after = {k: attempts(k) for k in before}
    # FR-019 — succeeded 선행 단계는 새 stage_run이 없어야 한다
    for k in ("analysis", "implement", "auto_checks", "security", "code_review"):
        assert after[k] == before[k], f"stage {k} re-executed: {before[k]} -> {after[k]}"
    # from_stage 이후는 새 attempt
    assert after["qa"] == before["qa"] + 1
    assert after["approval"] == before["approval"] + 1
    # attempt 번호는 이어진다(1이 아니라 이전 max+1)
    qa_runs = [s for s in store.get_stage_runs(run_id) if s.stage_key == "qa"]
    assert max(s.attempt for s in qa_runs) == before["qa"] + 1

    # 재승인 → 완료
    r2 = client.post(f"/api/v1/runs/{run_id}/approve", json={"decision": "approve"})
    assert r2.status_code == 200
    assert r2.json()["status"] == "completed"


def test_retry_preserves_history():
    """retry 후 이전 stage_run 레코드가 삭제되지 않고 남는다."""
    client, store, settings, llm = _client()
    run_id, runner, _ = run_dev_review(store, settings, llm, task="hist")
    runner.resume(run_id, {"decision": "approve", "comment": "ok"})
    before_ids = {s.id for s in store.get_stage_runs(run_id)}

    r = client.post(f"/api/v1/runs/{run_id}/retry?from_stage=security")
    assert r.status_code == 202
    _wait_status(client, run_id, {"waiting_approval", "completed", "failed", "needs_human"})

    after_ids = {s.id for s in store.get_stage_runs(run_id)}
    assert before_ids <= after_ids  # 이전 시도 전부 보존
    # security부터 재실행 — code_review/qa/approval도 새 attempt
    for k in ("security", "code_review", "qa", "approval"):
        attempts = [s for s in store.get_stage_runs(run_id) if s.stage_key == k]
        assert len(attempts) >= 2, k


if __name__ == "__main__":
    pytest.main([__file__, "-x", "-q"])
