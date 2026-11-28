"""purge_deleted_organizations — org_id 없는 자식 테이블 정리 검증.

stage_runs/approvals/task_runs/run_comments/routine_runs/trigger_fires/
wakeup_requests/agent_session_events는 org_id 컬럼이 없어 부모
(runs/tasks/routines/triggers/agent_sessions) 경유로 삭제돼야 한다.
PG에서는 stage_runs/approvals가 runs.id를 REFERENCES하므로 자식 선삭제가
없으면 purge가 FK 위반으로 실패한다.
"""

import json
import tempfile
from datetime import timedelta
from pathlib import Path

from jobmate.security import hash_password
from jobmate.store.db import Store
from jobmate.store.util import _iso, _now


def _store() -> Store:
    db = str(Path(tempfile.mkdtemp()) / "t.db")
    return Store(db)


def _mkuser(store: Store, email: str) -> str:
    user = store.create_user(email, hash_password("Str0ng#Passw0rd!"), "U", "", True)
    return str(user["id"] if isinstance(user, dict) else user)


def _count(store: Store, sql: str, params: tuple = ()) -> int:
    row = store._conn.execute(sql, params).fetchone()
    return int(row["n"])


def _seed_org_data(store: Store, oid: str, uid: str, tag: str) -> dict[str, str]:
    """org 스코프 run + 모든 비-org_id 자식 테이블 행을 만든다."""
    run = store.create_run("dev_review", "v1", {"task": "x"}, f"trace-{tag}", org_id=oid)
    store.start_stage(run.id, "analyze", 1, {})
    store.add_approval(run.id, "analyze", "approve")
    store.add_run_comment(run.id, uid, "u@b.com", "c")
    task = store.create_task(f"task-{tag}", org_id=oid)
    assert task is not None
    store.link_task_run(str(task["id"]), run.id)
    routine = store.create_routine(name=f"r-{tag}", org_id=oid)
    store.create_routine_run(
        routine_id=str(routine["id"]),
        scheduled_for=_iso(_now()),
        idempotency_key=f"rk-{tag}",
    )
    trigger = store.create_trigger(match={}, action={}, org_id=oid)
    store.claim_trigger_fire(str(trigger["id"]), "tool", f"sess-{tag}", "running", "fp")
    # run_id 직결 wakeup + payload org_id만으로 연결되는 deferred wakeup
    store.create_wakeup(
        scope_key=f"scope-{tag}",
        source="test",
        payload=json.dumps({"org_id": oid, "input": {}}),
        run_id=run.id,
    )
    store.create_wakeup(
        scope_key=f"scope2-{tag}",
        source="test",
        payload=json.dumps({"org_id": oid, "input": {}}),
        run_id="",
    )
    store.upsert_agent_session(f"tool-{tag}", f"sess-{tag}", "running", org_id=oid)
    store._conn.execute(
        "INSERT INTO agent_session_events(id,tool,session_key,status,created_at) VALUES(?,?,?,?,?)",
        (f"ev-{tag}", f"tool-{tag}", f"sess-{tag}", "running", _iso(_now())),
    )
    store._conn.commit()
    return {"run_id": run.id, "task_id": str(task["id"]), "routine_id": str(routine["id"])}


def _child_counts(store: Store) -> dict[str, int]:
    return {
        "runs": _count(store, "SELECT COUNT(*) AS n FROM runs"),
        "stage_runs": _count(store, "SELECT COUNT(*) AS n FROM stage_runs"),
        "approvals": _count(store, "SELECT COUNT(*) AS n FROM approvals"),
        "run_comments": _count(store, "SELECT COUNT(*) AS n FROM run_comments"),
        "task_runs": _count(store, "SELECT COUNT(*) AS n FROM task_runs"),
        "routine_runs": _count(store, "SELECT COUNT(*) AS n FROM routine_runs"),
        "trigger_fires": _count(store, "SELECT COUNT(*) AS n FROM trigger_fires"),
        "wakeup_requests": _count(store, "SELECT COUNT(*) AS n FROM wakeup_requests"),
        "agent_session_events": _count(store, "SELECT COUNT(*) AS n FROM agent_session_events"),
        "agent_sessions": _count(store, "SELECT COUNT(*) AS n FROM agent_sessions"),
    }


def test_purge_removes_org_scoped_children() -> None:
    store = _store()
    try:
        uid = _mkuser(store, "purge@b.com")
        oid = store.create_organization("Org", "purge-org", created_by=uid)
        assert oid
        store.add_org_member(oid, uid, "org_owner")
        ids = _seed_org_data(store, oid, uid, "a")

        # 대조군 — 다른 조직의 동일 자식 행은 purge 후에도 남아야 한다
        oid2 = store.create_organization("Org2", "keep-org", created_by=uid)
        assert oid2
        store.add_org_member(oid2, uid, "org_owner")
        keep = _seed_org_data(store, oid2, uid, "b")

        assert store.set_organization_status(oid, "deleted")
        old = _iso(_now() - timedelta(days=31))
        store._conn.execute("UPDATE organizations SET updated_at=? WHERE id=?", (old, oid))
        store._conn.commit()

        counts = store.purge_deleted_organizations()
        assert counts["purged_organizations"] == 1
        assert store.get_organization(oid) is None

        c = _child_counts(store)
        # 대상 조직의 모든 자식 행 삭제
        assert c["runs"] == 1  # keep 조직 run만 잔존
        assert c["stage_runs"] == 1
        assert c["approvals"] == 1
        assert c["run_comments"] == 1
        assert c["task_runs"] == 1
        assert c["routine_runs"] == 1
        assert c["trigger_fires"] == 1
        assert c["wakeup_requests"] == 2
        # upsert가 전이 이벤트를 자동 기록하므로 조직당 이벤트 2건 —
        # purge 후에는 keep 조직 것만 남는다
        assert c["agent_session_events"] == 2
        assert c["agent_sessions"] == 1
        assert (
            _count(
                store,
                "SELECT COUNT(*) AS n FROM agent_session_events WHERE session_key='sess-a'",
            )
            == 0
        )
        # 잔존 행이 keep 조직 것인지 확인
        assert store.get_run(keep["run_id"]) is not None
        row = store._conn.execute(
            "SELECT org_id FROM runs WHERE id=?", (keep["run_id"],)
        ).fetchone()
        assert row["org_id"] == oid2
        assert ids["run_id"] != keep["run_id"]
    finally:
        store.close()
