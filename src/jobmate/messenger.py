"""Slack/Telegram 양방향 메신저 커맨드 + 발송 헬퍼.

- parse_command: 수신 텍스트를 {"action": ...} 커맨드로 파싱 (한/영).
- send_slack/send_telegram: env 기반 아웃바운드 발송 (httpx, timeout).
- notify: JOBMATE_SLACK_WEBHOOK_URL / JOBMATE_TELEGRAM_BOT_TOKEN+
  JOBMATE_TELEGRAM_CHAT_ID가 설정된 채널로 best-effort 알림.

토큰·webhook URL 원문은 절대 로깅하지 않는다.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

import httpx

from jobmate.webutil import FetchError, pinned_transport_for, validate_outbound_url

logger = logging.getLogger("jobmate.messenger")

HELP_TEXT = (
    "jobMate 커맨드:\n"
    "• 승인해 [run_id]: 대기 중인 run을 승인합니다 (run_id를 빼면 가장 최근 것)\n"
    "• 시작 <workflow> <작업>: run을 생성합니다 (예: 시작 dev_review 버그 수정)\n"
    "• 현황: 최근 7일 요약\n"
    "• 도움말: 이 목록"
)

_APPROVE_RE = re.compile(r"^(?:승인(?:해|하기)?|approve)\b\s*(?P<run_id>[0-9a-zA-Z_-]*)\s*$", re.I)
_STATUS_RE = re.compile(r"^(?:현황|상태|status)\b\s*$", re.I)
_HELP_RE = re.compile(r"^(?:도움말|help|\?)\s*$", re.I)
# "시작 dev_review 작업..." / "run 시작 dev_review 작업..." / "start dev_review ..."
_START_RES = [
    re.compile(r"^(?:run\s+)?(?:시작|start)\s+(?P<wf>\S+)\s+(?P<task>.+)$", re.I),
    # "run dev_review 작업..." — workflow가 WORKFLOWS에 있는지는 실행부에서 검증
    re.compile(r"^run\s+(?P<wf>\S+)\s+(?P<task>.+)$", re.I),
]


def parse_command(text: str) -> dict[str, Any] | None:
    """메신저 텍스트 → 커맨드 dict 또는 None(무시).

    반환 형태:
      {"action":"approve","run_id": str|""}
      {"action":"start","workflow": str,"task": str}
      {"action":"status"} / {"action":"help"}
    """
    t = (text or "").strip()
    if not t:
        return None
    m = _APPROVE_RE.match(t)
    if m:
        return {"action": "approve", "run_id": (m.group("run_id") or "").strip()}
    if _STATUS_RE.match(t):
        return {"action": "status"}
    if _HELP_RE.match(t):
        return {"action": "help"}
    for rx in _START_RES:
        m = rx.match(t)
        if m:
            return {
                "action": "start",
                "workflow": m.group("wf").strip(),
                "task": m.group("task").strip()[:2000],
            }
    return None


def _post_json(url: str, payload: dict[str, Any]) -> httpx.Response:
    """JSON POST — 공인 호스트는 검증된 IP로 connect를 고정해 DNS 리바인딩을
    차단한다. URL은 호출 전 validate_outbound_url을 통과해야 한다."""
    transport = pinned_transport_for(url)
    if transport is not None:
        with httpx.Client(transport=transport, timeout=10) as client:
            return client.post(url, json=payload)
    return httpx.post(url, json=payload, timeout=10)


def send_slack(webhook_url: str, text: str) -> bool:
    """Slack incoming webhook으로 텍스트 발송. 실패 시 False + 경고 로그."""
    if not webhook_url:
        return False
    try:
        url = validate_outbound_url(webhook_url, what="slack_webhook")
    except FetchError:
        logger.warning("messenger slack webhook url rejected")
        return False
    try:
        resp = _post_json(url, {"text": text})
        resp.raise_for_status()
        logger.info("messenger slack sent")
        return True
    except Exception as e:
        # webhook URL/응답 본문은 로그에 남기지 않는다
        logger.warning("messenger slack send failed: %s", type(e).__name__)
        return False


def send_telegram(bot_token: str, chat_id: str, text: str) -> bool:
    """Telegram Bot API sendMessage. 실패 시 False + 경고 로그."""
    if not bot_token or not chat_id:
        return False
    try:
        url = validate_outbound_url(
            f"https://api.telegram.org/bot{bot_token}/sendMessage", what="telegram_api"
        )
    except FetchError:
        logger.warning("messenger telegram url rejected")
        return False
    try:
        resp = _post_json(url, {"chat_id": chat_id, "text": text[:4000]})
        resp.raise_for_status()
        logger.info("messenger telegram sent")
        return True
    except Exception as e:
        logger.warning("messenger telegram send failed: %s", type(e).__name__)
        return False


def configured_channels() -> list[str]:
    """설정된 발송 채널 이름 목록 (알림용)."""
    channels: list[str] = []
    if os.environ.get("JOBMATE_SLACK_WEBHOOK_URL", ""):
        channels.append("slack")
    if os.environ.get("JOBMATE_TELEGRAM_BOT_TOKEN", "") and os.environ.get(
        "JOBMATE_TELEGRAM_CHAT_ID", ""
    ):
        channels.append("telegram")
    return channels


def notify(text: str) -> int:
    """설정된 모든 메신저 채널로 best-effort 발송. 발송 채널 수 반환."""
    sent = 0
    slack_url = os.environ.get("JOBMATE_SLACK_WEBHOOK_URL", "")
    if slack_url and send_slack(slack_url, text):
        sent += 1
    token = os.environ.get("JOBMATE_TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("JOBMATE_TELEGRAM_CHAT_ID", "")
    if token and chat_id and send_telegram(token, chat_id, text):
        sent += 1
    if sent:
        logger.info("messenger notify sent channels=%d", sent)
    return sent
