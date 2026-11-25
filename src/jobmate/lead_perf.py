"""성과 비교 집계 — perf_compare (GET /perf/compare).

Store 읽기 + 순수 함수, 상태 변경 없음.
"""

from __future__ import annotations

from collections import Counter
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from .lead_common import (
    _TERMINAL_BAD,
    _TERMINAL_OK,
    _now,
    _parse_ts,
    session_health,
)
from .pricing import cost_usd

if TYPE_CHECKING:
    from .store.db import Store


# ---------- 성과 비교 (GET /perf/compare) ----------


def _stage_agent_map(workflow_key: str) -> dict[str, str]:
    """stage_key → agent spec key (WORKFLOWS 기준)."""
    try:
        from .workflows import WORKFLOWS
    except Exception:
        return {}
    spec = WORKFLOWS.get(workflow_key)
    if spec is None:
        return {}
    return {s.key: (s.agent or "") for s in spec.stages}


def _dominant_model(models: list[str]) -> str:
    """run의 stage model 중 최빈값 (동률이면 먼저 등장)."""
    counts: Counter[str] = Counter(m for m in models if m)
    if not counts:
        return ""
    best = counts.most_common()
    top = best[0][1]
    for m, c in best:
        if c == top:
            return m
    return best[0][0]


def perf_compare(store: Store, workflow: str = "", group_by: str = "model") -> dict[str, Any]:
    """GET /perf/compare — run을 model/agent/tool로 그룹핑한 성공률/비용 비교."""
    group_by = group_by if group_by in ("model", "agent", "tool") else "model"
    runs = store.list_runs(limit=500, workflow=workflow or None)
    run_ids = {r.id for r in runs}
    created_dts = [
        d for d in (_parse_ts(getattr(r, "created_at", None)) for r in runs) if d is not None
    ]
    oldest = min(created_dts) if created_dts else None
    since = (
        (oldest - timedelta(days=1)).isoformat()
        if oldest
        else (_now() - timedelta(days=400)).isoformat()
    )
    stage_rows = [r for r in store.stage_run_rows(since) if str(r.get("run_id")) in run_ids]
    by_run: dict[str, list[dict[str, Any]]] = {}
    for r in stage_rows:
        by_run.setdefault(str(r["run_id"]), []).append(r)

    groups: dict[str, dict[str, Any]] = {}
    for run in runs:
        stages = by_run.get(run.id, [])
        models = [str(s.get("model") or "") for s in stages]
        if group_by == "model":
            group = _dominant_model(models) or "(unknown)"
        elif group_by == "agent":
            agent_map = _stage_agent_map(run.workflow_key)
            # 토큰 최다 stage의 agent — 없으면 첫 llm stage, 그래도 없으면 unknown
            best_stage = max(
                stages,
                key=lambda s: int(s.get("tokens_in") or 0) + int(s.get("tokens_out") or 0),
                default=None,
            )
            group = ""
            if best_stage is not None:
                group = agent_map.get(str(best_stage.get("stage_key") or ""), "")
            if not group:
                for s in stages:
                    group = agent_map.get(str(s.get("stage_key") or ""), "")
                    if group:
                        break
            group = group or "(unknown)"
        else:  # tool — 생성 주체 분류 (trigger/schedule/api-key/user)
            cb = str(getattr(run, "created_by", "") or "")
            if cb.startswith("trigger:"):
                group = "trigger"
            elif cb.startswith("schedule:"):
                group = "schedule"
            elif cb.startswith("radar"):
                group = "radar"
            elif cb in ("api-key", "local", ""):
                group = cb or "(system)"
            elif "@" in cb:
                group = "user"
            else:
                group = cb[:40] or "(system)"

        g = groups.setdefault(
            group,
            {
                "group": group,
                "run_count": 0,
                "succeeded": 0,
                "failed": 0,
                "durations": [],
                "tokens": 0,
                "cost": 0.0,
                "failures_by_stage": {},
            },
        )
        g["run_count"] += 1
        st = str(getattr(run, "status", "") or "")
        if st in _TERMINAL_OK:
            g["succeeded"] += 1
        elif st in _TERMINAL_BAD:
            g["failed"] += 1
        created = _parse_ts(getattr(run, "created_at", None))
        updated = _parse_ts(getattr(run, "updated_at", None))
        if st in (_TERMINAL_OK | _TERMINAL_BAD) and created and updated:
            g["durations"].append((updated - created).total_seconds())

        # run 단위 stage 집계 — 최신 attempt 실패만 failures_by_stage에 반영
        latest_attempt: dict[str, tuple[int, str]] = {}
        for s in stages:
            skey = str(s.get("stage_key") or "")
            att = int(s.get("attempt") or 0)
            if skey not in latest_attempt or att > latest_attempt[skey][0]:
                latest_attempt[skey] = (att, str(s.get("status") or ""))
            g["tokens"] += int(s.get("tokens_in") or 0) + int(s.get("tokens_out") or 0)
            g["cost"] += cost_usd(
                str(s.get("model") or ""),
                int(s.get("tokens_in") or 0),
                int(s.get("tokens_out") or 0),
            )
        for skey, (_a, stt) in latest_attempt.items():
            if stt == "failed":
                g["failures_by_stage"][skey] = g["failures_by_stage"].get(skey, 0) + 1

    items: list[dict[str, Any]] = []
    for g in groups.values():
        terminal = g["succeeded"] + g["failed"]
        items.append(
            {
                "group": g["group"],
                "run_count": g["run_count"],
                "success_rate": (round(g["succeeded"] / terminal * 100, 1) if terminal else None),
                "avg_seconds": (
                    round(sum(g["durations"]) / len(g["durations"]), 1) if g["durations"] else None
                ),
                "total_tokens": g["tokens"],
                "total_cost_usd": round(g["cost"], 6),
                "failures_by_stage": g["failures_by_stage"],
            }
        )
    items.sort(key=lambda x: (-x["run_count"], x["group"]))

    # 보조 섹션 — tool별 세션 통계 (agent_session_stats 재사용)
    sess_stats = store.agent_session_stats()
    tool_sessions: dict[str, dict[str, int]] = {}
    for s in store.list_agent_sessions(limit=500, include_ended=True):
        t = str(s.get("tool") or "")
        b = tool_sessions.setdefault(t, {"session_count": 0, "active": 0, "ended": 0})
        b["session_count"] += 1
        if str(s.get("status") or "") == "ended":
            b["ended"] += 1
        elif session_health(s) == "active":
            b["active"] += 1
    session_items: list[dict[str, Any]] = []
    for tool, b in tool_sessions.items():
        tool_stats = sess_stats.get(tool) or {}
        work = int(tool_stats.get("working_seconds") or 0)
        session_items.append(
            {
                "tool": tool,
                "session_count": b["session_count"],
                "active": b["active"],
                "ended": b["ended"],
                "avg_work_seconds": round(work / max(b["session_count"], 1), 1),
            }
        )
    session_items.sort(key=lambda x: (-x["session_count"], x["tool"]))

    return {
        "workflow": workflow or "",
        "group_by": group_by,
        "items": items,
        "sessions": {"items": session_items},
    }
