"""SMTP 이메일 발송 — stdlib smtplib만 사용(새 의존성 없음).

환경 변수:
  JOBMATE_SMTP_HOST   SMTP 서버 호스트 (미설정 시 발송 불가 → False 반환)
  JOBMATE_SMTP_PORT   포트 (기본 587)
  JOBMATE_SMTP_USER   SMTP 인증 사용자 (선택 — 없으면 무인증 릴레이)
  JOBMATE_SMTP_PASS   SMTP 인증 비밀번호 (선택)
  JOBMATE_SMTP_FROM   발신 주소 (기본: JOBMATE_SMTP_USER, 그것도 없으면 jobmate@localhost)
  JOBMATE_SMTP_TLS    starttls(기본) | ssl | none

주의: 비밀번호·수신자 원문 등 민감값은 로그에 남기지 않는다.
"""

from __future__ import annotations

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage

logger = logging.getLogger("jobmate.mailer")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def smtp_configured() -> bool:
    """SMTP_HOST가 설정됐는지 — 미설정이면 발송 시도 없이 False."""
    return bool(_env("JOBMATE_SMTP_HOST"))


def send_verification_email(to: str, verify_url: str) -> bool:
    """이메일 인증 링크 발송.

    입력: to=수신 이메일, verify_url=인증 링크 전체 URL
    출력: 발송 성공 True / 미설정·실패 False (예외를 밖으로 던지지 않음 —
          가입 플로우가 메일 실패로 깨지지 않도록).
    """
    host = _env("JOBMATE_SMTP_HOST")
    if not host:
        if os.environ.get("VERCEL"):
            logger.warning("smtp not configured: verification email cannot be sent")
        else:
            # 로컬 개발 편의 — 인증 링크를 로그에서 바로 확인할 수 있게 info로 출력
            logger.info("smtp not configured; verification url for %s: %s", to, verify_url)
        return False

    try:
        port = int(_env("JOBMATE_SMTP_PORT", "587") or "587")
    except ValueError:
        port = 587
    user = _env("JOBMATE_SMTP_USER")
    password = _env("JOBMATE_SMTP_PASS")
    sender = _env("JOBMATE_SMTP_FROM") or user or "jobmate@localhost"
    tls = _env("JOBMATE_SMTP_TLS", "starttls").lower()

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = "[jobMate] 이메일 인증을 완료해주세요"
    msg.set_content(
        "jobMate 가입을 완료하려면 아래 링크를 열어주세요.\n\n"
        f"{verify_url}\n\n"
        "링크는 24시간 동안 유효합니다.\n"
        "본인이 가입을 요청하지 않았다면 이 메일을 무시하세요.\n"
    )

    try:
        context = ssl.create_default_context()
        if tls == "ssl":
            with smtplib.SMTP_SSL(host, port, timeout=15, context=context) as smtp:
                if user:
                    smtp.login(user, password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=15) as smtp:
                if tls == "starttls":
                    smtp.starttls(context=context)
                if user:
                    smtp.login(user, password)
                smtp.send_message(msg)
    except Exception as e:
        # SMTP 응답에 서버 정보가 포함될 수 있으므로 길이 제한 후 기록
        logger.warning("verification email send failed: %s", str(e)[:200])
        return False
    logger.info("verification email sent")
    return True
