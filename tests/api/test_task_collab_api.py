"""task comments/artifacts + /management/goals|projects alias API 테스트.

spec 3차 B/F — 인증 컨텍스트 기반 author 결정, URI 보안 규칙,
workspace_path 권한 게이트, goal/project 소프트 취소를 검증한다.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jobmate.api.app import create_app
from jobmate.config import Settings
from jobmate.security import SESSION_COOKIE, hash_password, hash_token, new_session_token
from jobmate.store.db import Store
from tests.integration.helpers import default_outputs, scripted_llm

API_KEY = "collab-test-key"


def _client():
    db = str(Path(tempfile.mkdtemp()) / "t.db")
    settings = Settings(db_path=db, budget_max_tokens=100_000)
    store = Store(db)
    app = create_app(
        settings=settings,
        store=store,
        llm=scripted_llm(default_outputs()),
        adapters={},
    )
    return TestClient(app), store


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("JOBMATE_API_KEY", raising=False)
    yield
    import os

    os.environ.pop("JOBMATE_API_KEY", None)


def _mk_task(client: TestClient, headers: dict[str, str] | None = None, **kw) -> dict:
    r = client.post("/api/v1/management/tasks", json={"title": "t", **kw}, headers=headers or {})
    assert r.status_code == 201, r.text
    return r.json()


def _member_session(client: TestClient, store: Store) -> None:
    u = store.create_user("m@b.com", hash_password("Str0ng#Passw0rd!"), "m@b.com")
    assert u is not None
    store.set_user_role(u["id"], "member")
    token = new_session_token()
    store.create_session(u["id"], hash_token(token))
    client.cookies.set(SESSION_COOKIE, token)


# ---- comments ----


def test_comment_crud_local_mode() -> None:
    """인증 비활성 로컬 모드 — human/local author로 생성·조회."""
    client, _ = _client()
    task = _mk_task(client)
    r = client.post(
        f"/api/v1/management/tasks/{task['id']}/comments",
        json={"body": "진행 상황 공유", "content_type": "markdown"},
    )
    assert r.status_code == 201, r.text
    c = r.json()
    assert c["author_type"] == "human" and c["author_id"] == "local"
    assert c["content_type"] == "markdown"

    items = client.get(f"/api/v1/management/tasks/{task['id']}/comments").json()["items"]
    assert [i["id"] for i in items] == [c["id"]]

    # WMB-n identifier로도 조회 가능
    items2 = client.get("/api/v1/management/tasks/WMB-1/comments").json()["items"]
    assert len(items2) == 1

    # 없는 task → 404
    assert client.get("/api/v1/management/tasks/ghost/comments").status_code == 404


def test_comment_member_cannot_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """member 세션은 댓글 쓰기 403 — 읽기는 허용."""
    monkeypatch.setenv("JOBMATE_API_KEY", API_KEY)
    client, store = _client()
    task = _mk_task(client, headers={"X-API-Key": API_KEY})
    _member_session(client, store)
    # member 세션 쿠키가 API 키 헤더보다 우선 — 명시적 member로 판정
    r = client.post(
        f"/api/v1/management/tasks/{task['id']}/comments",
        json={"body": "hi"},
        headers={"X-API-Key": API_KEY},
    )
    assert r.status_code == 403
    assert client.get(f"/api/v1/management/tasks/{task['id']}/comments").status_code == 200


def test_comment_machine_key_author_system(monkeypatch: pytest.MonkeyPatch) -> None:
    """머신 API 키 작성 → author_type=system (agent identity는 해석 불가)."""
    monkeypatch.setenv("JOBMATE_API_KEY", API_KEY)
    client, _ = _client()
    task = _mk_task(client, headers={"X-API-Key": API_KEY})
    r = client.post(
        f"/api/v1/management/tasks/{task['id']}/comments",
        json={"body": "agent note"},
        headers={"X-API-Key": API_KEY},
    )
    assert r.status_code == 201, r.text
    assert r.json()["author_type"] == "system"
    assert r.json()["author_id"] == "api-key"


def test_comment_body_validation() -> None:
    client, _ = _client()
    task = _mk_task(client)
    url = f"/api/v1/management/tasks/{task['id']}/comments"
    assert client.post(url, json={"body": ""}).status_code == 422
    assert client.post(url, json={"body": "x" * 4001}).status_code == 422
    assert client.post(url, json={"body": "ok", "content_type": "html"}).status_code == 422


# ---- artifacts ----


def test_artifact_crud_and_uri_rules() -> None:
    client, store = _client()
    task = _mk_task(client)
    url = f"/api/v1/management/tasks/{task['id']}/artifacts"

    ok_https = client.post(
        url,
        json={
            "kind": "test_report",
            "name": "report.html",
            "uri": "https://ci.example.com/reports/1.html",
            "media_type": "text/html",
            "size_bytes": 2048,
            "checksum_sha256": "b" * 64,
        },
    )
    assert ok_https.status_code == 201, ok_https.text
    assert ok_https.json()["kind"] == "test_report"

    ok_rel = client.post(url, json={"kind": "diff", "name": "p.diff", "uri": "out/p.diff"})
    assert ok_rel.status_code == 201, ok_rel.text

    # unsafe URI 전부 422 — 서버는 fetch하지 않고 메타데이터만 저장
    for bad in (
        "http://example.com/x",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "data:text/html,x",
        "//evil.example.com/s",
        "/abs/path",
        "../escape",
        "a\\b",
    ):
        r = client.post(url, json={"kind": "other", "name": "x", "uri": bad})
        assert r.status_code == 422, (bad, r.text)

    items = client.get(url).json()["items"]
    assert len(items) == 2

    # run_id — task에 링크된 run만 허용
    run = store.create_run("dev_review", "1", {}, trace_id="")
    store.link_task_run(task["id"], run.id)
    r = client.post(
        url,
        json={
            "kind": "review",
            "name": "r.md",
            "uri": "docs/r.md",
            "run_id": run.id,
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["run_id"] == run.id

    other = store.create_run("dev_review", "1", {}, trace_id="")
    r = client.post(
        url,
        json={"kind": "review", "name": "r2.md", "uri": "docs/r2.md", "run_id": other.id},
    )
    assert r.status_code == 422


def test_task_runs_endpoint() -> None:
    client, store = _client()
    task = _mk_task(client)
    run = store.create_run("dev_review", "1", {}, trace_id="")
    store.link_task_run(task["id"], run.id)
    r = client.get(f"/api/v1/management/tasks/{task['id']}/runs")
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1 and items[0]["run_id"] == run.id
    assert items[0]["workflow"] == "dev_review"


# ---- /management/goals alias ----


def test_management_goals_alias_crud() -> None:
    client, _ = _client()
    r = client.post(
        "/api/v1/management/goals",
        json={
            "title": "Alias Goal",
            "status": "active",
            "success_metrics": {"kpi": "coverage>80"},
            "target_at": "2026-12-31",
        },
    )
    assert r.status_code == 201, r.text
    g = r.json()
    assert g["status"] == "active" and g["success_metrics"] == {"kpi": "coverage>80"}

    items = client.get("/api/v1/management/goals").json()["items"]
    assert [i["id"] for i in items] == [g["id"]]
    assert client.get("/api/v1/management/goals?status=cancelled").json()["items"] == []

    r = client.patch(f"/api/v1/management/goals/{g['id']}", json={"status": "paused"})
    assert r.status_code == 200 and r.json()["status"] == "paused"

    r = client.delete(f"/api/v1/management/goals/{g['id']}")
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    # 취소된 goal은 status 필터 목록에서 확인 — 행은 보존된다
    cancelled = client.get("/api/v1/management/goals?status=cancelled").json()["items"]
    assert [i["id"] for i in cancelled] == [g["id"]]


def test_management_goals_alias_validation() -> None:
    client, _ = _client()
    # 구 어휘 status는 422
    r = client.post("/api/v1/management/goals", json={"title": "x", "status": "done"})
    assert r.status_code == 422
    r = client.post("/api/v1/management/goals", json={"title": "x", "status": "archived"})
    assert r.status_code == 422


# ---- /management/projects alias ----


def test_management_projects_alias_crud() -> None:
    client, _ = _client()
    goal = client.post("/api/v1/goals", json={"title": "G"}).json()
    r = client.post(
        "/api/v1/management/projects",
        json={
            "name": "AliasProj",
            "status": "planned",
            "owner_agent_id": None,
            "repository_url": "https://github.com/o/r",
            "repository_ref": "main",
            "workspace_path": "/srv/jobmate/ws",
            "goal_id": goal["id"],
        },
    )
    assert r.status_code == 201, r.text
    p = r.json()
    assert p["status"] == "planned" and p["goal_id"] == goal["id"]
    # 로컬 모드(explicit lead) → workspace_path 노출
    assert p["workspace_path"] == "/srv/jobmate/ws"

    items = client.get("/api/v1/management/projects").json()["items"]
    assert [i["id"] for i in items] == [p["id"]]

    r = client.patch(f"/api/v1/management/projects/{p['id']}", json={"status": "active"})
    assert r.status_code == 200 and r.json()["status"] == "active"

    r = client.delete(f"/api/v1/management/projects/{p['id']}")
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    # 소프트 취소 — 행은 목록에 남는다
    items = client.get("/api/v1/management/projects").json()["items"]
    assert items[0]["status"] == "cancelled"


def test_management_projects_workspace_path_gated(monkeypatch: pytest.MonkeyPatch) -> None:
    """머신 API 키에는 workspace_path(로컬 절대경로)를 응답에서 제외한다."""
    monkeypatch.setenv("JOBMATE_API_KEY", API_KEY)
    client, _ = _client()
    headers = {"X-API-Key": API_KEY}
    r = client.post(
        "/api/v1/management/projects",
        json={"name": "P", "workspace_path": "/secret/ws"},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    assert "workspace_path" not in r.json()
    items = client.get("/api/v1/management/projects", headers=headers).json()["items"]
    assert "workspace_path" not in items[0]
    # 저장은 됨 — canonical /projects도 머신 컨텍스트에서는 동일하게 숨긴다
    got = client.get("/api/v1/projects", headers=headers)
    assert "workspace_path" not in got.json()["items"][0]


def test_project_update_audit_redacts_workspace_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M2 회귀 — PATCH audit detail에 workspace_path 원문이 남으면
    /api/v1/audit(머신 키 읽기 가능)이 _project_out 필터를 우회한다."""
    monkeypatch.setenv("JOBMATE_API_KEY", API_KEY)
    client, store = _client()
    headers = {"X-API-Key": API_KEY}
    p = client.post(
        "/api/v1/management/projects",
        json={"name": "P", "workspace_path": "/secret/ws"},
        headers=headers,
    ).json()
    r = client.patch(
        f"/api/v1/management/projects/{p['id']}",
        json={"workspace_path": "/etc/jobmate/private", "status": "active"},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    audits = [
        a
        for a in store.list_audit(500)
        if a.get("action") == "project_update" and a.get("target") == p["id"]
    ]
    assert audits, "project_update audit event missing"
    detail = audits[0].get("detail") or {}
    assert "/etc/jobmate/private" not in str(detail)
    assert detail.get("workspace_path") == "<redacted>"
    # 다른 필드는 그대로 기록된다
    assert detail.get("status") == "active"


def test_task_project_goal_refs() -> None:
    """task 생성/patch의 project_id·goal_id — 존재 검증 + effective goal."""
    client, store = _client()
    goal = client.post("/api/v1/goals", json={"title": "G"}).json()
    proj = client.post("/api/v1/projects", json={"name": "P", "goal_id": goal["id"]}).json()

    # 유효 참조 → 201 + 필드 저장
    r = client.post(
        "/api/v1/management/tasks",
        json={"title": "t1", "project_id": proj["id"], "goal_id": goal["id"]},
    )
    assert r.status_code == 201, r.text
    task = r.json()
    assert task["project_id"] == proj["id"] and task["goal_id"] == goal["id"]

    # 미존재 참조 → 422
    assert (
        client.post(
            "/api/v1/management/tasks",
            json={"title": "t2", "project_id": "ghost"},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/management/tasks", json={"title": "t3", "goal_id": "ghost"}
        ).status_code
        == 422
    )

    # effective goal — task.goal_id → project.goal_id → none
    g2 = client.post("/api/v1/goals", json={"title": "G2"}).json()
    t_proj_only = _mk_task(client, project_id=proj["id"])
    assert store.task_effective_goal_id(t_proj_only) == goal["id"]
    # task.goal_id가 project.goal_id보다 우선
    t_both = _mk_task(client, project_id=proj["id"], goal_id=g2["id"])
    assert store.task_effective_goal_id(t_both) == g2["id"]
    # 참조 없으면 ''
    t_none = _mk_task(client)
    assert store.task_effective_goal_id(t_none) == ""

    # PATCH로 goal_id 해제(None 명시)
    r = client.patch(f"/api/v1/management/tasks/{t_both['id']}", json={"goal_id": None})
    assert r.status_code == 200
    assert store.task_effective_goal_id(store.get_task(t_both["id"])) == goal["id"]  # type: ignore[arg-type]
