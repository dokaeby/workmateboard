"""Task collaboration routes — task comments/artifacts/runs + management
aliases (paperclip spec 3차 B/F).

GET /api/v1/management/tasks/{id}/comments|artifacts|runs — 인증된 사용자
읽기(GET /management/tasks와 동일 게이트).
POST comments/artifacts — lead 이상 또는 머신 API 키(_lead_gate).
author_type/author_id는 서버가 인증 컨텍스트에서만 결정한다 —
클라이언트 지정 author는 신뢰하지 않는다.

Path aliases (spec F): /api/v1/management/goals[/{id}],
/api/v1/management/projects[/{id}] — /api/v1/goals, /api/v1/projects와
동일 동작의 thin alias.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ... import goals as goals_svc
from ...models import TaskCommentAuthorType
from ...services import task_collab as collab_svc
from ...store.projects import PROJECT_STATUSES
from ...store.util import clean_scope_id
from ..schemas import (
    GoalCreateRequest,
    GoalPatchRequest,
    ProjectCreateRequest,
    ProjectPatchRequest,
    TaskArtifactCreateRequest,
    TaskCommentCreateRequest,
)
from ..support import sanitize_project_audit_fields

logger = logging.getLogger("jobmate.api")


def register(app: FastAPI, ctx: Any) -> None:
    store = ctx.store
    err = ctx.err
    _lead_gate = ctx._lead_gate
    _lead_or_403 = ctx._lead_or_403
    _explicit_lead_user = ctx._explicit_lead_user
    _audit_api = ctx._audit_api
    _actor_label = ctx._actor_label
    _session_user = ctx._session_user

    def _resolve_task(
        task_ref: str, request: Request
    ) -> tuple[dict[str, Any] | None, JSONResponse | None]:
        """id 또는 'WMB-n' identifier → task 행. 없으면 (None, 404)."""
        task = store.get_task(task_ref)
        if task is None:
            return None, err(404, "not_found", "task not found", request)
        return task, None

    def _author(request: Request) -> tuple[str, str]:
        """인증 컨텍스트 → (author_type, author_id). 클라이언트 입력 불신.

        - 세션 사용자 → human + user id
        - 유효 API 키(머신) → system + 'api-key' (agent identity는
          API 키에서 해석할 수 없다 — 지어내지 않는다)
        - 로컬 개발 폴백(인증 비활성) → human + 'local'
        """
        from ...security import api_key, authorized

        user = _session_user(request)
        if user is not None:
            return TaskCommentAuthorType.HUMAN.value, str(user.get("id") or "user")[:200]
        if api_key() and authorized(request):
            return TaskCommentAuthorType.SYSTEM.value, "api-key"
        return TaskCommentAuthorType.HUMAN.value, "local"

    # ==================== task comments ====================

    @app.get("/api/v1/management/tasks/{task_id}/comments")
    def mgmt_task_comments(task_id: str, request: Request, limit: int = 100) -> JSONResponse:
        task, e = _resolve_task(task_id, request)
        if e is not None:
            return e
        items = store.list_task_comments(str(task["id"]), limit=limit)  # type: ignore[index]
        return JSONResponse(content={"items": items})

    @app.post("/api/v1/management/tasks/{task_id}/comments", status_code=201)
    def mgmt_task_comment_create(
        task_id: str, body: TaskCommentCreateRequest, request: Request
    ) -> JSONResponse:
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        task, e = _resolve_task(task_id, request)
        if e is not None:
            return e
        fields = body.model_dump(exclude_unset=True)
        verr = collab_svc.validate_comment_fields(fields)
        if verr is not None:
            return err(422, "validation", verr, request)
        atype, aid = _author(request)
        comment = store.create_task_comment(
            str(task["id"]),  # type: ignore[index]
            author_type=atype,
            author_id=aid,
            body=str(fields.get("body") or ""),
            content_type=str(fields.get("content_type") or "text"),
        )
        if comment is None:
            return err(422, "validation", "comment could not be created", request)
        _audit_api(
            request,
            "task_comment_create",
            f"{task['id']}:{comment['id']}",  # type: ignore[index]
            {"author_type": atype},
        )
        logger.info(
            "task comment created task=%s comment=%s author_type=%s",
            task["id"],  # type: ignore[index]
            comment["id"],
            atype,
        )
        return JSONResponse(status_code=201, content=comment)

    # ==================== task artifacts ====================

    @app.get("/api/v1/management/tasks/{task_id}/artifacts")
    def mgmt_task_artifacts(task_id: str, request: Request, limit: int = 100) -> JSONResponse:
        task, e = _resolve_task(task_id, request)
        if e is not None:
            return e
        items = store.list_task_artifacts(str(task["id"]), limit=limit)  # type: ignore[index]
        return JSONResponse(content={"items": items})

    @app.post("/api/v1/management/tasks/{task_id}/artifacts", status_code=201)
    def mgmt_task_artifact_create(
        task_id: str, body: TaskArtifactCreateRequest, request: Request
    ) -> JSONResponse:
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        task, e = _resolve_task(task_id, request)
        if e is not None:
            return e
        tid = str(task["id"])  # type: ignore[index]
        fields = body.model_dump(exclude_unset=True)
        verr = collab_svc.validate_artifact_fields(store, tid, fields)
        if verr is not None:
            return err(422, "validation", verr, request)
        artifact = store.create_task_artifact(
            tid,
            kind=str(fields.get("kind") or "other"),
            name=str(fields.get("name") or ""),
            uri=str(fields.get("uri") or ""),
            run_id=(str(fields["run_id"]).strip() or None) if fields.get("run_id") else None,
            checksum_sha256=(
                str(fields["checksum_sha256"]).strip() or None
                if fields.get("checksum_sha256")
                else None
            ),
            media_type=str(fields.get("media_type") or ""),
            size_bytes=fields.get("size_bytes"),
            metadata=fields.get("metadata"),
            created_by=_actor_label(request),
        )
        if artifact is None:
            return err(422, "validation", "artifact could not be created", request)
        _audit_api(
            request,
            "task_artifact_create",
            f"{tid}:{artifact['id']}",
            {"kind": artifact.get("kind"), "run_id": artifact.get("run_id") or ""},
        )
        logger.info(
            "task artifact created task=%s artifact=%s kind=%s",
            tid,
            artifact["id"],
            artifact.get("kind"),
        )
        return JSONResponse(status_code=201, content=artifact)

    # ==================== task run history ====================

    @app.get("/api/v1/management/tasks/{task_id}/runs")
    def mgmt_task_runs(task_id: str, request: Request, limit: int = 100) -> JSONResponse:
        """task의 run 이력 — task_runs 링크 기준 최신순 (spec F task detail)."""
        task, e = _resolve_task(task_id, request)
        if e is not None:
            return e
        return JSONResponse(
            content={"items": collab_svc.task_runs_view(store, str(task["id"]), limit=limit)}  # type: ignore[index]
        )

    # ==================== aliases: /management/goals ====================
    # spec F — /api/v1/goals와 동일 동작의 thin alias.

    @app.get("/api/v1/management/goals")
    def alias_list_goals(request: Request, status: str | None = None) -> JSONResponse:
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

    @app.post("/api/v1/management/goals", status_code=201)
    def alias_create_goal(body: GoalCreateRequest, request: Request) -> JSONResponse:
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
            owner_agent_id=(
                str(fields["owner_agent_id"]).strip() or None
                if fields.get("owner_agent_id")
                else None
            ),
            success_metrics=fields.get("success_metrics"),
            target_at=(
                str(fields["target_at"]).strip() or None if fields.get("target_at") else None
            ),
            created_by=_actor_label(request),
        )
        if goal is None:
            return err(422, "validation", "goal could not be created", request)
        out = goals_svc.goal_out(store, goal)
        safe_title = clean_scope_id(str(goal.get("title") or ""))
        _audit_api(request, "goal_create", goal["id"], {"title": safe_title[:80]})
        logger.info("goal created (alias) id=%s title=%s", goal["id"], safe_title)
        return JSONResponse(status_code=201, content=out)

    @app.patch("/api/v1/management/goals/{goal_id}")
    def alias_patch_goal(goal_id: str, body: GoalPatchRequest, request: Request) -> JSONResponse:
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
        _audit_api(request, "goal_update", goal_id, {"fields": sorted(fields)})
        return JSONResponse(content=goals_svc.goal_out(store, goal))

    @app.delete("/api/v1/management/goals/{goal_id}")
    def alias_delete_goal(goal_id: str, request: Request) -> JSONResponse:
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        if store.get_goal(goal_id) is None:
            return err(404, "not_found", "goal not found", request)
        if not store.cancel_goal(goal_id):
            return err(404, "not_found", "goal not found", request)
        _audit_api(request, "goal_cancel", goal_id, {})
        logger.info("goal cancelled (alias) id=%s", goal_id)
        return JSONResponse(content={"ok": True, "status": "cancelled"})

    # ==================== aliases: /management/projects ====================
    # spec F — /api/v1/projects와 동일 동작의 thin alias. workspace_path는
    # 명시적 lead에게만 노출(sessions.py _project_out과 동일 규칙).

    def _alias_project_out(proj: dict[str, Any], request: Request) -> dict[str, Any]:
        out = dict(proj)
        if _explicit_lead_user(request) is None:
            out.pop("workspace_path", None)
        return out

    def _alias_valid_project_fields(
        fields: dict[str, Any], request: Request
    ) -> JSONResponse | None:
        if "status" in fields and fields["status"] is not None:
            st = str(fields["status"]).strip().lower()
            if st not in PROJECT_STATUSES:
                return err(
                    422,
                    "validation",
                    f"status must be one of {list(PROJECT_STATUSES)}",
                    request,
                )
        if "owner_agent_id" in fields and fields["owner_agent_id"]:
            oid = str(fields["owner_agent_id"]).strip()
            if store.get_agent(oid) is None:
                return err(422, "validation", "owner_agent_id: agent not found", request)
        if "repository_url" in fields and fields["repository_url"]:
            url = str(fields["repository_url"]).strip()
            try:
                p = urlparse(url)
            except ValueError:
                p = None
            # sessions.py _valid_repository_url과 동일 규칙 — https +
            # netloc 필수, userinfo(내장 credential) 거부.
            if p is None or p.scheme != "https" or not p.netloc or p.username or p.password:
                return err(
                    422,
                    "validation",
                    "repository_url must be an https URL without credentials",
                    request,
                )
        if "goal_id" in fields:
            gid = str(fields["goal_id"] or "").strip()
            if gid:
                goal_row = store.get_goal(gid)
                if goal_row is None:
                    return err(422, "validation", "goal not found", request)
                if str(goal_row.get("status") or "") == "cancelled":
                    return err(422, "validation", "goal is cancelled", request)
        return None

    @app.get("/api/v1/management/projects")
    def alias_list_projects(request: Request) -> JSONResponse:
        lead = _lead_or_403(request)
        if isinstance(lead, JSONResponse):
            return lead
        return JSONResponse(
            content={"items": [_alias_project_out(p, request) for p in store.list_projects()]}
        )

    @app.post("/api/v1/management/projects", status_code=201)
    def alias_create_project(body: ProjectCreateRequest, request: Request) -> JSONResponse:
        lead = _lead_or_403(request)
        if isinstance(lead, JSONResponse):
            return lead
        fields = body.model_dump(exclude_unset=True)
        verr = _alias_valid_project_fields(fields, request)
        if verr is not None:
            return verr
        gid = str(fields.get("goal_id") or "").strip()
        proj = store.create_project(
            body.name,
            color=body.color,
            description=body.description,
            goal=body.goal,
            target_count=body.target_count,
            goal_id=gid or None,
            status=str(fields.get("status") or "active"),
            owner_agent_id=(
                str(fields["owner_agent_id"]).strip() or None
                if fields.get("owner_agent_id")
                else None
            ),
            repository_url=(
                str(fields["repository_url"]).strip() or None
                if fields.get("repository_url")
                else None
            ),
            repository_ref=(
                str(fields["repository_ref"]).strip() or None
                if fields.get("repository_ref")
                else None
            ),
            workspace_path=(
                str(fields["workspace_path"]).strip() or None
                if fields.get("workspace_path")
                else None
            ),
            created_by=str(lead.get("email") or lead.get("id") or ""),
        )
        if proj is None:
            return err(409, "conflict", "project name already exists or invalid", request)
        _audit_api(request, "project_create", proj["id"], {"name": proj["name"]})
        logger.info("project created (alias) id=%s", proj["id"])
        return JSONResponse(status_code=201, content=_alias_project_out(proj, request))

    @app.patch("/api/v1/management/projects/{project_id}")
    def alias_patch_project(
        project_id: str, body: ProjectPatchRequest, request: Request
    ) -> JSONResponse:
        lead = _lead_or_403(request)
        if isinstance(lead, JSONResponse):
            return lead
        fields = body.model_dump(exclude_unset=True)
        verr = _alias_valid_project_fields(fields, request)
        if verr is not None:
            return verr
        proj = store.update_project(project_id, **fields)
        if proj is None:
            return err(404, "not_found", "project not found or name conflict", request)
        # sessions.py patch_project와 동일 — workspace_path 원문은 audit에
        # 남기지 않는다(/api/v1/audit이 응답 필터를 우회하기 때문).
        _audit_api(
            request,
            "project_update",
            project_id,
            sanitize_project_audit_fields(fields),
        )
        return JSONResponse(content=_alias_project_out(proj, request))

    @app.delete("/api/v1/management/projects/{project_id}")
    def alias_delete_project(project_id: str, request: Request) -> JSONResponse:
        """소프트 취소 — status='cancelled' (task 기록 보존, spec 3차 A/G)."""
        lead = _lead_or_403(request)
        if isinstance(lead, JSONResponse):
            return lead
        if not store.cancel_project(project_id):
            return err(404, "not_found", "project not found", request)
        _audit_api(request, "project_cancel", project_id, {})
        logger.info("project cancelled (alias) id=%s", project_id)
        return JSONResponse(content={"ok": True, "status": "cancelled"})
