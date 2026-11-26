"""Shared row/value helpers for store mixins."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

_CTRL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


def clean_scope_id(scope_id: str) -> str:
    """scope_id/project 태그 정규화 — 제어문자 제거(로그 위조 방지) + 길이 제한.

    budget policy의 scope_id와 run.project가 동일 규칙을 공유해야
    스코프드 예산 귀속(project 스코프)이 일치한다. budget_scope.clean_scope_id는
    이 함수의 재수출이다."""
    return _CTRL_CHARS_RE.sub("", str(scope_id or "")).strip()[:200]


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


def _loads(s: str) -> dict[str, Any]:
    try:
        v = json.loads(s)
        return v if isinstance(v, dict) else {}
    except json.JSONDecodeError:
        return {}


def _clamp_limit(limit: Any, default: int = 50, maximum: int = 1000) -> int:
    """LIMIT 파라미터 정규화 — 음수/비정수/None이 SQLite `LIMIT -1`(무제한)
    반환으로 이어지지 않도록 최소 1·최대 maximum로 클램프한다."""
    try:
        n = int(limit)
    except (TypeError, ValueError):
        n = default
    return max(1, min(n, maximum))
