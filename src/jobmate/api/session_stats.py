"""agent_sessions 행의 읽기 모델 헬퍼 — health 판정·USD 비용 추정.

support.py에서 분리. 순수 함수만 두며 외부 상태를 갖지 않는다.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

# 세션 토큰 누적치 → USD 추정치 (1M 토큰당). tool별 오버라이드, 없으면 default.
_SESSION_TOKEN_PRICES: dict[str, tuple[float, float]] = {
    "default": (3.0, 15.0),
    "claude": (3.0, 15.0),
    "codex": (1.5, 6.0),
    "gemini": (1.0, 4.0),
}

_SESSION_STALE_SECONDS = 300.0
_SESSION_LONG_IDLE_SECONDS = 43200.0


def _parse_session_ts(value: Any) -> datetime | None:
    """ISO 타임스탬프 파싱 — 실패/naive는 UTC 간주."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _session_health(row: dict[str, Any]) -> str:
    """세션 상태 건강도 — ended/stale/long_idle/active (Python 계산, SQL 아님)."""
    status = str(row.get("status") or "")
    if status == "ended":
        return "ended"
    ts = _parse_session_ts(row.get("last_event_at")) or _parse_session_ts(row.get("updated_at"))
    age = (datetime.now(UTC) - ts).total_seconds() if ts is not None else 0.0
    if status in ("working", "decision"):
        return "stale" if age > _SESSION_STALE_SECONDS else "active"
    if status == "rest":
        return "long_idle" if age > _SESSION_LONG_IDLE_SECONDS else "active"
    return "active"


def _session_est_cost_usd(row: dict[str, Any]) -> float | None:
    """tokens_in/out 누적치 → USD 추정. 둘 다 없으면 None."""
    tin = row.get("tokens_in")
    tout = row.get("tokens_out")
    if tin is None and tout is None:
        return None
    price_in, price_out = _SESSION_TOKEN_PRICES.get(
        str(row.get("tool") or ""), _SESSION_TOKEN_PRICES["default"]
    )
    return round(((tin or 0) * price_in + (tout or 0) * price_out) / 1_000_000, 6)


def _session_view(row: dict[str, Any]) -> dict[str, Any]:
    return {**row, "health": _session_health(row), "est_cost_usd": _session_est_cost_usd(row)}
