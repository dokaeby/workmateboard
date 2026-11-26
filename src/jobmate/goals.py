"""Goal 서비스 — 검증/계층 규칙/GoalOut 조립.

store는 행 조회·삽입만 담당하고, status enum·parent cancelled/사이클 규칙은
여기서 수행한다(jobmate/agents.py와 동일 분담 — 라우터는 얇게).

spec 3차 A: status 어휘는 active|paused|achieved|cancelled (레거시
done→achieved, archived→cancelled는 store 마이그레이션이 변환).
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from .models import GoalStatus
from .store.db import Store

GOAL_STATUSES = tuple(s.value for s in GoalStatus)
TITLE_MAX = 200
DESC_MAX = 2000
SUCCESS_METRICS_MAX_BYTES = 4096  # success_metrics JSON 직렬화 크기 상한
OWNER_ID_MAX = 64
TARGET_AT_MAX = 64


def validate_parent(store: Store, goal_id: str | None, parent_id: str) -> str | None:
    """parent_id 규칙 검증 — 에러 메시지 또는 None.

    - 대상 목표가 존재해야 함
    - cancelled 목표는 부모로 둘 수 없음 — 단, PATCH가 기존 parent_id를
      그대로 재전송하는 no-op이면 허용한다(agents.validate_reports_to와
      동일 — cancelled 부모를 가진 기존 행의 다른 필드 수정이 422로
      막히는 것을 방지). 새로 cancelled를 부모로 지정하는 것은 거부.
    - 자기 자신 지정 불가
    - 순환 참조 불가 (a→b→a 등 — parent_id 체인을 따라 올라가며 판정)
    """
    target = parent_id.strip()
    if not target:
        return "parent_id must not be empty"
    if goal_id is not None and target == goal_id:
        return "goal cannot be its own parent"
    if goal_id is not None:
        cur = store.get_goal(goal_id)
        if cur is not None and str(cur.get("parent_id") or "") == target:
            return None  # no-op — 기존 부모 유지(cancelled 부모 포함 재검증 생략)
    parent = store.get_goal(target)
    if parent is None:
        return "parent goal not found"
    if str(parent.get("status") or "") == GoalStatus.CANCELLED.value:
        return "cancelled goal cannot be a parent"
    if goal_id is not None and store.parent_would_cycle(goal_id, target):
        return "parent_id would create a cycle"
    return None


def _validate_target_at(value: Any) -> str | None:
    """target_at은 ISO date(YYYY-MM-DD) 또는 ISO datetime이어야 한다."""
    s = str(value or "").strip()
    if not s:
        return None
    if len(s) > TARGET_AT_MAX:
        return f"target_at must be at most {TARGET_AT_MAX} chars"
    try:
        datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return "target_at must be an ISO date or datetime"
    return None


def _validate_success_metrics(value: Any) -> str | None:
    """success_metrics는 JSON object여야 하며 직렬화 크기 ≤4KB."""
    if value is None:
        return None
    if not isinstance(value, dict):
        return "success_metrics must be a JSON object"
    try:
        blob = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return "success_metrics must be JSON-serializable"
    if len(blob.encode("utf-8")) > SUCCESS_METRICS_MAX_BYTES:
        return f"success_metrics must be at most {SUCCESS_METRICS_MAX_BYTES} bytes"
    return None


def validate_goal_fields(
    store: Store,
    fields: dict[str, Any],
    *,
    goal_id: str | None = None,
) -> str | None:
    """create/patch 공용 필드 검증 — 에러 메시지 또는 None.

    fields는 이미 스키마 검증을 통과한 값 dict. fields에 없는 키는
    검증하지 않는다(PATCH 의미 — 호출자가 model_fields_set으로 거른다).
    parent_id 키가 있고 값이 None/빈 문자열이면 루트 해제로 검증 생략."""
    if "title" in fields:
        title = str(fields["title"] or "").strip()
        if not title:
            return "title must not be empty"
        if len(title) > TITLE_MAX:
            return f"title must be at most {TITLE_MAX} chars"
    if "status" in fields:
        st = str(fields["status"] or "").strip().lower()
        if st not in GOAL_STATUSES:
            return f"status must be one of {list(GOAL_STATUSES)}"
    if "description" in fields:
        if len(str(fields["description"] or "")) > DESC_MAX:
            return f"description must be at most {DESC_MAX} chars"
    if "parent_id" in fields:
        pv = fields["parent_id"]
        if pv is not None and str(pv).strip():
            err = validate_parent(store, goal_id, str(pv))
            if err is not None:
                return err
    if "owner_agent_id" in fields:
        ov = fields["owner_agent_id"]
        oid = str(ov or "").strip()
        if oid:
            if store.get_agent(oid) is None:
                return "owner_agent_id: agent not found"
    if "success_metrics" in fields:
        err = _validate_success_metrics(fields["success_metrics"])
        if err is not None:
            return err
    if "target_at" in fields:
        tv = fields["target_at"]
        if tv is not None and str(tv).strip():
            err = _validate_target_at(tv)
            if err is not None:
                return err
    return None


def _goal_success_metrics(goal: dict[str, Any]) -> Any:
    """저장된 JSON text → API 응답용 파싱 값. 깨진 JSON은 원문 대신 {}."""
    raw = goal.get("success_metrics")
    if raw is None or raw == "":
        return {}
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(str(raw))
    except (ValueError, TypeError):
        return {}


def goal_out(store: Store, goal: dict[str, Any]) -> dict[str, Any]:
    """store 행 → API 계약 GoalOut. children_count 포함.

    list_goals는 이미 children_count를 집계해 행에 넣어주므로(N+1 방지)
    그 값을 우선 쓰고, 개별 조회(get_goal) 경로만 count_goal_children로
    계산한다."""
    gid = str(goal.get("id") or "")
    children = goal.get("children_count")
    if children is None:
        children = store.count_goal_children(gid)
    try:
        children = int(children)
    except (TypeError, ValueError):
        children = 0
    return {
        "id": gid,
        "title": goal.get("title") or "",
        "description": goal.get("description") or "",
        "parent_id": goal.get("parent_id") or None,
        "status": goal.get("status") or "active",
        "owner_agent_id": goal.get("owner_agent_id") or None,
        "success_metrics": _goal_success_metrics(goal),
        "target_at": goal.get("target_at") or None,
        "created_at": goal.get("created_at"),
        "updated_at": goal.get("updated_at"),
        "children_count": children,
    }
