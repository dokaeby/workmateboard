"""Agent 관리 계층 상태 전이 — allowlist 테이블과 순수 검증 함수.

spec: docs/paperclip-agent-management-spec.md 1차 A항.
모든 상태 전이는 여기의 테이블만이 근거이며, 서비스/API 계층은
can_transition_* 결과가 False이면 409로 거부한다.

레거시 호환: agents.status에는 신규 7값 외에 기존 운영값
'archived'가 남아 있을 수 있다. 'archived'는 'terminated'의 저장
별칭으로 canonical_agent_status()가 정규화한다 —
terminated는 terminal이며 어떤 상태로도 되돌아갈 수 없다.
"""

from __future__ import annotations

from .enums import AgentStatus, TaskStatus

# 저장소에 남을 수 있는 레거시 terminal 표기 → canonical 상태.
_LEGACY_STATUS_ALIASES = {"archived": AgentStatus.TERMINATED.value}

_AGENT_TRANSITIONS: dict[str, frozenset[str]] = {
    AgentStatus.PENDING_APPROVAL.value: frozenset(
        {AgentStatus.ACTIVE.value, AgentStatus.TERMINATED.value}
    ),
    AgentStatus.ACTIVE.value: frozenset(
        {
            AgentStatus.RUNNING.value,
            AgentStatus.PAUSED.value,
            AgentStatus.ERROR.value,
            AgentStatus.TERMINATED.value,
        }
    ),
    AgentStatus.IDLE.value: frozenset(
        {
            AgentStatus.RUNNING.value,
            AgentStatus.PAUSED.value,
            AgentStatus.ERROR.value,
            AgentStatus.TERMINATED.value,
        }
    ),
    AgentStatus.RUNNING.value: frozenset(
        {
            AgentStatus.IDLE.value,
            AgentStatus.PAUSED.value,
            AgentStatus.ERROR.value,
            AgentStatus.TERMINATED.value,
        }
    ),
    AgentStatus.PAUSED.value: frozenset({AgentStatus.ACTIVE.value, AgentStatus.TERMINATED.value}),
    AgentStatus.ERROR.value: frozenset(
        {AgentStatus.ACTIVE.value, AgentStatus.PAUSED.value, AgentStatus.TERMINATED.value}
    ),
    AgentStatus.TERMINATED.value: frozenset(),
}

_TASK_TRANSITIONS: dict[str, frozenset[str]] = {
    TaskStatus.BACKLOG.value: frozenset({TaskStatus.TODO.value, TaskStatus.CANCELLED.value}),
    TaskStatus.TODO.value: frozenset(
        {TaskStatus.IN_PROGRESS.value, TaskStatus.BLOCKED.value, TaskStatus.CANCELLED.value}
    ),
    TaskStatus.IN_PROGRESS.value: frozenset(
        {
            TaskStatus.IN_REVIEW.value,
            TaskStatus.WAITING_APPROVAL.value,
            TaskStatus.BLOCKED.value,
            TaskStatus.DONE.value,
            TaskStatus.CANCELLED.value,
        }
    ),
    TaskStatus.IN_REVIEW.value: frozenset(
        {
            TaskStatus.IN_PROGRESS.value,
            TaskStatus.WAITING_APPROVAL.value,
            TaskStatus.DONE.value,
            TaskStatus.BLOCKED.value,
            TaskStatus.CANCELLED.value,
        }
    ),
    TaskStatus.WAITING_APPROVAL.value: frozenset(
        {TaskStatus.IN_PROGRESS.value, TaskStatus.DONE.value, TaskStatus.CANCELLED.value}
    ),
    TaskStatus.BLOCKED.value: frozenset(
        {TaskStatus.TODO.value, TaskStatus.IN_PROGRESS.value, TaskStatus.CANCELLED.value}
    ),
    TaskStatus.DONE.value: frozenset(),
    TaskStatus.CANCELLED.value: frozenset(),
}

# 새 작업(wake/checkout)을 시작할 수 있는 agent 상태 — paused/error/
# pending_approval/terminated는 spec Review Focus상 신규 작업 불가.
AGENT_WORK_STATUSES = frozenset(
    {AgentStatus.ACTIVE.value, AgentStatus.IDLE.value, AgentStatus.RUNNING.value}
)


def canonical_agent_status(status: str | None) -> str:
    """저장된 status 표기를 canonical AgentStatus 값으로 정규화.

    알 수 없는 값은 소문자 trim 후 그대로 반환한다 — 호출부의 enum 검증이
    최종 방어선이 된다."""
    s = str(status or "").strip().lower()
    return _LEGACY_STATUS_ALIASES.get(s, s)


def is_agent_terminal(status: str | None) -> bool:
    return canonical_agent_status(status) == AgentStatus.TERMINATED.value


def can_transition_agent(from_status: str | None, to_status: str | None) -> bool:
    """agent status 전이 허용 여부 — 동일 상태는 no-op으로 허용."""
    frm = canonical_agent_status(from_status)
    to = canonical_agent_status(to_status)
    if frm == to:
        return True
    return to in _AGENT_TRANSITIONS.get(frm, frozenset())


def agent_transition_targets(from_status: str | None) -> frozenset[str]:
    return _AGENT_TRANSITIONS.get(canonical_agent_status(from_status), frozenset())


def can_transition_task(from_status: str | None, to_status: str | None) -> bool:
    """task status 전이 허용 여부 — 동일 상태는 no-op으로 허용."""
    frm = str(from_status or "").strip().lower()
    to = str(to_status or "").strip().lower()
    if frm == to:
        return True
    return to in _TASK_TRANSITIONS.get(frm, frozenset())


def task_transition_targets(from_status: str | None) -> frozenset[str]:
    return _TASK_TRANSITIONS.get(str(from_status or "").strip().lower(), frozenset())
