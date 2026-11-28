"""external_spans attributes → 오류 여부 판정 (spec 012).

otlp_ingest(삽입 시 error_flag 계산)와 observability(tool-stats 집계)가
공유한다 — 두 경로가 같은 규칙을 쓰도록 단일 소스로 둔다.
판정 근거가 없는 스팬은 오류로 간주하지 않는다(추정 금지)."""

from __future__ import annotations

from typing import Any

_ERROR_KEYS = frozenset(
    {
        "error",
        "error.type",
        "exception",
        "exception.type",
        "exception.message",
        "otel.status_code",
        "otel_status_code",
        "status.code",
        "status_code",
    }
)
_ERROR_VALUES = frozenset({"error", "status_code_error", "2", "true", "1"})
_FALSE_VALUES = frozenset({"", "0", "false", "ok"})


def span_attrs_is_error(attrs: Any) -> bool:
    """attributes dict → 오류 여부.

    오류 키가 truthy 값(또는 명시적 error/status_code_error/2)을 가지면
    True. success=False도 오류. dict가 아니면 False."""
    if not isinstance(attrs, dict):
        return False
    for k, v in attrs.items():
        lk = str(k).lower()
        if lk in _ERROR_KEYS:
            if isinstance(v, bool):
                if v:
                    return True
                continue
            sv = str(v or "").strip().lower()
            if sv and (sv in _ERROR_VALUES or sv not in _FALSE_VALUES):
                # 'error'/'exception' 계열 키는 값이 있으면 오류,
                # status 계열 키는 error 계열 값일 때만 오류.
                if lk.startswith(("error", "exception")) or sv in _ERROR_VALUES:
                    return True
        if lk == "success" and v is False:
            return True
    return False
