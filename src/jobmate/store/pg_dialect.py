"""PostgreSQL 방언 어댑터 — jobmate.store.db에서 분리.

SQLite 방언 SQL을 psycopg 연결로 실행하기 위한 변환 계층.
Serverless 환경의 끊긴 연결 자동 재접속도 여기서 처리한다.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

import psycopg
from psycopg.rows import dict_row

logger = logging.getLogger("jobmate.store.pg")


_PG_DSN_PREFIXES = ("postgres://", "postgresql://")


def _is_pg_dsn(path: str) -> bool:
    # 스킴은 대소문자 무시(RFC 3986) — 'Postgres://' DSN도 PG로 인식한다.
    return path.lower().startswith(_PG_DSN_PREFIXES)


def _pg_translate(sql: str) -> str:
    """SQLite 방언 → PostgreSQL 방언 변환 (? 플레이스홀더, INSERT OR IGNORE).

    psycopg3는 쿼리 텍스트의 bare '%'를 거부한다('%s'/'%b'/'%t' 플레이스홀더와
    '%%' 이스케이프만 허용). LIKE 패턴 등 리터럴 '%'가 SQL에 있으면
    ProgrammingError가 나므로, '?'→'%s' 변환 전에 기존 '%'를 전부 '%%'로
    이스케이프한다 — psycopg가 '%%'를 '%' 하나로 되돌리므로 PG가 보는
    리터럴은 SQLite 의도와 동일하다."""
    sql = sql.replace("%", "%%")
    sql = sql.replace("?", "%s")
    if "INSERT OR IGNORE INTO" in sql:
        sql = sql.replace("INSERT OR IGNORE INTO", "INSERT INTO", 1)
        if " RETURNING" in sql:
            sql = sql.replace(" RETURNING", " ON CONFLICT DO NOTHING RETURNING", 1)
        elif "ON CONFLICT" not in sql:
            # 이미 ON CONFLICT가 있으면 DO NOTHING을 또 붙이지 않는다 —
            # 'ON CONFLICT ... ON CONFLICT'는 PG 문법 오류다.
            sql += " ON CONFLICT DO NOTHING"
    return sql


class _PGConn:
    """psycopg 연결 래퍼 — Store의 sqlite3.Connection 사용 패턴을 모사.

    서버리스(Vercel)에서는 함수 freeze 동안 DB 측이 유휴 연결을 끊으므로,
    죽은 연결을 만나면 1회 재접속 후 재시도한다."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._reconnect_lock = threading.Lock()
        self._connect()

    def _connect(self) -> None:
        self._c = psycopg.connect(self._dsn, autocommit=True, row_factory=dict_row)

    def _reconnect(self) -> None:
        with self._reconnect_lock:
            try:
                self._c.close()
            except Exception as e:
                logger.debug("pg conn close before reconnect failed: %s", e)
            self._connect()

    def execute(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> Any:
        try:
            return self._c.execute(_pg_translate(sql), params)
        except psycopg.OperationalError:
            self._reconnect()
            return self._c.execute(_pg_translate(sql), params)

    def __enter__(self) -> _PGConn:
        try:
            self._tx = self._c.transaction()
            self._tx.__enter__()
        except psycopg.OperationalError:
            self._reconnect()
            self._tx = self._c.transaction()
            self._tx.__enter__()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        return self._tx.__exit__(exc_type, exc, tb)

    def close(self) -> None:
        self._c.close()
