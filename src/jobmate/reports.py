"""일일/주간 운영 리포트 생성 — GET /reports(온디맨드)와 자동 생성이 공용.

store의 기존 집계 메서드만 사용해 저렴하게 구성한다. 프롬프트/토큰 원문 등
민감값은 포함하지 않는다.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from .pricing import cost_usd
from .store.db import Store

_PERIOD_DAYS = {"daily": 1, "weekly": 7}


def build_report(store: Store, kind: str, settings: Any | None = None) -> dict[str, Any]:
    """kind='daily'|'weekly' 리포트 본문(dict)을 생성한다.

    섹션: period/runs/sessions/llm/approvals/radar/notable_failures.
    """
    if kind not in _PERIOD_DAYS:
        raise ValueError(f"unknown report kind: {kind}")
    days = _PERIOD_DAYS[kind]
    now = datetime.now(UTC)
    since_dt = now - timedelta(days=days)
    since_iso = since_dt.isoformat()

    stats = store.run_stats(None, days)
    sessions = store.agent_session_stats(since=since_iso)
    usage = store.llm_usage_summary(days=days)

    # LLM 비용 — 모델별 토큰 합계에 가격표 적용
    llm_models: list[dict[str, Any]] = []
    total_cost = 0.0
    total_in = total_out = 0
    for model, m in sorted(usage.get("models", {}).items()):
        tin = int(m.get("tokens_in") or 0)
        tout = int(m.get("tokens_out") or 0)
        cost = cost_usd(model, tin, tout)
        total_cost += cost
        total_in += tin
        total_out += tout
        llm_models.append(
            {
                "model": model,
                "calls": int(m.get("calls") or 0),
                "tokens_in": tin,
                "tokens_out": tout,
                "cost_usd": cost,
            }
        )

    # radar 신규 수집 아이템 수 (기간 내 snapshot items)
    radar_items = store.radar_items_since(since_dt.date().isoformat())
    radar_new = len({r.get("repo_full_name") for r in radar_items})

    failures = sorted((stats.get("stage_failures") or {}).items(), key=lambda kv: (-kv[1], kv[0]))

    return {
        "kind": kind,
        "generated_at": now.isoformat(),
        "period": {"days": days, "since": since_iso, "until": now.isoformat()},
        "runs": {
            "total": stats.get("runs", 0),
            "succeeded": stats.get("succeeded", 0),
            "failed": stats.get("failed", 0),
            "running": stats.get("running", 0),
            "waiting_approval": stats.get("waiting_approval", 0),
            "avg_duration_s": stats.get("avg_duration_s"),
            "tokens_in": stats.get("total_tokens_in", 0),
            "tokens_out": stats.get("total_tokens_out", 0),
            "daily": stats.get("daily", []),
        },
        "sessions": sessions,
        "llm": {
            "total_tokens_in": total_in,
            "total_tokens_out": total_out,
            "total_cost_usd": round(total_cost, 6),
            "by_model": llm_models,
        },
        "approvals": {"pending": stats.get("waiting_approval", 0)},
        "radar": {"new_items": radar_new},
        "notable_failures": [{"stage": s, "count": c} for s, c in failures[:10]],
    }
