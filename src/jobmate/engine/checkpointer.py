"""LangGraph checkpointer 생성·캐시·정리 — runner.py에서 분리된 모듈."""

from __future__ import annotations

import logging
import os
import threading
from typing import TYPE_CHECKING, Any

from langgraph.checkpoint.sqlite import SqliteSaver

if TYPE_CHECKING:
    from ..config import Settings

logger = logging.getLogger("jobmate.engine")


def build_checkpointer(db_path: str) -> SqliteSaver:
    """SqliteSaver 컨텍스트를 열어 반환한다. saver._ctx를 GC 전까지 유지."""
    ctx = SqliteSaver.from_conn_string(db_path)
    saver = ctx.__enter__()
    saver._ctx = ctx  # type: ignore[attr-defined]
    return saver


# ---------- checkpointer (프로세스 수명 동안 1회 생성 후 재사용) ----------
# run마다 psycopg.connect/SqliteSaver를 새로 만들면 PG 커넥션이 누수된다.
# (kind, target) 키로 캐시해 build_runner 호출이 같은 saver를 공유하게 한다.
_CHECKPOINTER_CACHE: dict[str, Any] = {}
_CHECKPOINTER_LOCK = threading.Lock()


def _checkpointer_target(settings: Settings) -> tuple[str, str]:
    """(kind, target) — "pg"+DSN 또는 "sqlite"+checkpoint 파일 경로.

    sqlite 경로는 Store와 동일한 database_dsn() 기준을 쓴다 — Vercel에서는
    /tmp/jobmate.db 이므로 checkpoint도 /tmp/jobmate.db.checkpoints가 되어
    Store DB와 같은 위치/수명을 갖는다 (이전에는 settings.db_path 그대로라
    Vercel에서 Store와 다른 경로를 가리켰다).
    """
    from ..config import database_dsn

    dsn = (
        settings.database_url
        or os.environ.get("POSTGRES_URL", "")
        or os.environ.get("POSTGRES_PRISMA_URL", "")
    )
    if dsn:
        return "pg", dsn
    return "sqlite", database_dsn(settings) + ".checkpoints"


def _build_pg_checkpointer(dsn: str) -> Any:
    import psycopg
    from langgraph.checkpoint.postgres import PostgresSaver
    from psycopg.rows import dict_row

    conn = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
    saver = PostgresSaver(conn)
    saver.setup()  # checkpoint 테이블 생성 (idempotent) — 캐시로 프로세스당 1회
    return saver


def build_checkpointer_for(settings: Settings) -> Any:
    """Postgres DSN이 있으면 PostgresSaver, 아니면 SQLite — 프로세스당 1회 생성.

    서버리스(Vercel)에서는 /tmp가 인스턴스별로 분리되어 SQLite 체크포인트가
    invoke 인스턴스와 resume 인스턴스 사이에서 유실된다. DB가 Postgres면
    checkpoint도 같은 DB에 두어 어느 인스턴스든 interrupt를 재개할 수 있게 한다.

    반환값은 모듈 레벨 캐시의 공유 인스턴스다 — run마다 새 커넥션을 열지
    않으므로 커넥션 누수가 없다. 명시적 정리가 필요하면 close_checkpointers().
    """
    kind, target = _checkpointer_target(settings)
    key = f"{kind}:{target}"
    with _CHECKPOINTER_LOCK:
        saver = _CHECKPOINTER_CACHE.get(key)
        if saver is not None:
            return saver
        saver = _build_pg_checkpointer(target) if kind == "pg" else build_checkpointer(target)
        _CHECKPOINTER_CACHE[key] = saver
        # pg DSN에는 비밀번호가 포함될 수 있으므로 kind만 기록한다.
        logger.info("checkpointer created kind=%s", kind)
        return saver


def _close_saver(saver: Any) -> None:
    """checkpointer의 커넥션/컨텍스트를 best-effort로 닫는다."""
    ctx = getattr(saver, "_ctx", None)  # SqliteSaver의 from_conn_string 컨텍스트
    if ctx is not None:
        try:
            ctx.__exit__(None, None, None)
        except Exception:
            logger.debug("checkpointer ctx close failed", exc_info=True)
    conn = getattr(saver, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            logger.debug("checkpointer conn close failed", exc_info=True)


def close_checkpointers() -> None:
    """캐시된 모든 checkpointer를 닫고 캐시를 비운다 (테스트/재빌드용 훅)."""
    with _CHECKPOINTER_LOCK:
        savers = list(_CHECKPOINTER_CACHE.values())
        _CHECKPOINTER_CACHE.clear()
    for saver in savers:
        _close_saver(saver)
