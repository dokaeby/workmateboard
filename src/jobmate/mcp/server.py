"""jobMate Store 데이터를 MCP(MODEL CONTEXT PROTOCOL) 도구로 노출하는 stdio 서버.

실행: ``python -m jobmate.mcp.server``
Claude Desktop 등 MCP 클라이언트의 stdio transport로 연결한다.
모든 도구는 읽기 전용이며 Store의 공개 메서드만 사용한다 (raw SQL 없음).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any

from mcp.server.fastmcp import FastMCP

from ..config import Settings, database_dsn
from ..store.db import Store

logger = logging.getLogger("jobmate.mcp.server")

mcp = FastMCP("jobmate")

_store: Store | None = None


def _get_store() -> Store:
    """Store를 지연 초기화한다. 실패 시 호출자에게 예외를 전파한다."""
    global _store
    if _store is None:
        try:
            dsn = database_dsn(Settings())
        except Exception:
            dsn = os.environ.get("JOBMATE_DB_DSN", "data/jobmate.db")
        _store = Store(dsn)
        logger.info("mcp store opened dsn=%s", dsn)
    return _store


def _json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def _clamp_limit(limit: int, cap: int = 500) -> int:
    return max(1, min(int(limit), cap))


@mcp.tool()
def list_runs(status: str = "", limit: int = 50) -> str:
    """워크플로 실행(run) 목록을 최신순으로 반환한다.

    Args:
        status: 선택. RunStatus 값 (queued/running/waiting_approval/completed/failed/...)
        limit: 최대 반환 건수 (기본 50, 최대 500)
    """
    store = _get_store()
    # status 필터는 Store에 없으므로 넉넉히 가져와 파이썬에서 필터링한다.
    fetch = _clamp_limit(limit * 4 if status else limit, cap=1000)
    runs = store.list_runs(limit=fetch)
    if status:
        runs = [r for r in runs if r.status.value == status]
    runs = runs[: _clamp_limit(limit)]
    return _json([r.model_dump(mode="json") for r in runs])


@mcp.tool()
def get_run(run_id: str) -> str:
    """run_id로 단일 실행의 상세 정보(Run 레코드)를 반환한다."""
    run = _get_store().get_run(run_id)
    if run is None:
        return _json({"error": f"run not found: {run_id}"})
    return _json(run.model_dump(mode="json"))


@mcp.tool()
def list_inbox_items(status: str = "", limit: int = 50) -> str:
    """메시지 인박스(message_extractions) 항목을 최신순으로 반환한다.

    Args:
        status: 선택. processing_status 값 (pending/processed/failed)
        limit: 최대 반환 건수 (기본 50, 최대 500)
    """
    store = _get_store()
    fetch = _clamp_limit(limit * 4 if status else limit, cap=1000)
    rows = store.list_message_extractions(limit=fetch)
    if status:
        rows = [r for r in rows if r.get("processing_status") == status]
    rows = rows[: _clamp_limit(limit)]
    return _json(rows)


@mcp.tool()
def list_scheduled_events(status: str = "", limit: int = 50) -> str:
    """스케줄 이벤트(scheduled_events) 목록을 최신순으로 반환한다.

    Args:
        status: 선택. candidate/needs_review/needs_confirmation/registered/rejected
        limit: 최대 반환 건수 (기본 50, 최대 500)
    """
    rows = _get_store().list_scheduled_events(status or None, _clamp_limit(limit))
    return _json(rows)


@mcp.tool()
def run_stats(days: int = 7) -> str:
    """최근 N일간 run 통계를 상태별/워크플로별 건수로 집계해 반환한다."""
    store = _get_store()
    days = max(1, min(int(days), 365))
    since = datetime.now(UTC) - timedelta(days=days)
    # 통계 정확도를 위해 상한을 넉넉히 둔다 (Store에 기간 필터가 없음).
    runs = store.list_runs(limit=5000)
    recent = [r for r in runs if r.created_at >= since]
    by_status: dict[str, int] = {}
    by_workflow: dict[str, int] = {}
    for r in recent:
        by_status[r.status.value] = by_status.get(r.status.value, 0) + 1
        by_workflow[r.workflow_key] = by_workflow.get(r.workflow_key, 0) + 1
    return _json(
        {
            "days": days,
            "since": since.isoformat(),
            "total": len(recent),
            "by_status": by_status,
            "by_workflow": by_workflow,
            "scanned": len(runs),
        }
    )


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    # 원격 transport 노출 금지 — stdio만 허용한다. 향후 HTTP/SSE transport를
    # 추가할 경우 반드시 JOBMATE_MCP_AUTH_TOKEN 기반 인증을 구현한 뒤에만
    # 활성화할 것(인증 없이 원격 노출하면 DB 조회 도구가 무방비가 된다).
    transport = os.environ.get("JOBMATE_MCP_TRANSPORT", "stdio").strip().lower()
    if transport != "stdio":
        raise SystemExit(f"MCP transport '{transport}' is not supported (only stdio is allowed)")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
