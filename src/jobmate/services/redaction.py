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
