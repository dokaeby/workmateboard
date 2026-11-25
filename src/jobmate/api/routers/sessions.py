"""Agent sessions / projects / lead dashboard / gmail-calendar routes.

Extracted from api/app.py create_app() — handlers are copied verbatim;
create_app locals are injected via ctx.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ...lead import (
    approval_queue,
    detect_risks,
    perf_compare,
    project_detail,
    project_overview,
)
from ..jobs import _poll_gmail
from ..schemas import (
    AgentSessionRequest,
    ProjectCreateRequest,
    ProjectPatchRequest,
    RunPatchRequest,
)
from ..support import _session_view

logger = logging.getLogger("jobmate.api")


def register(app: FastAPI, ctx: Any) -> None:
    adapters = ctx.adapters
    err = ctx.err
    settings = ctx.settings
    store = ctx.store
    _audit_api = ctx._audit_api
    _lead_gate = ctx._lead_gate
    _lead_or_403 = ctx._lead_or_403
    _lead_user = ctx._lead_user
    _push_attention = ctx._push_attention
    _session_push_allowed = ctx._session_push_allowed

    @app.post("/api/v1/agent-sessions")
    def push_agent_session(body: AgentSessionRequest, request: Request) -> JSONResponse:
        """머신 push 전용 — API 키 인증만 허용. 세션 쿠키만으로는 거부."""
        from ...security import authorized

        if not authorized(request):
            logger.warning("agent-sessions push rejected: session-only auth")
            return err(403, "forbidden", "machine API key required", request)
        tool = body.tool.strip()
        session_key = body.session_key.strip()
        if not tool or not session_key:
            return err(422, "validation_error", "tool/session_key must not be blank", request)
        prev = store.get_agent_session(tool, session_key)
        store.upsert_agent_session(
            tool,
            session_key,
            body.status,
            project=body.project,
            title=body.title,
            doing=body.doing,
            ask=body.ask,
            event_at=body.event_at,
            ended_summary=body.ended_summary,
            artifacts=body.artifacts,
            machine=body.machine,
            tokens_in=body.tokens_in,
            tokens_out=body.tokens_out,
        )
        # 활동 녹화 — status/doing 변경 또는 60초 경과 시에만 insert(스토어 스로틀)
        try:
            store.record_session_activity(
                tool, session_key, body.status, doing=body.doing, project=body.project
            )
        except Exception as e:
            logger.warning("session activity record failed: %s", str(e)[:200])
        # decision 진입(또는 ask 설정) '전이' 시에만 푸시 — 반복 poll은 무시
        attention = body.status == "decision" or bool((body.ask or "").strip())
        prev_attention = prev is not None and (
            prev.get("status") == "decision" or bool((prev.get("ask") or "").strip())
        )
        if attention and not prev_attention:
            # 통합 인박스 — 세션당 1건 dedupe
            try:
                store.upsert_inbox_item(
                    "session_attention",
                    f"승인 필요: {tool} · {body.project or session_key[:12]}",
                    body=((body.ask or body.doing or body.title or "").strip())[:300],
                    link="/?view=office",
                    dedupe_key=f"session:{tool}:{session_key}",
                )
            except Exception as e:
                logger.warning("inbox session_attention failed: %s", str(e)[:200])
            if _session_push_allowed(tool, body.project, body.status, body.ask):
                who = body.project or session_key[:12]
                detail = (body.ask or body.doing or body.title or "").strip()[:200]
                _push_attention(
                    f"agent:{tool}:{session_key}",
                    f"승인 필요: {tool} · {who}",
                    detail,
                )
            else:
                logger.debug("agent-session attention skipped by notify_rules tool=%s", tool)
        return JSONResponse(content={"ok": True})

    @app.get("/api/v1/agent-sessions")
    def list_agent_sessions(
        limit: int = 50,
        include_ended: bool = False,
        machine: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = store.list_agent_sessions(
            limit=max(1, min(limit, 500)),
            include_ended=include_ended,
            machine=machine,
        )
        return [_session_view(r) for r in rows]

    @app.get("/api/v1/agent-sessions/stats")
    def agent_session_stats(request: Request, since: str | None = None) -> Any:
        """tool별 세션 통계 + since(YYYY-MM-DD, 기본 오늘)의 시간대별 working 초."""
        day = since or datetime.now().astimezone().date().isoformat()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            return err(422, "validation_error", "since must be YYYY-MM-DD", request)
        try:
            hourly = store.agent_session_hourly(day)
        except ValueError:
            return err(422, "validation_error", "invalid since date", request)
        return {"tools": store.agent_session_stats(since=since), "hourly": hourly}

    @app.get("/api/v1/agent-sessions/{tool}/{session_key}/history")
    def agent_session_history(tool: str, session_key: str, limit: int = 100) -> dict[str, Any]:
        """세션 상태 전이 이력 + 회고 요약 (시간순)."""
        from ...lead import session_history

        hist = session_history(store, tool, session_key)
        if hist is None:
            return {"events": [], "timeline": [], "stats": {}, "summary": ""}
        # limit은 기존 이벤트 목록에만 적용 — timeline은 전체 유지
        hist["events"] = hist["events"][: min(max(limit, 1), 500)]
        return hist

    @app.get("/api/v1/agent-sessions/{session_key}/history")
    def agent_session_history_by_key(session_key: str, limit: int = 100) -> dict[str, Any]:
        """tool 미지정 세션 히스토리 — session_key로 최신 세션을 찾아 회고."""
        from ...lead import session_history

        sess = store.find_agent_session_by_key(session_key)
        if sess is None:
            return {"events": [], "timeline": [], "stats": {}, "summary": ""}
        hist = session_history(store, str(sess.get("tool") or ""), session_key)
        if hist is None:
            return {"events": [], "timeline": [], "stats": {}, "summary": ""}
        hist["events"] = hist["events"][: min(max(limit, 1), 500)]
        return hist

    @app.patch("/api/v1/agent-sessions/{session_key}")
    def patch_agent_session(
        session_key: str, body: RunPatchRequest, request: Request
    ) -> JSONResponse:
        """세션 project 태그 설정 (lead 이상). tool 미지정 — 최신 세션 우선."""
        lead = _lead_user(request)
        if lead is None:
            return err(403, "forbidden", "lead role required", request)
        if "project" not in body.model_fields_set:
            return err(422, "validation", "project field required", request)
        sess = store.set_agent_session_project(session_key, body.project or "")
        if sess is None:
            return err(404, "not_found", "session not found", request)
        _audit_api(
            request,
            "session_project_set",
            f"{sess.get('tool')}:{session_key}",
            {"project": sess.get("project") or ""},
        )
        logger.info("agent session %s project set", session_key)
        return JSONResponse(content=_session_view(sess))

    @app.get("/api/v1/agent-sessions/{tool}/{session_key}/activity")
    def agent_session_activity(tool: str, session_key: str, limit: int = 200) -> dict[str, Any]:
        """세션 활동 스냅샷 (시간순, 최대 200) — doing/status/프로젝트."""
        return {
            "activity": store.session_activity(tool, session_key, limit=min(max(limit, 1), 500))
        }

    @app.get("/api/v1/projects")
    def list_projects(request: Request) -> JSONResponse:
        """프로젝트 마스터 목록 (lead 이상)."""
        lead = _lead_or_403(request)
        if isinstance(lead, JSONResponse):
            return lead
        return JSONResponse(content={"items": store.list_projects()})

    @app.post("/api/v1/projects", status_code=201)
    def create_project(body: ProjectCreateRequest, request: Request) -> JSONResponse:
        lead = _lead_or_403(request)
        if isinstance(lead, JSONResponse):
            return lead
        proj = store.create_project(
            body.name,
            color=body.color,
            description=body.description,
            goal=body.goal,
            target_count=body.target_count,
            created_by=str(lead.get("email") or lead.get("id") or ""),
        )
        if proj is None:
            return err(409, "conflict", "project name already exists or invalid", request)
        _audit_api(request, "project_create", proj["id"], {"name": proj["name"]})
        logger.info("project created id=%s", proj["id"])
        return JSONResponse(status_code=201, content=proj)

    @app.get("/api/v1/projects/overview")
    def projects_overview(request: Request) -> JSONResponse:
        """프로젝트별 세션/run/승인/비용 집계 + '(기타)' 버킷 (lead 이상)."""
        lead = _lead_or_403(request)
        if isinstance(lead, JSONResponse):
            return lead
        return JSONResponse(content=project_overview(store))

    @app.get("/api/v1/projects/{project_id}/detail")
    def project_detail_view(project_id: str, request: Request) -> JSONResponse:
        """프로젝트 상세 — 세션/run 요약 + 타임라인 + 단계 실패 + 30일 비용."""
        lead = _lead_or_403(request)
        if isinstance(lead, JSONResponse):
            return lead
        detail = project_detail(store, project_id)
        if detail is None:
            return err(404, "not_found", "project not found", request)
        return JSONResponse(content=detail)

    @app.patch("/api/v1/projects/{project_id}")
    def patch_project(project_id: str, body: ProjectPatchRequest, request: Request) -> JSONResponse:
        lead = _lead_or_403(request)
        if isinstance(lead, JSONResponse):
            return lead
        fields = body.model_dump(exclude_unset=True)
        proj = store.update_project(project_id, **fields)
        if proj is None:
            return err(404, "not_found", "project not found or name conflict", request)
        _audit_api(request, "project_update", project_id, fields)
        logger.info("project updated id=%s fields=%s", project_id, sorted(fields))
        return JSONResponse(content=proj)

    @app.delete("/api/v1/projects/{project_id}")
    def delete_project(project_id: str, request: Request) -> JSONResponse:
        lead = _lead_or_403(request)
        if isinstance(lead, JSONResponse):
            return lead
        if not store.delete_project(project_id):
            return err(404, "not_found", "project not found", request)
        _audit_api(request, "project_delete", project_id, {})
        logger.info("project deleted id=%s", project_id)
        return JSONResponse(content={"ok": True})

    @app.get("/api/v1/risks")
    def risks(request: Request) -> JSONResponse:
        """라이브 리스크 탐지 — 심각도+최신순 최대 50건 (lead 이상)."""
        lead = _lead_or_403(request)
        if isinstance(lead, JSONResponse):
            return lead
        return JSONResponse(content=detect_risks(store))

    @app.get("/api/v1/approvals")
    def approvals(request: Request) -> JSONResponse:
        """통합 승인 큐 — run waiting_* + 세션 decision/ask + 트리거 오류."""
        lead = _lead_or_403(request)
        if isinstance(lead, JSONResponse):
            return lead
        return JSONResponse(content=approval_queue(store))

    @app.get("/api/v1/perf/compare")
    def perf_compare_view(
        request: Request,
        workflow: str = "",
        group_by: str = "model",
    ) -> JSONResponse:
        """run 성과 비교 — model/agent/tool 그룹별 성공률·비용 + 세션 tool 통계."""
        lead = _lead_or_403(request)
        if isinstance(lead, JSONResponse):
            return lead
        if group_by not in ("model", "agent", "tool"):
            return err(422, "validation", "group_by must be model|agent|tool", request)
        return JSONResponse(content=perf_compare(store, workflow=workflow, group_by=group_by))

    @app.get("/api/v1/usage/llm")
    def llm_usage(days: int = 7) -> dict[str, Any]:
        """LLM 토큰 사용량 요약 — 모델별 합계 + 일별 시계열."""
        days = max(1, min(days, 90))
        return {"days": days, **store.llm_usage_summary(days=days)}

    @app.get("/api/v1/gmail-calendar")
    def gmail_status() -> dict[str, Any]:
        # member 허용 — calendar 뷰 상태 표시용(민감 설정값 없음)
        auto = store.get_automation()
        return {
            "enabled": auto.get("enabled", False),
            "last_poll_at": auto.get("last_poll_at"),
            "last_poll_result": auto.get("last_poll_result", ""),
        }

    @app.post("/api/v1/gmail-calendar/toggle")
    def gmail_toggle(body: dict[str, Any], request: Request) -> JSONResponse:
        """자동 폴링 on/off — 자동화 설정 변경이므로 lead 이상."""
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        auto = store.get_automation()
        auto["enabled"] = bool(body.get("enabled", not auto.get("enabled", False)))
        store.set_automation(auto)
        return JSONResponse(content={"enabled": auto["enabled"]})

    @app.post("/api/v1/gmail-calendar/poll")
    def gmail_poll(request: Request) -> JSONResponse:
        """수동 gmail 폴링 — lead 이상."""
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        gmail = adapters.get("gmail")
        if gmail is None:
            return JSONResponse(
                status_code=202,
                content={"status": "accepted", "note": "poll requires configured gmail adapter"},
            )
        ingested = _poll_gmail(store, settings, gmail)
        return JSONResponse(status_code=202, content={"status": "accepted", "ingested": ingested})
