"""Budget policy/incident routes — scoped monthly USD budgets."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ... import budget_scope
from ..schemas import BudgetIncidentResolveRequest, BudgetPolicyRequest

logger = logging.getLogger("jobmate.api")


def register(app: FastAPI, ctx: Any) -> None:
    store = ctx.store
    err = ctx.err
    _lead_gate = ctx._lead_gate
    _audit_api = ctx._audit_api
    _actor_label = ctx._actor_label

    @app.get("/api/v1/budgets/overview")
    def budgets_overview(request: Request) -> JSONResponse:
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        month, _ws = budget_scope.month_window()
        return JSONResponse(
            content={"policies": budget_scope.list_policies_with_spend(store), "month": month}
        )

    @app.put("/api/v1/budgets/policies/{scope_type}")
    @app.put("/api/v1/budgets/policies/{scope_type}/{scope_id}")
    def upsert_budget_policy(
        scope_type: str, request: Request, body: BudgetPolicyRequest, scope_id: str = ""
    ) -> JSONResponse:
        """scope_id 없는 경로는 global로 간주."""
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        if scope_type not in budget_scope.SCOPE_TYPES:
            return err(
                422,
                "validation",
                f"scope_type must be one of {list(budget_scope.SCOPE_TYPES)}",
                request,
            )
        if scope_type != "global" and not scope_id:
            return err(422, "validation", "scope_id required for non-global scope", request)
        # 부분 갱신 — model_fields_set으로 "미전송"과 "명시 null"을 구분해
        # 토글 전용 PUT이 amount/기존 설정을 지우지 않게 한다.
        sent = body.model_fields_set
        try:
            policy = budget_scope.upsert_policy(
                store,
                scope_type,
                scope_id,
                amount_usd=body.amount_usd if "amount_usd" in sent else budget_scope.UNSET,
                amount_microusd=(
                    body.amount_microusd if "amount_microusd" in sent else budget_scope.UNSET
                ),
                reservation_microusd=(
                    body.reservation_microusd
                    if "reservation_microusd" in sent
                    else budget_scope.UNSET
                ),
                unpriced_usage_policy=(
                    body.unpriced_usage_policy
                    if "unpriced_usage_policy" in sent
                    else budget_scope.UNSET
                ),
                window_kind=(body.window_kind if "window_kind" in sent else budget_scope.UNSET),
                warn_percent=(body.warn_percent if "warn_percent" in sent else budget_scope.UNSET),
                hard_stop_enabled=(
                    body.hard_stop_enabled if "hard_stop_enabled" in sent else budget_scope.UNSET
                ),
                is_active=body.is_active if "is_active" in sent else budget_scope.UNSET,
            )
        except ValueError as e:
            return err(422, "validation", str(e), request)
        if policy is None:
            return err(422, "validation", "invalid budget policy", request)
        scope_id = budget_scope.clean_scope_id(scope_id)
        _audit_api(
            request,
            "budget_policy_upsert",
            f"{scope_type}:{scope_id}",
            {
                "amount_usd": policy.get("amount_usd"),
                "is_active": policy.get("is_active"),
            },
        )
        logger.info(
            "budget policy upserted scope=%s:%s amount=%s active=%s",
            scope_type,
            scope_id,
            policy.get("amount_usd"),
            policy.get("is_active"),
        )
        return JSONResponse(content=policy)

    @app.delete("/api/v1/budgets/policies/{scope_type}")
    @app.delete("/api/v1/budgets/policies/{scope_type}/{scope_id}")
    def delete_budget_policy(scope_type: str, request: Request, scope_id: str = "") -> JSONResponse:
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        if scope_type not in budget_scope.SCOPE_TYPES:
            return err(
                422,
                "validation",
                f"scope_type must be one of {list(budget_scope.SCOPE_TYPES)}",
                request,
            )
        if not budget_scope.delete_policy(store, scope_type, scope_id):
            return err(404, "not_found", "budget policy not found", request)
        _audit_api(request, "budget_policy_delete", f"{scope_type}:{scope_id}", {})
        logger.info("budget policy deactivated scope=%s:%s", scope_type, scope_id)
        return JSONResponse(content={"ok": True})

    @app.get("/api/v1/budget-incidents")
    def list_budget_incidents(request: Request, status: str | None = None) -> JSONResponse:
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        return JSONResponse(
            content={"items": store.list_budget_incidents(status=status or None, limit=200)}
        )

    @app.post("/api/v1/budget-incidents/{incident_id}/resolve")
    def resolve_budget_incident(
        incident_id: str, request: Request, body: BudgetIncidentResolveRequest
    ) -> JSONResponse:
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        try:
            inc = budget_scope.resolve_incident(
                store,
                incident_id,
                body.action,
                amount_usd=body.amount_usd,
                amount_microusd=body.amount_microusd,
                note=body.note,
                actor=_actor_label(request),
            )
        except ValueError as e:
            return err(422, "validation", str(e), request)
        if inc is None:
            return err(404, "not_found", "open budget incident not found", request)
        _audit_api(
            request,
            "budget_incident_resolve",
            incident_id,
            {"action": body.action, "amount_usd": body.amount_usd},
        )
        logger.info(
            "budget incident %s resolved action=%s actor=%s",
            incident_id,
            body.action,
            _actor_label(request),
        )
        return JSONResponse(content=inc)
