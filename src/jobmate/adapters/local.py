"""로컬 실행용 실용 어댑터 — 로그 기록 notifier 등."""

from __future__ import annotations

import logging

logger = logging.getLogger("jobmate.adapters.local")


class LoggingNotifier:
    """Notifier 프로토콜 — 로그에만 기록 (로컬 개발용)."""

    def send(self, *, title: str, body: str, source_url: str) -> None:
        # 알림 본문·제목·원본 URL은 개인정보를 포함할 수 있어 내용 대신
        # 길이만 기록한다 (로그 민감정보 원문 금지 원칙).
        logger.info(
            "notify sent title_chars=%d body_chars=%d has_url=%s",
            len(title),
            len(body),
            bool(source_url),
        )
