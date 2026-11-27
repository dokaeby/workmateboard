# mypy: disable-error-code="attr-defined"
"""범용 승인 서비스 — paperclip spec 2차 D.

approval_requests 생성/조회/결정 + 결정 부수효과(run resume, budget raise,
agent terminate, recovery resolve)를 담당한다. 상태 CAS는 store가,
권한( high/critical → 로그인된 사람) 검증은 라우터가 담당하고, 여기서는
도메인 규칙(safe_payload secret 거부, 액션별 부수효과, 감사)만 둔다.

부수효과 실패는 결정을 되돌리지 않는다 — spec "승인 후 실행 재개가
실패하면 승인 기록을 되돌리지 말고 follow-up error event를 남긴다".
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from .. import budget_scope
from ..models import (
    AgentStatus,
    ApprovalActionType,
    ApprovalRisk,
    ApprovalStatus,
)
from ..services import agent_management as mgmt
from ..store.db import Store

logger = logging.getLogger("jobmate.approvals")

APPROVAL_ACTION_TYPES = {a.value for a in ApprovalActionType}
APPROVAL_RISKS = {r.value for r in ApprovalRisk}
_HIGH_RISK = {ApprovalRisk.HIGH.value, ApprovalRisk.CRITICAL.value}
_MAX_SUMMARY = 300
_MAX_COMMENT = 500
_MAX_PAYLOAD_CHARS = 8000

# spec D: high/critical 결정은 로그인된 사람만 — 요청자가 risk_level을
# 낮춰 머신 자기승인으로 부수효과를 실행하는 것을 막기 위해 action_type별
# 최소 risk를 강제한다(요청값이 낮으면 상향 보정 — 거부보다 호환적).
# 사람 승인 없이 실행되면 안 되는 파괴/외부/비용 액션은 high(=사람 결정
# 강제), 그 외는 medium/low.
_RISK_ORDER = {
    ApprovalRisk.LOW.value: 0,
    ApprovalRisk.MEDIUM.value: 1,
    ApprovalRisk.HIGH.value: 2,
    ApprovalRisk.CRITICAL.value: 3,
}
ACTION_MIN_RISK: dict[str, str] = {
    ApprovalActionType.AGENT_TERMINATE.value: ApprovalRisk.HIGH.value,
    ApprovalActionType.BUDGET_RAISE.value: ApprovalRisk.HIGH.value,
    ApprovalActionType.EXTERNAL_SEND.value: ApprovalRisk.HIGH.value,
    ApprovalActionType.DEPLOY.value: ApprovalRisk.HIGH.value,
    ApprovalActionType.DESTRUCTIVE_TOOL.value: ApprovalRisk.HIGH.value,
    ApprovalActionType.AGENT_HIRE.value: ApprovalRisk.MEDIUM.value,
    ApprovalActionType.RUN_RESUME.value: ApprovalRisk.MEDIUM.value,
    ApprovalActionType.RECOVERY.value: ApprovalRisk.LOW.value,
}


def effective_risk_level(action_type: str, risk_level: str) -> str:
    """action_type 최소 risk와 요청 risk 중 높은 쪽 — 생성 보정과
    결정 게이트가 같은 기준을 쓰도록 한 곳에 둔다."""
    at = (action_type or "").strip().lower()
    rk = (risk_level or "").strip().lower()
    floor = ACTION_MIN_RISK.get(at, ApprovalRisk.LOW.value)
    if _RISK_ORDER.get(rk, 1) < _RISK_ORDER.get(floor, 0):
        return floor
    return rk


def _normalize_expires_at(value: str | None) -> str | None:
    """expires_at ISO-8601 검증 + UTC 정규화 — None/빈 문자열은 만료 없음.

    expiry 판정은 문자열 사전식 비교라 'Z'·naive·오프셋 혼재가 저장되면
    비교가 어긋난다. naive는 UTC로 간주하고, 과거 시각은 허용한다
    (생성 즉시 expired로 읽히는 게 명시적 의도)."""
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        raise mgmt.ManagementError(422, "validation", "expires_at must be ISO-8601") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


# resume_fn(run_id, decision, comment, actor) -> {"ok": bool, ...}
# app.py의 _do_approve를 라우터가 주입한다 — 서비스가 app 스코프를
# 알지 않게 하기 위함.
ResumeFn = Callable[[str, str, str, str], dict[str, Any]]


def _walk_payload_keys(value: Any, path: str = "") -> Any:
    """safe_payload dict를 재귀 순회하며 secret-like 키를 찾는다.

    값은 보지 않고 키 이름만 본다 — 'credentials' 같은 안전한 키 아래
    중첩된 'api_key'도 잡아야 하므로 재귀한다."""
    if isinstance(value, dict):
        for k, v in value.items():
            key_path = f"{path}.{k}" if path else str(k)
            if mgmt._SECRET_KEY_RE.search(str(k)):  # noqa: SLF001 — 동일 규칙 재사용
                return key_path
            found = _walk_payload_keys(v, key_path)
            if found:
                return found
    elif isinstance(value, list):
        for i, v in enumerate(value):
            found = _walk_payload_keys(v, f"{path}[{i}]")
            if found:
                return found
    return None


def validate_safe_payload(payload: Any) -> dict[str, Any]:
    """safe_payload 검증 — dict 아니거나 secret-like 키 포함 시 422.

    spec D: "safe_payload JSON: secret-like key를 validation 단계에서 거부".
    값 마스킹이 아니라 저장 자체를 거부한다 — 비밀값이 DB에 남는 경로를
    원천 차단."""
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise mgmt.ManagementError(422, "validation", "safe_payload must be an object")
    bad = _walk_payload_keys(payload)
    if bad:
        # 키 경로만 남기고 값은 절대 로깅/응답에 포함하지 않는다.
        raise mgmt.ManagementError(
            422,
            "secret_payload",
            f"safe_payload key '{bad}' looks like a secret; remove it before submitting",
        )
    # safe 키 아래 URL 문자열에 박힌 userinfo credential도 지운다 —
    # https://user:pass@host 형태가 승인함에 그대로 남는 것을 방지.
    # secret-like 키는 위에서 이미 거부됐으므로 redact는 URL 스크럽만 수행.
    return mgmt.redact_adapter_config(payload)


def _validate_refs(
    store: Store,
    *,
    requested_by_agent_id: str | None,
    task_id: str | None,
    run_id: str | None,
) -> None:
    """dangling 참조 거부 — cost-events와 같은 규칙. 존재하지 않는
    agent/task/run을 가리키는 승인 요청은 생성 시점에 422."""
    if requested_by_agent_id and store.get_agent(requested_by_agent_id) is None:
        raise mgmt.ManagementError(422, "validation", "requested_by_agent_id not found")
    if task_id and store.get_task(task_id) is None:
        raise mgmt.ManagementError(422, "validation", "task_id not found")
    if run_id and store.get_run(run_id) is None:
        raise mgmt.ManagementError(422, "validation", "run_id not found")


def _validate_action_binding(
    store: Store,
    *,
    action_type: str,
    payload: dict[str, Any],
    resource_type: str,
    resource_id: str,
    run_id: str | None,
) -> None:
    """action_type별 resource↔safe_payload 일관성 — 승인 row가 묶인
    resource와 부수효과가 실제로 건드리는 대상을 같게 강제한다.

    agent_terminate: resource_type='agent' + resource_id 필수, payload의
    agent_id는 있으면 resource_id와 일치해야 한다(우선순위는 resource_id —
    has_decided_approval 가드가 resource_id로 조회하기 때문).
    recovery: payload.recovery_id와 resource_id가 둘 다 있으면 일치 필수.
    run_resume/budget_raise: 참조 무결성."""
    rt = (resource_type or "").strip().lower()
    rid = (resource_id or "").strip()
    if action_type == ApprovalActionType.AGENT_TERMINATE.value:
        if rt != "agent" or not rid:
            raise mgmt.ManagementError(
                422,
                "validation",
                "agent_terminate requires resource_type='agent' and resource_id",
            )
        paid = str(payload.get("agent_id") or "").strip()
        if paid and paid != rid:
            raise mgmt.ManagementError(
                422,
                "validation",
                "safe_payload.agent_id must equal resource_id",
            )
    elif action_type == ApprovalActionType.RECOVERY.value:
        prid = str(payload.get("recovery_id") or "").strip()
        if prid and rid and prid != rid:
            raise mgmt.ManagementError(
                422,
                "validation",
                "safe_payload.recovery_id must equal resource_id",
            )
        if not prid and not rid:
            raise mgmt.ManagementError(422, "validation", "recovery approval requires recovery_id")
    elif action_type == ApprovalActionType.RUN_RESUME.value:
        prid = str(payload.get("run_id") or "").strip()
        if prid:
            if run_id and prid != run_id:
                raise mgmt.ManagementError(
                    422, "validation", "safe_payload.run_id must equal run_id"
                )
            if store.get_run(prid) is None:
                raise mgmt.ManagementError(422, "validation", "run_id not found")
        if rt == "run" and rid and run_id and rid != run_id:
            raise mgmt.ManagementError(
                422, "validation", "resource_id must equal run_id for run_resume"
            )
    elif action_type == ApprovalActionType.BUDGET_RAISE.value:
        iid = str(payload.get("incident_id") or "").strip()
        if iid:
            if store.get_budget_incident(iid) is None:
                raise mgmt.ManagementError(422, "validation", "budget incident not found")
        else:
            st = str(payload.get("scope_type") or "global").strip().lower()
            if st not in budget_scope.SCOPE_TYPES:
                raise mgmt.ManagementError(
                    422,
                    "validation",
                    f"scope_type must be one of {list(budget_scope.SCOPE_TYPES)}",
                )
            if payload.get("amount_microusd") is None and payload.get("amount_usd") is None:
                raise mgmt.ManagementError(
                    422,
                    "validation",
                    "budget_raise requires incident_id or amount_microusd/amount_usd",
                )


def _check_payload_size(payload: dict[str, Any]) -> None:
    """safe_payload는 통째로 JSON 저장 — 8000자를 넘으면 거부한다.
    잘린 JSON은 파싱 실패로 {}가 돼 부수효과 필드를 조용히 잃으므로
    절대 저장하지 않는다(store도 동일 한도로 방어)."""
    if len(json.dumps(payload, ensure_ascii=False, default=str)) > _MAX_PAYLOAD_CHARS:
        raise mgmt.ManagementError(
            422, "validation", f"safe_payload exceeds {_MAX_PAYLOAD_CHARS} chars"
        )


def request_approval(
    store: Store,
    *,
    action_type: str,
    risk_level: str = ApprovalRisk.MEDIUM.value,
    summary: str = "",
    safe_payload: dict[str, Any] | None = None,
    requested_by: str = "",
    requested_by_agent_id: str | None = None,
    task_id: str | None = None,
    run_id: str | None = None,
    resource_type: str = "",
    resource_id: str = "",
    expires_at: str | None = None,
    dedupe: bool = False,
) -> tuple[dict[str, Any] | None, bool]:
    """승인 요청 생성 — (row, created). 검증 실패 시 ManagementError.

    dedupe=True면 같은 action_type+resource의 pending 요청이 있으면
    새로 만들지 않고 기존 행을 반환한다(watchdog 자동 생성 경로의
    매 tick 중복 방지)."""
    at = (action_type or "").strip().lower()
    if at not in APPROVAL_ACTION_TYPES:
        raise mgmt.ManagementError(422, "validation", f"invalid action_type: {action_type}")
    rk = (risk_level or ApprovalRisk.MEDIUM.value).strip().lower()
    if rk not in APPROVAL_RISKS:
        raise mgmt.ManagementError(422, "validation", f"invalid risk_level: {risk_level}")
    # C1 — action_type 최소 risk로 상향 보정. low로 생성해 머신이
    # 스스로 결정하는 우회를 차단한다.
    rk = effective_risk_level(at, rk)
    summary = (summary or "").strip()[:_MAX_SUMMARY]
    payload = validate_safe_payload(safe_payload)
    _check_payload_size(payload)
    _validate_refs(
        store,
        requested_by_agent_id=requested_by_agent_id,
        task_id=task_id,
        run_id=run_id,
    )
    _validate_action_binding(
        store,
        action_type=at,
        payload=payload,
        resource_type=resource_type,
        resource_id=resource_id,
        run_id=run_id,
    )
    if dedupe and resource_type and resource_id:
        existing = store.find_pending_approval(at, resource_type, resource_id)
        if existing is not None:
            return existing, False
    row = store.create_approval_request(
        action_type=at,
        risk_level=rk,
        summary=summary,
        safe_payload=payload,
        requested_by=requested_by,
        requested_by_agent_id=requested_by_agent_id,
        task_id=task_id,
        run_id=run_id,
        resource_type=resource_type,
        resource_id=resource_id,
        expires_at=_normalize_expires_at(expires_at),
    )
    return row, row is not None


def list_approvals(store: Store, **filters: Any) -> list[dict[str, Any]]:
    return store.list_approval_requests(**filters)


def _apply_budget_raise(store: Store, row: dict[str, Any], comment: str, decided_by: str) -> None:
    """budget_raise 결정 부수효과 — incident 해소 또는 정책 한도 상향."""
    payload = row.get("safe_payload") or {}
    incident_id = str(payload.get("incident_id") or "").strip()
    if incident_id:
        result = budget_scope.resolve_incident(
            store,
            incident_id,
            "raise_budget_and_resume",
            amount_usd=payload.get("amount_usd"),
            amount_microusd=payload.get("amount_microusd"),
            note=comment,
            actor=decided_by or "approval",
        )
        if result is None:
            raise mgmt.ManagementError(409, "conflict", "budget incident not found or not open")
        return
    scope_type = str(payload.get("scope_type") or "global").strip().lower()
    if scope_type not in budget_scope.SCOPE_TYPES:
        raise mgmt.ManagementError(
            422, "validation", f"scope_type must be one of {list(budget_scope.SCOPE_TYPES)}"
        )
    scope_id = str(payload.get("scope_id") or "").strip()
    amount_mu = payload.get("amount_microusd")
    amount_usd = payload.get("amount_usd")
    if amount_mu is None and amount_usd is None:
        raise mgmt.ManagementError(
            422, "validation", "budget_raise requires amount_microusd or amount_usd"
        )
    budget_scope.upsert_policy(
        store,
        scope_type,
        scope_id,
        amount_usd=amount_usd if amount_usd is not None else budget_scope.UNSET,
        amount_microusd=amount_mu if amount_mu is not None else budget_scope.UNSET,
    )


def _apply_agent_terminate(store: Store, row: dict[str, Any], decided_by: str) -> None:
    """agent_terminate 결정 부수효과 — 승인된 요청 자체가 권한.

    resource_id의 agent를 terminated로 전이한다.
    transition_agent의 terminate_authorized 플래그가 승인 검사를
    우회하도록 한다 — 이 경로가 도달했다는 것 자체가 approved 결정의
    결과이기 때문."""
    # 대상은 resource_id가 단일 기준 — payload.agent_id는 생성/결정
    # 검증에서 resource_id와의 일치를 강제하므로 여기서 다시 우선하지
    # 않는다(승인 row가 묶인 resource와 실제 종료 대상을 같게 유지).
    agent_id = str(row.get("resource_id") or "").strip()
    if not agent_id:
        raise mgmt.ManagementError(422, "validation", "agent_terminate requires resource_id")
    if str(row.get("resource_type") or "") and str(row.get("resource_type")) != "agent":
        raise mgmt.ManagementError(
            422, "validation", "agent_terminate resource_type must be 'agent'"
        )
    mgmt.transition_agent(
        store,
        agent_id,
        AgentStatus.TERMINATED.value,
        actor=decided_by or "approval",
        terminate_authorized=True,
    )


def _apply_recovery_resolve(store: Store, row: dict[str, Any], comment: str) -> None:
    """recovery 승인 → 연결된 recovery_action을 resolved로."""
    payload = row.get("safe_payload") or {}
    # resource_id 우선 — payload.recovery_id는 생성/결정 검증이
    # resource_id와의 일치를 강제한다.
    rec_id = str(row.get("resource_id") or payload.get("recovery_id") or "").strip()
    if not rec_id:
        raise mgmt.ManagementError(422, "validation", "recovery approval requires recovery_id")
    if not store.set_recovery_status(
        rec_id, "resolved", resolution_note=comment or "approved via approval_request"
    ):
        raise mgmt.ManagementError(409, "conflict", "recovery action not open")


def _apply_decision(
    store: Store,
    row: dict[str, Any],
    decision: str,
    *,
    decided_by: str,
    comment: str,
    actor: str,
    resume_fn: ResumeFn | None,
) -> None:
    """승인/거절의 도메인 부수효과 — 실패 시 예외를 올려 호출자가
    감사/이벤트로 기록하게 한다(결정 자체는 이미 커밋됨)."""
    action = str(row.get("action_type") or "")
    if decision != "approve":
        # 거절 — run_resume 거절은 run도 reject로 닫는다(기존
        # /runs/{id}/approve의 reject와 동일 의미). 그 외 액션은 기록만.
        if action == ApprovalActionType.RUN_RESUME.value:
            rid = str(row.get("run_id") or (row.get("safe_payload") or {}).get("run_id") or "")
            if rid and resume_fn is not None:
                result = resume_fn(rid, "reject", comment, actor or decided_by or "approval")
                # 거절 resume 실패도 승인 기록은 유지 — approve 경로와
                # 동일하게 예외를 올려 호출자의 error 감사/이벤트로 남긴다.
                if isinstance(result, dict) and result.get("ok") is False:
                    raise mgmt.ManagementError(
                        502,
                        "resume_failed",
                        str(result.get("error") or "resume reject failed")[:300],
                    )
        return
    if action == ApprovalActionType.RUN_RESUME.value:
        rid = str(row.get("run_id") or (row.get("safe_payload") or {}).get("run_id") or "")
        if not rid:
            raise mgmt.ManagementError(422, "validation", "run_resume requires run_id")
        if resume_fn is None:
            raise mgmt.ManagementError(503, "unavailable", "run resume is not available")
        result = resume_fn(rid, "approve", comment, actor or decided_by or "approval")
        if not result.get("ok"):
            # spec: 승인 기록은 유지 — 실패는 follow-up error로 남긴다.
            raise mgmt.ManagementError(
                502, "resume_failed", str(result.get("error") or "resume failed")[:300]
            )
        return
    if action == ApprovalActionType.BUDGET_RAISE.value:
        _apply_budget_raise(store, row, comment, decided_by)
        return
    if action == ApprovalActionType.AGENT_TERMINATE.value:
        _apply_agent_terminate(store, row, decided_by)
        return
    if action == ApprovalActionType.RECOVERY.value:
        _apply_recovery_resolve(store, row, comment)
        return
    # agent_hire/external_send/deploy/destructive_tool — 이 단계에서는
    # 결정 기록 + 감사만 한다. 실제 실행은 각 도메인의 명시적 API가
    # 담당하며, 승인 row는 그 실행의 근거로 참조된다.
    logger.info(
        "approval %s decided but has no automatic side-effect (action=%s)",
        row.get("id"),
        action,
    )


def _prevalidate_decision(store: Store, row: dict[str, Any], decision: str) -> None:
    """CAS 전에 부수효과 실행 가능성을 검증 — spec D의 '결정+상태 변경+
    감사는 같은 transaction'에 최대한 가깝게 만들기 위해, 대부분의 실패
    (대상 소실/이미 결정됨/바인딩 불일치/파라미터 부재)를 커밋 전에
    ManagementError로 차단한다. 승인 row는 pending으로 남는다.

    완전한 원자성은 아니다 — resume_fn·budget resolve처럼 트랜잭션에
    참여할 수 없는 부수효과는 커밋 후 실행되며 실패 시
    approval_side_effect_error 감사로 보상한다(잔여 비원자성은
    docs/jobMate_v1.0.1/04_기능별상세설계서_v1.0.1.md 부록 A §26에 명시)."""
    if decision != "approve":
        return
    action = str(row.get("action_type") or "")
    payload = row.get("safe_payload") or {}
    rt = str(row.get("resource_type") or "").strip().lower()
    rid = str(row.get("resource_id") or "").strip()

    def _mismatch(msg: str) -> mgmt.ManagementError:
        return mgmt.ManagementError(422, "validation", msg)

    if action == ApprovalActionType.AGENT_TERMINATE.value:
        if rt != "agent" or not rid:
            raise _mismatch("agent_terminate requires resource_type='agent' and resource_id")
        paid = str(payload.get("agent_id") or "").strip()
        if paid and paid != rid:
            raise _mismatch("safe_payload.agent_id must equal resource_id")
        if store.get_agent(rid) is None:
            raise mgmt.ManagementError(404, "not_found", "agent not found")
    elif action == ApprovalActionType.RECOVERY.value:
        prid = str(payload.get("recovery_id") or "").strip()
        if prid and rid and prid != rid:
            raise _mismatch("safe_payload.recovery_id must equal resource_id")
        rec_id = rid or prid
        if not rec_id:
            raise _mismatch("recovery approval requires recovery_id")
        rec = store.get_recovery_action(rec_id)
        if rec is None:
            raise mgmt.ManagementError(404, "not_found", "recovery action not found")
        if str(rec.get("status") or "") not in ("active", "escalated"):
            raise mgmt.ManagementError(409, "conflict", "recovery action not open")
    elif action == ApprovalActionType.RUN_RESUME.value:
        run_ref = str(row.get("run_id") or payload.get("run_id") or "").strip()
        prid = str(payload.get("run_id") or "").strip()
        if prid and run_ref and prid != run_ref:
            raise _mismatch("safe_payload.run_id must equal run_id")
        if not run_ref:
            raise _mismatch("run_resume requires run_id")
        if store.get_run(run_ref) is None:
            raise mgmt.ManagementError(404, "not_found", "run not found")
    elif action == ApprovalActionType.BUDGET_RAISE.value:
        iid = str(payload.get("incident_id") or "").strip()
        if iid:
            inc = store.get_budget_incident(iid)
            if inc is None:
                raise mgmt.ManagementError(404, "not_found", "budget incident not found")
            if str(inc.get("status") or "") != "open":
                raise mgmt.ManagementError(409, "conflict", "budget incident not open")
        else:
            st = str(payload.get("scope_type") or "global").strip().lower()
            if st not in budget_scope.SCOPE_TYPES:
                raise _mismatch(f"scope_type must be one of {list(budget_scope.SCOPE_TYPES)}")
            if payload.get("amount_microusd") is None and payload.get("amount_usd") is None:
                raise _mismatch("budget_raise requires amount_microusd or amount_usd")


def decide_approval(
    store: Store,
    approval_id: str,
    decision: str,
    *,
    decided_by: str = "",
    decision_comment: str = "",
    actor: str = "",
    resume_fn: ResumeFn | None = None,
) -> dict[str, Any]:
    """pending 승인 요청을 approve|reject로 결정 — CAS 단 한 번만 성공.

    high/critical risk의 '로그인된 사람' 요구는 라우터가
    _explicit_lead_user로 검증한다(서비스는 인증 주체를 모른다).
    반환 row는 결정 후 최신 상태."""
    dec = (decision or "").strip().lower()
    if dec not in ("approve", "reject"):
        raise mgmt.ManagementError(422, "validation", "decision must be approve|reject")
    row = store.get_approval_request(approval_id)
    if row is None:
        raise mgmt.ManagementError(404, "not_found", "approval request not found")
    eff = str(row.get("status") or "")
    if eff == ApprovalStatus.EXPIRED.value:
        raise mgmt.ManagementError(409, "expired", "approval request has expired")
    if eff != ApprovalStatus.PENDING.value:
        raise mgmt.ManagementError(409, "already_decided", f"approval already {eff}")
    # H2 — 부수효과 실행 가능성을 CAS 전에 검증. 실패는 ManagementError로
    # 올라가며 승인은 pending으로 남는다(결정 자체가 원자적으로만 일어난다).
    _prevalidate_decision(store, row, dec)
    comment = (decision_comment or "").strip()[:_MAX_COMMENT]
    decided = store.decide_approval_request(
        approval_id, dec, decided_by=decided_by, decision_comment=comment
    )
    if decided is None:
        # CAS 실패 — 조회 후 결정 사이 상태가 바뀜(동시 결정/만료).
        raise mgmt.ManagementError(
            409, "already_decided", "approval was decided concurrently or expired"
        )
    # spec H: approval requested/resolved 로그 — id/action/risk/decision.
    logger.info(
        "approval resolved id=%s action=%s risk=%s decision=%s by=%s",
        approval_id,
        decided.get("action_type"),
        decided.get("risk_level"),
        dec,
        decided_by or actor or "-",
    )
    store.audit(
        str(decided.get("run_id") or ""),
        actor or decided_by or "approval",
        f"approval_{dec}",
        approval_id,
        {
            "action_type": decided.get("action_type"),
            "risk_level": decided.get("risk_level"),
            "resource_type": decided.get("resource_type"),
            "resource_id": decided.get("resource_id"),
        },
    )
    # 부수효과는 결정 커밋 이후 — 실패해도 결정을 되돌리지 않는다.
    try:
        _apply_decision(
            store,
            decided,
            dec,
            decided_by=decided_by,
            comment=comment,
            actor=actor,
            resume_fn=resume_fn,
        )
    except Exception as e:
        logger.warning("approval %s side-effect failed: %s", approval_id, str(e)[:200])
        store.audit(
            str(decided.get("run_id") or ""),
            actor or decided_by or "approval",
            "approval_side_effect_error",
            approval_id,
            {"error": str(e)[:300], "action_type": decided.get("action_type")},
        )
        mgmt.emit_event(
            store,
            "error",
            run_id=str(decided.get("run_id") or "") or None,
            task_id=str(decided.get("task_id") or "") or None,
            actor=actor or decided_by or "approval",
            summary=f"approval {dec} side-effect failed: {str(e)[:120]}",
            detail={"approval_id": approval_id, "action_type": decided.get("action_type")},
        )
    return store.get_approval_request(approval_id) or decided


def expire_pending(store: Store) -> int:
    """만료 sweep — watchdog/list 경로에서 호출."""
    return store.expire_stale_approval_requests()
