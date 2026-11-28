"""spec 008 US2 — 온라인 평가 스케줄 CRUD/검증 + 온라인 eval 발화/dedupe/inbox.

서비스 계층은 임시 SQLite Store + orgctx.org_scope로 조직을 지정해 검증하고,
API 계층은 create_app(TestClient)으로 라우트 계약을 검증한다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from jobmate.api.app import create_app
from jobmate.config import Settings
from jobmate.models import RunStatus, StageStatus
from jobmate.security import SESSION_COOKIE, hash_password, hash_token, new_session_token
from jobmate.services import online_evals
from jobmate.store import orgctx
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
    client, store = _env(tmp_path)
    client.headers["X-API-Key"] = "test-api-key"
    return client, store


def _login(store: Store, client: TestClient, email: str, role: str = "member") -> None:
    u = store.get_user_by_email(email) or store.create_user(
        email, hash_password("Str0ng#Passw0rd!"), email
    )
    assert u is not None
    store.set_user_role(u["id"], role)
    token = new_session_token()
    store.create_session(u["id"], hash_token(token))
    client.headers.pop("X-API-Key", None)
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE, token)


def _first_org(store: Store) -> str:
    """Store 기동 시 생성되는 기본 조직 id — org='' 레거시가 아닌 실제 org."""
    orgs = store.list_organizations()
    assert orgs
    return str(orgs[0]["id"])


def _mk_eval(
    store: Store,
    name: str = "eval1",
    checks: list[dict[str, Any]] | None = None,
    org: str = "",
):
    fields = (
        name,
        "dev_review",
        {"task": "fix the bug"},
        checks if checks is not None else [{"type": "stage_completed", "stage": "qa"}],
    )
    if org:
        with orgctx.org_scope(org):
            return store.create_eval(*fields)
    return store.create_eval(*fields)


def _mk_run(
    store: Store,
    stages: dict[str, dict[str, Any]] | None = None,
    *,
    status: RunStatus = RunStatus.COMPLETED,
    workflow: str = "dev_review",
    org: str = "",
) -> Any:
    def _build() -> Any:
        run = store.create_run(workflow, "0.1.0", {"task": "t"}, trace_id="t")
        for key, out in (stages or {}).items():
            sr = store.start_stage(run.id, key, 1, {})
            store.finish_stage(sr.id, status=StageStatus.SUCCEEDED, output=out)
        store.update_run(run.id, status=status)
        return run

    if org:
        with orgctx.org_scope(org):
            return _build()
    return _build()


def _mk_schedule(store: Store, org: str = "", **kw) -> dict[str, Any]:
    fields = {
        "name": "sched1",
        "target": "run_completed",
        "eval_name": "eval1",
        "sample_rate": 1.0,
        "enabled": True,
    }
    fields.update(kw)
    if org:
        with orgctx.org_scope(org):
            return store.create_eval_schedule(**fields)
    return store.create_eval_schedule(**fields)


# ---------------- API: 스케줄 CRUD + 검증 ----------------


def test_schedule_crud(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, store = _client(tmp_path, monkeypatch)
    _mk_eval(store, "eval1")
    body = {
        "name": "prod evals",
        "target": "run_completed",
        "workflow_key": "dev_review",
        "sample_rate": 0.25,
        "eval_name": "eval1",
        "enabled": True,
    }
    r = client.post("/api/v1/evals/schedules", json=body)
    assert r.status_code == 201, r.text
    item = r.json()["item"]
    assert item["name"] == "prod evals"
    assert item["target"] == "run_completed"
    assert item["sample_rate"] == 0.25
    assert item["eval_name"] == "eval1"
    sid = item["id"]

    items = client.get("/api/v1/evals/schedules").json()["items"]
    assert [s["id"] for s in items] == [sid]

    r = client.patch(f"/api/v1/evals/schedules/{sid}", json={"sample_rate": 0.5, "enabled": False})
    assert r.status_code == 200, r.text
    assert r.json()["item"]["sample_rate"] == 0.5
    assert r.json()["item"]["enabled"] is False

    r = client.delete(f"/api/v1/evals/schedules/{sid}")
    assert r.status_code == 200
    assert client.get("/api/v1/evals/schedules").json()["items"] == []
    assert client.delete("/api/v1/evals/schedules/nope").status_code == 404
    store.close()


def test_schedule_validation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, store = _client(tmp_path, monkeypatch)
    base = {"name": "s", "eval_name": "eval1"}
    # target enum
    r = client.post("/api/v1/evals/schedules", json={**base, "target": "bogus"})
    assert r.status_code == 422
    # sample_rate 범위
    for bad in (-0.1, 1.1):
        r = client.post("/api/v1/evals/schedules", json={**base, "sample_rate": bad})
        assert r.status_code == 422
    # 빈 필수값
    r = client.post("/api/v1/evals/schedules", json={"name": "", "eval_name": "e"})
    assert r.status_code == 422
    r = client.post("/api/v1/evals/schedules", json={"name": "s", "eval_name": ""})
    assert r.status_code == 422
    # 알 수 없는 workflow_key
    r = client.post(
        "/api/v1/evals/schedules",
        json={**base, "workflow_key": "no_such_wf"},
    )
    assert r.status_code == 422
    # 정상 생성(기본값)
    r = client.post("/api/v1/evals/schedules", json=base)
    assert r.status_code == 201, r.text
    assert r.json()["item"]["target"] == "run_completed"
    assert r.json()["item"]["sample_rate"] == 1.0
    store.close()


def test_schedule_role_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, store = _client(tmp_path, monkeypatch)
    _login(store, client, "m@x.com", "member")
    body = {"name": "s", "eval_name": "eval1"}
    # member는 schedules 목록도 lead+ — evals 리소스와 동일 게이트
    assert client.get("/api/v1/evals/schedules").status_code == 403
    assert client.post("/api/v1/evals/schedules", json=body).status_code == 403
    assert client.patch("/api/v1/evals/schedules/x", json={"enabled": False}).status_code == 403
    assert client.delete("/api/v1/evals/schedules/x").status_code == 403
    assert client.get("/api/v1/evals/schedules/x/history").status_code == 404  # 없으면 404
    _login(store, client, "l@x.com", "lead")
    assert client.get("/api/v1/evals/schedules").status_code == 200
    assert client.post("/api/v1/evals/schedules", json=body).status_code == 201
    store.close()


# ---------------- 서비스: 온라인 eval 발화 ----------------


def test_online_eval_fires_on_run_completed(tmp_path: Path) -> None:
    _client_env, store = _env(tmp_path)
    oid = _first_org(store)
    _mk_eval(store, "eval1", checks=[{"type": "stage_completed", "stage": "qa"}], org=oid)
    sched = _mk_schedule(store, org=oid)
    run = _mk_run(store, {"qa": {"decision": "pass"}}, org=oid)

    n = online_evals.on_run_terminal(store, run.id)
    assert n == 1
    ers = store._conn.execute(  # noqa: SLF001
        "SELECT * FROM eval_runs WHERE schedule_id=?", (sched["id"],)
    ).fetchall()
    assert len(ers) == 1
    er = store._eval_run_json(ers[0])  # noqa: SLF001
    assert er["source"] == "online"
    assert er["schedule_id"] == sched["id"]
    assert er["status"] == "scored"
    assert er["score"] == 100.0
    store.close()


def test_online_eval_dedupe(tmp_path: Path) -> None:
    _client_env, store = _env(tmp_path)
    oid = _first_org(store)
    _mk_eval(store, "eval1", org=oid)
    sched = _mk_schedule(store, org=oid)
    run = _mk_run(store, {"qa": {}}, org=oid)
    assert online_evals.on_run_terminal(store, run.id) == 1
    # 재호출(스윕/재시도 경로) — dedupe로 추가 생성 없음
    assert online_evals.on_run_terminal(store, run.id) == 0
    assert online_evals.evaluate_run(store, run.id) == 0
    n = store._conn.execute(  # noqa: SLF001
        "SELECT COUNT(*) AS n FROM eval_runs WHERE schedule_id=?", (sched["id"],)
    ).fetchone()["n"]
    assert n == 1
    store.close()


def test_online_eval_sample_rate_gate(tmp_path: Path) -> None:
    """M6 — sample_rate는 (schedule_id, run_id) 결정적 draw 1회.

    stage 수와 무관하게 같은 판정이 나와야 하므로 _sampled_for를 직접
    검증하고, 통합 경로는 draw 예측과 결과가 일치하는지 확인한다."""
    _client_env, store = _env(tmp_path)
    oid = _first_org(store)
    # 결정성/분포 — 같은 입력은 항상 같은 결과, 0<rate<1은 양쪽 결과 존재
    sched_probe = {"id": "sched-probe", "sample_rate": 0.5}
    outcomes = {online_evals._sampled_for(sched_probe, f"run-{i}") for i in range(200)}
    assert outcomes == {True, False}
    assert online_evals._sampled_for(sched_probe, "run-0") == online_evals._sampled_for(
        sched_probe, "run-0"
    )
    # 경계 — 0이면 항상 스킵, 1이면 항상 발화
    assert not online_evals._sampled_for({"id": "s", "sample_rate": 0.0}, "r")
    assert online_evals._sampled_for({"id": "s", "sample_rate": 1.0}, "r")

    _mk_eval(store, "eval1", org=oid)
    sched = _mk_schedule(store, org=oid, sample_rate=0.5)
    run = _mk_run(store, {"qa": {}}, org=oid)
    expect = online_evals._sampled_for(sched, run.id)
    assert online_evals.on_run_terminal(store, run.id) == (1 if expect else 0)
    store.close()


def test_online_eval_orgless_run_skipped(tmp_path: Path) -> None:
    """M4 — org_id='' run은 온라인 평가 대상이 아니다(cross-org 방지)."""
    _client_env, store = _env(tmp_path)
    oid = _first_org(store)
    _mk_eval(store, "eval1", org=oid)
    _mk_schedule(store, org=oid)
    # org_scope 없이 생성 → run.org_id='' (레거시/미귀속 행과 동일 상태)
    run = _mk_run(store, {"qa": {}})
    assert online_evals.on_run_terminal(store, run.id) == 0
    assert online_evals.evaluate_run(store, run.id) == 0
    n = store._conn.execute(  # noqa: SLF001
        "SELECT COUNT(*) AS n FROM eval_runs"
    ).fetchone()["n"]
    assert n == 0
    store.close()


def test_online_eval_dedupe_unique_index(tmp_path: Path) -> None:
    """M3 — (schedule_id, run_id) UNIQUE 부분 인덱스 + integrity→기존 행 반환."""
    _client_env, store = _env(tmp_path)
    oid = _first_org(store)
    ev = _mk_eval(store, "eval1", org=oid)
    sched = _mk_schedule(store, org=oid)
    run = _mk_run(store, {"qa": {}}, org=oid)
    er1 = store.create_online_eval_run(str(ev["id"]), run.id, sched["id"])
    # 같은 키 재생성 — 인덱스 충돌을 잡아 기존 행을 반환(새 행 아님)
    er2 = store.create_online_eval_run(er1["eval_id"], run.id, sched["id"])
    assert er2["id"] == er1["id"]
    n = store._conn.execute(  # noqa: SLF001
        "SELECT COUNT(*) AS n FROM eval_runs WHERE schedule_id=? AND run_id=?",
        (sched["id"], run.id),
    ).fetchone()["n"]
    assert n == 1
    # 인덱스 실재 확인
    idx = store._conn.execute(  # noqa: SLF001
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_eval_runs_online_dedupe'"
    ).fetchone()
    assert idx is not None
    store.close()


def test_online_eval_stage_completed_target(tmp_path: Path) -> None:
    _client_env, store = _env(tmp_path)
    oid = _first_org(store)
    _mk_eval(store, "eval1", org=oid)
    _mk_schedule(store, target="stage_completed", org=oid)
    run = _mk_run(store, {"analysis": {}, "qa": {}}, org=oid)
    # run_completed 스케줄이 없으므로 stage_completed만 발화
    n = online_evals.on_run_terminal(store, run.id)
    assert n == 1  # (schedule_id, run_id) dedupe — 첫 terminal stage 1건
    store.close()


def test_online_eval_workflow_filter(tmp_path: Path) -> None:
    _client_env, store = _env(tmp_path)
    oid = _first_org(store)
    _mk_eval(store, "eval1", org=oid)
    # workflow_key 불일치 스케줄 — dev_review run에 발화하지 않음
    _mk_schedule(store, workflow_key="gmail_calendar", org=oid)
    # workflow_key 일치 스케줄
    _mk_schedule(store, name="sched2", workflow_key="dev_review", org=oid)
    run = _mk_run(store, {"qa": {}}, org=oid)
    n = online_evals.on_run_terminal(store, run.id)
    assert n == 1
    store.close()


def test_online_eval_disabled_and_missing_eval(tmp_path: Path) -> None:
    _client_env, store = _env(tmp_path)
    oid = _first_org(store)
    _mk_eval(store, "eval1", org=oid)
    _mk_schedule(store, enabled=False, org=oid)
    _mk_schedule(store, name="sched2", eval_name="no_such_eval", org=oid)
    run = _mk_run(store, {"qa": {}}, org=oid)
    assert online_evals.on_run_terminal(store, run.id) == 0
    store.close()


def test_online_eval_failure_inbox(tmp_path: Path) -> None:
    """score < 80 → inbox kind='eval' 알림 1건(dedupe)."""
    _client_env, store = _env(tmp_path)
    oid = _first_org(store)
    # qa stage가 없으므로 stage_completed 체크 실패 → score 0
    _mk_eval(
        store,
        "eval1",
        checks=[{"type": "stage_completed", "stage": "qa"}],
        org=oid,
    )
    _mk_schedule(store, org=oid)
    run = _mk_run(store, {"analysis": {}}, org=oid)  # qa 없음
    assert online_evals.on_run_terminal(store, run.id) == 1
    items = store.list_inbox(limit=50)
    eval_items = [i for i in items if i["kind"] == "eval"]
    assert len(eval_items) == 1
    assert "온라인 평가 불합격" in eval_items[0]["title"]
    # 재평가해도 dedupe — inbox도 1건 유지
    online_evals.on_run_terminal(store, run.id)
    eval_items = [i for i in store.list_inbox(limit=50) if i["kind"] == "eval"]
    assert len(eval_items) == 1
    store.close()


def test_online_eval_org_isolation(tmp_path: Path) -> None:
    """org A 스케줄은 org B run에 발화하지 않는다."""
    _client_env, store = _env(tmp_path)
    org_a = store.create_organization("A", "orga")
    org_b = store.create_organization("B", "orgb")
    assert org_a and org_b
    with orgctx.org_scope(org_a):
        _mk_eval(store, "eval1")
        _mk_schedule(store)
    # org B 컨텍스트의 run — orgA 스케줄은 매칭되지 않아야 한다
    with orgctx.org_scope(org_b):
        run = store.create_run("dev_review", "0.1.0", {"task": "t"}, trace_id="t")
        store.update_run(run.id, status=RunStatus.COMPLETED)
        n = online_evals.evaluate_run(store, run.id)
        assert n == 0
    store.close()


def test_eval_schedule_history_endpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, store = _client(tmp_path, monkeypatch)
    # eval은 API로 생성 — 요청 org(defaultorg)와 스케줄 org가 일치해야
    # history의 org 필터를 통과한다(store 직접 생성은 org='' 레거시).
    r = client.post(
        "/api/v1/evals",
        json={
            "name": "eval1",
            "workflow_key": "dev_review",
            "input": {"task": "fix the bug"},
            "checks": [{"type": "stage_completed", "stage": "qa"}],
        },
    )
    assert r.status_code == 201, r.text
    ev = r.json()
    body = {"name": "s", "eval_name": "eval1", "sample_rate": 1.0}
    sid = client.post("/api/v1/evals/schedules", json=body).json()["item"]["id"]
    # 온라인 eval_run 1건 생성 — run도 eval과 같은 org에 둬야
    # history lazy 채점(get_run org 필터)이 run을 찾는다.
    sched = store.get_eval_schedule(sid)
    eval_org = str((store.get_eval(ev["id"]) or {}).get("org_id") or "")
    with orgctx.org_scope(eval_org):
        run = _mk_run(store, {"qa": {}})
    er = store.create_online_eval_run(ev["id"], run.id, sched["id"])
    r = client.get(f"/api/v1/evals/schedules/{sid}/history")
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert len(items) == 1
    it = items[0]
    assert it["id"] == er["id"]
    assert it["run_id"] == run.id
    assert it["source"] == "online"
    assert it["schedule_id"] == sid
    assert it["status"] in ("pending", "scored")
    # 없는 스케줄 → 404
    assert client.get("/api/v1/evals/schedules/nope/history").status_code == 404
    store.close()
