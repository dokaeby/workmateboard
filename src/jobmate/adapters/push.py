"""Web Push 발송 어댑터 — 저장된 구독으로 pywebpush 전송."""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import urlsplit

import requests
from pywebpush import WebPushException, webpush

logger = logging.getLogger("jobmate.adapters.push")


def valid_push_subscription(endpoint: Any, keys: Any) -> bool:
    """Only browser push providers may receive subscription requests and notifications."""
    if not isinstance(endpoint, str) or len(endpoint) > 4096:
        return False
    try:
        url = urlsplit(endpoint)
        host = url.hostname or ""
        trusted = host in ("fcm.googleapis.com", "updates.push.services.mozilla.com") or (
            host.endswith((".push.apple.com", ".notify.windows.com"))
        )
        if (
            url.scheme != "https"
            or not trusted
            or url.port not in (None, 443)
            or url.username is not None
            or url.password is not None
            or url.fragment
            or not url.path
            or any(ch.isspace() or ord(ch) < 32 for ch in endpoint)
        ):
            return False
    except ValueError:
        return False
    return isinstance(keys, dict) and all(
        isinstance(keys.get(k), str) and 0 < len(keys[k]) <= 512 for k in ("p256dh", "auth")
    )


def _fingerprint(endpoint: str) -> str:
    """로그용 비식별 지문 — endpoint 원문 대신 SHA256 앞 12자."""
    import hashlib

    return hashlib.sha256(endpoint.encode()).hexdigest()[:12]


class WebPushNotifier:
    """Notifier 프로토콜 — push_subscriptions 전체로 Web Push 발송.

    VAPID_PRIVATE_KEY 미설정 또는 구독 없음이면 조용히 no-op.
    404/410 응답 구독은 만료로 간주해 삭제한다.
    """

    def __init__(
        self, store: Any, *, private_key: str, subject: str = "mailto:admin@localhost"
    ) -> None:
        self._store = store
        self._private_key = private_key
        self._subject = subject
        self._vapid_warned = False

    def send(self, *, title: str, body: str, source_url: str) -> None:
        if not self._private_key:
            if not self._vapid_warned:
                self._vapid_warned = True
                logger.warning("webpush skipped: VAPID_PRIVATE_KEY not set")
            else:
                logger.debug("webpush skipped: VAPID_PRIVATE_KEY not set")
            return
        subs = self._store.list_push_subscriptions()
        if not subs:
            return
        payload = json.dumps({"title": title, "body": body, "url": source_url or "/"})
        stale: list[str] = []
        for sub in subs:
            if not valid_push_subscription(sub.get("endpoint"), sub.get("keys")):
                logger.warning("webpush skipped: invalid stored subscription")
                continue
            info = {"endpoint": sub["endpoint"], "keys": sub["keys"]}
            try:
                with requests.Session() as session:
                    session.max_redirects = (
                        0  # A trusted provider must not redirect to internal URLs.
                    )
                    webpush(
                        subscription_info=info,
                        data=payload,
                        vapid_private_key=self._private_key,
                        vapid_claims={"sub": self._subject},
                        requests_session=session,
                        timeout=10,
                    )
                logger.debug("webpush sent: sub=%s", _fingerprint(sub["endpoint"]))
            except WebPushException as e:
                status = getattr(e.response, "status_code", None)
                if status in (404, 410):
                    stale.append(sub["endpoint"])
                # endpoint는 push 발송 capability라 로그에 남기지 않고 지문만 기록
                logger.warning(
                    "webpush failed: sub=%s status=%s err=%s",
                    _fingerprint(sub["endpoint"]),
                    status,
                    type(e).__name__,
                )
            except Exception as e:
                logger.warning(
                    "webpush error: sub=%s err=%s",
                    _fingerprint(sub["endpoint"]),
                    type(e).__name__,
                )
        for endpoint in stale:
            self._store.delete_push_subscription(endpoint)
            logger.info("webpush subscription removed (expired): sub=%s", _fingerprint(endpoint))
