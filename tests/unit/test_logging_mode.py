"""mode별 로깅 레벨 + 토큰/endpoint 비노출 테스트."""

from __future__ import annotations

import logging

import pytest

from jobmate.adapters.push import WebPushNotifier, _fingerprint
from jobmate.config import Settings, configure_logging


@pytest.fixture(autouse=True)
def _reset_logging() -> None:
    yield
    lg = logging.getLogger("jobmate")
    lg.handlers.clear()
    lg.propagate = True
    lg.setLevel(logging.NOTSET)


def test_debug_mode_enables_debug() -> None:
    configure_logging(Settings(mode="debug"))
    assert logging.getLogger("jobmate").level == logging.DEBUG
    assert logging.getLogger("jobmate.adapters.push").isEnabledFor(logging.DEBUG)


def test_release_mode_hides_debug() -> None:
    configure_logging(Settings(mode="release"))
    assert logging.getLogger("jobmate").level == logging.INFO
    assert not logging.getLogger("jobmate.adapters.push").isEnabledFor(logging.DEBUG)
    assert logging.getLogger("jobmate.adapters.push").isEnabledFor(logging.INFO)


def test_push_failure_does_not_log_endpoint(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path
) -> None:
    endpoint = "https://fcm.googleapis.com/send/SECRET-TOKEN-123"

    class _Store:
        def list_push_subscriptions(self):
            return [{"endpoint": endpoint, "keys": {"p256dh": "a", "auth": "b"}}]

        def delete_push_subscription(self, ep: str) -> None:
            pass

    def _boom(**kwargs):
        raise RuntimeError(f"network down: {endpoint}")

    monkeypatch.setattr("jobmate.adapters.push.webpush", _boom)
    n = WebPushNotifier(_Store(), private_key="vapid-key")
    with caplog.at_level(logging.WARNING, logger="jobmate"):
        n.send(title="t", body="b", source_url="")
    assert "SECRET-TOKEN-123" not in caplog.text
    assert endpoint not in caplog.text
    assert _fingerprint(endpoint) in caplog.text
