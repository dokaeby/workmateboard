# mypy: disable-error-code="attr-defined"
"""run → task/agent 상태 투영 — spec 1차 E항.

terminal/waiting 전이 지점에서 호출되는 project_run_status와 내부 투영
로직을 담당한다. 어떤 단계에서 실패해도 run 결과를 유실시키지 않도록
내부에서 예외를 삼키고 warning 로그만 남긴다 — 투영은 복구 가능한
부수효과다.
"""

from __future__ import annotations

import logging
from typing import Any

from ..models import (
    AgentEventType,
    AgentStatus,
    RunStatus,
    TaskStatus,
    can_transition_agent,
    can_transition_task,
    canonical_agent_status,
)
from ..store.db import Store
from .management_common import emit_event
from .task_ops import (
    _TASK_EVENT_FOR_STATUS,
    _TASK_TERMINAL,
    _advance_task_status,
    _finalize_terminal_task,
)

logger = logging.getLogger("jobmate.management")

# run 상태 → task 목표 상태 (project_run_status).
_RUN_TASK_MAP = {
    RunStatus.RUNNING.value: TaskStatus.IN_PROGRESS.value,
    RunStatus.WAITING_APPROVAL.value: TaskStatus.WAITING_APPROVAL.value,
    RunStatus.WAITING_REVIEW.value: TaskStatus.WAITING_APPROVAL.value,
    RunStatus.COMPLETED.value: TaskStatus.DONE.value,
}
_RUN_TASK_TERMINAL_BLOCKED = {
    RunStatus.FAILED.value,
    RunStatus.REJECTED.value,
    RunStatus.CANCELLED.value,
    RunStatus.BUDGET_EXHAUSTED.value,
    RunStatus.NEEDS_HUMAN.value,
}
# 재개되지 않는 run 상태 — replay된 run이 이 상태면 task/agent를
# active로 되돌리지 않고 투영으로 종결 상태를 복원한다(H4).
_RUN_TERMINAL = _RUN_TASK_TERMINAL_BLOCKED | {RunStatus.COMPLETED.value}


def project_run_status(store: Store, run_id: str, *, actor: str = "system") -> None:
    """run 상태 → 연결된 task/agent 상태 투영 (E항 hook).

    terminal/waiting 전이 지점에서 호출된다. 어떤 단계에서 실패해도 run
    결과를 유실시키지 않도록 내부에서 예외를 삼키고 warning 로그만 남긴다 —
    투영은 복구 가능한 부수효과다."""
    try:
        _project_run_status_inner(store, run_id, actor=actor)
    except Exception:
        logger.warning("run->task projection failed run=%s", run_id, exc_info=True)


def _project_run_status_inner(store: Store, run_id: str, *, actor: str) -> None:
    run = store.get_run(run_id)
    if run is None:
        return
    rs = str(getattr(run.status, "value", run.status) or "")
    if not rs:
        return
    tasks = store.tasks_for_run(run_id)
    if not tasks:
        # task_runs 링크가 없는 run은 투영 대상이 아니다 —
        # 일반 /api/v1/runs 실행이 관리 task에 영향을 주지 않게 한다.
        return

    desired = _RUN_TASK_MAP.get(rs)
    if desired is None and rs in _RUN_TASK_TERMINAL_BLOCKED:
        desired = TaskStatus.BLOCKED.value
    if desired is None:
        # queued 등 — task는 건드리지 않고 agent 상태만 투영
        _project_agent_status(store, run, rs)
        return

    for task in tasks:
        tid = str(task["id"])
        # task_runs 링크는 이력이라 남지만 current_run_id가 다른 run을
        # 가리키면 이 run의 상태를 투영하면 안 된다 — stale 링크가
        # 최신 task 상태를 덮어쓰는 것을 막는다(L10).
        # 단, 가리키는 run이 이미 terminal/삭제됐으면 포인터 자체가
        # stale이다 — 이 run이 실제 현재 작업이므로 포인터를 교체하고
        # 투영을 진행한다(재시도 run의 projection이 영구 스킵되는
        # 데드락 방지).
        cur_run = str(task.get("current_run_id") or "")
        if cur_run and cur_run != run_id:
            cur_run_obj = store.get_run(cur_run)
            cur_run_status = ""
            if cur_run_obj is not None:
                cur_run_status = str(
                    getattr(cur_run_obj.status, "value", getattr(cur_run_obj, "status", "")) or ""
                )
            if cur_run_status and cur_run_status not in _RUN_TERMINAL:
                continue
            store.update_task(tid, current_run_id=run_id)
        cur = str(task.get("status") or "")
        new_status = task
        transitioned = False
        if desired == cur:
            pass  # 이미 목표 상태 — 아래 terminal 정리만 수행
        elif desired == TaskStatus.IN_PROGRESS.value:
            new_status = _advance_task_status(store, tid, desired) or task
            transitioned = True
        elif can_transition_task(cur, desired):
            new_status = store.update_task(tid, status=desired) or task
            transitioned = True
        else:
            logger.info("run projection skipped task=%s %s->%s run=%s", tid, cur, desired, run_id)
        new_st = str(new_status.get("status") or "")
        # terminal 정리 — 전이 성공·이미 종결·전이 불가 여부와 무관하게
        # checkout/current_run_id 잔여를 회수한다. 수동 PATCH나 heartbeat로
        # 먼저 종결된 task도 여기서 정리된다(M5 — 과거 desired==cur
        # early-continue가 정리를 건너뛰던 경로 포함). lifecycle 이벤트는
        # term:{task}:{status} 키로 모든 경로가 공유해 중복 기록되지 않는다.
        if new_st in _TASK_TERMINAL or cur in _TASK_TERMINAL:
            _finalize_terminal_task(
                store,
                tid,
                actor=actor,
                run_id=run_id,
                via="run_projection",
            )
            new_status = store.get_task(tid) or new_status
            new_st = str(new_status.get("status") or "")
        if not transitioned or new_st in _TASK_TERMINAL:
            # terminal 전이는 위 finalize가 lifecycle 이벤트를 기록했다 —
            # proj 이벤트와 중복 방지로 건너뛴다.
            continue
        ev_type = _TASK_EVENT_FOR_STATUS.get(new_st, "")
        if ev_type:
            emit_event(
                store,
                ev_type,
                task_id=tid,
                agent_id=str(task.get("assignee_agent_id") or "") or None,
                run_id=run_id,
                actor=actor,
                summary=f"run {rs} -> task {new_st}",
                detail={"run_status": rs, "task_status": new_st},
                idempotency_key=f"proj:{run_id}:{tid}:{new_st}",
            )
    _project_agent_status(store, run, rs)


def _agent_has_other_active_run(store: Store, agent_id: str, exclude_run_id: str) -> bool:
    """이 agent의 다른 queued/running run이 남아 있는지 — agent idle
    투영 전 확인용. list_active_runs는 볼륨이 작아 전체 스캔으로 충분하다."""
    for r in store.list_active_runs():
        if str(getattr(r, "id", "") or "") == exclude_run_id:
            continue
        if str(getattr(r, "agent_id", "") or "") == agent_id:
            return True
    return False


def _project_agent_status(store: Store, run: Any, run_status: str) -> None:
    """run.agent_id의 agent status 투영 — running 중이면 running,
    종결/대기면 idle. 단, 같은 agent의 다른 활성 run이 남아 있으면
    idle로 내리지 않는다 — 마지막 run이 끝날 때까지 agent는 작업 중이다."""
    aid = str(getattr(run, "agent_id", "") or "")
    if not aid:
        return
    agent = store.get_agent(aid)
    if agent is None:
        return
    canon = canonical_agent_status(agent.get("status"))
    if canon not in {AgentStatus.ACTIVE.value, AgentStatus.IDLE.value, AgentStatus.RUNNING.value}:
        return  # paused/error/pending/terminated는 사람/시스템 결정 존중
    if run_status == RunStatus.RUNNING.value:
        target = AgentStatus.RUNNING.value
    elif run_status in _RUN_TASK_MAP or run_status in _RUN_TASK_TERMINAL_BLOCKED:
        # run-aware — 이 run이 종결돼도 agent의 다른 활성 run이 있으면
        # idle로 내리면 안 된다(병렬 run 중 하나만 끝난 경우).
        if _agent_has_other_active_run(store, aid, str(getattr(run, "id", "") or "")):
            return
        target = AgentStatus.IDLE.value
    else:
        return
    if canon == target:
        return
    if not can_transition_agent(canon, target):
        return
    # CAS — 읽기~쓰기 사이 terminated/수동 전이가 있으면 덮어쓰지 않는다.
    if store.transition_agent_status(aid, canon, target) is None:
        return
    emit_event(
        store,
        AgentEventType.AGENT_STATUS_CHANGED,
        agent_id=aid,
        run_id=str(getattr(run, "id", "") or ""),
        actor="system",
        summary=f"run {run_status} -> agent {target}",
        detail={"from": canon, "to": target, "via": "run_projection"},
        idempotency_key=f"proj-agent:{getattr(run, 'id', '')}:{aid}:{target}",
    )
