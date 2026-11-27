# mypy: disable-error-code="attr-defined"
"""task 운영 — spec 1차 C항.

task 생성/부분 갱신/배정/checkout/해제와 terminal 정리(_finalize_terminal_task),
상태 전이 사슬 진행(_advance_task_status)을 담당한다. store는 원자적 쓰기만
제공하고 도메인 규칙은 여기 + models.management에 둔다. 라우터는 얇게 유지.
"""

from __future__ import annotations

from typing import Any

from ..models import (
    AGENT_WORK_STATUSES,
    AgentCapability,
    AgentEventType,
    AgentStatus,
    TaskStatus,
    can_transition_task,
    canonical_agent_status,
    is_agent_terminal,
)
from ..store.db import Store
from . import permissions as perm_svc
from .agent_lifecycle import _set_agent_status_quiet
from .management_common import (
    TASK_PRIORITY_VALUES,
    TASK_STATUS_VALUES,
    ManagementError,
    _require_agent,
    _require_task,
    emit_event,
)

# terminal task 목표 상태 — done/cancelled.
_TASK_TERMINAL = {TaskStatus.DONE.value, TaskStatus.CANCELLED.value}

# desired task status → 기록할 agent_event type (중복 방지 키에 상태 포함).
_TASK_EVENT_FOR_STATUS = {
    TaskStatus.DONE.value: AgentEventType.TASK_COMPLETED.value,
    TaskStatus.BLOCKED.value: AgentEventType.TASK_BLOCKED.value,
    TaskStatus.CANCELLED.value: AgentEventType.TASK_CANCELLED.value,
    TaskStatus.WAITING_APPROVAL.value: AgentEventType.REVIEW_REQUESTED.value,
    TaskStatus.IN_PROGRESS.value: AgentEventType.TASK_ASSIGNED.value,
}


def _task_ancestor_is(store: Store, ancestor_id: str, target_id: str) -> bool:
    """ancestor_id의 '상위' 체인(parent 방향)을 올라가며 target_id에
    도달하는지 검사 — ancestor_id 자신은 제외하고 시작한다.

    순환 방어 — 방문 집합으로 이미 오염된 체인에서도 무한 루프하지 않는다
    (store.reports_to_would_cycle과 동일 패턴)."""
    seen: set[str] = set()
    row = store.get_task(ancestor_id)
    cur = str((row or {}).get("parent_id") or "") or None
    while cur and cur not in seen:
        if cur == target_id:
            return True
        seen.add(cur)
        row = store.get_task(cur)
        cur = str((row or {}).get("parent_id") or "") or None
    return False


def validate_task_fields(fields: dict[str, Any]) -> str | None:
    """management task create/patch 공통 검증."""
    if "status" in fields and fields["status"] is not None:
        st = str(fields["status"]).strip().lower()
        if st not in TASK_STATUS_VALUES:
            return f"status must be one of {sorted(TASK_STATUS_VALUES)}"
    if "priority" in fields and fields["priority"] is not None:
        pr = str(fields["priority"]).strip().lower()
        if pr not in TASK_PRIORITY_VALUES:
            return f"priority must be one of {sorted(TASK_PRIORITY_VALUES)}"
    return None


def _validate_task_scope_refs(
    store: Store,
    fields: dict[str, Any],
) -> None:
    """spec 3차 A — task.project_id/goal_id 참조 존재 검증.

    fields에 키가 있고 값이 비어 있지 않을 때만 검사한다 — None/''는
    '해제' 의미로 허용. 존재하지 않는 참조는 422."""
    pid = fields.get("project_id")
    if pid:
        if store.get_project(str(pid).strip()) is None:
            raise ManagementError(422, "validation", "project not found")
    gid = fields.get("goal_id")
    if gid:
        if store.get_goal(str(gid).strip()) is None:
            raise ManagementError(422, "validation", "goal not found")


def _resolve_task_uuid(store: Store, ref: str) -> dict[str, Any] | None:
    """task 참조(id 또는 'WMB-n' identifier)를 행으로 해석."""
    return store.get_task(ref)


def _validate_task_agent_refs(
    store: Store,
    *,
    assignee_agent_id: Any = None,
    reviewer_agent_id: Any = None,
    check_assignee_terminal: bool = True,
) -> None:
    """create/patch 공통 — assignee/reviewer agent 존재 검증.

    assignee는 terminated agent 배정을 409로 거부한다(patch_task와 동일
    컨벤션). reviewer는 존재 여부만 본다."""
    if assignee_agent_id:
        aid = str(assignee_agent_id)
        agent = store.get_agent(aid)
        if agent is None:
            raise ManagementError(422, "validation", "assignee agent not found")
        if check_assignee_terminal and is_agent_terminal(agent.get("status")):
            raise ManagementError(409, "conflict", "terminated agent cannot be assigned")
    if reviewer_agent_id:
        rid = str(reviewer_agent_id)
        if store.get_agent(rid) is None:
            raise ManagementError(422, "validation", "reviewer agent not found")


def create_task(
    store: Store,
    *,
    title: str,
    description: str = "",
    status: str = "backlog",
    priority: str = "medium",
    assignee_agent_id: str | None = None,
    reviewer_agent_id: str | None = None,
    parent_id: str | None = None,
    project_id: str | None = None,
    goal_id: str | None = None,
    created_by: str = "",
    actor: str = "",
) -> dict[str, Any]:
    """task 생성 + task_created 이벤트. 실패 시 ManagementError.

    parent_id는 id 또는 'WMB-n' identifier 모두 허용 — 저장 전 uuid로
    해석한다. assignee/reviewer agent 존재도 patch와 동일하게 검증한다.
    project_id/goal_id는 spec 3차 A의 계층 참조 — 존재하는 행만 허용."""
    verr = validate_task_fields({"status": status, "priority": priority, "title": title})
    if verr:
        raise ManagementError(422, "validation", verr)
    if not title.strip():
        raise ManagementError(422, "validation", "title must not be empty")
    _validate_task_agent_refs(
        store,
        assignee_agent_id=assignee_agent_id,
        reviewer_agent_id=reviewer_agent_id,
    )
    _validate_task_scope_refs(store, {"project_id": project_id, "goal_id": goal_id})
    # spec 3차 D — assignee agent의 task:assign capability를 서버가 재검증.
    # task는 아직 없으므로 project 스코프까지만 평가한다(global+project).
    if assignee_agent_id:
        perm_svc.require_permission(
            store,
            str(assignee_agent_id),
            AgentCapability.TASK_ASSIGN.value,
            project_id=str(project_id or ""),
            actor=actor,
        )
    parent_uuid: str | None = None
    if parent_id:
        parent = _resolve_task_uuid(store, str(parent_id))
        if parent is None:
            raise ManagementError(422, "validation", "parent task not found")
        parent_uuid = str(parent["id"])
        # 기존 체인에 이미 순환이 있으면 여기서도 차단 — 신규 task가
        # 순환의 일부가 되는 것을 막는다(L5). 해석된 uuid 기준으로 비교.
        if _task_ancestor_is(store, parent_uuid, parent_uuid):
            raise ManagementError(422, "validation", "parent task chain has a cycle")
    task = store.create_task(
        title,
        description=description,
        status=status,
        priority=priority,
        assignee_agent_id=assignee_agent_id,
        reviewer_agent_id=reviewer_agent_id,
        parent_id=parent_uuid,
        project_id=project_id,
        goal_id=goal_id,
        created_by=created_by,
    )
    if task is None:
        raise ManagementError(422, "validation", "task create failed")
    emit_event(
        store,
        AgentEventType.TASK_CREATED,
        task_id=str(task["id"]),
        actor=actor or created_by,
        summary=f"task {task['identifier']} created",
        detail={"identifier": task["identifier"], "title": task["title"][:120]},
    )
    return task


def _finalize_terminal_task(
    store: Store,
    task_id: str,
    *,
    actor: str = "",
    run_id: str = "",
    via: str = "",
    record_event: bool = True,
) -> None:
    """terminal task의 잔여 점유 정리 — checkout 해제 + current_run_id 클리어
    + lifecycle agent_event(멱등 키 `term:{task}:{status}`).

    수동 PATCH·heartbeat·run 투영 어떤 경로로 done/cancelled가 됐든 같은
    정리를 거친다 — 경로별로 정리 로직이 흩어지면 잔여 checkout/current_run_id가
    남는다(M5). 이미 정리됐으면 no-op. record_event=False면 이벤트는
    호출자가 이미 기록한 것으로 간주한다(투영 전이 이벤트와 중복 방지)."""
    task = store.get_task(task_id)
    if task is None:
        return
    st = str(task.get("status") or "")
    if st not in _TASK_TERMINAL:
        return
    tid = str(task["id"])
    if task.get("checkout_agent_id"):
        store.release_task_checkout(tid)
    if task.get("current_run_id"):
        store.update_task(tid, current_run_id=None)
    if not record_event:
        return
    et = _TASK_EVENT_FOR_STATUS.get(st, "")
    if not et:
        return
    emit_event(
        store,
        et,
        task_id=tid,
        agent_id=str(task.get("assignee_agent_id") or "") or None,
        run_id=run_id or None,
        actor=actor,
        summary=f"task {task.get('identifier')} -> {st}",
        detail={"via": via, "status": st},
        # terminal 상태는 일방향 — task가 같은 종결 상태에 두 번 도달할 수
        # 없어 키가 안정적이다. 수동/투영 경로 중 먼저 온 쪽만 기록된다.
        idempotency_key=f"term:{tid}:{st}",
    )


def patch_task(
    store: Store,
    task_id: str,
    fields: dict[str, Any],
    *,
    actor: str = "",
) -> dict[str, Any]:
    """task 부분 갱신 — status는 전이 allowlist를 거친다.

    명시 null(title/status/priority) 의미론 — 이 필드들은 NULL 상태가
    없으므로 null은 422로 거부한다(과거 priority:null→medium 무음 리셋,
    title:null→404, status:null→409 오매핑을 정규화). nullable 필드
    (description/assignee_agent_id/reviewer_agent_id/parent_id)의 null은
    '비우기'로 해석된다.

    assignee_agent_id 변경이 checkout 보유자와 어긋나면 checkout을 자동
    해제한다 — 점유자와 배정자가 다른 상태가 남는 것을 막는다(M6)."""
    task = _require_task(store, task_id)
    fields = dict(fields)
    # 필수값 필드의 명시 null 거부 — 위 docstring 참조.
    for f in ("title", "status", "priority"):
        if f in fields and fields[f] is None:
            raise ManagementError(422, "validation", f"{f} must not be null")
    verr = validate_task_fields(fields)
    if verr:
        raise ManagementError(422, "validation", verr)
    if "status" in fields:
        to = str(fields["status"]).strip().lower()
        frm = str(task.get("status") or "")
        if to != frm and not can_transition_task(frm, to):
            raise ManagementError(
                409,
                "invalid_transition",
                f"cannot transition task from {frm} to {to}",
            )
    _validate_task_agent_refs(
        store,
        assignee_agent_id=fields.get("assignee_agent_id"),
        reviewer_agent_id=fields.get("reviewer_agent_id"),
    )
    _validate_task_scope_refs(store, fields)
    # spec 3차 D — assignee 변경은 대상 agent의 task:assign capability를
    # 서버가 재검증한다(task 스코프 — project/global 규칙도 함께 매칭).
    # assign_task는 이 경로를 거치므로 별도 검사가 필요 없다.
    new_assignee = fields.get("assignee_agent_id")
    if new_assignee:
        # project_id가 함께 바뀌면 대상 project 스코프에서도 재검증한다 —
        # task_id만 넘기면 현재 task의 project가 정본이라 이동 '후'의
        # project deny를 우회할 수 있다(R2-1). task 스코프 규칙은 현재
        # 컨텍스트 평가가 이미 커버하므로 target_project_id에는 project
        # 스코프만 명시한다(내부에서 task_id와 별도 컨텍스트로 평가 —
        # _resolve_scope_ids 충돌 422를 유발하지 않는다).
        target_pid = ""
        if "project_id" in fields:
            tp = str(fields.get("project_id") or "").strip()
            if tp and tp != str(task.get("project_id") or ""):
                target_pid = tp
        perm_svc.require_permission(
            store,
            str(new_assignee),
            AgentCapability.TASK_ASSIGN.value,
            task_id=str(task["id"]),
            target_project_id=target_pid,
            actor=actor,
        )
    if "parent_id" in fields and fields["parent_id"]:
        parent = _resolve_task_uuid(store, str(fields["parent_id"]))
        if parent is None:
            raise ManagementError(422, "validation", "parent task not found")
        pid = str(parent["id"])
        # identifier 입력도 uuid로 정규화해 저장 — 자기참조/체인 순환은
        # 해석된 uuid 기준으로 비교한다(L5).
        fields["parent_id"] = pid
        if pid == task["id"]:
            raise ManagementError(422, "validation", "task cannot be its own parent")
        # 새 부모의 조상 체인이 이 task에 도달하면 순환 — store는
        # 자기참조만 막고 체인 순환은 여기서 검증한다(L5).
        if _task_ancestor_is(store, pid, str(task["id"])):
            raise ManagementError(422, "validation", "parent_id would create a cycle")
    updated = store.update_task(task["id"], **fields)
    if updated is None:
        raise ManagementError(404, "not_found", "task not found")
    new_status = str(updated.get("status") or "")
    # assignee 변경이 checkout 보유자와 어긋나면 checkout 해제 —
    # checkout_agent_id가 다른 agent를 가리키는 채 assignee만 바뀌는
    # divergence를 막는다(M6). assignee를 보유자 본인으로 바꾸는 경우는 유지.
    if "assignee_agent_id" in fields:
        new_assignee = fields.get("assignee_agent_id")
        holder = str(task.get("checkout_agent_id") or "")
        if holder and new_assignee != holder:
            store.release_task_checkout(str(task["id"]))
            emit_event(
                store,
                AgentEventType.TASK_CHECKOUT_RELEASED,
                task_id=str(task["id"]),
                agent_id=holder,
                actor=actor,
                summary=f"task {task['identifier']} checkout released (assignee changed)",
                detail={"reason": "assignee_changed", "released_agent_id": holder},
                idempotency_key=f"uncheckout:{task['id']}:{holder}:assignee",
            )
    # terminal task의 잔여 점유 정리 — checkout/current_run_id 클리어 +
    # lifecycle 이벤트(멱등). status 필드와 무관한 PATCH라도 task가 이미
    # terminal이면 stale 잔여를 회수한다(M5).
    if new_status in _TASK_TERMINAL:
        _finalize_terminal_task(store, str(task["id"]), actor=actor, via="patch")
    if "assignee_agent_id" in fields and fields.get("assignee_agent_id"):
        emit_event(
            store,
            AgentEventType.TASK_ASSIGNED,
            task_id=task["id"],
            agent_id=str(fields["assignee_agent_id"]),
            actor=actor,
            summary=f"task {task['identifier']} assigned",
            detail={"assignee_agent_id": fields["assignee_agent_id"]},
        )
    # checkout 해제/terminal 정리가 updated에 반영되지 않았을 수 있으므로
    # 최종 상태를 다시 읽어 반환한다.
    return store.get_task(str(task["id"])) or updated


def assign_task(
    store: Store,
    task_id: str,
    agent_id: str,
    *,
    reviewer_agent_id: str | None = None,
    actor: str = "",
) -> dict[str, Any]:
    """task 배정 — task/agent 존재·종결 여부·상태 전이 검증 후 기록."""
    task = _require_task(store, task_id)
    if task["status"] in (TaskStatus.DONE.value, TaskStatus.CANCELLED.value):
        raise ManagementError(409, "conflict", f"task is {task['status']}")
    agent = _require_agent(store, agent_id)
    canon = canonical_agent_status(agent.get("status"))
    if canon == AgentStatus.TERMINATED.value:
        raise ManagementError(409, "conflict", "terminated agent cannot be assigned")
    fields: dict[str, Any] = {"assignee_agent_id": agent_id}
    if reviewer_agent_id is not None:
        if store.get_agent(reviewer_agent_id) is None:
            raise ManagementError(422, "validation", "reviewer agent not found")
        fields["reviewer_agent_id"] = reviewer_agent_id
    updated = patch_task(store, task["id"], fields, actor=actor)
    return updated


def checkout_task(
    store: Store,
    task_id: str,
    agent_id: str,
    *,
    actor: str = "",
    human_actor: bool = False,
) -> dict[str, Any]:
    """atomic checkout — 점유 충돌은 409. 성공 시 task를 in_progress로 진행.

    human_actor=True(명시적 lead 세션/로컬 admin — 라우터가 _explicit_lead_user
    결과로 판정해 전달)이면 agent permission 검사를 건너뛴다 — lead가 agent
    대신 checkout하는 관리 행위는 agent-actor 경로가 아니다. 머신 API 키 등
    비-명시 호출은 agent-actor로 간주해 task:update capability를 재검증한다."""
    task = _require_task(store, task_id)
    agent = _require_agent(store, agent_id)
    canon = canonical_agent_status(agent.get("status"))
    if canon not in AGENT_WORK_STATUSES:
        raise ManagementError(
            409,
            "agent_unavailable",
            f"agent status {canon} cannot accept work",
        )
    # spec 3차 D — checkout하는 agent 본인의 task:update capability를
    # 서버가 재검증한다(agent-actor 경로; 명시적 lead checkout은 면제).
    if not human_actor:
        perm_svc.require_permission(
            store,
            agent_id,
            AgentCapability.TASK_UPDATE.value,
            task_id=str(task["id"]),
            actor=actor,
        )
        # M1 회귀 — checkout은 assignee 없는 task를 checkout agent에게
        # 자동 배정한다(아래 fields). task:update만 검사하면 task:assign
        # deny를 우회해 자기 배정이 가능하므로, 자동 배정이 발생하는 경우
        # task:assign도 함께 요구한다.
        if not task.get("assignee_agent_id"):
            perm_svc.require_permission(
                store,
                agent_id,
                AgentCapability.TASK_ASSIGN.value,
                task_id=str(task["id"]),
                actor=actor,
            )
    if task["status"] in _TASK_TERMINAL:
        raise ManagementError(409, "conflict", f"task is {task['status']}")
    # 명시 배정이 다른 agent에게 있으면 checkout 불가 — checkout이
    # 암묵 재배정으로 작동해 배정자를 우회하는 것을 막는다(M5).
    assignee = task.get("assignee_agent_id")
    if assignee and assignee != agent_id:
        raise ManagementError(409, "conflict", "task assigned to another agent")
    # 다른 agent가 점유 중이면 409 — store.checkout_task가 원자 판정
    current_holder = task.get("checkout_agent_id")
    if current_holder and current_holder != agent_id:
        raise ManagementError(409, "checkout_conflict", "task already checked out")
    if not store.checkout_task(task["id"], agent_id):
        raise ManagementError(409, "checkout_conflict", "task already checked out")
    # 배정자 없으면 checkout agent를 assignee로 — 명시 배정이 없던 경로 커버
    fields: dict[str, Any] = {}
    if not task.get("assignee_agent_id"):
        fields["assignee_agent_id"] = agent_id
    updated = _advance_task_status(store, task["id"], TaskStatus.IN_PROGRESS.value)
    if fields:
        updated = store.update_task(task["id"], **fields) or updated
    emit_event(
        store,
        AgentEventType.TASK_CHECKED_OUT,
        task_id=task["id"],
        agent_id=agent_id,
        actor=actor,
        summary=f"task {task['identifier']} checked out",
        detail={"identifier": task["identifier"]},
    )
    # agent가 작업 가능 상태로 표시되도록 running 전이 (가능할 때만)
    _set_agent_status_quiet(store, agent_id, AgentStatus.RUNNING.value)
    return updated or store.get_task(task["id"]) or task


def release_task(store: Store, task_id: str, *, actor: str = "") -> dict[str, Any]:
    """checkout 해제 — 외부 runner 중단 등으로 checkout이 stranded된
    task의 탈출 경로. task 상태/assignee는 유지한다(재checkout 가능)."""
    task = _require_task(store, task_id)
    holder = str(task.get("checkout_agent_id") or "")
    if not holder:
        raise ManagementError(409, "conflict", "task is not checked out")
    store.release_task_checkout(task["id"])
    emit_event(
        store,
        AgentEventType.TASK_CHECKOUT_RELEASED,
        task_id=task["id"],
        agent_id=holder,
        actor=actor,
        summary=f"task {task['identifier']} checkout released",
        detail={"identifier": task["identifier"], "released_agent_id": holder},
    )
    return store.get_task(task["id"]) or task


def _advance_task_status(store: Store, task_id: str, target: str) -> dict[str, Any] | None:
    """허용 전이 사슬을 따라 target까지 진행 — backlog→todo→in_progress 등.

    중간 상태(todo)는 별도 이벤트 없이 통과시킨다."""
    task = store.get_task(task_id)
    if task is None:
        return None
    cur = str(task.get("status") or "")
    if cur == target:
        return task
    # backlog는 todo를 거쳐야 in_progress에 도달한다
    if cur == TaskStatus.BACKLOG.value and target == TaskStatus.IN_PROGRESS.value:
        store.update_task(task_id, status=TaskStatus.TODO.value)
        cur = TaskStatus.TODO.value
    if can_transition_task(cur, target):
        return store.update_task(task_id, status=target)
    return store.get_task(task_id)
