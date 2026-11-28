"""pg_dialect 번역 계층 테스트 — 실제 PG 서버 없이 번역 결과 SQL을 검증한다.

_pg_translate 출력은 psycopg3 플레이스홀더 규칙(%s/%b/%t, %% 이스케이프)을
따라야 하므로, 가능하면 psycopg 내부 쿼리 파서(_split_query)로도 검증한다.
"""

import socket
import threading
from unittest.mock import patch

import pytest

from jobmate.store.pg_dialect import _is_pg_dsn, _pg_translate, _resolve_ipv4_hostaddr


def _split_query(sql: str) -> None:
    """번역된 SQL을 psycopg3 플레이스홀더 파서에 통과시킨다(내부 API)."""
    try:
        from psycopg._queries import _split_query as split
    except ImportError:
        pytest.skip("psycopg internals unavailable")
    split(sql.encode("utf-8"))


def test_is_pg_dsn():
    assert _is_pg_dsn("postgres://u:p@h/db")
    assert _is_pg_dsn("postgresql://u:p@h/db")
    assert _is_pg_dsn("postgresql+psycopg://u:p@h/db") is False
    # 스킴 대소문자 무시
    assert _is_pg_dsn("Postgres://u:p@h/db")
    assert _is_pg_dsn("POSTGRESQL://u:p@h/db")
    assert not _is_pg_dsn("data/jobmate.db")
    assert not _is_pg_dsn("postgres-notes.db")
    assert not _is_pg_dsn("")


def test_placeholder_translation():
    out = _pg_translate("SELECT * FROM t WHERE a=? AND b<=?")
    assert out == "SELECT * FROM t WHERE a=%s AND b<=%s"


def test_insert_or_ignore_without_returning():
    out = _pg_translate("INSERT OR IGNORE INTO t(a,b) VALUES(?,?)")
    assert out == "INSERT INTO t(a,b) VALUES(%s,%s) ON CONFLICT DO NOTHING"


def test_insert_or_ignore_with_returning():
    out = _pg_translate("INSERT OR IGNORE INTO t(a) VALUES(?) RETURNING id")
    assert out == "INSERT INTO t(a) VALUES(%s) ON CONFLICT DO NOTHING RETURNING id"


def test_insert_or_ignore_existing_on_conflict_not_doubled():
    """INSERT OR IGNORE + 기존 ON CONFLICT 절 — DO NOTHING을 중복 append하지 않는다."""
    out = _pg_translate("INSERT OR IGNORE INTO t(a,b,c) VALUES(?,?,?) ON CONFLICT DO NOTHING")
    assert out == "INSERT INTO t(a,b,c) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING"
    assert out.count("ON CONFLICT") == 1


def test_plain_insert_passthrough():
    sql = "INSERT INTO t(a) VALUES(?)"
    assert _pg_translate(sql) == "INSERT INTO t(a) VALUES(%s)"


def test_on_conflict_parens_preserved():
    """PG는 ON CONFLICT 대상 컬럼에 괄호를 요구한다 — 번역이 망가뜨리지 않는지."""
    sql = (
        "INSERT INTO external_tasks(provider,company_id,external_id,title)"
        " VALUES(?,?,?,?,?)"
        " ON CONFLICT (provider,company_id,external_id) DO UPDATE SET"
        " title=excluded.title"
    )
    out = _pg_translate(sql)
    assert "ON CONFLICT (provider,company_id,external_id) DO UPDATE SET" in out
    assert "excluded.title" in out
    assert out.count("%s") == 5


def test_select_passthrough():
    sql = "SELECT id, name FROM agents WHERE lower(name)=? ORDER BY name LIMIT ?"
    out = _pg_translate(sql)
    assert out == sql.replace("?", "%s")


def test_literal_percent_escaped():
    """LIKE 패턴의 '%'는 psycopg가 거부한다 — '%%'로 이스케이프돼야 한다.

    회귀: radar.py의 title LIKE '%skill_radar%', management.py의
    identifier LIKE 'WMB-%'가 PG에서 ProgrammingError를 냈다."""
    out = _pg_translate(
        "DELETE FROM inbox_items WHERE kind='approval' AND title LIKE '%skill_radar%'"
    )
    assert "%%skill_radar%%" in out
    out = _pg_translate("SELECT identifier FROM tasks WHERE identifier LIKE 'WMB-%'")
    assert "WMB-%%" in out


def test_escaped_percent_survives_psycopg_parser():
    """번역 결과가 psycopg 플레이스홀더 파서를 실제로 통과하는지."""
    cases = [
        "DELETE FROM inbox_items WHERE kind='approval' AND title LIKE '%skill_radar%'",
        "SELECT identifier FROM tasks WHERE identifier LIKE 'WMB-%'",
        "INSERT OR IGNORE INTO t(a,b) VALUES(?,?)",
        "INSERT OR IGNORE INTO t(a) VALUES(?) RETURNING id",
        "SELECT * FROM t WHERE a=? AND b<=?",
        "INSERT INTO t(a) VALUES(?) ON CONFLICT (a) DO UPDATE SET a=excluded.a",
    ]
    for sql in cases:
        _split_query(_pg_translate(sql))


def test_percent_before_placeholder_order():
    """'%' 이스케이프가 '?'→'%s' 변환보다 먼저 일어나야 한다 —
    순서가 바뀌면 방금 만든 %s 플레이스홀더가 %%s로 깨진다."""
    out = _pg_translate("SELECT * FROM t WHERE p LIKE '%x%' AND id=?")
    assert out == "SELECT * FROM t WHERE p LIKE '%%x%%' AND id=%s"


def test_resolve_ipv4_hostaddr():
    """듀얼스택 호스트 → 첫 IPv4 주소. Vercel(IPv6 egress 없음) 500 회귀 방지."""
    dsn = "postgres://u:p@db.example.neon.tech:5432/app?sslmode=require"

    def fake_gai(host, port, family, type_):
        if family == socket.AF_INET:
            return [(2, 1, 6, "", ("52.76.108.241", 5432))]
        return []

    with patch("jobmate.store.pg_dialect.socket.getaddrinfo", side_effect=fake_gai):
        assert _resolve_ipv4_hostaddr(dsn) == "52.76.108.241"


def test_resolve_ipv4_hostaddr_no_ipv4():
    """IPv6 전용 호스트 → None (호출부가 hostaddr 없이 기존 경로로 접속)."""
    with patch("jobmate.store.pg_dialect.socket.getaddrinfo", return_value=[]):
        assert _resolve_ipv4_hostaddr("postgres://u:p@v6only.example/db") is None


def test_resolve_ipv4_hostaddr_non_tcp_dsn():
    """유닉스 소켓/비DSN → None."""
    assert _resolve_ipv4_hostaddr("/var/run/postgresql") is None
    assert _resolve_ipv4_hostaddr("data/jobmate.db") is None


# ---------- _PGConn 트랜잭션 스택 (실제 PG 없이 fake 연결로 검증) ----------


def _bare_pgconn():
    """__init__의 psycopg.connect를 우회한 _PGConn — _c/_txs만 수동 구성."""
    import psycopg

    from jobmate.store.pg_dialect import _PGConn

    conn = object.__new__(_PGConn)
    conn._dsn = "postgres://fake/db"
    conn._txs = []
    conn._lock = threading.RLock()
    conn._reconnect_lock = threading.Lock()
    conn._reconnects = 0
    conn._reconnect = lambda: setattr(conn, "_reconnects", conn._reconnects + 1)  # type: ignore[attr-defined]
    return conn, psycopg


class _FakeTx:
    def __init__(self, events: list[str], name: str) -> None:
        self._events = events
        self._name = name

    def __enter__(self) -> "_FakeTx":
        self._events.append(f"enter:{self._name}")
        return self

    def __exit__(self, *a: object) -> bool:
        self._events.append(f"exit:{self._name}")
        return False


def test_pgconn_nested_tx_lifo():
    """중첩 tx()는 스택으로 관리 — 안쪽 __exit__가 바깥 트랜잭션을 닫지 않는다."""
    conn, _ = _bare_pgconn()
    events: list[str] = []
    seq = iter(range(10))

    class FakeC:
        def transaction(self) -> _FakeTx:
            return _FakeTx(events, f"tx{next(seq)}")

    conn._c = FakeC()
    with conn:
        with conn:
            pass
    assert events == ["enter:tx0", "enter:tx1", "exit:tx1", "exit:tx0"]
    assert conn._txs == []


def test_pgconn_nested_enter_failure_no_reconnect():
    """중첩 tx 진입 실패 시 재접속 금지 — self._c 교체가 바깥 트랜잭션을 파괴."""
    conn, psycopg = _bare_pgconn()
    events: list[str] = []

    class FailTx:
        def __enter__(self) -> "FailTx":
            raise psycopg.OperationalError("dead")

        def __exit__(self, *a: object) -> bool:
            return False

    class FakeC:
        def __init__(self) -> None:
            self.calls = 0

        def transaction(self) -> object:
            self.calls += 1
            # 첫 번째(바깥) tx는 성공, 두 번째(안쪽) tx는 진입 실패
            return _FakeTx(events, "outer") if self.calls == 1 else FailTx()

    conn._c = FakeC()
    with conn:
        with pytest.raises(psycopg.OperationalError):
            with conn:
                pass
    # 재접속 없이 바깥 트랜잭션이 정상 종료돼야 한다
    assert conn._reconnects == 0
    assert events == ["enter:outer", "exit:outer"]
    assert conn._txs == []


def test_pgconn_execute_no_retry_inside_tx():
    """트랜잭션 안 OperationalError는 재접속 재시도 없이 전파된다."""
    conn, psycopg = _bare_pgconn()

    class FakeC:
        def execute(self, sql: str, params: object = ()) -> None:
            raise psycopg.OperationalError("dead")

        def transaction(self) -> _FakeTx:
            return _FakeTx([], "tx")

    conn._c = FakeC()
    with conn:
        with pytest.raises(psycopg.OperationalError):
            conn.execute("SELECT 1")
    assert conn._reconnects == 0
