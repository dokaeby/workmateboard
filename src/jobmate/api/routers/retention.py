"""Retention admin routes — spec 005 US1 (FR-002/FR-003/FR-007).

GET  /api/v1/admin/retention           — 유형별 유효 TTL/삭제 방식/출처 조회
PUT  /api/v1/admin/retention           — DB override 설정
     body: {"data_type": "...", "ttl_days": int|null, "mode": "..."}
     또는 {"policies": {"<type>": {"ttl_days":..., "mode":...}, ...}}
     ttl_days=null 은 override 삭제(기본값 복귀).
POST /api/v1/admin/retention/purge     — 수동 purge 실행
     ?dry_run=true 면 삭제 없이 유형별 예상 건수 반환(purge_runs에 dry_run=1).

플랫폼 admin 전용(_platform_admin_gate) — org 역할과 무관한 플랫폼 전역 설정.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ...store.retention import (
    LEGAL_MIN_TTL_DAYS,
    RETENTION_MODES,
    RetentionPolicyError,
)

logger = logging.getLogger("jobmate.api")


def _err_for_policy(exc: RetentionPolicyError, request: Request, ctx: Any) -> JSONResponse:
    status = 422
    return ctx.err(status, exc.code, exc.message, request)


def register(app: FastAPI, ctx: Any) -> None:
    store = ctx.store
    err = ctx.err
    _platform_admin_gate = ctx._platform_admin_gate
    _platform_admin_user = ctx._platform_admin_user

    @app.get("/api/v1/admin/retention")
    def get_retention(request: Request) -> JSONResponse:
        gate = _platform_admin_gate(request)
        if gate is not None:
            return gate
        try:
            items = store.list_retention_policies()
        except Exception as e:
            logger.warning("retention policy list failed: %s", type(e).__name__)
            return err(500, "retention_list_failed", "failed to list retention policies", request)
        return JSONResponse(
            content={
                "items": items,
                "legal_minimums": LEGAL_MIN_TTL_DAYS,
                "modes": list(RETENTION_MODES),
            }
        )

    @app.put("/api/v1/admin/retention")
    async def put_retention(request: Request) -> JSONResponse:
        gate = _platform_admin_gate(request)
        if gate is not None:
            return gate
        admin = _platform_admin_user(request) or {}
        actor = str(admin.get("email") or admin.get("id") or "admin")
        try:
            body = await request.json()
        except Exception:
            return err(400, "bad_request", "invalid JSON body", request)
        if not isinstance(body, dict):
            return err(400, "bad_request", "body must be a JSON object", request)

        # 단일 {"data_type":..., "ttl_days":..., "mode":...} 또는
        # 배치 {"policies": {type: {ttl_days, mode}}} — 배치는 전부 검증 후
        # 하나라도 실패하면 아무것도 쓰지 않는다.
        updates: list[tuple[str, Any, Any]] = []
        if "policies" in body:
            policies = body.get("policies")
            if not isinstance(policies, dict):
                return err(400, "bad_request", "policies must be an object", request)
            for data_type, spec in policies.items():
                if not isinstance(spec, dict):
                    return err(
                        400,
                        "bad_request",
                        f"policy spec for {data_type} must be an object",
                        request,
                    )
                updates.append(
                    (
                        str(data_type),
                        spec["ttl_days"] if "ttl_days" in spec else _MISSING,
                        spec.get("mode"),
                    )
                )
        else:
            data_type = str(body.get("data_type") or "")
            if not data_type:
                return err(400, "bad_request", "data_type is required", request)
            updates.append(
                (
                    data_type,
                    body.get("ttl_days") if "ttl_days" in body else _MISSING,
                    body.get("mode") if "mode" in body else None,
                )
            )
        if not updates:
            return err(400, "bad_request", "no policy updates provided", request)

        # 1단계: 전체 검증(dry) — 실패 시 쓰기 없음
        try:
            for data_type, ttl_days, mode in updates:
                _validate_policy_update(store, data_type, ttl_days, mode)
        except RetentionPolicyError as e:
            return _err_for_policy(e, request, ctx)
        # 2단계: 적용 + 유형별 감사
        results = []
        for data_type, ttl_days, mode in updates:
            try:
                kwargs: dict[str, Any] = {}
                if ttl_days is not _MISSING:
                    kwargs["ttl_days"] = ttl_days
                if mode is not None:
                    kwargs["mode"] = mode
                res = store.set_retention_policy(data_type, actor=actor, **kwargs)
            except RetentionPolicyError as e:
                # 검증 통과 후 실패는 드묾 — 부분 적용 상태를 명시해 반환
                logger.warning("retention policy update failed mid-batch: %s", e.code)
                return _err_for_policy(e, request, ctx)
            results.append(res)
        try:
            items = store.list_retention_policies()
        except Exception:
            items = []
        return JSONResponse(content={"updated": results, "items": items})

    @app.post("/api/v1/admin/retention/purge")
    def post_retention_purge(request: Request, dry_run: bool = False) -> JSONResponse:
        gate = _platform_admin_gate(request)
        if gate is not None:
            return gate
        admin = _platform_admin_user(request) or {}
        actor = str(admin.get("email") or admin.get("id") or "admin")
        try:
            result = store.run_purge(dry_run=dry_run, actor=actor)
        except Exception as e:
            logger.warning("retention purge failed: %s", type(e).__name__)
            return err(500, "retention_purge_failed", "retention purge failed", request)
        return JSONResponse(content=result)


_MISSING = object()


def _validate_policy_update(store: Any, data_type: str, ttl_days: Any, mode: Any) -> None:
    """set_retention_policy와 동일 규칙의 사전 검증 — 배치 PUT의 원자성을
    위해 쓰기 전에 전체를 검사한다. store의 비공개 헬퍼에 의존하지 않고
    공개 상수/목록으로 독립 검증한다."""
    items = {p["data_type"]: p for p in store.list_retention_policies()}
    item = items.get(data_type)
    if item is None:
        raise RetentionPolicyError("unknown_data_type", f"unknown retention data_type: {data_type}")
    if ttl_days is _MISSING and mode is None:
        raise RetentionPolicyError("empty_update", "ttl_days or mode is required")
    if mode is not None and mode not in RETENTION_MODES:
        raise RetentionPolicyError("invalid_mode", f"mode must be one of {RETENTION_MODES}")
    # set_retention_policy와 동일 규칙 — ttl_days=null(override 삭제)에
    # mode를 동반하면 store 단계에서 거부돼 배치가 부분 적용된다.
    if ttl_days is not _MISSING and ttl_days is None and mode is not None:
        raise RetentionPolicyError("invalid_mode", "mode cannot be combined with ttl_days=null")
    # 정책이 지원하지 않는 mode(clear_field/anonymize는 clear_field 대상
    # 컬럼이 있는 정책만)는 배치 쓰기 전에 여기서 거부한다.
    if ttl_days is not None or mode is not None:
        eff_mode = mode if mode is not None else str(item.get("mode") or "")
        supported = item.get("supported_modes") or ["hard_delete"]
        if eff_mode not in supported:
            raise RetentionPolicyError(
                "invalid_mode",
                f"mode '{eff_mode}' is not supported for {data_type}"
                f" (supported: {', '.join(supported)})",
            )
    if ttl_days is not _MISSING and ttl_days is not None:
        try:
            ttl = int(ttl_days)
        except (TypeError, ValueError):
            raise RetentionPolicyError("invalid_ttl", "ttl_days must be an integer") from None
        if ttl < 1:
            raise RetentionPolicyError("invalid_ttl", "ttl_days must be >= 1")
        min_days = LEGAL_MIN_TTL_DAYS.get(data_type)
        if min_days is not None and ttl < min_days:
            raise RetentionPolicyError(
                "retention_below_legal_minimum",
                f"{data_type} ttl_days must be >= {min_days} (legal minimum)",
            )
