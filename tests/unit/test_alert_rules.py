"""spec 006 US1 — 알림 규칙 엔진 단위 테스트.

condition 매칭, 채널 dispatch 결과, (rule_id, fingerprint) 멱등,
cooldown 억제, 미구성 채널 skipped_unconfigured를 검증한다.
외부 발송(SMTP/Slack/Telegram/Push)은 전부 미구성 환경이라
skipped_unconfigured로 떨어지는지 확인한다.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from jobmate.services import alert_rules as svc
from jobmate.store.db import Store


def _store() -> Store:
    return Store(str(Path(tempfile.mkdtemp()) / "t.db"))


@pytest.fixture(autouse=True)
def _clear_org():
    from jobmate.store import orgctx

    with orgctx.org_scope(""):
        yield


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


# ---- condition 매칭 ----


@pytest.mark.parametrize(
    ("cond", "fields", "expected"),
    [
        (
            {"field": "run.status", "op": "eq", "value": "failed"},
            {"run.status": "failed"},
            True,
        ),
        (
            {"field": "run.status", "op": "eq", "value": "failed"},
            {"run.status": "completed"},
            False,
        ),
        (
            {"field": "run.status", "op": "ne", "value": "failed"},
            {"run.status": "completed"},
            True,
        ),
        ({"field": "run.cost_usd", "op": "gt", "value": 1.5}, {"run.cost_usd": 2.0}, True),
        ({"field": "run.cost_usd", "op": "gte", "value": 2.0}, {"run.cost_usd": 2.0}, True),
        ({"field": "run.cost_usd", "op": "lt", "value": 1.0}, {"run.cost_usd": 2.0}, False),
        ({"field": "run.cost_usd", "op": "lte", "value": 2.0}, {"run.cost_usd": 2.0}, True),
        (
            {"field": "run.status", "op": "in", "value": ["failed", "cancelled"]},
            {"run.status": "cancelled"},
            True,
        ),
        (
            {"field": "run.workflow", "op": "contains", "value": "review"},
            {"run.workflow": "dev_review"},
            True,
        ),
        ({"field": "run.missing", "op": "eq", "value": "x"}, {"run.status": "failed"}, False),
    ],
)
def test_condition_matches(cond, fields, expected):
    assert svc.condition_matches(cond, fields) is expected


def test_validate_condition_rejects_bad():
    with pytest.raises(ValueError):
        svc.validate_condition({"field": "run.status", "op": "bogus", "value": "x"})
    with pytest.raises(ValueError):
        svc.validate_condition({"field": "run.status"})
    ok = svc.validate_condition({"field": "run.status", "op": "eq", "value": "failed"})
    assert ok["field"] == "run.status"


def test_validate_channels_normalizes():
    chans = svc.validate_channels(
        ["inbox", {"channel": "email", "to": "a@b.c"}, {"channel": "slack"}]
    )
    names = {c if isinstance(c, str) else c["type"] for c in chans}
    assert names == {"inbox", "email", "slack"}
    with pytest.raises(ValueError):
        svc.validate_channels([{"channel": "pigeon"}])


# ---- emit / dedup / cooldown ----


def test_emit_fires_inbox_and_records_fire():
    s = _store()
    run = _mk_run(s, "failed")
    _mk_rule(s, {"field": "run.status", "op": "eq", "value": "failed"})
    n = svc.emit_run_status_event(s, run.id)
    assert n == 1
    inbox = s.list_inbox(limit=10)
    assert any(i.get("kind") == "alert" for i in inbox)
    fires = s.list_alert_fires()
    assert len(fires) == 1 and fires[0]["status"] == "fired"


def test_emit_dedup_same_fingerprint():
    s = _store()
    run = _mk_run(s, "failed")
    _mk_rule(s, {"field": "run.status", "op": "eq", "value": "failed"})
    assert svc.emit_run_status_event(s, run.id) == 1
    # 같은 상태/updated_at 재평가 → fingerprint 동일 → 재발송 없음
    assert svc.emit_run_status_event(s, run.id) == 0
    assert len(s.list_alert_fires()) == 1


def test_emit_nonmatching_rule_skipped():
    s = _store()
    run = _mk_run(s, "completed")
    _mk_rule(s, {"field": "run.status", "op": "eq", "value": "failed"})
    assert svc.emit_run_status_event(s, run.id) == 0
    assert s.list_alert_fires() == []


def test_cooldown_suppresses_repeat():
    s = _store()
    run = _mk_run(s, "failed")
    rule = _mk_rule(
        s,
        {"field": "run.status", "op": "eq", "value": "failed"},
        cooldown_minutes=60,
    )
    assert svc.emit_run_status_event(s, run.id) == 1
    # 상태가 바뀌면 fingerprint가 달라지지만 cooldown 내라 억제된다
    from jobmate.models import RunStatus

    s.update_run(run.id, status=RunStatus.NEEDS_HUMAN)
    rule2 = s.get_alert_rule(rule["id"])
    assert rule2["last_fired_at"]
    s.update_run(run.id, status=RunStatus.FAILED)
    n = svc.emit_run_status_event(s, run.id)
    assert n == 0
    statuses = {f["status"] for f in s.list_alert_fires()}
    assert "suppressed_cooldown" in statuses


def test_unconfigured_channels_report_skipped():
    s = _store()
    run = _mk_run(s, "failed")
    _mk_rule(
        s,
        {"field": "run.status", "op": "eq", "value": "failed"},
        channels=[{"channel": "email", "to": "x@y.z"}, {"channel": "slack"}],
    )
    svc.emit_run_status_event(s, run.id)
    fire = s.list_alert_fires()[0]
    results = {c["channel"]: c["status"] for c in fire["channels"]}
    assert results["email"] == "skipped_unconfigured"
    assert results["slack"] == "skipped_unconfigured"


def test_disabled_rule_not_fired():
    s = _store()
    run = _mk_run(s, "failed")
    _mk_rule(s, {"field": "run.status", "op": "eq", "value": "failed"}, enabled=False)
    assert svc.emit_run_status_event(s, run.id) == 0
    assert s.list_alert_fires() == []


def test_org_scoped_rules():
    """다른 org의 규칙은 현재 org 컨텍스트에서 평가되지 않는다.

    이벤트 org와 규칙 org가 일치해야 발화한다 — 플랫폼 run(org='')은
    org-b 규칙을, org-b run은 플랫폼 규칙을 건드리지 않는다."""
    from jobmate.models import RunStatus
    from jobmate.store import orgctx

    s = _store()
    oid = s.create_organization("Org B", "org-b")
    assert oid
    run = _mk_run(s, "failed")  # org=''
    with orgctx.org_scope(oid):
        _mk_rule(s, {"field": "run.status", "op": "eq", "value": "failed"})
        run_b = s.create_run("dev_review", "1", {"task": "t"}, trace_id="b", org_id=oid)
        s.update_run(run_b.id, status=RunStatus.FAILED)
    with orgctx.org_scope(""):
        # 플랫폼 run → org-b 규칙 미발화
        assert svc.emit_run_status_event(s, run.id) == 0
    with orgctx.org_scope(oid):
        # org-b run → org-b 규칙 발화 (플랫폼 규칙은 없음)
        assert svc.emit_run_status_event(s, run_b.id) == 1


# ---- budget 임계 이벤트 ----


def test_budget_threshold_event_fields(monkeypatch):
    """budget.used_pct 조건 규칙이 임계 이벤트에서 발화한다."""
    s = _store()
    _mk_rule(
        s,
        {"field": "budget.used_pct", "op": "gte", "value": 80},
        name="budget-warn",
    )
    # emit_budget_threshold_event는 async — emit_alert_event 직접 호출로
    # 동일 경로(필드/지문/멱등)를 동기 검증한다.
    n = svc.emit_alert_event(
        s,
        event_fingerprint="budget:p1:2026-01:warn",
        fields={
            "budget.used_pct": 95.0,
            "budget.scope": "global:",
            "budget.threshold": "warn",
            "budget.policy_id": "p1",
        },
        org_id="",
    )
    assert n == 1
    # 같은 지문 재평가 → dedup
    n2 = svc.emit_alert_event(
        s,
        event_fingerprint="budget:p1:2026-01:warn",
        fields={"budget.used_pct": 99.0},
        org_id="",
    )
    assert n2 == 0


# ---- API CRUD (FR-006) ----


def test_api_alert_rule_crud(monkeypatch):
    import tempfile as _tf
    from pathlib import Path as _P

    from fastapi.testclient import TestClient

    from jobmate.api.app import create_app
    from jobmate.config import Settings

    db = str(_P(_tf.mkdtemp()) / "t.db")
    s = Store(db)
    app = create_app(settings=Settings(db_path=db), store=s, llm=None)
    client = TestClient(app)
    client.headers["X-API-Key"] = "alert-test-key"

    # 생성 — 유효 condition/channels
    r = client.post(
        "/api/v1/alert-rules",
        json={
            "name": "run failed",
            "condition": {"field": "run.status", "op": "eq", "value": "failed"},
            "channels": ["inbox"],
            "cooldown_minutes": 30,
        },
    )
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    assert r.json()["condition"]["field"] == "run.status"

    # 잘못된 condition → 422
    r = client.post(
        "/api/v1/alert-rules",
        json={"name": "bad", "condition": {"field": "x", "op": "bogus"}, "channels": []},
    )
    assert r.status_code == 422

    # 목록/조회
    assert any(i["id"] == rid for i in client.get("/api/v1/alert-rules").json()["items"])
    assert client.get(f"/api/v1/alert-rules/{rid}").json()["name"] == "run failed"

    # PATCH — enabled 토글
    r = client.patch(f"/api/v1/alert-rules/{rid}", json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False

    # fires 목록 (빈 상태도 200)
    assert client.get(f"/api/v1/alert-rules/{rid}/fires").status_code == 200

    # DELETE
    assert client.delete(f"/api/v1/alert-rules/{rid}").status_code == 200
    assert client.get(f"/api/v1/alert-rules/{rid}").status_code == 404


# ---- 실제 트리거 경로 / 채널 / 인가 ----


def test_alert_rule_sweep_fires_for_failed_run(monkeypatch):
    """실제 백스톱 경로 — api_jobs._alert_rule_sweep이 failed run을 스캔해
    inbox 규칙을 발화한다 (직접 emit이 놓친 경로의 결정적 검증)."""
    from jobmate.api import jobs as api_jobs

    s = _store()
    _mk_run(s, "failed")
    _mk_rule(s, {"field": "run.status", "op": "eq", "value": "failed"})
    # sweep은 emit_*_async(daemon thread)를 쓰므로 동기 실행으로 바꿔
    # 결정적으로 만든다 — emit_run_status_event 자체는 실제 경로 그대로.
    monkeypatch.setattr(
        svc,
        "emit_run_status_event_async",
        lambda store, run_id: svc.emit_run_status_event(store, run_id),
    )
    api_jobs._alert_rule_sweep(s)
    fires = s.list_alert_fires()
    assert len(fires) == 1 and fires[0]["status"] == "fired"
    assert any(i.get("kind") == "alert" for i in s.list_inbox(limit=10))


def test_configured_slack_channel_reports_sent(monkeypatch):
    """webhook env + messenger.send_slack 모킹 — fire 채널 결과가 sent."""
    import jobmate.messenger as messenger

    s = _store()
    run = _mk_run(s, "failed")
    sent: list[str] = []
    monkeypatch.setenv("JOBMATE_SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/T/B/X")
    monkeypatch.setattr(messenger, "send_slack", lambda url, text: sent.append(text) or True)
    _mk_rule(
        s,
        {"field": "run.status", "op": "eq", "value": "failed"},
        channels=["slack"],
    )
    assert svc.emit_run_status_event(s, run.id) == 1
    assert sent, "slack send_slack was not called"
    fire = s.list_alert_fires()[0]
    results = {c["channel"]: c["status"] for c in fire["channels"]}
    assert results["slack"] == "sent"
    assert fire["status"] == "fired"


# ---- API 인가/조직 격리/감사 ----

_PW = "Str0ng#Passw0rd!"


def _api_env(tmp_path):
    from fastapi.testclient import TestClient

    from jobmate.api.app import create_app
    from jobmate.config import Settings

    db = str(tmp_path / "api.db")
    s = Store(db)
    app = create_app(settings=Settings(db_path=db), store=s, llm=None)
    return TestClient(app), s


def _member_session(store, client, email="member@t.com"):
    """조직 없는 member 세션 — lead 게이트에서 403이어야 한다."""
    from jobmate.security import (
        SESSION_COOKIE,
        hash_password,
        hash_token,
        new_session_token,
    )

    u = store.get_user_by_email(email) or store.create_user(
        email, hash_password(_PW), email, email_verified=True
    )
    assert u is not None
    store.set_user_role(u["id"], "member")
    token = new_session_token()
    store.create_session(u["id"], hash_token(token))
    client.cookies.set(SESSION_COOKIE, token)


def _rule_body(**kw):
    body = {
        "name": "r",
        "condition": {"field": "run.status", "op": "eq", "value": "failed"},
        "channels": ["inbox"],
    }
    body.update(kw)
    return body


def test_member_cannot_create_alert_rule_but_can_list(tmp_path):
    client, store = _api_env(tmp_path)
    client.headers["X-API-Key"] = "alert-test-key"
    # lead(api key)로 규칙 1개 생성
    r = client.post("/api/v1/alert-rules", json=_rule_body(name="seed"))
    assert r.status_code == 201, r.text

    _member_session(store, client)
    r = client.post("/api/v1/alert-rules", json=_rule_body(name="nope"))
    assert r.status_code == 403
    # GET은 member도 허용(조회는 인증만 필요)
    assert client.get("/api/v1/alert-rules").status_code == 200


def test_alert_rule_isolated_between_orgs(tmp_path):
    """조직 A에서 만든 규칙은 조직 B 세션 목록/상세에 노출되지 않는다."""
    client, store = _api_env(tmp_path)

    def _signup(email):
        r = client.post(
            "/api/v1/auth/signup",
            json={"email": email, "password": _PW, "agree_terms": True},
        )
        assert r.status_code == 201, r.text
        with store.tx() as c:
            c.execute("UPDATE users SET email_verified=1 WHERE email=?", (email,))

    def _login(email):
        r = client.post("/api/v1/auth/login", json={"email": email, "password": _PW})
        assert r.status_code == 200, r.text

    _signup("orgA@t.com")
    _signup("orgB@t.com")

    _login("orgA@t.com")  # 개인 조직 org_owner → lead
    r = client.post("/api/v1/alert-rules", json=_rule_body(name="orgA-rule"))
    assert r.status_code == 201, r.text
    rid = r.json()["id"]

    _login("orgB@t.com")
    items = client.get("/api/v1/alert-rules").json()["items"]
    assert all(i["id"] != rid for i in items)
    assert client.get(f"/api/v1/alert-rules/{rid}").status_code == 404


def test_alert_rule_create_audit_has_no_channel_secrets(tmp_path):
    """alert_rule_create 감사 detail에 채널 수신자 등 비밀/개인값이 없다."""
    import json as _json

    client, store = _api_env(tmp_path)
    client.headers["X-API-Key"] = "alert-test-key"
    hidden_to = "someone-private@example.com"
    r = client.post(
        "/api/v1/alert-rules",
        json=_rule_body(
            name="audited",
            channels=[{"channel": "email", "to": hidden_to}],
        ),
    )
    assert r.status_code == 201, r.text

    rows = store.list_audit(include_platform=True)
    creates = [a for a in rows if a["action"] == "alert_rule_create"]
    assert creates, "alert_rule_create audit event missing"
    for a in creates:
        blob = _json.dumps(a.get("detail") or {}, ensure_ascii=False)
        assert hidden_to not in blob
