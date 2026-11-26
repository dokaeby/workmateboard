"""Routine routes — 반복 업무 루틴 CRUD (paperclip spec 3차 C/F).

CRUD /api/v1/management/routines — 스케줄 실행 자체는 기존
automation loop(_process_due_routines)과 /cron/tick이 담당한다.
GET은 member+ (tasks 목록과 동일한 읽기 정책), POST/PATCH/DELETE/
enable/disable은 lead+ (_lead_gate). routine_runs 이력은 삭제하지 않고
routine 삭제 후에도 보존한다.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ...services import routines as routines_svc
from ...store.util import clean_scope_id
from ..schemas import RoutineCreateRequest, RoutinePatchRequest

logger = logging.getLogger("jobmate.api")


def register(app: FastAPI, ctx: Any) -> None:
    store = ctx.store
    err = ctx.err
    _lead_gate = ctx._lead_gate
    _audit_api = ctx._audit_api
    _actor_label = ctx._actor_label

    @app.get("/api/v1/management/routines")
    def list_routines(
        request: Request,
        enabled: bool | None = None,
        assignee_agent_id: str | None = None,
        limit: int = 100,
    ) -> JSONResponse:
        """routine 목록(member+) — enabled/assignee 필터 선택."""
        items = store.list_routines(
            enabled=enabled,
            assignee_agent_id=assignee_agent_id,
            limit=limit,
        )
        return JSONResponse(content={"items": items})

    @app.post("/api/v1/management/routines", status_code=201)
    def create_routine(body: RoutineCreateRequest, request: Request) -> JSONResponse:
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        try:
            routine = routines_svc.create_routine(
                store,
                name=body.name,
                description=body.description,
                project_id=body.project_id,
                task_template=body.task_template,
                assignee_agent_id=body.assignee_agent_id,
                schedule_kind=body.schedule_kind,
                schedule=body.schedule,
                timezone=body.timezone,
                max_concurrent=body.max_concurrent,
                enabled=body.enabled,
                created_by=_actor_label(request),
            )
        except routines_svc.RoutineError as e:
            return err(e.status_code, e.code, e.message, request)
        _audit_api(
            request,
            "routine_create",
            str(routine.get("id") or ""),
            {
                "name": clean_scope_id(str(routine.get("name") or ""))[:80],
                "schedule_kind": routine.get("schedule_kind"),
            },
        )
        logger.info(
            "routine created id=%s kind=%s tz=%s",
            routine.get("id"),
            routine.get("schedule_kind"),
            routine.get("timezone"),
        )
        return JSONResponse(status_code=201, content=routine)

    @app.get("/api/v1/management/routines/{routine_id}")
    def get_routine(routine_id: str, request: Request) -> JSONResponse:
        routine = store.get_routine(routine_id)
        if routine is None:
            return err(404, "not_found", "routine not found", request)
        return JSONResponse(content=routine)

    @app.patch("/api/v1/management/routines/{routine_id}")
    def patch_routine(routine_id: str, body: RoutinePatchRequest, request: Request) -> JSONResponse:
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        fields = body.model_dump(exclude_unset=True)
        try:
            routine = routines_svc.update_routine(
                store, routine_id, fields, actor=_actor_label(request)
            )
        except routines_svc.RoutineError as e:
            return err(e.status_code, e.code, e.message, request)
        _audit_api(
            request,
            "routine_update",
            routine_id,
            {"fields": sorted(fields.keys())[:20]},
        )
        return JSONResponse(content=routine)

    @app.delete("/api/v1/management/routines/{routine_id}")
    def delete_routine(routine_id: str, request: Request) -> JSONResponse:
        """routine 삭제 — routine_runs 이력은 보존한다(감사 추적)."""
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        if store.get_routine(routine_id) is None:
            return err(404, "not_found", "routine not found", request)
        store.delete_routine(routine_id)
        _audit_api(request, "routine_delete", routine_id, {})
        logger.info("routine deleted id=%s", routine_id)
        return JSONResponse(content={"ok": True})

    @app.post("/api/v1/management/routines/{routine_id}/enable")
    def enable_routine(routine_id: str, request: Request) -> JSONResponse:
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        try:
            routine = routines_svc.set_routine_enabled(
                store, routine_id, True, actor=_actor_label(request)
            )
        except routines_svc.RoutineError as e:
            return err(e.status_code, e.code, e.message, request)
        _audit_api(request, "routine_enable", routine_id, {})
        return JSONResponse(content=routine)

    @app.post("/api/v1/management/routines/{routine_id}/disable")
    def disable_routine(routine_id: str, request: Request) -> JSONResponse:
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        try:
            routine = routines_svc.set_routine_enabled(
                store, routine_id, False, actor=_actor_label(request)
            )
        except routines_svc.RoutineError as e:
            return err(e.status_code, e.code, e.message, request)
        _audit_api(request, "routine_disable", routine_id, {})
        return JSONResponse(content=routine)

    @app.get("/api/v1/management/routines/{routine_id}/runs")
    def list_routine_runs(routine_id: str, request: Request, limit: int = 50) -> JSONResponse:
        """routine_runs 이력(member+) — 최신순."""
        if store.get_routine(routine_id) is None:
            return err(404, "not_found", "routine not found", request)
        items = store.list_routine_runs(routine_id, limit=limit)
        return JSONResponse(content={"items": items})
