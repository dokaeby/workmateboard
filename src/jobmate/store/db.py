"""SQLite/PostgreSQL 저장소 — runs/stage_runs/audit/approvals/event_links/notifications."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import psycopg
from psycopg.rows import dict_row

from ..models import (
    Approval,
    AuditEvent,
    EventLink,
    LinkStatus,
    Notification,
    NotificationStatus,
    Run,
    RunStatus,
    StageRun,
    StageStatus,
)

logger = logging.getLogger("jobmate.store")

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  workflow_key TEXT NOT NULL,
  workflow_version TEXT NOT NULL,
  input TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL,
  trace_id TEXT NOT NULL,
  error TEXT NOT NULL DEFAULT '',
  metadata TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);

CREATE TABLE IF NOT EXISTS stage_runs (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id),
  stage_key TEXT NOT NULL,
  attempt INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL,
  input TEXT NOT NULL DEFAULT '{}',
  output TEXT NOT NULL DEFAULT '{}',
  error TEXT NOT NULL DEFAULT '',
  model TEXT NOT NULL DEFAULT '',
  prompt_version TEXT NOT NULL DEFAULT '',
  tokens_in INTEGER NOT NULL DEFAULT 0,
  tokens_out INTEGER NOT NULL DEFAULT 0,
  started_at TEXT,
  finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_stage_runs_run ON stage_runs(run_id);

CREATE TABLE IF NOT EXISTS approvals (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id),
  stage_key TEXT NOT NULL,
  decision TEXT NOT NULL,
  comment TEXT NOT NULL DEFAULT '',
  decided_by TEXT NOT NULL DEFAULT 'local',
  decided_at TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS audit_events (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  object_ref TEXT NOT NULL DEFAULT '',
  detail TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_run ON audit_events(run_id);

CREATE TABLE IF NOT EXISTS event_links (
  id TEXT PRIMARY KEY,
  idempotency_key TEXT NOT NULL UNIQUE,
  gmail_message_id TEXT NOT NULL,
  connection_id TEXT NOT NULL DEFAULT 'default',
  status TEXT NOT NULL,
  calendar_event_id TEXT NOT NULL DEFAULT '',
  notification_id TEXT NOT NULL DEFAULT '',
  source_url TEXT NOT NULL DEFAULT '',
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_links_msg ON event_links(gmail_message_id);

CREATE TABLE IF NOT EXISTS notifications (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL DEFAULT '',
  link_id TEXT NOT NULL DEFAULT '',
  channel TEXT NOT NULL DEFAULT 'inapp',
  title TEXT NOT NULL,
  body TEXT NOT NULL DEFAULT '',
  source_url TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  sent_at TEXT
);

CREATE TABLE IF NOT EXISTS automation_state (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- calendar-agent: 멀티채널 수신함 (gmail/slack/telegram 공통)
CREATE TABLE IF NOT EXISTS message_extractions (
  source_provider TEXT NOT NULL,
  source_id TEXT NOT NULL,
  thread_id TEXT NOT NULL DEFAULT '',
  extraction_json TEXT NOT NULL DEFAULT '{}',
  processing_status TEXT NOT NULL DEFAULT 'pending',
  error_message TEXT NOT NULL DEFAULT '',
  attempts INTEGER NOT NULL DEFAULT 0,
  retry_at TEXT NOT NULL DEFAULT '',
  source_text TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  PRIMARY KEY (source_provider, source_id)
);

-- calendar-agent: 등록된 일정 레지스트리
CREATE TABLE IF NOT EXISTS scheduled_events (
  id TEXT PRIMARY KEY,
  source_provider TEXT NOT NULL,
  source_id TEXT NOT NULL,
  thread_id TEXT NOT NULL DEFAULT '',
  calendar_event_id TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL,
  start_at TEXT NOT NULL,
  end_at TEXT NOT NULL,
  timezone TEXT NOT NULL DEFAULT 'Asia/Seoul',
  location TEXT NOT NULL DEFAULT '',
  attendees_json TEXT NOT NULL DEFAULT '[]',
  summary TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'candidate',
  version INTEGER NOT NULL DEFAULT 1,
  confidence REAL NOT NULL DEFAULT 0.0,
  reason TEXT NOT NULL DEFAULT '',
  source_refs TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scheduled_events_status ON scheduled_events(status);
CREATE INDEX IF NOT EXISTS idx_scheduled_events_source
  ON scheduled_events(source_provider, source_id);

-- calendar-agent: 시작 전 알림 예약 작업
CREATE TABLE IF NOT EXISTS notification_jobs (
  id TEXT PRIMARY KEY,
  scheduled_event_id TEXT NOT NULL,
  notify_type TEXT NOT NULL,
  fire_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  delivered_at TEXT,
  error_message TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notification_jobs_fire ON notification_jobs(fire_at);

-- skillRadar: 플랫폼별 일일 수집 스냅샷
CREATE TABLE IF NOT EXISTS radar_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  platform TEXT NOT NULL,
  collected_date TEXT NOT NULL,
  metrics_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  UNIQUE(platform, collected_date)
);

CREATE TABLE IF NOT EXISTS radar_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  snapshot_id INTEGER NOT NULL REFERENCES radar_snapshots(id),
  rank INTEGER NOT NULL,
  repo_full_name TEXT NOT NULL,
  repo_url TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',
  stars INTEGER NOT NULL DEFAULT 0,
  language TEXT NOT NULL DEFAULT '',
  topics_json TEXT NOT NULL DEFAULT '[]',
  readme_excerpt TEXT NOT NULL DEFAULT '',
  summary_en TEXT NOT NULL DEFAULT '',
  summary_zh TEXT NOT NULL DEFAULT '',
  summary_ko TEXT NOT NULL DEFAULT '',
  content_hash TEXT NOT NULL DEFAULT '',
  stars_today INTEGER,
  license TEXT NOT NULL DEFAULT '',
  taxonomy TEXT NOT NULL DEFAULT '',
  taxonomy_confidence REAL NOT NULL DEFAULT 0.0,
  rank_score REAL NOT NULL DEFAULT 0.0,
  summary_status TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_radar_items_snapshot ON radar_items(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_radar_items_repo ON radar_items(repo_full_name, content_hash);

-- Web Push 구독 정보
CREATE TABLE IF NOT EXISTS push_subscriptions (
  endpoint TEXT PRIMARY KEY,
  keys_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

-- 이메일 3줄 요약 카드 (cron-job.org 등 주기 체크 결과)
CREATE TABLE IF NOT EXISTS email_summaries (
  source_provider TEXT NOT NULL,
  source_id TEXT NOT NULL,
  subject TEXT NOT NULL DEFAULT '',
  sender TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT '',
  message_at TEXT NOT NULL DEFAULT '',
  checked_at TEXT NOT NULL,
  PRIMARY KEY (source_provider, source_id)
);

-- 사용자 계정 (이메일/Google 로그인)
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  email TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL DEFAULT '',
  display_name TEXT NOT NULL DEFAULT '',
  google_sub TEXT NOT NULL DEFAULT '',
  role TEXT NOT NULL DEFAULT 'user',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- 세션 — 토큰 원문이 아닌 SHA-256 해시만 저장
CREATE TABLE IF NOT EXISTS auth_sessions (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_user ON auth_sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_exp ON auth_sessions(expires_at);

-- agent-sessions: 로컬 에이전트 브릿지가 push하는 코딩 에이전트 세션 상태
CREATE TABLE IF NOT EXISTS agent_sessions (
  id TEXT PRIMARY KEY,
  tool TEXT NOT NULL,
  session_key TEXT NOT NULL,
  project TEXT,
  title TEXT,
  status TEXT NOT NULL,
  doing TEXT,
  ask TEXT,
  ended_summary TEXT,
  artifacts TEXT,
  machine TEXT,
  tokens_in INTEGER,
  tokens_out INTEGER,
  last_event_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(tool, session_key)
);
CREATE INDEX IF NOT EXISTS idx_agent_sessions_status ON agent_sessions(status);

-- agent-sessions 이력: 상태 전이 이벤트 (upsert 시 변경분만 적재)
CREATE TABLE IF NOT EXISTS agent_session_events (
  id TEXT PRIMARY KEY,
  tool TEXT NOT NULL,
  session_key TEXT NOT NULL,
  status TEXT NOT NULL,
  project TEXT,
  doing TEXT,
  ask TEXT,
  event_at TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_session_events_key
  ON agent_session_events(tool, session_key, created_at);

-- llm_usage: LLM 호출 토큰 사용량 (MeteredLLM이 기록)
CREATE TABLE IF NOT EXISTS llm_usage (
  id TEXT PRIMARY KEY,
  model TEXT NOT NULL,
  tokens_in INTEGER NOT NULL DEFAULT 0,
  tokens_out INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_llm_usage_created ON llm_usage(created_at);

-- workflow_schedules: 주기적 workflow run 자동 생성
CREATE TABLE IF NOT EXISTS workflow_schedules (
  id TEXT PRIMARY KEY,
  workflow TEXT NOT NULL,
  input TEXT NOT NULL DEFAULT '{}',
  interval_minutes INTEGER NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  last_run_at TEXT,
  next_run_at TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_workflow_schedules_next ON workflow_schedules(next_run_at);

-- inbox_items: 통합 인박스 (notification/session_attention/approval/schedule/report)
CREATE TABLE IF NOT EXISTS inbox_items (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  title TEXT NOT NULL,
  body TEXT NOT NULL DEFAULT '',
  link TEXT NOT NULL DEFAULT '',
  read INTEGER NOT NULL DEFAULT 0,
  dedupe_key TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_inbox_items_created ON inbox_items(created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_inbox_items_dedupe
  ON inbox_items(dedupe_key) WHERE dedupe_key != '';

-- session_activity: 에이전트 세션 활동 스냅샷 (리플레이용, 7일 보관)
CREATE TABLE IF NOT EXISTS session_activity (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tool TEXT NOT NULL,
  session_key TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT '',
  doing TEXT,
  project TEXT,
  recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_session_activity_key
  ON session_activity(tool, session_key, recorded_at);

-- reports: 생성된 daily/weekly 리포트
CREATE TABLE IF NOT EXISTS reports (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  content TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reports_kind ON reports(kind, created_at);
"""

# PostgreSQL 스키마 — SCHEMA과 동일 구조, PG 타입/시퀀스로 변환
SCHEMA_PG = """
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  workflow_key TEXT NOT NULL,
  workflow_version TEXT NOT NULL,
  input TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL,
  trace_id TEXT NOT NULL,
  error TEXT NOT NULL DEFAULT '',
  metadata TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);

CREATE TABLE IF NOT EXISTS stage_runs (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id),
  stage_key TEXT NOT NULL,
  attempt INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL,
  input TEXT NOT NULL DEFAULT '{}',
  output TEXT NOT NULL DEFAULT '{}',
  error TEXT NOT NULL DEFAULT '',
  model TEXT NOT NULL DEFAULT '',
  prompt_version TEXT NOT NULL DEFAULT '',
  tokens_in INTEGER NOT NULL DEFAULT 0,
  tokens_out INTEGER NOT NULL DEFAULT 0,
  started_at TEXT,
  finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_stage_runs_run ON stage_runs(run_id);

CREATE TABLE IF NOT EXISTS approvals (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id),
  stage_key TEXT NOT NULL,
  decision TEXT NOT NULL,
  comment TEXT NOT NULL DEFAULT '',
  decided_by TEXT NOT NULL DEFAULT 'local',
  decided_at TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS audit_events (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  object_ref TEXT NOT NULL DEFAULT '',
  detail TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_run ON audit_events(run_id);

CREATE TABLE IF NOT EXISTS event_links (
  id TEXT PRIMARY KEY,
  idempotency_key TEXT NOT NULL UNIQUE,
  gmail_message_id TEXT NOT NULL,
  connection_id TEXT NOT NULL DEFAULT 'default',
  status TEXT NOT NULL,
  calendar_event_id TEXT NOT NULL DEFAULT '',
  notification_id TEXT NOT NULL DEFAULT '',
  source_url TEXT NOT NULL DEFAULT '',
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_links_msg ON event_links(gmail_message_id);

CREATE TABLE IF NOT EXISTS notifications (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL DEFAULT '',
  link_id TEXT NOT NULL DEFAULT '',
  channel TEXT NOT NULL DEFAULT 'inapp',
  title TEXT NOT NULL,
  body TEXT NOT NULL DEFAULT '',
  source_url TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  sent_at TEXT
);

CREATE TABLE IF NOT EXISTS automation_state (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS message_extractions (
  source_provider TEXT NOT NULL,
  source_id TEXT NOT NULL,
  thread_id TEXT NOT NULL DEFAULT '',
  extraction_json TEXT NOT NULL DEFAULT '{}',
  processing_status TEXT NOT NULL DEFAULT 'pending',
  error_message TEXT NOT NULL DEFAULT '',
  attempts INTEGER NOT NULL DEFAULT 0,
  retry_at TEXT NOT NULL DEFAULT '',
  source_text TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  PRIMARY KEY (source_provider, source_id)
);

CREATE TABLE IF NOT EXISTS scheduled_events (
  id TEXT PRIMARY KEY,
  source_provider TEXT NOT NULL,
  source_id TEXT NOT NULL,
  thread_id TEXT NOT NULL DEFAULT '',
  calendar_event_id TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL,
  start_at TEXT NOT NULL,
  end_at TEXT NOT NULL,
  timezone TEXT NOT NULL DEFAULT 'Asia/Seoul',
  location TEXT NOT NULL DEFAULT '',
  attendees_json TEXT NOT NULL DEFAULT '[]',
  summary TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'candidate',
  version INTEGER NOT NULL DEFAULT 1,
  confidence DOUBLE PRECISION NOT NULL DEFAULT 0.0,
  reason TEXT NOT NULL DEFAULT '',
  source_refs TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scheduled_events_status ON scheduled_events(status);
CREATE INDEX IF NOT EXISTS idx_scheduled_events_source
  ON scheduled_events(source_provider, source_id);

CREATE TABLE IF NOT EXISTS notification_jobs (
  id TEXT PRIMARY KEY,
  scheduled_event_id TEXT NOT NULL,
  notify_type TEXT NOT NULL,
  fire_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  delivered_at TEXT,
  error_message TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notification_jobs_fire ON notification_jobs(fire_at);

CREATE TABLE IF NOT EXISTS radar_snapshots (
  id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
  platform TEXT NOT NULL,
  collected_date TEXT NOT NULL,
  metrics_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  UNIQUE(platform, collected_date)
);

CREATE TABLE IF NOT EXISTS radar_items (
  id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
  snapshot_id BIGINT NOT NULL REFERENCES radar_snapshots(id),
  rank INTEGER NOT NULL,
  repo_full_name TEXT NOT NULL,
  repo_url TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',
  stars INTEGER NOT NULL DEFAULT 0,
  language TEXT NOT NULL DEFAULT '',
  topics_json TEXT NOT NULL DEFAULT '[]',
  readme_excerpt TEXT NOT NULL DEFAULT '',
  summary_en TEXT NOT NULL DEFAULT '',
  summary_zh TEXT NOT NULL DEFAULT '',
  summary_ko TEXT NOT NULL DEFAULT '',
  content_hash TEXT NOT NULL DEFAULT '',
  stars_today INTEGER,
  license TEXT NOT NULL DEFAULT '',
  taxonomy TEXT NOT NULL DEFAULT '',
  taxonomy_confidence DOUBLE PRECISION NOT NULL DEFAULT 0.0,
  rank_score DOUBLE PRECISION NOT NULL DEFAULT 0.0,
  summary_status TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_radar_items_snapshot ON radar_items(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_radar_items_repo ON radar_items(repo_full_name, content_hash);

CREATE TABLE IF NOT EXISTS push_subscriptions (
  endpoint TEXT PRIMARY KEY,
  keys_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS email_summaries (
  source_provider TEXT NOT NULL,
  source_id TEXT NOT NULL,
  subject TEXT NOT NULL DEFAULT '',
  sender TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT '',
  message_at TEXT NOT NULL DEFAULT '',
  checked_at TEXT NOT NULL,
  PRIMARY KEY (source_provider, source_id)
);

CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  email TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL DEFAULT '',
  display_name TEXT NOT NULL DEFAULT '',
  google_sub TEXT NOT NULL DEFAULT '',
  role TEXT NOT NULL DEFAULT 'user',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS auth_sessions (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_user ON auth_sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_exp ON auth_sessions(expires_at);

CREATE TABLE IF NOT EXISTS agent_sessions (
  id TEXT PRIMARY KEY,
  tool TEXT NOT NULL,
  session_key TEXT NOT NULL,
  project TEXT,
  title TEXT,
  status TEXT NOT NULL,
  doing TEXT,
  ask TEXT,
  ended_summary TEXT,
  artifacts TEXT,
  machine TEXT,
  tokens_in INTEGER,
  tokens_out INTEGER,
  last_event_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(tool, session_key)
);
CREATE INDEX IF NOT EXISTS idx_agent_sessions_status ON agent_sessions(status);

CREATE TABLE IF NOT EXISTS agent_session_events (
  id TEXT PRIMARY KEY,
  tool TEXT NOT NULL,
  session_key TEXT NOT NULL,
  status TEXT NOT NULL,
  project TEXT,
  doing TEXT,
  ask TEXT,
  event_at TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_session_events_key
  ON agent_session_events(tool, session_key, created_at);

-- llm_usage: LLM 호출 토큰 사용량 (MeteredLLM이 기록)
CREATE TABLE IF NOT EXISTS llm_usage (
  id TEXT PRIMARY KEY,
  model TEXT NOT NULL,
  tokens_in INTEGER NOT NULL DEFAULT 0,
  tokens_out INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_llm_usage_created ON llm_usage(created_at);

-- workflow_schedules: 주기적 workflow run 자동 생성
CREATE TABLE IF NOT EXISTS workflow_schedules (
  id TEXT PRIMARY KEY,
  workflow TEXT NOT NULL,
  input TEXT NOT NULL DEFAULT '{}',
  interval_minutes INTEGER NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  last_run_at TEXT,
  next_run_at TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_workflow_schedules_next ON workflow_schedules(next_run_at);

CREATE TABLE IF NOT EXISTS inbox_items (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  title TEXT NOT NULL,
  body TEXT NOT NULL DEFAULT '',
  link TEXT NOT NULL DEFAULT '',
  read INTEGER NOT NULL DEFAULT 0,
  dedupe_key TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_inbox_items_created ON inbox_items(created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_inbox_items_dedupe
  ON inbox_items(dedupe_key) WHERE dedupe_key != '';

CREATE TABLE IF NOT EXISTS session_activity (
  id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
  tool TEXT NOT NULL,
  session_key TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT '',
  doing TEXT,
  project TEXT,
  recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_session_activity_key
  ON session_activity(tool, session_key, recorded_at);

CREATE TABLE IF NOT EXISTS reports (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  content TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reports_kind ON reports(kind, created_at);
"""

_PG_DSN_PREFIXES = ("postgres://", "postgresql://")


def _is_pg_dsn(path: str) -> bool:
    return path.startswith(_PG_DSN_PREFIXES)


def _pg_translate(sql: str) -> str:
    """SQLite 방언 → PostgreSQL 방언 변환 (? 플레이스홀더, INSERT OR IGNORE)."""
    sql = sql.replace("?", "%s")
    if "INSERT OR IGNORE INTO" in sql:
        sql = sql.replace("INSERT OR IGNORE INTO", "INSERT INTO", 1)
        if " RETURNING" in sql:
            sql = sql.replace(" RETURNING", " ON CONFLICT DO NOTHING RETURNING", 1)
        else:
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


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


def _loads(s: str) -> dict[str, Any]:
    try:
        v = json.loads(s)
        return v if isinstance(v, dict) else {}
    except json.JSONDecodeError:
        return {}


class Store:
    """동기 store. path가 postgres(ql):// DSN이면 PostgreSQL, 아니면 SQLite 파일."""

    def __init__(self, path: str | Path) -> None:
        dsn = str(path)
        self._pg = _is_pg_dsn(dsn)
        self._integrity_errors: tuple[type[Exception], ...] = (
            (psycopg.errors.IntegrityError,) if self._pg else (sqlite3.IntegrityError,)
        )
        self._lock = threading.Lock()
        if self._pg:
            self._conn: Any = _PGConn(dsn)
            for stmt in SCHEMA_PG.split(";"):
                if stmt.strip():
                    self._conn.execute(stmt)
        else:
            self._path = Path(path)
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # CPython #118172: shared statement caches can mix concurrent query results.
            self._conn = sqlite3.connect(
                str(self._path), check_same_thread=False, cached_statements=0
            )
            self._conn.row_factory = sqlite3.Row
            with self._conn:
                self._conn.executescript(SCHEMA)
        self._migrate_agent_session_columns()
        self._migrate_approval_columns()
        self._migrate_user_columns()
        # DSN/경로 원문은 자격증명을 포함할 수 있으므로 백엔드 종류만 기록한다
        logger.info("store opened backend=%s", "postgresql" if self._pg else "sqlite")

    def _ensure_columns(self, table: str, wanted: dict[str, str]) -> None:
        """CREATE TABLE IF NOT EXISTS는 기존 테이블을 변경하지 않으므로
        나중에 추가된 컬럼은 ALTER로 보강한다.
        wanted: {컬럼명: 컬럼 DDL} — 테이블/컬럼명은 내부 상수만 사용."""
        try:
            if self._pg:
                rows = self._conn.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_name=?",
                    (table,),
                ).fetchall()
                have = {r["column_name"] for r in rows}
            else:
                rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
                have = {r["name"] for r in rows}
        except Exception as e:
            logger.warning("%s column check failed: %s", table, e)
            return
        for col, ddl in sorted(wanted.items()):
            if col in have:
                continue
            try:
                if self._pg:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {ddl}")
                else:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
                logger.info("%s column added: %s", table, col)
            except Exception as e:
                logger.warning("%s add column %s failed: %s", table, col, e)

    def _migrate_agent_session_columns(self) -> None:
        """기존 DB의 agent_sessions에 신규 컬럼 보강."""
        self._ensure_columns(
            "agent_sessions",
            {
                "ended_summary": "TEXT",
                "artifacts": "TEXT",
                "machine": "TEXT",
                "tokens_in": "INTEGER",
                "tokens_out": "INTEGER",
            },
        )

    def _migrate_approval_columns(self) -> None:
        """기존 DB의 approvals에 created_at 컬럼 보강."""
        self._ensure_columns("approvals", {"created_at": "TEXT"})

    def _migrate_user_columns(self) -> None:
        """기존 DB의 users에 role 컬럼 보강 (admin|user, 기본 user)."""
        self._ensure_columns("users", {"role": "TEXT NOT NULL DEFAULT 'user'"})

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def tx(self) -> Iterator[Any]:
        with self._lock, self._conn:
            yield self._conn

    # ---- runs ----
    def create_run(
        self,
        workflow_key: str,
        version: str,
        input_data: dict[str, Any],
        trace_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> Run:
        run = Run(
            id=uuid.uuid4().hex[:12],
            workflow_key=workflow_key,
            workflow_version=version,
            input=input_data,
            status=RunStatus.QUEUED,
            trace_id=trace_id,
            created_at=_now(),
            updated_at=_now(),
            metadata=metadata or {},
        )
        with self.tx() as c:
            c.execute(
                "INSERT INTO runs(id,workflow_key,workflow_version,input,status,trace_id,"
                "error,metadata,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    run.id,
                    run.workflow_key,
                    run.workflow_version,
                    json.dumps(run.input),
                    run.status.value,
                    run.trace_id,
                    run.error,
                    json.dumps(run.metadata),
                    _iso(run.created_at),
                    _iso(run.updated_at),
                ),
            )
        self.audit(
            run.id, "system", "run_created", run.id, {"workflow": workflow_key, "version": version}
        )
        return run

    def get_run(self, run_id: str) -> Run | None:
        row = self._conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return self._row_to_run(row) if row else None

    def list_runs(self, limit: int = 50, workflow: str | None = None) -> list[Run]:
        if workflow is None:
            rows = self._conn.execute(
                "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM runs WHERE workflow_key=? ORDER BY created_at DESC LIMIT ?",
                (workflow, limit),
            ).fetchall()
        return [self._row_to_run(r) for r in rows]

    def update_run(
        self,
        run_id: str,
        *,
        status: RunStatus | None = None,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        run = self.get_run(run_id)
        if run is None:
            return
        with self.tx() as c:
            c.execute(
                "UPDATE runs SET status=?, error=?, metadata=?, updated_at=? WHERE id=?",
                (
                    (status or run.status).value,
                    error if error is not None else run.error,
                    json.dumps(metadata if metadata is not None else run.metadata),
                    _iso(_now()),
                    run_id,
                ),
            )

    def _row_to_run(self, row: Any) -> Run:
        return Run(
            id=row["id"],
            workflow_key=row["workflow_key"],
            workflow_version=row["workflow_version"],
            input=_loads(row["input"]),
            status=RunStatus(row["status"]),
            trace_id=row["trace_id"],
            error=row["error"],
            metadata=_loads(row["metadata"]),
            created_at=_dt(row["created_at"]) or _now(),
            updated_at=_dt(row["updated_at"]) or _now(),
        )

    # ---- stage_runs ----
    def start_stage(
        self,
        run_id: str,
        stage_key: str,
        attempt: int,
        input_data: dict[str, Any],
        model: str = "",
        prompt_version: str = "",
    ) -> StageRun:
        sr = StageRun(
            id=uuid.uuid4().hex[:12],
            run_id=run_id,
            stage_key=stage_key,
            attempt=attempt,
            status=StageStatus.RUNNING,
            input=input_data,
            model=model,
            prompt_version=prompt_version,
            started_at=_now(),
        )
        with self.tx() as c:
            c.execute(
                "INSERT INTO stage_runs(id,run_id,stage_key,attempt,status,input,model,"
                "prompt_version,started_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    sr.id,
                    sr.run_id,
                    sr.stage_key,
                    sr.attempt,
                    sr.status.value,
                    json.dumps(input_data),
                    model,
                    prompt_version,
                    _iso(sr.started_at),
                ),
            )
        self.audit(
            run_id,
            "system",
            "stage_start",
            f"{stage_key}#{attempt}",
            {"stage": stage_key, "attempt": attempt},
        )
        return sr

    def finish_stage(
        self,
        stage_run_id: str,
        *,
        status: StageStatus | str,
        output: dict[str, Any] | None = None,
        error: str = "",
        tokens_in: int = 0,
        tokens_out: int = 0,
    ) -> None:
        status_enum = StageStatus(status)
        with self.tx() as c:
            c.execute(
                "UPDATE stage_runs SET status=?, output=?, error=?, tokens_in=?,"
                "tokens_out=?, finished_at=? WHERE id=?",
                (
                    status_enum.value,
                    json.dumps(output or {}),
                    error,
                    tokens_in,
                    tokens_out,
                    _iso(_now()),
                    stage_run_id,
                ),
            )
        if status_enum == StageStatus.SUCCEEDED:
            row = self._conn.execute(
                "SELECT run_id, stage_key, attempt FROM stage_runs WHERE id=?", (stage_run_id,)
            ).fetchone()
            if row:
                self.audit(
                    row["run_id"],
                    "system",
                    "stage_done",
                    f"{row['stage_key']}#{row['attempt']}",
                    {"status": status_enum.value},
                )

    def get_stage_runs(self, run_id: str) -> list[StageRun]:
        rows = self._conn.execute(
            "SELECT * FROM stage_runs WHERE run_id=? ORDER BY started_at, attempt", (run_id,)
        ).fetchall()
        return [self._row_to_stage(r) for r in rows]

    def latest_stage(self, run_id: str, stage_key: str) -> StageRun | None:
        rows = self.get_stage_runs(run_id)
        matched = [r for r in rows if r.stage_key == stage_key]
        return matched[-1] if matched else None

    def _row_to_stage(self, row: Any) -> StageRun:
        return StageRun(
            id=row["id"],
            run_id=row["run_id"],
            stage_key=row["stage_key"],
            attempt=row["attempt"],
            status=StageStatus(row["status"]),
            input=_loads(row["input"]),
            output=_loads(row["output"]),
            error=row["error"],
            model=row["model"],
            prompt_version=row["prompt_version"],
            tokens_in=row["tokens_in"],
            tokens_out=row["tokens_out"],
            started_at=_dt(row["started_at"]),
            finished_at=_dt(row["finished_at"]),
        )

    # ---- approvals ----
    def add_approval(
        self,
        run_id: str,
        stage_key: str,
        decision: Literal["approve", "reject"],
        comment: str = "",
        decided_by: str = "local",
    ) -> Approval:
        now = _now()
        ap = Approval(
            id=uuid.uuid4().hex[:12],
            run_id=run_id,
            stage_key=stage_key,
            decision=decision,
            comment=comment,
            decided_by=decided_by,
            decided_at=now,
            created_at=now,
        )
        with self.tx() as c:
            c.execute(
                "INSERT INTO approvals(id,run_id,stage_key,decision,comment,"
                "decided_by,decided_at,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    ap.id,
                    ap.run_id,
                    ap.stage_key,
                    ap.decision,
                    ap.comment,
                    ap.decided_by,
                    _iso(ap.decided_at),
                    _iso(ap.created_at),
                ),
            )
        self.audit(
            run_id, decided_by, f"approval_{decision}", stage_key, {"comment": comment[:200]}
        )
        return ap

    def get_approvals(self, run_id: str) -> list[Approval]:
        rows = self._conn.execute(
            "SELECT * FROM approvals WHERE run_id=? ORDER BY decided_at", (run_id,)
        ).fetchall()
        return [
            Approval(
                id=r["id"],
                run_id=r["run_id"],
                stage_key=r["stage_key"],
                decision=r["decision"],
                comment=r["comment"],
                decided_by=r["decided_by"],
                decided_at=_dt(r["decided_at"]) or _now(),
                created_at=_dt(r["created_at"]) or _dt(r["decided_at"]),
            )
            for r in rows
        ]

    # ---- audit ----
    def audit(
        self,
        run_id: str,
        actor: str,
        action: str,
        object_ref: str = "",
        detail: dict[str, Any] | None = None,
    ) -> None:
        """본문/토큰이 detail에 들어가지 않도록 호출자가 책임진다."""
        with self.tx() as c:
            c.execute(
                "INSERT INTO audit_events(id,run_id,actor,action,object_ref,detail,created_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (
                    uuid.uuid4().hex[:12],
                    run_id,
                    actor,
                    action,
                    object_ref,
                    json.dumps(detail or {}),
                    _iso(_now()),
                ),
            )

    def get_audit(self, run_id: str) -> list[AuditEvent]:
        rows = self._conn.execute(
            "SELECT * FROM audit_events WHERE run_id=? ORDER BY created_at", (run_id,)
        ).fetchall()
        return [
            AuditEvent(
                id=r["id"],
                run_id=r["run_id"],
                actor=r["actor"],
                action=r["action"],
                object_ref=r["object_ref"],
                detail=_loads(r["detail"]),
                created_at=_dt(r["created_at"]) or _now(),
            )
            for r in rows
        ]

    # ---- event_links (gmail_calendar 멱등) ----
    def reserve_link(
        self, idempotency_key: str, gmail_message_id: str, connection_id: str, source_url: str
    ) -> EventLink | None:
        """멱등 키로 pending 링크 예약. 이미 있으면 None 반환."""
        now = _now()
        link = EventLink(
            id=uuid.uuid4().hex[:12],
            idempotency_key=idempotency_key,
            gmail_message_id=gmail_message_id,
            connection_id=connection_id,
            status=LinkStatus.DISCOVERED,
            source_url=source_url,
            created_at=now,
            updated_at=now,
        )
        try:
            with self.tx() as c:
                c.execute(
                    "INSERT INTO event_links(id,idempotency_key,gmail_message_id,"
                    "connection_id,status,source_url,created_at,updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (
                        link.id,
                        link.idempotency_key,
                        link.gmail_message_id,
                        link.connection_id,
                        link.status.value,
                        link.source_url,
                        _iso(link.created_at),
                        _iso(link.updated_at),
                    ),
                )
        except self._integrity_errors:
            return None
        return link

    def get_link_by_key(self, key: str) -> EventLink | None:
        row = self._conn.execute(
            "SELECT * FROM event_links WHERE idempotency_key=?", (key,)
        ).fetchone()
        return self._row_to_link(row) if row else None

    def get_link_by_message(self, message_id: str) -> EventLink | None:
        row = self._conn.execute(
            "SELECT * FROM event_links WHERE gmail_message_id=? ORDER BY created_at DESC LIMIT 1",
            (message_id,),
        ).fetchone()
        return self._row_to_link(row) if row else None

    def update_link(
        self,
        link_id: str,
        *,
        status: LinkStatus | None = None,
        calendar_event_id: str | None = None,
        notification_id: str | None = None,
        last_error: str | None = None,
        attempts_inc: bool = False,
    ) -> None:
        link = self._conn.execute("SELECT * FROM event_links WHERE id=?", (link_id,)).fetchone()
        if not link:
            return
        with self.tx() as c:
            c.execute(
                "UPDATE event_links SET status=?, calendar_event_id=?,"
                "notification_id=?, last_error=?, attempts=attempts+?, updated_at=?"
                " WHERE id=?",
                (
                    (status or LinkStatus(link["status"])).value,
                    calendar_event_id
                    if calendar_event_id is not None
                    else link["calendar_event_id"],
                    notification_id if notification_id is not None else link["notification_id"],
                    last_error if last_error is not None else link["last_error"],
                    1 if attempts_inc else 0,
                    _iso(_now()),
                    link_id,
                ),
            )

    def list_links(self, limit: int = 50) -> list[EventLink]:
        rows = self._conn.execute(
            "SELECT * FROM event_links ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [self._row_to_link(r) for r in rows]

    def _row_to_link(self, row: Any) -> EventLink:
        return EventLink(
            id=row["id"],
            idempotency_key=row["idempotency_key"],
            gmail_message_id=row["gmail_message_id"],
            connection_id=row["connection_id"],
            status=LinkStatus(row["status"]),
            calendar_event_id=row["calendar_event_id"],
            notification_id=row["notification_id"],
            source_url=row["source_url"],
            attempts=row["attempts"],
            last_error=row["last_error"],
            created_at=_dt(row["created_at"]) or _now(),
            updated_at=_dt(row["updated_at"]) or _now(),
        )

    # ---- notifications ----
    def add_notification(
        self,
        title: str,
        body: str = "",
        source_url: str = "",
        run_id: str = "",
        link_id: str = "",
        channel: str = "inapp",
    ) -> Notification:
        n = Notification(
            id=uuid.uuid4().hex[:12],
            run_id=run_id,
            link_id=link_id,
            channel=channel,
            title=title,
            body=body,
            source_url=source_url,
            status=NotificationStatus.PENDING,
            created_at=_now(),
        )
        with self.tx() as c:
            c.execute(
                "INSERT INTO notifications(id,run_id,link_id,channel,title,body,source_url,"
                "status,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    n.id,
                    n.run_id,
                    n.link_id,
                    n.channel,
                    n.title,
                    n.body,
                    n.source_url,
                    n.status.value,
                    _iso(n.created_at),
                ),
            )
        return n

    def set_notification_status(self, notif_id: str, status: NotificationStatus) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE notifications SET status=?, sent_at=? WHERE id=?",
                (
                    status.value,
                    _iso(_now()) if status == NotificationStatus.SENT else None,
                    notif_id,
                ),
            )

    def list_notifications(self, limit: int = 50) -> list[Notification]:
        rows = self._conn.execute(
            "SELECT * FROM notifications ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [self._row_to_notif(r) for r in rows]

    def _row_to_notif(self, row: Any) -> Notification:
        return Notification(
            id=row["id"],
            run_id=row["run_id"],
            link_id=row["link_id"],
            channel=row["channel"],
            title=row["title"],
            body=row["body"],
            source_url=row["source_url"],
            status=NotificationStatus(row["status"]),
            created_at=_dt(row["created_at"]) or _now(),
            sent_at=_dt(row["sent_at"]),
        )

    # ---- automation state ----
    def get_automation(self) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT value FROM automation_state WHERE key='gmail_calendar'"
        ).fetchone()
        return _loads(row["value"]) if row else {"enabled": False}

    def set_automation(self, data: dict[str, Any]) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO automation_state(key,value) VALUES('gmail_calendar',?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (json.dumps(data),),
            )

    # ---- 범용 상태 (automation_state key/value) ----
    def get_state(self, key: str, default: dict[str, Any] | None = None) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT value FROM automation_state WHERE key=?", (key,)
        ).fetchone()
        return _loads(row["value"]) if row else (default or {})

    def set_state(self, key: str, data: dict[str, Any]) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO automation_state(key,value) VALUES(?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(data)),
            )

    # ---- message_extractions (멀티채널 수신함) ----
    def save_message_extraction(
        self,
        provider: str,
        source_id: str,
        thread_id: str,
        extraction: dict[str, Any],
        status: str,
        source_text: str = "",
    ) -> bool:
        """INSERT OR IGNORE — 재전송이면 False."""
        now = _now()
        with self.tx() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO message_extractions"
                "(source_provider,source_id,thread_id,extraction_json,"
                "processing_status,source_text,created_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (
                    provider,
                    source_id,
                    thread_id,
                    json.dumps(extraction),
                    status,
                    source_text,
                    _iso(now),
                ),
            )
            return cur.rowcount > 0

    def has_message(self, provider: str, source_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM message_extractions WHERE source_provider=? AND source_id=?",
            (provider, source_id),
        ).fetchone()
        return row is not None

    def pending_messages(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM message_extractions WHERE processing_status='pending'"
            " AND retry_at <= ? ORDER BY created_at LIMIT ?",
            (_iso(_now()), limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def finish_message(
        self,
        provider: str,
        source_id: str,
        status: str,
        error: str = "",
        retry_at: str = "",
        attempts: int = 0,
    ) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE message_extractions SET processing_status=?,"
                " error_message=?, retry_at=?, attempts=?"
                " WHERE source_provider=? AND source_id=?",
                (status, error, retry_at, attempts, provider, source_id),
            )

    def list_message_extractions(
        self, provider: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        q = "SELECT * FROM message_extractions"
        args: list[Any] = []
        if provider:
            q += " WHERE source_provider=?"
            args.append(provider)
        q += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        out = []
        for r in self._conn.execute(q, args).fetchall():
            d = dict(r)
            d["extraction"] = _loads(d.pop("extraction_json"))
            out.append(d)
        return out

    # ---- scheduled_events ----
    def save_scheduled_event(
        self,
        *,
        event_id: str,
        source_provider: str,
        source_id: str,
        thread_id: str = "",
        calendar_event_id: str = "",
        title: str,
        start_at: str,
        end_at: str,
        timezone: str,
        location: str = "",
        attendees_json: str = "[]",
        summary: str = "",
        status: str = "candidate",
        confidence: float = 0.0,
        reason: str = "",
        source_refs: str = "",
    ) -> str:
        now = _iso(_now())
        with self.tx() as c:
            c.execute(
                "INSERT INTO scheduled_events(id,source_provider,source_id,"
                "thread_id,calendar_event_id,title,start_at,end_at,timezone,"
                "location,attendees_json,summary,status,confidence,reason,"
                "source_refs,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET status=excluded.status,"
                " calendar_event_id=excluded.calendar_event_id,"
                " version=version+1, updated_at=excluded.updated_at",
                (
                    event_id,
                    source_provider,
                    source_id,
                    thread_id,
                    calendar_event_id,
                    title,
                    start_at,
                    end_at,
                    timezone,
                    location,
                    attendees_json,
                    summary,
                    status,
                    confidence,
                    reason,
                    source_refs,
                    now,
                    now,
                ),
            )
        return event_id

    def get_scheduled_by_source(self, provider: str, source_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM scheduled_events WHERE source_provider=? AND source_id=?",
            (provider, source_id),
        ).fetchone()
        return dict(row) if row else None

    def list_scheduled_events(
        self, status: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        q = "SELECT * FROM scheduled_events"
        args: list[Any] = []
        if status:
            q += " WHERE status=?"
            args.append(status)
        q += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self._conn.execute(q, args).fetchall()]

    _SCHEDULED_EVENT_FIELDS = (
        "title",
        "start_at",
        "end_at",
        "timezone",
        "location",
        "attendees_json",
        "summary",
        "status",
        "confidence",
        "reason",
        "calendar_event_id",
        "source_refs",
    )

    def get_scheduled_event(self, event_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM scheduled_events WHERE id=?", (event_id,)
        ).fetchone()
        return dict(row) if row else None

    def update_scheduled_event(self, event_id: str, **fields: Any) -> bool:
        sets = {k: v for k, v in fields.items() if k in self._SCHEDULED_EVENT_FIELDS}
        if not sets:
            return False
        sets["updated_at"] = _iso(_now())
        cols = ", ".join(f"{k}=?" for k in sets)
        with self.tx() as c:
            cur = c.execute(
                f"UPDATE scheduled_events SET {cols}, version=version+1 WHERE id=?",  # noqa: S608 - cols는 화이트리스트 필드명만 사용
                (*sets.values(), event_id),
            )
        return cur.rowcount > 0

    # ---- notification_jobs ----
    def save_notification_job(self, scheduled_event_id: str, notify_type: str, fire_at: str) -> str:
        job_id = f"job:{scheduled_event_id}:{notify_type}:{fire_at}"
        now = _iso(_now())
        with self.tx() as c:
            c.execute(
                "INSERT OR IGNORE INTO notification_jobs"
                "(id,scheduled_event_id,notify_type,fire_at,status,"
                "created_at,updated_at) VALUES(?,?,?,?,'pending',?,?)",
                (job_id, scheduled_event_id, notify_type, fire_at, now, now),
            )
        return job_id

    def due_notification_jobs(self, now: str | None = None) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM notification_jobs WHERE status='pending'"
            " AND fire_at <= ? ORDER BY fire_at",
            (now or _iso(_now()),),
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_job(self, job_id: str, status: str, error: str = "") -> None:
        delivered = _iso(_now()) if status == "delivered" else None
        with self.tx() as c:
            c.execute(
                "UPDATE notification_jobs SET status=?, delivered_at=?,"
                " error_message=?, updated_at=? WHERE id=?",
                (status, delivered, error, _iso(_now()), job_id),
            )

    def list_notification_jobs(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM notification_jobs ORDER BY fire_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- radar (skillRadar) ----
    def radar_snapshot_exists(self, platform: str, date_str: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM radar_snapshots WHERE platform=? AND collected_date=?",
            (platform, date_str),
        ).fetchone()
        return row is not None

    def save_radar_snapshot(self, platform: str, date_str: str, metrics: dict[str, Any]) -> int:
        with self.tx() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO radar_snapshots"
                "(platform,collected_date,metrics_json,created_at)"
                " VALUES(?,?,?,?) RETURNING id",
                (platform, date_str, json.dumps(metrics), _iso(_now())),
            )
            row = cur.fetchone()
            if row:
                return int(row["id"] if isinstance(row, dict) else row[0])
            row = c.execute(
                "SELECT id FROM radar_snapshots WHERE platform=? AND collected_date=?",
                (platform, date_str),
            ).fetchone()
            return int(row["id"])

    def delete_radar_snapshot(self, snapshot_id: int) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM radar_items WHERE snapshot_id=?", (snapshot_id,))
            c.execute("DELETE FROM radar_snapshots WHERE id=?", (snapshot_id,))

    def add_radar_item(self, snapshot_id: int, item: dict[str, Any]) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO radar_items(snapshot_id,rank,repo_full_name,"
                "repo_url,description,stars,language,topics_json,"
                "readme_excerpt,summary_en,summary_zh,summary_ko,"
                "content_hash,stars_today,license,taxonomy,"
                "taxonomy_confidence,rank_score,summary_status)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    snapshot_id,
                    item.get("rank", 0),
                    item["repo_full_name"],
                    item.get("repo_url", ""),
                    item.get("description", ""),
                    item.get("stars", 0),
                    item.get("language", ""),
                    json.dumps(item.get("topics") or []),
                    item.get("readme_excerpt", ""),
                    item.get("summary_en", ""),
                    item.get("summary_zh", ""),
                    item.get("summary_ko", ""),
                    item.get("content_hash", ""),
                    item.get("stars_today"),
                    item.get("license", ""),
                    item.get("taxonomy", ""),
                    item.get("taxonomy_confidence", 0.0),
                    item.get("rank_score", 0.0),
                    item.get("summary_status", ""),
                ),
            )

    def radar_items(self, snapshot_id: int) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM radar_items WHERE snapshot_id=? ORDER BY rank", (snapshot_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def radar_snapshot(self, platform: str, date_str: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM radar_snapshots WHERE platform=? AND collected_date=?",
            (platform, date_str),
        ).fetchone()
        return dict(row) if row else None

    def radar_dates(self, platform: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT collected_date FROM radar_snapshots WHERE platform=?"
            " ORDER BY collected_date DESC",
            (platform,),
        ).fetchall()
        return [r["collected_date"] for r in rows]

    def radar_prev_stars(self, platform: str, date_str: str) -> dict[str, int]:
        row = self._conn.execute(
            "SELECT id FROM radar_snapshots WHERE platform=?"
            " AND collected_date<? ORDER BY collected_date DESC LIMIT 1",
            (platform, date_str),
        ).fetchone()
        if not row:
            return {}
        return {
            r["repo_full_name"]: r["stars"]
            for r in self._conn.execute(
                "SELECT repo_full_name, stars FROM radar_items WHERE snapshot_id=?", (row["id"],)
            ).fetchall()
        }

    def radar_cached_repo(self, full_name: str, chash: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT summary_en,summary_zh,summary_ko,taxonomy,"
            "taxonomy_confidence,summary_status FROM radar_items"
            " WHERE repo_full_name=? AND content_hash=?"
            " AND summary_en != '' ORDER BY id DESC LIMIT 1",
            (full_name, chash),
        ).fetchone()
        return dict(row) if row else None

    def radar_items_since(self, since_date: str) -> list[dict[str, Any]]:
        """since_date 이후 수집분의 item 행 + platform/collected_date (토픽·랭킹·digest용)."""
        rows = self._conn.execute(
            "SELECT i.*, s.platform AS platform, s.collected_date AS collected_date"
            " FROM radar_items i JOIN radar_snapshots s ON i.snapshot_id = s.id"
            " WHERE s.collected_date >= ?",
            (since_date,),
        ).fetchall()
        return [dict(r) for r in rows]

    def radar_repo_names_before(self, date_str: str) -> set[str]:
        """date_str 이전에 한 번이라도 수집된 repo_full_name 집합."""
        rows = self._conn.execute(
            "SELECT DISTINCT i.repo_full_name FROM radar_items i"
            " JOIN radar_snapshots s ON i.snapshot_id = s.id"
            " WHERE s.collected_date < ?",
            (date_str,),
        ).fetchall()
        return {r["repo_full_name"] for r in rows}

    def radar_repo_latest(self, full_name: str) -> dict[str, Any] | None:
        """repo의 가장 최근 수집 item + platform/collected_date."""
        row = self._conn.execute(
            "SELECT i.*, s.platform AS platform, s.collected_date AS collected_date"
            " FROM radar_items i JOIN radar_snapshots s ON i.snapshot_id = s.id"
            " WHERE i.repo_full_name = ?"
            " ORDER BY s.collected_date DESC, i.rank ASC LIMIT 1",
            (full_name,),
        ).fetchone()
        return dict(row) if row else None

    # ---- push_subscriptions ----
    def save_push_subscription(self, endpoint: str, keys: dict[str, Any]) -> None:
        from ..security import encrypt_str

        with self.tx() as c:
            c.execute(
                "INSERT INTO push_subscriptions(endpoint,keys_json,created_at)"
                " VALUES(?,?,?) ON CONFLICT(endpoint) DO UPDATE SET"
                " keys_json=excluded.keys_json",
                (endpoint, encrypt_str(json.dumps(keys)), _iso(_now())),
            )

    def delete_push_subscription(self, endpoint: str) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM push_subscriptions WHERE endpoint=?", (endpoint,))

    def list_push_subscriptions(self) -> list[dict[str, Any]]:
        from ..security import decrypt_str

        rows = self._conn.execute("SELECT * FROM push_subscriptions").fetchall()
        return [
            {"endpoint": r["endpoint"], "keys": _loads(decrypt_str(r["keys_json"]))} for r in rows
        ]

    # ---- users / auth_sessions ----
    @staticmethod
    def _user_json(r: Any) -> dict[str, Any]:
        return {
            "id": r["id"],
            "email": r["email"],
            "display_name": r["display_name"],
            "google_sub": r["google_sub"],
            "role": r["role"] if "role" in r.keys() else "user",
            "created_at": r["created_at"],
        }

    def create_user(
        self,
        email: str,
        password_hash: str = "",
        display_name: str = "",
        google_sub: str = "",
    ) -> dict[str, Any] | None:
        """신규 사용자 생성. email 중복 시 None."""
        uid = uuid.uuid4().hex[:16]
        now = _iso(_now())
        try:
            with self.tx() as c:
                c.execute(
                    "INSERT INTO users(id,email,password_hash,display_name,google_sub,"
                    "created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                    (uid, email.lower(), password_hash, display_name, google_sub, now, now),
                )
        except self._integrity_errors:
            return None
        return self.get_user_by_email(email)

    def get_user_by_email(self, email: str) -> dict[str, Any] | None:
        r = self._conn.execute("SELECT * FROM users WHERE email=?", (email.lower(),)).fetchone()
        return self._user_json(r) if r else None

    def get_user_by_id(self, uid: str) -> dict[str, Any] | None:
        r = self._conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        return self._user_json(r) if r else None

    def get_user_by_google_sub(self, sub: str) -> dict[str, Any] | None:
        r = self._conn.execute("SELECT * FROM users WHERE google_sub=?", (sub,)).fetchone()
        return self._user_json(r) if r else None

    def link_google_sub(self, uid: str, google_sub: str) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE users SET google_sub=?, updated_at=? WHERE id=?",
                (google_sub, _iso(_now()), uid),
            )

    def get_user_password_hash(self, email: str) -> tuple[str, str] | None:
        """(user_id, password_hash) — 로그인 검증용. 비밀번호 계정만."""
        r = self._conn.execute(
            "SELECT id, password_hash FROM users WHERE email=?", (email.lower(),)
        ).fetchone()
        return (r["id"], r["password_hash"]) if r else None

    def create_session(self, user_id: str, token_hash: str, ttl_days: int = 30) -> None:
        """세션 생성 — id는 토큰의 SHA-256 해시(원문 토큰은 저장하지 않음)."""
        now = _now()
        exp = now + timedelta(days=ttl_days)
        with self.tx() as c:
            c.execute(
                "INSERT INTO auth_sessions(id,user_id,created_at,expires_at) VALUES(?,?,?,?)",
                (token_hash, user_id, _iso(now), _iso(exp)),
            )

    def get_session_user(self, token_hash: str) -> dict[str, Any] | None:
        """유효 세션의 사용자 반환. 만료/폐기 세션은 None."""
        r = self._conn.execute(
            "SELECT u.id, u.email, u.display_name, u.google_sub, u.role, u.created_at,"
            " s.expires_at"
            " FROM auth_sessions s JOIN users u ON u.id = s.user_id"
            " WHERE s.id=? AND s.revoked_at IS NULL",
            (token_hash,),
        ).fetchone()
        if not r:
            return None
        exp = _dt(r["expires_at"])
        if exp is None or exp <= _now():
            return None
        return self._user_json(r)

    def revoke_session(self, token_hash: str) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE auth_sessions SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
                (_iso(_now()), token_hash),
            )

    # ---- email_summaries (3줄 요약 카드) ----
    def save_email_summary(
        self,
        provider: str,
        source_id: str,
        *,
        subject: str,
        sender: str,
        summary: str,
        message_at: str = "",
    ) -> None:
        from ..security import encrypt_str

        with self.tx() as c:
            c.execute(
                "INSERT INTO email_summaries(source_provider,source_id,"
                "subject,sender,summary,message_at,checked_at)"
                " VALUES(?,?,?,?,?,?,?)"
                " ON CONFLICT(source_provider,source_id) DO UPDATE SET"
                " subject=excluded.subject, sender=excluded.sender,"
                " summary=excluded.summary, message_at=excluded.message_at,"
                " checked_at=excluded.checked_at",
                (
                    provider,
                    source_id,
                    encrypt_str(subject),
                    encrypt_str(sender),
                    encrypt_str(summary),
                    message_at,
                    _iso(_now()),
                ),
            )

    def list_email_summaries(
        self, provider: str = "gmail", limit: int = 30
    ) -> list[dict[str, Any]]:
        from ..security import decrypt_str

        rows = self._conn.execute(
            "SELECT * FROM email_summaries WHERE source_provider=?"
            " ORDER BY checked_at DESC LIMIT ?",
            (provider, limit),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            for k in ("subject", "sender", "summary"):
                d[k] = decrypt_str(d.get(k, ""))
            out.append(d)
        return out

    def purge_undecryptable_email_summaries(self, provider: str = "gmail") -> int:
        """복호화되지 않는(enc:v1: 접두사가 복호화 후에도 남는) 행 삭제.
        암호화 키 로테이션 등으로 읽을 수 없게 된 행을 정리할 때 사용."""
        from ..security import PREFIX, decrypt_str

        rows = self._conn.execute(
            "SELECT source_provider, source_id, subject, sender, summary"
            " FROM email_summaries WHERE source_provider=?",
            (provider,),
        ).fetchall()
        bad = [
            (r["source_provider"], r["source_id"])
            for r in rows
            if any(
                decrypt_str(r.get(k, "")).startswith(PREFIX)
                for k in ("subject", "sender", "summary")
            )
        ]
        if not bad:
            return 0
        with self.tx() as c:
            for prov, sid in bad:
                c.execute(
                    "DELETE FROM email_summaries WHERE source_provider=? AND source_id=?",
                    (prov, sid),
                )
        return len(bad)

    # ---- agent_sessions (로컬 에이전트 브릿지 push 세션) ----
    _SESSION_EVENT_DEDUP_SECONDS = 60.0
    _WORKING_GAP_CAP_SECONDS = 3600.0

    def upsert_agent_session(
        self,
        tool: str,
        session_key: str,
        status: str,
        *,
        project: str | None = None,
        title: str | None = None,
        doing: str | None = None,
        ask: str | None = None,
        event_at: str | None = None,
        ended_summary: str | None = None,
        artifacts: list[str] | None = None,
        machine: str | None = None,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
    ) -> None:
        """(tool, session_key) 기준 upsert.

        선택 필드가 None이면 기존 값을 유지한다(COALESCE) — 브릿지가 부분
        상태만 보내도 이전 값이 지워지지 않게 하기 위함.
        tokens_in/out은 브릿지가 누적 카운터를 보내므로 MAX로 갱신한다.
        status/doing/ask/project/title이 실제로 바뀐 경우에만
        agent_session_events에 전이 이벤트를 남긴다 (60초 내 동일 이벤트는 생략)."""
        now_dt = _now()
        now = _iso(now_dt)
        artifacts_json = (
            json.dumps(artifacts, ensure_ascii=False) if artifacts is not None else None
        )
        with self.tx() as c:
            prev = c.execute(
                "SELECT status,project,title,doing,ask FROM agent_sessions"
                " WHERE tool=? AND session_key=?",
                (tool, session_key),
            ).fetchone()
            # COALESCE 의미와 동일하게 effective 값을 계산해 변경 여부 판정
            eff = {
                "status": status,
                "project": project if project is not None else (prev["project"] if prev else None),
                "title": title if title is not None else (prev["title"] if prev else None),
                "doing": doing if doing is not None else (prev["doing"] if prev else None),
                "ask": ask if ask is not None else (prev["ask"] if prev else None),
            }
            changed = prev is None or any(
                eff[k] != prev[k] for k in ("status", "project", "title", "doing", "ask")
            )
            c.execute(
                "INSERT INTO agent_sessions(id,tool,session_key,project,title,status,"
                "doing,ask,ended_summary,artifacts,machine,tokens_in,tokens_out,"
                "last_event_at,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(tool,session_key) DO UPDATE SET"
                " project=COALESCE(excluded.project, agent_sessions.project),"
                " title=COALESCE(excluded.title, agent_sessions.title),"
                " status=excluded.status,"
                " doing=COALESCE(excluded.doing, agent_sessions.doing),"
                " ask=COALESCE(excluded.ask, agent_sessions.ask),"
                " ended_summary=COALESCE(excluded.ended_summary,"
                " agent_sessions.ended_summary),"
                " artifacts=COALESCE(excluded.artifacts, agent_sessions.artifacts),"
                " machine=COALESCE(excluded.machine, agent_sessions.machine),"
                " tokens_in=CASE WHEN excluded.tokens_in IS NOT NULL"
                " AND (agent_sessions.tokens_in IS NULL"
                " OR excluded.tokens_in>agent_sessions.tokens_in)"
                " THEN excluded.tokens_in ELSE agent_sessions.tokens_in END,"
                " tokens_out=CASE WHEN excluded.tokens_out IS NOT NULL"
                " AND (agent_sessions.tokens_out IS NULL"
                " OR excluded.tokens_out>agent_sessions.tokens_out)"
                " THEN excluded.tokens_out ELSE agent_sessions.tokens_out END,"
                " last_event_at=COALESCE(excluded.last_event_at,"
                " agent_sessions.last_event_at),"
                " updated_at=excluded.updated_at",
                (
                    uuid.uuid4().hex[:12],
                    tool,
                    session_key,
                    project,
                    title,
                    status,
                    doing,
                    ask,
                    ended_summary,
                    artifacts_json,
                    machine,
                    tokens_in,
                    tokens_out,
                    event_at,
                    now,
                    now,
                ),
            )
            if changed and not self._recent_duplicate_event(c, tool, session_key, eff, now_dt):
                c.execute(
                    "INSERT INTO agent_session_events(id,tool,session_key,status,"
                    "project,doing,ask,event_at,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        uuid.uuid4().hex[:12],
                        tool,
                        session_key,
                        eff["status"],
                        eff["project"],
                        eff["doing"],
                        eff["ask"],
                        event_at or now,
                        now,
                    ),
                )

    def _recent_duplicate_event(
        self,
        c: Any,
        tool: str,
        session_key: str,
        eff: dict[str, Any],
        now_dt: datetime,
    ) -> bool:
        """직전 이벤트와 status/doing/ask/project가 같고 60초 이내면 중복으로 간주."""
        row = c.execute(
            "SELECT status,doing,ask,project,created_at FROM agent_session_events"
            " WHERE tool=? AND session_key=?"
            " ORDER BY created_at DESC, id DESC LIMIT 1",
            (tool, session_key),
        ).fetchone()
        if row is None:
            return False
        if not (
            row["status"] == eff["status"]
            and (row["doing"] or None) == (eff["doing"] or None)
            and (row["ask"] or None) == (eff["ask"] or None)
            and (row["project"] or None) == (eff["project"] or None)
        ):
            return False
        prev_at = _dt(row["created_at"])
        if prev_at is None:
            return False
        if prev_at.tzinfo is None:
            prev_at = prev_at.replace(tzinfo=UTC)
        return (now_dt - prev_at).total_seconds() < self._SESSION_EVENT_DEDUP_SECONDS

    def get_agent_session(self, tool: str, session_key: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM agent_sessions WHERE tool=? AND session_key=?",
            (tool, session_key),
        ).fetchone()
        return dict(row) if row else None

    def list_agent_sessions(
        self,
        limit: int = 50,
        include_ended: bool = False,
        machine: str | None = None,
    ) -> list[dict[str, Any]]:
        """최근 이벤트 순 세션 목록. include_ended=False면 status='ended' 제외.
        machine 지정 시 해당 머신의 세션만 (정확 일치)."""
        sql = (
            "SELECT id,tool,session_key,project,title,status,doing,ask,"
            "ended_summary,artifacts,machine,tokens_in,tokens_out,"
            "last_event_at,created_at,updated_at"
            " FROM agent_sessions"
        )
        clauses: list[str] = []
        params: list[Any] = []
        if not include_ended:
            clauses.append("status != 'ended'")
        if machine is not None:
            clauses.append("machine=?")
            params.append(machine)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY last_event_at DESC NULLS LAST, updated_at DESC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def agent_session_history(
        self, tool: str, session_key: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        """세션의 상태 전이 이벤트를 시간순(오래된→최신)으로 반환."""
        rows = self._conn.execute(
            "SELECT id,tool,session_key,status,project,doing,ask,event_at,created_at"
            " FROM agent_session_events WHERE tool=? AND session_key=?"
            " ORDER BY created_at DESC, id DESC LIMIT ?",
            (tool, session_key, limit),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def agent_session_stats(self, since: str | None = None) -> dict[str, Any]:
        """tool별 세션 통계.

        - sessions: agent_sessions 행 수 (전체 기간)
        - working_seconds: 이벤트 간격 추정 (working 상태 이벤트 → 다음 이벤트,
          간격은 최대 1시간으로 캡, 마지막 working 이벤트는 현재까지)
        - decisions/ended: 해당 status 이벤트 수
        - last_active: last_event_at/updated_at 중 최신 (ISO)
        since(ISO 날짜/시각) 지정 시 working/decision/ended 집계만 그 이후로 제한."""
        per: dict[str, dict[str, Any]] = {}

        def bucket(tool: str) -> dict[str, Any]:
            return per.setdefault(
                tool,
                {
                    "sessions": 0,
                    "working_seconds": 0.0,
                    "decisions": 0,
                    "ended": 0,
                    "last_active": "",
                },
            )

        sess_rows = self._conn.execute(
            "SELECT tool,status,last_event_at,updated_at FROM agent_sessions"
        ).fetchall()
        for r in sess_rows:
            b = bucket(r["tool"])
            b["sessions"] += 1
            ts = r["last_event_at"] or r["updated_at"] or ""
            if ts > b["last_active"]:
                b["last_active"] = ts

        ev_sql = (
            "SELECT tool,session_key,status,doing,ask,event_at,created_at FROM agent_session_events"
        )
        params: list[Any] = []
        if since:
            ev_sql += " WHERE created_at >= ?"
            params.append(since)
        ev_sql += " ORDER BY tool, session_key, created_at, id"
        rows = [dict(r) for r in self._conn.execute(ev_sql, params).fetchall()]

        now_dt = _now()
        prev_key: tuple[str, str] | None = None
        prev_t: datetime | None = None
        prev_status = ""
        last_working: dict[tuple[str, str], datetime] = {}
        for r in rows:
            tool = r["tool"]
            key = (tool, r["session_key"])
            b = bucket(tool)
            st = r["status"] or ""
            if st == "decision":
                b["decisions"] += 1
            elif st == "ended":
                b["ended"] += 1
            t = _dt(r["event_at"]) or _dt(r["created_at"])
            if t is not None and t.tzinfo is None:
                t = t.replace(tzinfo=UTC)
            if prev_key == key and prev_status == "working" and t and prev_t:
                gap = (t - prev_t).total_seconds()
                if gap > 0:
                    b["working_seconds"] += min(gap, self._WORKING_GAP_CAP_SECONDS)
            prev_key, prev_t, prev_status = key, t, st
            if t is not None:
                if st == "working":
                    last_working[key] = t
                else:
                    last_working.pop(key, None)
        # 마지막 이벤트가 working인 세션은 현재 시각까지의 간격을 추정에 포함
        for (tool, _sk), t in last_working.items():
            gap = (now_dt - t).total_seconds()
            if gap > 0:
                bucket(tool)["working_seconds"] += min(gap, self._WORKING_GAP_CAP_SECONDS)
        out: dict[str, Any] = {}
        for tool, b in per.items():
            out[tool] = {
                "sessions": b["sessions"],
                "working_seconds": int(b["working_seconds"]),
                "decisions": b["decisions"],
                "ended": b["ended"],
                "last_active": b["last_active"],
            }
        return out

    def agent_session_hourly(self, day: str) -> list[dict[str, Any]]:
        """로컬 날짜(YYYY-MM-DD)의 시간대별 working/decision 초 — 24개 버킷.

        agent_session_events를 (tool, session_key)별로 시간순 정렬해,
        working/decision 이벤트의 event_at부터 다음 이벤트의 created_at까지의
        구간을 해당 로컬 날짜로 클립해 시간 버킷에 합산한다. 다음 이벤트가
        없으면 그날의 끝(미래면 현재 시각)까지로 본다."""
        try:
            d = datetime.fromisoformat(day)
        except ValueError as exc:
            raise ValueError(f"bad day: {day!r}") from exc
        # naive → 로컬 자정 aware
        day_start = datetime(d.year, d.month, d.day).astimezone()
        day_end = day_start + timedelta(days=1)
        end_cap = min(day_end, _now())
        rows = self._conn.execute(
            "SELECT tool,session_key,status,event_at,created_at"
            " FROM agent_session_events"
            " ORDER BY tool, session_key, created_at, id"
        ).fetchall()
        per: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for r in rows:
            row = dict(r)
            per.setdefault((row["tool"], row["session_key"]), []).append(row)
        seconds = [0.0] * 24
        for evs in per.values():
            for i, ev in enumerate(evs):
                if (ev.get("status") or "") not in ("working", "decision"):
                    continue
                start = _dt(ev.get("event_at")) or _dt(ev.get("created_at"))
                if start is None:
                    continue
                if start.tzinfo is None:
                    start = start.replace(tzinfo=UTC)
                nxt = evs[i + 1] if i + 1 < len(evs) else None
                end = _dt(nxt.get("created_at")) if nxt else None
                if end is not None and end.tzinfo is None:
                    end = end.replace(tzinfo=UTC)
                if end is None or end > end_cap:
                    end = end_cap
                # 해당 로컬 날짜로 클립
                s = max(start, day_start)
                e = min(end, day_end)
                if e <= s:
                    continue
                cur = s.astimezone()
                end_local = e.astimezone()
                while cur < end_local:
                    nxt_hour = cur.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
                    seg_end = min(nxt_hour, end_local)
                    seconds[cur.hour] += (seg_end - cur).total_seconds()
                    cur = seg_end
        return [{"hour": h, "working_seconds": int(round(seconds[h]))} for h in range(24)]

    # ---- llm_usage (LLM 토큰 사용량) ----
    def record_llm_usage(self, model: str, tokens_in: int, tokens_out: int) -> None:
        """LLM 호출 1건의 토큰 사용량 기록. 프롬프트/응답 본문은 저장하지 않는다."""
        with self.tx() as c:
            c.execute(
                "INSERT INTO llm_usage(id,model,tokens_in,tokens_out,created_at) VALUES(?,?,?,?,?)",
                (
                    uuid.uuid4().hex[:12],
                    model,
                    int(tokens_in or 0),
                    int(tokens_out or 0),
                    _iso(_now()),
                ),
            )

    def llm_usage_summary(self, days: int = 7) -> dict[str, Any]:
        """최근 days일 사용량 — 모델별 합계(models) + 일별 시계열(daily)."""
        days = max(1, min(int(days), 365))
        cutoff = _iso(_now() - timedelta(days=days))
        rows = self._conn.execute(
            "SELECT model,tokens_in,tokens_out,created_at FROM llm_usage WHERE created_at >= ?",
            (cutoff,),
        ).fetchall()
        models: dict[str, dict[str, int]] = {}
        daily: dict[str, dict[str, int]] = {}
        for r in rows:
            m = models.setdefault(r["model"], {"calls": 0, "tokens_in": 0, "tokens_out": 0})
            m["calls"] += 1
            m["tokens_in"] += int(r["tokens_in"] or 0)
            m["tokens_out"] += int(r["tokens_out"] or 0)
            day = (r["created_at"] or "")[:10]
            d = daily.setdefault(day, {"tokens_in": 0, "tokens_out": 0})
            d["tokens_in"] += int(r["tokens_in"] or 0)
            d["tokens_out"] += int(r["tokens_out"] or 0)
        return {
            "models": models,
            "daily": [{"date": k, **daily[k]} for k in sorted(daily)],
        }

    # ---- workflow_schedules (주기적 workflow run) ----
    @staticmethod
    def _schedule_json(r: Any) -> dict[str, Any]:
        d = dict(r)
        d["input"] = _loads(d.get("input") or "{}")
        d["enabled"] = bool(d.get("enabled"))
        return d

    def create_schedule(
        self, workflow: str, input_data: dict[str, Any], interval_minutes: int
    ) -> dict[str, Any]:
        now = _now()
        nxt = now + timedelta(minutes=interval_minutes)
        sid = uuid.uuid4().hex[:12]
        with self.tx() as c:
            c.execute(
                "INSERT INTO workflow_schedules(id,workflow,input,interval_minutes,"
                "enabled,last_run_at,next_run_at,created_at) VALUES(?,?,?,?,1,NULL,?,?)",
                (
                    sid,
                    workflow,
                    json.dumps(input_data or {}),
                    int(interval_minutes),
                    _iso(nxt),
                    _iso(now),
                ),
            )
        sched = self.get_schedule(sid)
        return sched if sched is not None else {"id": sid}

    def list_schedules(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM workflow_schedules ORDER BY created_at DESC"
        ).fetchall()
        return [self._schedule_json(r) for r in rows]

    def get_schedule(self, schedule_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM workflow_schedules WHERE id=?", (schedule_id,)
        ).fetchone()
        return self._schedule_json(row) if row else None

    def set_schedule_enabled(self, schedule_id: str, enabled: bool) -> dict[str, Any] | None:
        """enabled 토글. 활성화 시 next_run_at을 now+interval로 재계산."""
        sched = self.get_schedule(schedule_id)
        if sched is None:
            return None
        next_run = sched.get("next_run_at")
        if enabled:
            nxt = _now() + timedelta(minutes=int(sched["interval_minutes"]))
            next_run = _iso(nxt)
        with self.tx() as c:
            c.execute(
                "UPDATE workflow_schedules SET enabled=?, next_run_at=? WHERE id=?",
                (1 if enabled else 0, next_run, schedule_id),
            )
        return self.get_schedule(schedule_id)

    def delete_schedule(self, schedule_id: str) -> bool:
        with self.tx() as c:
            cur = c.execute("DELETE FROM workflow_schedules WHERE id=?", (schedule_id,))
        return cur.rowcount > 0

    def due_schedules(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM workflow_schedules WHERE enabled=1"
            " AND next_run_at IS NOT NULL AND next_run_at <= ?"
            " ORDER BY next_run_at",
            (_iso(_now()),),
        ).fetchall()
        return [self._schedule_json(r) for r in rows]

    def mark_schedule_fired(self, schedule_id: str, last_run_at: str, next_run_at: str) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE workflow_schedules SET last_run_at=?, next_run_at=? WHERE id=?",
                (last_run_at, next_run_at, schedule_id),
            )

    # ---- run stats (GET /runs/stats) ----
    _STATS_FAILED = {"failed", "rejected", "budget_exhausted", "needs_human", "cancelled"}
    _STATS_TERMINAL = _STATS_FAILED | {"completed"}

    def run_stats(self, workflow_key: str | None, days: int) -> dict[str, Any]:
        """최근 days일 run/stage_runs 집계. created_at 기준 윈도우."""
        days = max(1, min(int(days), 365))
        cutoff = _iso(_now() - timedelta(days=days))
        # workflow_key=None이면 전체 — "? IS NULL OR" 패턴으로 분기 없이 처리
        run_params: list[Any] = [cutoff, workflow_key, workflow_key]
        stage_params: list[Any] = [cutoff, workflow_key, workflow_key]
        run_rows = self._conn.execute(
            "SELECT id,status,created_at,updated_at FROM runs"
            " WHERE created_at >= ? AND (? IS NULL OR workflow_key=?)",
            run_params,
        ).fetchall()
        stage_rows = self._conn.execute(
            "SELECT sr.run_id, sr.stage_key, sr.attempt, sr.status, sr.tokens_in,"
            " sr.tokens_out, sr.started_at FROM stage_runs sr"
            " JOIN runs r ON r.id = sr.run_id"
            " WHERE r.created_at >= ? AND (? IS NULL OR r.workflow_key=?)",
            stage_params,
        ).fetchall()

        counts = {"succeeded": 0, "failed": 0, "running": 0, "waiting_approval": 0}
        durations: list[float] = []
        daily: dict[str, dict[str, int]] = {}
        for row in run_rows:
            st = row["status"]
            if st == "completed":
                counts["succeeded"] += 1
            elif st in self._STATS_FAILED:
                counts["failed"] += 1
            elif st in ("queued", "running"):
                counts["running"] += 1
            elif st == "waiting_approval":
                counts["waiting_approval"] += 1
            created = _dt(row["created_at"])
            updated = _dt(row["updated_at"])
            if st in self._STATS_TERMINAL and created and updated:
                durations.append((updated - created).total_seconds())
            day = (row["created_at"] or "")[:10]
            if day:
                daily.setdefault(day, {"runs": 0, "tokens": 0})["runs"] += 1

        tokens_in = tokens_out = 0
        latest_attempt: dict[tuple[str, str], tuple[int, str]] = {}
        for row in stage_rows:
            tin = int(row["tokens_in"] or 0)
            tout = int(row["tokens_out"] or 0)
            tokens_in += tin
            tokens_out += tout
            skey = (row["run_id"], row["stage_key"])
            attempt = int(row["attempt"] or 0)
            if skey not in latest_attempt or attempt > latest_attempt[skey][0]:
                latest_attempt[skey] = (attempt, row["status"])
            day = (row["started_at"] or "")[:10]
            if day:
                daily.setdefault(day, {"runs": 0, "tokens": 0})["tokens"] += tin + tout

        stage_failures: dict[str, int] = {}
        for (_run_id, stage_key), (_attempt, st) in latest_attempt.items():
            if st == "failed":
                stage_failures[stage_key] = stage_failures.get(stage_key, 0) + 1

        today = _now().date()
        start = today - timedelta(days=days - 1)
        out_daily: list[dict[str, Any]] = []
        d = start
        while d <= today:
            key = d.isoformat()
            bucket = daily.get(key, {"runs": 0, "tokens": 0})
            out_daily.append({"date": key, "runs": bucket["runs"], "tokens": bucket["tokens"]})
            d += timedelta(days=1)

        return {
            "workflow": workflow_key,
            "days": days,
            "runs": len(run_rows),
            **counts,
            "avg_duration_s": (round(sum(durations) / len(durations), 1) if durations else None),
            "total_tokens_in": tokens_in,
            "total_tokens_out": tokens_out,
            "stage_failures": stage_failures,
            "daily": out_daily,
        }

    # ---- inbox_items (통합 인박스) ----
    def upsert_inbox_item(
        self,
        kind: str,
        title: str,
        body: str = "",
        link: str = "",
        dedupe_key: str = "",
        reset_read: bool = False,
    ) -> str:
        """인박스 항목 추가. dedupe_key가 있으면 기존 행을 갱신(읽음 상태는 유지,
        reset_read=True면 미읽음으로 되돌림) — 세션 어텐션/승인 대기처럼
        반복 발생하는 이벤트의 중복을 막는다."""
        now = _iso(_now())
        with self.tx() as c:
            if dedupe_key:
                row = c.execute(
                    "SELECT id FROM inbox_items WHERE dedupe_key=?", (dedupe_key,)
                ).fetchone()
                if row is not None:
                    iid = row["id"]
                    # created_at는 유지 — 반복 이벤트가 목록 순서를 바꾸지 않게
                    c.execute(
                        "UPDATE inbox_items SET kind=?, title=?, body=?, link=?,"
                        " read=CASE WHEN ? THEN 0 ELSE read END WHERE id=?",
                        (kind, title, body, link, 1 if reset_read else 0, iid),
                    )
                    return iid
            iid = uuid.uuid4().hex[:12]
            c.execute(
                "INSERT INTO inbox_items(id,kind,title,body,link,read,dedupe_key,"
                "created_at) VALUES(?,?,?,?,?,0,?,?)",
                (iid, kind, title, body, link, dedupe_key, now),
            )
            return iid

    def list_inbox(self, limit: int = 50, unread_only: bool = False) -> list[dict[str, Any]]:
        cols = "id,kind,title,body,link,read,created_at"
        if unread_only:
            sql = f"SELECT {cols} FROM inbox_items WHERE read=0"  # noqa: S608 - cols는 고정 상수
        else:
            sql = f"SELECT {cols} FROM inbox_items"  # noqa: S608 - cols는 고정 상수
        sql += " ORDER BY created_at DESC LIMIT ?"
        rows = self._conn.execute(sql, (limit,)).fetchall()
        return [
            {
                "id": r["id"],
                "kind": r["kind"],
                "title": r["title"],
                "body": r["body"],
                "link": r["link"],
                "read": bool(r["read"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    def mark_inbox_read(self, item_id: str) -> bool:
        with self.tx() as c:
            cur = c.execute("UPDATE inbox_items SET read=1 WHERE id=?", (item_id,))
        return cur.rowcount > 0

    def mark_inbox_read_dedupe(self, dedupe_key: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE inbox_items SET read=1 WHERE dedupe_key=?", (dedupe_key,))

    def mark_all_inbox_read(self) -> int:
        with self.tx() as c:
            cur = c.execute("UPDATE inbox_items SET read=1 WHERE read=0")
        return cur.rowcount

    def inbox_unread_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM inbox_items WHERE read=0").fetchone()
        return int(row["n"]) if row else 0

    def sync_notifications_to_inbox(self, limit: int = 200) -> int:
        """notifications 테이블 → inbox_items(kind='notification') 지연 미러링.
        dedupe_key=notif:{id}로 멱등 — GET /inbox 호출 시마다 호출해도 안전."""
        rows = self._conn.execute(
            "SELECT id,title,body,source_url FROM notifications ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        for r in rows:
            self.upsert_inbox_item(
                "notification",
                r["title"],
                body=r["body"],
                link=r["source_url"],
                dedupe_key=f"notif:{r['id']}",
            )
        return len(rows)

    # ---- session_activity (세션 활동 녹화/리플레이) ----
    _ACTIVITY_THROTTLE_SECONDS = 60.0
    _ACTIVITY_RETENTION_DAYS = 7

    def record_session_activity(
        self,
        tool: str,
        session_key: str,
        status: str,
        doing: str | None = None,
        project: str | None = None,
    ) -> None:
        """push된 세션 상태를 활동 로그에 적재. status/doing이 바뀌었거나
        마지막 기록 후 60초가 지나야만 insert(스로틀). 가끔 7일 이전 행을 정리."""
        import secrets

        now_dt = _now()
        now = _iso(now_dt)
        with self.tx() as c:
            row = c.execute(
                "SELECT status,doing,recorded_at FROM session_activity"
                " WHERE tool=? AND session_key=?"
                " ORDER BY recorded_at DESC, id DESC LIMIT 1",
                (tool, session_key),
            ).fetchone()
            if row is not None:
                prev_at = _dt(row["recorded_at"])
                same = (row["status"] or "") == (status or "") and (row["doing"] or None) == (
                    doing or None
                )
                if same and prev_at is not None:
                    if prev_at.tzinfo is None:
                        prev_at = prev_at.replace(tzinfo=UTC)
                    if (now_dt - prev_at).total_seconds() < self._ACTIVITY_THROTTLE_SECONDS:
                        return
            c.execute(
                "INSERT INTO session_activity(tool,session_key,status,doing,project,"
                "recorded_at) VALUES(?,?,?,?,?,?)",
                (tool, session_key, status, doing, project, now),
            )
            # 저비용 보존 정책 — insert마다 ~2% 확률로 7일 이전 행 삭제
            if secrets.randbelow(50) == 0:
                cutoff = _iso(now_dt - timedelta(days=self._ACTIVITY_RETENTION_DAYS))
                c.execute("DELETE FROM session_activity WHERE recorded_at < ?", (cutoff,))

    def session_activity(
        self, tool: str, session_key: str, limit: int = 200
    ) -> list[dict[str, Any]]:
        """세션 활동을 시간순(오래된→최신)으로 반환."""
        rows = self._conn.execute(
            "SELECT recorded_at,status,doing,project FROM session_activity"
            " WHERE tool=? AND session_key=?"
            " ORDER BY recorded_at DESC, id DESC LIMIT ?",
            (tool, session_key, limit),
        ).fetchall()
        return [
            {
                "at": r["recorded_at"],
                "status": r["status"],
                "doing": r["doing"],
                "project": r["project"],
            }
            for r in reversed(rows)
        ]

    # ---- reports (daily/weekly 리포트) ----
    def save_report(self, kind: str, content: dict[str, Any]) -> str:
        rid = uuid.uuid4().hex[:12]
        with self.tx() as c:
            c.execute(
                "INSERT INTO reports(id,kind,content,created_at) VALUES(?,?,?,?)",
                (rid, kind, json.dumps(content, ensure_ascii=False), _iso(_now())),
            )
        return rid

    def latest_report(self, kind: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT id,kind,content,created_at FROM reports WHERE kind=?"
            " ORDER BY created_at DESC LIMIT 1",
            (kind,),
        ).fetchone()
        if not row:
            return None
        return {
            "id": row["id"],
            "kind": row["kind"],
            "content": _loads(row["content"]),
            "created_at": row["created_at"],
        }

    def list_reports(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT id,kind,created_at FROM reports ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- llm_usage 상세 (비용 대시보드용 행 단위 조회) ----
    def llm_usage_rows(self, since_iso: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT model,tokens_in,tokens_out,created_at FROM llm_usage"
            " WHERE created_at >= ? ORDER BY created_at",
            (since_iso,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- agent_sessions 브릿지/헬스 집계 ----
    def agent_sessions_bridge_info(self) -> dict[str, Any]:
        """last_push_at(전체 최신 updated_at) + 머신별 그룹 집계."""
        row = self._conn.execute(
            "SELECT MAX(updated_at) AS last_push_at FROM agent_sessions"
        ).fetchone()
        last_push_at = row["last_push_at"] if row else None
        rows = self._conn.execute(
            "SELECT machine, MAX(updated_at) AS last_push_at, COUNT(*) AS sessions"
            " FROM agent_sessions GROUP BY machine"
        ).fetchall()
        machines = [
            {
                "machine": r["machine"] or "",
                "last_push_at": r["last_push_at"],
                "sessions": int(r["sessions"]),
            }
            for r in rows
        ]
        return {"last_push_at": last_push_at, "machines": machines}

    # ---- users 관리 ----
    def list_users(self) -> list[dict[str, Any]]:
        """전체 사용자 — 비밀번호 해시 제외."""
        rows = self._conn.execute(
            "SELECT id,email,display_name,role,created_at FROM users ORDER BY created_at"
        ).fetchall()
        return [dict(r) for r in rows]

    def set_user_role(self, user_id: str, role: str) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE users SET role=?, updated_at=? WHERE id=?",
                (role, _iso(_now()), user_id),
            )

    # ---- db 메타 (헬스 상세) ----
    def db_info(self) -> dict[str, Any]:
        """백엔드 종류/경로/SQLite 파일 크기(PG는 size_bytes=None)."""
        if self._pg:
            return {"type": "postgresql", "path": None, "size_bytes": None}
        path = str(self._path)
        size = None
        try:
            size = self._path.stat().st_size
        except OSError:
            pass
        return {"type": "sqlite", "path": path, "size_bytes": size}
