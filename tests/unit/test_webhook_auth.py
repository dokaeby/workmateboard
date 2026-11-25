"""calendar-agent 포팅 기능 테스트 — 웹훅 인증, 충돌 검사, 알림 예약."""

import hashlib
import hmac
import time

from jobmate.sensors import (
    allowed_identifier,
    verify_slack_signature,
    verify_telegram_secret,
)


def _slack_sig(body: bytes, ts: str, signing_key: str) -> str:
    return (
        "v0="
        + hmac.new(
            signing_key.encode(), b"v0:" + ts.encode() + b":" + body, hashlib.sha256
        ).hexdigest()
    )


def test_slack_signature_valid():
    signing_key = "s3cr3t"
    body = b'{"type":"event_callback"}'
    ts = str(int(time.time()))
    assert verify_slack_signature(body, ts, _slack_sig(body, ts, signing_key), signing_key)


def test_slack_signature_bad():
    signing_key = "s3cr3t"
    body = b'{"type":"event_callback"}'
    ts = str(int(time.time()))
    assert not verify_slack_signature(body, ts, "v0=bad", signing_key)


def test_slack_signature_expired():
    signing_key = "s3cr3t"
    body = b"{}"
    ts = str(int(time.time()) - 400)
    assert not verify_slack_signature(body, ts, _slack_sig(body, ts, signing_key), signing_key)


def test_slack_signature_no_signing_key():
    assert not verify_slack_signature(b"{}", "1", "v0=x", "")


def test_telegram_secret():
    assert verify_telegram_secret("tok", "tok")
    assert not verify_telegram_secret("bad", "tok")
    assert not verify_telegram_secret("tok", "")


def test_allowed_identifier():
    # fail-closed: 허용 목록이 비어 있으면 기본 거부
    assert not allowed_identifier("C1", "")
    assert allowed_identifier("C1", "C1,C2")
    assert not allowed_identifier("C3", "C1,C2")


def test_allowed_identifier_allow_all_env(monkeypatch):
    """JOBMATE_MESSENGER_ALLOW_ALL=1 일 때만 빈 목록이 전체 허용된다."""
    import jobmate.sensors.webhook_auth as wa

    monkeypatch.delenv("JOBMATE_MESSENGER_ALLOW_ALL", raising=False)
    assert not allowed_identifier("C1", "")
    monkeypatch.setenv("JOBMATE_MESSENGER_ALLOW_ALL", "1")
    assert allowed_identifier("C1", "")
    assert allowed_identifier("anything", "  , ,")
    # env 해제 시 다시 fail-closed
    monkeypatch.delenv("JOBMATE_MESSENGER_ALLOW_ALL")
    assert not allowed_identifier("C1", "")
    # '0'/'false' 등은 허용하지 않음
    monkeypatch.setenv("JOBMATE_MESSENGER_ALLOW_ALL", "0")
    assert not allowed_identifier("C1", "")
    assert wa.ALLOW_ALL_ENV == "JOBMATE_MESSENGER_ALLOW_ALL"
