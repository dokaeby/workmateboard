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
  created_by TEXT NOT NULL DEFAULT '',
  project TEXT NOT NULL DEFAULT '',
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
  email_verified INTEGER NOT NULL DEFAULT 1,
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

-- 이메일 인증 토큰 — 원문 금지, SHA-256 해시만 저장
CREATE TABLE IF NOT EXISTS email_verifications (
  id TEXT PRIMARY KEY,
  email TEXT NOT NULL,
  token_hash TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  used_at TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_email_verifications_email ON email_verifications(email);
CREATE INDEX IF NOT EXISTS idx_email_verifications_hash ON email_verifications(token_hash);

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

-- playbooks: 원클릭 작업 템플릿
CREATE TABLE IF NOT EXISTS playbooks (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  workflow TEXT NOT NULL,
  input TEXT NOT NULL DEFAULT '{}',
  created_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);

-- run_comments: run/diff 토론
CREATE TABLE IF NOT EXISTS run_comments (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  user_id TEXT NOT NULL DEFAULT '',
  email TEXT NOT NULL DEFAULT '',
  body TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_run_comments_run ON run_comments(run_id);

-- connectors: 외부 데이터 소스 (github/jira/notion/linear/webhook)
CREATE TABLE IF NOT EXISTS connectors (
  id TEXT PRIMARY KEY,
  type TEXT NOT NULL,
  name TEXT NOT NULL,
  config TEXT NOT NULL DEFAULT '{}',
  enabled INTEGER NOT NULL DEFAULT 1,
  has_token INTEGER NOT NULL DEFAULT 0,
  last_fetch_at TEXT,
  last_error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);

-- custom_workflows: 비주얼 빌더로 만든 WorkflowSpec 영속화
CREATE TABLE IF NOT EXISTS custom_workflows (
  name TEXT PRIMARY KEY,
  spec TEXT NOT NULL,
  created_at TEXT NOT NULL
);

-- triggers: 세션 상태 전이 기반 자동화
CREATE TABLE IF NOT EXISTS triggers (
  id TEXT PRIMARY KEY,
  enabled INTEGER NOT NULL DEFAULT 1,
  match TEXT NOT NULL DEFAULT '{}',
  action TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  last_fired_at TEXT,
  last_error TEXT NOT NULL DEFAULT ''
);

-- trigger_fires: trigger+session+state 전이당 1회 발사의 멱등 기록
CREATE TABLE IF NOT EXISTS trigger_fires (
  id TEXT PRIMARY KEY,
  trigger_id TEXT NOT NULL,
  tool TEXT NOT NULL,
  session_key TEXT NOT NULL,
  state TEXT NOT NULL,
  fp TEXT NOT NULL DEFAULT '',
  fired_at TEXT NOT NULL,
  UNIQUE(trigger_id, tool, session_key, state, fp)
);
CREATE INDEX IF NOT EXISTS idx_trigger_fires_trigger ON trigger_fires(trigger_id);

-- share_links: 공개 스냅샷 공유 토큰 (+30일 만료)
CREATE TABLE IF NOT EXISTS share_links (
  token TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  ref_id TEXT NOT NULL DEFAULT '',
  payload TEXT NOT NULL DEFAULT '{}',
  created_by TEXT NOT NULL DEFAULT '',
  expires_at TEXT NOT NULL,
  created_at TEXT NOT NULL
);

-- dashboard_cards: 사용자 등록 webhook/JSON 위젯
CREATE TABLE IF NOT EXISTS dashboard_cards (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  url TEXT NOT NULL,
  json_path TEXT NOT NULL DEFAULT '',
  enabled INTEGER NOT NULL DEFAULT 1,
  created_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);

-- projects: 팀장 프로젝트 마스터 — 세션/run의 project 태그와 ci 매칭
CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  color TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',
  goal TEXT NOT NULL DEFAULT '',
  target_count INTEGER NOT NULL DEFAULT 0,
  created_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_projects_name_lower ON projects(lower(name));
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
  created_by TEXT NOT NULL DEFAULT '',
  project TEXT NOT NULL DEFAULT '',
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
  email_verified INTEGER NOT NULL DEFAULT 1,
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

CREATE TABLE IF NOT EXISTS email_verifications (
  id TEXT PRIMARY KEY,
  email TEXT NOT NULL,
  token_hash TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  used_at TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_email_verifications_email ON email_verifications(email);
CREATE INDEX IF NOT EXISTS idx_email_verifications_hash ON email_verifications(token_hash);

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

-- playbooks: 원클릭 작업 템플릿
CREATE TABLE IF NOT EXISTS playbooks (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  workflow TEXT NOT NULL,
  input TEXT NOT NULL DEFAULT '{}',
  created_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);

-- run_comments: run/diff 토론
CREATE TABLE IF NOT EXISTS run_comments (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  user_id TEXT NOT NULL DEFAULT '',
  email TEXT NOT NULL DEFAULT '',
  body TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_run_comments_run ON run_comments(run_id);

-- connectors: 외부 데이터 소스 (github/jira/notion/linear/webhook)
CREATE TABLE IF NOT EXISTS connectors (
  id TEXT PRIMARY KEY,
  type TEXT NOT NULL,
  name TEXT NOT NULL,
  config TEXT NOT NULL DEFAULT '{}',
  enabled INTEGER NOT NULL DEFAULT 1,
  has_token INTEGER NOT NULL DEFAULT 0,
  last_fetch_at TEXT,
  last_error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);

-- custom_workflows: 비주얼 빌더로 만든 WorkflowSpec 영속화
CREATE TABLE IF NOT EXISTS custom_workflows (
  name TEXT PRIMARY KEY,
  spec TEXT NOT NULL,
  created_at TEXT NOT NULL
);

-- triggers: 세션 상태 전이 기반 자동화
CREATE TABLE IF NOT EXISTS triggers (
  id TEXT PRIMARY KEY,
  enabled INTEGER NOT NULL DEFAULT 1,
  match TEXT NOT NULL DEFAULT '{}',
  action TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  last_fired_at TEXT,
  last_error TEXT NOT NULL DEFAULT ''
);

-- trigger_fires: trigger+session+state 전이당 1회 발사의 멱등 기록
CREATE TABLE IF NOT EXISTS trigger_fires (
  id TEXT PRIMARY KEY,
  trigger_id TEXT NOT NULL,
  tool TEXT NOT NULL,
  session_key TEXT NOT NULL,
  state TEXT NOT NULL,
  fp TEXT NOT NULL DEFAULT '',
  fired_at TEXT NOT NULL,
  UNIQUE(trigger_id, tool, session_key, state, fp)
);
CREATE INDEX IF NOT EXISTS idx_trigger_fires_trigger ON trigger_fires(trigger_id);

-- share_links: 공개 스냅샷 공유 토큰 (+30일 만료)
CREATE TABLE IF NOT EXISTS share_links (
  token TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  ref_id TEXT NOT NULL DEFAULT '',
  payload TEXT NOT NULL DEFAULT '{}',
  created_by TEXT NOT NULL DEFAULT '',
  expires_at TEXT NOT NULL,
  created_at TEXT NOT NULL
);

-- dashboard_cards: 사용자 등록 webhook/JSON 위젯
CREATE TABLE IF NOT EXISTS dashboard_cards (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  url TEXT NOT NULL,
  json_path TEXT NOT NULL DEFAULT '',
  enabled INTEGER NOT NULL DEFAULT 1,
  created_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);

-- projects: 팀장 프로젝트 마스터 — 세션/run의 project 태그와 ci 매칭
CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  color TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',
  goal TEXT NOT NULL DEFAULT '',
  target_count INTEGER NOT NULL DEFAULT 0,
  created_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_projects_name_lower ON projects(lower(name));
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


def _clamp_limit(limit: Any, default: int = 50, maximum: int = 1000) -> int:
    """LIMIT 파라미터 정규화 — 음수/비정수/None이 SQLite `LIMIT -1`(무제한)
    반환으로 이어지지 않도록 최소 1·최대 maximum로 클램프한다."""
    try:
        n = int(limit)
    except (TypeError, ValueError):
        n = default
    return max(1, min(n, maximum))


class Store:
    """동기 store. path가 postgres(ql):// DSN이면 PostgreSQL, 아니면 SQLite 파일."""

    def __init__(self, path: str | Path) -> None:
        dsn = str(path)
        self._pg = _is_pg_dsn(dsn)
        self._integrity_errors: tuple[type[Exception], ...] = (
            (psycopg.errors.IntegrityError,) if self._pg else (sqlite3.IntegrityError,)
        )
        # RLock — tx() 안에서 다른 store 메서드(각자 tx()를 잡는)를 호출하는
        # 중첩 경로가 생겨도 같은 스레드면 재진입 가능해야 한다(비재진입 Lock은
        # 자기 데드락). PG/SQLite 모두 tx 직렬화 의미는 동일하다.
        self._lock = threading.RLock()
        self._local = threading.local()
        self._sqlite_conns: list[Any] = []
        self._closed = False
        if self._pg:
            self._pg_conn = _PGConn(dsn)
            for stmt in SCHEMA_PG.split(";"):
                if stmt.strip():
                    self._conn.execute(stmt)
        else:
            self._path = Path(path)
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # CPython #118172: shared statement caches can mix concurrent query results.
            with self._conn:
                self._conn.executescript(SCHEMA)
        self._migrate_agent_session_columns()
        self._migrate_approval_columns()
        self._migrate_user_columns()
        self._migrate_run_columns()
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
        """기존 DB의 users에 role/email_verified 컬럼 보강.
        email_verified 기본값 1 — 기존 계정은 이미 로그인 중이므로 검증을
        강요하지 않는다. 신규 비밀번호 가입만 create_user가 명시적으로 0을 넣는다."""
        self._ensure_columns("users", {"role": "TEXT NOT NULL DEFAULT 'user'"})
        self._ensure_columns("users", {"email_verified": "INTEGER NOT NULL DEFAULT 1"})

    def _migrate_run_columns(self) -> None:
        """기존 DB의 runs에 created_by/project 컬럼 보강."""
        self._ensure_columns(
            "runs",
            {"created_by": "TEXT NOT NULL DEFAULT ''", "project": "TEXT NOT NULL DEFAULT ''"},
        )

    @property
    def _conn(self) -> Any:
        """스레드별 커넥션.

        요청 스레드와 _automation_loop 스레드가 같은 sqlite3.Connection을
        동시에 쓰면 간헐적 segfault이 나므로(CPython sqlite3는 연결 단위
        스레드 안전이 없다) 스레드마다 연결을 하나씩 둔다. WAL + busy_timeout으로
        읽기/쓰기 동시성을 확보한다."""
        if self._pg:
            return self._pg_conn
        if self._closed:
            raise RuntimeError("store is closed")
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(
                str(self._path),
                check_same_thread=False,
                timeout=30,
                cached_statements=0,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn = conn
            self._sqlite_conns.append(conn)
        return conn

    def close(self) -> None:
        """호출 스레드의 커넥션만 닫는다.

        다른 스레드(automation loop 등)의 커넥션을 여기서 닫으면 사용
        중인 연결을 닫아 segfault가 난다 — 각 스레드는 자기 커넥션을
        쓰는 동안 살아 있고, 프로세스 종료 시 함께 회수된다.
        새 커넥션 생성은 _closed 플래그로 차단한다."""
        self._closed = True
        if self._pg:
            self._pg_conn.close()
            return
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            self._local.conn = None
            try:
                self._sqlite_conns.remove(conn)
            except ValueError:
                pass
            try:
                conn.close()
            except Exception as e:
                logger.debug("sqlite conn close failed: %s", e)

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
        created_by: str = "",
    ) -> Run:
        # input.project 자유 태그를 컬럼으로도 유지 (PATCH /runs/{id}와 동일 경로)
        project = ""
        pv = input_data.get("project")
        if isinstance(pv, str):
            project = pv.strip()[:200]
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
            created_by=created_by,
            project=project,
        )
        with self.tx() as c:
            c.execute(
                "INSERT INTO runs(id,workflow_key,workflow_version,input,status,trace_id,"
                "error,metadata,created_by,project,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run.id,
                    run.workflow_key,
                    run.workflow_version,
                    json.dumps(run.input),
                    run.status.value,
                    run.trace_id,
                    run.error,
                    json.dumps(run.metadata),
                    run.created_by,
                    project,
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
        limit = _clamp_limit(limit, 50, 500)
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
        """run 갱신 — 단일 UPDATE로 lost update 방지.

        이전 구현은 get_run으로 읽은 뒤 전체 컬럼을 다시 써서, 읽기~쓰기 사이에
        다른 writer(stage 완료 등)가 바꾼 status/metadata를 덮어쓸 수 있었다.
        COALESCE(?, col) 패턴으로 None 인자만 기존 값을 유지하고, status/error/
        metadata 각각 독립적으로 현재 행 기준 갱신한다."""
        with self.tx() as c:
            c.execute(
                "UPDATE runs SET status=COALESCE(?, status),"
                " error=COALESCE(?, error), metadata=COALESCE(?, metadata),"
                " updated_at=? WHERE id=?",
                (
                    status.value if status is not None else None,
                    error,
                    json.dumps(metadata) if metadata is not None else None,
                    _iso(_now()),
                    run_id,
                ),
            )

    def claim_run_status(self, run_id: str, from_status: str, to_status: str) -> bool:
        """run 상태 조건부 전이 — WHERE status=? 원자 claim.

        동시 승인/재개 요청이 둘 다 waiting_approval을 읽고 resume하는 것을
        막는다. rowcount>0이면 이 호출이 claim에 성공한 것."""
        with self.tx() as c:
            cur = c.execute(
                "UPDATE runs SET status=?, updated_at=? WHERE id=? AND status=?",
                (to_status, _iso(_now()), run_id, from_status),
            )
        return cur.rowcount > 0

    def fail_stale_runs(self, stale_before_iso: str, error: str = "stale_timeout") -> int:
        """updated_at이 stale_before_iso보다 오래된 queued/running run을 failed로 마감.

        재시작/강제종료로 running에 고착된 run 복구용. waiting_approval 등
        사람 승인 대기 상태는 정상 대기이므로 절대 건드리지 않는다.
        각 run의 아직 running인 stage_runs도 failed로 정리하고 audit을 남긴다.
        반환값: 마감된 run 수."""
        now = _iso(_now())
        reaped: list[str] = []
        with self.tx() as c:
            rows = c.execute(
                "SELECT id FROM runs WHERE status IN ('queued','running') AND updated_at < ?",
                (stale_before_iso,),
            ).fetchall()
            for r in rows:
                rid = str(r["id"])
                cur = c.execute(
                    "UPDATE runs SET status='failed', error=?, updated_at=?"
                    " WHERE id=? AND status IN ('queued','running')",
                    (error, now, rid),
                )
                if int(getattr(cur, "rowcount", 0) or 0) <= 0:
                    continue  # 조회~갱신 사이 상태 전이 — 다른 경로가 처리 중
                reaped.append(rid)
                c.execute(
                    "UPDATE stage_runs SET status='failed', error=?, finished_at=?"
                    " WHERE run_id=? AND status='running'",
                    (error, now, rid),
                )
        for rid in reaped:
            self.audit(rid, "system", "run_stale_reaped", rid, {"error": error})
        return len(reaped)

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
            created_by=row["created_by"] if "created_by" in row.keys() else "",
            project=row["project"] if "project" in row.keys() else "",
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
        limit = _clamp_limit(limit)
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
        limit = _clamp_limit(limit)
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

    # 처리 중 메시지 리스 — claim 시 retry_at을 미래로 밀어 다른 worker가
    # 같은 행을 집지 않게 한다. 프로세스 크래시면 리스 만료 후 재claim된다.
    # gmail_calendar run은 LLM 다단계라 수 분 걸릴 수 있어 리스를 넉넉히 둔다.
    # 크래시 시 이 시간만큼 처리가 지연될 뿐 중복 처리는 없다.
    _MESSAGE_LEASE_SECONDS = 1800

    def pending_messages(self, limit: int = 100) -> list[dict[str, Any]]:
        """처리 대상 메시지 — 'pending'이거나 리스 만료된 'processing' 행.

        retry_at은 백오프(미래) 또는 리스(미래) 표시로도 쓰인다. ''(신규)는
        어떤 ISO 문자열보다 작으므로 즉시 대상이 된다."""
        rows = self._conn.execute(
            "SELECT * FROM message_extractions"
            " WHERE processing_status IN ('pending','processing')"
            " AND retry_at <= ? ORDER BY created_at LIMIT ?",
            (_iso(_now()), max(1, min(int(limit), 1000))),
        ).fetchall()
        return [dict(r) for r in rows]

    def claim_message(self, provider: str, source_id: str) -> bool:
        """메시지 원자 claim — pending(또는 리스 만료 processing)을 processing으로.

        UPDATE ... WHERE processing_status 조건 + retry_at 리스 설정을 한
        문장으로 수행해, 복수 worker/cron tick이 같은 메시지를 동시에
        처리해 run을 중복 생성하는 것을 막는다. rowcount>0이면 claim 성공."""
        lease_until = _iso(_now() + timedelta(seconds=self._MESSAGE_LEASE_SECONDS))
        now_iso = _iso(_now())
        with self.tx() as c:
            cur = c.execute(
                "UPDATE message_extractions SET processing_status='processing',"
                " retry_at=?"
                " WHERE source_provider=? AND source_id=?"
                " AND processing_status IN ('pending','processing')"
                " AND retry_at <= ?",
                (lease_until, provider, source_id, now_iso),
            )
        return cur.rowcount > 0

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
        limit = _clamp_limit(limit, 20, 500)
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
        limit = _clamp_limit(limit)
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

    def claim_event_review(self, event_id: str, stale_before: str) -> bool:
        """일정 검토 원자 claim — status를 'registering'으로 조건부 전이.

        needs_review/needs_confirmation/candidate 상태이거나, 이전 claim이
        크래시로 남은 'registering'(updated_at < stale_before)일 때만 성공.
        find_by_marker→create_event 사이의 중복 캘린더 생성을 막는다.
        rowcount>0이면 claim 성공 — 호출자는 작업 후 registered/rejected 등으로
        전이하거나, 실패 시 원래 상태로 복구해야 한다."""
        now = _iso(_now())
        with self.tx() as c:
            cur = c.execute(
                "UPDATE scheduled_events SET status='registering', updated_at=?"
                " WHERE id=? AND (status IN"
                " ('needs_review','needs_confirmation','candidate')"
                " OR (status='registering' AND updated_at < ?))",
                (now, event_id, stale_before),
            )
        return cur.rowcount > 0

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
        limit = _clamp_limit(limit)
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
    def _norm_role(role: Any) -> str:
        """users.role 정규화 — member|lead|admin. 레거시 'user'는 member."""
        r = str(role or "").strip().lower()
        if r == "user":
            r = "member"
        return r if r in ("member", "lead", "admin") else "member"

    @staticmethod
    def _user_json(r: Any) -> dict[str, Any]:
        return {
            "id": r["id"],
            "email": r["email"],
            "display_name": r["display_name"],
            "google_sub": r["google_sub"],
            "role": Store._norm_role(r["role"] if "role" in r.keys() else "user"),
            # 컬럼이 없는 구 스키마 SELECT(세션 JOIN 등)는 검증 완료로 간주
            "email_verified": bool(r["email_verified"]) if "email_verified" in r.keys() else True,
            "created_at": r["created_at"],
        }

    def create_user(
        self,
        email: str,
        password_hash: str = "",
        display_name: str = "",
        google_sub: str = "",
        email_verified: bool = False,
    ) -> dict[str, Any] | None:
        """신규 사용자 생성. email 중복 시 None.
        email_verified 기본 False — 비밀번호 가입은 이메일 소유권 미증명.
        Google OAuth처럼 이메일이 검증된 경로만 True를 넘긴다."""
        uid = uuid.uuid4().hex[:16]
        now = _iso(_now())
        try:
            with self.tx() as c:
                c.execute(
                    "INSERT INTO users(id,email,password_hash,display_name,google_sub,"
                    "email_verified,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        uid,
                        email.lower(),
                        password_hash,
                        display_name,
                        google_sub,
                        1 if email_verified else 0,
                        now,
                        now,
                    ),
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

    def revoke_user_sessions(self, user_id: str) -> int:
        """사용자의 모든 유효 세션 폐기 — 비밀번호 변경 등 보안 이벤트용.
        반환값: 폐기된 세션 수."""
        with self.tx() as c:
            cur = c.execute(
                "UPDATE auth_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
                (_iso(_now()), user_id),
            )
            return int(getattr(cur, "rowcount", 0) or 0)

    def list_user_sessions(self, user_id: str) -> list[dict[str, Any]]:
        """사용자의 유효(미폐기·미만료) 세션 목록 — id는 토큰 해시."""
        rows = self._conn.execute(
            "SELECT id, created_at, expires_at FROM auth_sessions"
            " WHERE user_id=? AND revoked_at IS NULL AND expires_at>?"
            " ORDER BY created_at DESC",
            (user_id, _iso(_now())),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- email_verifications (가입 이메일 인증) ----
    def create_email_verification(self, email: str, ttl_hours: int = 24) -> str:
        """인증 토큰 발급 — 반환값이 원문(메일 링크용), DB에는 SHA-256 해시만 저장."""
        import hashlib
        import secrets

        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        now = _now()
        # opportunistic 정리 — used/expired 행이 누적되지 않도록 발급 시 함께 삭제
        self.delete_expired_verifications(now)
        with self.tx() as c:
            # 같은 이메일의 기존 미사용 토큰은 이번 발급으로 무효화 —
            # 재발송/재가입 시 이전 링크가 계속 유효한 채 남는 것을 막는다.
            c.execute(
                "UPDATE email_verifications SET used_at=? WHERE email=? AND used_at IS NULL",
                (_iso(now), email.lower()),
            )
            c.execute(
                "INSERT INTO email_verifications(id,email,token_hash,expires_at,created_at)"
                " VALUES(?,?,?,?,?)",
                (
                    uuid.uuid4().hex[:16],
                    email.lower(),
                    token_hash,
                    _iso(now + timedelta(hours=ttl_hours)),
                    _iso(now),
                ),
            )
        return token

    def get_valid_verification(self, token_hash: str) -> str | None:
        """미사용·미만료 토큰의 이메일 반환. 없거나 만료/사용이면 None."""
        r = self._conn.execute(
            "SELECT email, expires_at FROM email_verifications"
            " WHERE token_hash=? AND used_at IS NULL",
            (token_hash,),
        ).fetchone()
        if not r:
            return None
        exp = _dt(r["expires_at"])
        if exp is None or exp <= _now():
            return None
        return str(r["email"])

    def consume_email_verification(self, token_hash: str) -> str | None:
        """토큰 원자적 consume — 유효(미사용·미만료)하면 used_at 마킹 후 이메일 반환.

        SELECT 후 UPDATE하는 비원자 경로는 동시 요청이 같은 토큰을 둘 다
        유효하다고 판정할 수 있다. WHERE used_at IS NULL AND expires_at>? 조건의
        UPDATE rowcount로 단 한 번만 성공하도록 직렬화한다(SQLite/PG 공용)."""
        now = _now()
        with self.tx() as c:
            cur = c.execute(
                "UPDATE email_verifications SET used_at=?"
                " WHERE token_hash=? AND used_at IS NULL AND expires_at>?",
                (_iso(now), token_hash, _iso(now)),
            )
            if int(getattr(cur, "rowcount", 0) or 0) <= 0:
                return None
            r = c.execute(
                "SELECT email FROM email_verifications WHERE token_hash=?", (token_hash,)
            ).fetchone()
            return str(r["email"]) if r else None

    def mark_verification_used(self, token_hash: str) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE email_verifications SET used_at=? WHERE token_hash=? AND used_at IS NULL",
                (_iso(_now()), token_hash),
            )

    def delete_expired_verifications(self, now: datetime | None = None) -> int:
        """만료되었거나 이미 사용된 인증 토큰 행 삭제 — 누적 방지용.

        used 행도 함께 지운다(재사용은 어차피 불가). 최신 발급 시각 기반
        재발송 rate limit은 삭제 전에 평가되므로 영향 없음.
        반환값: 삭제된 행 수."""
        cutoff = _iso(now or _now())
        with self.tx() as c:
            cur = c.execute(
                "DELETE FROM email_verifications WHERE expires_at < ? OR used_at IS NOT NULL",
                (cutoff,),
            )
            return int(getattr(cur, "rowcount", 0) or 0)

    def latest_verification_at(self, email: str) -> datetime | None:
        """해당 이메일의 가장 최근 인증 토큰 발급 시각 — 재발송 rate limit용."""
        r = self._conn.execute(
            "SELECT created_at FROM email_verifications WHERE email=?"
            " ORDER BY created_at DESC LIMIT 1",
            (email.lower(),),
        ).fetchone()
        return _dt(r["created_at"]) if r else None

    def set_user_email_verified(self, user_id: str) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE users SET email_verified=1, updated_at=? WHERE id=?",
                (_iso(_now()), user_id),
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

        limit = _clamp_limit(limit, 30, 200)

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
        params.append(_clamp_limit(limit, 50, 500))
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
            (tool, session_key, _clamp_limit(limit, 100, 500)),
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

    def session_work_seconds(self, since_iso: str) -> dict[str, Any]:
        """since 이후 윈도우의 tool/project별 세션 통계 (인사이트 뷰용).

        agent_session_events를 (tool, session_key)별로 시간순 정렬해
        agent_session_stats와 동일한 간격 추정으로 working_seconds를 계산한다
        (working 이벤트 → 다음 이벤트, 최대 _WORKING_GAP_CAP_SECONDS 캡,
        마지막 working 이벤트는 현재 시각까지). 윈도우 이전 이벤트는 로드하지
        않으므로 경계에서 시작된 working 구간은 누락될 수 있다(단순화).

        반환:
          tools:    {tool: {sessions, working_seconds, decisions}}
          projects: {project: {sessions, working_seconds}}
          total_sessions: 윈도우 내 이벤트가 있는 고유 (tool, session_key) 수
        project는 이벤트 값 우선, 없으면 agent_sessions의 현재 project,
        둘 다 없으면 "(no project)". 세션 수는 tool/project별 고유
        session_key 수 — 프로젝트를 오간 세션은 각 버킷에 모두 집계된다."""
        rows = [
            dict(r)
            for r in self._conn.execute(
                "SELECT tool,session_key,project,status,event_at,created_at"
                " FROM agent_session_events WHERE created_at >= ?"
                " ORDER BY tool, session_key, created_at, id",
                (since_iso,),
            ).fetchall()
        ]
        sess_project = {
            (r["tool"], r["session_key"]): r["project"]
            for r in self._conn.execute(
                "SELECT tool,session_key,project FROM agent_sessions"
            ).fetchall()
        }

        def project_of(tool: str, session_key: str, event_project: Any) -> str:
            return str(event_project or sess_project.get((tool, session_key)) or "(no project)")

        tools: dict[str, dict[str, Any]] = {}
        projects: dict[str, dict[str, Any]] = {}
        tool_sessions: dict[str, set[str]] = {}
        proj_sessions: dict[str, set[str]] = {}
        all_sessions: set[tuple[str, str]] = set()

        def t_bucket(tool: str) -> dict[str, Any]:
            return tools.setdefault(tool, {"sessions": 0, "working_seconds": 0.0, "decisions": 0})

        def p_bucket(project: str) -> dict[str, Any]:
            return projects.setdefault(project, {"sessions": 0, "working_seconds": 0.0})

        def add_work(tool: str, project: str, seconds: float) -> None:
            t_bucket(tool)["working_seconds"] += seconds
            p_bucket(project)["working_seconds"] += seconds

        now_dt = _now()
        prev_key: tuple[str, str] | None = None
        prev_t: datetime | None = None
        prev_status = ""
        prev_project = ""
        last_working: dict[tuple[str, str], tuple[datetime, str, str]] = {}
        for r in rows:
            tool = str(r["tool"])
            skey = str(r["session_key"])
            key = (tool, skey)
            project = project_of(tool, skey, r["project"])
            st = str(r["status"] or "")
            all_sessions.add(key)
            tool_sessions.setdefault(tool, set()).add(skey)
            proj_sessions.setdefault(project, set()).add(skey)
            if st == "decision":
                t_bucket(tool)["decisions"] += 1
            t = _dt(r["event_at"]) or _dt(r["created_at"])
            if t is not None and t.tzinfo is None:
                t = t.replace(tzinfo=UTC)
            if prev_key == key and prev_status == "working" and t and prev_t:
                gap = (t - prev_t).total_seconds()
                if gap > 0:
                    add_work(tool, prev_project, min(gap, self._WORKING_GAP_CAP_SECONDS))
            prev_key, prev_t, prev_status, prev_project = key, t, st, project
            if t is not None:
                if st == "working":
                    last_working[key] = (t, tool, project)
                else:
                    last_working.pop(key, None)
        # 마지막 이벤트가 working인 세션은 현재 시각까지의 간격을 추정에 포함
        for _key, (t, tool, project) in last_working.items():
            gap = (now_dt - t).total_seconds()
            if gap > 0:
                add_work(tool, project, min(gap, self._WORKING_GAP_CAP_SECONDS))

        for tool, b in tools.items():
            b["sessions"] = len(tool_sessions.get(tool, ()))
            b["working_seconds"] = int(b["working_seconds"])
        for project, b in projects.items():
            b["sessions"] = len(proj_sessions.get(project, ()))
            b["working_seconds"] = int(b["working_seconds"])
        return {
            "tools": tools,
            "projects": projects,
            "total_sessions": len(all_sessions),
        }

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

    def claim_schedule_fire(
        self,
        schedule_id: str,
        expected_next_run_at: str,
        last_run_at: str,
        next_run_at: str,
    ) -> bool:
        """스케줄 발사 원자 claim — next_run_at compare-and-swap.

        due_schedules()로 읽은 행의 next_run_at이 그대로일 때만 last/next를
        갱신한다. automation loop과 cron tick(또는 다중 인스턴스)이 동시에
        같은 스케줄을 발사해 run을 중복 생성하는 것을 막는다.
        rowcount>0이면 claim 성공 — 호출자는 run 생성으로 진행한다."""
        with self.tx() as c:
            cur = c.execute(
                "UPDATE workflow_schedules SET last_run_at=?, next_run_at=?"
                " WHERE id=? AND enabled=1 AND next_run_at=?",
                (last_run_at, next_run_at, schedule_id, expected_next_run_at),
            )
        return cur.rowcount > 0

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

    def llm_stats(self, days: int, monthly_budget: int | None = None) -> dict[str, Any]:
        """Agent Insights 대시보드 집계 — run 지연시간 백분위, 토큰, 모델/프롬프트
        버전, stage 호출 수, 오류율, 승인 백로그, 일별 시계열, 최근 실패 run,
        stage 병목·모델 라우팅·재시도 낭비·워크플로 단가·월 예산 사용량.
        (블로그 권고: p95 latency, avg tokens, tool call count, retrieval hit rate
        대체 지표로 stage call·실패율, eval pass rate 대체로 승인 대기 수)"""
        days = max(1, min(int(days), 365))
        cutoff = _iso(_now() - timedelta(days=days))
        run_rows = self._conn.execute(
            "SELECT id,workflow_key,status,created_at,updated_at,error,project,trace_id"
            " FROM runs WHERE created_at >= ?",
            (cutoff,),
        ).fetchall()
        stage_rows = self._conn.execute(
            "SELECT sr.run_id, sr.stage_key, sr.attempt, sr.status, sr.model,"
            " sr.prompt_version, sr.tokens_in, sr.tokens_out, sr.error,"
            " sr.started_at, sr.finished_at"
            " FROM stage_runs sr JOIN runs r ON r.id = sr.run_id"
            " WHERE r.created_at >= ?",
            (cutoff,),
        ).fetchall()

        by_status: dict[str, int] = {}
        durations: list[float] = []
        daily: dict[str, dict[str, int]] = {}
        recent_failures: list[dict[str, Any]] = []
        waiting: list[datetime] = []
        run_workflow: dict[str, str] = {}
        wf_stats: dict[str, dict[str, Any]] = {}
        now_dt = _now()
        for row in run_rows:
            st = row["status"]
            by_status[st] = by_status.get(st, 0) + 1
            wf_key = row["workflow_key"] or "(unknown)"
            run_workflow[row["id"]] = wf_key
            wfs = wf_stats.setdefault(
                wf_key, {"workflow": wf_key, "runs": 0, "failed": 0, "durations": [], "tokens": 0}
            )
            wfs["runs"] += 1
            created = _dt(row["created_at"])
            updated = _dt(row["updated_at"])
            if st in self._STATS_TERMINAL and created and updated:
                dur = max(0.0, (updated - created).total_seconds())
                durations.append(dur)
                wfs["durations"].append(dur)
            if st in self._STATS_FAILED:
                wfs["failed"] += 1
            day = (row["created_at"] or "")[:10]
            if day:
                daily.setdefault(day, {"runs": 0, "tokens": 0, "errors": 0})
                daily[day]["runs"] += 1
                if st in self._STATS_FAILED:
                    daily[day]["errors"] += 1
            if st == "waiting_approval" and updated:
                waiting.append(updated)
            if st in self._STATS_FAILED:
                recent_failures.append(
                    {
                        "id": row["id"],
                        "workflow": row["workflow_key"],
                        "status": st,
                        "project": row["project"],
                        "error": (row["error"] or "")[:300],
                        "updated_at": row["updated_at"],
                        "trace_id": row["trace_id"],
                    }
                )
        recent_failures.sort(key=lambda x: str(x["updated_at"]), reverse=True)

        def pct(vals: list[float], p: float) -> float | None:
            if not vals:
                return None
            s = sorted(vals)
            k = max(0, min(len(s) - 1, round(p / 100 * (len(s) - 1))))
            return round(s[k], 1)

        tokens_in = tokens_out = 0
        stage_calls = stage_failures = 0
        models: dict[str, dict[str, Any]] = {}
        prompt_versions: set[str] = set()
        stage_durations: list[float] = []
        token_runs: set[str] = set()
        month_key = now_dt.strftime("%Y-%m")
        retry_calls = 0
        retry_tokens = 0
        failed_stage_tokens = 0
        attempts_hist: dict[str, int] = {}
        stage_agg: dict[str, dict[str, Any]] = {}
        smodel_agg: dict[tuple[str, str], dict[str, Any]] = {}
        pv_agg: dict[str, dict[str, Any]] = {}
        for row in stage_rows:
            tin = int(row["tokens_in"] or 0)
            tout = int(row["tokens_out"] or 0)
            tok = tin + tout
            tokens_in += tin
            tokens_out += tout
            if tin or tout:
                token_runs.add(row["run_id"])
            stage_calls += 1
            attempt = int(row["attempt"] or 1)
            attempts_hist[str(attempt)] = attempts_hist.get(str(attempt), 0) + 1
            failed = row["status"] == "failed"
            if failed:
                stage_failures += 1
                failed_stage_tokens += tok
            if attempt > 1:
                retry_calls += 1
                retry_tokens += tok
            wf = run_workflow.get(row["run_id"], "(unknown)")
            wfs = wf_stats.setdefault(
                wf, {"workflow": wf, "runs": 0, "failed": 0, "durations": [], "tokens": 0}
            )
            wfs["tokens"] += tok
            model = row["model"] or "(unknown)"
            m = models.setdefault(model, {"model": model, "calls": 0, "tokens": 0})
            m["calls"] += 1
            m["tokens"] += tok
            skey = row["stage_key"] or "(unknown)"
            sa = stage_agg.setdefault(
                skey,
                {
                    "stage_key": skey,
                    "calls": 0,
                    "failures": 0,
                    "tokens": 0,
                    "durations": [],
                    "models": {},
                },
            )
            sa["calls"] += 1
            sa["tokens"] += tok
            if failed:
                sa["failures"] += 1
            sm = sa["models"].setdefault(
                model, {"model": model, "calls": 0, "tokens": 0, "failures": 0}
            )
            sm["calls"] += 1
            sm["tokens"] += tok
            if failed:
                sm["failures"] += 1
            smk = (skey, model)
            sma = smodel_agg.setdefault(
                smk, {"stage_key": skey, "model": model, "calls": 0, "tokens": 0, "failures": 0}
            )
            sma["calls"] += 1
            sma["tokens"] += tok
            if failed:
                sma["failures"] += 1
            pv = row["prompt_version"] or ""
            if pv:
                prompt_versions.add(pv)
                pa = pv_agg.setdefault(pv, {"version": pv, "calls": 0, "tokens": 0, "failures": 0})
                pa["calls"] += 1
                pa["tokens"] += tok
                if failed:
                    pa["failures"] += 1
            st, ft = _dt(row["started_at"]), _dt(row["finished_at"])
            if st and ft:
                dur = max(0.0, (ft - st).total_seconds())
                stage_durations.append(dur)
                sa["durations"].append(dur)
            day = (row["started_at"] or "")[:10]
            if day and day in daily:
                daily[day]["tokens"] += tok

        terminal = sum(by_status.get(s, 0) for s in self._STATS_TERMINAL)
        failed = sum(by_status.get(s, 0) for s in self._STATS_FAILED)
        total_tokens = tokens_in + tokens_out
        today = _now().date()
        start = today - timedelta(days=days - 1)
        out_daily: list[dict[str, Any]] = []
        d = start
        while d <= today:
            key = d.isoformat()
            b = daily.get(key, {"runs": 0, "tokens": 0, "errors": 0})
            out_daily.append(
                {"date": key, "runs": b["runs"], "tokens": b["tokens"], "errors": b["errors"]}
            )
            d += timedelta(days=1)

        model_list = sorted(models.values(), key=lambda x: -x["tokens"])[:10]
        token_sum = sum(m["tokens"] for m in model_list)
        for m in model_list:
            m["share_pct"] = round(m["tokens"] / token_sum * 100, 1) if token_sum else 0

        # 월 예산 — 윈도우와 무관하게 당월 전체 stage 토큰 합산
        month_start = now_dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        mrow = self._conn.execute(
            "SELECT COALESCE(SUM(sr.tokens_in + sr.tokens_out),0) AS t"
            " FROM stage_runs sr WHERE sr.started_at >= ?",
            (_iso(month_start),),
        ).fetchone()
        month_tokens = int(mrow["t"] or 0) if mrow else 0
        budget = None
        if monthly_budget:
            used_pct = round(month_tokens / monthly_budget * 100, 1)
            budget = {
                "month": month_key,
                "monthly_tokens": month_tokens,
                "budget_max": monthly_budget,
                "used_pct": used_pct,
                "alert": used_pct >= 80,
            }

        by_stage = []
        for sa in stage_agg.values():
            durs = sa.pop("durations")
            models_by_stage = sorted(sa["models"].values(), key=lambda x: -x["tokens"])[:5]
            by_stage.append(
                {
                    "stage_key": sa["stage_key"],
                    "calls": sa["calls"],
                    "failures": sa["failures"],
                    "failure_rate_pct": round(sa["failures"] / sa["calls"] * 100, 1)
                    if sa["calls"]
                    else None,
                    "tokens": sa["tokens"],
                    "latency_s": {
                        "p50": pct(durs, 50),
                        "p95": pct(durs, 95),
                        "avg": round(sum(durs) / len(durs), 1) if durs else None,
                    },
                    "models": models_by_stage,
                }
            )
        by_stage.sort(key=lambda x: -(x["latency_s"]["p95"] or 0))

        by_workflow = []
        for w in wf_stats.values():
            durs = w["durations"]
            by_workflow.append(
                {
                    "workflow": w["workflow"],
                    "runs": w["runs"],
                    "failed": w["failed"],
                    "avg_duration_s": round(sum(durs) / len(durs), 1) if durs else None,
                    "total_tokens": w["tokens"],
                    "avg_tokens_per_run": round(w["tokens"] / w["runs"], 1) if w["runs"] else None,
                }
            )
        by_workflow.sort(key=lambda x: -x["total_tokens"])

        waiting_total_s = (
            round(sum((now_dt - w).total_seconds() for w in waiting), 0) if waiting else None
        )

        return {
            "days": days,
            "runs_total": len(run_rows),
            "by_status": by_status,
            "terminal_runs": terminal,
            "failed_runs": failed,
            "error_rate_pct": round(failed / terminal * 100, 1) if terminal else None,
            "latency_s": {
                "p50": pct(durations, 50),
                "p95": pct(durations, 95),
                "p99": pct(durations, 99),
                "avg": round(sum(durations) / len(durations), 1) if durations else None,
                "max": round(max(durations), 1) if durations else None,
                "samples": len(durations),
            },
            "stage_latency_s": {
                "p50": pct(stage_durations, 50),
                "p95": pct(stage_durations, 95),
                "avg": round(sum(stage_durations) / len(stage_durations), 1)
                if stage_durations
                else None,
            },
            "tokens": {
                "total_in": tokens_in,
                "total_out": tokens_out,
                "total": total_tokens,
                "avg_per_run": round(total_tokens / len(token_runs), 1) if token_runs else None,
                "runs_with_tokens": len(token_runs),
            },
            "models": model_list,
            "prompt_versions": sorted(prompt_versions),
            "prompt_version_stats": sorted(pv_agg.values(), key=lambda x: -x["tokens"]),
            "stage_model_matrix": [
                {
                    **v,
                    "failure_rate_pct": round(v["failures"] / v["calls"] * 100, 1)
                    if v["calls"]
                    else None,
                }
                for v in sorted(smodel_agg.values(), key=lambda x: -x["tokens"])
            ][:50],
            "by_stage": by_stage,
            "by_workflow": by_workflow,
            "stage_calls": stage_calls,
            "stage_failures": stage_failures,
            "stage_failure_rate_pct": round(stage_failures / stage_calls * 100, 1)
            if stage_calls
            else None,
            "retry": {
                "calls": retry_calls,
                "tokens": retry_tokens,
                "token_pct": round(retry_tokens / total_tokens * 100, 1) if total_tokens else None,
                "failed_stage_tokens": failed_stage_tokens,
                "attempts_hist": dict(sorted(attempts_hist.items(), key=lambda x: int(x[0]))),
            },
            "tokens_ratio": {
                "input_pct": round(tokens_in / total_tokens * 100, 1) if total_tokens else None,
                "output_pct": round(tokens_out / total_tokens * 100, 1) if total_tokens else None,
            },
            "budget": budget,
            "approval_backlog": {
                "waiting": by_status.get("waiting_approval", 0),
                "oldest_wait_s": round((now_dt - min(waiting)).total_seconds(), 0)
                if waiting
                else None,
                "total_wait_s": waiting_total_s,
            },
            "daily": out_daily,
            "recent_failures": recent_failures[:10],
        }

    def cleanup_radar_waits(self) -> int:
        """skill_radar의 waiting_approval 잔여 run을 cancelled 처리하고
        관련 인박스 승인 알림을 삭제한다.

        radar는 모든 실행 경로(daily 자동수집/cron refresh/수동 refresh)에서
        auto_approve=True로 무인 발행되므로, 승인 대기에 남은 run은
        auto_approve 도입 이전의 비정상 잔여물이다."""
        now = _iso(_now())
        with self.tx() as c:
            rows = c.execute(
                "SELECT id FROM runs WHERE workflow_key='skill_radar' AND status='waiting_approval'"
            ).fetchall()
            if not rows:
                return 0
            for r in rows:
                c.execute(
                    "UPDATE runs SET status='cancelled', error=?, updated_at=? WHERE id=?",
                    ("radar_auto_cleanup: approval not required", now, r["id"]),
                )
            c.execute(
                "DELETE FROM inbox_items WHERE kind='approval' AND title LIKE '%skill_radar%'"
            )
            return len(rows)

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
        rows = self._conn.execute(sql, (_clamp_limit(limit, 50, 200),)).fetchall()
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
            (_clamp_limit(limit, 200, 1000),),
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
            (tool, session_key, _clamp_limit(limit, 200, 500)),
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

    def retention_sweep(self, now: datetime | None = None) -> dict[str, int]:
        """보존 기간 경과 데이터 정리 — cron tick에서 하루 1회 호출 권장.

        - audit_events: created_at < now-90d 삭제
        - llm_usage: created_at < now-180d 삭제
        - auth_sessions: expires_at < now 삭제(만료 세션)
        - runs/stage_runs: completed|failed 이고 created_at < now-180d 삭제
          (진행 중 queued/running/waiting_* 는 절대 삭제하지 않는다)
        - message_extractions: processed 이고 created_at < now-30d면
          source_text만 비움(행은 유지 — 재처리 방지용 키/상태 보존)
        - agent_session_events: created_at < now-30d 삭제
        반환값: 테이블별 삭제/정리 건수."""
        now = now or _now()
        d30 = _iso(now - timedelta(days=30))
        d90 = _iso(now - timedelta(days=90))
        d180 = _iso(now - timedelta(days=180))
        now_iso = _iso(now)
        counts: dict[str, int] = {}
        with self.tx() as c:
            cur = c.execute("DELETE FROM audit_events WHERE created_at < ?", (d90,))
            counts["audit_events"] = int(getattr(cur, "rowcount", 0) or 0)
            cur = c.execute("DELETE FROM llm_usage WHERE created_at < ?", (d180,))
            counts["llm_usage"] = int(getattr(cur, "rowcount", 0) or 0)
            cur = c.execute("DELETE FROM auth_sessions WHERE expires_at < ?", (now_iso,))
            counts["auth_sessions"] = int(getattr(cur, "rowcount", 0) or 0)
            cur = c.execute("DELETE FROM agent_session_events WHERE created_at < ?", (d30,))
            counts["agent_session_events"] = int(getattr(cur, "rowcount", 0) or 0)
            cur = c.execute(
                "UPDATE message_extractions SET source_text=''"
                " WHERE processing_status='processed' AND created_at < ? AND source_text <> ''",
                (d30,),
            )
            counts["message_extractions_cleared"] = int(getattr(cur, "rowcount", 0) or 0)
            # 종결 run만 삭제 — 자식(stage_runs/approvals/run_comments)부터 정리.
            # IN 리스트 대신 동일 조건의 서브쿼리로 지워 바인딩 변수 수를
            # 일정하게 유지한다(삭제 대상 수와 무관하게 단일 문장).
            cur = c.execute(
                "DELETE FROM stage_runs WHERE run_id IN "
                "(SELECT id FROM runs WHERE status IN ('completed','failed')"
                " AND created_at < ?)",
                (d180,),
            )
            counts["stage_runs"] = int(getattr(cur, "rowcount", 0) or 0)
            cur = c.execute(
                "DELETE FROM approvals WHERE run_id IN "
                "(SELECT id FROM runs WHERE status IN ('completed','failed')"
                " AND created_at < ?)",
                (d180,),
            )
            counts["approvals"] = int(getattr(cur, "rowcount", 0) or 0)
            cur = c.execute(
                "DELETE FROM run_comments WHERE run_id IN "
                "(SELECT id FROM runs WHERE status IN ('completed','failed')"
                " AND created_at < ?)",
                (d180,),
            )
            counts["run_comments"] = int(getattr(cur, "rowcount", 0) or 0)
            cur = c.execute(
                "DELETE FROM runs WHERE status IN ('completed','failed') AND created_at < ?",
                (d180,),
            )
            counts["runs"] = int(getattr(cur, "rowcount", 0) or 0)
        return counts

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
        out = []
        for r in rows:
            d = dict(r)
            d["role"] = self._norm_role(d.get("role"))
            out.append(d)
        return out

    def set_user_role(self, user_id: str, role: str) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE users SET role=?, updated_at=? WHERE id=?",
                (role, _iso(_now()), user_id),
            )

    def set_user_password(self, user_id: str, password_hash: str) -> None:
        """비밀번호 해시 교체 — 호출 측에서 현재 비밀번호 검증 + 세션 revoke 책임."""
        with self.tx() as c:
            c.execute(
                "UPDATE users SET password_hash=?, updated_at=? WHERE id=?",
                (password_hash, _iso(_now()), user_id),
            )

    def promote_to_admin_if_first(self, user_id: str) -> bool:
        """사용자가 유일한 사용자일 때만 admin 승격 — 단일 UPDATE 조건으로
        동시 가입 레이스에서 둘 다 admin이 되거나 둘 다 놓치는 것을 막는다.
        반환값: 실제로 승격됐으면 True."""
        with self.tx() as c:
            cur = c.execute(
                "UPDATE users SET role='admin', updated_at=?"
                " WHERE id=? AND (SELECT COUNT(*) FROM users)=1",
                (_iso(_now()), user_id),
            )
            return int(getattr(cur, "rowcount", 0) or 0) > 0

    def delete_user_cascade(self, user_id: str) -> dict[str, int]:
        """계정 삭제(개인정보보호법 대응) — users + auth_sessions +
        email_verifications + 본인 귀속 데이터(run_comments)를 정리한다.

        audit_events는 법적 보존이 필요할 수 있으므로 삭제하지 않고
        actor만 'deleted_user'로 마스킹해 유지한다. 호출 측에서 삭제 행위를
        audit에 먼저 기록해야 한다(마스킹 후에도 action은 남는다).
        타 사용자 데이터는 절대 건드리지 않는다.
        반환값: 테이블별 삭제 건수."""
        user = self.get_user_by_id(user_id)
        email = str((user or {}).get("email") or "").lower()
        counts: dict[str, int] = {}
        with self.tx() as c:
            cur = c.execute("DELETE FROM auth_sessions WHERE user_id=?", (user_id,))
            counts["auth_sessions"] = int(getattr(cur, "rowcount", 0) or 0)
            if email:
                cur = c.execute("DELETE FROM email_verifications WHERE email=?", (email,))
                counts["email_verifications"] = int(getattr(cur, "rowcount", 0) or 0)
                c.execute(
                    "UPDATE audit_events SET actor='deleted_user' WHERE actor=? OR lower(actor)=?",
                    (user_id, email),
                )
                c.execute(
                    "DELETE FROM run_comments WHERE user_id=? OR lower(email)=?",
                    (user_id, email),
                )
            cur = c.execute("DELETE FROM users WHERE id=?", (user_id,))
            counts["users"] = int(getattr(cur, "rowcount", 0) or 0)
        return counts

    def export_user_data(self, user_id: str) -> dict[str, Any]:
        """본인 데이터 export(개인정보 열람권) — 비밀번호 해시·세션 토큰
        해시·인증 토큰 해시는 제외하고 본인 귀속 데이터만 반환한다."""
        user = self.get_user_by_id(user_id) or {}
        email = str(user.get("email") or "").lower()
        with self.tx() as c:
            sess_rows = c.execute(
                "SELECT created_at, expires_at, revoked_at FROM auth_sessions"
                " WHERE user_id=? ORDER BY created_at DESC",
                (user_id,),
            ).fetchall()
            audit_rows = c.execute(
                "SELECT run_id,actor,action,object_ref,detail,created_at"
                " FROM audit_events WHERE actor=? OR lower(actor)=?"
                " ORDER BY created_at DESC LIMIT 1000",
                (user_id, email),
            ).fetchall()
            comment_rows = c.execute(
                "SELECT run_id,body,created_at FROM run_comments"
                " WHERE user_id=? OR lower(email)=? ORDER BY created_at DESC LIMIT 1000",
                (user_id, email),
            ).fetchall()
            verify_rows = c.execute(
                "SELECT email,expires_at,used_at,created_at FROM email_verifications"
                " WHERE email=? ORDER BY created_at DESC",
                (email,),
            ).fetchall()
        safe_user = {k: v for k, v in user.items() if k != "password_hash"}
        return {
            "exported_at": _iso(_now()),
            "user": safe_user,
            "sessions": [dict(r) for r in sess_rows],
            "audit_events": [
                {
                    "run_id": r["run_id"],
                    "actor": r["actor"],
                    "action": r["action"],
                    "target": r["object_ref"],
                    "detail": _loads(r["detail"]),
                    "created_at": r["created_at"],
                }
                for r in audit_rows
            ],
            "run_comments": [dict(r) for r in comment_rows],
            "email_verifications": [dict(r) for r in verify_rows],
        }

    # ---- playbooks (원클릭 작업 템플릿) ----
    @staticmethod
    def _playbook_json(r: Any) -> dict[str, Any]:
        d = dict(r)
        d["input"] = _loads(d.get("input") or "{}")
        return d

    def create_playbook(
        self, name: str, workflow: str, input_data: dict[str, Any], created_by: str = ""
    ) -> dict[str, Any]:
        pid = uuid.uuid4().hex[:12]
        now = _iso(_now())
        with self.tx() as c:
            c.execute(
                "INSERT INTO playbooks(id,name,workflow,input,created_by,created_at)"
                " VALUES(?,?,?,?,?,?)",
                (pid, name, workflow, json.dumps(input_data or {}), created_by, now),
            )
        pb = self.get_playbook(pid)
        return pb if pb is not None else {"id": pid}

    def list_playbooks(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM playbooks ORDER BY created_at DESC").fetchall()
        return [self._playbook_json(r) for r in rows]

    def get_playbook(self, playbook_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM playbooks WHERE id=?", (playbook_id,)).fetchone()
        return self._playbook_json(row) if row else None

    def delete_playbook(self, playbook_id: str) -> bool:
        with self.tx() as c:
            cur = c.execute("DELETE FROM playbooks WHERE id=?", (playbook_id,))
        return cur.rowcount > 0

    # ---- run_comments (run/diff 토론) ----
    def add_run_comment(self, run_id: str, user_id: str, email: str, body: str) -> dict[str, Any]:
        cid = uuid.uuid4().hex[:12]
        now = _iso(_now())
        with self.tx() as c:
            c.execute(
                "INSERT INTO run_comments(id,run_id,user_id,email,body,created_at)"
                " VALUES(?,?,?,?,?,?)",
                (cid, run_id, user_id, email, body, now),
            )
        return {
            "id": cid,
            "run_id": run_id,
            "user_id": user_id,
            "email": email,
            "body": body,
            "created_at": now,
        }

    def list_run_comments(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT id,run_id,user_id,email,body,created_at FROM run_comments"
            " WHERE run_id=? ORDER BY created_at, id",
            (run_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- connectors (외부 데이터 소스) ----
    @staticmethod
    def _connector_json(r: Any, *, with_config: bool = False) -> dict[str, Any]:
        d = dict(r)
        d["enabled"] = bool(d.get("enabled"))
        d["has_token"] = bool(d.get("has_token"))
        if with_config:
            d["config"] = _loads(d.get("config") or "{}")
        else:
            d.pop("config", None)
        return d

    def create_connector(
        self,
        ctype: str,
        name: str,
        config: dict[str, Any],
        enabled: bool = True,
        has_token: bool = False,
    ) -> dict[str, Any]:
        cid = uuid.uuid4().hex[:12]
        now = _iso(_now())
        with self.tx() as c:
            c.execute(
                "INSERT INTO connectors(id,type,name,config,enabled,has_token,"
                "last_fetch_at,last_error,created_at) VALUES(?,?,?,?,?,?,NULL,'',?)",
                (
                    cid,
                    ctype,
                    name,
                    json.dumps(config or {}, ensure_ascii=False),
                    1 if enabled else 0,
                    int(has_token),
                    now,
                ),
            )
        conn = self.get_connector(cid)
        if conn is not None:
            conn.pop("config", None)  # 생성 응답에도 config(암호문 포함) 비노출
        return conn if conn is not None else {"id": cid}

    def list_connectors(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM connectors ORDER BY created_at DESC").fetchall()
        return [self._connector_json(r) for r in rows]

    def get_connector(self, connector_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM connectors WHERE id=?", (connector_id,)).fetchone()
        return self._connector_json(row, with_config=True) if row else None

    def delete_connector(self, connector_id: str) -> bool:
        with self.tx() as c:
            cur = c.execute("DELETE FROM connectors WHERE id=?", (connector_id,))
        return cur.rowcount > 0

    def set_connector_enabled(self, connector_id: str, enabled: bool) -> dict[str, Any] | None:
        conn = self.get_connector(connector_id)
        if conn is None:
            return None
        with self.tx() as c:
            c.execute(
                "UPDATE connectors SET enabled=? WHERE id=?",
                (1 if enabled else 0, connector_id),
            )
        return self.get_connector(connector_id)

    def mark_connector_fetch(self, connector_id: str, error: str = "") -> None:
        """fetch 시도 결과 기록 — last_fetch_at은 성공/실패 무관하게 갱신."""
        with self.tx() as c:
            c.execute(
                "UPDATE connectors SET last_fetch_at=?, last_error=? WHERE id=?",
                (_iso(_now()), error[:300], connector_id),
            )

    # ---- custom_workflows (비주얼 빌더 WorkflowSpec) ----
    def upsert_custom_workflow(self, name: str, spec: dict[str, Any]) -> dict[str, Any]:
        now = _iso(_now())
        with self.tx() as c:
            c.execute(
                "INSERT INTO custom_workflows(name,spec,created_at) VALUES(?,?,?)"
                " ON CONFLICT(name) DO UPDATE SET spec=excluded.spec",
                (name, json.dumps(spec, ensure_ascii=False), now),
            )
        return {"name": name, "spec": spec, "created_at": now}

    def list_custom_workflows(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT name,spec,created_at FROM custom_workflows ORDER BY name"
        ).fetchall()
        out = []
        for r in rows:
            spec = _loads(r["spec"])
            out.append({"name": r["name"], "spec": spec, "created_at": r["created_at"]})
        return out

    def get_custom_workflow(self, name: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT name,spec,created_at FROM custom_workflows WHERE name=?", (name,)
        ).fetchone()
        if not row:
            return None
        return {"name": row["name"], "spec": _loads(row["spec"]), "created_at": row["created_at"]}

    def delete_custom_workflow(self, name: str) -> bool:
        with self.tx() as c:
            cur = c.execute("DELETE FROM custom_workflows WHERE name=?", (name,))
        return cur.rowcount > 0

    # ---- triggers (세션 상태 전이 자동화) ----
    @staticmethod
    def _trigger_json(r: Any) -> dict[str, Any]:
        d = dict(r)
        d["enabled"] = bool(d.get("enabled"))
        d["match"] = _loads(d.get("match") or "{}")
        d["action"] = _loads(d.get("action") or "{}")
        return d

    def create_trigger(
        self, match: dict[str, Any], action: dict[str, Any], enabled: bool = True
    ) -> dict[str, Any]:
        tid = uuid.uuid4().hex[:12]
        now = _iso(_now())
        with self.tx() as c:
            c.execute(
                "INSERT INTO triggers(id,enabled,match,action,created_at,"
                "last_fired_at,last_error) VALUES(?,?,?,?,?,NULL,'')",
                (
                    tid,
                    1 if enabled else 0,
                    json.dumps(match or {}, ensure_ascii=False),
                    json.dumps(action or {}, ensure_ascii=False),
                    now,
                ),
            )
        trig = self.get_trigger(tid)
        return trig if trig is not None else {"id": tid}

    def list_triggers(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM triggers"
        if enabled_only:
            sql += " WHERE enabled=1"
        sql += " ORDER BY created_at DESC"
        rows = self._conn.execute(sql).fetchall()
        return [self._trigger_json(r) for r in rows]

    def get_trigger(self, trigger_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM triggers WHERE id=?", (trigger_id,)).fetchone()
        return self._trigger_json(row) if row else None

    def delete_trigger(self, trigger_id: str) -> bool:
        with self.tx() as c:
            cur = c.execute("DELETE FROM triggers WHERE id=?", (trigger_id,))
            c.execute("DELETE FROM trigger_fires WHERE trigger_id=?", (trigger_id,))
        return cur.rowcount > 0

    def set_trigger_enabled(self, trigger_id: str, enabled: bool) -> dict[str, Any] | None:
        trig = self.get_trigger(trigger_id)
        if trig is None:
            return None
        with self.tx() as c:
            c.execute(
                "UPDATE triggers SET enabled=? WHERE id=?",
                (1 if enabled else 0, trigger_id),
            )
        return self.get_trigger(trigger_id)

    def claim_trigger_fire(
        self,
        trigger_id: str,
        tool: str,
        session_key: str,
        state: str,
        fp: str,
    ) -> bool:
        """trigger+session+state+fp 조합의 발사권 선점 — 최초 1회만 True.

        fp는 상태 전이 지문(세션 updated_at 등). 같은 전이의 반복 평가는
        UNIQUE 충돌로 False를 반환해 중복 발사를 막는다."""
        fid = uuid.uuid4().hex[:12]
        with self.tx() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO trigger_fires"
                "(id,trigger_id,tool,session_key,state,fp,fired_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (fid, trigger_id, tool, session_key, state, fp, _iso(_now())),
            )
            return cur.rowcount > 0

    def mark_trigger_error(self, trigger_id: str, error: str) -> None:
        """발사 실패 기록 — last_error만 갱신(last_fired_at은 유지)."""
        with self.tx() as c:
            c.execute(
                "UPDATE triggers SET last_error=? WHERE id=?",
                (error[:300], trigger_id),
            )

    def mark_trigger_fired(self, trigger_id: str) -> None:
        """발사 성공 기록 — last_fired_at 갱신 + last_error 클리어."""
        with self.tx() as c:
            c.execute(
                "UPDATE triggers SET last_fired_at=?, last_error='' WHERE id=?",
                (_iso(_now()), trigger_id),
            )

    # ---- share_links (공개 스냅샷 공유) ----
    def create_share_link(
        self,
        token: str,
        kind: str,
        ref_id: str,
        payload: dict[str, Any],
        created_by: str,
        expires_at: str,
    ) -> None:
        now = _iso(_now())
        with self.tx() as c:
            c.execute(
                "INSERT INTO share_links(token,kind,ref_id,payload,created_by,"
                "expires_at,created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    token,
                    kind,
                    ref_id,
                    json.dumps(payload, ensure_ascii=False, default=str),
                    created_by,
                    expires_at,
                    now,
                ),
            )

    def get_share_link(self, token: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT token,kind,ref_id,payload,created_by,expires_at,created_at"
            " FROM share_links WHERE token=?",
            (token,),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["payload"] = _loads(d.get("payload") or "{}")
        return d

    # ---- dashboard_cards (사용자 등록 JSON 위젯) ----
    def create_card(
        self,
        title: str,
        url: str,
        json_path: str = "",
        enabled: bool = True,
        created_by: str = "",
    ) -> dict[str, Any]:
        cid = uuid.uuid4().hex[:12]
        now = _iso(_now())
        with self.tx() as c:
            c.execute(
                "INSERT INTO dashboard_cards(id,title,url,json_path,enabled,"
                "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                (cid, title, url, json_path, 1 if enabled else 0, created_by, now),
            )
        card = self.get_card(cid)
        return card if card is not None else {"id": cid}

    def list_cards(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM dashboard_cards ORDER BY created_at DESC"
        ).fetchall()
        return [self._card_json(r) for r in rows]

    @staticmethod
    def _card_json(r: Any) -> dict[str, Any]:
        d = dict(r)
        d["enabled"] = bool(d.get("enabled"))
        return d

    def get_card(self, card_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM dashboard_cards WHERE id=?", (card_id,)).fetchone()
        return self._card_json(row) if row else None

    def delete_card(self, card_id: str) -> bool:
        with self.tx() as c:
            cur = c.execute("DELETE FROM dashboard_cards WHERE id=?", (card_id,))
        return cur.rowcount > 0

    def set_card_enabled(self, card_id: str, enabled: bool) -> dict[str, Any] | None:
        card = self.get_card(card_id)
        if card is None:
            return None
        with self.tx() as c:
            c.execute(
                "UPDATE dashboard_cards SET enabled=? WHERE id=?",
                (1 if enabled else 0, card_id),
            )
        return self.get_card(card_id)

    # ---- 감사 로그 뷰 (audit_events 전역 조회) ----
    def list_audit(self, limit: int = 200) -> list[dict[str, Any]]:
        """전체 audit_events 최신순 — 관리자 감사 타임라인용."""
        rows = self._conn.execute(
            "SELECT id,run_id,actor,action,object_ref,detail,created_at"
            " FROM audit_events ORDER BY created_at DESC, id DESC LIMIT ?",
            (_clamp_limit(limit, 200, 1000),),
        ).fetchall()
        return [
            {
                "id": r["id"],
                "run_id": r["run_id"],
                "actor": r["actor"],
                "action": r["action"],
                "target": r["object_ref"],
                "detail": _loads(r["detail"]),
                "ts": r["created_at"],
            }
            for r in rows
        ]

    # ---- team 뷰 (다중 사용자 활동 집계) ----
    def team_activity(self) -> list[dict[str, Any]]:
        """users × runs.created_by × auth_sessions 집계.

        created_by는 세션 사용자 email(없으면 'api-key'/'local' 등 시스템 값) —
        email/id 어느 쪽으로 기록됐든 매칭한다."""
        users = self.list_users()
        run_rows = self._conn.execute(
            "SELECT created_by, COUNT(*) AS n, MAX(created_at) AS last_at"
            " FROM runs WHERE created_by != '' GROUP BY created_by"
        ).fetchall()
        sess_rows = self._conn.execute(
            "SELECT user_id, COUNT(*) AS n, MAX(created_at) AS last_at"
            " FROM auth_sessions GROUP BY user_id"
        ).fetchall()
        run_by_actor = {r["created_by"]: dict(r) for r in run_rows}
        sess_by_user = {r["user_id"]: dict(r) for r in sess_rows}
        items = []
        for u in users:
            uid, email = u["id"], u["email"]
            rr = run_by_actor.get(email) or run_by_actor.get(uid)
            sr = sess_by_user.get(uid)
            last_active = ""
            for cand in (
                (rr or {}).get("last_at"),
                (sr or {}).get("last_at"),
            ):
                if cand and cand > last_active:
                    last_active = cand
            items.append(
                {
                    "id": uid,
                    "email": email,
                    "name": u.get("display_name", ""),
                    "role": Store._norm_role(u.get("role", "user")),
                    "run_count": int((rr or {}).get("n") or 0),
                    "session_count": int((sr or {}).get("n") or 0),
                    "last_active_at": last_active or None,
                }
            )
        items.sort(key=lambda x: (x["last_active_at"] or "", x["email"]), reverse=True)
        return items

    # ---- export 헬퍼 ----
    def export_runs(self, since_iso: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT id,workflow_key,workflow_version,status,trace_id,error,"
            "created_by,created_at,updated_at FROM runs WHERE created_at >= ?"
            " ORDER BY created_at",
            (since_iso,),
        ).fetchall()
        return [dict(r) for r in rows]

    def export_sessions(self, since_iso: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT tool,session_key,project,title,status,machine,"
            "tokens_in,tokens_out,last_event_at,created_at,updated_at"
            " FROM agent_sessions WHERE updated_at >= ? ORDER BY updated_at",
            (since_iso,),
        ).fetchall()
        return [dict(r) for r in rows]

    def export_reports(self, since_iso: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT id,kind,content,created_at FROM reports WHERE created_at >= ?"
            " ORDER BY created_at",
            (since_iso,),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["content"] = _loads(d.get("content") or "{}")
            out.append(d)
        return out

    def get_report(self, report_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT id,kind,content,created_at FROM reports WHERE id=?", (report_id,)
        ).fetchone()
        if not row:
            return None
        return {
            "id": row["id"],
            "kind": row["kind"],
            "content": _loads(row["content"]),
            "created_at": row["created_at"],
        }

    # ---- projects (팀장 프로젝트 마스터/태깅) ----
    @staticmethod
    def _project_json(r: Any) -> dict[str, Any]:
        d = dict(r)
        try:
            d["target_count"] = int(d.get("target_count") or 0)
        except (TypeError, ValueError):
            d["target_count"] = 0
        return d

    def create_project(
        self,
        name: str,
        color: str = "",
        description: str = "",
        goal: str = "",
        target_count: int = 0,
        created_by: str = "",
    ) -> dict[str, Any] | None:
        """프로젝트 생성. name은 trim 후 lower() 기준 중복 시 None."""
        name = name.strip()[:200]
        if not name:
            return None
        if self.find_project_by_name(name) is not None:
            return None
        pid = uuid.uuid4().hex[:12]
        now = _iso(_now())
        try:
            with self.tx() as c:
                c.execute(
                    "INSERT INTO projects(id,name,color,description,goal,target_count,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        pid,
                        name,
                        color.strip()[:32],
                        description.strip()[:2000],
                        goal.strip()[:500],
                        int(target_count or 0),
                        created_by[:200],
                        now,
                    ),
                )
        except self._integrity_errors:
            return None
        return self.get_project(pid)

    def list_projects(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM projects ORDER BY lower(name), name").fetchall()
        return [self._project_json(r) for r in rows]

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        return self._project_json(row) if row else None

    def find_project_by_name(self, name: str) -> dict[str, Any] | None:
        """프로젝트 이름 조회 — trim + case-insensitive."""
        row = self._conn.execute(
            "SELECT * FROM projects WHERE lower(name)=lower(?)",
            (name.strip()[:200],),
        ).fetchone()
        return self._project_json(row) if row else None

    def update_project(self, project_id: str, **fields: Any) -> dict[str, Any] | None:
        """name/color/description/goal/target_count 부분 갱신. name 변경 시 ci 중복 검사."""
        proj = self.get_project(project_id)
        if proj is None:
            return None
        updates: dict[str, Any] = {}
        if "name" in fields:
            new_name = str(fields["name"] or "").strip()[:200]
            if not new_name:
                return None
            other = self.find_project_by_name(new_name)
            if other is not None and other["id"] != project_id:
                return None
            updates["name"] = new_name
        if "color" in fields:
            updates["color"] = str(fields["color"] or "").strip()[:32]
        if "description" in fields:
            updates["description"] = str(fields["description"] or "").strip()[:2000]
        if "goal" in fields:
            updates["goal"] = str(fields["goal"] or "").strip()[:500]
        if "target_count" in fields:
            try:
                updates["target_count"] = max(0, int(fields["target_count"] or 0))
            except (TypeError, ValueError):
                return None
        if not updates:
            return proj
        sets = ", ".join(f"{k}=?" for k in updates)
        try:
            with self.tx() as c:
                c.execute(
                    f"UPDATE projects SET {sets} WHERE id=?",  # noqa: S608 - keys는 고정 허용 목록
                    (*updates.values(), project_id),
                )
        except self._integrity_errors:
            return None
        return self.get_project(project_id)

    def delete_project(self, project_id: str) -> bool:
        """프로젝트 삭제 — 세션/run의 태그 값은 그대로 둔다(고아 태그 허용)."""
        with self.tx() as c:
            cur = c.execute("DELETE FROM projects WHERE id=?", (project_id,))
        return cur.rowcount > 0

    # ---- 프로젝트 태깅 (runs.project / agent_sessions.project) ----
    def set_run_project(self, run_id: str, project: str) -> bool:
        """run의 project 태그 설정. ''이면 태그 해제. updated_at은 건드리지 않는다."""
        with self.tx() as c:
            cur = c.execute(
                "UPDATE runs SET project=? WHERE id=?",
                (project.strip()[:200], run_id),
            )
        return cur.rowcount > 0

    def find_agent_session_by_key(self, session_key: str) -> dict[str, Any] | None:
        """session_key만으로 세션 조회 — 복수 tool이 같은 key를 쓰면 최신 updated_at 우선."""
        row = self._conn.execute(
            "SELECT * FROM agent_sessions WHERE session_key=?"
            " ORDER BY updated_at DESC, last_event_at DESC LIMIT 1",
            (session_key,),
        ).fetchone()
        return dict(row) if row else None

    def set_agent_session_project(
        self, session_key: str, project: str, tool: str | None = None
    ) -> dict[str, Any] | None:
        """세션 project 태그 설정 (PATCH /agent-sessions/{key}).

        tool이 주어지면 (tool, session_key) 정확 매칭, 아니면 최신 세션을 고른다.
        변경 시 agent_session_events에 project 전이 이벤트 1건을 남긴다.
        반환: 갱신된 세션 행 또는 None(세션 없음)."""
        sess = (
            self.get_agent_session(tool, session_key)
            if tool
            else self.find_agent_session_by_key(session_key)
        )
        if sess is None:
            return None
        new_project = project.strip()[:500] if project else ""
        old_project = sess.get("project") or ""
        now = _iso(_now())
        with self.tx() as c:
            c.execute(
                "UPDATE agent_sessions SET project=?, updated_at=? WHERE tool=? AND session_key=?",
                (new_project or None, now, sess["tool"], sess["session_key"]),
            )
            if new_project != old_project:
                c.execute(
                    "INSERT INTO agent_session_events(id,tool,session_key,status,"
                    "project,doing,ask,event_at,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        uuid.uuid4().hex[:12],
                        sess["tool"],
                        sess["session_key"],
                        sess.get("status") or "",
                        new_project or None,
                        sess.get("doing"),
                        sess.get("ask"),
                        now,
                        now,
                    ),
                )
        return self.get_agent_session(sess["tool"], sess["session_key"])

    # ---- 리드 대시보드 집계용 raw row 조회 ----
    def stage_run_rows(self, since_iso: str) -> list[dict[str, Any]]:
        """since 이후 생성된 run의 stage_runs + run 컨텍스트 JOIN.

        리스크(7d 단계 실패)·성과 비교·프로젝트 상세(30d 비용) 집계용.
        반환 dict: stage 필드 + workflow_key/project/run_status/run_created_at/
        run_updated_at."""
        rows = self._conn.execute(
            "SELECT sr.run_id, sr.stage_key, sr.attempt, sr.status, sr.model,"
            " sr.tokens_in, sr.tokens_out, sr.started_at, sr.finished_at, sr.error,"
            " r.workflow_key, r.project, r.status AS run_status,"
            " r.created_at AS run_created_at, r.updated_at AS run_updated_at"
            " FROM stage_runs sr JOIN runs r ON r.id = sr.run_id"
            " WHERE r.created_at >= ? ORDER BY sr.started_at",
            (since_iso,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- SSE 스트림용 증분 조회 ----
    def activity_since(self, since_iso: str, limit: int = 500) -> dict[str, list[dict[str, Any]]]:
        """since_iso(ISO8601, 경계 포함) 이후 갱신된 행을 테이블별로 반환.

        SSE 엔드포인트의 폴링 tailing용. 각 행의 "ts"는 워터마크 비교용
        타임스탬프(runs=updated_at, stages=COALESCE(finished_at,started_at),
        sessions=created_at). 같은 ts의 복수 행은 호출자가 id로 중복 제거한다.
        """
        out: dict[str, list[dict[str, Any]]] = {"runs": [], "stages": [], "sessions": []}
        rows = self._conn.execute(
            "SELECT id, workflow_key, status, project, created_at, updated_at"
            " FROM runs WHERE updated_at >= ? ORDER BY updated_at, id LIMIT ?",
            (since_iso, limit),
        ).fetchall()
        for r in rows:
            out["runs"].append(
                {
                    "id": r["id"],
                    "workflow": r["workflow_key"],
                    "status": r["status"],
                    "project": r["project"] or "",
                    "created_at": r["created_at"],
                    "updated_at": r["updated_at"],
                    "ts": r["updated_at"],
                }
            )
        rows = self._conn.execute(
            "SELECT id, run_id, stage_key, attempt, status, started_at, finished_at"
            " FROM stage_runs"
            " WHERE COALESCE(finished_at, started_at) >= ?"
            " ORDER BY COALESCE(finished_at, started_at), id LIMIT ?",
            (since_iso, limit),
        ).fetchall()
        for r in rows:
            ts = r["finished_at"] or r["started_at"] or ""
            out["stages"].append(
                {
                    "id": r["id"],
                    "run_id": r["run_id"],
                    "stage_key": r["stage_key"],
                    "attempt": r["attempt"],
                    "status": r["status"],
                    "started_at": r["started_at"],
                    "finished_at": r["finished_at"],
                    "ts": ts,
                }
            )
        rows = self._conn.execute(
            "SELECT id, tool, session_key, status, project, doing, ask, created_at"
            " FROM agent_session_events WHERE created_at >= ?"
            " ORDER BY created_at, id LIMIT ?",
            (since_iso, limit),
        ).fetchall()
        for r in rows:
            out["sessions"].append(
                {
                    "id": r["id"],
                    "tool": r["tool"],
                    "session_key": r["session_key"],
                    "status": r["status"],
                    "project": r["project"] or "",
                    "doing": r["doing"] or "",
                    "ask": r["ask"] or "",
                    "created_at": r["created_at"],
                    "ts": r["created_at"],
                }
            )
        return out

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
