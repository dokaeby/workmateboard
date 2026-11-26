"""세션 회고 집계 — session_history (GET /agent-sessions/{key}/history).

Store 읽기 + 순수 함수, 상태 변경 없음.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from typing import TYPE_CHECKING, Any

from .lead_common import (
    _fmt_duration,
    _parse_ts,
    session_est_cost_usd,
    session_view,
)

if TYPE_CHECKING:
    from .store.db import Store


# ---------- 세션 회고 (GET /agent-sessions/{key}/history) ----------


def session_history(store: Store, tool: str, session_key: str) -> dict[str, Any] | None:
    """세션별 회고 — 전이 타임라인 + 결정적 한국어 stats/summary."""
    sess = store.get_agent_session(tool, session_key)
    if sess is None:
        return None
    events = store.agent_session_history(tool, session_key, limit=500)

    # 타임라인 — from_state→to_state + 다음 이벤트까지의 duration
    parsed: list[tuple[datetime | None, dict[str, Any]]] = []
    for e in events:
        at = _parse_ts(e.get("event_at")) or _parse_ts(e.get("created_at"))
        parsed.append((at, e))
    timeline: list[dict[str, Any]] = []
    for i, (at, e) in enumerate(parsed):
        nxt = parsed[i + 1][0] if i + 1 < len(parsed) else None
        duration = (
            round((nxt - at).total_seconds(), 1)
            if at is not None and nxt is not None and nxt > at
            else None
        )
        text = str(e.get("doing") or e.get("ask") or "")[:160]
        timeline.append(
            {
                "at": (
                    at.isoformat() if at else str(e.get("event_at") or e.get("created_at") or "")
                ),
                "from_state": str(parsed[i - 1][1].get("status") or "") if i > 0 else "",
                "to_state": str(e.get("status") or ""),
                "text": text,
                "duration_s": duration,
            }
        )

    state_counts = Counter(str(e.get("status") or "") for e in events)
    decisions = sum(1 for e in events if e.get("status") == "decision")
    # 승인 = decision 상태로 들어간 뒤 다른 상태로 빠져나간 횟수(응답된 ask)
    approvals = 0
    for i, e in enumerate(events):
        if e.get("status") != "decision":
            continue
        if i + 1 < len(events) and events[i + 1].get("status") != "decision":
            approvals += 1
    errors = sum(1 for e in events if str(e.get("status") or "") in ("error", "failed", "blocked"))
    tin = int(sess.get("tokens_in") or 0)
    tout = int(sess.get("tokens_out") or 0)
    has_tokens = sess.get("tokens_in") is not None or sess.get("tokens_out") is not None
    est_cost = session_est_cost_usd(sess) if has_tokens else None
    first_at = parsed[0][0] if parsed else None
    last_at = parsed[-1][0] if parsed else None
    if first_at is None:
        first_at = _parse_ts(sess.get("created_at"))
    if last_at is None:
        last_at = _parse_ts(sess.get("updated_at")) or _parse_ts(sess.get("last_event_at"))
    total_seconds = (
        round((last_at - first_at).total_seconds(), 1)
        if first_at is not None and last_at is not None and last_at > first_at
        else 0.0
    )

    # files_touched — ended_summary/artifacts의 경로 목록 (최대 50)
    files_touched: list[str] = []
    raw_artifacts = sess.get("artifacts")
    if raw_artifacts:
        try:
            items = json.loads(str(raw_artifacts))
        except (ValueError, TypeError):
            items = [raw_artifacts]
        if isinstance(items, dict):
            items = list(items.keys())
        if isinstance(items, list):
            files_touched = [str(x)[:300] for x in items[:50]]

    # 결정적 한국어 요약 — LLM 호출 없음
    tool = str(sess.get("tool") or "")
    parts = [f"이 세션({tool})은 {_fmt_duration(total_seconds)} 동안"]
    token_bits = []
    if tin:
        token_bits.append(f"입력 토큰 약 {tin / 1000:.1f}k")
    if tout:
        token_bits.append(f"출력 토큰 약 {tout / 1000:.1f}k")
    extras = []
    if decisions:
        extras.append(f"결정 요청 {decisions}회")
    if approvals:
        extras.append(f"승인 응답 {approvals}회")
    if errors:
        extras.append(f"오류 {errors}건")
    has_tail = bool(token_bits) or est_cost is not None or bool(extras)
    state_str = ", ".join(
        f"{k} {state_counts[k]}회"
        for k in ("working", "decision", "rest", "ended")
        if state_counts.get(k)
    )
    # 꼬리(토큰·비용·extras)가 없으면 연결어미 -고로 문장이 끊기지 않게 합니다체로 닫는다
    if state_str:
        parts.append(f" {state_str}의 상태 전이를 거쳤{'고' if has_tail else '습니다'}")
    else:
        parts.append(f" 전이 이벤트가 거의 없었{'고' if has_tail else '습니다'}")
    if token_bits:
        parts.append(", " + ", ".join(token_bits))
    if est_cost is not None:
        parts.append(f", 예상 비용 ${est_cost:.4f}")
    parts.append(".")
    if extras:
        parts.append(" " + ", ".join(extras) + ".")
    summary = "".join(parts)

    return {
        "session": session_view(sess),
        # 구 이벤트 포맷 호환 — 기존 GET /{tool}/{key}/history 소비자용
        "events": events,
        "timeline": timeline,
        "stats": {
            "state_counts": dict(state_counts),
            "decisions": decisions,
            "approvals": approvals,
            "errors": errors,
            "tokens_in": tin,
            "tokens_out": tout,
            "est_cost_usd": est_cost,
            "total_seconds": total_seconds,
        },
        "files_touched": files_touched,
        "summary": summary,
        "ended_summary": sess.get("ended_summary") or "",
    }
