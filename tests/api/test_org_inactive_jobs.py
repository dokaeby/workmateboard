"""FR-031 — suspended/deleted 조직의 자동화 경로 신규 run 생성 차단.

스케줄/루틴 차단은 test_org_lifecycle.py가 커버한다. 여기서는 나머지
자동화 진입점을 검증한다:
- 메시지 intake(_process_message_job) — terminal failed 마감, 재시도 없음
- deferred wakeup promote(on_run_finished) — finishing run의 wakeup 마감은
  유지하되 suspended 조직 run 생성은 차단, 복귀 시 재promote
- trigger 발사(_evaluate_triggers) — suspended 조직 trigger 스킵
- "첫 활성 조직" 헬퍼(_default_org_id/_first_active_org) — suspended 제외
"""

import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from jobmate import wakeups
from jobmate.api import jobs
from jobmate.api import triggers as triggers_mod
from jobmate.api.app import create_app
from jobmate.config import Settings
from jobmate.models import RunStatus
from jobmate.store import orgctx
from jobmate.store.db import Store


def _store() -> Store:
    return Store(str(Path(tempfile.mkdtemp()) / "t.db"))


def _settings(db_path: str = "") -> Settings:
    return Settings(db_path=db_path or str(Path(tempfile.mkdtemp()) / "t.db"))


def _mkorg(store: Store, name: str, slug: str, created_at: str = "") -> str:
    oid = store.create_organization(name, slug, kind="team", created_by="")
    assert oid
    if created_at:
        # list_organizations는 created_at 순 — 정지 조직을 항상 먼저
        # 오게 해 "첫 조직" 선택 테스트를 결정적으로 만든다.
        store._conn.execute("UPDATE organizations SET created_at=? WHERE id=?", (created_at, oid))
        store._conn.commit()
    return oid


def _org_runs(store: Store, oid: str) -> list:
    with orgctx.org_scope(oid):
        return store.list_runs(limit=50)


# ---------- "첫 활성 조직" 헬퍼 ----------


def _default_oid(store: Store) -> str:
    """Store 초기화가 자동 생성하는 기본 조직(defaultorg01, slug='default').

    created_at 순서가 아니라 slug로 찾는다 — 테스트가 created_at을
    과거로 바꾼 조직이 첫 행을 차지할 수 있기 때문."""
    for o in store.list_organizations():
        if o["slug"] == "default":
            return str(o["id"])
    raise AssertionError("default org not found")


def test_default_org_id_skips_suspended():
    """jobs._default_org_id — suspended 조직은 건너뛰고 active 조직을 고른다."""
    store = _store()
    dflt = _default_oid(store)
    oid_a = _mkorg(store, "Suspended", "susp-a", created_at="2020-01-01T00:00:00+00:00")
    oid_b = _mkorg(store, "Active", "act-b")
    # 자동 생성 default 조직까지 정지 — 첫 조직(oid_a)도 suspended면 oid_b
    store.set_organization_status(dflt, "suspended")
    store.set_organization_status(oid_a, "suspended")
    assert jobs._default_org_id(store) == oid_b
    store.set_organization_status(oid_b, "suspended")
    # 활성 조직 0 — ''가 아니라 '__no_org__' 센티널(무필터 ctx 방지)
    assert jobs._default_org_id(store) == "__no_org__"
    store.close()


def test_first_active_org_skips_suspended(monkeypatch):
    """appctx._first_active_org — env API 키 행위자의 조직 컨텍스트는
    첫 '활성' 조직. suspended 조직이 더 오래돼도 건너뛴다."""
    monkeypatch.setenv("JOBMATE_API_KEY", "test-api-key")
    db = str(Path(tempfile.mkdtemp()) / "t.db")
    settings = Settings(db_path=db, budget_max_tokens=100_000)
    store = Store(db)
    app = create_app(settings=settings, store=store, llm=None, adapters={})
    client = TestClient(app)
    oid_a = _mkorg(store, "Suspended", "susp-a", created_at="2020-01-01T00:00:00+00:00")
    oid_b = _mkorg(store, "Active", "act-b")
    # 자동 생성 default 조직 + oid_a를 정지 — 첫 '활성' 조직은 oid_b여야 한다
    store.set_organization_status(_default_oid(store), "suspended")
    store.set_organization_status(oid_a, "suspended")
    r = client.post(
        "/api/v1/triggers",
        json={"match": {}, "action": {"type": "report", "report_kind": "daily"}},
        headers={"X-API-Key": "test-api-key"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["org_id"] == oid_b
    store.close()


# ---------- 메시지 intake ----------


def test_message_intake_skipped_for_suspended_org():
    """_process_message_job — suspended 조직 메시지는 run 없이
    processing_status='failed'(error='org_inactive')로 terminal 마감.
    pending_messages가 재수집하지 않아 무한 재시도가 없다."""
    store = _store()
    settings = _settings()
    oid = _mkorg(store, "Suspended", "susp-msg")
    store.set_organization_status(oid, "suspended")
    assert store.save_message_extraction(
        "testprov", "m1", "th1", {}, "pending", source_text="hi", org_id=oid
    )
    rows = store.pending_messages()
    assert len(rows) == 1
    jobs._process_message_job(store, settings, None, {}, dict(rows[0]))
    row = store._conn.execute(
        "SELECT processing_status, error_message FROM message_extractions"
        " WHERE source_provider='testprov' AND source_id='m1'"
    ).fetchone()
    assert row["processing_status"] == "failed"
    assert row["error_message"] == "org_inactive"
    # 재수집되지 않고 run도 생성되지 않는다
    assert store.pending_messages() == []
    assert _org_runs(store, oid) == []
    assert store.list_runs(limit=50) == []
    store.close()


# ---------- deferred wakeup promote ----------


def _deferred_wakeup(store: Store, oid: str):
    """동일 scope active run + deferred wakeup 1건을 만든다.

    반환: (run1, deferred_wakeup_id, 첫 wakeup_id)."""
    with orgctx.org_scope(oid):
        r1 = wakeups.admit_run(
            store,
            workflow_key="dev_review",
            input_data={"task": "t"},
            source="schedule",
            created_by="test",
            version="1",
        )
        assert r1["action"] == "created"
        r2 = wakeups.admit_run(
            store,
            workflow_key="dev_review",
            input_data={"task": "t"},
            source="schedule",
            created_by="test",
            version="1",
            force_new=True,
        )
        assert r2["action"] == "deferred"
    return r1["run"], r2["wakeup_id"], r1["wakeup_id"]


def test_deferred_promote_skipped_for_suspended_org():
    """on_run_finished — finishing run의 wakeup 마감은 수행하되,
    suspended 조직의 deferred wakeup은 promote하지 않는다.
    조직 복귀 후 다음 on_run_finished에서 promote된다."""
    store = _store()
    oid = _mkorg(store, "Team", "team-promote")
    run1, wid, w1id = _deferred_wakeup(store, oid)
    store.update_run(run1.id, status=RunStatus.COMPLETED)

    store.set_organization_status(oid, "suspended")
    with orgctx.org_scope(oid):
        created = wakeups.on_run_finished(store, run1.id)
    # promote 없음 — deferred wakeup은 'deferred'로 남는다
    assert created == []
    assert store.get_wakeup(wid)["status"] == "deferred"
    # finishing run 자신의 wakeup은 정상 마감(completed)
    assert store.get_wakeup(w1id)["status"] == "completed"
    assert len(_org_runs(store, oid)) == 1

    # 조직 복귀 — 같은 finishing run의 재호출로 promote 재시도 성공
    store.set_organization_status(oid, "active")
    with orgctx.org_scope(oid):
        created = wakeups.on_run_finished(store, run1.id)
    assert len(created) == 1
    assert len(_org_runs(store, oid)) == 2
    assert store.get_wakeup(wid).get("run_id")
    store.close()


# ---------- triggers ----------


def test_trigger_fire_skipped_for_suspended_org():
    """_evaluate_triggers — suspended 조직 trigger는 발사하지 않고,
    active 조직 trigger는 정상 발사된다(회귀 방지)."""
    store = _store()
    settings = _settings()
    oid_s = _mkorg(store, "Suspended", "susp-trig")
    oid_a = _mkorg(store, "Active", "act-trig")
    store.set_organization_status(oid_s, "suspended")

    action = {"type": "run_workflow", "workflow": "dev_review"}
    trig_s = store.create_trigger({}, action, org_id=oid_s)
    trig_a = store.create_trigger({}, action, org_id=oid_a)

    # 1차 평가 — 시드(전이 없음)만 기록
    store.upsert_agent_session(
        "claude", "s-susp", "working", event_at="2026-01-01T00:00:00+00:00", org_id=oid_s
    )
    store.upsert_agent_session(
        "claude", "s-act", "working", event_at="2026-01-01T00:00:00+00:00", org_id=oid_a
    )
    assert triggers_mod._evaluate_triggers(store, settings, None, {}, defer=[]) == 0

    # 상태 전이 — suspended 조직 trigger는 org_inactive로 스킵.
    # list_agent_sessions가 org_id를 반환하므로 세션↔trigger 조직 매칭도
    # 동작한다: suspended 조직 세션 전이는 active trigger를 발사하지
    # 않는다(교차 조직 자동화 오발사 차단).
    store.upsert_agent_session(
        "claude", "s-susp", "ended", event_at="2026-01-01T01:00:00+00:00", org_id=oid_s
    )
    store.upsert_agent_session(
        "claude", "s-act", "ended", event_at="2026-01-01T01:00:00+00:00", org_id=oid_a
    )
    defer: list = []
    fired = triggers_mod._evaluate_triggers(store, settings, None, {}, defer=defer)
    # active 세션 전이 × active trigger = 1회 발사.
    # suspended 세션 전이는 active trigger에도 org 불일치로 미발사,
    # suspended trigger는 org_inactive로 스킵.
    assert fired == 1
    assert len(defer) == 1
    assert _org_runs(store, oid_s) == []
    assert len(_org_runs(store, oid_a)) == 1
    # suspended trigger는 발사 기록이 없다
    assert not store.get_trigger(trig_s["id"]).get("last_fired_at")
    assert store.get_trigger(trig_a["id"]).get("last_fired_at")
    store.close()


# ---------- create_run 중앙 차단 (FR-031) ----------


def test_create_run_blocked_for_inactive_org():
    """store.create_run — suspended/deleted 조직의 run 생성을 모든
    진입점에서 차단하는 choke point. API 키·evals 등 precheck를 우회하는
    경로도 여기서 OrgInactiveError로 막힌다."""
    from jobmate.store.orgs import OrgInactiveError

    store = _store()
    oid = _mkorg(store, "Team", "team-cr")
    store.set_organization_status(oid, "suspended")
    with orgctx.org_scope(oid):
        try:
            store.create_run("dev_review", "1", {"task": "t"}, "tr")
            raise AssertionError("expected OrgInactiveError")
        except OrgInactiveError:
            pass
    store.set_organization_status(oid, "deleted")
    with orgctx.org_scope(oid):
        try:
            store.create_run("dev_review", "1", {"task": "t"}, "tr")
            raise AssertionError("expected OrgInactiveError")
        except OrgInactiveError:
            pass
    # active 조직은 정상 생성
    oid2 = _mkorg(store, "Active", "act-cr")
    with orgctx.org_scope(oid2):
        run = store.create_run("dev_review", "1", {"task": "t"}, "tr")
    assert run.id
    store.close()
