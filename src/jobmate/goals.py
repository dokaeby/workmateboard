"""Goal 서비스 — 검증/계층 규칙/GoalOut 조립.

store는 행 조회·삽입만 담당하고, status enum·parent archived/사이클 규칙은
여기서 수행한다(jobmate/agents.py와 동일 분담 — 라우터는 얇게).
"""

from __future__ import annotations

from typing import Any

from .store.db import Store

GOAL_STATUSES = ("active", "done", "archived")
TITLE_MAX = 200
DESC_MAX = 2000


def validate_parent(store: Store, goal_id: str | None, parent_id: str) -> str | None:
    """parent_id 규칙 검증 — 에러 메시지 또는 None.

    - 대상 목표가 존재해야 함
    - archived 목표는 부모로 둘 수 없음 — 단, PATCH가 기존 parent_id를
      그대로 재전송하는 no-op이면 허용한다(agents.validate_reports_to와
      동일 — archived 부모를 가진 기존 행의 다른 필드 수정이 422로
      막히는 것을 방지). 새로 archived를 부모로 지정하는 것은 거부.
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
            return None  # no-op — 기존 부모 유지(archived 부모 포함 재검증 생략)
    parent = store.get_goal(target)
    if parent is None:
        return "parent goal not found"
    if str(parent.get("status") or "") == "archived":
        return "archived goal cannot be a parent"
    if goal_id is not None and store.parent_would_cycle(goal_id, target):
        return "parent_id would create a cycle"
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
    return None


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
        "created_at": goal.get("created_at"),
        "updated_at": goal.get("updated_at"),
        "children_count": children,
    }
