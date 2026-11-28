"""Backup admin routes — spec 004 US3 (FR-023).

GET /api/v1/backups — 실행 이력 목록
POST /api/v1/backups/run — 수동 1회 백업(동기 실행)
GET /api/v1/backups/{id}/verify — 수동 복원 검증 트리거

백업은 플랫폼 레벨 작업이므로 _platform_admin_gate 사용(org 역할 무관).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ...services import backup as backup_svc

logger = logging.getLogger("jobmate.api")


def register(app: FastAPI, ctx: Any) -> None:
    store = ctx.store
    settings = ctx.settings
    _platform_admin_gate = ctx._platform_admin_gate

    @app.get("/api/v1/backups")
    def list_backups(request: Request, limit: int = 50) -> JSONResponse:
        gate = _platform_admin_gate(request)
        if gate is not None:
            return gate
        try:
            rows = store.list_backup_runs(limit=limit)
        except Exception as e:
            logger.warning("backup list failed: %s", type(e).__name__)
            return ctx.err(500, "backup_list_failed", "failed to list backups", request)
        return JSONResponse(content={"items": rows, "count": len(rows)})

    @app.post("/api/v1/backups/run")
    def run_backup_now(request: Request) -> JSONResponse:
        gate = _platform_admin_gate(request)
        if gate is not None:
            return gate
        # SQLite 크기에서는 동기 실행이 안전 — 대형 PG는 별도 잡 권장(문서화)
        res = backup_svc.run_backup(store, settings)
        rid = str(res.get("run_id") or "")
        try:
            store.audit(
                rid or "system",
                "admin",
                "backup_run",
                str(res.get("file") or ""),
                {"manual": True, "status": res.get("status")},
            )
        except Exception:
            logger.debug("backup_run audit failed", exc_info=True)
        status_code = 200 if res.get("status") == "success" else 500
        return JSONResponse(content=res, status_code=status_code)

    @app.get("/api/v1/backups/{run_id}/verify")
    def verify_backup(request: Request, run_id: str) -> JSONResponse:
        gate = _platform_admin_gate(request)
        if gate is not None:
            return gate
        if store.get_backup_run(run_id) is None:
            return ctx.err(404, "not_found", "backup run not found", request)
        res = backup_svc.verify_latest_backup(store, settings, run_id=run_id)
        try:
            store.audit(
                run_id,
                "admin",
                "backup_verify",
                str(res.get("file") or ""),
                {"manual": True, "verify_status": res.get("verify_status")},
            )
        except Exception:
            logger.debug("backup_verify audit failed", exc_info=True)
        ok = res.get("verify_status") == "ok"
        return JSONResponse(content=res, status_code=200 if ok else 500)
