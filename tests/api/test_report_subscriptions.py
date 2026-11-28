"""spec 008 US3 — 리포트 구독 CRUD/검증/시크릿 1회/테스트 발송/조직 격리.

HTTP 채널 발송은 post 콜러블·messenger/mailer monkeypatch로 fake 처리한다
(실제 outbound 없음). 시크릿은 JOBMATE_API_KEY를 키 재료로 쓰는 Fernet을
통과하므로 encrypt/decrypt가 테스트 환경에서도 동작한다.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from jobmate import mailer, messenger
from jobmate.api.app import create_app
from jobmate.config import Settings
from jobmate.security import SESSION_COOKIE, hash_password, hash_token, new_session_token
from jobmate.services import reporting as reporting_svc
from jobmate.services import webhooks as webhooks_svc
from jobmate.store import orgctx
from jobmate.store.db import Store
from tests.integration.helpers import default_outputs, scripted_llm

_LOOPBACK = "http://127.0.0.1:8080/hook"


def _env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("JOBMATE_API_KEY", "test-api-key")
    for v in (
        "JOBMATE_ALLOWED_EMAILS",
        "VERCEL",
        "JOBMATE_MODE",
        "JOBMATE_ENCRYPTION_KEY",
        "JOBMATE_PUBLIC_HOSTS",
        "JOBMATE_ADMIN_EMAILS",
        "JOBMATE_LEAD_EMAILS",
        "JOBMATE_WEBHOOK_LOOPBACK_PORTS",
    ):
        monkeypatch.delenv(v, raising=False)


def _client(monkeypatch: pytest.MonkeyPatch):
    _env(monkeypatch)
    db = str(Path(tempfile.mkdtemp()) / "t.db")
    settings = Settings(db_path=db, budget_max_tokens=100_000)
    store = Store(db)
    llm = scripted_llm(default_outputs())
    app = create_app(settings=settings, store=store, llm=llm, adapters={})
    client = TestClient(app)
    client.headers["X-API-Key"] = "test-api-key"
    return client, store, settings


def _login(store: Store, client: TestClient, email: str, role: str = "member") -> None:
    u = store.get_user_by_email(email) or store.create_user(
        email, hash_password("Str0ng#Passw0rd!"), email
    )
    assert u is not None
    store.set_user_role(u["id"], role)
    token = new_session_token()
    store.create_session(u["id"], hash_token(token))
    client.headers.pop("X-API-Key", None)
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE, token)


def _report() -> dict[str, Any]:
    return {
        "kind": "daily",
        "generated_at": "2026-01-01T00:00:00+00:00",
        "runs": {"total": 3, "failed": 1},
        "approvals": {"pending": 2},
        "llm": {"total_cost_usd": 0.5},
    }


# ---------------- API: CRUD + 검증 + 시크릿 ----------------


def test_subscription_crud_secret_once(monkeypatch: pytest.MonkeyPatch) -> None:
    client, store, _ = _client(monkeypatch)
    # email 구독
    r = client.post(
        "/api/v1/reports/subscriptions",
        json={"kind": "daily", "channel": "email", "target": "ops@example.com"},
    )
    assert r.status_code == 201, r.text
    item = r.json()["item"]
    assert item["kind"] == "daily" and item["channel"] == "email"
    assert "secret" not in r.json()
    sid = item["id"]

    # webhook 구독 — 시크릿 1회 반환
    r = client.post(
        "/api/v1/reports/subscriptions",
        json={"kind": "weekly", "channel": "webhook", "target": _LOOPBACK},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["secret"]
    wid = body["item"]["id"]

    # list — secret 원문·암호문 모두 미노출
    r = client.get("/api/v1/reports/subscriptions")
    assert r.status_code == 200
    assert "secret" not in r.text and "secret_enc" not in r.text
    items = r.json()["items"]
    assert {i["id"] for i in items} == {sid, wid}

    # PATCH — 비활성화
    r = client.patch(f"/api/v1/reports/subscriptions/{sid}", json={"enabled": False})
    assert r.status_code == 200 and r.json()["item"]["enabled"] is False

    # DELETE
    assert client.delete(f"/api/v1/reports/subscriptions/{sid}").status_code == 200
    assert client.delete(f"/api/v1/reports/subscriptions/{sid}").status_code == 404
    store.close()


def test_subscription_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    client, store, _ = _client(monkeypatch)
    base = {"kind": "daily", "channel": "email", "target": "ops@example.com"}
    # kind/channel enum
    assert (
        client.post(
            "/api/v1/reports/subscriptions",
            json={**base, "kind": "exec_daily"},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/reports/subscriptions",
            json={**base, "channel": "sms"},
        ).status_code
        == 422
    )
    # 빈 target → pydantic 422
    assert (
        client.post(
            "/api/v1/reports/subscriptions",
            json={"kind": "daily", "channel": "email", "target": ""},
        ).status_code
        == 422
    )
    # 잘못된 이메일
    r = client.post(
        "/api/v1/reports/subscriptions",
        json={**base, "target": "not-an-email"},
    )
    assert r.status_code == 422
    # SSRF — 비-loopback http / 사설 IP 거부
    for bad in ("http://169.254.169.254/latest", "http://192.168.0.1/hook"):
        r = client.post(
            "/api/v1/reports/subscriptions",
            json={**base, "channel": "webhook", "target": bad},
        )
        assert r.status_code == 422, bad
    # PATCH도 동일 검증
    sid = client.post("/api/v1/reports/subscriptions", json=base).json()["item"]["id"]
    r = client.patch(
        f"/api/v1/reports/subscriptions/{sid}",
        json={"target": "http://169.254.169.254/x"},
    )
    assert r.status_code == 422
    store.close()


def test_subscription_role_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    client, store, _ = _client(monkeypatch)
    _login(store, client, "m@x.com", "member")
    body = {"kind": "daily", "channel": "email", "target": "ops@example.com"}
    # target은 webhook/slack 자격증명 — member는 목록 조회도 403
    assert client.get("/api/v1/reports/subscriptions").status_code == 403
    assert client.post("/api/v1/reports/subscriptions", json=body).status_code == 403
    assert (
        client.patch("/api/v1/reports/subscriptions/x", json={"enabled": False}).status_code == 403
    )
    assert client.delete("/api/v1/reports/subscriptions/x").status_code == 403
    assert client.post("/api/v1/reports/subscriptions/x/test").status_code == 403
    _login(store, client, "l@x.com", "lead")
    assert client.get("/api/v1/reports/subscriptions").status_code == 200
    assert client.post("/api/v1/reports/subscriptions", json=body).status_code == 201
    store.close()


def test_subscription_patch_channel_switch_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """webhook→email 전환 시 secret_enc 폐기, email→webhook 전환 시 재발급."""
    client, store, _ = _client(monkeypatch)
    r = client.post(
        "/api/v1/reports/subscriptions",
        json={"kind": "daily", "channel": "webhook", "target": _LOOPBACK},
    )
    wid = r.json()["item"]["id"]
    # webhook → email: secret 없이 item만
    r = client.patch(
        f"/api/v1/reports/subscriptions/{wid}",
        json={"channel": "email", "target": "ops@example.com"},
    )
    assert r.status_code == 200, r.text
    assert "secret" not in r.json()
    row = store.get_report_subscription(wid)
    assert row is not None and not row.get("secret_enc")
    # email → webhook: 새 시크릿 1회 반환
    r = client.patch(
        f"/api/v1/reports/subscriptions/{wid}",
        json={"channel": "webhook", "target": _LOOPBACK},
    )
    assert r.status_code == 200 and r.json().get("secret")
    store.close()


# ---------------- API: /test 발송 ----------------


def test_test_endpoint_email_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    client, store, _ = _client(monkeypatch)
    sid = client.post(
        "/api/v1/reports/subscriptions",
        json={"kind": "daily", "channel": "email", "target": "ops@example.com"},
    ).json()["item"]["id"]
    r = client.post(f"/api/v1/reports/subscriptions/{sid}/test")
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": False, "detail": "skipped_unconfigured"}
    assert client.post("/api/v1/reports/subscriptions/nope/test").status_code == 404
    store.close()


def test_test_endpoint_webhook_signed(monkeypatch: pytest.MonkeyPatch) -> None:
    client, store, settings = _client(monkeypatch)
    r = client.post(
        "/api/v1/reports/subscriptions",
        json={"kind": "daily", "channel": "webhook", "target": _LOOPBACK},
    )
    secret = r.json()["secret"]
    sid = r.json()["item"]["id"]

    captured: dict[str, Any] = {}

    class _Resp:
        status_code = 200

    def fake_post(url, body, headers, timeout, *, post=None, transport=None):
        captured.update({"url": url, "body": body, "headers": headers})
        return _Resp()

    monkeypatch.setattr(webhooks_svc, "_post_delivery", fake_post)
    r = client.post(f"/api/v1/reports/subscriptions/{sid}/test")
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "detail": "sent"}
    assert captured["url"] == _LOOPBACK
    assert captured["headers"]["X-JobMate-Event"] == "report.daily"
    sig = captured["headers"]["X-JobMate-Signature"]
    assert sig.startswith("t=") and ",v1=" in sig
    payload = json.loads(captured["body"])
    assert payload["type"] == "report.daily"
    assert payload["data"]["subscription_id"] == sid
    # 발송 성공 → last_sent_at 기록
    sub = store.get_report_subscription(sid)
    assert sub is not None and sub.get("last_sent_at")
    # 시크릿 원문은 응답 어디에도 없어야 한다
    assert secret not in r.text
    store.close()


def test_test_endpoint_slack_mocked(monkeypatch: pytest.MonkeyPatch) -> None:
    client, store, _ = _client(monkeypatch)
    sid = client.post(
        "/api/v1/reports/subscriptions",
        json={"kind": "weekly", "channel": "slack", "target": _LOOPBACK},
    ).json()["item"]["id"]
    seen: dict[str, Any] = {}
    monkeypatch.setattr(
        messenger, "send_slack", lambda url, text: seen.update(url=url, text=text) or True
    )
    r = client.post(f"/api/v1/reports/subscriptions/{sid}/test")
    assert r.json() == {"ok": True, "detail": "sent"}
    assert seen["url"] == _LOOPBACK
    assert "kind=weekly" in seen["text"]
    store.close()


# ---------------- 서비스: deliver_report_subscriptions ----------------


def test_deliver_report_subscriptions_filters_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    client, store, settings = _client(monkeypatch)
    client.post(
        "/api/v1/reports/subscriptions",
        json={"kind": "daily", "channel": "email", "target": "d@x.com"},
    )
    client.post(
        "/api/v1/reports/subscriptions",
        json={"kind": "weekly", "channel": "email", "target": "w@x.com"},
    )
    sent: list[str] = []
    monkeypatch.setattr(mailer, "smtp_configured", lambda: True)
    monkeypatch.setattr(
        mailer,
        "_deliver",
        lambda to, *a, **k: sent.append(to) or True,  # noqa: SLF001
    )
    counts = reporting_svc.deliver_report_subscriptions(store, settings, "daily", _report())
    assert counts == {"sent": 1, "failed": 0, "skipped": 0}
    assert sent == ["d@x.com"]
    # weekly kind는 daily 발송에서 제외
    counts = reporting_svc.deliver_report_subscriptions(store, settings, "weekly", _report())
    assert counts["sent"] == 1 and sent == ["d@x.com", "w@x.com"]
    store.close()


def test_deliver_subscription_webhook_no_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """secret_enc 없는 webhook 구독 → skipped_unconfigured (발송 안 함)."""
    _env(monkeypatch)
    db = str(Path(tempfile.mkdtemp()) / "t.db")
    store = Store(db)
    settings = Settings(db_path=db, budget_max_tokens=100_000)
    sub = store.create_report_subscription(
        kind="daily",
        channel="webhook",
        target=_LOOPBACK,
        secret_enc="",
    )
    called = {"n": 0}

    def fake_post(*a: Any) -> Any:
        called["n"] += 1
        raise AssertionError("post must not be called without secret")

    ok, detail = reporting_svc.deliver_subscription(store, settings, sub, _report(), post=fake_post)
    assert (ok, detail) == (False, "skipped_unconfigured")
    assert called["n"] == 0
    store.close()


def test_report_subscriptions_org_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    """org A 구독은 org B 목록/발송에서 보이지 않는다."""
    _env(monkeypatch)
    db = str(Path(tempfile.mkdtemp()) / "t.db")
    settings = Settings(db_path=db, budget_max_tokens=100_000)
    store = Store(db)
    with orgctx.org_scope("orgA"):
        store.create_report_subscription(kind="daily", channel="email", target="a@x.com")
    with orgctx.org_scope("orgB"):
        assert store.list_report_subscriptions() == []
        counts = reporting_svc.deliver_report_subscriptions(store, settings, "daily", _report())
        assert counts == {"sent": 0, "failed": 0, "skipped": 0}
    store.close()
