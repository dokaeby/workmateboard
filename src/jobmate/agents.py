"""Agent registry 서비스 — 검증/조직도 규칙/AgentOut 조립.

store는 행 조회·집계만 담당하고, 어댑터/상태 enum 검증과 reports_to
사이클·아카이브 규칙, 30일 통계 집계는 여기서 수행한다(라우터는 얇게).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from .pricing import cost_usd
from .store.db import Store

# bridge_* 스캐너가 push하는 tool 값(agent_bridge.py)과 동일 — 'custom'은
# 레지스트리에만 존재하는 수동 등록 어댑터.
ADAPTER_TYPES = ("claude", "codex", "gemini", "devin", "custom")
AGENT_STATUSES = ("active", "paused", "archived")
NAME_MAX = 100
STATS_WINDOW_DAYS = 30
# name 중복은 409, 나머지 검증 실패는 422 — 라우터가 이 상수로 구분한다.
ERR_NAME_CONFLICT = "agent name already exists"


def validate_reports_to(store: Store, agent_id: str | None, reports_to: str) -> str | None:
    """reports_to 규칙 검증 — 에러 메시지 또는 None.

    - 대상 에이전트가 존재해야 함
    - archived 에이전트는 상위로 둘 수 없음 — 단, PATCH가 기존 reports_to
      값을 그대로 재전송하는 no-op이면 허용한다(프론트가 현재값을 항상
      함께 보내므로, archived 상위를 가진 기존 행의 다른 필드 수정이
      422로 막히는 것을 방지). 새로 archived를 상위로 지정하는 것은 거부.
    - 자기 자신 지정 불가
    - 순환 참조 불가 (a→b→a 등 — reports_to 체인을 따라 올라가며 판정)
    """
    target = reports_to.strip()
    if not target:
        return "reports_to must not be empty"
    if agent_id is not None and target == agent_id:
        return "agent cannot report to itself"
    if agent_id is not None:
        cur = store.get_agent(agent_id)
        if cur is not None and str(cur.get("reports_to") or "") == target:
            return None  # no-op — 기존 상위 유지(archived 상위 포함 재검증 생략)
    parent = store.get_agent(target)
    if parent is None:
        return "reports_to agent not found"
    if str(parent.get("status") or "") == "archived":
        return "archived agent cannot be a reports_to target"
    if agent_id is not None and store.reports_to_would_cycle(agent_id, target):
        return "reports_to would create a cycle"
    return None


def clean_capabilities(raw: Any) -> list[str] | None:
    """capabilities 입력 정규화 — str 항목만, 공백 제거, 최대 50개.
    잘못된 타입이면 None."""
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        return None
    return [str(c).strip()[:100] for c in raw if str(c).strip()][:50]


def validate_agent_fields(
    store: Store,
    fields: dict[str, Any],
    *,
    agent_id: str | None = None,
) -> str | None:
    """create/patch 공용 필드 검증 — 에러 메시지 또는 None.

    fields는 이미 스키마 검증을 통과한 값 dict. fields에 없는 키는
    검증하지 않는다(PATCH 의미 — 호출자가 model_fields_set으로 거른다)."""
    if "name" in fields:
        name = str(fields["name"] or "").strip()
        if not name:
            return "name must not be empty"
        if len(name) > NAME_MAX:
            return f"name must be at most {NAME_MAX} chars"
        other = store.find_agent_by_name(name)
        if other is not None and other["id"] != agent_id:
            return ERR_NAME_CONFLICT
    if "adapter_type" in fields:
        at = str(fields["adapter_type"] or "").strip().lower()
        if at not in ADAPTER_TYPES:
            return f"adapter_type must be one of {list(ADAPTER_TYPES)}"
    if "status" in fields:
        st = str(fields["status"] or "").strip().lower()
        if st not in AGENT_STATUSES:
            return f"status must be one of {list(AGENT_STATUSES)}"
    if "monthly_budget_usd" in fields:
        mb = fields["monthly_budget_usd"]
        if mb is not None:
            try:
                if float(mb) < 0:
                    return "monthly_budget_usd must be >= 0"
            except (TypeError, ValueError):
                return "monthly_budget_usd must be a number"
    if "capabilities" in fields:
        if clean_capabilities(fields["capabilities"]) is None:
            return "capabilities must be a list of strings"
    if "reports_to" in fields:
        rt = fields["reports_to"]
        if rt is not None and str(rt).strip():
            err = validate_reports_to(store, agent_id, str(rt))
            if err is not None:
                return err
    return None


def stats_by_agent(store: Store) -> tuple[dict[str, dict[str, Any]], dict[str, float]]:
    """(run 통계 맵, agent별 30일 비용 맵) — AgentOut 집계용 1회 계산."""
    since = (datetime.now(UTC) - timedelta(days=STATS_WINDOW_DAYS)).isoformat()
    run_stats = store.agent_run_stats(since)
    cost: dict[str, float] = {}
    for r in store.agent_spend_rows(since):
        aid = str(r.get("agent_id") or "")
        if not aid:
            continue
        cost[aid] = cost.get(aid, 0.0) + cost_usd(
            str(r.get("model") or ""),
            int(r.get("tokens_in") or 0),
            int(r.get("tokens_out") or 0),
        )
    return run_stats, cost


def agent_out(store: Store, agent: dict[str, Any]) -> dict[str, Any]:
    """store 행 → API 계약 AgentOut. stats는 호출자가 주입 가능.

    stats=None이면 개별 조회용으로 30일 윈도우를 새로 계산한다(목록에서는
    agents_payload가 한 번만 계산해 주입)."""
    run_stats, cost_map = stats_by_agent(store)
    return agent_out_with_stats(agent, run_stats.get(agent["id"], {}), cost_map)


def agent_out_with_stats(
    agent: dict[str, Any],
    run_stat: dict[str, Any],
    cost_map: dict[str, float],
) -> dict[str, Any]:
    """AgentOut 조립 — stats 계산을 호출자에게 위임(목록 N+1 방지)."""
    aid = str(agent.get("id") or "")
    return {
        "id": aid,
        "name": agent.get("name") or "",
        "adapter_type": agent.get("adapter_type") or "",
        "role": agent.get("role") or "",
        "description": agent.get("description") or "",
        "capabilities": list(agent.get("capabilities") or []),
        "reports_to": agent.get("reports_to") or None,
        "status": agent.get("status") or "active",
        "monthly_budget_usd": agent.get("monthly_budget_usd"),
        "created_at": agent.get("created_at"),
        "updated_at": agent.get("updated_at"),
        "runs_30d": int(run_stat.get("runs_30d") or 0),
        "cost_30d_usd": round(float(cost_map.get(aid, 0.0)), 6),
        "last_run_at": run_stat.get("last_run_at"),
    }


def agents_payload(store: Store) -> dict[str, Any]:
    """GET /api/v1/agents 응답 — stats를 한 번만 집계해 모든 행에 적용."""
    run_stats, cost_map = stats_by_agent(store)
    items = [
        agent_out_with_stats(a, run_stats.get(str(a.get("id") or ""), {}), cost_map)
        for a in store.list_agents()
    ]
    return {"items": items}
