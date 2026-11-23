import tempfile

from fastapi.testclient import TestClient

from jobmate.api.app import create_app
from jobmate.config import Settings
from jobmate.store.db import Store
from tests.integration.helpers import default_outputs, scripted_llm


def _client():
    db = str(__import__("pathlib").Path(tempfile.mkdtemp()) / "t.db")
    settings = Settings(db_path=db, budget_max_tokens=100_000)
    store = Store(db)
    llm = scripted_llm(default_outputs())
    app = create_app(settings=settings, store=store, llm=llm)
    return TestClient(app), store


def test_health():
    client, _ = _client()
    assert client.get("/api/v1/health").json() == {"status": "ok"}


def test_create_run_accepted():
    client, store = _client()
    r = client.post("/api/v1/runs", json={"workflow": "dev_review", "task": "do thing"})
    assert r.status_code == 202
    run_id = r.json()["id"]
    # 백그라운드 스레드 실행 대기
    import time

    for _ in range(50):
        d = client.get(f"/api/v1/runs/{run_id}").json()
        if d["status"] in ("waiting_approval", "completed", "failed", "needs_human", "rejected"):
            break
        time.sleep(0.1)
    assert d["status"] == "waiting_approval"
    assert len(d["stages"]) == 7


def test_create_run_unknown_workflow():
    client, _ = _client()
    r = client.post("/api/v1/runs", json={"workflow": "nope", "task": "x"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "unknown_workflow"


def test_create_run_validation_error():
    client, _ = _client()
    r = client.post("/api/v1/runs", json={"workflow": "dev_review", "task": ""})
    assert r.status_code == 422


def test_list_runs_workflow_filter():
    client, store = _client()
    store.create_run("dev_review", "0.1.0", {"task": "t"}, trace_id="a")
    store.create_run("gmail_calendar", "0.1.0", {"task": "t"}, trace_id="b")
    assert len(client.get("/api/v1/runs").json()) == 2
    dr = client.get("/api/v1/runs?workflow=dev_review").json()
    assert len(dr) == 1
    assert dr[0]["workflow"] == "dev_review"
    assert client.get("/api/v1/runs?workflow=nope").json() == []


def test_run_not_found():
    client, _ = _client()
    r = client.get("/api/v1/runs/nonexistent")
    assert r.status_code == 404
    assert "request_id" in r.json()["error"]


def test_approve_flow():
    client, store = _client()
    r = client.post("/api/v1/runs", json={"workflow": "dev_review", "task": "t"})
    run_id = r.json()["id"]
    import time

    for _ in range(50):
        d = client.get(f"/api/v1/runs/{run_id}").json()
        if d["status"] == "waiting_approval":
            break
        time.sleep(0.1)
    r = client.post(f"/api/v1/runs/{run_id}/approve", json={"decision": "approve", "comment": "ok"})
    assert r.status_code == 200
    assert r.json()["status"] == "completed"


def test_approve_invalid_state():
    client, store = _client()
    run = store.create_run("dev_review", "0.1.0", {"task": "t"}, trace_id="x")
    r = client.post(f"/api/v1/runs/{run.id}/approve", json={"decision": "approve"})
    assert r.status_code == 409


def test_notifications_and_links_empty():
    client, _ = _client()
    assert client.get("/api/v1/notifications").json() == []
    assert client.get("/api/v1/event-links").json() == []


def test_index_page_served():
    client, _ = _client()
    r = client.get("/")
    assert r.status_code == 200
    assert "jobMate" in r.text


def test_email_check_and_summaries():
    import pathlib
    import tempfile

    from jobmate.adapters.fakes import FakeGmail
    from jobmate.llm import FakeLLM

    db = str(pathlib.Path(tempfile.mkdtemp()) / "t.db")
    settings = Settings(db_path=db)
    store = Store(db)
    msgs = [
        {
            "id": "m1",
            "subject": "회의 안내",
            "from_addr": "a@b.com",
            "internal_date": "1763000000000",
            "body_text": "내일 오후 2시 회의실B에서 미팅",
        }
    ]
    llm = FakeLLM(lambda **kw: "일정 안내 메일입니다\n내일 14시 미팅\n장소는 회의실B")
    app = create_app(settings=settings, store=store, llm=llm, adapters={"gmail": FakeGmail(msgs)})
    client = TestClient(app)

    r = client.post("/api/v1/email/check", json={"limit": 5})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] and d["checked"] == 1
    card = d["cards"][0]
    assert card["subject"] == "회의 안내"
    assert card["summary"].count("\n") == 2  # 정확히 3줄
    assert "mail.google.com" in card["url"]

    r2 = client.get("/api/v1/email/summaries")
    assert len(r2.json()["cards"]) == 1

    # 재호출 시 같은 source_id는 upsert (카드 수 유지)
    client.post("/api/v1/email/check", json={"limit": 5})
    assert len(client.get("/api/v1/email/summaries").json()["cards"]) == 1


def test_email_check_no_gmail_adapter():
    import pathlib
    import tempfile

    db = str(pathlib.Path(tempfile.mkdtemp()) / "t.db")
    settings = Settings(db_path=db)
    app = create_app(settings=settings, store=Store(db), llm=None, adapters={})
    client = TestClient(app)
    r = client.post("/api/v1/email/check", json={})
    assert r.status_code == 503


def test_settings_prefs_roundtrip():
    client, store = _client()
    r = client.get("/api/v1/settings")
    assert r.status_code == 200
    assert r.json()["gmail_check_minutes"] == 30

    r = client.post(
        "/api/v1/settings",
        json={
            "gmail_check_minutes": 60,
            "radar_interval": "weekly",
        },
    )
    assert r.status_code == 200
    d = client.get("/api/v1/settings").json()
    assert d["gmail_check_minutes"] == 60
    assert d["radar_interval"] == "weekly"
    # 영속화 확인
    assert store.get_state("prefs")["radar_interval"] == "weekly"

    r = client.post("/api/v1/settings", json={"gmail_check_minutes": 15})
    assert r.status_code == 422


class _FakeCalendar:
    def __init__(self, existing: str | None = None) -> None:
        self.created: list[dict] = []
        self._existing = existing

    def find_by_marker(self, marker: str) -> str | None:
        return self._existing

    def create_event(self, **kwargs) -> str:
        self.created.append(kwargs)
        return "cal-evt-1"


def _seed_review_event(store: Store, event_id: str = "gmail:m1", **over) -> None:
    kw = dict(
        event_id=event_id,
        source_provider="gmail",
        source_id="m1",
        title="팀 회의",
        start_at="not-a-date",
        end_at="2025-01-10T11:00:00+09:00",
        timezone="Asia/Seoul",
        status="needs_review",
        confidence=0.3,
        reason="bad_datetime",
    )
    kw.update(over)
    store.save_scheduled_event(**kw)


def _client_with_calendar(cal):
    db = str(__import__("pathlib").Path(tempfile.mkdtemp()) / "t.db")
    settings = Settings(db_path=db, budget_max_tokens=100_000)
    store = Store(db)
    llm = scripted_llm(default_outputs())
    app = create_app(settings=settings, store=store, llm=llm, adapters={"calendar": cal})
    return TestClient(app), store


def test_event_review_reject():
    cal = _FakeCalendar()
    client, store = _client_with_calendar(cal)
    _seed_review_event(store)
    r = client.post("/api/v1/events/gmail:m1/review", json={"action": "reject"})
    assert r.status_code == 200 and r.json()["status"] == "rejected"
    assert store.get_scheduled_event("gmail:m1")["status"] == "rejected"
    assert cal.created == []


def test_event_review_approve_with_edits():
    cal = _FakeCalendar()
    client, store = _client_with_calendar(cal)
    _seed_review_event(store)
    r = client.post(
        "/api/v1/events/gmail:m1/review",
        json={
            "action": "approve",
            "start_at": "2025-01-10T10:00:00+09:00",
            "end_at": "2025-01-10T11:00:00+09:00",
        },
    )
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["status"] == "registered" and d["calendar_event_id"] == "cal-evt-1"
    assert len(cal.created) == 1
    assert cal.created[0]["marker"] == "review:gmail:m1"
    ev = store.get_scheduled_event("gmail:m1")
    assert ev["status"] == "registered" and ev["calendar_event_id"] == "cal-evt-1"


def test_event_review_approve_still_invalid():
    cal = _FakeCalendar()
    client, store = _client_with_calendar(cal)
    _seed_review_event(store)
    r = client.post(
        "/api/v1/events/gmail:m1/review",
        json={"action": "approve", "end_at": "2020-01-01T00:00:00+09:00"},
    )
    assert r.status_code == 422
    assert store.get_scheduled_event("gmail:m1")["status"] == "needs_review"
    assert cal.created == []


def test_event_review_duplicate_marker_and_states():
    cal = _FakeCalendar(existing="cal-existing")
    client, store = _client_with_calendar(cal)
    _seed_review_event(store)
    _seed_review_event(store, event_id="gmail:m2", status="registered")
    r = client.post(
        "/api/v1/events/gmail:m1/review",
        json={
            "action": "approve",
            "start_at": "2025-01-10T10:00:00+09:00",
            "end_at": "2025-01-10T11:00:00+09:00",
        },
    )
    assert r.status_code == 200
    assert r.json()["calendar_event_id"] == "cal-existing"
    assert cal.created == []  # find_by_marker 재사용, 중복 생성 없음
    # 이미 registered인 이벤트는 리뷰 불가
    r2 = client.post("/api/v1/events/gmail:m2/review", json={"action": "reject"})
    assert r2.status_code == 409
    r3 = client.post("/api/v1/events/nope/review", json={"action": "reject"})
    assert r3.status_code == 404


def test_event_review_no_calendar_adapter():
    db = str(__import__("pathlib").Path(tempfile.mkdtemp()) / "t.db")
    settings = Settings(db_path=db, budget_max_tokens=100_000)
    store = Store(db)
    app = create_app(
        settings=settings, store=store, llm=scripted_llm(default_outputs()), adapters={}
    )
    client = TestClient(app)
    _seed_review_event(store)
    r = client.post(
        "/api/v1/events/gmail:m1/review",
        json={
            "action": "approve",
            "start_at": "2025-01-10T10:00:00+09:00",
            "end_at": "2025-01-10T11:00:00+09:00",
        },
    )
    assert r.status_code == 503


def test_settings_cronjob_autocreate(monkeypatch):
    """job id 없이 API 키+public URL만 있으면 cron job 자동 생성 후 id 영속화."""
    import jobmate.adapters.cronjob as cronjob_mod

    monkeypatch.setenv("CRONJOB_ORG_API_KEY", "ckey")
    monkeypatch.setenv("JOBMATE_PUBLIC_BASE_URL", "https://jm.example.com")
    created: list[dict] = []
    patched: list[str] = []

    def fake_create(api_key, **kw):
        created.append(kw)
        return 777 + len(created)

    monkeypatch.setattr(cronjob_mod, "create_job", fake_create)
    monkeypatch.setattr(
        cronjob_mod.CronJobOrgClient,
        "update_schedule",
        lambda self, sched, **kw: patched.append(self._job_id) or {},
    )

    db = str(__import__("pathlib").Path(tempfile.mkdtemp()) / "t.db")
    settings = Settings(db_path=db, budget_max_tokens=100_000)
    store = Store(db)
    app = create_app(
        settings=settings, store=store, llm=scripted_llm(default_outputs()), adapters={}
    )
    client = TestClient(app)

    r = client.post("/api/v1/settings", json={"gmail_check_minutes": 60})
    assert r.status_code == 200
    cj = r.json()["cronjob"]
    assert cj["email"].startswith("created(") and cj["radar"].startswith("created(")
    prefs = store.get_state("prefs")
    assert prefs["cronjob_email_job_id"] == "778"
    assert prefs["cronjob_radar_job_id"] == "779"
    assert len(created) == 2
    assert created[0]["url"] == "https://jm.example.com/api/v1/email/check"
    assert created[1]["url"] == "https://jm.example.com/api/v1/radar/refresh"

    # 두 번째 저장 — 생성된 id로 update만 수행, 중복 생성 없음
    r2 = client.post("/api/v1/settings", json={"gmail_check_minutes": 30})
    assert r2.status_code == 200
    cj2 = r2.json()["cronjob"]
    assert cj2["email"] == "updated" and cj2["radar"] == "updated"
    assert len(created) == 2
    assert patched == ["778", "779", "778", "779"]


def test_token_path_for_env_json(monkeypatch, tmp_path):
    """JOBMATE_*_TOKEN_JSON env → 임시 토큰 파일 materialize."""
    import json as _json

    from jobmate.api.app import _token_path_for

    token = {"client_id": "c", "client_secret": "s", "refresh_token": "r"}
    monkeypatch.setenv("JOBMATE_GMAIL_TOKEN_JSON", _json.dumps(token))
    settings = Settings(db_path=tmp_path / "t.db", gmail_token_path=None, calendar_token_path=None)
    path = _token_path_for("gmail", settings)
    assert path and _json.loads(open(path).read()) == token
    import os
    import stat

    if os.name != "nt":  # Windows chmod does not implement POSIX permission bits.
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    # 잘못된 JSON이면 None
    monkeypatch.setenv("JOBMATE_CALENDAR_TOKEN_JSON", "{bad")
    assert _token_path_for("calendar", settings) is None


def test_cron_tick_vercel_requires_secret(monkeypatch):
    """VERCEL에서 cron tick은 CRON_SECRET Bearer 없이는 401."""
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("CRON_SECRET", "s3cret")
    client, _ = _client()
    r = client.get("/api/v1/cron/tick")
    assert r.status_code == 401
    r2 = client.get("/api/v1/cron/tick", headers={"Authorization": "Bearer s3cret"})
    assert r2.status_code == 200
    # API 키 미들웨어 예외 경로 — JOBMATE_API_KEY 없어도 tick은 secret만 검사
    monkeypatch.setenv("JOBMATE_API_KEY", "apikey123")
    r3 = client.get("/api/v1/cron/tick", headers={"Authorization": "Bearer s3cret"})
    assert r3.status_code == 200


def test_radar_refresh_serverless_runs_via_background_tasks(monkeypatch):
    """VERCEL 환경: refresh가 BackgroundTasks로 run을 끝까지 실행해 waiting_approval 도달."""
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("JOBMATE_API_KEY", "serverless-test-key")
    from jobmate.adapters.fakes import FakeGitHub
    from jobmate.models import RadarClassification, RadarVerify

    db = str(__import__("pathlib").Path(tempfile.mkdtemp()) / "t.db")
    settings = Settings(db_path=db, budget_max_tokens=100_000)
    store = Store(db)
    gh = FakeGitHub(
        repos={
            "*": [
                {
                    "full_name": "a/x",
                    "html_url": "https://github.com/a/x",
                    "description": "desc",
                    "stars": 100,
                    "language": "Python",
                    "topics": ["agent"],
                    "archived": False,
                    "fork": False,
                    "license": "MIT",
                }
            ]
        },
        readmes={"a/x": "# readme"},
    )
    llm = scripted_llm(
        {
            "RadarClassification": [RadarClassification(label="skills", confidence=0.9)],
            "": [
                {
                    "summary_en": ["e1", "e2", "e3"],
                    "summary_zh": ["z1", "z2", "z3"],
                    "summary_ko": ["한글1", "한글2", "한글3"],
                }
            ],
            "RadarVerify": [RadarVerify(supported=True)],
        }
    )
    app = create_app(
        settings=settings, store=store, llm=llm, adapters={"github": gh, "notifier": None}
    )
    client = TestClient(app)
    client.headers["X-API-Key"] = "serverless-test-key"
    r = client.post("/api/v1/radar/refresh", json={"platforms": ["claude"], "force": True})
    assert r.status_code == 202
    run_id = r.json()["run_ids"][0]
    # TestClient는 BackgroundTasks 완료까지 응답을 기다린다
    d = client.get(f"/api/v1/runs/{run_id}").json()
    assert d["status"] == "waiting_approval"
    assert [s["stage_key"] for s in d["stages"]] == [
        "collect",
        "dedupe",
        "summarize",
        "verify",
        "rank",
        "approve",
    ]
