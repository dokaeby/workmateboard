"""spec 005 US1 — 데이터 보존 정책 관리와 자동 purge.

커버: 기본 sweep 동작 유지, DB override + 감사, 법정 하한 422,
dry-run 무삭제+기록, purge_runs 기록, 배치 멱등(재실행 건수 0),
비종결 run/미완료 wakeup/open incident/pending eval 보존.
"""

from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jobmate.api.app import create_app
from jobmate.api.appctx import build_app_context
from jobmate.api.routers import retention as retention_router
from jobmate.config import Settings
from jobmate.models import RunStatus
from jobmate.security import hash_password, hash_token
from jobmate.store.db import Store
from tests.integration.helpers import default_outputs, scripted_llm

_PW = "Str0ng#Passw0rd!"


def _store() -> Store:
    return Store(str(Path(tempfile.mkdtemp()) / "t.db"))


def _env(monkeypatch):
    monkeypatch.setenv("JOBMATE_API_KEY", "test-api-key")
    for v in (
        "JOBMATE_ALLOWED_EMAILS",
        "VERCEL",
        "JOBMATE_MODE",
        "JOBMATE_ENCRYPTION_KEY",
        "JOBMATE_PUBLIC_HOSTS",
        "JOBMATE_ADMIN_EMAILS",
        "JOBMATE_LEAD_EMAILS",
    ):
        monkeypatch.delenv(v, raising=False)


def _client(monkeypatch):
    _env(monkeypatch)
    db = str(Path(tempfile.mkdtemp()) / "t.db")
    settings = Settings(db_path=db, budget_max_tokens=100_000)
    store = Store(db)
    llm = scripted_llm(default_outputs())
    app = create_app(settings=settings, store=store, llm=llm)
    # app.py 라우터 등록은 부모 작업 — 테스트에서는 동일 ctx로 수동 등록
    retention_router.register(app, build_app_context(store, settings, llm, {}))
    c = TestClient(app)
    c.headers.update({"Authorization": "Bearer test-api-key"})
    return c, store


def _backdate(store: Store, sql: str, params: tuple) -> None:
    with store.tx() as c:
        c.execute(sql, params)


def _audit(store: Store, action: str) -> None:
    store.audit("", "tester", action, "", {})


def _audit_rows(store: Store, action: str) -> list[dict]:
    with store.tx() as c:
        rows = c.execute("SELECT * FROM audit_events WHERE action=?", (action,)).fetchall()
    return [dict(r) for r in rows]


def _mkuser(store: Store, email: str, role: str = "") -> str:
    user = store.create_user(email, hash_password(_PW), "U", "", True)
    uid = user["id"] if isinstance(user, dict) else user
    if role:
        store.set_user_role(uid, role)
    return uid


def _sess(store: Store, app, user_id: str, tag: str) -> TestClient:
    token = f"sess-{tag}-{user_id}"
    store.create_session(user_id, hash_token(token))
    c = TestClient(app)
    c.cookies.set("jobmate_session", token)
    return c


# ---------- store 레벨: 기본 sweep ----------


def test_default_sweep_audit_ttl_365_boundary() -> None:
    """audit_events 기본 TTL은 법정 하한 365일 — 400일 전만 삭제, 100일 전 유지."""
    store = _store()
    old = (datetime.now(UTC) - timedelta(days=400)).isoformat()
    mid = (datetime.now(UTC) - timedelta(days=100)).isoformat()
    _audit(store, "old_evt")
    _audit(store, "mid_evt")
    _backdate(
        store,
        "UPDATE audit_events SET created_at=? WHERE action='old_evt'",
        (old,),
    )
    _backdate(
        store,
        "UPDATE audit_events SET created_at=? WHERE action='mid_evt'",
        (mid,),
    )
    counts = store.retention_sweep()
    assert counts["audit_events"] == 1
    assert _audit_rows(store, "old_evt") == []
    assert len(_audit_rows(store, "mid_evt")) == 1
    # sweep 자체의 retention_purge 감사 이벤트가 남는다
    purge_audits = _audit_rows(store, "retention_purge")
    assert len(purge_audits) == 1
    store.close()


def test_default_sweep_preserves_non_terminal_and_inflight() -> None:
    """FR-009 — 비종결 run·미완료 wakeup·open incident·pending eval 보존."""
    store = _store()
    old = (datetime.now(UTC) - timedelta(days=400)).isoformat()

    live = store.create_run("dev_review", "0.1.0", {"task": "t"}, trace_id="live")
    store.update_run(live.id, status=RunStatus.RUNNING)
    _backdate(store, "UPDATE runs SET created_at=? WHERE id=?", (old, live.id))

    with store.tx() as c:
        c.execute(
            "INSERT INTO wakeup_requests(id,scope_key,status,created_at)"
            " VALUES('w1','s','queued',?)",
            (old,),
        )
        c.execute(
            "INSERT INTO budget_incidents(id,policy_id,scope_type,window_start,"
            "threshold_type,amount_limit,amount_observed,status,created_at)"
            " VALUES('b1','p','global','2020-01-01','tokens',1,2,'open',?)",
            (old,),
        )
    store.retention_sweep()
    assert store.get_run(live.id) is not None
    with store.tx() as c:
        assert c.execute("SELECT COUNT(*) AS n FROM wakeup_requests").fetchone()["n"] == 1
        assert c.execute("SELECT COUNT(*) AS n FROM budget_incidents").fetchone()["n"] == 1
    store.close()


def test_purge_runs_row_written() -> None:
    store = _store()
    _audit(store, "evt")
    result = store.run_purge(dry_run=False, actor="tester")
    assert result["status"] == "completed"
    with store.tx() as c:
        row = c.execute("SELECT * FROM purge_runs WHERE id=?", (result["purge_run_id"],)).fetchone()
    assert row is not None
    assert row["dry_run"] == 0
    assert row["status"] == "completed"
    counts = json.loads(row["counts_json"])
    assert "audit_events" in counts
    assert row["finished_at"]
    store.close()


def test_purge_idempotent_second_run_zero() -> None:
    """FR-004/US1-5 — 같은 데이터 재실행 시 두 번째는 0건(멱등)."""
    store = _store()
    old = (datetime.now(UTC) - timedelta(days=400)).isoformat()
    _audit(store, "old_evt")
    _backdate(
        store,
        "UPDATE audit_events SET created_at=? WHERE action='old_evt'",
        (old,),
    )
    first = store.run_purge(dry_run=False, actor="tester")
    assert first["counts"]["audit_events"] == 1
    second = store.run_purge(dry_run=False, actor="tester")
    assert second["counts"]["audit_events"] == 0
    assert _audit_rows(store, "old_evt") == []
    store.close()


def test_dry_run_deletes_nothing_and_records() -> None:
    """FR-007 — dry_run은 예상 건수만 반환, 행은 남고 purge_runs dry_run=1."""
    store = _store()
    old = (datetime.now(UTC) - timedelta(days=400)).isoformat()
    _audit(store, "old_evt")
    _backdate(
        store,
        "UPDATE audit_events SET created_at=? WHERE action='old_evt'",
        (old,),
    )
    result = store.run_purge(dry_run=True, actor="tester")
    assert result["dry_run"] is True
    assert result["counts"]["audit_events"] >= 1
    assert len(_audit_rows(store, "old_evt")) == 1  # 삭제되지 않음
    with store.tx() as c:
        row = c.execute(
            "SELECT dry_run,status FROM purge_runs WHERE id=?",
            (result["purge_run_id"],),
        ).fetchone()
    assert row["dry_run"] == 1
    assert row["status"] == "completed"
    store.close()


def test_clear_field_mode_keeps_row() -> None:
    """FR-010 — message_extractions는 source_text만 비우고 행 유지."""
    store = _store()
    old = (datetime.now(UTC) - timedelta(days=60)).isoformat()
    store.save_message_extraction(
        "gmail", "m1", "", {"k": "v"}, "processed", source_text="secret body"
    )
    _backdate(
        store,
        "UPDATE message_extractions SET created_at=? WHERE source_id='m1'",
        (old,),
    )
    counts = store.retention_sweep()
    assert counts["message_extractions_cleared"] == 1
    with store.tx() as c:
        row = c.execute(
            "SELECT source_text, processing_status FROM message_extractions WHERE source_id='m1'"
        ).fetchone()
    assert row["source_text"] == ""
    assert row["processing_status"] == "processed"
    store.close()


# ---------- store 레벨: override ----------


def test_override_changes_effective_ttl() -> None:
    """US1-1/2 — DB override가 기본값을 덮어쓰고 purge에 반영된다."""
    store = _store()
    mid = (datetime.now(UTC) - timedelta(days=100)).isoformat()
    _audit(store, "mid_evt")
    _backdate(
        store,
        "UPDATE audit_events SET created_at=? WHERE action='mid_evt'",
        (mid,),
    )
    # audit_events 기본 365일 → 100일 전 행은 유지돼야 한다
    assert store.retention_sweep()["audit_events"] == 0
    # llm_usage override 30일 → 100일 전 usage 행이 삭제 대상이 된다
    store.set_retention_policy("llm_usage", ttl_days=30, actor="tester")
    store.record_llm_usage("m", 1, 1)
    _backdate(store, "UPDATE llm_usage SET created_at=?", (mid,))
    counts = store.retention_sweep()
    assert counts["llm_usage"] == 1
    assert counts["audit_events"] == 0  # mid_evt(100d) 여전히 유지
    # override 삭제(기본값 복귀) 후에는 같은 행이 다시 보존 대상
    store.set_retention_policy("llm_usage", ttl_days=None, actor="tester")
    store.record_llm_usage("m2", 1, 1)
    _backdate(store, "UPDATE llm_usage SET created_at=? WHERE model='m2'", (mid,))
    assert store.retention_sweep()["llm_usage"] == 0
    store.close()


def test_set_policy_rejects_below_legal_min() -> None:
    from jobmate.store.retention import RetentionPolicyError

    store = _store()
    with pytest.raises(RetentionPolicyError) as ei:
        store.set_retention_policy("audit_events", ttl_days=30, actor="t")
    assert ei.value.code == "retention_below_legal_minimum"
    with pytest.raises(RetentionPolicyError) as ei2:
        store.set_retention_policy("nope", ttl_days=30, actor="t")
    assert ei2.value.code == "unknown_data_type"
    store.close()


def test_set_policy_writes_audit() -> None:
    store = _store()
    store.set_retention_policy("llm_usage", ttl_days=30, mode="hard_delete", actor="admin@x")
    rows = _audit_rows(store, "retention_policy_update")
    assert len(rows) == 1
    detail = json.loads(rows[0]["detail"])
    assert rows[0]["object_ref"] == "llm_usage"
    assert detail["new"]["ttl_days"] == 30
    assert detail["old"]["source"] == "default"
    store.close()


# ---------- API ----------


def test_api_get_and_put_retention(monkeypatch) -> None:
    client, store = _client(monkeypatch)
    r = client.get("/api/v1/admin/retention")
    assert r.status_code == 200
    items = {i["data_type"]: i for i in r.json()["items"]}
    assert items["audit_events"]["ttl_days"] == 365
    assert items["audit_events"]["source"] == "default"
    assert items["audit_events"]["legal_min_days"] == 365

    r = client.put(
        "/api/v1/admin/retention",
        json={"data_type": "llm_usage", "ttl_days": 30},
    )
    assert r.status_code == 200, r.text
    items = {i["data_type"]: i for i in r.json()["items"]}
    assert items["llm_usage"]["ttl_days"] == 30
    assert items["llm_usage"]["source"] == "override"

    r = client.put(
        "/api/v1/admin/retention",
        json={"data_type": "audit_events", "ttl_days": 30},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "retention_below_legal_minimum"

    # 감사 이벤트 기록
    rows = _audit_rows(store, "retention_policy_update")
    assert any(json.loads(r2["detail"]).get("new", {}).get("ttl_days") == 30 for r2 in rows)
    store.close()


def test_api_put_reset_override(monkeypatch) -> None:
    client, store = _client(monkeypatch)
    client.put("/api/v1/admin/retention", json={"data_type": "llm_usage", "ttl_days": 30})
    r = client.put("/api/v1/admin/retention", json={"data_type": "llm_usage", "ttl_days": None})
    assert r.status_code == 200
    items = {i["data_type"]: i for i in r.json()["items"]}
    assert items["llm_usage"]["source"] == "default"
    assert items["llm_usage"]["ttl_days"] == 180
    store.close()


def test_api_purge_dry_run_and_real(monkeypatch) -> None:
    client, store = _client(monkeypatch)
    old = (datetime.now(UTC) - timedelta(days=400)).isoformat()
    _audit(store, "api_old_evt")
    _backdate(
        store,
        "UPDATE audit_events SET created_at=? WHERE action='api_old_evt'",
        (old,),
    )
    r = client.post("/api/v1/admin/retention/purge?dry_run=true")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dry_run"] is True
    assert body["counts"]["audit_events"] >= 1
    assert len(_audit_rows(store, "api_old_evt")) == 1

    r = client.post("/api/v1/admin/retention/purge")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dry_run"] is False
    assert body["status"] == "completed"
    assert _audit_rows(store, "api_old_evt") == []

    # purge_runs 2건(dry + real)
    with store.tx() as c:
        n = c.execute("SELECT COUNT(*) AS n FROM purge_runs").fetchone()["n"]
    assert n == 2
    store.close()


def test_api_retention_requires_admin(monkeypatch) -> None:
    client, store = _client(monkeypatch)
    # 비인증 → 401
    anon = TestClient(client.app)
    assert anon.get("/api/v1/admin/retention").status_code == 401
    # member 세션 → 403
    uid = _mkuser(store, "mem@b.com")
    member = _sess(store, client.app, uid, "mem")
    assert member.get("/api/v1/admin/retention").status_code == 403
    assert member.post("/api/v1/admin/retention/purge").status_code == 403
    store.close()


# ---------- store 레벨: mode override 검증 (리뷰 수정) ----------


def test_mode_override_rejected_without_clear_field() -> None:
    """clear_field 대상 컬럼이 없는 정책에 clear_field/anonymize mode는
    RetentionPolicyError — 매 sweep 깨진 SQL/무한 재집계를 막는다."""
    from jobmate.store.retention import RetentionPolicyError

    store = _store()
    for dt in ("audit_events", "llm_usage", "runs", "agent_events", "eval_runs"):
        for mode in ("clear_field", "anonymize"):
            with pytest.raises(RetentionPolicyError) as ei:
                store.set_retention_policy(dt, mode=mode, actor="t")
            assert ei.value.code == "invalid_mode", (dt, mode)
    # clear_field 대상이 있는 정책은 세 mode 모두 허용
    store.set_retention_policy("message_extractions", mode="anonymize", actor="t")
    store.set_retention_policy("message_extractions", mode="clear_field", actor="t")
    store.set_retention_policy("message_extractions", mode="hard_delete", actor="t")
    store.close()


def test_anonymize_mode_counts_accurate_on_second_run() -> None:
    """anonymize override — 이미 마스킹된 행을 재집계하지 않는다
    (두 번째 purge 건수 0)."""
    store = _store()
    old = (datetime.now(UTC) - timedelta(days=60)).isoformat()
    store.save_message_extraction(
        "gmail", "m-anon", "", {"k": "v"}, "processed", source_text="secret body"
    )
    _backdate(
        store,
        "UPDATE message_extractions SET created_at=? WHERE source_id='m-anon'",
        (old,),
    )
    store.set_retention_policy("message_extractions", mode="anonymize", actor="t")
    first = store.run_purge(dry_run=False, actor="t")
    assert first["counts"]["message_extractions_anonymized"] == 1
    with store.tx() as c:
        row = c.execute(
            "SELECT source_text, processing_status FROM message_extractions"
            " WHERE source_id='m-anon'"
        ).fetchone()
    assert row["source_text"] == "[anonymized]"
    assert row["processing_status"] == "processed"  # 행은 유지
    second = store.run_purge(dry_run=False, actor="t")
    assert second["counts"]["message_extractions_anonymized"] == 0
    store.close()


def test_corrupt_override_row_does_not_kill_sweep() -> None:
    """ttl_days 파싱 불가 override 행 하나가 sweep 전체를 실패시키지 않는다."""
    store = _store()
    old = (datetime.now(UTC) - timedelta(days=400)).isoformat()
    _audit(store, "survivor_evt")
    _backdate(
        store,
        "UPDATE audit_events SET created_at=? WHERE action='survivor_evt'",
        (old,),
    )
    with store.tx() as c:
        c.execute(
            "INSERT INTO retention_policies(data_type,ttl_days,mode,updated_by,"
            "updated_at) VALUES('llm_usage','not-a-number','hard_delete','t',?)",
            (datetime.now(UTC).isoformat(),),
        )
    result = store.run_purge(dry_run=False, actor="t")
    assert result["status"] == "completed"
    assert result["counts"]["audit_events"] >= 1
    assert _audit_rows(store, "survivor_evt") == []
    store.close()


def test_purge_batch_over_500_deletes_all() -> None:
    """FR-004 — 500행 배치를 넘는 대상도 전부 삭제된다(재개 가능 배치)."""
    store = _store()
    old = (datetime.now(UTC) - timedelta(days=400)).isoformat()
    with store.tx() as c:
        for i in range(600):
            c.execute(
                "INSERT INTO audit_events(id,run_id,actor,action,object_ref,"
                "detail,created_at,org_id) VALUES(?,?,?,?,?,?,?,?)",
                (f"bulk-{i}", "", "t", "bulk_evt", "", "{}", old, ""),
            )
    result = store.run_purge(dry_run=False, actor="t", only={"audit_events"})
    assert result["counts"]["audit_events"] == 600
    with store.tx() as c:
        n = c.execute("SELECT COUNT(*) AS n FROM audit_events WHERE action='bulk_evt'").fetchone()[
            "n"
        ]
    assert n == 0
    store.close()


def test_partial_status_when_one_policy_fails() -> None:
    """한 유형 purge 실패 → purge_runs status='partial'(전체 failed 아님)."""
    store = _store()
    orig = store._purge_policy

    def flaky(pol, *, now, dry_run):
        if pol.key == "llm_usage":
            raise RuntimeError("boom")
        return orig(pol, now=now, dry_run=dry_run)

    store._purge_policy = flaky  # type: ignore[method-assign]
    result = store.run_purge(dry_run=False, actor="t")
    assert result["status"] == "partial"
    with store.tx() as c:
        row = c.execute(
            "SELECT status FROM purge_runs WHERE id=?", (result["purge_run_id"],)
        ).fetchone()
    assert row["status"] == "partial"
    store.close()


def test_eval_runs_pending_preserved_scored_purged() -> None:
    """FR-009 — pending eval_runs는 TTL 경과해도 보존, scored만 삭제."""
    store = _store()
    old = (datetime.now(UTC) - timedelta(days=400)).isoformat()
    with store.tx() as c:
        c.execute(
            "INSERT INTO eval_runs(id,eval_id,run_id,status,created_at)"
            " VALUES('ev-p','e1','r1','pending',?)",
            (old,),
        )
        c.execute(
            "INSERT INTO eval_runs(id,eval_id,run_id,status,score,created_at)"
            " VALUES('ev-d','e1','r1','done',0.9,?)",
            (old,),
        )
    counts = store.retention_sweep()
    assert counts.get("eval_runs") == 1
    with store.tx() as c:
        ids = {r["id"] for r in c.execute("SELECT id FROM eval_runs").fetchall()}
    assert ids == {"ev-p"}
    store.close()


def test_dry_run_keeps_expired_fixed_step_tokens() -> None:
    """FR-007 — dry_run은 만료 password_reset_tokens도 삭제하지 않는다."""
    store = _store()
    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    with store.tx() as c:
        c.execute(
            "INSERT INTO password_reset_tokens(id,user_id,email,token_hash,"
            "expires_at,created_at) VALUES('prt-1','u1','u@b.com','h',?,?)",
            (past, past),
        )
    result = store.run_purge(dry_run=True, actor="t")
    assert result["counts"].get("password_reset_tokens") == 1
    with store.tx() as c:
        n = c.execute(
            "SELECT COUNT(*) AS n FROM password_reset_tokens WHERE id='prt-1'"
        ).fetchone()["n"]
    assert n == 1  # dry-run — 행 유지
    real = store.run_purge(dry_run=False, actor="t")
    assert real["counts"].get("password_reset_tokens") == 1
    with store.tx() as c:
        n = c.execute(
            "SELECT COUNT(*) AS n FROM password_reset_tokens WHERE id='prt-1'"
        ).fetchone()["n"]
    assert n == 0
    store.close()


# ---------- API ----------


def test_api_rejects_unsupported_mode_overrides(monkeypatch) -> None:
    client, store = _client(monkeypatch)
    for dt in ("audit_events", "llm_usage", "runs"):
        for mode in ("clear_field", "anonymize"):
            r = client.put(
                "/api/v1/admin/retention",
                json={"data_type": dt, "mode": mode},
            )
            assert r.status_code == 422, (dt, mode, r.text)
            assert r.json()["error"]["code"] == "invalid_mode"
    # 지원 mode는 허용
    r = client.put(
        "/api/v1/admin/retention",
        json={"data_type": "message_extractions", "mode": "anonymize"},
    )
    assert r.status_code == 200, r.text
    store.close()


def test_api_batch_null_ttl_with_mode_rejected_atomically(monkeypatch) -> None:
    """배치 PUT — ttl_days=null+mode 조합은 검증 단계에서 거부돼
    다른 정책도 적용되지 않는다(원자성)."""
    client, store = _client(monkeypatch)
    r = client.put(
        "/api/v1/admin/retention",
        json={
            "policies": {
                "llm_usage": {"ttl_days": 30},
                "audit_events": {"ttl_days": None, "mode": "hard_delete"},
            }
        },
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_mode"
    items = {i["data_type"]: i for i in client.get("/api/v1/admin/retention").json()["items"]}
    assert items["llm_usage"]["source"] == "default"
    assert items["llm_usage"]["ttl_days"] == 180
    store.close()
