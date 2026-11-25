"""Security-audit regression tests — role gates, atomic claims, sanitization."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import jobmate.api.app as app_mod
from jobmate.api.app import create_app
from jobmate.config import Settings
from jobmate.models import RunStatus
from jobmate.store.db import Store


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("JOBMATE_ALLOWED_EMAILS", raising=False)
    monkeypatch.delenv("VERCEL", raising=False)
    store = Store(str(tmp_path / "audit.db"))
    app = create_app(
        settings=Settings(db_path=tmp_path / "audit.db"), store=store, llm=None, adapters={}
    )
    yield TestClient(app), store
    store.close()


def _signup(c: TestClient, email: str) -> None:
    r = c.post("/api/v1/auth/signup", json={"email": email, "password": "password123"})
    assert r.status_code == 201, r.text


def _member_client(app) -> TestClient:
    """두 번째 가입 사용자(member role) 클라이언트 — 첫 가입자는 admin."""
    admin = TestClient(app)
    member = TestClient(app)
    _signup(admin, "admin@example.com")
    _signup(member, "member@example.com")
    return member


# ---- role gates ----


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/insights",
        "/api/v1/playbooks",
        "/api/v1/team",
        "/api/v1/triggers",
        "/api/v1/connectors",
        "/api/v1/workflows/custom",
        "/api/v1/audit",
        "/api/v1/automation/overview",
        "/api/v1/developer/status",
        "/api/v1/archive",
        "/api/v1/event-links",
    ],
)
def test_member_gets_403_on_lead_endpoints(client, path):
    c, _ = client
    member = _member_client(c.app)
    assert member.get(path).status_code == 403


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/runs",
        "/api/v1/inbox",
        "/api/v1/notifications",
        # inbox/모바일 요약 바 위젯 — 읽기 전용 GET은 member 허용(쓰기만 lead)
        "/api/v1/cards",
        "/api/v1/usage/summary",
    ],
)
def test_member_can_access_member_endpoints(client, path):
    c, _ = client
    member = _member_client(c.app)
    assert member.get(path).status_code == 200


def test_admin_session_passes_lead_gate(client):
    c, _ = client
    _signup(c, "admin@example.com")  # 첫 사용자 → admin
    assert c.get("/api/v1/insights").status_code == 200
    assert c.get("/api/v1/cards").status_code == 200


def test_lead_endpoints_open_in_local_mode(client):
    """인증 미설정 로컬 모드 — lead/admin 게이트는 local-admin으로 통과."""
    c, _ = client
    assert c.get("/api/v1/insights").status_code == 200
    assert c.get("/api/v1/cards").status_code == 200


# ---- radar/refresh platforms 검증 ----


@pytest.mark.parametrize(
    "payload",
    [
        {"platforms": "claude"},  # list가 아님
        {"platforms": ["evil-platform"]},  # 화이트리스트 외
        {"platforms": ["claude", "chatgpt", "gemini", "extra"]},  # 개수 초과
        {"platforms": [1, 2]},  # 비문자열 원소
    ],
)
def test_radar_refresh_rejects_bad_platforms(client, payload):
    c, _ = client
    r = c.post("/api/v1/radar/refresh", json=payload)
    assert r.status_code == 422, r.text


def test_radar_refresh_empty_platforms_ok(client):
    c, _ = client
    r = c.post("/api/v1/radar/refresh", json={"platforms": []})
    assert r.status_code == 202
    assert r.json()["run_ids"] == []


# ---- 음수/과대 limit 클램프 ----


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/notifications?limit=-5",
        "/api/v1/events?limit=-1",
        "/api/v1/notification-jobs?limit=-1",
        "/api/v1/email/summaries?limit=-3",
        "/api/v1/archive?limit=-1",
    ],
)
def test_negative_limit_clamped(client, path):
    c, _ = client
    assert c.get(path).status_code == 200


# ---- CSV 수식 인젝션 살균 ----


def test_csv_export_sanitizes_formula_cells(client):
    c, store = client
    run = store.create_run(
        "dev_review",
        "1",
        {"task": "t"},
        trace_id="t1",
        created_by='=HYPERLINK("http://evil")',
    )
    store.update_run(run.id, status=RunStatus.FAILED, error="=cmd|' /c calc'!A1")
    r = c.get("/api/v1/export?kind=runs&format=csv&days=30")
    assert r.status_code == 200
    body = r.text
    assert "'=HYPERLINK" in body
    assert "'=cmd" in body
    # 원시 수식이 셀 선두에 오면 안 됨
    for line in body.splitlines():
        for cell in line.split(","):
            assert not cell.startswith("=")


# ---- Store 원자 claim ----


def _store(tmp_path) -> Store:
    return Store(str(tmp_path / "s.db"))


def test_claim_message_exclusive(tmp_path):
    store = _store(tmp_path)
    try:
        assert store.save_message_extraction("gmail", "m1", "th", {}, "pending", source_text="hi")
        rows = store.pending_messages()
        assert any(r["source_id"] == "m1" for r in rows)
        # 첫 claim 성공, 두 번째는 리스 중이라 실패
        assert store.claim_message("gmail", "m1") is True
        assert store.claim_message("gmail", "m1") is False
        # 리스 중에는 pending_messages에도 안 잡힘
        assert all(r["source_id"] != "m1" for r in store.pending_messages())
        # pending으로 되돌리면(retry_at 과거) 재claim 가능
        store.finish_message("gmail", "m1", "pending", retry_at="", attempts=1)
        assert store.claim_message("gmail", "m1") is True
    finally:
        store.close()


def test_claim_schedule_fire_cas(tmp_path):
    store = _store(tmp_path)
    try:
        sched = store.create_schedule("dev_review", {"task": "x"}, 60)
        sid = sched["id"]
        # next_run_at을 과거로 당겨 due 상태로 만든다
        past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
        with store.tx() as c:
            c.execute("UPDATE workflow_schedules SET next_run_at=? WHERE id=?", (past, sid))
        due = store.due_schedules()
        assert any(s["id"] == sid for s in due)
        expected = next(s["next_run_at"] for s in due if s["id"] == sid)
        fired = datetime.now(UTC).isoformat()
        nxt = (datetime.now(UTC) + timedelta(minutes=60)).isoformat()
        assert store.claim_schedule_fire(sid, expected, fired, nxt) is True
        # 같은 expected로 재claim은 실패 (next_run_at이 이미 변경됨)
        assert store.claim_schedule_fire(sid, expected, fired, nxt) is False
    finally:
        store.close()


def test_claim_event_review_atomic(tmp_path):
    store = _store(tmp_path)
    try:
        store.save_scheduled_event(
            event_id="ev1",
            source_provider="gmail",
            source_id="m1",
            title="회의",
            start_at="2026-01-01T10:00:00+00:00",
            end_at="2026-01-01T11:00:00+00:00",
            timezone="UTC",
            status="needs_review",
        )
        stale = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
        assert store.claim_event_review("ev1", stale) is True
        # 이미 registering — stale_before 이전이 아니면 재claim 불가
        assert store.claim_event_review("ev1", stale) is False
        ev = store.get_scheduled_event("ev1")
        assert ev is not None and ev["status"] == "registering"
    finally:
        store.close()


def test_claim_run_status_conditional(tmp_path):
    store = _store(tmp_path)
    try:
        run = store.create_run("dev_review", "1", {"task": "t"}, trace_id="t")
        assert store.claim_run_status(run.id, "queued", "running") is True
        assert store.claim_run_status(run.id, "queued", "running") is False
        assert store.claim_run_status(run.id, "running", "waiting_approval") is True
    finally:
        store.close()


def test_update_run_does_not_clobber_status(tmp_path):
    """update_run(status=None)은 기존 status를 유지한다 (lost update 방지)."""
    store = _store(tmp_path)
    try:
        run = store.create_run("dev_review", "1", {"task": "t"}, trace_id="t")
        store.update_run(run.id, status=RunStatus.RUNNING)
        store.update_run(run.id, error="boom")  # status 미지정
        done = store.get_run(run.id)
        assert done is not None and done.status == RunStatus.RUNNING
        assert done.error == "boom"
    finally:
        store.close()


# ---- gmail poll 백오프 ----


def test_gmail_poll_failure_backs_off(tmp_path, monkeypatch):
    store = _store(tmp_path)
    try:
        store.set_automation({"enabled": True, "last_gmail_poll_at": ""})

        class BadGmail:
            calls = 0

            def list_messages(self, *a, **k):
                self.calls += 1
                raise RuntimeError("gmail down")

        gmail = BadGmail()
        settings = Settings(db_path=str(tmp_path / "s.db"), gmail_poll_interval_seconds=30)
        adapters = {"gmail": gmail}

        app_mod._maybe_poll_gmail(store, settings, adapters)
        auto = store.get_automation()
        assert auto["last_gmail_error"] == "RuntimeError"
        assert auto["gmail_fail_count"] == 1
        assert auto["gmail_backoff_until"]

        # 백오프 중에는 재호출해도 gmail을 두드리지 않는다
        app_mod._maybe_poll_gmail(store, settings, adapters)
        assert gmail.calls == 1
    finally:
        store.close()


# ---- _create_and_run 실패 시 run FAILED ----


def test_create_and_run_marks_failed_on_invoke_error(tmp_path, monkeypatch):
    store = _store(tmp_path)
    try:

        class BadRunner:
            def invoke(self, run_id, payload, gmail_ctx=None):
                raise RuntimeError("invoke exploded")

        monkeypatch.setattr(app_mod, "build_runner", lambda *a, **k: BadRunner())
        settings = Settings(db_path=str(tmp_path / "s.db"))
        with pytest.raises(RuntimeError):
            app_mod._create_and_run(store, settings, None, {}, "dev_review", "task", None)
        runs = store.list_runs(10)
        assert len(runs) == 1
        assert runs[0].status == RunStatus.FAILED
        assert "invoke exploded" in (runs[0].error or "")
    finally:
        store.close()
