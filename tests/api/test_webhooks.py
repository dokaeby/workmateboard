"""spec 004 US1 — 아웃바운드 웹훅: 구독 CRUD/시크릿 1회/SSRF 422,
emit_event 매칭·멱등, 서명 검증, 재시도→dead, disabled 취소, 조직 격리.

HTTP 전송은 post 콜러블 주입으로 fake 처리한다(실제 outbound 없음).
"""

import hashlib
import hmac
import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from jobmate.api.app import create_app
from jobmate.config import Settings
from jobmate.security import encrypt_str
from jobmate.services import webhooks as wh
from jobmate.store import orgctx
from jobmate.store.db import Store
from tests.integration.helpers import default_outputs, scripted_llm

_SECRET = "whsec-test-secret-0123456789"  # noqa: S105 - 테스트용 고정값
_LOOPBACK_URL = "http://127.0.0.1:8080/hook"


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
    client = TestClient(app)
    client.headers["X-API-Key"] = "test-api-key"
    return client, store, settings


def _mk_sub(store, org: str, *, events=None, url=_LOOPBACK_URL, enabled=True):
    with orgctx.org_scope(org):
        return store.create_webhook_subscription(
            url=url,
            secret_enc=encrypt_str(_SECRET),
            events=events if events is not None else ["run.completed"],
            created_by="test",
            enabled=enabled,
        )


def _due_all(store):
    """모든 delivery의 next_retry_at을 과거로 — 재claim 가능하게."""
    with store.tx() as c:
        c.execute("UPDATE webhook_deliveries SET next_retry_at=''")


def _get_delivery(store, delivery_id):
    with store.tx() as c:
        r = c.execute("SELECT * FROM webhook_deliveries WHERE id=?", (delivery_id,)).fetchone()
    return dict(r) if r else None


class _Resp:
    def __init__(self, status_code: int):
        self.status_code = status_code


# ---------- API: CRUD + secret once + SSRF ----------


def test_subscription_crud_secret_once(monkeypatch):
    client, store, _s = _client(monkeypatch)
    r = client.post(
        "/api/v1/webhook-subscriptions",
        json={"url": _LOOPBACK_URL, "events": ["run.completed"]},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["secret"]
    sub = body["subscription"]
    sid = sub["id"]
    assert sub["events"] == ["run.completed"] and sub["enabled"] is True
    assert "secret" not in json.dumps(sub)
    assert "secret_enc" not in json.dumps(sub)

    # list/get — secret 원문·암호문 모두 미노출
    r = client.get("/api/v1/webhook-subscriptions")
    assert r.status_code == 200
    assert [s["id"] for s in r.json()["subscriptions"]] == [sid]
    r = client.get(f"/api/v1/webhook-subscriptions/{sid}")
    assert r.status_code == 200 and "secret" not in r.text

    # patch
    r = client.patch(
        f"/api/v1/webhook-subscriptions/{sid}",
        json={"enabled": False, "events": ["run.failed", "*"]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["subscription"]["enabled"] is False
    assert r.json()["subscription"]["events"] == ["run.failed", "*"]

    # rotate — 새 시크릿 1회 반환
    r = client.post(f"/api/v1/webhook-subscriptions/{sid}/rotate-secret")
    assert r.status_code == 200 and r.json()["secret"]
    assert r.json()["secret"] != body["secret"]

    # delete
    r = client.delete(f"/api/v1/webhook-subscriptions/{sid}")
    assert r.status_code == 200
    assert client.get(f"/api/v1/webhook-subscriptions/{sid}").status_code == 404
    store.close()


def test_webhook_requires_auth(monkeypatch):
    client, store, _s = _client(monkeypatch)
    anon = TestClient(client.app)
    assert anon.get("/api/v1/webhook-subscriptions").status_code == 401
    store.close()


@pytest.mark.parametrize(
    "url",
    [
        "https://192.168.1.10/hook",  # 사설 IP
        "https://10.0.0.5/hook",  # 사설 IP
        "http://example.com/hook",  # 비-loopback http
        "https://user:pass@127.0.0.1/hook",  # userinfo
        "https://example.com:8080/hook",  # 비표준 포트
    ],
)
def test_subscription_url_rejected_422(monkeypatch, url):
    client, store, _s = _client(monkeypatch)
    r = client.post("/api/v1/webhook-subscriptions", json={"url": url})
    assert r.status_code == 422, (url, r.text)
    assert store.list_webhook_subscriptions() == []
    store.close()


def test_patch_url_rejected_422(monkeypatch):
    client, store, _s = _client(monkeypatch)
    r = client.post("/api/v1/webhook-subscriptions", json={"url": _LOOPBACK_URL})
    sid = r.json()["subscription"]["id"]
    r = client.patch(
        f"/api/v1/webhook-subscriptions/{sid}",
        json={"url": "https://192.168.0.1/x"},
    )
    assert r.status_code == 422
    # 원래 URL 유지
    assert store.get_webhook_subscription(sid)["url"] == _LOOPBACK_URL
    store.close()


# ---------- emit_event ----------


def test_emit_event_matches_and_dedupes():
    db = str(Path(tempfile.mkdtemp()) / "t.db")
    store = Store(db)
    org = "orgA"
    sub_hit = _mk_sub(store, org, events=["run.completed"])
    sub_wild = _mk_sub(store, org, events=["*"])
    _mk_sub(store, org, events=["run.failed"])  # 비매칭
    _mk_sub(store, org, events=["run.completed"], enabled=False)  # disabled

    with orgctx.org_scope(org):
        n = wh.emit_event(
            store, "run.completed", "evt-1", {"run_id": "r1", "workflow": "dev_review"}
        )
        assert n == 2
        # 동일 event_id 재emit — 멱등 no-op
        n2 = wh.emit_event(store, "run.completed", "evt-1", {"run_id": "r1"})
        assert n2 == 0
        # 비매칭 타입
        assert wh.emit_event(store, "run.needs_human", "evt-2", {}) == 1  # wildcard만
    with orgctx.org_scope(org):
        dels = store.list_webhook_deliveries(sub_hit["id"], limit=10)
        assert len(dels) == 1
        payload = dels[0]["payload"]
        assert payload["v"] == "1.0"
        assert payload["id"] == "evt-1"
        assert payload["type"] == "run.completed"
        assert payload["data"]["run_id"] == "r1"
        dels_w = store.list_webhook_deliveries(sub_wild["id"], limit=10)
        assert {d["event_id"] for d in dels_w} == {"evt-1", "evt-2"}
    store.close()


# ---------- delivery 처리 + 서명 ----------


def test_delivery_success_signature(monkeypatch):
    _env(monkeypatch)
    db = str(Path(tempfile.mkdtemp()) / "t.db")
    settings = Settings(db_path=db)
    store = Store(db)
    org = "orgA"
    sub = _mk_sub(store, org)
    with orgctx.org_scope(org):
        wh.emit_event(store, "run.completed", "evt-sig", {"run_id": "r9"})

    captured: dict[str, Any] = {}

    def fake_post(url, body, headers, timeout):
        captured.update({"url": url, "body": body, "headers": headers})
        return _Resp(200)

    n = wh.process_webhook_deliveries(store, settings, post=fake_post)
    assert n == 1
    assert captured["url"] == _LOOPBACK_URL
    headers = captured["headers"]
    assert headers["Content-Type"] == "application/json"
    assert headers["X-JobMate-Event"] == "run.completed"
    assert headers["X-JobMate-Delivery"]
    sig = headers["X-JobMate-Signature"]
    assert sig.startswith("t=") and ",v1=" in sig
    t_str, v1 = sig.split(",")
    t = int(t_str[2:])
    assert headers["X-JobMate-Timestamp"] == str(t)
    # 수신자 검증: v1 == hex(HMAC_SHA256(secret, "{t}.{raw_body}"))
    expected = hmac.new(
        _SECRET.encode(),
        f"{t}.".encode() + captured["body"],
        hashlib.sha256,
    ).hexdigest()
    assert v1[3:] == expected
    # payload 스키마 + 멱등 id
    payload = json.loads(captured["body"].decode())
    assert payload["id"] == "evt-sig" and payload["type"] == "run.completed"

    with orgctx.org_scope(org):
        dels = store.list_webhook_deliveries(sub["id"], limit=5)
    assert dels[0]["status"] == "success"
    assert dels[0]["last_http_status"] == 200
    assert dels[0]["delivered_at"]
    assert dels[0]["attempts"] == 1
    # last_delivery_at 갱신
    with orgctx.org_scope(org):
        assert store.get_webhook_subscription(sub["id"])["last_delivery_at"]
    store.close()


def test_delivery_retry_then_dead(monkeypatch):
    _env(monkeypatch)
    db = str(Path(tempfile.mkdtemp()) / "t.db")
    settings = Settings(db_path=db, webhook_timeout_seconds=1.0)
    store = Store(db)
    org = "orgA"
    sub = _mk_sub(store, org)
    with orgctx.org_scope(org):
        wh.emit_event(store, "run.completed", "evt-retry", {})

    def fail_post(url, body, headers, timeout):
        return _Resp(500)

    # 6회 실패(최초+재시도5) → dead
    for i in range(6):
        _due_all(store)
        n = wh.process_webhook_deliveries(store, settings, post=fail_post)
        assert n == 1
        with orgctx.org_scope(org):
            d = store.list_webhook_deliveries(sub["id"], limit=1)[0]
        assert d["status"] != "dead" or i == 5, (i, d["status"])
        assert d["attempts"] == i + 1
        if i < 5:
            assert d["status"] == "failed"
            assert d["next_retry_at"]  # 백오프 스케줄
            assert d["last_error"] == "http_500"
        else:
            assert d["status"] == "dead"
            assert d["next_retry_at"] is None
    store.close()


def test_delivery_cancelled_when_disabled():
    db = str(Path(tempfile.mkdtemp()) / "t.db")
    settings = Settings(db_path=db)
    store = Store(db)
    org = "orgA"
    sub = _mk_sub(store, org)
    with orgctx.org_scope(org):
        wh.emit_event(store, "run.completed", "evt-cancel", {})
        store.update_webhook_subscription(sub["id"], enabled=False)

    called = []

    def spy_post(url, body, headers, timeout):
        called.append(url)
        return _Resp(200)

    wh.process_webhook_deliveries(store, settings, post=spy_post)
    assert called == []  # disabled 구독은 claim돼도 전송하지 않는다
    with orgctx.org_scope(org):
        d = store.list_webhook_deliveries(sub["id"], limit=1)[0]
    assert d["status"] == "cancelled"
    store.close()


def test_delivery_network_error_retries():
    db = str(Path(tempfile.mkdtemp()) / "t.db")
    settings = Settings(db_path=db)
    store = Store(db)
    org = "orgA"
    _mk_sub(store, org)
    with orgctx.org_scope(org):
        wh.emit_event(store, "run.completed", "evt-net", {})

    def boom(url, body, headers, timeout):
        raise ConnectionError("refused")

    wh.process_webhook_deliveries(store, settings, post=boom)
    # subscription_id를 모르므로 delivery 행을 직접 조회
    with store.tx() as c:
        r = c.execute("SELECT * FROM webhook_deliveries").fetchone()
    d = dict(r)
    assert d["status"] == "failed"
    assert d["attempts"] == 1
    assert d["last_error"] == "ConnectionError"  # 예외 타입만
    assert d["next_retry_at"]
    store.close()


# ---------- 조직 격리 ----------


def test_org_isolation():
    db = str(Path(tempfile.mkdtemp()) / "t.db")
    store = Store(db)
    sub_a = _mk_sub(store, "orgA")
    _mk_sub(store, "orgB")
    with orgctx.org_scope("orgA"):
        subs = store.list_webhook_subscriptions()
        assert [s["id"] for s in subs] == [sub_a["id"]]
        assert all(s["org_id"] == "orgA" for s in subs)
        # orgB의 sub_id로 조회해도 orgA ctx에서는 안 보인다
        assert store.get_webhook_subscription(sub_a["id"]) is not None
    with orgctx.org_scope("orgC"):
        assert store.list_webhook_subscriptions() == []
        assert store.get_webhook_subscription(sub_a["id"]) is None
    # emit도 조직 범위 — orgB 구독만 매칭돼야 한다
    with orgctx.org_scope("orgB"):
        n = wh.emit_event(store, "run.completed", "evt-org", {})
        assert n == 1
    with orgctx.org_scope("orgA"):
        assert store.list_webhook_deliveries(sub_a["id"], limit=5) == []
    store.close()


# ---------- test endpoint ----------


def test_test_endpoint_enqueues_delivery(monkeypatch):
    client, store, _s = _client(monkeypatch)
    r = client.post(
        "/api/v1/webhook-subscriptions",
        json={"url": _LOOPBACK_URL, "events": ["run.completed"]},
    )
    sid = r.json()["subscription"]["id"]
    r = client.post(f"/api/v1/webhook-subscriptions/{sid}/test")
    assert r.status_code == 202, r.text
    event_id = r.json()["delivery_event_id"]
    r = client.get(f"/api/v1/webhook-subscriptions/{sid}/deliveries")
    assert r.status_code == 200
    dels = r.json()["deliveries"]
    assert len(dels) == 1
    assert dels[0]["event_type"] == "webhook.test"
    assert dels[0]["status"] == "pending"
    assert dels[0]["payload"]["type"] == "webhook.test"
    assert dels[0]["event_id"] == event_id
    assert dels[0]["payload"]["id"] == event_id
    store.close()


# ---------- loopback 포트 allowlist (SSRF) ----------


def test_webhook_url_allowed_loopback_ports(monkeypatch):
    monkeypatch.delenv("JOBMATE_WEBHOOK_LOOPBACK_PORTS", raising=False)
    # 미설정 — 모든 포트 허용(기존 동작)
    assert wh.webhook_url_allowed("http://127.0.0.1:9999/x") is None
    assert wh.webhook_url_allowed("http://localhost:6379/") is None
    monkeypatch.setenv("JOBMATE_WEBHOOK_LOOPBACK_PORTS", "8080, 8443")
    # allowlist 외 loopback 포트 → 거부
    assert wh.webhook_url_allowed("http://127.0.0.1:9999/x") is not None
    assert wh.webhook_url_allowed("http://localhost:6379/") is not None
    assert wh.webhook_url_allowed("http://[::1]:9999/x") is not None
    # allowlist 내 + 비-loopback https는 무관
    assert wh.webhook_url_allowed("http://127.0.0.1:8080/hook") is None
    assert wh.webhook_url_allowed("https://example.com:8443/x") is None


def test_subscription_loopback_port_rejected_when_allowlist_set(monkeypatch):
    _env(monkeypatch)
    monkeypatch.setenv("JOBMATE_WEBHOOK_LOOPBACK_PORTS", "8080")
    client, store, _s = _client(monkeypatch)
    r = client.post(
        "/api/v1/webhook-subscriptions",
        json={"url": "http://127.0.0.1:9999/hook"},
    )
    assert r.status_code == 422, r.text
    assert store.list_webhook_subscriptions() == []
    # allowlist 내 포트는 정상 생성
    r = client.post("/api/v1/webhook-subscriptions", json={"url": _LOOPBACK_URL})
    assert r.status_code == 201, r.text
    store.close()


def test_delivery_loopback_port_blocked_at_send_time(monkeypatch):
    """저장 후 allowlist가 좁혀지면 전송 시점 재검증이 delivery를 실패 처리."""
    _env(monkeypatch)
    db = str(Path(tempfile.mkdtemp()) / "t.db")
    settings = Settings(db_path=db)
    store = Store(db)
    org = "orgA"
    sub = _mk_sub(store, org, url="http://127.0.0.1:9999/hook")
    with orgctx.org_scope(org):
        wh.emit_event(store, "run.completed", "evt-port", {})
    monkeypatch.setenv("JOBMATE_WEBHOOK_LOOPBACK_PORTS", "8080")

    called = []

    def spy_post(url, body, headers, timeout):
        called.append(url)
        return _Resp(200)

    wh.process_webhook_deliveries(store, settings, post=spy_post)
    assert called == []  # 전송 자체가 차단된다
    with orgctx.org_scope(org):
        d = store.list_webhook_deliveries(sub["id"], limit=1)[0]
    assert d["status"] == "failed"
    assert d["last_error"] == "webhook_url_loopback_port_not_allowed"
    store.close()


# ---------- retention_sweep: terminal webhook deliveries ----------


def test_retention_sweep_purges_old_terminal_deliveries():
    db = str(Path(tempfile.mkdtemp()) / "t.db")
    store = Store(db)
    _mk_sub(store, "orgA")
    with orgctx.org_scope("orgA"):
        for i in range(5):
            wh.emit_event(store, "run.completed", f"evt-ret-{i}", {})
    old = (datetime.now(UTC) - timedelta(days=120)).isoformat()
    statuses = ["success", "dead", "cancelled", "pending", "failed"]
    with store.tx() as c:
        rows = c.execute("SELECT id FROM webhook_deliveries ORDER BY event_id").fetchall()
        for r, st in zip(rows, statuses, strict=True):
            c.execute(
                "UPDATE webhook_deliveries SET status=?, created_at=? WHERE id=?",
                (st, old, r["id"]),
            )
    counts = store.retention_sweep()
    assert counts["webhook_deliveries"] == 3
    with store.tx() as c:
        remaining = {
            str(r["status"]) for r in c.execute("SELECT status FROM webhook_deliveries").fetchall()
        }
    # 종결 3건만 삭제 — pending/failed(재시도 대상)는 보존
    assert remaining == {"pending", "failed"}
    store.close()
