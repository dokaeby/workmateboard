# mypy: disable-error-code="attr-defined"
"""management 서비스 공용 기반 — spec 1차 C항.

도메인 오류(ManagementError), 상태 어휘 상수, agent 응답 직렬화
(agent_out/agent_out_with_task), agent_events 기록(emit_event),
store 행 조회 헬퍼(_require_agent/_require_task)를 제공한다.
다른 services 모듈이 공유하는 leaf 모듈 — services 간 import 없음.
"""

from __future__ import annotations

import uuid
from typing import Any

from ..models import (
    AgentEventType,
    AgentKind,
    AgentStatus,
    TaskStatus,
    canonical_agent_status,
)
from ..models.enums import TaskPriority
from ..store.db import Store
from .redaction import redact_adapter_config

AGENT_STATUS_VALUES = {s.value for s in AgentStatus} | {"archived"}
TASK_STATUS_VALUES = {s.value for s in TaskStatus}
TASK_PRIORITY_VALUES = {s.value for s in TaskPriority}


class ManagementError(Exception):
    """도메인 오류 — status_code/http 의미를 그대로 담는다."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def agent_out(agent: dict[str, Any]) -> dict[str, Any]:
    """store 행 → 관리 API 응답 — canonical status + redacted config."""
    out = dict(agent)
    out["status"] = canonical_agent_status(agent.get("status"))
    out["kind"] = str(agent.get("kind") or AgentKind.WORKFLOW.value)
    out["adapter_config"] = redact_adapter_config(agent.get("adapter_config"))
    out["current_task"] = None  # 호출자가 필요하면 store.current_task_for_agent로 채움
    return out


def agent_out_with_task(store: Store, agent: dict[str, Any]) -> dict[str, Any]:
    out = agent_out(agent)
    out["current_task"] = store.current_task_for_agent(str(agent.get("id") or ""))
    return out


def emit_event(
    store: Store,
    event_type: AgentEventType | str,
    *,
    agent_id: str | None = None,
    task_id: str | None = None,
    run_id: str | None = None,
    actor: str = "",
    summary: str = "",
    detail: dict[str, Any] | None = None,
    idempotency_key: str = "",
) -> tuple[dict[str, Any] | None, bool]:
    et = event_type.value if isinstance(event_type, AgentEventType) else str(event_type)
    return store.append_agent_event(
        idempotency_key=idempotency_key or f"auto:{uuid.uuid4().hex}",
        event_type=et,
        agent_id=agent_id,
        task_id=task_id,
        run_id=run_id,
        actor=actor,
        summary=summary[:200],
        detail=detail or {},
    )


def row_org_id(store: Store, table: str, row_id: str) -> str:
    """행의 org_id를 org 컨텍스트 필터와 무관하게 직접 읽는다 (spec 003 FR-019).

    스케줄러·스레드 경계처럼 contextvar이 전파되지 않는 경로에서 "이 행이
    속한 조직"을 org_scope로 재적용할 때 사용한다. table은 이 모듈의
    고정 호출자(api/jobs, services/watchdog)만 넘기는 내부 식별자다."""
    try:
        row = store._conn.execute(  # noqa: SLF001 — org 필터를 우회하는 단건 조회
            f"SELECT org_id FROM {table} WHERE id=?",  # noqa: S608 — table은 내부 고정 식별자, 값은 바인딩
            (row_id,),
        ).fetchone()
    except Exception:
        return ""
    if row is None:
        return ""
    try:
        return str(row["org_id"] or "")
    except Exception:
        return ""


def _require_agent(store: Store, agent_id: str) -> dict[str, Any]:
    agent = store.get_agent(agent_id)
    if agent is None:
        raise ManagementError(404, "not_found", "agent not found")
    return agent


def _require_task(store: Store, task_id: str) -> dict[str, Any]:
    task = store.get_task(task_id)
    if task is None:
        raise ManagementError(404, "not_found", "task not found")
    return task
