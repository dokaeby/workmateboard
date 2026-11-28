"""secret-like 키 마스킹 — adapter_config/이벤트 detail/API 응답 공용.

원문을 저장·응답에 남기지 않기 위한 순수 함수 모음. services 계층 어디서나
import할 수 있도록 다른 services 모듈에 의존하지 않는다.
"""

from __future__ import annotations

import re
from typing import Any

# adapter_config/이벤트 detail에서 마스킹할 secret-like 키 — API 응답과
# 저장 detail에 원문을 남기지 않는다. 'key'는 부분 문자열로 매칭해
# apikey/secret_key 등을 잡는다 — monkey 같은 오탐은 과도 마스킹일 뿐
# 보안상 안전한 방향이다(M3). dsn/database_url/connection_string/
# authorization/session/cookie도 연결 문자열·세션 값이 credentials를
# 품는 경우가 많아 마스킹 대상이다. 'auth'는 author 오탐을 피해
# 단어 경계(뒤에 영문자가 오지 않음)로만 매칭한다.
_SECRET_KEY_RE = re.compile(
    r"(key|secret|token|password|passwd|api[_-]?key|credential|private|bearer"
    r"|dsn|database[_-]?url|connection[_-]?string|authorization|session|cookie"
    r"|authenticat|auth(?![a-z]))",
    re.IGNORECASE,
)
# safe 키(endpoint/webhook_url 등) 아래 URL 문자열에 박힌
# userinfo(credentials)를 지운다 — https://user:pass@host 형태.
_URL_USERINFO_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://)([^/@\s:]+):([^/@\s]+)@")
REDACTED = "REDACTED"


def _scrub_url_userinfo(v: Any) -> Any:
    """문자열 값이 URL 형태면 userinfo의 비밀번호 부분을 마스킹한다."""
    if isinstance(v, str) and "://" in v:
        return _URL_USERINFO_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}:{REDACTED}@", v)
    return v


def _redact_value(v: Any) -> Any:
    """중첩 dict/list 재귀 마스킹 — 리스트 안 dict의 secret 키도 치환(M3).
    safe 키 아래 문자열은 URL userinfo의 내장 credential을 지운다."""
    if isinstance(v, dict):
        return redact_adapter_config(v)
    if isinstance(v, list):
        return [_redact_value(i) for i in v]
    return _scrub_url_userinfo(v)


def redact_adapter_config(cfg: Any) -> dict[str, Any]:
    """adapter_config/임의 detail dict 복사본 — secret-like 키의 값을
    REDACTED로 치환. dict가 아니면 {}를 반환한다."""
    if not isinstance(cfg, dict):
        return {}
    out: dict[str, Any] = {}
    for k, v in cfg.items():
        key = str(k)
        if _SECRET_KEY_RE.search(key):
            out[key] = REDACTED if v else v
        else:
            out[key] = _redact_value(v)
    return out


# ---- PII 마스킹 (spec 005 US4 / FR-026~029) ----
#
# secret 키 마스킹(_SECRET_KEY_RE, 값 단위)과 독립된 계층 — 자유 텍스트
# 단위로 이메일·국내 전화번호·주민등록번호 형태·신용카드 형태를 치환한다.
# 적용 지점은 agent_events.detail / agent_session_events doing·ask 등
# 관찰 이벤트 직렬화 경로로 한정(FR-027). audit_events.actor,
# users.email 같은 행위자 식별 필드는 호출자가 이 함수를 적용하지 않는다.
#
# 게이트: config.pii_mask == "on" 일 때만 동작(기본 off = 원문 유지,
# FR-028). config를 생략하면 get_settings()를 지연 로드한다 — store 계층이
# settings를 들고 있지 않기 때문. get_settings 실패 시 off로 간주(보수적).

# 이메일 — 로컬 첫 글자만 남기고 도메인은 유지: a***@example.com
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)\b")
# 신용카드 형태 — 4자리 그룹 3~4개(12~16자리, 구분자 선택). 전화번호
# (10~11자리)보다 먼저 적용해 하이픈 없는 긴 숫자열의 오매칭을 줄인다.
_CARD_RE = re.compile(r"(?<!\d)(?:\d{4}[-\s]?){2,3}\d{4}(?!\d)")
# 주민등록번호 형태 — 앞 6자리 + [1-4] + 뒤 6자리(하이픈 선택).
_RRN_RE = re.compile(r"(?<!\d)\d{6}-?[1-4]\d{6}(?!\d)")
# 국내 전화번호 — 모바일(010/011/016/017/018/019), 서울 02,
# 지역번호 03x~06x. 숫자만/하이픈/공백 구분을 모두 허용한다.
_PHONE_RES = (
    re.compile(r"(?<!\d)01[016789][-\s]?\d{3,4}[-\s]?\d{4}(?!\d)"),
    re.compile(r"(?<!\d)0[3-6]\d[-\s]?\d{3,4}[-\s]?\d{4}(?!\d)"),
    re.compile(r"(?<!\d)02[-\s]?\d{3,4}[-\s]?\d{4}(?!\d)"),
)


def _pii_mask_on(config: Any) -> bool:
    """pii_mask 게이트 — config가 없으면 전역 settings로 폴백."""
    if config is None:
        try:
            from ..config import get_settings

            config = get_settings()
        except Exception:  # 설정 로드 실패 시 마스킹 off(기존 동작 유지)
            return False
    if isinstance(config, dict):
        return str(config.get("pii_mask") or "") == "on"
    return str(getattr(config, "pii_mask", "") or "") == "on"


def _mask_email(m: re.Match[str]) -> str:
    local = m.group(0).split("@", 1)[0]
    head = local[:1] if local else ""
    return f"{head}***@{m.group(1)}"


def _mask_phone(m: re.Match[str]) -> str:
    """전화번호 — 지역/모바일 prefix와 끝 4자리만 남기고 가운데를 마스킹.

    예: 010-1234-5678 → 010-****-5678, 02-123-4567 → 02-****-4567,
    031-123-4567 → 031-****-4567 (FR-026 예시 010-****-1234 형태)."""
    digits = re.sub(r"\D", "", m.group(0))
    prefix = "02" if digits.startswith("02") else digits[:3]
    return f"{prefix}-****-{digits[-4:]}"


def _mask_card(m: re.Match[str]) -> str:
    """카드 형태 — 끝 4자리만 남기고 나머지를 *로 치환(구분자 유지)."""
    text = m.group(0)
    digits = [c for c in text if c.isdigit()]
    keep = len(digits) - 4
    out: list[str] = []
    seen = 0
    for ch in text:
        if ch.isdigit():
            seen += 1
            out.append(ch if seen > keep else "*")
        else:
            out.append(ch)
    return "".join(out)


def mask_pii(text: str, config: Any = None) -> str:
    """자유 텍스트 PII 마스킹 — pii_mask=on일 때만 적용되는 순수 함수.

    패턴: 이메일 / 국내 전화번호 / 주민등록번호 형태 / 신용카드 형태.
    off(기본)이면 입력을 그대로 반환한다 — 기존 동작·테스트와 하위 호환.
    반환값은 항상 str; 비문자열 입력은 그대로 반환한다."""
    if not isinstance(text, str) or not text or not _pii_mask_on(config):
        return text
    out = _CARD_RE.sub(_mask_card, text)
    out = _RRN_RE.sub("******-*******", out)
    out = _EMAIL_RE.sub(_mask_email, out)
    for rx in _PHONE_RES:
        out = rx.sub(_mask_phone, out)
    return out
