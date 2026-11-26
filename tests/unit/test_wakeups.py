"""Wakeup coalescing(admit_run/on_run_finished) 단위 테스트."""

from __future__ import annotations

import sqlite3
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from jobmate.models import RunStatus
from jobmate.store.db import Store
from jobmate.wakeups import admit_run, on_run_finished, scope_key_for


def _store() -> Store:
    return Store(str(Path(tempfile.mkdtemp()) / "t.db"))


def _admit(s: Store, task: str = "t", **kw):
    return admit_run(
        s,
        workflow_key=kw.pop("workflow_key", "dev_review"),
        input_data={"task": task},
        source=kw.pop("source", "api"),
        version="1.0",
        **kw,
    )


def test_admit_creates_run_and_wakeup():
    s = _store()
    res = _admit(s, "hello")
    assert res["action"] == "created"
    run = res["run"]
    assert run.status == RunStatus.QUEUED
    w = s.get_wakeup(res["wakeup_id"])
    assert w["status"] == "queued" and w["run_id"] == run.id and w["source"] == "api"


def test_same_scope_coalesces():
    s = _store()
    r1 = _admit(s, "same task")
    r2 = _admit(s, "same task")
    assert r1["action"] == "created" and r2["action"] == "coalesced"
    assert r2["run_id"] == r1["run_id"]
    # run은 1개뿐
    assert len(s.list_runs()) == 1
    # coalesced wakeup 기록
    w2 = s.get_wakeup(r2["wakeup_id"])
    assert w2["status"] == "coalesced" and w2["coalesced_count"] == 1


def test_different_input_does_not_coalesce():
    s = _store()
    r1 = _admit(s, "task A")
    r2 = _admit(s, "task B")
    assert r1["action"] == "created" and r2["action"] == "created"
    assert r1["run_id"] != r2["run_id"]
    assert len(s.list_runs()) == 2


def test_different_workflow_does_not_coalesce():
    s = _store()
    r1 = _admit(s, "same", workflow_key="dev_review")
    r2 = _admit(s, "same", workflow_key="code_review")
    assert r1["action"] == "created" and r2["action"] == "created"


def test_force_new_defers_and_promotes_on_finish():
    s = _store()
    r1 = _admit(s, "dup")
    r2 = _admit(s, "dup", force_new=True)
    assert r2["action"] == "deferred" and r2["run_id"] == ""
    assert len(s.list_runs()) == 1

    scheduled = []
    created = on_run_finished(s, r1["run_id"], schedule=lambda run, jp: scheduled.append((run, jp)))
    # r1은 아직 queued — 비종결이라 아무 일도 안 일어남
    assert created == []

    # r1을 completed로 마감 → deferred promote
    s.update_run(r1["run_id"], status=RunStatus.COMPLETED)
    created = on_run_finished(s, r1["run_id"], schedule=lambda run, jp: scheduled.append((run, jp)))
    assert len(created) == 1
    new_run, job_payload = created[0]
    assert new_run.id != r1["run_id"]
    assert job_payload == {"task": "dup"}
    assert len(scheduled) == 1
    # deferred wakeup이 새 run을 가리킴
    w = s.get_wakeup(r2["wakeup_id"])
    assert w["run_id"] == new_run.id and w["status"] == "queued"

    # 새 run도 같은 input — 다시 coalesce 대상
    r3 = _admit(s, "dup")
    assert r3["action"] == "coalesced" and r3["run_id"] == new_run.id


def test_on_run_finished_marks_wakeups():
    s = _store()
    res = _admit(s, "x")
    s.update_run(res["run_id"], status=RunStatus.COMPLETED)
    on_run_finished(s, res["run_id"])
    w = s.get_wakeup(res["wakeup_id"])
    assert w["status"] == "completed" and w["finished_at"]

    # 실패도 failed로
    res2 = _admit(s, "y")
    s.update_run(res2["run_id"], status=RunStatus.FAILED)
    on_run_finished(s, res2["run_id"])
    assert s.get_wakeup(res2["wakeup_id"])["status"] == "failed"


def test_coalesced_status_survives_run_finish():
    """run 종결 후에도 coalesced wakeup 행은 status='coalesced'를 유지 —
    archive/목록의 병합 뱃지(coalesced_counts_for_runs)가 죽지 않게.
    finished_at은 기록돼 retention sweep 삭제 대상에는 포함된다."""
    s = _store()
    r1 = _admit(s, "same task")
    r2 = _admit(s, "same task")
    assert r2["action"] == "coalesced"

    s.update_run(r1["run_id"], status=RunStatus.COMPLETED)
    on_run_finished(s, r1["run_id"])

    primary = s.get_wakeup(r1["wakeup_id"])
    merged = s.get_wakeup(r2["wakeup_id"])
    assert primary["status"] == "completed"
    assert merged["status"] == "coalesced"
    assert merged["finished_at"]  # retention 대상에는 포함
    assert s.coalesced_counts_for_runs([r1["run_id"]]) == {r1["run_id"]: 1}


def test_coalesced_status_survives_failed_run():
    s = _store()
    r1 = _admit(s, "same task")
    r2 = _admit(s, "same task")
    s.update_run(r1["run_id"], status=RunStatus.FAILED)
    on_run_finished(s, r1["run_id"])
    assert s.get_wakeup(r1["wakeup_id"])["status"] == "failed"
    assert s.get_wakeup(r2["wakeup_id"])["status"] == "coalesced"
    assert s.coalesced_counts_for_runs([r1["run_id"]]) == {r1["run_id"]: 1}


def test_idempotency_key_replay():
    s = _store()
    r1 = admit_run(
        s,
        workflow_key="dev_review",
        input_data={"task": "t"},
        source="api",
        version="1",
        idempotency_key="key-1",
    )
    assert r1["action"] == "created"
    # 활성 run 동안 같은 키 → coalesced(같은 run)
    r2 = admit_run(
        s,
        workflow_key="dev_review",
        input_data={"task": "t"},
        source="api",
        version="1",
        idempotency_key="key-1",
    )
    assert r2["action"] == "coalesced" and r2["run_id"] == r1["run_id"]
    # run 종료 후 같은 키 → replayed(기존 run 반환, 새 run 없음)
    s.update_run(r1["run_id"], status=RunStatus.COMPLETED)
    r3 = admit_run(
        s,
        workflow_key="dev_review",
        input_data={"task": "t"},
        source="api",
        version="1",
        idempotency_key="key-1",
    )
    assert r3["action"] == "replayed" and r3["run_id"] == r1["run_id"]
    assert len(s.list_runs()) == 1


def test_source_recording_and_list():
    s = _store()
    admit_run(
        s,
        workflow_key="dev_review",
        input_data={"task": "t"},
        source="trigger",
        version="1",
        created_by="trigger:trig1",
    )
    items = s.list_wakeups(source="trigger")
    assert len(items) == 1 and items[0]["source"] == "trigger"
    assert s.list_wakeups(source="schedule") == []


def test_scope_key_canonical():
    assert scope_key_for("w", {"a": 1, "b": 2}) == scope_key_for("w", {"b": 2, "a": 1})
    assert scope_key_for("w", {"a": 1}) != scope_key_for("w", {"a": 2})
    assert scope_key_for("w", {}) != scope_key_for("x", {})


def test_deferred_idempotent_retry_returns_deferred():
    """run_id 없는 deferred wakeup의 idempotent 재시도는 'deferred'를 반환 —
    'coalesced'+빈 run_id로 {"id":"","status":"unknown"}이 되는 것을 막는다."""
    s = _store()
    _admit(s, "dup")
    d1 = admit_run(
        s,
        workflow_key="dev_review",
        input_data={"task": "dup"},
        source="api",
        version="1",
        force_new=True,
        idempotency_key="k-defer",
    )
    assert d1["action"] == "deferred"
    r2 = admit_run(
        s,
        workflow_key="dev_review",
        input_data={"task": "dup"},
        source="api",
        version="1",
        idempotency_key="k-defer",
    )
    assert r2["action"] == "deferred"
    assert r2["run_id"] == "" and r2["wakeup_id"] == d1["wakeup_id"]


def test_promote_blocked_by_budget():
    """deferred promote 시점에 예산이 차단됐으면 run을 만들지 않고
    wakeup을 budget_exceeded로 마감한다."""
    from jobmate import budget_scope

    s = _store()
    r1 = _admit(s, "dup")
    d1 = _admit(s, "dup", force_new=True)
    s.update_run(r1["run_id"], status=RunStatus.COMPLETED)
    # hard_stop을 넘는 지출 기록 ($15 @ default out price)
    sr = s.start_stage(r1["run_id"], "llm", 1, {})
    s.finish_stage(sr.id, status="succeeded", output={}, tokens_out=1_000_000)
    budget_scope._spend_cache.clear()
    budget_scope.upsert_policy(s, "workflow", "dev_review", amount_usd=1.0)

    created = on_run_finished(s, r1["run_id"])
    assert created == []
    assert s.get_wakeup(d1["wakeup_id"])["status"] == "budget_exceeded"
    assert len(s.list_runs()) == 1  # promote된 run 없음


def test_single_flight_only_one_deferred_promoted():
    s = _store()
    r1 = _admit(s, "dup")
    d1 = _admit(s, "dup", force_new=True)
    d2 = _admit(s, "dup", force_new=True)
    assert d1["action"] == "deferred" and d2["action"] == "deferred"

    s.update_run(r1["run_id"], status=RunStatus.COMPLETED)
    created = on_run_finished(s, r1["run_id"])
    assert len(created) == 1
    # 두 번째 deferred는 아직 대기 — promote된 run이 활성이라 스킵
    new_run_id = created[0][0].id
    assert s.get_wakeup(d2["wakeup_id"])["status"] == "deferred"

    # promote된 run 종료 → 두 번째 deferred promote
    s.update_run(new_run_id, status=RunStatus.COMPLETED)
    created2 = on_run_finished(s, new_run_id)
    assert len(created2) == 1
    assert s.get_wakeup(d2["wakeup_id"])["status"] == "queued"


def test_oversized_payload_roundtrips_on_promote():
    """8KB 초과 입력도 payload가 잘리지 않고 저장돼 promote 시 원래 input으로 run 생성."""
    s = _store()
    big = "x" * 12000
    r1 = admit_run(
        s, workflow_key="dev_review", input_data={"task": big}, source="api", version="1"
    )
    d = admit_run(
        s,
        workflow_key="dev_review",
        input_data={"task": big},
        source="api",
        version="1",
        force_new=True,
    )
    assert d["action"] == "deferred"

    s.update_run(r1["run_id"], status=RunStatus.COMPLETED)
    created = on_run_finished(s, r1["run_id"])
    assert len(created) == 1
    new_run = created[0][0]
    # 완료된 run의 input을 복제한 게 아니라 deferred 요청의 원본 input이다
    assert new_run.input["task"] == big
    assert new_run.id != r1["run_id"]


@pytest.mark.parametrize("bad_payload", ["not-json{", '{"job":{},"version":"1"}', "[1,2]"])
def test_corrupt_payload_fails_wakeup_no_run(bad_payload):
    """promote 시 payload 파싱 실패/input 부재 → wakeup failed, run 미생성.

    run.input을 복제해 추측 실행하는 경로는 없다."""
    s = _store()
    r1 = _admit(s, "dup")
    d = _admit(s, "dup", force_new=True)
    with s.tx() as c:
        c.execute(
            "UPDATE wakeup_requests SET payload=? WHERE id=?",
            (bad_payload, d["wakeup_id"]),
        )
    s.update_run(r1["run_id"], status=RunStatus.COMPLETED)
    created = on_run_finished(s, r1["run_id"])
    assert created == []
    w = s.get_wakeup(d["wakeup_id"])
    assert w["status"] == "failed" and w["finished_at"]
    assert len(s.list_runs()) == 1  # promote된 run 없음


def test_stale_claimed_wakeup_recovered():
    """claim 후 run 생성 전 크래시로 남은 queued/run_id='' wakeup은
    STALE_CLAIM_AGE 경과 후 다음 promote에서 회수된다."""
    s = _store()
    r1 = _admit(s, "dup")
    d = _admit(s, "dup", force_new=True)
    # 크래시 상태 시뮬레이션: deferred → queued claim, run_id 미설정, claimed_at 과거
    old = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    with s.tx() as c:
        c.execute(
            "UPDATE wakeup_requests SET status='queued', claimed_at=?, run_id='' WHERE id=?",
            (old, d["wakeup_id"]),
        )
    s.update_run(r1["run_id"], status=RunStatus.COMPLETED)
    created = on_run_finished(s, r1["run_id"])
    assert len(created) == 1
    w = s.get_wakeup(d["wakeup_id"])
    assert w["run_id"] == created[0][0].id


def test_fresh_claim_not_stolen():
    """claimed_at이 최근인 queued/run_id='' 행은 진행 중 claim으로 보고 회수하지 않는다."""
    s = _store()
    r1 = _admit(s, "dup")
    d = _admit(s, "dup", force_new=True)
    now = datetime.now(UTC).isoformat()
    with s.tx() as c:
        c.execute(
            "UPDATE wakeup_requests SET status='queued', claimed_at=?, run_id='' WHERE id=?",
            (now, d["wakeup_id"]),
        )
    s.update_run(r1["run_id"], status=RunStatus.COMPLETED)
    created = on_run_finished(s, r1["run_id"])
    assert created == []
    assert s.get_wakeup(d["wakeup_id"])["status"] == "queued"


def test_idempotency_scope_mismatch_conflict():
    """동일 idempotency_key에 다른 입력 → 재생이 아니라 conflict."""
    s = _store()
    r1 = admit_run(
        s,
        workflow_key="dev_review",
        input_data={"task": "A"},
        source="api",
        version="1",
        idempotency_key="k-mismatch",
    )
    assert r1["action"] == "created"
    s.update_run(r1["run_id"], status=RunStatus.COMPLETED)
    r2 = admit_run(
        s,
        workflow_key="dev_review",
        input_data={"task": "B-different"},
        source="api",
        version="1",
        idempotency_key="k-mismatch",
    )
    assert r2["action"] == "conflict"
    assert r2["run_id"] == ""
    assert len(s.list_runs()) == 1


def test_idempotency_unique_index_rejects_duplicate():
    """부분 UNIQUE 인덱스 — 비어있지 않은 idempotency_key는 1행만 허용."""
    s = _store()
    s.create_wakeup(scope_key="s", source="api", idempotency_key="dup-key")
    with pytest.raises(sqlite3.IntegrityError):
        s.create_wakeup(scope_key="s", source="api", idempotency_key="dup-key")
    # 빈 키는 인덱스 대상이 아니라 중복 허용
    s.create_wakeup(scope_key="s", source="api", idempotency_key="")
    s.create_wakeup(scope_key="s", source="api", idempotency_key="")


def test_idempotency_dedupe_migration(tmp_path):
    """기존 DB의 중복 idempotency_key는 Store 오픈 시 최신 행만 키를 유지."""
    from jobmate.store.db import SCHEMA

    path = str(tmp_path / "d.db")
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.executemany(
        "INSERT INTO wakeup_requests(id,source,scope_key,payload,status,coalesced_count,"
        "idempotency_key,run_id,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        [
            (
                "w-old",
                "api",
                "s",
                "{}",
                "completed",
                0,
                "dup-key",
                "",
                "2024-01-01T00:00:00+00:00",
            ),
            (
                "w-new",
                "api",
                "s",
                "{}",
                "completed",
                0,
                "dup-key",
                "",
                "2024-01-02T00:00:00+00:00",
            ),
        ],
    )
    conn.commit()
    conn.close()

    s = Store(path)
    rows = s.list_wakeups()
    keyed = [w for w in rows if w["idempotency_key"] == "dup-key"]
    assert [w["id"] for w in keyed] == ["w-new"]  # 최신 행만 키 유지
    assert len(rows) == 2  # 감사 행 자체는 보존(키만 비움)
    # dedupe 후 UNIQUE 인덱스가 동작한다
    with pytest.raises(sqlite3.IntegrityError):
        s.create_wakeup(scope_key="s", source="api", idempotency_key="dup-key")
