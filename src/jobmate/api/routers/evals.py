"""Eval routes — 평가 픽스처 CRUD + eval run 생성/채점 조회 (Paperclip pillar-3).

모든 엔드포인트 lead+ — eval input 픽스처가 민감 정보를 담을 수 있고
UI(/evals 뷰, nav minRole=lead)도 lead 전용이다. DELETE는 하드 삭제가
아니라 status='archived' 소프트 삭제 — eval_runs 채점 이력을 보존한다.

채점은 lazy: run이 terminal이 되면 GET /evals/{id}/runs 조회 시점에
결정적 체크를 실행한다(evals.maybe_score_eval_run). run 생성 시점에
이미 terminal이면(거의 없음) 동일 경로로 즉시 채점한다.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse

from ... import budget_scope
from ... import evals as evals_svc
from ...models import RunStatus
from ...store.util import clean_scope_id
from ...workflows.resolver import resolve_workflow_spec
from ..schemas import EvalCreateRequest, EvalPatchRequest

logger = logging.getLogger("jobmate.api")


def register(app: FastAPI, ctx: Any) -> None:
    store = ctx.store
    err = ctx.err
    _lead_gate = ctx._lead_gate
    _audit_api = ctx._audit_api
    _actor_label = ctx._actor_label
    _run_rate_limited = ctx._run_rate_limited
    _monthly_budget_usd = ctx._monthly_budget_usd
    _mtd_llm_cost_usd = ctx._mtd_llm_cost_usd
    _schedule_run_jobs = ctx._schedule_run_jobs

    def _latest_map() -> dict[str, dict[str, Any]]:
        return store.latest_eval_runs()

    @app.get("/api/v1/evals")
    def list_evals(request: Request, status: str | None = None) -> JSONResponse:
        """eval 목록(lead+) — status 필터 선택(active|archived).
        각 항목에 last_score/last_run_at(최신 eval_run 집계) 포함."""
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        status = (status or "").strip().lower() or None
        if status is not None and status not in evals_svc.EVAL_STATUSES:
            return err(
                422,
                "validation",
                f"status must be one of {list(evals_svc.EVAL_STATUSES)}",
                request,
            )
        latest = _latest_map()
        items = [
            evals_svc.eval_out(ev, latest.get(str(ev.get("id") or "")))
            for ev in store.list_evals(status=status)
        ]
        return JSONResponse(content={"items": items})

    @app.post("/api/v1/evals", status_code=201)
    def create_eval(body: EvalCreateRequest, request: Request) -> JSONResponse:
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        fields = body.model_dump(exclude_unset=True)
        verr, vstatus = evals_svc.validate_eval_fields(store, fields)
        if verr is not None:
            code = "conflict" if vstatus == 409 else "validation"
            return err(vstatus, code, verr, request)
        ev = store.create_eval(
            str(fields.get("name") or ""),
            str(fields.get("workflow_key") or ""),
            dict(fields.get("input") or {}),
            list(fields.get("checks") or []),
            status=str(fields.get("status") or "active"),
            created_by=_actor_label(request),
        )
        if ev is None:
            return err(409, "conflict", evals_svc.ERR_NAME_CONFLICT, request)
        safe_name = clean_scope_id(str(ev.get("name") or ""))
        _audit_api(request, "eval_create", ev["id"], {"name": safe_name[:80]})
        logger.info("eval created id=%s name=%s", ev["id"], safe_name)
        return JSONResponse(
            status_code=201, content=evals_svc.eval_out(ev, _latest_map().get(ev["id"]))
        )

    @app.get("/api/v1/evals/{eval_id}")
    def get_eval(eval_id: str, request: Request) -> JSONResponse:
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        ev = store.get_eval(eval_id)
        if ev is None:
            return err(404, "not_found", "eval not found", request)
        return JSONResponse(content=evals_svc.eval_out(ev, _latest_map().get(eval_id)))

    @app.patch("/api/v1/evals/{eval_id}")
    def patch_eval(eval_id: str, body: EvalPatchRequest, request: Request) -> JSONResponse:
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        if store.get_eval(eval_id) is None:
            return err(404, "not_found", "eval not found", request)
        fields = body.model_dump(exclude_unset=True)
        if not fields:
            return err(422, "validation", "no fields to update", request)
        verr, vstatus = evals_svc.validate_eval_fields(store, fields, eval_id=eval_id)
        if verr is not None:
            code = "conflict" if vstatus == 409 else "validation"
            return err(vstatus, code, verr, request)
        ev = store.update_eval(eval_id, **fields)
        if ev is None:
            # store는 '없음'과 'name 중복(integrity/사전검사)'을 같은 None으로
            # 반환한다 — 재조회로 구분해 name 충돌은 409로 매핑한다
            # (agents PATCH와 동일 패턴).
            if store.get_eval(eval_id) is None:
                return err(404, "not_found", "eval not found", request)
            return err(409, "conflict", evals_svc.ERR_NAME_CONFLICT, request)
        _audit_api(request, "eval_update", eval_id, {"fields": sorted(fields)})
        logger.info("eval updated id=%s fields=%s", eval_id, sorted(fields))
        return JSONResponse(content=evals_svc.eval_out(ev, _latest_map().get(eval_id)))

    @app.delete("/api/v1/evals/{eval_id}")
    def delete_eval(eval_id: str, request: Request) -> JSONResponse:
        """소프트 삭제 — status='archived'. eval_runs 채점 이력은 보존."""
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        ev = store.get_eval(eval_id)
        if ev is None:
            return err(404, "not_found", "eval not found", request)
        if not store.archive_eval(eval_id):
            return err(404, "not_found", "eval not found", request)
        safe_name = clean_scope_id(str(ev.get("name") or ""))
        _audit_api(request, "eval_archive", eval_id, {"name": safe_name[:80]})
        logger.info("eval archived id=%s name=%s", eval_id, safe_name)
        return JSONResponse(content={"ok": True, "status": "archived"})

    @app.post("/api/v1/evals/{eval_id}/run", status_code=201)
    def run_eval(eval_id: str, request: Request, background_tasks: BackgroundTasks) -> JSONResponse:
        """eval 픽스처로 실제 run 생성 — 코얼레싱 우회, 예산 심사는 POST /runs와 동일.

        eval_runs 행을 run과 함께 만들고 run.metadata에 eval_id/eval_run_id를
        기록해 run→eval 역추적이 가능하게 한다. run은 queued로 반환되고
        백그라운드에서 정상 파이프라인대로 실행된다."""
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        if _run_rate_limited(_actor_label(request)):
            return err(429, "rate_limited", "run 생성 요청이 너무 많습니다", request)
        ev = store.get_eval(eval_id)
        if ev is None:
            return err(404, "not_found", "eval not found", request)
        if str(ev.get("status") or "") == "archived":
            return err(409, "conflict", "eval is archived", request)
        input_data = ev.get("input") or {}
        if evals_svc.validate_eval_input(input_data) is not None:
            # 생성 시점 검증을 통과했어도 이후 직접 DB 수정 등으로 깨질 수 있다.
            return err(422, "validation", "eval input is invalid", request)
        workflow = str(ev.get("workflow_key") or "")
        wf_spec = resolve_workflow_spec(store, workflow)
        if wf_spec is None:
            return err(404, "unknown_workflow", f"workflow '{workflow}' not found", request)
        # 월간 LLM 예산 + 스코프드 정책 — POST /runs와 동일한 두 단계 심사.
        # eval run도 실제 비용을 쓰므로 동일하게 차단한다.
        budget = _monthly_budget_usd()
        if budget is not None:
            mtd = _mtd_llm_cost_usd()
            if mtd > budget:
                _audit_api(
                    request,
                    "run_budget_exceeded",
                    workflow,
                    {"budget_usd": budget, "mtd_cost_usd": round(mtd, 6), "eval_id": eval_id},
                )
                logger.warning(
                    "eval run rejected: monthly budget exceeded eval=%s mtd=%.4f budget=%.4f",
                    eval_id,
                    mtd,
                    budget,
                )
                return err(429, "budget_exceeded", "monthly LLM budget exceeded", request)
        project = ""
        pv = input_data.get("project")
        if isinstance(pv, str):
            project = budget_scope.clean_scope_id(pv)
        blocks = budget_scope.evaluate_admission(store, workflow, project)
        if blocks:
            _audit_api(
                request,
                "run_budget_exceeded",
                workflow,
                {"blocks": blocks[:5], "eval_id": eval_id},
            )
            logger.warning(
                "eval run rejected: scoped budget exceeded eval=%s workflow=%s blocks=%s",
                eval_id,
                workflow,
                blocks[:3],
            )
            return err(
                429,
                "budget_exceeded",
                f"budget exceeded: {blocks[0]['scope']} "
                f"(spend ${blocks[0]['month_spend_usd']:.4f} / "
                f"limit ${blocks[0]['amount_usd']:.4f})",
                request,
            )
        # 코얼레싱 우회 — eval run은 매번 별개 실행이어야 하므로 admit_run 대신
        # create_run을 직접 호출한다(wakeup_requests 행 없음 — 지표상 eval run은
        # 일반 run 목록에 workflow/input으로 그대로 보인다).
        run = store.create_run(
            workflow,
            wf_spec.version,
            dict(input_data),
            trace_id=uuid.uuid4().hex,
            metadata={"eval_id": eval_id},
            created_by=_actor_label(request),
        )
        try:
            er = store.create_eval_run(eval_id, run.id)
        except Exception:
            # eval_run 링크 실패 시 고아 queued run을 남기지 않는다.
            store.update_run(run.id, status=RunStatus.CANCELLED, error="eval_run_link_failed")
            raise
        # run.metadata에 eval_run_id를 보강 — run detail에서 eval_run 역추적.
        meta = dict(run.metadata or {})
        meta["eval_run_id"] = str(er.get("id") or "")
        store.update_run(run.id, metadata=meta)
        _audit_api(
            request,
            "eval_run_create",
            eval_id,
            {"eval_run_id": er.get("id"), "run_id": run.id},
        )
        logger.info(
            "eval run created eval=%s eval_run=%s run=%s workflow=%s",
            eval_id,
            er.get("id"),
            run.id,
            workflow,
        )
        # run이 이미 terminal이면(동기 실행 환경/즉시 실패) 즉시 채점 —
        # 비종결이면 pending으로 두고 GET /evals/{id}/runs가 lazy 채점한다.
        try:
            er = evals_svc.maybe_score_eval_run(store, er)
        except Exception:
            logger.warning("eval_run immediate scoring failed id=%s", er.get("id"), exc_info=True)
        job_payload = dict(input_data)
        _schedule_run_jobs([(run.id, workflow, job_payload, None)], background_tasks)
        return JSONResponse(
            status_code=201,
            content={
                "eval_run_id": str(er.get("id") or ""),
                "run_id": run.id,
                "status": str(er.get("status") or "pending"),
            },
        )

    @app.get("/api/v1/evals/{eval_id}/runs")
    def list_eval_runs(eval_id: str, request: Request, limit: int = 50) -> JSONResponse:
        """eval의 eval_run 목록(lead+) — 최신순.

        조회 시점에 pending + run terminal인 행을 lazy 채점한다(멱등)."""
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        if store.get_eval(eval_id) is None:
            return err(404, "not_found", "eval not found", request)
        items = []
        for er in store.list_eval_runs(eval_id, limit=max(1, min(limit, 200))):
            try:
                er = evals_svc.maybe_score_eval_run(store, er)
            except Exception:
                logger.warning("eval_run lazy scoring failed id=%s", er.get("id"), exc_info=True)
            items.append(evals_svc.eval_run_out(er))
        return JSONResponse(content={"items": items})
