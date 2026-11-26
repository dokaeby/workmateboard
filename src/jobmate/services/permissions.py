# mypy: disable-error-code="attr-defined"
"""Agent capability 권한 판정 — paperclip spec 3차 D.

agent_permissions 규칙을 해석해 allow|deny|needs_approval을 결정한다.

판정 규칙 (spec D):
- explicit deny가 allow보다 우선 — 매칭되는 scope 전체에서 deny 1건이면 deny.
- scope가 좁은 규칙(task > project > global)을 먼저 평가하되 deny 우선은 유지.
- 규칙이 없으면 capability 기본값 — 대부분 allow, 외부발송/배포/파괴 3종은
  기본 deny + 명시적 allow와 2차 approval을 모두 요구한다.
- 사람 관리자(세션 lead/member)와 머신 agent의 권한 모델은 구분된다 —
  이 모듈의 판정은 agent_id를 가진 agent 행위자 경로에만 적용한다.

승인 매핑: needs_approval capability는 ApprovalActionType의 기존 action과
연결되고, has_decided_approval(action, 'agent', agent_id)로 2차 승인을 확인한다.
승인은 (action_type, agent) 단위의 standing grant다 — 한 번 승인되면 이후
동일 capability의 모든 호출에 유효하며 만료/소비되지 않는다(spec D는
"명시적 allow + 2차 approval"만 요구하고 승인 단위를 정의하지 않으므로
가장 단순한 해석을 택했다). scope별 승인이 필요해지면 approval_requests의
resource/safe_payload에 scope를 싣는 확장이 필요하다.
"""

from __future__ import annotations

import logging
from typing import Any

from ..models import AgentCapability, ApprovalActionType, PermissionEffect, PermissionScopeType
from ..store.db import Store

logger = logging.getLogger("jobmate.permissions")

AGENT_CAPABILITIES = tuple(c.value for c in AgentCapability)
PERMISSION_EFFECTS = tuple(e.value for e in PermissionEffect)
PERMISSION_SCOPE_TYPES = tuple(s.value for s in PermissionScopeType)

# spec D: 외부발송·배포·파괴는 기본 deny — 명시적 allow + 2차 approval 모두 필요.
APPROVAL_REQUIRED_CAPABILITIES = {
    AgentCapability.EXTERNAL_SEND.value,
    AgentCapability.DEPLOY_EXECUTE.value,
    AgentCapability.DESTRUCTIVE_EXECUTE.value,
}

# needs_approval capability → 기존 approval_requests.action_type.
# ACTION_MIN_RISK가 이 action들을 high로 강제하므로 사람(로그인 lead/admin)
# 결정 없이는 승인 row가 approved가 되지 않는다(services/approvals.py).
CAPABILITY_APPROVAL_ACTION: dict[str, str] = {
    AgentCapability.EXTERNAL_SEND.value: ApprovalActionType.EXTERNAL_SEND.value,
    AgentCapability.DEPLOY_EXECUTE.value: ApprovalActionType.DEPLOY.value,
    AgentCapability.DESTRUCTIVE_EXECUTE.value: ApprovalActionType.DESTRUCTIVE_TOOL.value,
}

# scope 좁음 순 — 좁은 scope의 규칙을 먼저 평가한다(결정 자체는 deny 우선).
_SCOPE_RANK = {
    PermissionScopeType.TASK.value: 0,
    PermissionScopeType.PROJECT.value: 1,
    PermissionScopeType.GLOBAL.value: 2,
}

DECISION_ALLOW = "allow"
DECISION_DENY = "deny"
DECISION_NEEDS_APPROVAL = "needs_approval"


def _resolve_scope_ids(
    store: Store,
    *,
    scope_type: str = "",
    scope_id: str = "",
    task_id: str = "",
    project_id: str = "",
) -> tuple[str, str]:
    """판정 컨텍스트의 (task_id, project_id) 정규화.

    - 명시 scope_type/scope_id는 호출자가 지정한 컨텍스트로 취급한다.
    - task_id가 주어지면 해당 task의 project_id를 scope 해석에 포함한다
      (task 스코프 규칙 + 그 task가 속한 project/global 규칙이 매칭된다).
    - id/identifier 모두 store.get_task가 해석한다."""
    st = (scope_type or "").strip().lower()
    sid = (scope_id or "").strip()
    tid = (task_id or "").strip()
    pid = (project_id or "").strip()
    if st == PermissionScopeType.TASK.value and sid and not tid:
        tid = sid
    elif st == PermissionScopeType.PROJECT.value and sid and not pid:
        pid = sid
    if tid:
        task = store.get_task(tid)
        if task is not None:
            # identifier('WMB-1') 입력도 canonical uuid로 정규화 — 저장된
            # scope_id와 판정 컨텍스트의 task_id가 항상 같은 형태여야 한다.
            tid = str(task.get("id") or tid)
            if not pid:
                pid = str(task.get("project_id") or "").strip()
    return tid, pid


def _matching_rules(
    store: Store,
    agent_id: str,
    capability: str,
    tid: str,
    pid: str,
) -> list[dict[str, Any]]:
    """컨텍스트에 매칭되는 규칙 — global + 해당 project + 해당 task.

    반환은 scope 좁은 순 정렬이다. 매칭 기준:
    - global 규칙은 항상 매칭.
    - project 규칙은 컨텍스트 project_id와 scope_id가 같을 때 매칭.
    - task 규칙은 컨텍스트 task_id와 scope_id가 같을 때 매칭."""
    rows = store.list_agent_permissions(agent_id.strip()[:64], capability=capability)
    out: list[dict[str, Any]] = []
    for r in rows:
        st = str(r.get("scope_type") or PermissionScopeType.GLOBAL.value)
        rsid = str(r.get("scope_id") or "")
        if st == PermissionScopeType.GLOBAL.value:
            out.append(r)
        elif st == PermissionScopeType.PROJECT.value and pid and rsid == pid:
            out.append(r)
        elif st == PermissionScopeType.TASK.value and tid and rsid == tid:
            out.append(r)
    out.sort(key=lambda r: _SCOPE_RANK.get(str(r.get("scope_type") or ""), 99))
    return out


def matching_permission_rules(
    store: Store,
    agent_id: str,
    capability: str,
    *,
    scope_type: str = "",
    scope_id: str = "",
    task_id: str = "",
    project_id: str = "",
) -> list[dict[str, Any]]:
    """(agent, capability)에 매칭되는 규칙 — scope 좁은 순 정렬."""
    tid, pid = _resolve_scope_ids(
        store,
        scope_type=scope_type,
        scope_id=scope_id,
        task_id=task_id,
        project_id=project_id,
    )
    return _matching_rules(store, agent_id, capability, tid, pid)


def evaluate_permission(
    store: Store,
    agent_id: str,
    capability: str,
    *,
    scope_type: str = "",
    scope_id: str = "",
    task_id: str = "",
    project_id: str = "",
) -> dict[str, Any]:
    """capability 판정 — {"decision": allow|deny|needs_approval, ...} 반환.

    - 매칭 규칙 중 deny가 하나라도 있으면 deny(deny 우선, scope 무관).
    - allow만 있으면: approval-required capability는 needs_approval,
      나머지는 allow.
    - 규칙 없음: approval-required capability는 deny(기본 deny),
      나머지는 allow(기본 허용 — permissions는 opt-in 제한 계층)."""
    cap = (capability or "").strip().lower()
    aid = (agent_id or "").strip()
    tid, pid = _resolve_scope_ids(
        store,
        scope_type=scope_type,
        scope_id=scope_id,
        task_id=task_id,
        project_id=project_id,
    )
    rules = _matching_rules(store, aid, cap, tid, pid)
    base: dict[str, Any] = {
        "agent_id": aid,
        "capability": cap,
        "task_id": tid,
        "project_id": pid,
        "rules_matched": len(rules),
        "scope_type": "",
        "scope_id": "",
        "approval_action": CAPABILITY_APPROVAL_ACTION.get(cap, ""),
    }
    deny_rules = [r for r in rules if str(r.get("effect")) == PermissionEffect.DENY.value]
    if deny_rules:
        # deny 우선 — 기여한 가장 좁은 scope를 기록한다.
        deny_rule = deny_rules[0]
        return {
            **base,
            "decision": DECISION_DENY,
            "effect": PermissionEffect.DENY.value,
            "scope_type": str(deny_rule.get("scope_type") or ""),
            "scope_id": str(deny_rule.get("scope_id") or ""),
        }
    if rules:
        # allow만 존재 — 가장 좁은(먼저 평가된) 규칙의 scope를 기록.
        allow_rule = rules[0]
        decision = (
            DECISION_NEEDS_APPROVAL if cap in APPROVAL_REQUIRED_CAPABILITIES else DECISION_ALLOW
        )
        return {
            **base,
            "decision": decision,
            "effect": PermissionEffect.ALLOW.value,
            "scope_type": str(allow_rule.get("scope_type") or ""),
            "scope_id": str(allow_rule.get("scope_id") or ""),
        }
    # 규칙 없음 — capability 기본값.
    if cap in APPROVAL_REQUIRED_CAPABILITIES:
        return {**base, "decision": DECISION_DENY, "effect": PermissionEffect.DENY.value}
    return {**base, "decision": DECISION_ALLOW, "effect": "default"}


def _permission_denied_message(capability: str, decision: dict[str, Any]) -> str:
    scope = str(decision.get("scope_type") or "")
    return f"agent lacks permission '{capability}'" + (f" (scope: {scope})" if scope else "")


def require_permission(
    store: Store,
    agent_id: str,
    capability: str,
    *,
    scope_type: str = "",
    scope_id: str = "",
    task_id: str = "",
    project_id: str = "",
    actor: str = "",
) -> dict[str, Any]:
    """agent-actor 경로의 강제 지점 — spec "서버가 반드시 다시 검증".

    - deny → ManagementError(403, 'permission_denied') + denial 로그
      (actor/capability/resource id만 — payload 원문 금지, spec H).
    - needs_approval → has_decided_approval(action, 'agent', agent_id)가
      없으면 ManagementError(409, 'approval_required'). 승인은
      (action_type, agent) standing grant — 과거 승인 이력이 있으면 계속
      유효하다(모듈 docstring 참조). 승인 row는 POST /management/approvals로
      사람이 만들고 결정한다 — high risk는 로그인된 lead/admin만 결정
      가능(ACTION_MIN_RISK 강제).
    - allow → 판정 dict 반환(호출자는 무시 가능)."""
    from .agent_management import ManagementError  # 지연 import — 순환 방지

    decision = evaluate_permission(
        store,
        agent_id,
        capability,
        scope_type=scope_type,
        scope_id=scope_id,
        task_id=task_id,
        project_id=project_id,
    )
    dec = str(decision.get("decision") or "")
    if dec == DECISION_DENY:
        logger.warning(
            "permission denied actor=%s agent=%s capability=%s scope=%s:%s task=%s project=%s",
            actor or "-",
            agent_id,
            capability,
            decision.get("scope_type") or "-",
            decision.get("scope_id") or "-",
            decision.get("task_id") or "-",
            decision.get("project_id") or "-",
        )
        raise ManagementError(
            403, "permission_denied", _permission_denied_message(capability, decision)
        )
    if dec == DECISION_NEEDS_APPROVAL:
        action = str(decision.get("approval_action") or "")
        approved = bool(action) and store.has_decided_approval(action, "agent", agent_id)
        if not approved:
            logger.warning(
                "permission approval required actor=%s agent=%s capability=%s action=%s",
                actor or "-",
                agent_id,
                capability,
                action or "-",
            )
            raise ManagementError(
                409,
                "approval_required",
                f"capability '{capability}' requires an approved '{action or 'approval'}'"
                " approval_request",
            )
    return decision


def validate_permission_rules(
    store: Store, agent_id: str, rules: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """PUT /permissions의 규칙 집합 검증 + 정규화 — ManagementError on 실패.

    - agent 존재 필수(404).
    - capability/effect/scope_type은 enum 어휘만 허용(422).
    - scope_type != global이면 scope_id 필수 + project/task 실존 검증,
      저장용 scope_id는 canonical id로 정규화한다(422).
    - 동일 (capability, scope_type, scope_id, effect) 중복은 422 —
      UNIQUE 제약 위반을 요청 단계에서 명확한 오류로 바꾼다.
    - 반환값은 store.replace_agent_permissions에 그대로 전달 가능한
      정규화 dict 목록."""
    from .agent_management import ManagementError  # 지연 import — 순환 방지

    agent = store.get_agent(agent_id)
    if agent is None:
        raise ManagementError(404, "not_found", "agent not found")
    caps = set(AGENT_CAPABILITIES)
    effects = set(PERMISSION_EFFECTS)
    scope_types = set(PERMISSION_SCOPE_TYPES)
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for i, raw in enumerate(rules):
        if not isinstance(raw, dict):
            raise ManagementError(422, "validation", f"permissions[{i}] must be an object")
        cap = str(raw.get("capability") or "").strip().lower()
        if cap not in caps:
            raise ManagementError(
                422, "validation", f"permissions[{i}].capability invalid: {cap or '(empty)'}"
            )
        eff = str(raw.get("effect") or "").strip().lower()
        if eff not in effects:
            raise ManagementError(422, "validation", f"permissions[{i}].effect must be allow|deny")
        st = str(raw.get("scope_type") or PermissionScopeType.GLOBAL.value).strip().lower()
        if st not in scope_types:
            raise ManagementError(
                422,
                "validation",
                f"permissions[{i}].scope_type must be global|project|task",
            )
        sid = str(raw.get("scope_id") or "").strip()[:200]
        if st == PermissionScopeType.GLOBAL.value:
            sid = ""
        elif not sid:
            raise ManagementError(
                422,
                "validation",
                f"permissions[{i}].scope_id is required for scope_type '{st}'",
            )
        elif st == PermissionScopeType.PROJECT.value:
            proj = store.get_project(sid)
            if proj is None:
                raise ManagementError(422, "validation", f"project not found: {sid}")
            sid = str(proj.get("id") or sid)
        elif st == PermissionScopeType.TASK.value:
            task = store.get_task(sid)
            if task is None:
                raise ManagementError(422, "validation", f"task not found: {sid}")
            sid = str(task.get("id") or sid)
        key = (cap, st, sid, eff)
        if key in seen:
            raise ManagementError(
                422,
                "validation",
                f"duplicate permission rule: {cap}/{st}/{sid or '-'}/{eff}",
            )
        seen.add(key)
        out.append(
            {
                "capability": cap,
                "effect": eff,
                "scope_type": st,
                "scope_id": sid,
            }
        )
    return out
