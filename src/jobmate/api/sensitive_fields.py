"""민감 필드 처리 — audit detail 마스킹 + connector config 비밀값 암호화.

support.py에서 분리. API 응답/audit에 원문을 남기지 않기 위한 필드 단위
보호 로직을 한 곳에 모은다.
"""

from __future__ import annotations

import re
from typing import Any

# ---------- audit detail 비밀/내부경로 마스킹 ----------

# API 응답에서는 _project_out/_alias_project_out이 비-명시 lead에게 숨기는
# 프로젝트 필드 — audit detail에도 원문을 남기지 않는다(/api/v1/audit은
# admin=머신 키까지 읽을 수 있어 응답 필터를 우회한다).
_AUDIT_REDACTED_PROJECT_FIELDS = frozenset({"workspace_path"})


def sanitize_project_audit_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """project create/patch audit detail용 — 민감 필드 값을 '<redacted>'로
    치환한 사본을 반환한다(키 존재 여부는 유지해 변경 필드 목록은 남긴다)."""
    return {
        k: ("<redacted>" if k in _AUDIT_REDACTED_PROJECT_FIELDS else v) for k, v in fields.items()
    }


# ---------- connector config 비밀값 암호화 ----------
_SECRET_FIELD_RE = re.compile(
    r"(?i)(token|secret|password|api[_-]?key|private[_-]?key|credential|authorization)"
)


def _config_has_secret(config: dict[str, Any]) -> bool:
    """config에 비밀로 취급할 비어있지 않은 문자열 필드가 있는지."""

    def walk(v: Any, key_hint: str = "") -> bool:
        if isinstance(v, dict):
            return any(walk(x, str(k)) for k, x in v.items())
        if isinstance(v, list):
            return any(walk(x, key_hint) for x in v)
        return isinstance(v, str) and bool(v) and bool(_SECRET_FIELD_RE.search(key_hint))

    return walk(config)


def _encrypt_config_secrets(config: dict[str, Any]) -> dict[str, Any]:
    """비밀 필드(token/secret/password/api_key 등) 값만 Fernet 암호화한 config 사본."""
    from ..security import encrypt_str

    def walk(v: Any, key_hint: str = "") -> Any:
        if isinstance(v, dict):
            return {k: walk(x, str(k)) for k, x in v.items()}
        if isinstance(v, list):
            return [walk(x, key_hint) for x in v]
        if (
            isinstance(v, str)
            and v
            and _SECRET_FIELD_RE.search(key_hint)
            and not v.startswith("enc:v1:")
        ):
            return encrypt_str(v)
        return v

    return walk(config)


def _decrypt_config_secrets(config: dict[str, Any]) -> dict[str, Any]:
    """저장된 config의 enc:v1: 값을 복호화한 사본 — fetch 직전 사용."""
    from ..security import decrypt_str

    def walk(v: Any) -> Any:
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}
        if isinstance(v, list):
            return [walk(x) for x in v]
        if isinstance(v, str) and v.startswith("enc:v1:"):
            return decrypt_str(v)
        return v

    return walk(config)


def _connector_token(cfg: dict[str, Any]) -> str:
    """복호화된 config에서 bearer token 후보를 찾는다."""
    for key in ("token", "access_token", "api_key", "apikey", "secret", "password"):
        v = cfg.get(key)
        if isinstance(v, str) and v:
            return v
    return ""
