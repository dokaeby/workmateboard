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
        # psycopg Connection 하나를 모든 스레드가 공유한다 — 일반 읽기
        # execute()도 tx()와 같은 락으로 직렬화해야, tx 도중 다른 스레드의
        # 문장이 열린 트랜잭션에 끼어들어 미커밋 읽기/abort 전파가 일어나지
        # 않는다. RLock — tx 보유 중 같은 스레드의 execute 재진입 허용.
        self._lock = threading.RLock()
        # 중첩 tx()는 psycopg가 savepoint로 처리한다 — 단일 슬롯이면 안쪽
        # __exit__가 바깥 트랜잭션까지 닫은 것으로 오인하므로 스택으로 둔다.
        self._txs: list[Any] = []
        self._connect()

    def _connect(self) -> None:
        self._c = psycopg.connect(self._dsn, autocommit=True, row_factory=dict_row)

    def _reconnect(self) -> None:
        # 호출자는 반드시 self._lock을 잡고 있어야 한다 — 열린 tx가 있으면
        # 커넥션 교체가 바깥 트랜잭션을 파괴하므로 __enter__에서 걸러진다.
        with self._reconnect_lock:
            try:
                self._c.close()
            except Exception as e:
                logger.debug("pg conn close before reconnect failed: %s", e)
            self._connect()

    def execute(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> Any:
        # 락을 execute 단위로만 잡으면 tx() 블록 도중 다른 스레드의 읽기가
        # 끼어들 수 있으므로, __enter__가 잡은 락을 __exit__까지 보유하는
        # 구조와 결합해 커넥션 접근 전체를 직렬화한다.
        with self._lock:
            try:
                return self._c.execute(_pg_translate(sql), params)
            except psycopg.OperationalError:
                # 트랜잭션 안에서는 재시도하지 않는다 — 새 autocommit 연결로
                # 같은 문장을 다시 실행하면 부분 커밋/중복 쓰기가 생긴다.
                # 예외를 올려 tx 블록 전체가 실패하게 하고 호출자가 블록
                # 단위로 재시도한다.
                if self._txs:
                    raise
                self._reconnect()
                return self._c.execute(_pg_translate(sql), params)

    def __enter__(self) -> _PGConn:
        self._lock.acquire()
        try:
            tx = self._c.transaction()
            try:
                tx.__enter__()
            except psycopg.OperationalError:
                # 중첩 tx(바깥 트랜잭션 진행 중)에서는 재접속 금지 — self._c를
                # 새 커넥션으로 바꾸면 self._txs에 남은 바깥 tx가 죽은 커넥션을
                # 가리켜 바깥 트랜잭션의 원자성이 깨진다.
                if self._txs:
                    raise
                self._reconnect()
                tx = self._c.transaction()
                tx.__enter__()
            # __enter__가 성공한 트랜잭션만 스택에 올린다 — 실패 시 잔여 상태가
            # 남아 이후 execute()의 재접속 재시도가 영구 차단되는 것을 막는다.
            self._txs.append(tx)
        except BaseException:
            self._lock.release()
            raise
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        try:
            tx = self._txs.pop()
            return tx.__exit__(exc_type, exc, tb)
        finally:
            self._lock.release()

    def close(self) -> None:
        with self._lock:
            self._c.close()
