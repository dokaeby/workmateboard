"""Shared row/value helpers for store mixins."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any


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
