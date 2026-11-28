"""routines API 테스트 — /api/v1/management/routines CRUD + 인증 + tick 발사.

paperclip spec 3차 C/F/G. 인증 없는 로컬 개발 모드는 admin 상당,
member 세션은 읽기만 가능하고 쓰기는 403.
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from jobmate.api.app import create_app
from jobmate.config import Settings
from jobmate.security import hash_token
from jobmate.store.db import Store


def _client():
    db = str(Path(tempfile.mkdtemp()) / "t.db")
    settings = Settings(db_path=db, budget_max_tokens=100_000)
    store = Store(db)
    app = create_app(settings=settings, store=store, llm=None, adapters={})
    return TestClient(app), store


def _member_client(client: TestClient, store: Store, email: str = "m@b.com"):
    """member 세션 쿠키를 단 별도 TestClient."""
    user = store.create_user(email, password_hash="x" * 32, email_verified=True)
    assert user is not None
    _orgs = store.list_organizations()
    assert _orgs
    store.add_org_member(_orgs[0]["id"], user["id"], "member")
    token = f"sess-{email}"
    store.create_session(user["id"], hash_token(token))
    c = TestClient(client.app)
    c.cookies.set("jobmate_session", token)
    return c


def _create(client: TestClient, **over) -> dict:
    body = {
        "name": "standup",
        "schedule_kind": "interval",
        "schedule": {"minutes": 60},
        "timezone": "Asia/Seoul",
        "task_template": {"title": "daily standup"},
    }
    body.update(over)
    r = client.post("/api/v1/management/routines", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def test_crud_lifecycle():
    client, store = _client()
    r = _create(client)
    rid = r["id"]
    assert r["status"] == "active" and r["enabled"] is True
    assert r["next_run_at"]
    assert r["schedule"] == {"minutes": 60}
    assert r["task_template"]["title"] == "daily standup"

    # GET 단건/목록
    assert client.get(f"/api/v1/management/routines/{rid}").status_code == 200
    lst = client.get("/api/v1/management/routines").json()["items"]
    assert [x["id"] for x in lst] == [rid]
    assert client.get("/api/v1/management/routines", params={"enabled": True}).json()["items"]

    # PATCH — schedule 변경 시 next_run_at 재계산
    old_next = r["next_run_at"]
    p = client.patch(
        f"/api/v1/management/routines/{rid}",
        json={"schedule": {"minutes": 5}, "max_concurrent": 3},
    )
    assert p.status_code == 200, p.text
    assert p.json()["schedule"] == {"minutes": 5}
    assert p.json()["max_concurrent"] == 3
    assert p.json()["next_run_at"] != old_next

    # enable/disable
    d = client.post(f"/api/v1/management/routines/{rid}/disable")
    assert d.status_code == 200 and d.json()["enabled"] is False
    e = client.post(f"/api/v1/management/routines/{rid}/enable")
    assert e.status_code == 200 and e.json()["enabled"] is True

    # runs 이력 (빈 목록)
    runs = client.get(f"/api/v1/management/routines/{rid}/runs")
    assert runs.status_code == 200 and runs.json()["items"] == []

    # DELETE — routine_runs 이력은 보존
    assert client.delete(f"/api/v1/management/routines/{rid}").status_code == 200
    assert client.get(f"/api/v1/management/routines/{rid}").status_code == 404


def test_create_validation_errors():
    client, _ = _client()
    # 잘못된 timezone
    r = client.post(
        "/api/v1/management/routines",
        json={
            "name": "x",
            "schedule_kind": "interval",
            "schedule": {"minutes": 1},
            "timezone": "Not/AZone",
        },
    )
    assert r.status_code == 422
    # 잘못된 schedule_kind
    r = client.post(
        "/api/v1/management/routines",
        json={
            "name": "x",
            "schedule_kind": "bogus",
            "schedule": {"minutes": 1},
        },
    )
    assert r.status_code == 422
    # cron_weekly weekday 범위
    r = client.post(
        "/api/v1/management/routines",
        json={
            "name": "x",
            "schedule_kind": "cron_weekly",
            "schedule": {"weekday": 9, "time": "09:00"},
        },
    )
    assert r.status_code == 422
    # max_concurrent < 1 — pydantic ge=1
    r = client.post(
        "/api/v1/management/routines",
        json={
            "name": "x",
            "schedule_kind": "interval",
            "schedule": {"minutes": 1},
            "max_concurrent": 0,
        },
    )
    assert r.status_code == 422
    # name 없음
    r = client.post(
        "/api/v1/management/routines",
        json={"schedule_kind": "interval", "schedule": {"minutes": 1}},
    )
    assert r.status_code == 422


def test_auth_gating():
    """member는 읽기만 — 쓰기/enable/disable/delete는 403."""
    client, store = _client()
    r = _create(client)
    rid = r["id"]
    member = _member_client(client, store)
    # spec 003 US3 — 로컬 모드(ctx='')로 만든 routine은 org_id=''라
    # 조직 멤버 세션에서는 보이지 않는다. 기본 조직으로 스탬프한다.
    _oid = store.list_organizations()[0]["id"]
    with store.tx() as c:
        c.execute("UPDATE routines SET org_id=? WHERE id=?", (_oid, rid))

    assert member.get("/api/v1/management/routines").status_code == 200
    assert member.get(f"/api/v1/management/routines/{rid}").status_code == 200
    assert member.get(f"/api/v1/management/routines/{rid}/runs").status_code == 200

    assert (
        member.post(
            "/api/v1/management/routines",
            json={"name": "x", "schedule_kind": "interval", "schedule": {"minutes": 1}},
        ).status_code
        == 403
    )
    assert member.patch(f"/api/v1/management/routines/{rid}", json={"name": "y"}).status_code == 403
    assert member.delete(f"/api/v1/management/routines/{rid}").status_code == 403
    assert member.post(f"/api/v1/management/routines/{rid}/enable").status_code == 403
    assert member.post(f"/api/v1/management/routines/{rid}/disable").status_code == 403


def test_patch_404_and_null_enabled_ignored():
    client, store = _client()
    r = _create(client)
    rid = r["id"]
    assert client.patch("/api/v1/management/routines/nope", json={"name": "x"}).status_code == 404
    # enabled 명시 null은 무시 — routine이 disable되지 않는다.
    p = client.patch(f"/api/v1/management/routines/{rid}", json={"enabled": None})
    assert p.status_code == 200 and p.json()["enabled"] is True


def test_cron_tick_fires_due_routine_once():
    """/cron/tick이 due routine을 발사해 task를 만들고, 재tick은 중복 생성 없음."""
    client, store = _client()
    # assignee agent — workflow kind (dev_review).
    agent = store.create_agent(
        "dev", "claude", kind="workflow", adapter_config={"workflow": "dev_review"}
    )
    assert agent is not None
    r = _create(
        client,
        assignee_agent_id=agent["id"],
        task_template={"title": "tick task"},
        schedule={"minutes": 30},
    )
    rid = r["id"]
    # next_run_at을 과거로 — tick이 due로 잡는다.
    past = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    store.update_routine(rid, next_run_at=past)

    tick = client.post("/api/v1/cron/tick")
    assert tick.status_code == 200, tick.text

    runs = store.list_routine_runs(rid)
    assert len(runs) == 1
    assert runs[0]["status"] == "created"
    task = store.get_task(runs[0]["task_id"])
    assert task is not None and task["title"] == "tick task"
    assert task["assignee_agent_id"] == agent["id"]

    # 두 번째 tick — next_run_at이 미래라 due 아님 → 중복 없음.
    tick2 = client.post("/api/v1/cron/tick")
    assert tick2.status_code == 200
    assert len(store.list_routine_runs(rid)) == 1
    # task도 1개뿐.
    tasks = [t for t in store.list_tasks() if t["title"] == "tick task"]
    assert len(tasks) == 1


def test_automation_loop_and_tick_no_double_fire():
    """loop와 tick이 같은 routine을 동시에 처리해도 claim CAS가 1회만 허용."""
    from jobmate.api.jobs import _process_due_routines

    client, store = _client()
    settings = Settings(db_path=":memory:", budget_max_tokens=1000)
    r = _create(client, schedule={"minutes": 30})
    rid = r["id"]
    past = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    store.update_routine(rid, next_run_at=past)

    # 두 경로를 순차 호출 — 두 번째는 due가 아니므로 아무 일도 안 한다.
    jobs: list = []
    _process_due_routines(store, settings, None, {}, defer=jobs)
    _process_due_routines(store, settings, None, {}, defer=[])
    runs = store.list_routine_runs(rid)
    assert len(runs) == 1
    assert runs[0]["status"] == "created"
