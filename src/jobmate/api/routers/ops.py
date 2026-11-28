"""Ops routes — schedules, notifications, inbox, usage, ask, reports, users."""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ... import messenger
from ...models import RunStatus
from ...pricing import cost_usd
from ...reports import REPORT_KIND_LABELS, build_report
from ...workflows.resolver import resolve_workflow_spec
from ..schemas import (
    AskRequest,
    ReportGenerateRequest,
    ScheduleCreateRequest,
    ScheduleToggleRequest,
    UserRoleRequest,
)
from ..session_stats import _session_est_cost_usd

logger = logging.getLogger("jobmate.api")


def register(app: FastAPI, ctx: Any) -> None:
    store = ctx.store
    settings = ctx.settings
    llm = ctx.llm
    err = ctx.err
    _admin_user = ctx._admin_user
    _effective_role = ctx._effective_role
    _normalize_role = ctx._normalize_role
    _audit_api = ctx._audit_api
    _lead_gate = ctx._lead_gate
    _prefs = ctx._prefs
    _ask_key = ctx._ask_key
    _ask_daily = ctx._ask_daily
    _ASK_DAILY_LIMIT = ctx._ASK_DAILY_LIMIT
    _ASK_TIMEOUT_S = ctx._ASK_TIMEOUT_S
    _REPORT_KINDS = ctx._REPORT_KINDS

    @app.get("/api/v1/schedules")
    def list_schedules() -> list[dict[str, Any]]:
        return store.list_schedules()

    @app.post("/api/v1/schedules", status_code=201)
    def create_schedule(body: ScheduleCreateRequest, request: Request) -> JSONResponse:
        if _admin_user(request) is None:
            return err(403, "forbidden", "admin role required", request)
        if resolve_workflow_spec(store, body.workflow) is None:
            return err(404, "unknown_workflow", f"workflow '{body.workflow}' not found", request)
        sched = store.create_schedule(body.workflow, body.input, body.interval_minutes)
        store.audit(
            "",
            "api",
            "schedule_create",
            sched["id"],
            {"workflow": body.workflow, "interval_minutes": body.interval_minutes},
        )
        logger.info(
            "schedule created id=%s workflow=%s interval=%dm",
            sched["id"],
            body.workflow,
            body.interval_minutes,
        )
        return JSONResponse(status_code=201, content=sched)

    @app.post("/api/v1/schedules/{schedule_id}/toggle")
    def toggle_schedule(
        schedule_id: str, body: ScheduleToggleRequest, request: Request
    ) -> JSONResponse:
        if _admin_user(request) is None:
            return err(403, "forbidden", "admin role required", request)
        sched = store.set_schedule_enabled(schedule_id, body.enabled)
        if sched is None:
            return err(404, "not_found", "schedule not found", request)
        store.audit("", "api", "schedule_toggle", schedule_id, {"enabled": body.enabled})
        logger.info("schedule %s toggled enabled=%s", schedule_id, body.enabled)
        return JSONResponse(content=sched)

    @app.delete("/api/v1/schedules/{schedule_id}")
    def delete_schedule(schedule_id: str, request: Request) -> JSONResponse:
        if _admin_user(request) is None:
            return err(403, "forbidden", "admin role required", request)
        if not store.delete_schedule(schedule_id):
            return err(404, "not_found", "schedule not found", request)
        store.audit("", "api", "schedule_delete", schedule_id, {})
        logger.info("schedule %s deleted", schedule_id)
        return JSONResponse(content={"ok": True})

    @app.get("/api/v1/notifications")
    def notifications(limit: int = 50) -> list[dict[str, Any]]:
        return [
            n.model_dump(mode="json") for n in store.list_notifications(min(max(limit, 1), 200))
        ]

    # ---- 통합 인박스 (알림 센터) ----
    # notifications 테이블은 별도 유지 — GET /inbox 호출 시 dedupe_key=notif:{id}로
    # inbox_items에 지연 미러링한다(멱등). 나머지 kind는 이벤트 발생 시 직접 insert.
    @app.get("/api/v1/inbox")
    def inbox(limit: int = 50, unread_only: bool = False) -> list[dict[str, Any]]:
        store.sync_notifications_to_inbox()
        return store.list_inbox(limit=min(max(limit, 1), 200), unread_only=unread_only)

    @app.post("/api/v1/inbox/{item_id}/read")
    def inbox_read(item_id: str, request: Request) -> JSONResponse:
        if not store.mark_inbox_read(item_id):
            return err(404, "not_found", "inbox item not found", request)
        return JSONResponse(content={"ok": True})

    @app.post("/api/v1/inbox/read-all")
    def inbox_read_all() -> dict[str, Any]:
        return {"ok": True, "updated": store.mark_all_inbox_read()}

    @app.get("/api/v1/inbox/unread-count")
    def inbox_unread_count() -> dict[str, int]:
        return {"count": store.inbox_unread_count()}

    # ---- 비용 대시보드 ----
    @app.get("/api/v1/usage/summary")
    def usage_summary(request: Request, days: int = 30) -> JSONResponse:
        """llm_usage 기반 비용/토큰 요약 + 에이전트 세션 추정 비용 top + 월 예산.
        집계값만 반환 — member 인박스/모바일 요약 바가 사용하므로 읽기는 전체 인증 사용자."""
        days = max(1, min(days, 365))
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        rows = store.llm_usage_rows(cutoff)

        by_model: dict[str, dict[str, Any]] = {}
        by_day: dict[str, dict[str, Any]] = {}
        total_cost = 0.0
        total_in_i = total_out_i = 0
        for r in rows:
            model = str(r.get("model") or "")
            tin = int(r.get("tokens_in") or 0)
            tout = int(r.get("tokens_out") or 0)
            cost = cost_usd(model, tin, tout)
            total_cost += cost
            total_in_i += tin
            total_out_i += tout
            m = by_model.setdefault(model, {"model": model, "cost_usd": 0.0, "tokens": 0})
            m["cost_usd"] += cost
            m["tokens"] += tin + tout
            day = (str(r.get("created_at") or ""))[:10]
            if day:
                d = by_day.setdefault(day, {"date": day, "cost_usd": 0.0, "tokens": 0})
                d["cost_usd"] += cost
                d["tokens"] += tin + tout

        # 에이전트 세션 추정 비용 — 상위 10개 (토큰 없으면 스킵)
        top_sessions: list[dict[str, Any]] = []
        for s in store.list_agent_sessions(limit=200, include_ended=True):
            est = _session_est_cost_usd(s)
            if est is None:
                continue
            top_sessions.append(
                {
                    "tool": s.get("tool") or "",
                    "session_key": s.get("session_key") or "",
                    "cost_usd": est,
                }
            )
        top_sessions.sort(key=lambda x: x["cost_usd"], reverse=True)

        prefs = _prefs()
        budget_raw = prefs.get("monthly_budget_usd")
        try:
            budget = float(budget_raw) if budget_raw is not None else None
        except (TypeError, ValueError):
            budget = None
        # 월초(UTC) 이후 llm_usage 비용 = month-to-date
        month_start = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        mtd_cost = 0.0
        for r in store.llm_usage_rows(month_start.isoformat()):
            mtd_cost += cost_usd(
                str(r.get("model") or ""),
                int(r.get("tokens_in") or 0),
                int(r.get("tokens_out") or 0),
            )
        budget_exceeded = budget is not None and mtd_cost > budget

        return JSONResponse(
            content={
                "total_cost_usd": round(total_cost, 6),
                "total_tokens_in": total_in_i,
                "total_tokens_out": total_out_i,
                "by_model": [
                    {
                        "model": m["model"],
                        "cost_usd": round(m["cost_usd"], 6),
                        "tokens": m["tokens"],
                    }
                    for m in sorted(by_model.values(), key=lambda x: -x["cost_usd"])
                ],
                "by_day": [
                    {"date": d["date"], "cost_usd": round(d["cost_usd"], 6), "tokens": d["tokens"]}
                    for d in sorted(by_day.values(), key=lambda x: x["date"])
                ],
                "top_sessions": top_sessions[:10],
                "budget_usd": budget,
                "budget_exceeded": budget_exceeded,
            }
        )

    # ---- 자연어 쿼리 (ask) ----

    @app.post("/api/v1/ask")
    def ask(body: AskRequest, request: Request) -> JSONResponse:
        """자연어 질문 → LLM 답변(한국어). LLM 실패/타임아웃 시 503 + error 필드.
        사용자/IP당 하루 20회 인메모리 레이트리밋."""
        from concurrent.futures import TimeoutError as FutureTimeout

        question = body.question.strip()
        if not question:
            return err(422, "validation_error", "question must not be blank", request)
        key = _ask_key(request)
        today = datetime.now(UTC).date().isoformat()
        day, count = _ask_daily.get(key, ("", 0))
        if day != today:
            day, count = today, 0
        if count >= _ASK_DAILY_LIMIT:
            return err(429, "rate_limited", "daily ask limit reached", request)
        _ask_daily[key] = (day, count + 1)
        if len(_ask_daily) > 1000:
            _ask_daily.clear()

        # 컴팩트 컨텍스트 — 비밀값/프롬프트 원문 없이 집계만
        try:
            stats = store.run_stats(None, 7)
            sessions = store.agent_session_stats(
                since=(datetime.now(UTC) - timedelta(days=7)).isoformat()
            )
            usage = store.llm_usage_summary(days=7)
            context = {
                "runs_7d": {
                    k: stats.get(k)
                    for k in ("runs", "succeeded", "failed", "running", "waiting_approval")
                },
                "tokens_7d": {
                    "in": stats.get("total_tokens_in"),
                    "out": stats.get("total_tokens_out"),
                },
                "stage_failures": stats.get("stage_failures"),
                "sessions": sessions,
                "llm_models": usage.get("models"),
            }
        except Exception as e:
            logger.warning("ask context build failed: %s", str(e)[:200])
            context = {}
        ctx_text = json.dumps(context, ensure_ascii=False, default=str)[:6000]
        system = (
            "너는 jobMate 대시보드의 운영 어시스턴트다. 제공된 집계 컨텍스트만 근거로 "
            "한국어로 간결하게 답한다(3~6문장). 모르는 내용은 추측하지 말고 "
            "데이터가 없다고 답한다. 비밀번호·토큰 등 비밀값은 절대 언급하지 않는다."
        )
        model = settings.model_for("_ask", "default")
        pool = ThreadPoolExecutor(max_workers=1)
        try:
            fut = pool.submit(
                llm.complete,
                model=model,
                system=system,
                user=f"컨텍스트:\n{ctx_text}\n\n질문: {question}",
            )
            out, _usage = fut.result(timeout=_ASK_TIMEOUT_S)
        except FutureTimeout:
            logger.warning("ask llm timeout")
            return JSONResponse(status_code=503, content={"answer": "", "error": "llm_unavailable"})
        except Exception as e:
            logger.warning("ask llm failed: %s", str(e)[:200])
            return JSONResponse(status_code=503, content={"answer": "", "error": "llm_unavailable"})
        finally:
            pool.shutdown(wait=False)
        answer = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
        return JSONResponse(content={"answer": answer})

    # ---- 리포트 ----

    @app.get("/api/v1/reports")
    def get_report(kind: str = "daily") -> JSONResponse:
        """온디맨드 리포트 — 저장 없이 즉시 생성."""
        if kind not in _REPORT_KINDS:
            return JSONResponse(
                status_code=422,
                content={
                    "error": {
                        "code": "validation",
                        "message": "kind must be daily|weekly|exec_daily|exec_weekly",
                    }
                },
            )
        try:
            content = build_report(store, kind, settings)
        except Exception as e:
            logger.warning("report build failed kind=%s: %s", kind, str(e)[:200])
            return JSONResponse(
                status_code=500,
                content={"error": {"code": "report_failed", "message": str(e)[:200]}},
            )
        return JSONResponse(content=content)

    @app.get("/api/v1/reports/latest")
    def latest_report(request: Request, kind: str = "daily") -> JSONResponse:
        """저장된 최신 리포트. 없으면 온디맨드 생성 결과를 반환(404 없음)."""
        if kind not in _REPORT_KINDS:
            return err(422, "validation", "kind must be daily|weekly", request)
        rep = store.latest_report(kind)
        if rep is None:
            try:
                rep = {
                    "id": "",
                    "kind": kind,
                    "content": build_report(store, kind, settings),
                    "created_at": datetime.now(UTC).isoformat(),
                }
            except Exception as e:
                logger.warning("report build failed kind=%s: %s", kind, str(e)[:200])
                return err(500, "report_failed", str(e)[:200], request)
        return JSONResponse(content=rep)

    @app.post("/api/v1/reports/generate", status_code=201)
    def generate_report(body: ReportGenerateRequest, request: Request) -> JSONResponse:
        """리포트 생성·저장 + 인박스 알림 + 메신저 알림 (관리자 전용)."""
        if _admin_user(request) is None:
            return err(403, "forbidden", "admin role required", request)
        try:
            content = build_report(store, body.kind, settings)
        except Exception as e:
            logger.warning("report build failed kind=%s: %s", body.kind, str(e)[:200])
            return err(500, "report_failed", str(e)[:200], request)
        rid = store.save_report(body.kind, content)
        store.upsert_inbox_item(
            "report",
            f"{REPORT_KIND_LABELS.get(body.kind, body.kind)} 리포트 생성됨",
            body=f"runs={content['runs']['total']} failed={content['runs']['failed']}",
            link="/?view=reports",
            dedupe_key=f"report:{rid}",
        )
        store.audit("", "api", "report_generate", rid, {"kind": body.kind})
        logger.info("report generated id=%s kind=%s", rid, body.kind)
        try:
            kind_label = REPORT_KIND_LABELS.get(body.kind, body.kind)
            messenger.notify(
                f"jobMate {kind_label} 리포트: run {content['runs']['total']}건 "
                f"(실패 {content['runs']['failed']}), 승인 대기 {content['approvals']['pending']}"
            )
        except Exception:
            logger.debug("messenger notify after report failed", exc_info=True)
        return JSONResponse(
            status_code=201,
            content={"id": rid, "kind": body.kind, "content": content},
        )

    # ---- PWA 뱃지 ----
    @app.get("/api/v1/badge/count")
    def badge_count() -> dict[str, int]:
        """앱 뱃지 = 미읽음 인박스 + 승인 대기 run."""
        waiting = sum(
            1 for r in store.list_runs(limit=100) if r.status == RunStatus.WAITING_APPROVAL
        )
        return {"count": store.inbox_unread_count() + waiting}

    # ---- 멀티유저 역할 ----
    @app.get("/api/v1/users")
    def list_users(request: Request) -> JSONResponse:
        """사용자 목록 (관리자 전용) — 비밀번호 해시 제외."""
        admin = _admin_user(request)
        if admin is None:
            return err(403, "forbidden", "admin role required", request)
        users = []
        for u in store.list_users():
            users.append(
                {
                    "id": u["id"],
                    "email": u["email"],
                    "display_name": u.get("display_name", ""),
                    "role": _effective_role(u),
                    "created_at": u.get("created_at"),
                }
            )
        return JSONResponse(content={"users": users})

    @app.patch("/api/v1/users/{user_id}/role")
    def patch_user_role(user_id: str, body: UserRoleRequest, request: Request) -> JSONResponse:
        """사용자 role 변경 (관리자 전용) — 마지막 admin 강등 차단 + 감사 로그."""
        admin = _admin_user(request)
        if admin is None:
            return err(403, "forbidden", "admin role required", request)
        target = store.get_user_by_id(user_id)
        if target is None:
            return err(404, "not_found", "user not found", request)
        # 유효하지 않은 role 문자열은 member로 강등하지 않고 422로 거부
        raw_role = str(body.role or "").strip().lower()
        if raw_role == "user":
            raw_role = "member"
        if raw_role not in ("member", "lead", "admin"):
            return err(422, "validation", "role must be member|lead|admin", request)
        new_role = raw_role
        old_role = _normalize_role(target.get("role"))
        if new_role == old_role:
            return JSONResponse(content={"id": user_id, "role": new_role, "changed": False})
        # 마지막 admin 강등 방지 — admin 수 확인과 role 변경을 store 락 안에서
        # 원자적으로 묶어 동시 요청이 둘 다 통과하는 TOCTOU를 막는다.
        with store.exclusive():
            if old_role == "admin" and new_role != "admin":
                admin_count = sum(
                    1 for u in store.list_users() if _normalize_role(u.get("role")) == "admin"
                )
                if admin_count <= 1:
                    return err(
                        409,
                        "last_admin",
                        "cannot demote the last admin",
                        request,
                    )
            store.set_user_role(user_id, new_role)
        detail = {
            "email": target.get("email", ""),
            "old_role": old_role,
            "new_role": new_role,
        }
        store.audit(
            str(admin.get("email") or admin.get("id") or "api"), "api", "user_role", user_id, detail
        )
        _audit_api(request, "user_role", user_id, detail)
        logger.info("user role changed id=%s %s->%s", user_id, old_role, new_role)
        return JSONResponse(content={"id": user_id, "role": new_role, "changed": True})

    @app.get("/api/v1/event-links")
    def event_links(request: Request, limit: int = 50) -> JSONResponse:
        """event_links 목록 — 멤버 뷰에서 쓰지 않으므로 lead 이상."""
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        return JSONResponse(
            content=[x.model_dump(mode="json") for x in store.list_links(min(max(limit, 1), 200))]
        )
