"""paperclip bridge URL 정책 — `jobmate.webutil`에 위임한다.

정책의 단일 기준은 webutil.validate_outbound_url이다:
http는 loopback 전용, userinfo(내장 credential) 금지, 비-loopback
호스트는 is_safe_fetch_url SSRF 가드 통과 필수. Bearer/API 키가 실리는
엔드포인트용이라 paperclip 시절과 동일하게 엄격하다.

webutil은 FetchError를 던지므로, 여기서 PaperclipConfigError로 변환해
paperclip 예외 계약(타입 + 메시지)을 유지한다. 에러 메시지에 URL
원문/자격증명은 포함하지 않는다.
"""

from __future__ import annotations

from ..webutil import FetchError, validate_outbound_url
from .errors import PaperclipConfigError

# FetchError 코드(`{what}_{code}`) → paperclip 시절 메시지 본문.
# webutil 코드와 1:1 대응하며 순서/판정은 validate_outbound_url이 담당한다.
_MESSAGES = {
    "empty": "url is empty",
    "invalid": "url is invalid",
    "scheme": "url scheme must be http or https",
    "no_host": "url has no host",
    "credentials": "url must not contain credentials",
    "https_required": "url must use https for non-loopback hosts",
    "not_allowed": "url host is not allowed",
}


def _validate_url_policy(url: str, *, what: str) -> str:
    """공용 URL 정책 — bearer/API 키가 실리는 엔드포인트용.

    webutil.validate_outbound_url과 판정이 동일하다(http loopback 전용,
    userinfo 금지, 비-loopback은 SSRF 가드). 위반 시
    PaperclipConfigError."""
    try:
        return validate_outbound_url(url, what=what)
    except FetchError as e:
        code = str(e).removeprefix(f"{what}_")
        detail = _MESSAGES.get(code, "url is not allowed")
        raise PaperclipConfigError(f"{what} {detail}") from e


def validate_paperclip_url(url: str) -> str:
    """paperclip base URL 검증 — 정규화된 base URL을 반환한다.

    허용: https + (loopback 호스트 또는 webutil SSRF 가드 통과 공인
    호스트), 또는 http + loopback만. Bearer 토큰이 실리므로 공인 호스트의
    http는 거부한다(spec의 "HTTPS/localhost URL 정책")."""
    return _validate_url_policy(url, what="paperclip")


def validate_jobmate_url(url: str) -> str:
    """bridge → jobMate snapshot 엔드포인트 base URL 검증.

    X-API-Key가 실리므로 paperclip과 동일 정책 — https 공인 호스트 또는
    http/https loopback만 허용한다."""
    return _validate_url_policy(url, what="jobmate")
