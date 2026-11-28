# mypy: disable-error-code="attr-defined"
"""agent runtime — spec 1차 C항.

머신 heartbeat와 wake(wake_agent/wake_event_key/replay 판정)를 담당한다.
workflow kind wake는 run 생성 + task/agent bookkeeping 후 schedule 콜백을
호출하고, external kind는 이벤트만 기록한다(프로세스 없음).
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from .. import wakeups
from ..models import (
    AGENT_WORK_STATUSES,
    AgentCapability,
    AgentEventType,
    AgentKind,
    AgentStatus,
    TaskStatus,
    can_transition_agent,
    can_transition_task,
    canonical_agent_status,
)
from ..store import orgctx
from ..store.db import Store
from ..store.util import _iso, _now
from ..workflows.resolver import resolve_workflow_spec
from . import permissions as perm_svc
from .agent_lifecycle import _set_agent_status_quiet
from .management_common import (
    TASK_STATUS_VALUES,
    ManagementError,
    _require_agent,
    agent_out,
    emit_event,
)
from .redaction import redact_adapter_config
from .run_projection import _RUN_TERMINAL, project_run_status
from .task_ops import (
    _TASK_TERMINAL,
    _advance_task_status,
    _finalize_terminal_task,
)

logger = logging.getLogger("jobmate.management")

# heartbeat body.status(머신 어휘) → canonical agent status.
# 'decision'은 사람 결정 대기 — agent는 idle로 두고 task가 waiting_approval로 간다.
_HEARTBEAT_AGENT_STATUS = {
    "working": AgentStatus.RUNNING.value,
    "run": AgentStatus.RUNNING.value,
    "running": AgentStatus.RUNNING.value,
    "idle": AgentStatus.IDLE.value,
    "rest": AgentStatus.IDLE.value,
    "paused": AgentStatus.PAUSED.value,
    "decision": AgentStatus.IDLE.value,
    "waiting_approval": AgentStatus.IDLE.value,
    "error": AgentStatus.ERROR.value,
    "failed": AgentStatus.ERROR.value,
}


def heartbeat(
    store: Store,
    agent_id: str,
    *,
    status: str = "",
    task_id: str = "",
    task_status: str = "",
    detail: dict[str, Any] | None = None,
    actor: str = "",
) -> dict[str, Any]:
    """머신 heartbeat — last_seen_at 갱신 + 상태 투영.

    status 어휘(working|idle|rest|paused|decision|error|failed|running)는
    canonical agent status로 매핑하고, allowlist가 허용할 때만 적용한다.
    task_id가 주어지면 해당 task가 이 agent의 checkout/배정 대상인지 검증하고
    task_status(명시) 또는 status 파생 매핑으로 task 상태를 갱신한다."""
    agent = _require_agent(store, agent_id)
    canon = canonical_agent_status(agent.get("status"))
    if canon == AgentStatus.TERMINATED.value:
        raise ManagementError(409, "conflict", "terminated agent cannot heartbeat")

    updates: dict[str, Any] = {"last_seen_at": _iso(_now())}
    mapped = ""
    if status:
        key = status.strip().lower()
        mapped = _HEARTBEAT_AGENT_STATUS.get(key, "")
        if not mapped:
            raise ManagementError(
                422, "validation", f"status must be one of {sorted(_HEARTBEAT_AGENT_STATUS)}"
            )
        if mapped != canon and can_transition_agent(canon, mapped):
            # CAS 전이 — 체크~쓰기 사이 terminated 등으로 바뀐 status를
            # heartbeat가 되살리지 않는다. 경합 패자는 상태 변경 없이
            # last_seen_at만 갱신하고 계속 진행한다.
            if store.transition_agent_status(agent_id, canon, mapped) is not None:
                # 06 호스팅 로깅 문서가 주장하는 heartbeat 상태 전이 로그 —
                # 매 heartbeat가 아니라 상태가 실제로 바뀔 때만 남긴다(L7).
                logger.info(
                    "agent %s status %s -> %s via heartbeat",
                    agent_id,
                    canon,
                    mapped,
                )
                emit_event(
                    store,
                    AgentEventType.AGENT_STATUS_CHANGED,
                    agent_id=agent_id,
                    actor=actor,
                    summary=f"heartbeat: {canon} -> {mapped}",
                    detail={"from": canon, "to": mapped, "via": "heartbeat"},
                )
            else:
                logger.info(
                    "heartbeat status transition lost race agent=%s %s->%s",
                    agent_id,
                    canon,
                    mapped,
                )

    task_out: dict[str, Any] | None = None
    if task_id:
        task = store.get_task(task_id)
        if task is None:
            raise ManagementError(404, "not_found", "task not found")
        holder = task.get("checkout_agent_id") or task.get("assignee_agent_id")
        if holder != agent_id:
            raise ManagementError(
                409, "conflict", "task is not checked out to or assigned to this agent"
            )
        # 명시 task_status 우선, 없으면 status 파생 매핑
        desired = (task_status or "").strip().lower() or ""
        if desired and desired not in TASK_STATUS_VALUES:
            raise ManagementError(422, "validation", f"invalid task_status: {task_status}")
        if not desired:
            if mapped == AgentStatus.RUNNING.value:
                desired = TaskStatus.IN_PROGRESS.value
            elif status.strip().lower() in ("decision", "waiting_approval"):
                desired = TaskStatus.WAITING_APPROVAL.value
        if desired:
            cur = str(task.get("status") or "")
            if desired != cur:
                # spec 3차 D — heartbeat로 task 상태를 바꾸는 agent 본인의
                # task:update capability를 서버가 재검증한다.
                perm_svc.require_permission(
                    store,
                    agent_id,
                    AgentCapability.TASK_UPDATE.value,
                    task_id=str(task["id"]),
                    actor=actor,
                )
                if desired == TaskStatus.IN_PROGRESS.value:
                    task_out = _advance_task_status(store, task["id"], desired)
                elif can_transition_task(cur, desired):
                    task_out = store.update_task(task["id"], status=desired)
                else:
                    logger.info(
                        "heartbeat task transition skipped task=%s %s->%s",
                        task["id"],
                        cur,
                        desired,
                    )
        task_out = task_out or store.get_task(task["id"])
        # heartbeat 경유 terminal task 정리 — checkout/current_run_id
        # 클리어 + lifecycle 이벤트(멱등 — 이미 terminal이어도 stale
        # 잔여를 회수한다, M5).
        if task_out is not None and str(task_out.get("status") or "") in _TASK_TERMINAL:
            _finalize_terminal_task(
                store,
                str(task["id"]),
                actor=actor,
                run_id=str(task.get("current_run_id") or ""),
                via="heartbeat",
            )
            task_out = store.get_task(str(task["id"])) or task_out

    updated = store.update_agent(agent_id, **updates)
    emit_event(
        store,
        AgentEventType.HEARTBEAT_RECEIVED,
        agent_id=agent_id,
        task_id=task_id or None,
        actor=actor,
        summary=f"heartbeat {status or 'ping'}",
        # 머신이 보낸 detail은 비신뢰 입력 — secret-like 키는 저장 전에
        # 마스킹해 events API/DB에 원문이 남지 않게 한다(L3, M3 재사용).
        detail=redact_adapter_config({"status": status, **(detail or {})}),
    )
    out = agent_out(updated or agent)
    out["task"] = task_out
    return out


def wake_event_key(agent_id: str, idempotency_key: str) -> str:
    """wake 멱등의 agent_events 키 — wake_agent 내부 계산과 동일 규칙.

    라우터가 replay 여부를 예산 검사 전에 확인할 때 사용한다 —
    키 생성 규칙이 두 곳에 복제되면 replay 판정이 어긋난다."""
    raw_key = f"wake:{agent_id}:{(idempotency_key or '').strip()}"
    return (
        raw_key
        if len(raw_key) <= 200
        else "wake:" + hashlib.sha256(raw_key.encode()).hexdigest()[:64]
    )


def _wake_replay(store: Store, prev: dict[str, Any]) -> dict[str, Any]:
    """동일 idempotency key의 wake 재생 — 기록된 응답을 그대로 반환."""
    detail = prev.get("detail") or {}
    return {
        "action": str(detail.get("action") or "replayed"),
        "run_id": str(detail.get("run_id") or ""),
        "wakeup_id": str(detail.get("wakeup_id") or ""),
        "task_id": str(detail.get("task_id") or ""),
        "agent_id": str(prev.get("agent_id") or ""),
        "replayed": True,
    }


def wake_agent(
    store: Store,
    agent_id: str,
    *,
    task_id: str = "",
    idempotency_key: str = "",
    actor: str = "",
    schedule: Any = None,
) -> dict[str, Any]:
    """agent wake — workflow는 run 생성, external은 이벤트만(프로세스 없음).

    idempotency_key 필수(라우터가 검증). 동일 키 재전송은 wake_requested
    이벤트의 detail에 기록된 결과를 재생한다 — run 중복 생성 없음.
    """
    agent = _require_agent(store, agent_id)
    # spec 003 FR-019 — agent 행의 org로 wake 전체를 스코프한다. 웹훅·
    # 스케줄러 등 ctx='' 경로에서도 생성되는 run/task 링크/agent_events가
    # agent의 조직을 상속하게 한다(재귀 1회로 컨텍스트 정규화).
    oid = str(agent.get("org_id") or "")
    if oid and orgctx.current_org_id() != oid:
        with orgctx.org_scope(oid):
            return wake_agent(
                store,
                agent_id,
                task_id=task_id,
                idempotency_key=idempotency_key,
                actor=actor,
                schedule=schedule,
            )
    canon = canonical_agent_status(agent.get("status"))
    if canon == AgentStatus.TERMINATED.value:
        raise ManagementError(409, "conflict", "terminated agent cannot be woken")
    if canon not in AGENT_WORK_STATUSES:
        raise ManagementError(
            409,
            "agent_unavailable",
            f"agent status {canon} cannot accept work",
        )
    key = (idempotency_key or "").strip()
    if not key:
        raise ManagementError(422, "validation", "idempotency_key is required")
    # 복합키가 저장 계층 상한(200자)을 넘으면 해시로 축약 — agent_events와
    # wakeup_requests 양쪽에서 동일 키가 잘리지 않고 일치해야 replay가
    # 성립한다(M1). 스키마 상한(150자) + 일반 agent_id(12자)에서는
    # 이 분기에 도달하지 않는다. 규칙은 wake_event_key와 공유한다.
    ev_key = wake_event_key(agent_id, key)
    # 재생 판정 전에 요청 task_id를 정규 id로 해석 — identifier('WMB-1')
    # 요청과 저장된 uuid 비교가 어긋나지 않게 한다. task 미존재 시 원문
    # 그대로 비교해 이후 404 경로로 진행한다.
    req_task_key = (task_id or "").strip()
    resolved_task_id = req_task_key
    if req_task_key:
        trow = store.get_task(req_task_key)
        if trow is not None:
            resolved_task_id = str(trow["id"])
    prev = store.get_agent_event_by_key(ev_key)
    if prev is not None:
        # 동일 키 재전송이지만 task_id가 다르면 replay가 아니라 충돌 —
        # 기록된 wake를 다른 task에 잘못 재생하는 것을 막는다(H3).
        prev_task = str((prev.get("detail") or {}).get("task_id") or "")
        if prev_task != resolved_task_id:
            raise ManagementError(
                409,
                "idempotency_conflict",
                "idempotency_key already used with a different task",
            )
        return _wake_replay(store, prev)

    task: dict[str, Any] | None = None
    if task_id:
        task = store.get_task(task_id)
        if task is None:
            raise ManagementError(404, "not_found", "task not found")
        if task["status"] in _TASK_TERMINAL:
            raise ManagementError(409, "conflict", f"task is {task['status']}")
        assignee = task.get("assignee_agent_id")
        if assignee and assignee != agent_id:
            raise ManagementError(409, "conflict", "task assigned to another agent")

    # spec 3차 D — wake되는 agent의 capability를 서버가 재검증한다.
    # agent:wake는 항상, workflow kind의 run 생성은 run:execute,
    # task-bound wake는 task:assign을 추가로 요구한다. idempotent replay는
    # 부수효과가 없으므로 이 검사 이전에 반환된다(위 replay 블록 참조).
    wake_task_scope = str(task["id"]) if task is not None else ""
    perm_svc.require_permission(
        store,
        agent_id,
        AgentCapability.AGENT_WAKE.value,
        task_id=wake_task_scope,
        actor=actor,
    )
    kind = str(agent.get("kind") or AgentKind.WORKFLOW.value)
    if kind == AgentKind.WORKFLOW.value:
        perm_svc.require_permission(
            store,
            agent_id,
            AgentCapability.RUN_EXECUTE.value,
            task_id=wake_task_scope,
            actor=actor,
        )
    if task is not None:
        perm_svc.require_permission(
            store,
            agent_id,
            AgentCapability.TASK_ASSIGN.value,
            task_id=wake_task_scope,
            actor=actor,
        )
    if kind == AgentKind.EXTERNAL.value:
        # 외부 runner pull/push — 서버는 프로세스를 만들지 않는다.
        detail = {
            "action": "wake_recorded",
            "run_id": "",
            "wakeup_id": "",
            "task_id": str(task["id"]) if task else "",
        }
        emit_event(
            store,
            AgentEventType.WAKE_REQUESTED,
            agent_id=agent_id,
            task_id=task["id"] if task else None,
            actor=actor,
            summary=f"wake requested (external) task={task['identifier'] if task else '-'}",
            detail=detail,
            idempotency_key=ev_key,
        )
        logger.info(
            "external wake recorded agent=%s task=%s actor=%s",
            agent_id,
            task["id"] if task else "-",
            actor,
        )
        return {**detail, "agent_id": agent_id, "replayed": False}

    # workflow adapter — adapter_config.workflow (기본 dev_review)
    if task is None:
        raise ManagementError(422, "validation", "task_id is required for workflow wake")
    cfg = agent.get("adapter_config") or {}
    workflow = str(cfg.get("workflow") or "dev_review").strip()
    # 이 시점의 org 컨텍스트는 wake_agent 상단에서 agent 행의 org로 재적용된
    # 상태 — 커스텀 spec은 그 조직 것을 해석한다(spec 003 US3).
    wf_spec = resolve_workflow_spec(store, workflow)
    if wf_spec is None:
        raise ManagementError(422, "validation", f"unknown workflow in adapter_config: {workflow}")
    task_text = f"[{task['identifier']}] {task['title']}"
    desc = str(task.get("description") or "").strip()
    if desc:
        task_text += f"\n\n{desc[:4000]}"
    input_data: dict[str, Any] = {
        "task": task_text,
        # 기존 workflow 입력 형식(task/project/meta)을 유지하면서
        # 관리 계층 참조를 input.meta에 싣는다.
        "meta": {
            "task_id": task["id"],
            "agent_id": agent_id,
            "identifier": task["identifier"],
        },
    }
    res = wakeups.admit_run(
        store,
        workflow_key=workflow,
        input_data=input_data,
        source="api",
        idempotency_key=ev_key,
        created_by=actor or "management",
        version=wf_spec.version,
        job_payload={"task": task_text},
        agent_id=agent_id,
    )
    action = str(res.get("action") or "")
    run_id = str(res.get("run_id") or "")
    wakeup_id = str(res.get("wakeup_id") or "")
    if action == "invalid_goal":
        raise ManagementError(422, "validation", str(res.get("error") or "invalid goal"))
    if action == "conflict":
        raise ManagementError(
            409, "idempotency_conflict", "idempotency_key already used with different input"
        )
    run_obj = res.get("run")
    run_status = ""
    if run_obj is not None:
        run_status = str(getattr(run_obj.status, "value", run_obj.status) or "")
    run_terminal = run_status in _RUN_TERMINAL
    if run_id:
        # ---- bookkeeping FIRST (C1) — schedule()은 모든 task/agent
        # bookkeeping이 끝난 뒤 마지막에 호출한다. run이 즉시 종결돼도
        # task_runs 링크·current_run_id·agent_events가 이미 존재하므로
        # project_run_status가 투영을 놓치지 않는다.
        store.link_task_run(str(task["id"]), run_id)
        # run metadata에 관리 참조 기록 — 관측/디버깅용(권위 링크는 task_runs)
        try:
            run = store.get_run(run_id)
            if run is not None:
                meta = dict(run.metadata or {})
                meta["task_id"] = task["id"]
                meta["agent_id"] = agent_id
                store.update_run(run_id, metadata=meta)
        except Exception:
            logger.warning("run metadata tag failed run=%s", run_id, exc_info=True)
        t = store.get_task(str(task["id"]))
        cur_run = str((t or {}).get("current_run_id") or "")
        # current_run_id는 비어 있거나 이 run이거나, terminal replay 복원일
        # 때만 쓴다 — 다른 run을 가리키는 최신 링크를 덮지 않는다(L10).
        # terminal replay에서 비어 있으면 설정해 projection이 종결 상태를
        # 복원할 수 있게 한다(투영 후 다시 NULL로 정리된다).
        # action=="created"는 새로 admit된 run이므로 이전 포인터가 죽은
        # run(실패→blocked 잔여 등)을 가리켜도 덮어쓴다 — 안 그러면
        # 이후 이 run의 projection이 전부 스킵돼 task가 영구 고착된다.
        if not cur_run or cur_run == run_id or action == "created":
            store.update_task(str(task["id"]), current_run_id=run_id)
        if action == "created" and not run_terminal:
            _advance_task_status(store, str(task["id"]), TaskStatus.IN_PROGRESS.value)
            t2 = store.get_task(str(task["id"]))
            if t2 is not None and not t2.get("assignee_agent_id"):
                store.update_task(str(task["id"]), assignee_agent_id=agent_id)
            emit_event(
                store,
                AgentEventType.RUN_LINKED,
                agent_id=agent_id,
                task_id=str(task["id"]),
                run_id=run_id,
                actor=actor,
                summary=f"run {run_id} linked to {task['identifier']}",
                detail={"workflow": workflow},
            )
            _set_agent_status_quiet(store, agent_id, AgentStatus.RUNNING.value)
        else:
            # replayed/coalesced 또는 이미 종결된 run — 실제 run 상태로
            # task/agent를 재투영한다. 종결 run replay가 task를
            # in_progress/agent를 running으로 되돌리는 것을 막고(H4),
            # agent_events 유실 크래시 후 재시도도 여기서 복원된다.
            project_run_status(store, run_id, actor=actor or "system")
    detail = {
        "action": action or "unknown",
        "run_id": run_id,
        "wakeup_id": wakeup_id,
        "task_id": str(task["id"]),
        "workflow": workflow,
    }
    emit_event(
        store,
        AgentEventType.WAKE_REQUESTED,
        agent_id=agent_id,
        task_id=str(task["id"]),
        run_id=run_id or None,
        actor=actor,
        summary=f"wake {workflow} run={run_id or '-'} task={task['identifier']}",
        detail=detail,
        idempotency_key=ev_key,
    )
    logger.info(
        "workflow wake agent=%s task=%s workflow=%s action=%s run=%s",
        agent_id,
        task["id"],
        workflow,
        action,
        run_id or "-",
    )
    # run 실행 예약은 모든 bookkeeping(링크/상태/이벤트) 이후 마지막 —
    # 콜백이 즉시 run을 끝내도 투영 대상이 이미 존재한다(C1). 예약 실패는
    # run을 queued로 남길 뿐 wake 응답에는 영향이 없다.
    if action == "created" and schedule is not None and run_id:
        try:
            schedule(res.get("run"), res.get("job_payload") or {"task": task_text})
        except Exception:
            logger.warning("wake schedule callback failed run=%s", run_id, exc_info=True)
    return {
        **detail,
        "agent_id": agent_id,
        "replayed": action == "replayed",
    }
