"""Goal 계층 단위 테스트 — CRUD/사이클 규칙/프로젝트·run 연동/인증 게이트.

임시 SQLite Store + create_app(TestClient)으로 검증한다.
네트워크·LLM은 사용하지 않는다(run 생성은 store 직접 호출 또는
scripted_llm TestClient 경로).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jobmate import wakeups
from jobmate.api.app import create_app
from jobmate.config import Settings
from jobmate.models import RunStatus
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


def _mk(client: TestClient, title: str = "Goal", **kw) -> dict:
    body = {"title": title, **kw}
    r = client.post("/api/v1/goals", json=body)
    assert r.status_code == 201, r.text
    return r.json()


# ---------------- CRUD ----------------


def test_goal_crud(tmp_path: Path) -> None:
    client, store = _env(tmp_path)
    g = _mk(client, "Q4 목표", description="분기 목표")
    assert g["title"] == "Q4 목표"
    assert g["status"] == "active"
    assert g["parent_id"] is None
    assert g["children_count"] == 0
    assert g["created_at"] and g["updated_at"]

    # 목록 + children_count
    child = _mk(client, "하위 목표", parent_id=g["id"])
    items = {i["id"]: i for i in client.get("/api/v1/goals").json()["items"]}
    assert items[g["id"]]["children_count"] == 1
    assert items[child["id"]]["children_count"] == 0
    assert items[child["id"]]["parent_id"] == g["id"]

    # PATCH 부분 갱신
    r = client.patch(f"/api/v1/goals/{g['id']}", json={"status": "done", "title": "새 제목"})
    assert r.status_code == 200
    assert r.json()["status"] == "done" and r.json()["title"] == "새 제목"
    assert r.json()["description"] == "분기 목표"  # 미전송 필드 유지

    # parent_id=null → 루트 해제
    r = client.patch(f"/api/v1/goals/{child['id']}", json={"parent_id": None})
    assert r.status_code == 200 and r.json()["parent_id"] is None

    # PATCH unknown → 404
    assert client.patch("/api/v1/goals/nope", json={"title": "x"}).status_code == 404

    # DELETE → 소프트 삭제(archived), 행은 보존
    r = client.delete(f"/api/v1/goals/{g['id']}")
    assert r.status_code == 200 and r.json()["status"] == "archived"
    assert store.get_goal(g["id"])["status"] == "archived"
    assert client.delete("/api/v1/goals/nope").status_code == 404


def test_goal_status_filter(tmp_path: Path) -> None:
    client, _ = _env(tmp_path)
    _mk(client, "A")
    b = _mk(client, "B")
    client.patch(f"/api/v1/goals/{b['id']}", json={"status": "done"})

    assert len(client.get("/api/v1/goals").json()["items"]) == 2
    assert len(client.get("/api/v1/goals?status=active").json()["items"]) == 1
    done = client.get("/api/v1/goals?status=done").json()["items"]
    assert [i["id"] for i in done] == [b["id"]]
    assert client.get("/api/v1/goals?status=bogus").status_code == 422
    assert client.get("/api/v1/goals?status=").status_code == 200


def test_goal_validation(tmp_path: Path) -> None:
    client, _ = _env(tmp_path)
    # 빈 제목
    assert client.post("/api/v1/goals", json={"title": "  "}).status_code == 422
    # 잘못된 status
    r = client.post("/api/v1/goals", json={"title": "x", "status": "dead"})
    assert r.status_code == 422 and "status" in r.json()["error"]["message"]
    # 미존재 parent → 422
    r = client.post("/api/v1/goals", json={"title": "x", "parent_id": "ghost"})
    assert r.status_code == 422 and "not found" in r.json()["error"]["message"]


# ---------------- 계층 규칙 ----------------


def test_goal_parent_rules(tmp_path: Path) -> None:
    client, _ = _env(tmp_path)
    a = _mk(client, "A")
    b = _mk(client, "B", parent_id=a["id"])
    c = _mk(client, "C", parent_id=b["id"])

    # 자기 자신을 부모로 → 422
    r = client.patch(f"/api/v1/goals/{a['id']}", json={"parent_id": a["id"]})
    assert r.status_code == 422 and "own parent" in r.json()["error"]["message"]

    # 순환 a→b→c→a: a의 부모를 c로 → cycle
    r = client.patch(f"/api/v1/goals/{a['id']}", json={"parent_id": c["id"]})
    assert r.status_code == 422 and "cycle" in r.json()["error"]["message"]
    # c의 부모를 a로 → c→a→(a.parent=null)? a.parent는 여전히 null이므로
    # c→a 체인은 순환이 아니다 — 단 a의 조상이 c가 아니므로 허용돼야 한다.
    # (a→c 시도는 위에서 거부됐으므로 a는 루트 유지)
    r = client.patch(f"/api/v1/goals/{c['id']}", json={"parent_id": a["id"]})
    assert r.status_code == 200

    # archived 부모는 새 지정 불가
    client.delete(f"/api/v1/goals/{a['id']}")
    r = client.post("/api/v1/goals", json={"title": "new", "parent_id": a["id"]})
    assert r.status_code == 422 and "archived" in r.json()["error"]["message"]

    # archived 부모를 가진 기존 행의 다른 필드 PATCH는 parent_id 재전송해도 200
    r = client.patch(f"/api/v1/goals/{b['id']}", json={"title": "B2", "parent_id": a["id"]})
    assert r.status_code == 200 and r.json()["title"] == "B2"

    # 다른 archived 목표를 새 부모로 지정 → 422
    d = _mk(client, "D")
    client.delete(f"/api/v1/goals/{d['id']}")
    r = client.patch(f"/api/v1/goals/{b['id']}", json={"parent_id": d["id"]})
    assert r.status_code == 422 and "archived" in r.json()["error"]["message"]

    # parent_id 해제(null)는 허용
    r = client.patch(f"/api/v1/goals/{b['id']}", json={"parent_id": None})
    assert r.status_code == 200 and r.json()["parent_id"] is None


def test_store_goal_defense(tmp_path: Path) -> None:
    """store 계층 방어 — 존재하지 않는 parent/자기참조는 None 반환."""
    _, store = _env(tmp_path)
    a = store.create_goal("A")
    assert a is not None
    assert store.create_goal("B", parent_id="ghost") is None
    assert store.update_goal(a["id"], parent_id=a["id"]) is None
    assert store.get_goal(a["id"]) is not None
    b = store.create_goal("B")
    assert b is not None
    assert store.update_goal(a["id"], parent_id=b["id"]) is not None


# ---------------- 프로젝트/런 연동 ----------------


def test_project_goal_id_linkage(tmp_path: Path) -> None:
    client, store = _env(tmp_path)
    g = _mk(client, "목표")

    # 프로젝트 생성 시 goal_id
    r = client.post("/api/v1/projects", json={"name": "Proj", "goal_id": g["id"]})
    assert r.status_code == 201, r.text
    proj = r.json()
    assert proj["goal_id"] == g["id"]

    # 목록/상세 응답에도 goal_id
    items = client.get("/api/v1/projects").json()["items"]
    assert items[0]["goal_id"] == g["id"]

    # 미존재 goal_id → 422
    r = client.post("/api/v1/projects", json={"name": "P2", "goal_id": "ghost"})
    assert r.status_code == 422

    # PATCH로 goal_id 변경/해제
    g2 = _mk(client, "목표2")
    r = client.patch(f"/api/v1/projects/{proj['id']}", json={"goal_id": g2["id"]})
    assert r.status_code == 200 and r.json()["goal_id"] == g2["id"]
    r = client.patch(f"/api/v1/projects/{proj['id']}", json={"goal_id": None})
    assert r.status_code == 200 and r.json()["goal_id"] is None


def test_run_goal_id_resolution(tmp_path: Path) -> None:
    """runs.goal_id 해석 순서: 명시 → input.goal_id → 프로젝트 goal_id → NULL."""
    client, store = _env(tmp_path)
    g = _mk(client, "목표")

    # 1) 명시 goal_id (store 직접 호출)
    r1 = store.create_run("dev_review", "1", {"task": "t"}, trace_id="a", goal_id=g["id"])
    assert r1.goal_id == g["id"]
    # input에 goal 컨텍스트 {id,title} 주입
    assert r1.input["goal"] == {"id": g["id"], "title": "목표"}

    # 2) 프로젝트 goal_id 폴백 — 프로젝트 생성 후 run.project 태그
    client.post("/api/v1/projects", json={"name": "Linked", "goal_id": g["id"]})
    r2 = store.create_run("dev_review", "1", {"task": "t", "project": "Linked"}, trace_id="b")
    assert r2.goal_id == g["id"]
    assert r2.input["goal"]["id"] == g["id"]

    # 3) input_data.goal_id 폴백 — 저장 input에서는 goal_id 키가 제거되고
    #    "goal" 컨텍스트만 남는다(코얼레싱 canonical 형태 일치용).
    r3 = store.create_run("dev_review", "1", {"task": "t", "goal_id": g["id"]}, trace_id="c")
    assert r3.goal_id == g["id"]
    assert "goal_id" not in r3.input
    assert r3.input["goal"]["id"] == g["id"]

    # 4) 해석 실패 → NULL/빈 문자열, goal 컨텍스트 없음
    r4 = store.create_run("dev_review", "1", {"task": "t"}, trace_id="d")
    assert r4.goal_id == ""
    assert "goal" not in r4.input


def test_run_goal_id_via_api(tmp_path: Path) -> None:
    client, store = _env(tmp_path)
    g = _mk(client, "API 목표")

    # POST /runs 명시 goal_id → 202 + runs.goal_id
    r = client.post(
        "/api/v1/runs",
        json={"workflow": "dev_review", "task": "goal run", "goal_id": g["id"]},
    )
    assert r.status_code == 202, r.text
    run = store.get_run(r.json()["id"])
    assert run is not None and run.goal_id == g["id"]

    # run 목록/상세 응답에 goal_id
    items = client.get("/api/v1/runs").json()
    assert any(i["id"] == run.id and i["goal_id"] == g["id"] for i in items)
    detail = client.get(f"/api/v1/runs/{run.id}").json()
    assert detail["goal_id"] == g["id"]
    assert detail["input"]["goal"]["title"] == "API 목표"

    # 미존재 goal_id → 422
    r = client.post(
        "/api/v1/runs",
        json={"workflow": "dev_review", "task": "x", "goal_id": "ghost"},
    )
    assert r.status_code == 422

    # archived goal은 신규 run 태깅 불가
    client.delete(f"/api/v1/goals/{g['id']}")
    r = client.post(
        "/api/v1/runs",
        json={"workflow": "dev_review", "task": "x", "goal_id": g["id"]},
    )
    assert r.status_code == 422 and "archived" in r.json()["error"]["message"]


def test_run_patch_goal_id(tmp_path: Path) -> None:
    client, store = _env(tmp_path)
    g = _mk(client, "패치 목표")
    run = store.create_run("dev_review", "1", {"task": "t"}, trace_id="p")

    # PATCH goal_id 설정 (lead 게이트 — 기본 클라이언트는 관리자 폴백)
    r = client.patch(f"/api/v1/runs/{run.id}", json={"goal_id": g["id"]})
    assert r.status_code == 200, r.text
    assert store.get_run(run.id).goal_id == g["id"]  # type: ignore[union-attr]

    # 미존재 → 422, 해제(null) → NULL
    r = client.patch(f"/api/v1/runs/{run.id}", json={"goal_id": "ghost"})
    assert r.status_code == 422
    r = client.patch(f"/api/v1/runs/{run.id}", json={"goal_id": None})
    assert r.status_code == 200
    assert store.get_run(run.id).goal_id == ""  # type: ignore[union-attr]


def test_run_coalescing_includes_goal_id(tmp_path: Path) -> None:
    """동일 input·다른 goal_id는 coalesce되지 않고 별도 run."""
    client, store = _env(tmp_path)
    g1 = _mk(client, "G1")
    g2 = _mk(client, "G2")

    r1 = client.post(
        "/api/v1/runs",
        json={"workflow": "dev_review", "task": "same", "goal_id": g1["id"]},
    )
    assert r1.status_code == 202
    id1 = r1.json()["id"]

    r2 = client.post(
        "/api/v1/runs",
        json={"workflow": "dev_review", "task": "same", "goal_id": g2["id"]},
    )
    assert r2.status_code == 202 and r2.json()["id"] != id1

    # 같은 goal_id + 같은 input → coalesce
    r3 = client.post(
        "/api/v1/runs",
        json={"workflow": "dev_review", "task": "same", "goal_id": g1["id"]},
    )
    assert r3.status_code == 202
    assert r3.json()["id"] == id1 and r3.json().get("action") == "coalesced"
    assert len(store.list_runs()) == 2


# ---------------- H1: project 폴백 goal의 scope 일관성 ----------------


def test_project_goal_coalescing_and_deferred_promote(tmp_path: Path) -> None:
    """H1 회귀 — 프로젝트 goal 폴백이 scope_key/payload/run에 모두 반영돼
    coalescing이 동작하고 deferred wakeup이 promote된다.

    이전 버그: admit_run이 scope_key를 goal_id=""로 계산하고 create_run이
    project goal로 해석 → scope 불일치로 중복 run + deferred 영구 고착."""
    _, store = _env(tmp_path)
    g = store.create_goal("연동 목표")
    assert g is not None
    store.create_project("Linked", goal_id=g["id"])

    inp = {"task": "t", "project": "Linked"}
    r1 = wakeups.admit_run(
        store, workflow_key="dev_review", input_data=dict(inp), source="api", version="1"
    )
    assert r1["action"] == "created"
    run1 = r1["run"]
    assert run1.goal_id == g["id"]

    # 동일 요청 → coalesce (버그 시 두 번째 run이 생성됐음)
    r2 = wakeups.admit_run(
        store, workflow_key="dev_review", input_data=dict(inp), source="api", version="1"
    )
    assert r2["action"] == "coalesced"
    assert r2["run_id"] == run1.id
    assert len(store.list_runs()) == 1

    # force_new → deferred, run1 종결 시 promote — goal 태그 유지
    r3 = wakeups.admit_run(
        store,
        workflow_key="dev_review",
        input_data=dict(inp),
        source="api",
        version="1",
        force_new=True,
    )
    assert r3["action"] == "deferred"
    store.update_run(run1.id, status=RunStatus.COMPLETED)
    created = wakeups.on_run_finished(store, run1.id)
    assert len(created) == 1
    new_run = created[0][0]
    assert new_run.goal_id == g["id"]
    w = store.get_wakeup(str(r3["wakeup_id"]))
    assert w is not None and w["status"] != "deferred"


def test_run_project_goal_fallback_api(tmp_path: Path) -> None:
    """API 경로 — project 태그의 goal_id가 run에 폴백 해석된다."""
    client, store = _env(tmp_path)
    g = _mk(client, "API 폴백 목표")
    r = client.post("/api/v1/projects", json={"name": "P-Goal", "goal_id": g["id"]})
    assert r.status_code == 201, r.text

    r = client.post(
        "/api/v1/runs",
        json={"workflow": "dev_review", "task": "t", "project": "P-Goal"},
    )
    assert r.status_code == 202, r.text
    run = store.get_run(r.json()["id"])
    assert run is not None and run.goal_id == g["id"]
    assert run.input["goal"]["id"] == g["id"]


# ---------------- M1: archived goal 차단 ----------------


def test_project_rejects_archived_goal(tmp_path: Path) -> None:
    """프로젝트 create/patch는 archived goal_id를 422로 거부한다."""
    client, _ = _env(tmp_path)
    g = _mk(client, "곧 보관")
    client.delete(f"/api/v1/goals/{g['id']}")

    r = client.post("/api/v1/projects", json={"name": "P1", "goal_id": g["id"]})
    assert r.status_code == 422 and "archived" in r.json()["error"]["message"]

    # patch 경로 — 활성 goal로 생성 후 goal을 archive하고 재지정 시도
    g2 = _mk(client, "활성 목표")
    p = client.post("/api/v1/projects", json={"name": "P2", "goal_id": g2["id"]})
    assert p.status_code == 201
    client.delete(f"/api/v1/goals/{g2['id']}")
    r = client.patch(f"/api/v1/projects/{p.json()['id']}", json={"goal_id": g2["id"]})
    assert r.status_code == 422 and "archived" in r.json()["error"]["message"]


def test_run_fallback_skips_archived_goal(tmp_path: Path) -> None:
    """create_run의 project 폴백은 archived/missing goal을 태깅하지 않는다."""
    _, store = _env(tmp_path)
    g = store.create_goal("G")
    assert g is not None
    store.archive_goal(g["id"])
    store.create_project("ArchProj", goal_id=g["id"])

    run = store.create_run("dev_review", "1", {"task": "t", "project": "ArchProj"}, trace_id="a")
    assert run.goal_id == ""
    assert "goal" not in run.input

    # 호출자 주입 input["goal"]도 항상 제거된다(예약 키)
    run2 = store.create_run(
        "dev_review", "1", {"task": "t", "goal": {"id": "x", "title": "spoof"}}, trace_id="b"
    )
    assert "goal" not in run2.input


def test_admit_run_rejects_invalid_explicit_goal(tmp_path: Path) -> None:
    """M2 — admit_run은 모든 경로의 명시 goal_id를 검증한다."""
    _, store = _env(tmp_path)

    # 미존재 goal (input_data 경유 — 스케줄/플레이북 JSON 시뮬레이션)
    res = wakeups.admit_run(
        store,
        workflow_key="dev_review",
        input_data={"task": "t", "goal_id": "ghost"},
        source="schedule",
    )
    assert res["action"] == "invalid_goal"
    assert store.list_runs() == []

    # archived goal
    g = store.create_goal("G")
    assert g is not None
    store.archive_goal(g["id"])
    res = wakeups.admit_run(
        store,
        workflow_key="dev_review",
        input_data={"task": "t", "goal_id": g["id"]},
        source="api",
        goal_id=g["id"],
    )
    assert res["action"] == "invalid_goal"

    # project 폴백의 archived goal은 strip — run은 생성되되 goal 없음
    store.create_project("GA", goal_id=g["id"])
    res = wakeups.admit_run(
        store,
        workflow_key="dev_review",
        input_data={"task": "t2", "project": "GA"},
        source="api",
    )
    assert res["action"] == "created"
    assert res["run"].goal_id == ""


def test_deferred_promote_fails_on_archived_goal(tmp_path: Path) -> None:
    """defer 이후 goal이 archived되면 promote하지 않고 wakeup을 failed로."""
    _, store = _env(tmp_path)
    g = store.create_goal("G")
    assert g is not None

    r1 = wakeups.admit_run(
        store,
        workflow_key="dev_review",
        input_data={"task": "t", "goal_id": g["id"]},
        source="api",
    )
    d = wakeups.admit_run(
        store,
        workflow_key="dev_review",
        input_data={"task": "t", "goal_id": g["id"]},
        source="api",
        force_new=True,
    )
    assert r1["action"] == "created" and d["action"] == "deferred"

    store.archive_goal(g["id"])
    store.update_run(r1["run_id"], status=RunStatus.COMPLETED)
    created = wakeups.on_run_finished(store, r1["run_id"])
    assert created == []
    w = store.get_wakeup(str(d["wakeup_id"]))
    assert w is not None and w["status"] == "failed"


# ---------------- M4: PATCH 검증-후-변경 순서 ----------------


def test_patch_run_validates_before_mutate(tmp_path: Path) -> None:
    """goal_id 422여도 project 변경이 부분 적용되지 않는다."""
    client, store = _env(tmp_path)
    run = store.create_run("dev_review", "1", {"task": "t"}, trace_id="p")

    r = client.patch(f"/api/v1/runs/{run.id}", json={"project": "NewProj", "goal_id": "ghost"})
    assert r.status_code == 422
    after = store.get_run(run.id)
    assert after is not None and after.project == ""


# ---------------- H2: deferred scope_key 마이그레이션 ----------------


def test_deferred_wakeup_scope_migration(tmp_path: Path) -> None:
    """구형식 scope_key의 deferred wakeup이 Store 재오픈 시 재계산된다."""
    import json as _json

    path = str(tmp_path / "mig.db")
    s1 = Store(path)
    g = s1.create_goal("MG")
    assert g is not None
    s1.create_project("MigProj", goal_id=g["id"])
    # 구형식(잘못된) scope_key + goal_id 없는 payload의 deferred 행 삽입
    wid = s1.create_wakeup(
        scope_key="dev_review:legacydeadbeef",
        source="api",
        payload=_json.dumps(
            {
                "input": {"task": "t", "project": "MigProj"},
                "version": "1",
                "created_by": "test",
                "job": {"task": "t"},
                "agent_id": "",
                "goal_id": "",
            }
        ),
        status="deferred",
    )
    # 파싱 불가 payload의 deferred 행 — 재오픈 시 failed 마감돼야 한다
    bad = s1.create_wakeup(
        scope_key="dev_review:badbadbadbad",
        source="api",
        payload="not-json{{{",
        status="deferred",
    )
    s1.close()

    s2 = Store(path)
    w = s2.get_wakeup(wid)
    assert w is not None
    expected = wakeups.scope_key_for("dev_review", {"task": "t", "project": "MigProj"}, "", g["id"])
    assert w["scope_key"] == expected
    payload = _json.loads(str(w["payload"]))
    assert payload["goal_id"] == g["id"]
    # 재계산된 scope로 deferred 조회에 잡힌다 — promote 경로와 일치
    assert any(x["id"] == wid for x in s2.list_deferred_wakeups(expected))

    wb = s2.get_wakeup(bad)
    assert wb is not None and wb["status"] == "failed"
    s2.close()


# ---------------- 인증 게이트 ----------------


def test_goals_auth_gating(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, store = _client(tmp_path, monkeypatch)
    member = TestClient(client.app)
    lead = TestClient(client.app)
    _login(store, member, "mem@b.com", "member")
    _login(store, lead, "lead@b.com", "lead")

    # GET은 member+ — 민감 정보 없음
    assert member.get("/api/v1/goals").status_code == 200

    # member의 쓰기는 403
    assert member.post("/api/v1/goals", json={"title": "x"}).status_code == 403

    # lead는 생성/수정/삭제 가능
    r = lead.post("/api/v1/goals", json={"title": "L"})
    assert r.status_code == 201
    gid = r.json()["id"]
    assert lead.patch(f"/api/v1/goals/{gid}", json={"status": "done"}).status_code == 200
    assert lead.delete(f"/api/v1/goals/{gid}").status_code == 200
