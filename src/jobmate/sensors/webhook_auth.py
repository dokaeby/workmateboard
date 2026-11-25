"""Slack/Telegram 웹훅 서명 검증 — calendar-agent message_listeners 포팅."""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time

logger = logging.getLogger("jobmate.webhook_auth")

# 허용 목록(채널/chat id)이 비어 있을 때 전체 허용으로 fail-open 되던 것을
# fail-closed로 변경 — 웹훅 시크릿이 설정된 환경에서 allowlist 미설정은
# 사실상 인증 없는 수신이므로 거부한다. 기존 배포 호환을 위해
# JOBMATE_MESSENGER_ALLOW_ALL=1 env로 명시적 탈출구를 둔다.
ALLOW_ALL_ENV = "JOBMATE_MESSENGER_ALLOW_ALL"
_ALLOW_ALL_VALUES = {"1", "true", "yes", "on"}
_allow_all_warned = False


def _allow_all() -> bool:
    """JOBMATE_MESSENGER_ALLOW_ALL=1 일 때만 True — 최초 1회 경고 로그."""
    global _allow_all_warned
    if os.environ.get(ALLOW_ALL_ENV, "").strip().lower() not in _ALLOW_ALL_VALUES:
        return False
    if not _allow_all_warned:
        _allow_all_warned = True
        logger.warning(
            "%s=1 — 메신저 웹훅 허용 목록 미설정 시 모든 채널/Chat ID를 허용합니다 "
            "(fail-open). 운영 환경에서는 허용 목록 설정을 권장합니다.",
            ALLOW_ALL_ENV,
        )
    return True


def verify_slack_signature(body: bytes, timestamp: str, signature: str, secret: str) -> bool:
    """Slack 서명 + 5분 유효시간 검사. 원본 바이트 기준 HMAC."""
    if not secret:
        return False
    try:
        if not timestamp.isascii() or not timestamp.isdigit():
            return False
        if abs(time.time() - int(timestamp)) > 300:
            return False
    except ValueError:
        return False
    expected = (
        "v0="
        + hmac.new(
            secret.encode(), b"v0:" + timestamp.encode() + b":" + body, hashlib.sha256
        ).hexdigest()
    )
    return hmac.compare_digest(expected.encode(), signature.encode())


def verify_telegram_secret(provided: str, secret: str) -> bool:
    """Telegram webhook secret token 비교."""
    if not secret:
        return False
    return hmac.compare_digest(provided.encode(), secret.encode())


def allowed_identifier(identifier: str, configured: str) -> bool:
    """허용 목록(쉼표 구분) 검사 — fail-closed.

    configured가 비어 있으면 기본 거부(False). 명시적 탈출구로
    JOBMATE_MESSENGER_ALLOW_ALL=1 env가 설정된 경우에만 전체 허용한다
    (그 경우 기동/최초 사용 시 경고 로그 1회)."""
    allowed = {item.strip() for item in configured.split(",") if item.strip()}
    if allowed:
        return identifier in allowed
    return _allow_all()
