# mypy: disable-error-code="attr-defined"
"""Agent management 서비스 — spec 1차 C항.

상태 전이 검증(models.management allowlist), 배정/checkout/wake/heartbeat,
run→task/agent 상태 투영을 담당한다. store는 원자적 쓰기만 제공하고
도메인 규칙은 여기 + models.management에 둔다. 라우터는 얇게 유지.

인증 정책은 라우터 책임 — 이 모듈은 "machine" 여부를 모른다.
"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from typing import Any

from .. import wakeups
from ..models import (
    AGENT_WORK_STATUSES,
    AgentEventType,
    AgentKind,
    AgentStatus,
    ApprovalActionType,
    RunStatus,
    TaskStatus,
    can_transition_agent,
    can_transition_task,
    canonical_agent_status,
    is_agent_terminal,
)
from ..models.enums import TaskPriority
from ..store.db import Store
from ..store.util import _iso, _now
from ..workflows import WORKFLOWS

logger = logging.getLogger("jobmate.management")

# adapter_config/이벤트 detail에서 마스킹할 secret-like 키 — API 응답과
# 저장 detail에 원문을 남기지 않는다. 'key'는 부분 문자열로 매칭해
# apikey/secret_key 등을 잡는다 — monkey 같은 오탐은 과도 마스킹일 뿐
# 보안상 안전한 방향이다(M3). dsn/database_url/connection_string/
# authorization/session/cookie도 연결 문자열·세션 값이 credentials를
# 품는 경우가 많아 마스킹 대상이다. 'auth'는 author 오탐을 피해
# 단어 경계(뒤에 영문자가 오지 않음)로만 매칭한다.
_SECRET_KEY_RE = re.compile(
    r"(key|secret|token|password|passwd|api[_-]?key|credential|private|bearer"
    r"|dsn|database[_-]?url|connection[_-]?string|authorization|session|cookie"
    r"|authenticat|auth(?![a-z]))",
    re.IGNORECASE,
)
# safe 키(endpoint/webhook_url 등) 아래 URL 문자열에 박힌
# userinfo(credentials)를 지운다 — https://user:pass@host 형태.
_URL_USERINFO_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://)([^/@\s:]+):([^/@\s]+)@")
REDACTED = "REDACTED"


def _scrub_url_userinfo(v: Any) -> Any:
    """문자열 값이 URL 형태면 userinfo의 비밀번호 부분을 마스킹한다."""
    if isinstance(v, str) and "://" in v:
        return _URL_USERINFO_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}:{REDACTED}@", v)
    return v


AGENT_STATUS_VALUES = {s.value for s in AgentStatus} | {"archived"}
TASK_STATUS_VALUES = {s.value for s in TaskStatus}
TASK_PRIORITY_VALUES = {s.value for s in TaskPriority}

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
_TASK_TERMINAL = {TaskStatus.DONE.value, TaskStatus.CANCELLED.value}

# desired task status → 기록할 agent_event type (중복 방지 키에 상태 포함).
_TASK_EVENT_FOR_STATUS = {
    TaskStatus.DONE.value: AgentEventType.TASK_COMPLETED.value,
    TaskStatus.BLOCKED.value: AgentEventType.TASK_BLOCKED.value,
    TaskStatus.CANCELLED.value: AgentEventType.TASK_CANCELLED.value,
    TaskStatus.WAITING_APPROVAL.value: AgentEventType.REVIEW_REQUESTED.value,
    TaskStatus.IN_PROGRESS.value: AgentEventType.TASK_ASSIGNED.value,
}


class ManagementError(Exception):
    """도메인 오류 — status_code/http 의미를 그대로 담는다."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def _redact_value(v: Any) -> Any:
    """중첩 dict/list 재귀 마스킹 — 리스트 안 dict의 secret 키도 치환(M3).
    safe 키 아래 문자열은 URL userinfo의 내장 credential을 지운다."""
    if isinstance(v, dict):
        return redact_adapter_config(v)
    if isinstance(v, list):
        return [_redact_value(i) for i in v]
    return _scrub_url_userinfo(v)


def redact_adapter_config(cfg: Any) -> dict[str, Any]:
    """adapter_config/임의 detail dict 복사본 — secret-like 키의 값을
    REDACTED로 치환. dict가 아니면 {}를 반환한다."""
    if not isinstance(cfg, dict):
        return {}
    out: dict[str, Any] = {}
    for k, v in cfg.items():
        key = str(k)
        if _SECRET_KEY_RE.search(key):
            out[key] = REDACTED if v else v
        else:
            out[key] = _redact_value(v)
    return out


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
    created_by: str = "",
    actor: str = "",
) -> dict[str, Any]:
    """task 생성 + task_created 이벤트. 실패 시 ManagementError.

    parent_id는 id 또는 'WMB-n' identifier 모두 허용 — 저장 전 uuid로
    해석한다. assignee/reviewer agent 존재도 patch와 동일하게 검증한다."""
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


def checkout_task(store: Store, task_id: str, agent_id: str, *, actor: str = "") -> dict[str, Any]:
    """atomic checkout — 점유 충돌은 409. 성공 시 task를 in_progress로 진행."""
    task = _require_task(store, task_id)
    agent = _require_agent(store, agent_id)
    canon = canonical_agent_status(agent.get("status"))
    if canon not in AGENT_WORK_STATUSES:
        raise ManagementError(
            409,
            "agent_unavailable",
            f"agent status {canon} cannot accept work",
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


def _set_agent_status_quiet(store: Store, agent_id: str, to: str) -> None:
    """전이 가능할 때만 agent status 변경 — 이벤트는 호출자가 필요 시 기록.
    CAS 전이 사용 — 경합/이미 전이된 경우 조용히 건너뛴다."""
    agent = store.get_agent(agent_id)
    if agent is None:
        return
    frm = canonical_agent_status(agent.get("status"))
    if frm != to and can_transition_agent(frm, to):
        store.transition_agent_status(agent_id, frm, to)


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

    kind = str(agent.get("kind") or AgentKind.WORKFLOW.value)
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
    if workflow not in WORKFLOWS:
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
        version=WORKFLOWS[workflow].version,
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
