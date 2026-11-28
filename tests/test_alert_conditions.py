"""spec 007 US4 — 신규 alert condition 필드 테스트.

- run.latency_ms / error.error_class: 이벤트 경로 필드
- error.rate_pct / session.stale_count / cost.delta_pct: 주기 평가 필드
  (evaluate_periodic_rules — emit_alert_event 경유 자동 호출 + force 직접 호출)
- API 생성/검증: 신규 필드 허용, 미지 필드 422
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from jobmate.services import alert_rules as svc
from jobmate.store import orgctx
from jobmate.store.db import Store


def _store() -> Store:
    return Store(str(Path(tempfile.mkdtemp()) / "t.db"))


@pytest.fixture(autouse=True)
def _clear_org_and_throttle():
    svc._PERIODIC_LAST.clear()
    with orgctx.org_scope(""):
        yield
    svc._PERIODIC_LAST.clear()


def _mk_rule(store, condition, channels=None, **kw):
    return store.create_alert_rule(
        name=kw.pop("name", "r1"),
        condition=condition,
        channels=channels if channels is not None else [{"channel": "inbox"}],
        **kw,
    )


def _mk_run(store, status="failed", workflow="dev_review"):
    from jobmate.models import RunStatus

    run = store.create_run(workflow, "1", {"task": "t"}, trace_id="a")
    if status != run.status.value:
        store.update_run(run.id, status=RunStatus(status))
        run = store.get_run(run.id)
    return run


# ---- 검증 ----


@pytest.mark.parametrize(
    "field",
    [
        "run.latency_ms",
        "error.error_class",
        "error.rate_pct",
        "session.stale_count",
        "cost.delta_pct",
    ],
)
def test_validate_condition_accepts_new_fields(field):
    ok = svc.validate_condition({"field": field, "op": "gte", "value": 1})
    assert ok["field"] == field


def test_validate_condition_rejects_unknown_field():
    with pytest.raises(ValueError):
        svc.validate_condition({"field": "run.bogus", "op": "eq", "value": "x"})
    with pytest.raises(ValueError):
        svc.validate_condition({"field": "error.rate", "op": "gt", "value": 1})


# ---- 이벤트 경로 필드 ----


def test_run_latency_ms_alias_and_threshold():
    """run.latency_ms는 run.duration_ms와 같은 값 — created_at을 과거로
    되돌려 비영(0) duration을 만든다."""
    s = _store()
    run = _mk_run(s, "completed")
    old = (datetime.now(UTC) - timedelta(seconds=90)).isoformat()
    s._conn.execute("UPDATE runs SET created_at=? WHERE id=?", (old, run.id))  # noqa: SLF001
    s._conn.commit()

    built = svc.run_event_fields(s, run.id)
    assert built is not None
    f, _org = built
    assert f["run.latency_ms"] == f["run.duration_ms"]
    assert f["run.latency_ms"] >= 60_000

    _mk_rule(s, {"field": "run.latency_ms", "op": "gte", "value": 60_000})
    _mk_rule(s, {"field": "run.latency_ms", "op": "lt", "value": 1_000}, name="low")
    assert svc.emit_run_status_event(s, run.id) == 1
    fires = s.list_alert_fires()
    assert len(fires) == 1


def test_run_error_class_field():
    """실패 run의 error_class가 이벤트 필드로 실린다 — update_run이
    error_taxonomy로 분류한 컬럼값을 읽는다."""
    s = _store()
    run = _mk_run(s, "failed")
    from jobmate.models import RunStatus

    s.update_run(run.id, status=RunStatus.FAILED, error="connection timeout to db")
    fields, _org = svc.run_event_fields(s, run.id)
    eclass = fields["error.error_class"]
    assert eclass, "error_class should be classified from error text"

    _mk_rule(s, {"field": "error.error_class", "op": "eq", "value": eclass})
    _mk_rule(
        s,
        {"field": "error.error_class", "op": "eq", "value": "definitely_not_this"},
        name="other",
    )
    assert svc.emit_run_status_event(s, run.id) == 1


def test_error_class_of_fallbacks():
    """_error_class_of: dict.get → getattr → 원시 컬럼 순 방어적 읽기."""
    s = _store()
    assert svc._error_class_of({"error_class": "quota"}, s, "runs", "x") == "quota"
    assert svc._error_class_of(SimpleNamespace(error_class="timeout"), s, "runs", "x") == (
        "timeout"
    )
    # obj 없음/빈 값 → 원시 컬럼 fallback (없는 행은 '')
    assert svc._error_class_of(None, s, "runs", "nope") == ""
    assert svc._error_class_of(object(), s, "runs", "nope") == ""


def test_stage_error_class_event():
    """stage_run 실패 이벤트에 stage_runs.error_class가 실린다."""
    s = _store()
    run = _mk_run(s, "running")
    sr = s.start_stage(run.id, "impl", 1, {})
    # finish_stage 대신 직접 마감 — finish_stage는 async emit을 쏴서
    # 테스트와 경합한다. 컬럼 기록 경로는 동일 SQL이다.
    s._conn.execute(  # noqa: SLF001
        "UPDATE stage_runs SET status='failed', error=?, error_class=?, finished_at=? WHERE id=?",
        ("boom", "timeout", datetime.now(UTC).isoformat(), sr.id),
    )
    s._conn.commit()

    built = svc.stage_event_fields(s, run.id, sr.stage_key, "failed")
    assert built is not None
    fields, org_id = built
    fields["error.error_class"] = svc._error_class_of(
        s.get_stage_run(sr.id), s, "stage_runs", sr.id
    )
    assert fields["error.error_class"] == "timeout"

    _mk_rule(
        s,
        {
            "field": "error.error_class",
            "op": "eq",
            "value": "timeout",
        },
    )
    n = svc.emit_alert_event(
        s,
        event_fingerprint=f"stage:{sr.id}:failed",
        fields=fields,
        org_id=org_id,
    )
    assert n == 1


# ---- 주기 평가 필드 ----


def test_error_rate_pct_periodic():
    """trailing 60분 실패율 — 4 runs 중 2 failed = 50%."""
    s = _store()
    for i, st in enumerate(["failed", "completed", "failed", "completed"]):
        r = s.create_run("dev_review", "1", {"task": f"t{i}"}, trace_id=f"r{i}")
        from jobmate.models import RunStatus

        s.update_run(r.id, status=RunStatus(st))

    _mk_rule(s, {"field": "error.rate_pct", "op": "gt", "value": 40})
    _mk_rule(s, {"field": "error.rate_pct", "op": "gt", "value": 90}, name="strict")
    assert svc.evaluate_periodic_rules(s, org_id="", force=True) == 1
    fires = s.list_alert_fires()
    assert len(fires) == 1
    assert fires[0]["event_fingerprint"].startswith("periodic:")
    # 같은 시간 버킷 재평가 → fingerprint dedup
    assert svc.evaluate_periodic_rules(s, org_id="", force=True) == 0


def test_error_rate_pct_empty_window():
    s = _store()
    _mk_rule(s, {"field": "error.rate_pct", "op": "gt", "value": 0})
    # run 없음 → 0.0 → gt 0 불매칭
    assert svc.evaluate_periodic_rules(s, org_id="", force=True) == 0


def test_periodic_rule_via_event_emit_path():
    """이벤트 컨텍스트에 없는 주기 필드는 이벤트 매칭을 건너뛰고,
    emit_alert_event 끝의 주기 평가가 실제 집계값으로 판정한다 —
    ne 조건이 None에 오발화하지 않음을 회귀 검증."""
    s = _store()
    for i in range(2):
        r = s.create_run("dev_review", "1", {"task": f"t{i}"}, trace_id=f"p{i}")
        from jobmate.models import RunStatus

        s.update_run(r.id, status=RunStatus.FAILED)
    # 사전 실패 2 + 이 이벤트 run 1 = 3/3 → 100%. ne 50은 주기 평가에서
    # 실제 값 100.0과 비교해 성립한다. 이벤트 경로가 이 규칙을 직접
    # 매칭했다면 fingerprint가 'run:...'이 됐을 것 — 'periodic:' 지문이
    # 주기 평가 경로로 발화했음을 증명한다.
    _mk_rule(s, {"field": "error.rate_pct", "op": "ne", "value": 50})
    run = _mk_run(s, "failed")
    n = svc.emit_run_status_event(s, run.id)
    assert n == 1
    fire = s.list_alert_fires()[0]
    assert fire["event_fingerprint"].startswith("periodic:")


def test_session_stale_count_periodic():
    """working 세션이 stale 임계(300s) 초과 → stale_count 집계."""
    s = _store()
    old = (datetime.now(UTC) - timedelta(seconds=400)).isoformat()
    for i in range(2):
        s.upsert_agent_session("claude", f"k{i}", "working")
        s._conn.execute(  # noqa: SLF001
            "UPDATE agent_sessions SET last_event_at=?, updated_at=? WHERE session_key=?",
            (old, old, f"k{i}"),
        )
    # 활성 세션 + ended 세션은 카운트 제외
    s.upsert_agent_session("claude", "fresh", "working")
    s.upsert_agent_session("claude", "done", "ended")
    s._conn.commit()

    _mk_rule(s, {"field": "session.stale_count", "op": "gte", "value": 2})
    _mk_rule(s, {"field": "session.stale_count", "op": "gt", "value": 5}, name="many")
    assert svc.evaluate_periodic_rules(s, org_id="", force=True) == 1
    assert len(s.list_alert_fires()) == 1


def test_session_stale_count_beyond_recent_limit():
    """stale 세션이 최신 활동 상위 밖에 있어도 COUNT로 정확히 집계한다.

    구 구현은 list_agent_sessions(limit=500, last_event_at DESC) 상위만
    스캔해 가장 오래된 stale 세션이 잘려 과소계상됐다 — 회귀 테스트는
    활성 세션 600개(구 limit 초과) 뒤에 stale 세션을 둔다."""
    s = _store()
    now = datetime.now(UTC)
    recent = now.isoformat()
    old = (now - timedelta(seconds=400)).isoformat()
    vals = [
        (f"pad{i}", "claude", f"pad{i}", "idle", recent, recent, recent, "") for i in range(600)
    ]
    vals += [(f"st{i}", "claude", f"st{i}", "working", old, old, old, "") for i in range(3)]
    s._conn.executemany(  # noqa: SLF001
        "INSERT INTO agent_sessions(id,tool,session_key,status,last_event_at,"
        "created_at,updated_at,org_id) VALUES(?,?,?,?,?,?,?,?)",
        vals,
    )
    s._conn.commit()
    # 'idle'은 session_health 기준 active — stale 카운트 제외
    assert svc._session_stale_count(s) == 3


def test_alert_rule_sweep_evaluates_periodic_without_events():
    """이벤트가 전혀 없어도 _alert_rule_sweep이 주기 규칙을 평가한다 —
    조용한 시스템의 session.stale_count 규칙이 stale 세션만으로 발화."""
    from jobmate.api import jobs as api_jobs

    s = _store()
    old = (datetime.now(UTC) - timedelta(seconds=400)).isoformat()
    s.upsert_agent_session("claude", "stale-sweep", "working")
    s._conn.execute(  # noqa: SLF001
        "UPDATE agent_sessions SET last_event_at=?, updated_at=? WHERE session_key=?",
        (old, old, "stale-sweep"),
    )
    s._conn.commit()
    _mk_rule(s, {"field": "session.stale_count", "op": "gte", "value": 1})

    api_jobs._alert_rule_sweep(s)
    fires = s.list_alert_fires()
    assert len(fires) == 1
    assert fires[0]["event_fingerprint"].startswith("periodic:")


def test_cost_delta_pct_periodic():
    """오늘 비용 vs 직전 7일 일평균 — today=300, prev avg=100 → +200%."""
    s = _store()
    now = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
    today = now.date()
    # 직전 7일 매일 100_000_000 µUSD
    for i in range(1, 8):
        d = (today - timedelta(days=i)).isoformat()
        s.record_cost_event(
            idempotency_key=f"prev{i}",
            cost_status="estimated",
            cost_microusd=100_000_000,
            occurred_at=f"{d}T01:00:00+00:00",
        )
    s.record_cost_event(
        idempotency_key="today",
        cost_status="estimated",
        cost_microusd=300_000_000,
        occurred_at=f"{today.isoformat()}T02:00:00+00:00",
    )
    # 윈도우 밖(8일 전) 비용은 기준선에 포함되지 않는다
    s.record_cost_event(
        idempotency_key="old",
        cost_status="estimated",
        cost_microusd=9_000_000_000,
        occurred_at=f"{(today - timedelta(days=8)).isoformat()}T02:00:00+00:00",
    )

    _mk_rule(s, {"field": "cost.delta_pct", "op": "gt", "value": 50})
    _mk_rule(s, {"field": "cost.delta_pct", "op": "gt", "value": 300}, name="huge")
    assert svc.evaluate_periodic_rules(s, org_id="", now=now, force=True) == 1
    fire = s.list_alert_fires()[0]
    assert fire["event_fingerprint"].startswith("periodic:")
    # 인박스 본문에 cost.delta_pct 값이 실린다
    inbox = s.list_inbox(limit=10)
    assert any("cost.delta_pct" in (i.get("body") or "") for i in inbox)


def test_periodic_org_isolation():
    """org-b 규칙은 org-b 데이터로만 평가 — 플랫폼(org='') 평가에 섞이지 않는다."""
    from jobmate.models import RunStatus

    s = _store()
    oid = s.create_organization("Org B", "org-b")
    assert oid
    with orgctx.org_scope(oid):
        _mk_rule(s, {"field": "error.rate_pct", "op": "gt", "value": 0})
        for i in range(2):
            r = s.create_run("dev_review", "1", {"task": f"b{i}"}, trace_id=f"b{i}", org_id=oid)
            s.update_run(r.id, status=RunStatus.FAILED)
    # 플랫폼 컨텍스트 — org-b run은 org_id='' 쿼리에 잡히지 않아 rate=0
    assert svc.evaluate_periodic_rules(s, org_id="", force=True) == 0
    # org-b 스코프로 평가하면 발화
    assert svc.evaluate_periodic_rules(s, org_id=oid, force=True) == 1


# ---- API 수준 ----


def _api_env(tmp_path):
    from fastapi.testclient import TestClient

    from jobmate.api.app import create_app
    from jobmate.config import Settings

    db = str(tmp_path / "api.db")
    s = Store(db)
    app = create_app(settings=Settings(db_path=db), store=s, llm=None)
    return TestClient(app), s


def test_api_accepts_new_condition_fields(tmp_path):
    client, _s = _api_env(tmp_path)
    client.headers["X-API-Key"] = "alert-test-key"
    for field in [
        "run.latency_ms",
        "error.error_class",
        "error.rate_pct",
        "session.stale_count",
        "cost.delta_pct",
    ]:
        r = client.post(
            "/api/v1/alert-rules",
            json={
                "name": f"rule-{field}",
                "condition": {"field": field, "op": "gte", "value": 1},
                "channels": ["inbox"],
            },
        )
        assert r.status_code == 201, (field, r.text)
        assert r.json()["condition"]["field"] == field


def test_api_rejects_unknown_condition_field(tmp_path):
    client, _s = _api_env(tmp_path)
    client.headers["X-API-Key"] = "alert-test-key"
    r = client.post(
        "/api/v1/alert-rules",
        json={
            "name": "bad",
            "condition": {"field": "run.nope", "op": "eq", "value": "x"},
            "channels": ["inbox"],
        },
    )
    assert r.status_code == 422
