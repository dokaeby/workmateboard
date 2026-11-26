"""일일/주간/임원 리포트 생성 — GET /reports(온디맨드)와 자동 생성이 공용.

store의 기존 집계 메서드 + lead.py의 리스크/승인 큐 집계를 사용해 저렴하게
구성한다. 프롬프트/토큰 원문 등 민감값은 포함하지 않는다.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from .lead import approval_queue, detect_risks
from .pricing import cost_usd
from .store.db import Store

_PERIOD_DAYS = {"daily": 1, "weekly": 7, "exec_daily": 1, "exec_weekly": 7}
_EXEC_KINDS = ("exec_daily", "exec_weekly")


def _base_stats(store: Store, days: int) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """run_stats + 세션 통계 + llm 사용량 요약 (기간 days일)."""
    since_iso = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    return (
        store.run_stats(None, days),
        store.agent_session_stats(since=since_iso),
        store.llm_usage_summary(days=days),
    )


def _llm_cost(usage: dict[str, Any]) -> tuple[list[dict[str, Any]], float, int, int]:
    """모델별 토큰 합계에 가격표 적용 → (by_model, total_cost, tokens_in, tokens_out)."""
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
    return llm_models, total_cost, total_in, total_out


def _period_block(kind: str, days: int, now: datetime) -> dict[str, Any]:
    since_dt = now - timedelta(days=days)
    return {
        "days": days,
        "since": since_dt.isoformat(),
        "until": now.isoformat(),
    }


def _build_exec_report(store: Store, kind: str, days: int, now: datetime) -> dict[str, Any]:
    """팀장용 내러티브 리포트 — run/세션/비용/승인/리스크/하이라이트.

    daily/weekly와 동일한 호출자 계약을 유지하기 위해 runs.total/failed와
    approvals.pending 키를 포함한다(인박스/메신저 알림이 이 키를 읽는다)."""
    stats, sessions, usage = _base_stats(store, days)
    llm_models, total_cost, total_in, total_out = _llm_cost(usage)

    # 세션 도구별 합계
    by_tool: dict[str, dict[str, int]] = {}
    for tool, s in (sessions or {}).items():
        b = by_tool.setdefault(
            tool, {"sessions": 0, "working_seconds": 0, "decisions": 0, "ended": 0}
        )
        b["sessions"] += int(s.get("sessions") or 0)
        b["working_seconds"] += int(s.get("working_seconds") or 0)
        b["decisions"] += int(s.get("decisions") or 0)
        b["ended"] += int(s.get("ended") or 0)
    tools_sorted = sorted(by_tool.items(), key=lambda kv: (-kv[1]["working_seconds"], kv[0]))

    # 승인 큐/리스크 (현재 스냅샷 — 기간 무관)
    approvals = approval_queue(store)
    risks = detect_risks(store)
    risk_items = risks.get("items") or []
    pending_approvals = len(approvals.get("items") or [])

    # 실패 원인 상위 — stage_failures
    failures = sorted((stats.get("stage_failures") or {}).items(), key=lambda kv: (-kv[1], kv[0]))

    succeeded = int(stats.get("succeeded") or 0)
    failed = int(stats.get("failed") or 0)
    total_runs = int(stats.get("runs") or 0)
    waiting = int(stats.get("waiting_approval") or 0)
    total_sessions = sum(b["sessions"] for _t, b in tools_sorted)

    # 하이라이트 — 결정적 한국어 문장
    highlights: list[str] = []
    if total_runs:
        highlights.append(
            f"기간 내 run {total_runs}건: 성공 {succeeded}, 실패 {failed}"
            + (f", 승인 대기 {waiting}" if waiting else "")
        )
    if tools_sorted:
        top_tool, top_b = tools_sorted[0]
        highlights.append(
            f"가장 활동적인 도구: {top_tool} (세션 {top_b['sessions']}, "
            f"작업 {_fmt_hm(top_b['working_seconds'])})"
        )
    if failures:
        top_stage, top_n = failures[0]
        highlights.append(f"주요 실패 단계: {top_stage} ({top_n}회)")
    if pending_approvals:
        highlights.append(f"승인 대기 {pending_approvals}건, 확인이 필요합니다")
    if risk_items:
        sev = str(risk_items[0].get("severity") or "")
        highlights.append(
            f"감지된 리스크 {len(risk_items)}건 (최고 심각도 {sev}): "
            f"{risk_items[0].get('title', '')}"
        )
    highlights.append(f"LLM 추정 비용 ${total_cost:.4f} (토큰 {total_in + total_out:,})")

    # 내러티브 — 팀장 보고용 요약 문단
    label = "일일" if kind == "exec_daily" else "주간"
    narrative = (
        f"[{label} 임원 리포트] {days}일간 run {total_runs}건"
        f"(성공 {succeeded}·실패 {failed}·승인 대기 {waiting}), "
        f"에이전트 세션 {total_sessions}건, LLM 추정 비용 ${total_cost:.4f}. "
        f"승인 대기 {pending_approvals}건과 리스크 {len(risk_items)}건이 남아 있습니다."
    )

    return {
        "kind": kind,
        "generated_at": now.isoformat(),
        "period": _period_block(kind, days, now),
        "narrative": narrative,
        "highlights": highlights,
        "runs": {
            "total": total_runs,
            "succeeded": succeeded,
            "failed": failed,
            "running": int(stats.get("running") or 0),
            "waiting_approval": waiting,
            "avg_duration_s": stats.get("avg_duration_s"),
            "tokens_in": stats.get("total_tokens_in", 0),
            "tokens_out": stats.get("total_tokens_out", 0),
            "daily": stats.get("daily", []),
        },
        "sessions": {
            "total_sessions": total_sessions,
            "by_tool": [{"tool": t, **b} for t, b in tools_sorted],
        },
        "llm": {
            "total_tokens_in": total_in,
            "total_tokens_out": total_out,
            "total_cost_usd": round(total_cost, 6),
            "by_model": llm_models,
        },
        "approvals": {
            "pending": pending_approvals,
            "waiting_approval_runs": waiting,
            "items": approvals.get("items") or [],
        },
        "risks": {
            "count": len(risk_items),
            "items": risk_items[:10],
        },
        "notable_failures": [{"stage": s, "count": c} for s, c in failures[:10]],
    }


def _fmt_hm(seconds: int) -> str:
    m = int(seconds) // 60
    if m < 60:
        return f"{m}분"
    h, rem = divmod(m, 60)
    return f"{h}시간 {rem}분" if rem else f"{h}시간"


def build_report(store: Store, kind: str, settings: Any | None = None) -> dict[str, Any]:
    """kind='daily'|'weekly'|'exec_daily'|'exec_weekly' 리포트 본문(dict)을 생성한다.

    daily/weekly 섹션: period/runs/sessions/llm/approvals/radar/notable_failures.
    exec_* 섹션: narrative/highlights/runs/sessions/llm/approvals/risks.
    """
    if kind not in _PERIOD_DAYS:
        raise ValueError(f"unknown report kind: {kind}")
    days = _PERIOD_DAYS[kind]
    now = datetime.now(UTC)
    if kind in _EXEC_KINDS:
        return _build_exec_report(store, kind, days, now)

    since_dt = now - timedelta(days=days)
    since_iso = since_dt.isoformat()

    stats = store.run_stats(None, days)
    sessions = store.agent_session_stats(since=since_iso)
    usage = store.llm_usage_summary(days=days)

    llm_models, total_cost, total_in, total_out = _llm_cost(usage)

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
