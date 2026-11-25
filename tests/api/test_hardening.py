"""상용 배포 감사 후속 회귀 테스트.

stale reaper / 전역 예외 핸들러+request_id / 동시 run 세마포어 /
월간 budget 429 / retention sweep / 계정 삭제+export / 약관 동의 / 캐시 헤더.
"""

from __future__ import annotations

import tempfile
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from jobmate.api import support as app_module
from jobmate.api.app import create_app
from jobmate.config import Settings
from jobmate.models import RunStatus, StageStatus
from jobmate.store.db import Store
from tests.integration.helpers import default_outputs, scripted_llm


def _client() -> tuple[TestClient, Store, FastAPI]:
    db = str(Path(tempfile.mkdtemp()) / "t.db")
    settings = Settings(db_path=db, budget_max_tokens=100_000)
    store = Store(db)
    llm = scripted_llm(default_outputs())
    application = create_app(settings=settings, store=store, llm=llm)
    return TestClient(application), store, application


def _store() -> Store:
    return Store(str(Path(tempfile.mkdtemp()) / "t.db"))


def _backdate(store: Store, sql: str, row_id: str, ts: str) -> None:
    """테스트용 타임스탬프 역기입 — tx()로 커밋 보장."""
    with store.tx() as c:
        c.execute(sql, (ts, row_id))


# ---------- 1. stale run reaper ----------


def test_fail_stale_runs_reaps_only_stale_active_runs():
    store = _store()
    old = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    recent = datetime.now(UTC).isoformat()

    stale = store.create_run("dev_review", "0.1.0", {"task": "t"}, trace_id="a")
    store.update_run(stale.id, status=RunStatus.RUNNING)
    _backdate(store, "UPDATE runs SET updated_at=? WHERE id=?", stale.id, old)

    fresh = store.create_run("dev_review", "0.1.0", {"task": "t"}, trace_id="b")
    store.update_run(fresh.id, status=RunStatus.RUNNING)
    _backdate(store, "UPDATE runs SET updated_at=? WHERE id=?", fresh.id, recent)

    waiting = store.create_run("dev_review", "0.1.0", {"task": "t"}, trace_id="c")
    store.update_run(waiting.id, status=RunStatus.WAITING_APPROVAL)
    _backdate(store, "UPDATE runs SET updated_at=? WHERE id=?", waiting.id, old)

    cutoff = (datetime.now(UTC) - timedelta(minutes=30)).isoformat()
    assert store.fail_stale_runs(cutoff, error="stale_timeout") == 1

    assert store.get_run(stale.id).status == RunStatus.FAILED
    assert store.get_run(stale.id).error == "stale_timeout"
    assert store.get_run(fresh.id).status == RunStatus.RUNNING
    # waiting_approval은 사람 승인 대기 — reaper가 절대 건드리지 않는다
    assert store.get_run(waiting.id).status == RunStatus.WAITING_APPROVAL


def test_fail_stale_runs_closes_running_stage_runs():
    store = _store()
    old = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    run = store.create_run("dev_review", "0.1.0", {"task": "t"}, trace_id="a")
    store.update_run(run.id, status=RunStatus.RUNNING)
    store.start_stage(run.id, "implement", 1, {})
    _backdate(store, "UPDATE runs SET updated_at=? WHERE id=?", run.id, old)

    cutoff = (datetime.now(UTC) - timedelta(minutes=30)).isoformat()
    store.fail_stale_runs(cutoff)
    stages = store.get_stage_runs(run.id)
    assert stages[0].status == StageStatus.FAILED


def test_cron_tick_reaps_stale_run():
    client, store, _ = _client()
    app_module._REAPER_STATE["last_at"] = 0.0  # 60초 스로틀 리셋
    run = store.create_run("dev_review", "0.1.0", {"task": "t"}, trace_id="a")
    store.update_run(run.id, status=RunStatus.RUNNING)
    old = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    _backdate(store, "UPDATE runs SET updated_at=? WHERE id=?", run.id, old)

    r = client.post("/api/v1/cron/tick")
    assert r.status_code == 200
    d = client.get(f"/api/v1/runs/{run.id}").json()
    assert d["status"] == "failed"
    assert d["error"] == "stale_timeout"


# ---------- 2. 예외 핸들러 + request_id ----------


def test_error_response_reuses_access_log_request_id():
    client, _, _ = _client()
    r = client.get("/api/v1/runs/nonexistent", headers={"x-request-id": "rid-abc-1"})
    assert r.status_code == 404
    assert r.json()["error"]["request_id"] == "rid-abc-1"
    assert r.headers["x-request-id"] == "rid-abc-1"


def test_unhandled_exception_returns_500_without_stack():
    # Exception 핸들러는 ServerErrorMiddleware가 응답 후 재전파하므로
    # raise_server_exceptions=False로 500 응답을 검증한다.
    db = str(Path(tempfile.mkdtemp()) / "t.db")
    settings = Settings(db_path=db, budget_max_tokens=100_000)
    store = Store(db)
    llm = scripted_llm(default_outputs())
    application = create_app(settings=settings, store=store, llm=llm)
    client = TestClient(application, raise_server_exceptions=False)

    @application.get("/api/v1/boom")
    def boom() -> None:
        raise RuntimeError("boom-secret-internal-detail")

    r = client.get("/api/v1/boom", headers={"x-request-id": "rid-boom"})
    assert r.status_code == 500
    body = r.json()
    assert body["error"]["code"] == "internal_error"
    assert body["error"]["request_id"] == "rid-boom"
    assert "boom-secret" not in r.text
    assert "Traceback" not in r.text


# ---------- 3. 동시 run 세마포어 ----------


def test_run_semaphore_blocks_until_slot_released(monkeypatch):
    client, store, _ = _client()
    sem = threading.BoundedSemaphore(1)
    sem.acquire()  # 유일 슬롯 점유 — run은 대기해야 한다
    monkeypatch.setattr(app_module, "_RUN_SEMAPHORE", sem)

    r = client.post("/api/v1/runs", json={"workflow": "dev_review", "task": "t"})
    assert r.status_code == 202
    run_id = r.json()["id"]

    time.sleep(0.5)
    # 슬롯이 없으므로 runner.invoke에 진입하지 못해 queued 유지
    assert store.get_run(run_id).status == RunStatus.QUEUED

    sem.release()
    for _ in range(100):
        st = store.get_run(run_id).status
        if st in (RunStatus.WAITING_APPROVAL, RunStatus.COMPLETED, RunStatus.FAILED):
            break
        time.sleep(0.1)
    assert store.get_run(run_id).status == RunStatus.WAITING_APPROVAL


# ---------- 4. 월간 budget 집행 ----------


def test_monthly_budget_exceeded_returns_429():
    client, store, _ = _client()
    store.set_state("prefs", {"monthly_budget_usd": 0.001})
    # claude 계열 단가로 환산 시 0.018 USD — 예산 초과
    store.record_llm_usage("claude-test", 1000, 1000)
    r = client.post("/api/v1/runs", json={"workflow": "dev_review", "task": "t"})
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "budget_exceeded"


def test_monthly_budget_under_allows_run():
    client, store, _ = _client()
    store.set_state("prefs", {"monthly_budget_usd": 100.0})
    store.record_llm_usage("claude-test", 1000, 1000)
    r = client.post("/api/v1/runs", json={"workflow": "dev_review", "task": "t"})
    assert r.status_code == 202


# ---------- 5. retention sweep ----------


def test_retention_sweep_deletes_only_expired_terminal_data():
    store = _store()
    now = datetime.now(UTC)
    old = (now - timedelta(days=200)).isoformat()
    mid = (now - timedelta(days=40)).isoformat()

    # 200일 전 completed run + stage — 삭제 대상
    dead = store.create_run("dev_review", "0.1.0", {"task": "t"}, trace_id="d")
    store.update_run(dead.id, status=RunStatus.COMPLETED)
    store.start_stage(dead.id, "s1", 1, {})
    with store.tx() as c:
        c.execute("UPDATE runs SET created_at=? WHERE id=?", (old, dead.id))
        c.execute("UPDATE stage_runs SET started_at=? WHERE run_id=?", (old, dead.id))

    # 200일 전 running run — 진행 중이므로 절대 삭제 금지
    live = store.create_run("dev_review", "0.1.0", {"task": "t"}, trace_id="l")
    store.update_run(live.id, status=RunStatus.RUNNING)
    with store.tx() as c:
        c.execute("UPDATE runs SET created_at=? WHERE id=?", (old, live.id))

    # 200일 전 audit/llm_usage — 삭제 대상
    store.audit("", "sys", "old_event", "", {})
    store.record_llm_usage("m", 1, 1)
    with store.tx() as c:
        c.execute("UPDATE audit_events SET created_at=? WHERE action='old_event'", (old,))
        c.execute("UPDATE llm_usage SET created_at=?", (old,))

    # 만료된 세션 — 삭제 대상
    user = store.create_user("ret@example.com", "", "n")
    assert user is not None
    store.create_session(user["id"], "deadbeef" * 8, ttl_days=-1)

    # 40일 전 processed 메시지 — source_text만 비움
    store.save_message_extraction("gmail", "m-old", "", {}, "processed", source_text="secret")
    with store.tx() as c:
        c.execute("UPDATE message_extractions SET created_at=? WHERE source_id='m-old'", (mid,))

    # 40일 전 agent_session_events — 삭제 대상
    with store.tx() as c:
        c.execute(
            "INSERT INTO agent_session_events(id,tool,session_key,status,created_at)"
            " VALUES('ev1','t','k','s',?)",
            (mid,),
        )

    counts = store.retention_sweep()
    assert counts["runs"] == 1
    assert counts["stage_runs"] == 1
    assert counts["audit_events"] >= 1
    assert counts["llm_usage"] >= 1
    assert counts["auth_sessions"] >= 1
    assert counts["agent_session_events"] >= 1
    assert counts["message_extractions_cleared"] == 1

    assert store.get_run(dead.id) is None
    assert store.get_run(live.id) is not None  # 진행 중 run 보존
    rows = store.list_message_extractions("gmail")
    assert rows and rows[0]["source_text"] == ""
    assert rows[0]["processing_status"] == "processed"  # 행은 유지


def test_retention_sweep_via_cron_tick_is_throttled():
    client, store, _ = _client()
    app_module._REAPER_STATE["last_at"] = 0.0
    # 첫 틱에서 sweep 실행 → automation_state에 기록
    assert client.post("/api/v1/cron/tick").status_code == 200
    state = store.get_state("retention_state", {})
    assert state.get("last_sweep_at")
    # 두 번째 틱은 20시간 가드로 스킵 — state가 덮어쓰이지 않는다
    first = state["last_sweep_at"]
    assert client.post("/api/v1/cron/tick").status_code == 200
    assert store.get_state("retention_state", {})["last_sweep_at"] == first


# ---------- 6. last_tick_at 인스턴스 무관화 ----------


def test_health_detail_reports_db_backed_last_tick():
    client, store, _ = _client()
    app_module._REAPER_STATE["last_at"] = 0.0
    assert client.post("/api/v1/cron/tick").status_code == 200
    db_tick = store.get_state("cron_tick", {}).get("last_tick_at")
    assert db_tick
    r = client.get("/api/v1/health/detail")
    assert r.status_code == 200
    body = r.json()
    assert body["cron"]["last_tick_at"] == db_tick
    assert "reaper" in body["cron"]


# ---------- 7. 계정 삭제 + 본인 export ----------


def _signup(client: TestClient, email: str, password: str = "password123") -> None:  # noqa: S107
    r = client.post(
        "/api/v1/auth/signup",
        json={
            "email": email,
            "password": password,
            "display_name": "T",
            "agree_terms": True,
        },
    )
    assert r.status_code == 201, r.text


def test_delete_account_requires_password_and_blocks_last_admin():
    client, store, _ = _client()
    _signup(client, "admin@example.com")  # 첫 사용자 → 자동 admin
    _signup(client, "member@example.com")  # member

    # member 세션으로 잘못된 비밀번호 → 401
    r = client.request("DELETE", "/api/v1/auth/account", json={"password": "wrong-password"})
    assert r.status_code == 401

    # 올바른 비밀번호 → 삭제 성공, 세션도 무효화
    r = client.request("DELETE", "/api/v1/auth/account", json={"password": "password123"})
    assert r.status_code == 200
    assert store.get_user_by_email("member@example.com") is None
    assert store.get_user_by_email("admin@example.com") is not None
    assert client.get("/api/v1/auth/export").status_code == 401

    # 남은 admin(첫 사용자)의 세션은 별도로 로그인해 last_admin 확인
    client2, _, _ = _client()
    _signup(client2, "only@example.com")
    r = client2.request("DELETE", "/api/v1/auth/account", json={"password": "password123"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "last_admin"


def test_export_returns_own_data_as_attachment():
    client, store, _ = _client()
    _signup(client, "exp@example.com")
    r = client.get("/api/v1/auth/export")
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]
    data = r.json()
    assert data["user"]["email"] == "exp@example.com"
    assert "password_hash" not in data["user"]
    assert data["sessions"], "export에 세션 메타가 포함되어야 한다"
    # export 행위 자체가 감사 로그에 남는다
    assert any(a["action"] == "auth_data_export" for a in store.list_audit(500))


# ---------- 8. 가입 약관 동의 ----------


def test_signup_terms_required_in_auth_mode(monkeypatch):
    monkeypatch.setenv("JOBMATE_API_KEY", "terms-test-key")
    monkeypatch.setenv("JOBMATE_ALLOWED_EMAILS", "terms@example.com")
    client, _, _ = _client()
    headers = {"x-api-key": "terms-test-key"}
    # agree_terms 누락 → 422 terms_required
    r = client.post(
        "/api/v1/auth/signup",
        json={"email": "terms@example.com", "password": "password123"},
        headers=headers,
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "terms_required"
    # 동의 포함 → 가입 진행 (이메일 인증 대기)
    r = client.post(
        "/api/v1/auth/signup",
        json={
            "email": "terms@example.com",
            "password": "password123",
            "agree_terms": True,
        },
        headers=headers,
    )
    assert r.status_code == 201


def test_signup_terms_exempt_in_local_dev_mode():
    client, _, _ = _client()
    r = client.post(
        "/api/v1/auth/signup",
        json={"email": "dev@example.com", "password": "password123"},
    )
    assert r.status_code == 201


# ---------- 9. 캐시 헤더 ----------


def test_index_and_service_worker_no_cache():
    client, _, _ = _client()
    # v2/dist 존재 시 /는 /app/으로 302 — 두 경로 모두 no-cache여야 한다
    r = client.get("/", follow_redirects=False)
    assert r.headers["cache-control"] == "no-cache"
    assert client.get("/sw.js").headers["cache-control"] == "no-cache"
