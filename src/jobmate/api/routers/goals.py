"""Goal routes — 목표 계층 CRUD (Paperclip goals 포팅).

GET은 member+ (목표는 민감 정보가 아니며 runs/projects가 참조한다).
POST/PATCH/DELETE는 lead+ — 계층 구조 변경은 조직 설계 행위.
DELETE는 하드 삭제가 아니라 status='archived' 소프트 삭제 —
runs.goal_id/projects.goal_id가 참조하는 이력을 보존한다.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ... import goals as goals_svc
from ...store.util import clean_scope_id
from ..schemas import GoalCreateRequest, GoalPatchRequest

logger = logging.getLogger("jobmate.api")


def register(app: FastAPI, ctx: Any) -> None:
    store = ctx.store
    err = ctx.err
    _lead_gate = ctx._lead_gate
    _audit_api = ctx._audit_api
    _actor_label = ctx._actor_label

    @app.get("/api/v1/goals")
    def list_goals(request: Request, status: str | None = None) -> JSONResponse:
        """목표 목록(member+) — status 필터 선택(active|done|archived).
        각 항목에 children_count(직계 자식 수) 포함."""
        status = (status or "").strip().lower() or None
        if status is not None and status not in goals_svc.GOAL_STATUSES:
            return err(
                422,
                "validation",
                f"status must be one of {list(goals_svc.GOAL_STATUSES)}",
                request,
            )
        items = [goals_svc.goal_out(store, g) for g in store.list_goals(status=status)]
        return JSONResponse(content={"items": items})

    @app.post("/api/v1/goals", status_code=201)
    def create_goal(body: GoalCreateRequest, request: Request) -> JSONResponse:
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        fields = body.model_dump(exclude_unset=True)
        verr = goals_svc.validate_goal_fields(store, fields)
        if verr is not None:
            return err(422, "validation", verr, request)
        goal = store.create_goal(
            str(fields.get("title") or ""),
            description=str(fields.get("description") or ""),
            parent_id=(
                str(fields["parent_id"]).strip() or None if fields.get("parent_id") else None
            ),
            status=str(fields.get("status") or "active"),
            created_by=_actor_label(request),
        )
        if goal is None:
            return err(422, "validation", "goal could not be created", request)
        out = goals_svc.goal_out(store, goal)
        # title은 사용자 입력 — 로그 위조 방지로 제어문자를 제거한다.
        safe_title = clean_scope_id(str(goal.get("title") or ""))
        _audit_api(request, "goal_create", goal["id"], {"title": safe_title[:80]})
        logger.info("goal created id=%s title=%s", goal["id"], safe_title)
        return JSONResponse(status_code=201, content=out)

    @app.patch("/api/v1/goals/{goal_id}")
    def patch_goal(goal_id: str, body: GoalPatchRequest, request: Request) -> JSONResponse:
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        if store.get_goal(goal_id) is None:
            return err(404, "not_found", "goal not found", request)
        fields = body.model_dump(exclude_unset=True)
        verr = goals_svc.validate_goal_fields(store, fields, goal_id=goal_id)
        if verr is not None:
            return err(422, "validation", verr, request)
        goal = store.update_goal(goal_id, **fields)
        if goal is None:
            return err(404, "not_found", "goal not found", request)
        out = goals_svc.goal_out(store, goal)
        _audit_api(request, "goal_update", goal_id, {"fields": sorted(fields)})
        logger.info("goal updated id=%s fields=%s", goal_id, sorted(fields))
        return JSONResponse(content=out)

    @app.delete("/api/v1/goals/{goal_id}")
    def delete_goal(goal_id: str, request: Request) -> JSONResponse:
        """소프트 삭제 — status='archived'. runs/projects 참조 이력은 보존.

        자식 목표는 parent_id를 유지한다 — 프론트가 트리 렌더링 시
        archived 부모 아래 자식을 어떻게 표시할지 결정한다."""
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        goal = store.get_goal(goal_id)
        if goal is None:
            return err(404, "not_found", "goal not found", request)
        if not store.archive_goal(goal_id):
            return err(404, "not_found", "goal not found", request)
        safe_title = clean_scope_id(str(goal.get("title") or ""))
        _audit_api(request, "goal_archive", goal_id, {"title": safe_title[:80]})
        logger.info("goal archived id=%s title=%s", goal_id, safe_title)
        return JSONResponse(content={"ok": True, "status": "archived"})
