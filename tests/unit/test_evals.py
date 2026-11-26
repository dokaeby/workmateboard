"""Eval 하니스 단위 테스트 — CRUD/검증/run 연결/lazy 채점/체크 타입.

임시 SQLite Store + create_app(TestClient)으로 검증한다.
실제 run 파이프라인은 store 직접 fabrication(start_stage/finish_stage)으로
결정적으로 만들고, API run 생성은 링크 필드만 검증한다(백그라운드 실행은
scripted_llm으로 무해하게 완료/대기된다).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from jobmate import evals as evals_svc
from jobmate.api.app import create_app
from jobmate.config import Settings
from jobmate.models import RunStatus, StageStatus
from jobmate.security import SESSION_COOKIE, hash_password, hash_token, new_session_token
from jobmate.store.db import Store
from tests.integration.helpers import default_outputs, scripted_llm


def _env(tmp_path: Path):
    db = str(tmp_path / "t.db")
    settings = Settings(db_path=db, budget_max_tokens=100_000)
    store = Store(db)
    llm = scripted_llm(default_outputs())
    app = create_app(settings=settings, store=store, llm=llm, adapters={})
    return TestClient(app), store


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """JOBMATE_API_KEY 설정 — 로컬 관리자 폴백을 꺼 role 게이트를 실제로 검증."""
    monkeypatch.setenv("JOBMATE_API_KEY", "test-api-key")
    return _env(tmp_path)


def _login(store: Store, client: TestClient, email: str, role: str = "member") -> None:
    u = store.get_user_by_email(email) or store.create_user(
        email, hash_password("password123"), email
    )
    assert u is not None
    store.set_user_role(u["id"], role)
    token = new_session_token()
    store.create_session(u["id"], hash_token(token))
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE, token)


def _mk_eval(client: TestClient, name: str = "eval1", **kw) -> dict:
    body = {
        "name": name,
        "workflow_key": "dev_review",
        "input": {"task": "fix the bug"},
        "checks": [{"type": "stage_completed", "stage": "qa"}],
        **kw,
    }
    r = client.post("/api/v1/evals", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _mk_run(
    store: Store,
    stages: dict[str, dict[str, Any]] | None = None,
    *,
    status: RunStatus = RunStatus.COMPLETED,
    tokens: tuple[int, int] = (0, 0),
    model: str = "claude",
) -> Any:
    """stage_runs를 직접 fabrication한 terminal run."""
    run = store.create_run("dev_review", "0.1.0", {"task": "t"}, trace_id="t")
    for key, out in (stages or {}).items():
        sr = store.start_stage(run.id, key, 1, {}, model=model)
        store.finish_stage(
            sr.id,
            status=StageStatus.SUCCEEDED,
            output=out,
            tokens_in=tokens[0],
            tokens_out=tokens[1],
        )
    store.update_run(run.id, status=status)
    return run


def _mk_eval_run(store: Store, eval_id: str, run_id: str) -> dict:
    return store.create_eval_run(eval_id, run_id)


# ---------------- CRUD + 검증 ----------------


def test_eval_crud(tmp_path: Path) -> None:
    client, store = _env(tmp_path)
    ev = _mk_eval(client, "Q4 회귀")
    assert ev["name"] == "Q4 회귀"
    assert ev["status"] == "active"
    assert ev["workflow_key"] == "dev_review"
    assert ev["input"] == {"task": "fix the bug"}
    assert ev["checks"] == [{"type": "stage_completed", "stage": "qa"}]
    assert "last_score" in ev and "last_run_at" in ev

    # 목록 + 상세
    items = client.get("/api/v1/evals").json()["items"]
    assert [i["id"] for i in items] == [ev["id"]]
    assert client.get(f"/api/v1/evals/{ev['id']}").json()["name"] == "Q4 회귀"
    assert client.get("/api/v1/evals/nope").status_code == 404

    # PATCH 부분 갱신
    r = client.patch(
        f"/api/v1/evals/{ev['id']}",
        json={"name": "새 이름", "checks": [{"type": "output_contains", "value": "ok"}]},
    )
    assert r.status_code == 200
    assert r.json()["name"] == "새 이름"
    assert r.json()["checks"] == [{"type": "output_contains", "value": "ok"}]
    assert r.json()["input"] == {"task": "fix the bug"}  # 미전송 필드 유지

    # status 필터 + 잘못된 status
    assert client.get("/api/v1/evals?status=bogus").status_code == 422
    assert len(client.get("/api/v1/evals?status=active").json()["items"]) == 1

    # DELETE → 소프트 삭제, 이력 보존
    r = client.delete(f"/api/v1/evals/{ev['id']}")
    assert r.status_code == 200 and r.json()["status"] == "archived"
    assert store.get_eval(ev["id"])["status"] == "archived"
    assert client.delete("/api/v1/evals/nope").status_code == 404


def test_eval_create_validation(tmp_path: Path) -> None:
    client, _ = _env(tmp_path)
    base = {"name": "e", "workflow_key": "dev_review", "input": {"task": "t"}, "checks": []}
    # 빈 task
    r = client.post("/api/v1/evals", json={**base, "input": {"task": "  "}})
    assert r.status_code == 422
    r = client.post("/api/v1/evals", json={**base, "input": {}})
    assert r.status_code == 422
    # task 없음/비문자열
    r = client.post("/api/v1/evals", json={**base, "input": {"task": 3}})
    assert r.status_code == 422
    # 알 수 없는 workflow
    r = client.post("/api/v1/evals", json={**base, "workflow_key": "nope"})
    assert r.status_code == 422
    # 잘못된 check type
    r = client.post(
        "/api/v1/evals",
        json={**base, "checks": [{"type": "llm_judge", "stage": "qa"}]},
    )
    assert r.status_code == 422
    # stage_completed에 stage 없음
    r = client.post("/api/v1/evals", json={**base, "checks": [{"type": "stage_completed"}]})
    assert r.status_code == 422
    # output_regex 잘못된 패턴
    r = client.post(
        "/api/v1/evals",
        json={**base, "checks": [{"type": "output_regex", "pattern": "(["}]},
    )
    assert r.status_code == 422
    # json_path에 equals/contains 둘 다 없음
    r = client.post("/api/v1/evals", json={**base, "checks": [{"type": "json_path", "path": "a"}]})
    assert r.status_code == 422
    # max_cost_usd 음수
    r = client.post(
        "/api/v1/evals",
        json={**base, "checks": [{"type": "max_cost_usd", "value": -1}]},
    )
    assert r.status_code == 422
    # 빈 checks는 허용
    r = client.post("/api/v1/evals", json=base)
    assert r.status_code == 201


def test_eval_validation_size_and_value_caps(tmp_path: Path) -> None:
    """ReDoS/대형 페이로드 방지 캡 + 빈 contains 값 거부 (회귀)."""
    client, _ = _env(tmp_path)
    base = {"name": "e", "workflow_key": "dev_review", "input": {"task": "t"}, "checks": []}

    # output_contains/output_not_contains 빈 문자열 — trivially 통과 방지
    for ct in ("output_contains", "output_not_contains"):
        r = client.post(
            "/api/v1/evals",
            json={**base, "checks": [{"type": ct, "value": ""}]},
        )
        assert r.status_code == 422, ct
        r = client.post(
            "/api/v1/evals",
            json={**base, "checks": [{"type": ct, "value": "   "}]},
        )
        assert r.status_code == 422, ct
    # json_path contains 빈 문자열도 동일하게 거부
    r = client.post(
        "/api/v1/evals",
        json={**base, "checks": [{"type": "json_path", "path": "a", "contains": ""}]},
    )
    assert r.status_code == 422

    # 문자열 필드 1000자 초과 거부
    r = client.post(
        "/api/v1/evals",
        json={**base, "checks": [{"type": "output_contains", "value": "x" * 1001}]},
    )
    assert r.status_code == 422
    r = client.post(
        "/api/v1/evals",
        json={**base, "checks": [{"type": "output_regex", "pattern": "x" * 1001}]},
    )
    assert r.status_code == 422
    r = client.post(
        "/api/v1/evals",
        json={**base, "checks": [{"type": "json_path", "path": "a" * 1001, "equals": 1}]},
    )
    assert r.status_code == 422

    # 중첩 정량자 ReDoS 휴리스틱 — `(a+)+` 류 거부, 정상 패턴은 허용
    for bad in ("(a+)+", "(a*)*", "(x|y+)*"):
        r = client.post(
            "/api/v1/evals",
            json={**base, "checks": [{"type": "output_regex", "pattern": bad}]},
        )
        assert r.status_code == 422, bad
    r = client.post(
        "/api/v1/evals",
        json={
            **base,
            "name": "e-regex-ok",
            "checks": [{"type": "output_regex", "pattern": r"decision:\s*pass"}],
        },
    )
    assert r.status_code == 201

    # input.task 20000자 초과 거부 (RunCreateRequest와 동일 상한)
    r = client.post(
        "/api/v1/evals", json={**base, "name": "e-task", "input": {"task": "t" * 20001}}
    )
    assert r.status_code == 422

    # input+checks 직렬화 200KB 초과 거부 — 필드 캡을 우회하는 대형 페이로드
    r = client.post(
        "/api/v1/evals",
        json={**base, "name": "e-blob", "input": {"task": "t", "blob": "x" * 210_000}},
    )
    assert r.status_code == 422


def test_eval_name_conflict_case_insensitive(tmp_path: Path) -> None:
    client, _ = _env(tmp_path)
    _mk_eval(client, "MyEval")
    r = client.post(
        "/api/v1/evals",
        json={
            "name": "myeval",
            "workflow_key": "dev_review",
            "input": {"task": "t"},
            "checks": [],
        },
    )
    assert r.status_code == 409
    # PATCH 시 자기 자신 이름은 허용
    ev = client.get("/api/v1/evals").json()["items"][0]
    r = client.patch(f"/api/v1/evals/{ev['id']}", json={"name": "MYEVAL"})
    assert r.status_code == 200


def test_eval_role_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, store = _client(tmp_path, monkeypatch)
    _login(store, client, "m@x.com", "member")
    body = {
        "name": "e",
        "workflow_key": "dev_review",
        "input": {"task": "t"},
        "checks": [],
    }
    assert client.post("/api/v1/evals", json=body).status_code == 403
    assert client.patch("/api/v1/evals/x", json={"name": "y"}).status_code == 403
    assert client.delete("/api/v1/evals/x").status_code == 403
    assert client.post("/api/v1/evals/x/run").status_code == 403
    # GET도 lead+ — 픽스처가 민감 정보를 담을 수 있고 UI 뷰도 lead 전용.
    assert client.get("/api/v1/evals").status_code == 403
    assert client.get("/api/v1/evals/x").status_code == 403
    assert client.get("/api/v1/evals/x/runs").status_code == 403
    _login(store, client, "l@x.com", "lead")
    assert client.get("/api/v1/evals").status_code == 200
    assert client.post("/api/v1/evals", json=body).status_code == 201


# ---------------- run 생성 연결 ----------------


def test_eval_run_creates_linked_run(tmp_path: Path) -> None:
    client, store = _env(tmp_path)
    ev = _mk_eval(client)
    r = client.post(f"/api/v1/evals/{ev['id']}/run")
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["status"] == "pending"
    assert data["run_id"] and data["eval_run_id"]
    # eval_run ↔ run 링크
    er = store.get_eval_run(data["eval_run_id"])
    assert er is not None and er["run_id"] == data["run_id"] and er["eval_id"] == ev["id"]
    run = store.get_run(data["run_id"])
    assert run is not None
    assert run.workflow_key == "dev_review"
    assert run.input.get("task") == "fix the bug"
    # run.metadata에 eval 마커 — run detail에서도 조회 가능
    assert run.metadata.get("eval_id") == ev["id"]
    assert run.metadata.get("eval_run_id") == data["eval_run_id"]
    detail = client.get(f"/api/v1/runs/{data['run_id']}").json()
    assert detail["metadata"]["eval_id"] == ev["id"]
    # 목록에서 eval_run 조회 — run이 non-terminal(waiting/running)이면 pending 유지
    items = client.get(f"/api/v1/evals/{ev['id']}/runs").json()["items"]
    assert len(items) == 1 and items[0]["id"] == data["eval_run_id"]


def test_eval_run_archived_rejected(tmp_path: Path) -> None:
    client, _ = _env(tmp_path)
    ev = _mk_eval(client)
    assert client.delete(f"/api/v1/evals/{ev['id']}").status_code == 200
    r = client.post(f"/api/v1/evals/{ev['id']}/run")
    assert r.status_code == 409
    assert client.post("/api/v1/evals/nope/run").status_code == 404


def test_eval_run_budget_blocked(tmp_path: Path) -> None:
    client, store = _env(tmp_path)
    ev = _mk_eval(client)
    # 지출을 만들고 global hard-stop 한도를 그보다 작게 설정
    run = _mk_run(store, {"qa": {"decision": "pass"}}, tokens=(1_000_000, 1_000_000))
    assert run.id
    store.upsert_budget_policy(
        "global", "", amount_usd=0.000001, warn_percent=80, hard_stop_enabled=True
    )
    r = client.post(f"/api/v1/evals/{ev['id']}/run")
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "budget_exceeded"
    # 차단된 요청은 eval_run을 만들지 않는다
    assert client.get(f"/api/v1/evals/{ev['id']}/runs").json()["items"] == []


# ---------------- lazy 채점 ----------------


def test_lazy_scoring_on_terminal_run(tmp_path: Path) -> None:
    client, store = _env(tmp_path)
    ev = _mk_eval(
        client,
        checks=[
            {"type": "stage_completed", "stage": "qa"},
            {"type": "output_contains", "value": "pass"},
        ],
    )
    run = _mk_run(store, {"qa": {"decision": "pass", "rationale": "all good"}})
    er = _mk_eval_run(store, ev["id"], run.id)
    assert er["status"] == "pending"
    items = client.get(f"/api/v1/evals/{ev['id']}/runs").json()["items"]
    assert len(items) == 1
    it = items[0]
    assert it["status"] == "scored"
    assert it["score"] == 100.0
    assert len(it["results"]) == 2
    assert all(r["passed"] for r in it["results"])
    assert it["scored_at"]
    # 멱등 — 재조회해도 같은 결과, 재채점 없음
    it2 = client.get(f"/api/v1/evals/{ev['id']}/runs").json()["items"][0]
    assert it2["scored_at"] == it["scored_at"] and it2["score"] == 100.0
    # eval 목록에 last_score 반영
    ev2 = client.get("/api/v1/evals").json()["items"][0]
    assert ev2["last_score"] == 100.0 and ev2["last_run_at"]


def test_lazy_scoring_failed_run_low_score(tmp_path: Path) -> None:
    client, store = _env(tmp_path)
    ev = _mk_eval(client, checks=[{"type": "stage_completed", "stage": "qa"}])
    run = _mk_run(store, {}, status=RunStatus.FAILED)
    _mk_eval_run(store, ev["id"], run.id)
    it = client.get(f"/api/v1/evals/{ev['id']}/runs").json()["items"][0]
    assert it["status"] == "scored"
    assert it["score"] == 0.0
    assert it["results"][0]["passed"] is False


def test_lazy_scoring_pending_run_untouched(tmp_path: Path) -> None:
    client, store = _env(tmp_path)
    ev = _mk_eval(client)
    run = store.create_run("dev_review", "0.1.0", {"task": "t"}, trace_id="t")
    _mk_eval_run(store, ev["id"], run.id)  # run은 queued — 비종결
    it = client.get(f"/api/v1/evals/{ev['id']}/runs").json()["items"][0]
    assert it["status"] == "pending" and it["score"] is None


def test_empty_checks_score_null(tmp_path: Path) -> None:
    client, store = _env(tmp_path)
    ev = _mk_eval(client, checks=[])
    run = _mk_run(store, {"qa": {"decision": "pass"}})
    _mk_eval_run(store, ev["id"], run.id)
    it = client.get(f"/api/v1/evals/{ev['id']}/runs").json()["items"][0]
    assert it["status"] == "scored"
    assert it["score"] is None and it["results"] == []


# ---------------- 채점 엔진 (체크 타입별) ----------------


_score_seq = 0


def _score(store: Store, checks: list[dict[str, Any]], stages: dict[str, Any], **kw) -> dict:
    global _score_seq
    _score_seq += 1
    ev = store.create_eval(f"e{_score_seq}", "dev_review", {"task": "t"}, checks)
    assert ev is not None
    run = _mk_run(store, stages, **kw)
    score, results, err = evals_svc.score_eval_run(store, ev, run.id)
    return {"score": score, "results": results, "error": err}


def test_check_stage_completed(tmp_path: Path) -> None:
    _, store = _env(tmp_path)
    out = _score(
        store,
        [{"type": "stage_completed", "stage": "qa"}],
        {"qa": {"decision": "pass"}},
    )
    assert out["score"] == 100.0 and out["results"][0]["passed"]
    # stage 없음 → 실패
    out = _score(store, [{"type": "stage_completed", "stage": "missing"}], {"qa": {}})
    assert out["score"] == 0.0
    # status 오버라이드 — failed 기대
    run = _mk_run(store, {"qa": {}})
    sr = store.start_stage(run.id, "qa2", 1, {})
    store.finish_stage(sr.id, status=StageStatus.FAILED, output={})
    store.update_run(run.id, status=RunStatus.FAILED)
    ev = store.create_eval(
        "e-status-override",
        "dev_review",
        {"task": "t"},
        [{"type": "stage_completed", "stage": "qa2", "status": "failed"}],
    )
    score, results, _ = evals_svc.score_eval_run(store, ev, run.id)
    assert score == 100.0 and results[0]["passed"]


def test_check_output_text_checks(tmp_path: Path) -> None:
    _, store = _env(tmp_path)
    stages = {"qa": {"decision": "pass", "rationale": "all good"}}
    out = _score(
        store,
        [
            {"type": "output_contains", "value": "all good"},
            {"type": "output_not_contains", "value": "secret-token"},
        ],
        stages,
    )
    assert out["score"] == 100.0
    out = _score(
        store,
        [
            {"type": "output_contains", "value": "nope"},
            {"type": "output_not_contains", "value": "all good"},
        ],
        stages,
    )
    assert out["score"] == 0.0


def test_check_output_regex(tmp_path: Path) -> None:
    _, store = _env(tmp_path)
    out = _score(
        store,
        [{"type": "output_regex", "pattern": r"decision:\s*pass"}],
        {"qa": {"decision": "pass"}},
    )
    assert out["score"] == 100.0
    out = _score(
        store,
        [{"type": "output_regex", "pattern": r"decision:\s*fail"}],
        {"qa": {"decision": "pass"}},
    )
    assert out["score"] == 0.0
    # 검증을 우회해 저장된 잘못된 regex도 크래시 없이 체크 실패
    ev = store.create_eval(
        "badre", "dev_review", {"task": "t"}, [{"type": "output_regex", "pattern": "(["}]
    )
    run = _mk_run(store, {"qa": {"decision": "pass"}})
    score, results, _ = evals_svc.score_eval_run(store, ev, run.id)
    assert score == 0.0 and "invalid regex" in results[0]["detail"]


def test_check_json_path(tmp_path: Path) -> None:
    _, store = _env(tmp_path)
    stages = {
        "qa": {"decision": "pass", "items": [{"name": "a"}, {"name": "b"}]},
        "analysis": {"requirements": "fix bug"},
    }
    out = _score(
        store,
        [
            {"type": "json_path", "path": "qa.decision", "equals": "pass"},
            {"type": "json_path", "path": "qa.items[1].name", "equals": "b"},
            {"type": "json_path", "path": "analysis.requirements", "contains": "bug"},
            {"type": "json_path", "path": "qa.missing", "equals": "x"},
        ],
        stages,
    )
    assert out["score"] == 75.0
    assert out["results"][3]["passed"] is False
    # 경로가 가리키는 값이 JSON 문자열이면 파싱해 비교한다
    # (stage output 자체는 항상 dict — _loads가 보장).
    out = _score(
        store,
        [{"type": "json_path", "path": "qa.payload", "equals": {"decision": "pass"}}],
        {"qa": {"payload": '{"decision": "pass"}'}},
    )
    assert out["score"] == 100.0


def test_check_max_cost_usd(tmp_path: Path) -> None:
    _, store = _env(tmp_path)
    # claude 단가 (3.0 in / 15.0 out $/1M) — 100k in + 0 out = $0.30
    out = _score(
        store,
        [
            {"type": "max_cost_usd", "value": 0.5},
            {"type": "max_cost_usd", "value": 0.1},
        ],
        {"qa": {"decision": "pass"}},
        tokens=(100_000, 0),
    )
    assert out["score"] == 50.0
    assert "0.3" in out["results"][0]["detail"]


def test_score_math_partial(tmp_path: Path) -> None:
    _, store = _env(tmp_path)
    out = _score(
        store,
        [
            {"type": "stage_completed", "stage": "qa"},
            {"type": "stage_completed", "stage": "missing"},
            {"type": "output_contains", "value": "pass"},
            {"type": "output_contains", "value": "zzz"},
        ],
        {"qa": {"decision": "pass"}},
    )
    assert out["score"] == 50.0


# ---------------- API run → lazy 채점 E2E ----------------


def test_api_run_scores_after_completion(tmp_path: Path) -> None:
    """POST /evals/{id}/run으로 만든 run이 terminal이 되면 lazy 채점된다."""
    client, store = _env(tmp_path)
    ev = _mk_eval(
        client,
        checks=[{"type": "stage_completed", "stage": "no_such_stage"}],
    )
    r = client.post(f"/api/v1/evals/{ev['id']}/run")
    assert r.status_code == 201
    run_id = r.json()["run_id"]
    # 백그라운드 실행(scripted_llm)이 run을 waiting_approval 등 안정 상태까지
    # 진행할 때까지 기다린 뒤 terminal로 마감 — 도중에 FAILED를 쓰면 runner가
    # 상태를 덮어써 비종결로 남을 수 있다.
    deadline = time.time() + 15
    while time.time() < deadline:
        run = store.get_run(run_id)
        if run is not None and run.status.value not in ("queued", "running"):
            break
        time.sleep(0.2)
    store.update_run(run_id, status=RunStatus.FAILED, error="test")
    deadline = time.time() + 10
    scored = None
    while time.time() < deadline:
        items = client.get(f"/api/v1/evals/{ev['id']}/runs").json()["items"]
        if items and items[0]["status"] == "scored":
            scored = items[0]
            break
        time.sleep(0.2)
    assert scored is not None
    assert scored["run_id"] == run_id
    # qa stage가 없으므로 stage_completed는 실패 → score 0
    assert scored["score"] == 0.0


# ---------------- 계약/회귀 (리뷰 반영) ----------------


def test_eval_run_results_shape(tmp_path: Path) -> None:
    """results 항목은 {check_type, check(원본 객체), passed, detail}."""
    client, store = _env(tmp_path)
    chk = {"type": "output_contains", "value": "pass"}
    ev = _mk_eval(client, checks=[chk, {"type": "stage_completed", "stage": "qa"}])
    run = _mk_run(store, {"qa": {"decision": "pass"}})
    _mk_eval_run(store, ev["id"], run.id)
    it = client.get(f"/api/v1/evals/{ev['id']}/runs").json()["items"][0]
    assert it["status"] == "scored"
    r0 = it["results"][0]
    assert r0["check_type"] == "output_contains"
    assert r0["check"] == chk  # 원본 객체 보존 — 프론트가 툴팁에 표시
    assert r0["passed"] is True and r0["detail"]
    assert it["results"][1]["check_type"] == "stage_completed"


def test_json_path_dollar_prefix_tolerated(tmp_path: Path) -> None:
    """json_path는 선행 `$.`/`$`를 벗겨 해석한다(프론트 placeholder 관용)."""
    _, store = _env(tmp_path)
    out = _score(
        store,
        [{"type": "json_path", "path": "$.qa.decision", "equals": "pass"}],
        {"qa": {"decision": "pass"}},
    )
    assert out["score"] == 100.0
    out = _score(
        store,
        [{"type": "json_path", "path": "$qa.items[0].name", "equals": "a"}],
        {"qa": {"items": [{"name": "a"}]}},
    )
    assert out["score"] == 100.0


def test_last_score_ignores_pending_newest_run(tmp_path: Path) -> None:
    """마지막 eval_run이 pending(score null)이어도 last_score는 최신 채점 점수."""
    client, store = _env(tmp_path)
    ev = _mk_eval(client)
    # 1) 채점된 run — score 기록
    scored_run = _mk_run(store, {"qa": {"decision": "pass"}})
    er1 = _mk_eval_run(store, ev["id"], scored_run.id)
    store.mark_eval_run_scored(er1["id"], score=100.0, results=[])
    time.sleep(0.01)  # created_at 정렬 보장
    # 2) 그보다 최신의 pending run — run은 비종결
    pending_run = store.create_run("dev_review", "0.1.0", {"task": "t"}, trace_id="t2")
    _mk_eval_run(store, ev["id"], pending_run.id)
    item = client.get("/api/v1/evals").json()["items"][0]
    assert item["last_score"] == 100.0
    # last_run_at은 가장 최근(pending) run 시각
    latest = store.latest_eval_runs()[ev["id"]]
    assert item["last_run_at"] == latest["created_at"]


def test_eval_patch_name_conflict_409(tmp_path: Path) -> None:
    """PATCH name이 다른 eval과 충돌하면 404가 아니라 409."""
    client, _ = _env(tmp_path)
    _mk_eval(client, "first")
    second = _mk_eval(client, "second")
    r = client.patch(f"/api/v1/evals/{second['id']}", json={"name": "FIRST"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "conflict"


def test_retention_sweep_eval_runs(tmp_path: Path) -> None:
    """retention_sweep는 365d 지난 scored/error eval_runs만 삭제, pending 보존."""
    from datetime import UTC, datetime, timedelta

    _, store = _env(tmp_path)
    ev = store.create_eval("e", "dev_review", {"task": "t"}, [])
    assert ev is not None
    run = _mk_run(store, {})
    old = _mk_eval_run(store, ev["id"], run.id)
    store.mark_eval_run_scored(old["id"], score=1.0, results=[])
    pend = _mk_eval_run(store, ev["id"], run.id)
    # created_at을 과거로 이동 — pending은 유지돼야 한다
    past = (datetime.now(UTC) - timedelta(days=400)).isoformat()
    with store.tx() as c:
        c.execute("UPDATE eval_runs SET created_at=? WHERE id=?", (past, old["id"]))
        c.execute("UPDATE eval_runs SET created_at=? WHERE id=?", (past, pend["id"]))
    counts = store.retention_sweep()
    assert counts["eval_runs"] == 1  # scored만 삭제, pending 보존
    assert store.get_eval_run(old["id"]) is None
    assert store.get_eval_run(pend["id"]) is not None
    assert store.get_eval(ev["id"]) is not None  # 픽스처 자체는 유지
