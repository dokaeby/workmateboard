"""spec 005 FR-014 — 계정 삭제 처리 매트릭스 추가 커버리지.

커버: 비종결 run의 created_by anonymize(행은 보존), email='' 계정의
user_id 매칭 정리, actor-ish 컬럼 전반 anonymize, 개인 워크스페이스
soft-delete(멤버 잔존 시 보존), export의 share_links.token 제외.
"""

from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from jobmate.models import RunStatus
from jobmate.security import hash_password
from jobmate.store.db import Store

_PW = "Str0ng#Passw0rd!"


def _store() -> Store:
    return Store(str(Path(tempfile.mkdtemp()) / "t.db"))


def _mkuser(store: Store, email: str = "u@b.com") -> dict:
    return store.create_user(email, hash_password(_PW), "U", "", True)


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def test_running_run_anonymized_but_not_deleted() -> None:
    """FR-014/US2-4 — 진행 중 run은 삭제하지 않고 created_by만 마스킹."""
    store = _store()
    user = _mkuser(store, "run-owner@b.com")
    uid, email = user["id"], user["email"]
    run = store.create_run("dev_review", "0.1.0", {"task": "t"}, trace_id="mx-1")
    store.update_run(run.id, status=RunStatus.RUNNING)
    with store.tx() as c:
        c.execute("UPDATE runs SET created_by=? WHERE id=?", (email, run.id))

    counts = store.delete_user_cascade(uid)

    row = store._conn.execute(
        "SELECT status, created_by FROM runs WHERE id=?", (run.id,)
    ).fetchone()
    assert row is not None  # run 자체는 보존
    assert row["status"] == "running"
    assert row["created_by"] == "deleted_user"
    assert counts.get("runs_created_by_anonymized") == 1
    store.close()


def test_empty_email_user_still_cleans_by_user_id() -> None:
    """email='' 계정 — run_comments/audit actor의 user_id 매칭 정리가
    email 가드 밖에서 동작해야 한다."""
    store = _store()
    user = _mkuser(store, "noemail@b.com")
    uid = user["id"]
    with store.tx() as c:
        c.execute("UPDATE users SET email='' WHERE id=?", (uid,))
        c.execute(
            "INSERT INTO run_comments(id,run_id,user_id,email,body,created_at)"
            " VALUES('rc-x','run-x',?,'','hello',?)",
            (uid, _iso_now()),
        )
        c.execute(
            "INSERT INTO audit_events(id,run_id,actor,action,object_ref,detail,"
            "created_at,org_id) VALUES('a-x','','user-act','act','ref','{}',?,'')",
            (_iso_now(),),
        )
        # actor=user_id가 아닌 email 매칭도 없는지 확인용 타인 행
        c.execute(
            "INSERT INTO audit_events(id,run_id,actor,action,object_ref,detail,"
            "created_at,org_id) VALUES('a-y','','other@b.com','act','ref','{}',?,'')",
            (_iso_now(),),
        )
    # actor=user_id로 기록된 케이스 + 타인 email 행
    with store.tx() as c:
        c.execute(
            "INSERT INTO audit_events(id,run_id,actor,action,object_ref,detail,"
            "created_at,org_id) VALUES('a-z','','act2','act','ref','{}',?,'')",
            (_iso_now(),),
        )
        c.execute("UPDATE audit_events SET actor=? WHERE id='a-z'", (uid,))

    counts = store.delete_user_cascade(uid)

    n = store._conn.execute("SELECT COUNT(*) AS n FROM run_comments").fetchone()["n"]
    assert n == 0
    assert counts.get("run_comments") == 1
    az = store._conn.execute("SELECT actor FROM audit_events WHERE id='a-z'").fetchone()
    assert az["actor"] == "deleted_user"
    ay = store._conn.execute("SELECT actor FROM audit_events WHERE id='a-y'").fetchone()
    assert ay["actor"] == "other@b.com"  # 타인 행 보존
    store.close()


def test_actor_columns_anonymized_across_tables() -> None:
    """created_by/decided_by/requested_by/author_id 컬럼이 'deleted_user'로."""
    store = _store()
    user = _mkuser(store, "actor@b.com")
    uid, email = user["id"], user["email"]
    now = _iso_now()
    run = store.create_run("dev_review", "0.1.0", {"task": "t"}, trace_id="mx-ap")
    with store.tx() as c:
        c.execute(
            "INSERT INTO agents(id,name,adapter_type,created_by,created_at,updated_at)"
            " VALUES('ag-1','a1','workflow',?,?,?)",
            (email, now, now),
        )
        c.execute(
            "INSERT INTO tasks(id,identifier,title,created_by,created_at,updated_at)"
            " VALUES('tk-1','T-1','t',?,?,?)",
            (email, now, now),
        )
        c.execute(
            "INSERT INTO projects(id,name,created_by,created_at,updated_at)"
            " VALUES('pj-1','p',?,?,?)",
            (email, now, now),
        )
        c.execute(
            "INSERT INTO approvals(id,run_id,stage_key,decision,decided_by,"
            "decided_at,created_at) VALUES('ap-1',?,'s','approved',?,?,?)",
            (run.id, email, now, now),
        )
        c.execute(
            "INSERT INTO approval_requests(id,action_type,requested_by,decided_by,"
            "created_at,updated_at) VALUES('ar-1','a',?,?,?,?)",
            (email, email, now, now),
        )
        c.execute(
            "INSERT INTO task_comments(id,task_id,author_type,author_id,body,"
            "created_at) VALUES('tc-1','tk-1','human',?,'hi',?)",
            (uid, now),
        )
        c.execute(
            "INSERT INTO organizations(id,name,slug,kind,status,created_by,"
            "created_at,updated_at) VALUES('org-team','Team','team-slug-x',"
            "'team','active',?,?,?)",
            (email, now, now),
        )
    store.delete_user_cascade(uid)

    checks = [
        ("agents", "created_by", "ag-1"),
        ("tasks", "created_by", "tk-1"),
        ("projects", "created_by", "pj-1"),
        ("approvals", "decided_by", "ap-1"),
        ("approval_requests", "requested_by", "ar-1"),
        ("approval_requests", "decided_by", "ar-1"),
        ("organizations", "created_by", "org-team"),
    ]
    for table, col, row_id in checks:
        row = store._conn.execute(
            f"SELECT {col} AS v FROM {table} WHERE id=?",  # noqa: S608
            (row_id,),
        ).fetchone()
        assert row is not None, f"{table} row missing"
        assert row["v"] == "deleted_user", f"{table}.{col}={row['v']!r}"
    tc = store._conn.execute("SELECT author_id FROM task_comments WHERE id='tc-1'").fetchone()
    assert tc["author_id"] == "deleted_user"
    store.close()


def test_personal_org_soft_deleted_not_removed() -> None:
    """본인 개인 워크스페이스는 status='deleted' soft-delete — 행은 남고
    purge_deleted_organizations가 이후 정리한다."""
    store = _store()
    user = _mkuser(store, "solo@b.com")
    uid = user["id"]
    oid = store.create_personal_org(dict(user))
    assert oid

    counts = store.delete_user_cascade(uid)

    row = store._conn.execute(
        "SELECT status, created_by FROM organizations WHERE id=?", (oid,)
    ).fetchone()
    assert row is not None  # hard delete 아님
    assert row["status"] == "deleted"
    assert counts.get("organizations") == 1
    # 멤버십은 제거됨
    n = store._conn.execute(
        "SELECT COUNT(*) AS n FROM organization_members WHERE org_id=?", (oid,)
    ).fetchone()["n"]
    assert n == 0
    store.close()


def test_unrelated_memberless_personal_org_untouched() -> None:
    """삭제 사용자가 만든/소속이 아닌 memberless 개인 조직은 보존."""
    store = _store()
    user = _mkuser(store, "del@b.com")
    other = _mkuser(store, "other@b.com")
    uid = user["id"]
    now = _iso_now()
    with store.tx() as c:
        c.execute(
            "INSERT INTO organizations(id,name,slug,kind,status,created_by,"
            "created_at,updated_at) VALUES('org-orphan','Orphan','orphan-slug-x',"
            "'personal','active',?,?,?)",
            (other["id"], now, now),
        )
    store.delete_user_cascade(uid)
    row = store._conn.execute("SELECT status FROM organizations WHERE id='org-orphan'").fetchone()
    assert row is not None and row["status"] == "active"
    store.close()


def test_personal_org_with_remaining_member_not_deleted() -> None:
    """본인이 만든 개인 조직에 다른 멤버가 남아 있으면 soft-delete 안 함."""
    store = _store()
    user = _mkuser(store, "owner@b.com")
    member = _mkuser(store, "member@b.com")
    uid = user["id"]
    oid = store.create_personal_org(dict(user))
    assert oid
    store.add_org_member(oid, member["id"], "member")

    store.delete_user_cascade(uid)

    row = store._conn.execute("SELECT status FROM organizations WHERE id=?", (oid,)).fetchone()
    assert row is not None and row["status"] == "active"
    store.close()


def test_export_excludes_share_link_token() -> None:
    """share_links.token은 bearer 자격증명 — export 메타에 포함하지 않는다."""
    store = _store()
    user = _mkuser(store, "exp@b.com")
    uid, email = user["id"], user["email"]
    store.create_share_link(
        "live-token-1",  # noqa: S106 — 테스트 픽스처 토큰
        "run",
        "r1",
        {"x": 1},
        email,
        (datetime.now(UTC) + timedelta(days=30)).isoformat(),
    )
    data = store.export_user_data(uid)
    assert len(data["share_links"]) == 1
    sl = data["share_links"][0]
    assert "token" not in sl
    assert sl["kind"] == "run"
    assert sl["ref_id"] == "r1"
    assert "live-token-1" not in json.dumps(data)
    store.close()
