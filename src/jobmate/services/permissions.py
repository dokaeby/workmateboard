# mypy: disable-error-code="attr-defined"
"""Agent capability 권한 판정 — paperclip spec 3차 D.

agent_permissions 규칙을 해석해 allow|deny|needs_approval을 결정한다.

판정 규칙 (spec D):
- explicit deny가 allow보다 우선 — 매칭되는 scope 전체에서 deny 1건이면 deny.
- scope가 좁은 규칙(task > project > global)을 먼저 평가하되 deny 우선은 유지.
- 규칙이 없으면 capability 기본값 — 대부분 allow, 외부발송/배포/파괴 3종은
  기본 deny + 명시적 allow와 2차 approval을 모두 요구한다.
- 사람 관리자(세션 lead/member)와 머신 agent, 조직 사용자(user
  principal — spec 003 FR-022)의 권한 모델은 구분된다. agent 판정은
  agent_id 경로(evaluate_permission/require_permission), user 판정은
  evaluate_user_permission/require_user_permission이 담당하며
  user_permissions 규칙 + organization_members.role 기본값
  (ORG_ROLE_BASE_DECISIONS, FR-023)을 사용한다.

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
from ..store import orgctx
from ..store.db import Store
from .management_common import ManagementError

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

# spec 003 FR-023 — 조직 역할 → capability 기본 판정.
# member의 관리 계열 기본 deny 목록. FR-023이 명시하지 않은 비관리
# capability(task:create/task:update/task:assign/run:approve 등)는 기존
# member 행위 유지를 위해 allow로 둔다 — 개별 user_permissions 규칙이
# 이 역할 기본값보다 우선한다(deny > allow > role default).
MEMBER_DENIED_CAPABILITIES = frozenset(
    {
        AgentCapability.AGENT_MANAGE.value,
        AgentCapability.BUDGET_MANAGE.value,
        AgentCapability.INTEGRATION_MANAGE.value,
        AgentCapability.DEPLOY_EXECUTE.value,
        AgentCapability.DESTRUCTIVE_EXECUTE.value,
        AgentCapability.EXTERNAL_SEND.value,
    }
)


def _role_base_decisions(denied: frozenset[str]) -> dict[str, str]:
    return {cap: (DECISION_DENY if cap in denied else DECISION_ALLOW) for cap in AGENT_CAPABILITIES}


ORG_ROLE_BASE_DECISIONS: dict[str, dict[str, str]] = {
    "org_owner": _role_base_decisions(frozenset()),
    "org_admin": _role_base_decisions(frozenset()),
    "member": _role_base_decisions(MEMBER_DENIED_CAPABILITIES),
}


def resolve_project_scope_id(store: Store, ref: str) -> str:
    """project 참조(project id 또는 이름 문자열)를 permission scope_id
    (canonical project uuid)로 해석한다.

    run/wakeup input의 project 필드는 이름 문자열일 수 있으므로 판정 전에
    이 헬퍼로 정규화해야 project-scoped 규칙이 매칭된다. 해석 실패 시
    ""를 반환 — 호출자는 task_id 유도 project가 대신 쓰이게 둔다
    (api/routers/runs.py, wakeups.py promote 경로와 동일 규칙)."""
    ref = (ref or "").strip()
    if not ref:
        return ""
    proj = store.get_project(ref) or store.find_project_by_name(ref)
    if proj is None:
        return ""
    return str(proj.get("id") or "")


def _resolve_scope_ids(
    store: Store,
    *,
    scope_type: str = "",
    scope_id: str = "",
    task_id: str = "",
    project_id: str = "",
) -> tuple[str, str, str]:
    """판정 컨텍스트의 (task_id, project_id, conflict_project_id) 정규화.

    - 명시 scope_type/scope_id는 호출자가 지정한 컨텍스트로 취급한다.
    - task_id가 주어지면 해당 task의 project_id가 project 스코프의
      **정본**이다 — 호출자가 project_id를 함께 넘겨도 task가 속한
      project로 덮어쓴다(다른 project 컨텍스트를 끼워 넣어 project deny를
      우회하는 것을 막는다). 덮어쓴 값이 호출자 입력과 다르면
      conflict_project_id에 원래 pid를 담아 반환한다 — require_permission이
      422로 거부하고 evaluate_permission은 무시한다.
    - id/identifier 모두 store.get_task가 해석한다."""
    st = (scope_type or "").strip().lower()
    sid = (scope_id or "").strip()
    tid = (task_id or "").strip()
    pid = (project_id or "").strip()
    if st == PermissionScopeType.TASK.value and sid and not tid:
        tid = sid
    elif st == PermissionScopeType.PROJECT.value and sid and not pid:
        pid = sid
    conflict_pid = ""
    if tid:
        task = store.get_task(tid)
        if task is not None:
            # identifier('WMB-1') 입력도 canonical uuid로 정규화 — 저장된
            # scope_id와 판정 컨텍스트의 task_id가 항상 같은 형태여야 한다.
            tid = str(task.get("id") or tid)
            task_pid = str(task.get("project_id") or "").strip()
            if task_pid:
                if pid and pid != task_pid:
                    conflict_pid = pid
                pid = task_pid
    return tid, pid, conflict_pid


def _matching_rules_from_rows(
    rows: list[dict[str, Any]],
    tid: str,
    pid: str,
) -> list[dict[str, Any]]:
    """규칙 행 목록에서 컨텍스트 매칭 + scope 좁은 순 정렬.

    agent/user 양쪽 principal 경로가 공유하는 순수 매칭 로직 —
    org 검증(FR-026)은 호출 측에서 추가로 수행한다."""
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
    - task 규칙은 컨텍스트 task_id와 scope_id가 같을 때 매칭.

    FR-026 — project/task scoped 규칙은 리소스의 org가 agent의 org와
    같을 때만 매칭한다(교차 조직 권한 참조 방지). 리소스를 조회할 수
    없으면(ctx 필터로 숨겨진 cross-org 포함) 해당 규칙은 제외한다.
    global 규칙과 agent_org='' 레거시 조합은 org 비교를 적용하지 않는다
    — 양쪽 org가 모두 ''이면 동일 조직으로 간주해 기존 동작을 유지한다."""
    aid = agent_id.strip()[:64]
    rows = store.list_agent_permissions(aid, capability=capability)
    matched = _matching_rules_from_rows(rows, tid, pid)
    if not any(
        str(r.get("scope_type") or PermissionScopeType.GLOBAL.value)
        != PermissionScopeType.GLOBAL.value
        for r in matched
    ):
        return matched
    agent = store.get_agent(aid)
    agent_org = str((agent or {}).get("org_id") or "")
    org_cache: dict[tuple[str, str], str | None] = {}

    def _resource_org(scope_type: str, scope_id: str) -> str | None:
        """scoped 리소스의 org_id — 조회 불가(cross-org/삭제)면 None."""
        key = (scope_type, scope_id)
        if key not in org_cache:
            row: dict[str, Any] | None = None
            if scope_type == PermissionScopeType.PROJECT.value:
                row = store.get_project(scope_id)
            elif scope_type == PermissionScopeType.TASK.value:
                row = store.get_task(scope_id)
            org_cache[key] = None if row is None else str(row.get("org_id") or "")
        return org_cache[key]

    out: list[dict[str, Any]] = []
    for r in matched:
        st = str(r.get("scope_type") or PermissionScopeType.GLOBAL.value)
        if st == PermissionScopeType.GLOBAL.value:
            out.append(r)
            continue
        res_org = _resource_org(st, str(r.get("scope_id") or ""))
        # res_org None = 리소스 부재/ctx 밖 — org를 검증할 수 없으므로 제외.
        if res_org is not None and res_org == agent_org:
            out.append(r)
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
    tid, pid, _conflict = _resolve_scope_ids(
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
    tid, pid, conflict_pid = _resolve_scope_ids(
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
    if conflict_pid:
        base["conflict_project_id"] = conflict_pid
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


def _permission_denied_message(
    capability: str, decision: dict[str, Any], principal_type: str = "agent"
) -> str:
    scope = str(decision.get("scope_type") or "")
    return f"{principal_type} lacks permission '{capability}'" + (
        f" (scope: {scope})" if scope else ""
    )


def _enforce_decision(
    store: Store,
    principal_id: str,
    capability: str,
    decision: dict[str, Any],
    *,
    actor: str,
    principal_type: str = "agent",
) -> None:
    """판정 dict를 강제한다 — deny/needs_approval은 ManagementError.

    principal_type은 approval grant의 resource_type('agent'|'user')과
    로그 필드에 사용된다 — 판정 로직 자체는 principal 종류와 무관하다."""
    dec = str(decision.get("decision") or "")
    if dec == DECISION_DENY:
        logger.warning(
            "permission denied actor=%s principal=%s:%s capability=%s"
            " scope=%s:%s task=%s project=%s",
            actor or "-",
            principal_type,
            principal_id,
            capability,
            decision.get("scope_type") or "-",
            decision.get("scope_id") or "-",
            decision.get("task_id") or "-",
            decision.get("project_id") or "-",
        )
        raise ManagementError(
            403,
            "permission_denied",
            _permission_denied_message(capability, decision, principal_type),
        )
    if dec == DECISION_NEEDS_APPROVAL:
        action = str(decision.get("approval_action") or "")
        approved = bool(action) and store.has_decided_approval(action, principal_type, principal_id)
        if not approved:
            logger.warning(
                "permission approval required actor=%s principal=%s:%s capability=%s action=%s",
                actor or "-",
                principal_type,
                principal_id,
                capability,
                action or "-",
            )
            raise ManagementError(
                409,
                "approval_required",
                f"capability '{capability}' requires an approved '{action or 'approval'}'"
                " approval_request",
            )


def require_permission(
    store: Store,
    agent_id: str,
    capability: str,
    *,
    scope_type: str = "",
    scope_id: str = "",
    task_id: str = "",
    project_id: str = "",
    target_project_id: str = "",
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
    - allow → 판정 dict 반환(호출자는 무시 가능).

    target_project_id — "이동 후" 스코프 평가(R2-1). task/project를 다른
    project로 옮기는 연산에서, 현재 컨텍스트(task→project 정본) 평가가
    통과한 뒤 대상 project 스코프를 두 번째 컨텍스트로 평가한다.
    project_id 인자와 달리 task_id와 함께 넘겨도 _resolve_scope_ids 충돌
    검사를 거치지 않는다 — 대상 project는 '현재'가 아니라 '목표' 컨텍스트다.
    현재 컨텍스트 평가가 먼저 강제되므로 deny/needs_approval 우선순위는
    기존 두 번의 require_permission 호출과 동일하다. 호출자가 현재와
    같은 project를 넘기면 동일 규칙을 재평가할 뿐 결과는 같다."""
    # task 컨텍스트가 있으면 task.project_id가 project 스코프의 정본이다.
    # 호출자가 다른 project_id를 함께 넘기면(두 스코프가 충돌) 조용히
    # 한쪽을 무시하지 않고 422로 거부한다 — project deny 우회 방지.
    _tid, _pid, conflict_pid = _resolve_scope_ids(
        store,
        scope_type=scope_type,
        scope_id=scope_id,
        task_id=task_id,
        project_id=project_id,
    )
    if conflict_pid:
        raise ManagementError(
            422,
            "validation",
            "project_id conflicts with task's project "
            f"(task belongs to {_pid or 'no project'}, got {conflict_pid})",
        )
    decision = evaluate_permission(
        store,
        agent_id,
        capability,
        scope_type=scope_type,
        scope_id=scope_id,
        task_id=task_id,
        project_id=project_id,
    )
    _enforce_decision(store, agent_id, capability, decision, actor=actor)
    # 이동 대상 project 스코프 — 현재 컨텍스트 강제가 모두 통과한 뒤에만
    # 평가한다(기존 patch_task의 두 번째 require_permission 호출과 동일 순서).
    target = (target_project_id or "").strip()
    if target:
        target_decision = evaluate_permission(
            store,
            agent_id,
            capability,
            project_id=target,
        )
        _enforce_decision(store, agent_id, capability, target_decision, actor=actor)
    return decision


def _user_role_default(store: Store, user_id: str, capability: str) -> tuple[str, str]:
    """현재 org ctx의 organization_members.role → (decision, role).

    Returns ("", "") when there is no role default — orgctx가 비어
    있거나(ctx='' 시스템/내부 경로) 현재 조직의 active 멤버십이 없을
    때. 이 경우 호출자는 agent 경로와 동일한 capability 기본값
    (approval-required→deny, 나머지 allow)으로 폴백한다 — 역할 정보가
    없는 principal에게 조직 관리자급 기본값을 부여하지 않기 위함이다.
    알 수 없는 role 문자열은 가장 제한적인 member 매핑으로 처리한다."""
    oid = orgctx.current_org_id()
    if not oid:
        return "", ""
    membership = store.get_org_membership(oid, user_id)
    if membership is None:
        return "", ""
    role = str(membership.get("role") or "")
    decisions = ORG_ROLE_BASE_DECISIONS.get(role) or ORG_ROLE_BASE_DECISIONS["member"]
    return decisions.get(capability, DECISION_ALLOW), role


def evaluate_user_permission(
    store: Store,
    user_id: str,
    capability: str,
    *,
    scope_type: str = "",
    scope_id: str = "",
    task_id: str = "",
    project_id: str = "",
) -> dict[str, Any]:
    """user principal의 capability 판정 — spec 003 FR-022/023.

    반환 shape는 evaluate_permission와 동일 + "principal": "user",
    "user_id". 결정 우선순위(FR-023): explicit deny 규칙 > explicit
    allow 규칙 > 조직 역할 기본값(ORG_ROLE_BASE_DECISIONS) > capability
    기본값. user_permissions 행은 store.list_user_permissions가 현재
    org ctx로 필터링하므로 agent 경로의 FR-026 리소스 org 검사를
    반복하지 않는다(규칙 행 자체가 ctx 조직 소속).

    역할 기본값 판정: approval-required capability가 역할상 allow면
    needs_approval로 승격한다(explicit allow 경로와 동일). 역할 기본값이
    deny면 deny — member의 관리 계열 deny가 여기서 결정된다."""
    cap = (capability or "").strip().lower()
    uid = (user_id or "").strip()
    tid, pid, conflict_pid = _resolve_scope_ids(
        store,
        scope_type=scope_type,
        scope_id=scope_id,
        task_id=task_id,
        project_id=project_id,
    )
    rows = store.list_user_permissions(uid, capability=cap)
    rules = _matching_rules_from_rows(rows, tid, pid)
    base: dict[str, Any] = {
        "principal": "user",
        "user_id": uid,
        "capability": cap,
        "task_id": tid,
        "project_id": pid,
        "rules_matched": len(rules),
        "scope_type": "",
        "scope_id": "",
        "approval_action": CAPABILITY_APPROVAL_ACTION.get(cap, ""),
    }
    if conflict_pid:
        base["conflict_project_id"] = conflict_pid
    deny_rules = [r for r in rules if str(r.get("effect")) == PermissionEffect.DENY.value]
    if deny_rules:
        deny_rule = deny_rules[0]
        return {
            **base,
            "decision": DECISION_DENY,
            "effect": PermissionEffect.DENY.value,
            "scope_type": str(deny_rule.get("scope_type") or ""),
            "scope_id": str(deny_rule.get("scope_id") or ""),
        }
    if rules:
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
    # 규칙 없음 — 조직 역할 기본값(FR-023). 역할 기본값이 없으면
    # agent 경로와 동일한 capability 기본값으로 폴백한다.
    role_decision, role = _user_role_default(store, uid, cap)
    if role_decision == DECISION_DENY:
        return {
            **base,
            "decision": DECISION_DENY,
            "effect": PermissionEffect.DENY.value,
            "role": role,
        }
    if role_decision == DECISION_ALLOW:
        decision = (
            DECISION_NEEDS_APPROVAL if cap in APPROVAL_REQUIRED_CAPABILITIES else DECISION_ALLOW
        )
        return {
            **base,
            "decision": decision,
            "effect": "role_default",
            "role": role,
        }
    # 역할 정보 없음(ctx='' 또는 비멤버) — capability 기본값.
    if cap in APPROVAL_REQUIRED_CAPABILITIES:
        return {**base, "decision": DECISION_DENY, "effect": PermissionEffect.DENY.value}
    return {**base, "decision": DECISION_ALLOW, "effect": "default"}


def require_user_permission(
    store: Store,
    user_id: str,
    capability: str,
    *,
    scope_type: str = "",
    scope_id: str = "",
    task_id: str = "",
    project_id: str = "",
    target_project_id: str = "",
    actor: str = "",
) -> dict[str, Any]:
    """user-actor 경로의 강제 지점 — require_permission의 user 판.

    의미론은 동일: conflict project_id는 422, deny→403,
    needs_approval→승인 없으면 409. 승인 grant는
    has_decided_approval(action, 'user', user_id)로 확인한다 —
    agent 경로의 (action,'agent',agent_id)와 별도 standing grant."""
    _tid, _pid, conflict_pid = _resolve_scope_ids(
        store,
        scope_type=scope_type,
        scope_id=scope_id,
        task_id=task_id,
        project_id=project_id,
    )
    if conflict_pid:
        raise ManagementError(
            422,
            "validation",
            "project_id conflicts with task's project "
            f"(task belongs to {_pid or 'no project'}, got {conflict_pid})",
        )
    decision = evaluate_user_permission(
        store,
        user_id,
        capability,
        scope_type=scope_type,
        scope_id=scope_id,
        task_id=task_id,
        project_id=project_id,
    )
    _enforce_decision(store, user_id, capability, decision, actor=actor, principal_type="user")
    target = (target_project_id or "").strip()
    if target:
        target_decision = evaluate_user_permission(
            store,
            user_id,
            capability,
            project_id=target,
        )
        _enforce_decision(
            store,
            user_id,
            capability,
            target_decision,
            actor=actor,
            principal_type="user",
        )
    return decision


def _validate_permission_rule_set(
    store: Store, rules: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """규칙 집합 검증 + 정규화 공통부 — principal 존재 확인은 호출자 몫.

    - capability/effect/scope_type은 enum 어휘만 허용(422).
    - scope_type != global이면 scope_id 필수 + project/task 실존 검증,
      저장용 scope_id는 canonical id로 정규화한다(422). get_project/
      get_task는 ctx 조직 필터가 적용되므로 cross-org scope_id는
      자연스럽게 'not found'로 실패한다.
    - 동일 (capability, scope_type, scope_id, effect) 중복은 422 —
      UNIQUE 제약 위반을 요청 단계에서 명확한 오류로 바꾼다.
    - 반환값은 store.replace_*_permissions에 그대로 전달 가능한
      정규화 dict 목록."""
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


def validate_permission_rules(
    store: Store, agent_id: str, rules: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """PUT /permissions의 규칙 집합 검증 + 정규화 — ManagementError on 실패.

    - agent 존재 필수(404).
    - 규칙 검증/정규화는 _validate_permission_rule_set과 동일."""
    agent = store.get_agent(agent_id)
    if agent is None:
        raise ManagementError(404, "not_found", "agent not found")
    return _validate_permission_rule_set(store, rules)


def validate_user_permission_rules(
    store: Store, user_id: str, org_id: str, rules: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """user_permissions PUT의 규칙 집합 검증 + 정규화 — spec 003 FR-024.

    - user 존재 필수(404, store.get_user_by_id).
    - org_id 조직의 active 멤버십 필수 — 비멤버는 422 validation
      (get_org_membership은 ctx 무관하게 명시 org_id로 조회한다).
    - 규칙 검증/정규화는 _validate_permission_rule_set과 동일 —
      scope_id의 project/task 실존 검증은 ctx 조직 필터를 타므로
      cross-org scope는 자연스럽게 실패한다."""
    user = store.get_user_by_id((user_id or "").strip())
    if user is None:
        raise ManagementError(404, "not_found", "user not found")
    if store.get_org_membership((org_id or "").strip(), (user_id or "").strip()) is None:
        raise ManagementError(422, "validation", "user is not an org member")
    return _validate_permission_rule_set(store, rules)
