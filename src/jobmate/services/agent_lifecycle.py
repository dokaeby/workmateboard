# mypy: disable-error-code="attr-defined"
"""agent lifecycle — spec 1차 C항.

agent status 전이 검증(models.management allowlist), 생성 필드 검증,
전이 가능할 때만 조용히 status를 바꾸는 _set_agent_status_quiet을 담당한다.
인증 정책은 라우터 책임 — 이 모듈은 "machine" 여부를 모른다.
"""

from __future__ import annotations

from typing import Any

from ..models import (
    AgentEventType,
    AgentKind,
    AgentStatus,
    ApprovalActionType,
    can_transition_agent,
    canonical_agent_status,
)
from ..store.db import Store
from .management_common import (
    AGENT_STATUS_VALUES,
    ManagementError,
    _require_agent,
    agent_out,
    emit_event,
)


def transition_agent(
    store: Store,
    agent_id: str,
    to_status: str,
    *,
    actor: str = "",
    terminate_authorized: bool = False,
) -> dict[str, Any]:
    """agent status 전이 — allowlist 위반/terminal 출발은 409.

    terminated 전이는 spec 2차 D의 agent_terminate 승인이 필요하다 —
    approved approval_request(agent/agent_id)가 있거나, 승인 부수효과
    경로가 terminate_authorized=True로 호출할 때만 허용한다."""
    agent = _require_agent(store, agent_id)
    to = canonical_agent_status(to_status)
    if to not in AGENT_STATUS_VALUES or to == "archived":
        raise ManagementError(422, "validation", f"invalid agent status: {to_status}")
    frm = canonical_agent_status(agent.get("status"))
    if to == AgentStatus.TERMINATED.value and to != frm and not terminate_authorized:
        if not store.has_decided_approval(
            ApprovalActionType.AGENT_TERMINATE.value, "agent", agent_id
        ):
            raise ManagementError(
                409,
                "approval_required",
                "agent terminate requires an approved agent_terminate approval_request",
            )
    if not can_transition_agent(frm, to):
        raise ManagementError(
            409,
            "invalid_transition",
            f"cannot transition agent from {frm} to {to}",
        )
    if to == frm:
        return agent_out(agent)
    # CAS — 읽기~쓰기 사이 status가 바뀌었으면(특히 terminated) 덮어쓰지
    # 않는다. 경합 패자는 409로 재시도 가능하게 한다.
    updated = store.transition_agent_status(agent_id, frm, to)
    if updated is None:
        fresh = store.get_agent(agent_id)
        if fresh is not None and canonical_agent_status(fresh.get("status")) == to:
            return agent_out(fresh)  # 경합 중 동일 목표에 도달 — no-op
        raise ManagementError(
            409,
            "invalid_transition",
            "agent status changed concurrently; retry",
        )
    emit_event(
        store,
        AgentEventType.AGENT_STATUS_CHANGED,
        agent_id=agent_id,
        actor=actor,
        summary=f"agent {frm} -> {to}",
        detail={"from": frm, "to": to},
    )
    return agent_out(updated or agent)


def validate_agent_create_fields(fields: dict[str, Any]) -> str | None:
    """management POST /agents 검증 — 에러 메시지 또는 None."""
    status = fields.get("status")
    if status is not None:
        st = canonical_agent_status(str(status))
        if st not in AGENT_STATUS_VALUES or st == "archived":
            return f"status must be one of {sorted(AGENT_STATUS_VALUES - {'archived'})}"
    kind = fields.get("kind")
    if kind is not None and str(kind).strip().lower() not in (
        AgentKind.WORKFLOW.value,
        AgentKind.EXTERNAL.value,
    ):
        return "kind must be one of ['workflow', 'external']"
    cfg = fields.get("adapter_config")
    if cfg is not None and not isinstance(cfg, dict):
        return "adapter_config must be an object"
    return None


def _set_agent_status_quiet(store: Store, agent_id: str, to: str) -> None:
    """전이 가능할 때만 agent status 변경 — 이벤트는 호출자가 필요 시 기록.
    CAS 전이 사용 — 경합/이미 전이된 경우 조용히 건너뛴다."""
    agent = store.get_agent(agent_id)
    if agent is None:
        return
    frm = canonical_agent_status(agent.get("status"))
    if frm != to and can_transition_agent(frm, to):
        store.transition_agent_status(agent_id, frm, to)
