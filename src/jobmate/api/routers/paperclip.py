"""Paperclip integration routes — 외부 오케스트레이터 projection (spec 3차 E/F).

POST /api/v1/management/integrations/paperclip/snapshot (machine auth +
idempotency),
GET /api/v1/management/integrations/paperclip/status.

외부 상태는 읽기 전용 projection — native agents/tasks/runs와 섞지 않고
외부 task를 native run으로 자동 실행하지 않는다. snapshot은 upsert
의미라 재전송해도 안전하고, snapshot에 없는 행은 삭제하지 않는다
(끊긴 외부 연결에서도 마지막 정상 상태가 stale 배지로 유지된다).
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ...store.external_sync import SnapshotConflictError
from ..schemas import PaperclipSnapshotRequest

logger = logging.getLogger("jobmate.api")


def register(app: FastAPI, ctx: Any) -> None:
    store = ctx.store
    err = ctx.err
    _lead_gate = ctx._lead_gate
    _audit_api = ctx._audit_api

    from ...security import authorized

    def _machine_gate(request: Request) -> JSONResponse | None:
        """머진 전용 — 유효 API 키만 허용. 세션 쿠키만으로는 403.

        management._machine_gate와 동일 규칙 — authorized()는 api_key
        미설정 로컬 개발 모드에서도 True를 반환한다."""
        if not authorized(request):
            logger.warning("paperclip snapshot rejected: session-only auth")
            return err(403, "forbidden", "machine API key required", request)
        return None

    @app.post("/api/v1/management/integrations/paperclip/snapshot")
    def paperclip_snapshot(body: PaperclipSnapshotRequest, request: Request) -> JSONResponse:
        """외부 snapshot ingest — paperclip-bridge가 변경분을 push한다.

        멱등: Idempotency-Key 헤더 또는 body.idempotency_key 필수. 동일
        키+동일 payload는 200 replay, 동일 키+다른 payload는 409.
        로깅은 provider/company/건수/소요시간만 — payload·자격증명 금지."""
        gate = _machine_gate(request)
        if gate is not None:
            return gate
        idem_key = (body.idempotency_key or request.headers.get("idempotency-key") or "").strip()
        if not idem_key:
            return err(
                422,
                "validation",
                "Idempotency-Key header or idempotency_key is required",
                request,
            )
        provider = (body.provider or "").strip().lower()[:40] or "paperclip"
        company_id = body.company_id.strip()[:200]
        # payload 해시 — 정렬된 canonical JSON 기준(키 순서 무관).
        # provider/company_id도 해시 입력에 포함한다 — 같은 idempotency_key로
        # 다른 company/제공자의 snapshot이 replay로 잘못 합쳐지는 것을 막는다.
        # (provider는 위에서 lowercase 정규화 — 'Paperclip'과 'paperclip'은 동일 해시)
        canonical_items = {
            "provider": provider,
            "company_id": company_id,
            "agents": [a.model_dump(mode="json") for a in body.agents],
            "tasks": [t.model_dump(mode="json") for t in body.tasks],
            "runs": [r.model_dump(mode="json") for r in body.runs],
            "cursors": dict(sorted((body.cursors or {}).items())),
        }
        payload_hash = hashlib.sha256(
            json.dumps(canonical_items, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        started = time.perf_counter()
        try:
            result = store.ingest_external_snapshot(
                provider=provider,
                company_id=company_id,
                agents=[a.model_dump(mode="json") for a in body.agents],
                tasks=[t.model_dump(mode="json") for t in body.tasks],
                runs=[r.model_dump(mode="json") for r in body.runs],
                cursors=dict(body.cursors or {}),
                idempotency_key=idem_key,
                payload_hash=payload_hash,
            )
        except SnapshotConflictError:
            return err(
                409,
                "idempotency_conflict",
                "idempotency_key already used with a different payload",
                request,
            )
        except ValueError as e:
            return err(422, "validation", str(e), request)
        elapsed_ms = (time.perf_counter() - started) * 1000
        replayed = bool(result.get("replayed"))
        logger.info(
            "paperclip snapshot %s provider=%s company=%s agents=%s tasks=%s "
            "runs=%s replayed=%s %.0fms",
            "replayed" if replayed else "accepted",
            provider,
            company_id[:80],
            result.get("agents", 0),
            result.get("tasks", 0),
            result.get("runs", 0),
            replayed,
            elapsed_ms,
        )
        _audit_api(
            request,
            "paperclip_snapshot",
            f"{provider}/{company_id}",
            {
                "key": idem_key[:80],
                "agents": result.get("agents", 0),
                "tasks": result.get("tasks", 0),
                "runs": result.get("runs", 0),
                "replayed": replayed,
            },
        )
        return JSONResponse(
            status_code=200 if replayed else 201,
            content=result,
        )

    @app.get("/api/v1/management/integrations/paperclip/status")
    def paperclip_status(request: Request, provider: str | None = None) -> JSONResponse:
        """동기화 상태 — 머신 API 키 또는 lead 세션. 읽기 전용 카운트/커서만
        반환하며 payload 원문은 절대 포함하지 않는다."""
        if not authorized(request):
            gate = _lead_gate(request)
            if gate is not None:
                return gate
        return JSONResponse(content=store.external_sync_status(provider=provider))
