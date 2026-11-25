"""lead.py 공용 헬퍼 — 타임스탬프/프로젝트 태그 정규화 + 세션 헬스/비용/뷰.

lead_projects/lead_sessions/lead_risks/lead_approvals/lead_perf가 공유하는
순수 함수 모음. 상태 변경 없음.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

UNTAGGED = "(기타)"
_MAX_ITEMS = 50

# app.py _SESSION_TOKEN_PRICES와 동일 매핑 — 세션 est_cost는 tool 라벨을
# pricing.price_for_model에 넣어 계산한다(동일 단가표).
_TERMINAL_OK = {"completed"}
_TERMINAL_BAD = {"failed", "rejected", "budget_exhausted", "needs_human", "cancelled"}
_APPROVAL_STATES = {"waiting_approval", "waiting_review"}


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse_ts(value: Any) -> datetime | None:
    """ISO 타임스탬프 파싱 — 실패/naive는 UTC 간주 (app.py와 동일 규칙)."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _age_seconds(at: Any, now: datetime | None = None) -> float | None:
    dt = _parse_ts(at)
    if dt is None:
        return None
    return max(0.0, ((now or _now()) - dt).total_seconds())


def _proj_key(value: Any) -> str:
    """프로젝트 태그 정규화 키 — trim + case-insensitive."""
    return str(value or "").strip().lower()


def _proj_raw(value: Any) -> str:
    return str(value or "").strip()


def _run_project(run: Any) -> str:
    """run의 프로젝트 태그 — project 컬럼 우선, input.project 폴백."""
    col = _proj_raw(getattr(run, "project", ""))
    if col:
        return col
    inp = getattr(run, "input", None)
    if isinstance(inp, dict):
        return _proj_raw(inp.get("project"))
    return ""


# ---------- 세션 헬스/비용 (app.py _session_health/_session_est_cost_usd 미러) ----------

_SESSION_STALE_SECONDS = 300.0
_SESSION_LONG_IDLE_SECONDS = 43200.0


def session_health(row: dict[str, Any]) -> str:
    """ended/stale/long_idle/active — app.py _session_health와 동일 규칙."""
    status = str(row.get("status") or "")
    if status == "ended":
        return "ended"
    ts = _parse_ts(row.get("last_event_at")) or _parse_ts(row.get("updated_at"))
    age = (_now() - ts).total_seconds() if ts is not None else 0.0
    if status in ("working", "decision"):
        return "stale" if age > _SESSION_STALE_SECONDS else "active"
    if status == "rest":
        return "long_idle" if age > _SESSION_LONG_IDLE_SECONDS else "active"
    return "active"


def session_est_cost_usd(row: dict[str, Any]) -> float | None:
    """tokens_in/out → USD 추정. 둘 다 없으면 None (app.py와 동일 값)."""
    tin = row.get("tokens_in")
    tout = row.get("tokens_out")
    if tin is None and tout is None:
        return None
    from .pricing import price_for_model

    price_in, price_out = price_for_model(str(row.get("tool") or ""))
    return round(((tin or 0) * price_in + (tout or 0) * price_out) / 1_000_000, 6)


def session_view(row: dict[str, Any]) -> dict[str, Any]:
    return {**row, "health": session_health(row), "est_cost_usd": session_est_cost_usd(row)}


def _fmt_duration(seconds: float) -> str:
    """초 → '2시간 13분'/'45분'/'30초' 한국어 표기."""
    s = int(round(seconds))
    if s < 60:
        return f"{s}초"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}분" if s == 0 else f"{m}분 {s}초"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}시간 {m}분" if m else f"{h}시간"
    d, h = divmod(h, 24)
    return f"{d}일 {h}시간"
