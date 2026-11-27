"""SMTP 이메일 발송 — stdlib smtplib만 사용(새 의존성 없음).

환경 변수:
  JOBMATE_SMTP_HOST   SMTP 서버 호스트 (미설정 시 발송 불가 → False 반환)
  JOBMATE_SMTP_PORT   포트 (기본 587)
  JOBMATE_SMTP_USER   SMTP 인증 사용자 (선택 — 없으면 무인증 릴레이)
  JOBMATE_SMTP_PASS   SMTP 인증 비밀번호 (선택)
  JOBMATE_SMTP_FROM   발신 주소 (기본: JOBMATE_SMTP_USER, 그것도 없으면 jobmate@localhost)
  JOBMATE_SMTP_TLS    starttls(기본) | ssl | none

SMTP_HOST 미설정 시 gmail 백엔드(send_message 지원 객체)로 폴백 —
Gmail API gmail.send scope 토큰이 있으면 별도 SMTP 계정 없이 발송된다.

주의: 비밀번호·수신자 원문 등 민감값은 로그에 남기지 않는다.
"""

from __future__ import annotations

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from typing import Any
from urllib.parse import urlparse, urlunparse

logger = logging.getLogger("jobmate.mailer")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def smtp_configured() -> bool:
    """SMTP_HOST가 설정됐는지 — 미설정이면 발송 시도 없이 False."""
    return bool(_env("JOBMATE_SMTP_HOST"))


_SENSITIVE_QUERY_KEYS = {"token", "code", "key", "secret", "password", "sig", "signature"}


def _log_safe_url(url: str) -> str:
    """로깅용 URL — 토큰/시크릿류 쿼리 값을 마스킹한다.

    로컬 개발에서 인증 링크를 로그로 확인하는 편의는 유지하되,
    token=... 원문이 로그에 남지 않도록 쿼리 값만 ***로 치환한다.
    """
    try:
        p = urlparse(url)
    except ValueError:
        return "(unparseable-url)"
    if not p.query:
        return urlunparse((p.scheme, p.netloc, p.path, "", "", ""))
    masked = []
    for pair in p.query.split("&"):
        k, _, _v = pair.partition("=")
        masked.append(f"{k}=***" if k.lower() in _SENSITIVE_QUERY_KEYS else pair)
    return urlunparse((p.scheme, p.netloc, p.path, "", "&".join(masked), ""))


def _verification_body(verify_url: str) -> tuple[str, str]:
    return (
        "[jobMate] 이메일 인증을 완료해 주세요",
        "jobMate 가입을 완료하려면 아래 링크를 열어 주세요.\n\n"
        f"{verify_url}\n\n"
        "링크는 24시간 동안 유효합니다.\n"
        "본인이 가입을 요청하지 않았다면 이 메일은 무시하셔도 됩니다.\n",
    )


def _reset_body(reset_url: str) -> tuple[str, str]:
    return (
        "[jobMate] 비밀번호 재설정",
        "비밀번호 재설정을 요청하셨습니다. 아래 링크에서 새 비밀번호를 설정해 주세요.\n\n"
        f"{reset_url}\n\n"
        "링크는 30분 동안 유효합니다.\n"
        "본인이 요청하지 않았다면 이 메일은 무시하셔도 됩니다 — 비밀번호는 변경되지 않습니다.\n",
    )


def _invite_body(org_name: str, role: str, invite_url: str) -> tuple[str, str]:
    role_ko = "관리자" if role == "org_admin" else "멤버"
    return (
        f"[jobMate] {org_name} 조직 초대",
        f"{org_name} 조직에 {role_ko}로 초대되었습니다.\n\n"
        "아래 링크를 열어 초대를 확인하고 수락해 주세요.\n\n"
        f"{invite_url}\n\n"
        "링크는 72시간 동안 유효하며 한 번만 사용할 수 있습니다.\n"
        "초대받은 이메일과 동일한 계정으로 로그인해야 수락할 수 있습니다.\n"
        "본인이 요청하지 않았다면 이 메일은 무시하셔도 됩니다.\n",
    )


def _deliver(to: str, subject: str, body: str, gmail: Any | None, kind: str, link: str) -> bool:
    """SMTP→Gmail API 폴백 발송 공통부. kind는 로그 라벨, link는 로컬 개발용
    마스킹 URL(_log_safe_url 적용)."""
    if not smtp_configured():
        if gmail is not None:
            try:
                gmail.send_message(to, subject, body)
            except Exception as e:
                logger.warning("gmail %s email send failed: %s", kind, str(e)[:200])
                return False
            logger.info("%s email sent via gmail api", kind)
            return True
        if os.environ.get("VERCEL"):
            logger.warning("no mail backend configured: %s email cannot be sent", kind)
        else:
            # 로컬 개발 편의 — 링크를 로그에서 바로 확인할 수 있게 info로 출력.
            # 토큰 등 민감 쿼리 값은 마스킹한다(로그에 토큰 원문 금지 원칙).
            logger.info(
                "mail backend not configured; %s url for %s: %s",
                kind,
                to,
                _log_safe_url(link),
            )
        return False

    host = _env("JOBMATE_SMTP_HOST")

    try:
        port = int(_env("JOBMATE_SMTP_PORT", "587") or "587")
    except ValueError:
        port = 587
    user = _env("JOBMATE_SMTP_USER")
    password = _env("JOBMATE_SMTP_PASS")
    sender = _env("JOBMATE_SMTP_FROM") or user or "jobmate@localhost"
    tls = _env("JOBMATE_SMTP_TLS", "starttls").lower()

    try:
        msg = EmailMessage()
        msg["From"] = sender
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)

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
        logger.warning("%s email send failed: %s", kind, str(e)[:200])
        return False
    logger.info("%s email sent", kind)
    return True


def send_verification_email(to: str, verify_url: str, gmail: Any | None = None) -> bool:
    """이메일 인증 링크 발송.

    입력: to=수신 이메일, verify_url=인증 링크 전체 URL,
          gmail=send_message(to, subject, body) 지원 객체(선택)
    출력: 발송 성공 True / 미설정·실패 False (예외를 밖으로 던지지 않음 —
          가입 플로우가 메일 실패로 깨지지 않도록).

    우선순위: JOBMATE_SMTP_HOST 설정 시 SMTP → 아니면 gmail 백엔드
    (Gmail API gmail.send scope — 별도 SMTP 계정 불필요).
    """
    subject, body = _verification_body(verify_url)
    return _deliver(to, subject, body, gmail, "verification", verify_url)


def send_password_reset_email(to: str, reset_url: str, gmail: Any | None = None) -> bool:
    """비밀번호 재설정 링크 발송 — send_verification_email과 동일한
    SMTP→Gmail API 폴백 구조."""
    subject, body = _reset_body(reset_url)
    return _deliver(to, subject, body, gmail, "password-reset", reset_url)


def send_org_invite_email(
    to: str,
    org_name: str,
    role: str,
    invite_url: str,
    gmail: Any | None = None,
) -> bool:
    """조직 초대 링크 발송 — spec 003 FR-008. 토큰 원문은 invite_url에만 포함."""
    subject, body = _invite_body(org_name, role, invite_url)
    return _deliver(to, subject, body, gmail, "org-invite", invite_url)


def send_login_alert_email(
    to: str,
    ip: str,
    ua_summary: str,
    when: str,
    gmail: Any | None = None,
) -> bool:
    """신규 기기/새 IP 로그인 알림 — spec 002 FR-018.

    입력: to=수신 이메일, ip=접속 IP, ua_summary=User-Agent 요약(80자 이내),
          when=로그인 시각 문자열. 세션 토큰·비밀번호는 포함하지 않는다."""
    subject = "[jobMate] 새 기기에서 로그인되었습니다"
    body = (
        "jobMate 계정에 새 기기에서 로그인이 있었습니다.\n\n"
        f"- 시각: {when}\n"
        f"- IP: {ip}\n"
        f"- 브라우저: {ua_summary or '(unknown)'}\n\n"
        "본인이 아니라면 즉시 비밀번호를 변경하고 관리자에게 문의하세요."
    )
    return _deliver(to, subject, body, gmail, "login-alert", "")
