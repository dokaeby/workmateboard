"""SQLite/PostgreSQL 저장소 — runs/stage_runs/audit/approvals/event_links/notifications."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from ..models import RunStatus as RunStatus
from ..models import StageStatus as StageStatus
from .admin import AdminStoreMixin
from .agent_sessions import AgentSessionStoreMixin
from .agents import AgentStoreMixin
from .budgets import BudgetStoreMixin
from .builder import BuilderStoreMixin
from .comms import CommsStoreMixin
from .costs import CostStoreMixin
from .evals import EvalStoreMixin
from .goals import GoalStoreMixin
from .inbox import InboxStoreMixin
from .management import ManagementStoreMixin
from .projects import ProjectStoreMixin
from .radar import RadarStoreMixin
from .runs import RunStoreMixin
from .usage import UsageStoreMixin
from .users import UserStoreMixin
from .util import _iso, _now
from .wakeups import WakeupStoreMixin

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
  agent_id TEXT,
  goal_id TEXT,
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
-- latest_revision/updated_at은 workflow_revisions append-only 이력의
-- 현재 포인터 — spec 본문은 항상 최신 리비전과 동기화된다.
CREATE TABLE IF NOT EXISTS custom_workflows (
  name TEXT PRIMARY KEY,
  spec TEXT NOT NULL,
  created_at TEXT NOT NULL,
  latest_revision INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT
);

-- workflow_revisions: WorkflowSpec append-only 버전 이력 (Paperclip document
-- versioning 포팅) — 리비전은 절대 재작성하지 않고, 복원은 새 리비전으로
-- 추가한다. UNIQUE(name, revision)가 CAS 직렬화의 최후 보루.
CREATE TABLE IF NOT EXISTS workflow_revisions (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  revision INTEGER NOT NULL,
  spec TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  created_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  UNIQUE(name, revision)
);
CREATE INDEX IF NOT EXISTS idx_workflow_revisions_name ON workflow_revisions(name);

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
  goal_id TEXT,
  created_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_projects_name_lower ON projects(lower(name));

-- budget_policies: 스코프별 월간 USD 예산 (global/project/workflow)
CREATE TABLE IF NOT EXISTS budget_policies (
  id TEXT PRIMARY KEY,
  scope_type TEXT NOT NULL,
  scope_id TEXT NOT NULL DEFAULT '',
  window_kind TEXT NOT NULL DEFAULT 'calendar_month_utc',
  amount_usd REAL NOT NULL,
  warn_percent INTEGER NOT NULL DEFAULT 80,
  hard_stop_enabled INTEGER NOT NULL DEFAULT 1,
  is_active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(scope_type, scope_id)
);

-- budget_incidents: 예산 임계(warn/hard) 초과 사건 — 정책+윈도우+임계당 open 1건
CREATE TABLE IF NOT EXISTS budget_incidents (
  id TEXT PRIMARY KEY,
  policy_id TEXT NOT NULL,
  scope_type TEXT NOT NULL,
  scope_id TEXT NOT NULL DEFAULT '',
  window_start TEXT NOT NULL,
  threshold_type TEXT NOT NULL,
  amount_limit REAL NOT NULL,
  amount_observed REAL NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_budget_incidents_policy
  ON budget_incidents(policy_id, status);

-- wakeup_requests: run 생성 단일 비행 + 동일 입력 코얼레싱 감사
CREATE TABLE IF NOT EXISTS wakeup_requests (
  id TEXT PRIMARY KEY,
  source TEXT NOT NULL DEFAULT 'api',
  scope_key TEXT NOT NULL,
  payload TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'queued',
  coalesced_count INTEGER NOT NULL DEFAULT 0,
  idempotency_key TEXT NOT NULL DEFAULT '',
  run_id TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  claimed_at TEXT,
  finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_wakeup_scope ON wakeup_requests(scope_key, status);
CREATE INDEX IF NOT EXISTS idx_wakeup_run ON wakeup_requests(run_id);
CREATE INDEX IF NOT EXISTS idx_wakeup_idem ON wakeup_requests(idempotency_key);

-- agents: 에이전트 레지스트리/조직도 — adapter_type(claude|codex|gemini|
-- devin|custom) + reports_to 계층 + status(active|paused|archived).
-- status는 라이프사이클과 분리된 운영 상태(Paperclip agents 포팅).
CREATE TABLE IF NOT EXISTS agents (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  adapter_type TEXT NOT NULL,
  role TEXT,
  title TEXT,
  description TEXT,
  capabilities TEXT NOT NULL DEFAULT '[]',
  kind TEXT NOT NULL DEFAULT 'workflow',
  adapter_config TEXT NOT NULL DEFAULT '{}',
  reports_to TEXT REFERENCES agents(id),
  status TEXT NOT NULL DEFAULT 'active',
  monthly_budget_usd REAL,
  monthly_budget_microusd INTEGER NOT NULL DEFAULT 0,
  last_seen_at TEXT,
  created_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agents_reports_to ON agents(reports_to);
CREATE INDEX IF NOT EXISTS idx_agents_status ON agents(status);

-- goals: 목표 계층 (Paperclip goals 포팅) — parent_id 자기참조 트리,
-- status(active|done|archived). FK는 두지 않고 앱 계층이 존재/사이클을
-- 검증한다(agents.reports_to와 동일 패턴 — SQLite FK 미강제 커넥션 대비).
CREATE TABLE IF NOT EXISTS goals (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  parent_id TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  created_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_goals_parent ON goals(parent_id);
CREATE INDEX IF NOT EXISTS idx_goals_status ON goals(status);

-- evals: 저장된 입력 픽스처 + 결정적 체크 (Paperclip pillar-3).
-- name은 lower() UNIQUE 인덱스로 대소문자 무시 중복을 막는다
-- (agents/goals의 앱 레벨 ci 검사 + 인덱스 이중 방어와 동일 패턴).
CREATE TABLE IF NOT EXISTS evals (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  workflow_key TEXT NOT NULL,
  input TEXT NOT NULL DEFAULT '{}',
  checks TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'active',
  created_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_evals_name_lower ON evals(lower(name));
CREATE INDEX IF NOT EXISTS idx_evals_status ON evals(status);

-- eval_runs: eval 1회 실행의 채점 결과 — run이 terminal이 되면 lazy 채점.
CREATE TABLE IF NOT EXISTS eval_runs (
  id TEXT PRIMARY KEY,
  eval_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  score REAL,
  results TEXT NOT NULL DEFAULT '[]',
  error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  scored_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_eval_runs_eval ON eval_runs(eval_id);
CREATE INDEX IF NOT EXISTS idx_eval_runs_run ON eval_runs(run_id);

-- agent-management 1차 (paperclip spec): 영속 작업 티켓.
-- task=업무 소유권, run=실행 시도 — task_runs가 둘을 연결한다.
-- identifier는 'WMB-<n>' 사람용 유일 키(Store.create_task가 채번).
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  identifier TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'backlog',
  priority TEXT NOT NULL DEFAULT 'medium',
  assignee_agent_id TEXT,
  reviewer_agent_id TEXT,
  parent_id TEXT,
  current_run_id TEXT,
  checkout_agent_id TEXT,
  checkout_at TEXT,
  created_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_assignee ON tasks(assignee_agent_id);
CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_id);
CREATE INDEX IF NOT EXISTS idx_tasks_checkout ON tasks(checkout_agent_id);

-- agent-management 1차: 통합 에이전트 이벤트 — idempotency_key UNIQUE로
-- 재전송/중복 기록을 막는다(동일 키 append는 기존 행 반환).
CREATE TABLE IF NOT EXISTS agent_events (
  id TEXT PRIMARY KEY,
  idempotency_key TEXT NOT NULL UNIQUE,
  event_type TEXT NOT NULL,
  agent_id TEXT,
  task_id TEXT,
  run_id TEXT,
  actor TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT '',
  detail TEXT NOT NULL DEFAULT '{}',
  occurred_at TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_events_agent ON agent_events(agent_id, created_at);
CREATE INDEX IF NOT EXISTS idx_agent_events_task ON agent_events(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_agent_events_run ON agent_events(run_id, created_at);

-- task↔run 다대다 링크 — (task_id, run_id) 복합 UNIQUE, attempt는
-- 같은 task의 재실행 순번.
CREATE TABLE IF NOT EXISTS task_runs (
  task_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  attempt INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  PRIMARY KEY (task_id, run_id)
);
CREATE INDEX IF NOT EXISTS idx_task_runs_run ON task_runs(run_id);

-- agent-management 2차 (paperclip spec): 비용 원장 — 정수 microusd만
-- 저장한다(float 금액 금지). cost_status='unpriced'이면 cost_microusd는
-- 반드시 NULL(0 대체 금지). idempotency_key UNIQUE로 stage_run당 1건/
-- 외부 runner 재전송 중복을 막는다.
-- agent_id는 spec상 필수지만 runs.agent_id가 비어 있는 run이 많아
-- 미태깅은 ''로 저장한다(스펙 대비 의도적 완화 — docs §24 참조).
CREATE TABLE IF NOT EXISTS cost_events (
  id TEXT PRIMARY KEY,
  idempotency_key TEXT NOT NULL UNIQUE,
  agent_id TEXT NOT NULL DEFAULT '',
  task_id TEXT,
  run_id TEXT,
  stage_run_id TEXT,
  provider TEXT NOT NULL DEFAULT '',
  model TEXT NOT NULL DEFAULT '',
  billing_type TEXT NOT NULL DEFAULT 'unknown',
  cost_status TEXT NOT NULL DEFAULT 'unpriced',
  input_tokens INTEGER NOT NULL DEFAULT 0,
  cached_input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  cost_microusd INTEGER,
  pricing_source TEXT NOT NULL DEFAULT '',
  pricing_version TEXT NOT NULL DEFAULT '',
  occurred_at TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cost_events_agent ON cost_events(agent_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_cost_events_task ON cost_events(task_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_cost_events_run ON cost_events(run_id);
CREATE INDEX IF NOT EXISTS idx_cost_events_stage ON cost_events(stage_run_id);
CREATE INDEX IF NOT EXISTS idx_cost_events_occurred ON cost_events(occurred_at);
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
  agent_id TEXT,
  goal_id TEXT,
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
-- latest_revision/updated_at은 workflow_revisions append-only 이력의
-- 현재 포인터 — spec 본문은 항상 최신 리비전과 동기화된다.
CREATE TABLE IF NOT EXISTS custom_workflows (
  name TEXT PRIMARY KEY,
  spec TEXT NOT NULL,
  created_at TEXT NOT NULL,
  latest_revision INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT
);

-- workflow_revisions: WorkflowSpec append-only 버전 이력 (Paperclip document
-- versioning 포팅) — 리비전은 절대 재작성하지 않고, 복원은 새 리비전으로
-- 추가한다. UNIQUE(name, revision)가 CAS 직렬화의 최후 보루.
CREATE TABLE IF NOT EXISTS workflow_revisions (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  revision INTEGER NOT NULL,
  spec TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  created_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  UNIQUE(name, revision)
);
CREATE INDEX IF NOT EXISTS idx_workflow_revisions_name ON workflow_revisions(name);

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
  goal_id TEXT,
  created_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_projects_name_lower ON projects(lower(name));

-- budget_policies: 스코프별 월간 USD 예산 (global/project/workflow)
CREATE TABLE IF NOT EXISTS budget_policies (
  id TEXT PRIMARY KEY,
  scope_type TEXT NOT NULL,
  scope_id TEXT NOT NULL DEFAULT '',
  window_kind TEXT NOT NULL DEFAULT 'calendar_month_utc',
  amount_usd DOUBLE PRECISION NOT NULL,
  warn_percent INTEGER NOT NULL DEFAULT 80,
  hard_stop_enabled INTEGER NOT NULL DEFAULT 1,
  is_active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(scope_type, scope_id)
);

-- budget_incidents: 예산 임계(warn/hard) 초과 사건 — 정책+윈도우+임계당 open 1건
CREATE TABLE IF NOT EXISTS budget_incidents (
  id TEXT PRIMARY KEY,
  policy_id TEXT NOT NULL,
  scope_type TEXT NOT NULL,
  scope_id TEXT NOT NULL DEFAULT '',
  window_start TEXT NOT NULL,
  threshold_type TEXT NOT NULL,
  amount_limit DOUBLE PRECISION NOT NULL,
  amount_observed DOUBLE PRECISION NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_budget_incidents_policy
  ON budget_incidents(policy_id, status);

-- wakeup_requests: run 생성 단일 비행 + 동일 입력 코얼레싱 감사
CREATE TABLE IF NOT EXISTS wakeup_requests (
  id TEXT PRIMARY KEY,
  source TEXT NOT NULL DEFAULT 'api',
  scope_key TEXT NOT NULL,
  payload TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'queued',
  coalesced_count INTEGER NOT NULL DEFAULT 0,
  idempotency_key TEXT NOT NULL DEFAULT '',
  run_id TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  claimed_at TEXT,
  finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_wakeup_scope ON wakeup_requests(scope_key, status);
CREATE INDEX IF NOT EXISTS idx_wakeup_run ON wakeup_requests(run_id);
CREATE INDEX IF NOT EXISTS idx_wakeup_idem ON wakeup_requests(idempotency_key);

-- agents: 에이전트 레지스트리/조직도 — SQLite판과 동일 구조
CREATE TABLE IF NOT EXISTS agents (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  adapter_type TEXT NOT NULL,
  role TEXT,
  title TEXT,
  description TEXT,
  capabilities TEXT NOT NULL DEFAULT '[]',
  kind TEXT NOT NULL DEFAULT 'workflow',
  adapter_config TEXT NOT NULL DEFAULT '{}',
  reports_to TEXT REFERENCES agents(id),
  status TEXT NOT NULL DEFAULT 'active',
  monthly_budget_usd DOUBLE PRECISION,
  monthly_budget_microusd BIGINT NOT NULL DEFAULT 0,
  last_seen_at TEXT,
  created_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agents_reports_to ON agents(reports_to);
CREATE INDEX IF NOT EXISTS idx_agents_status ON agents(status);

-- goals: 목표 계층 (Paperclip goals 포팅) — SQLite판과 동일 구조
CREATE TABLE IF NOT EXISTS goals (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  parent_id TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  created_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_goals_parent ON goals(parent_id);
CREATE INDEX IF NOT EXISTS idx_goals_status ON goals(status);

-- evals/eval_runs: SQLite판과 동일 구조 (score만 DOUBLE PRECISION)
CREATE TABLE IF NOT EXISTS evals (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  workflow_key TEXT NOT NULL,
  input TEXT NOT NULL DEFAULT '{}',
  checks TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'active',
  created_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_evals_name_lower ON evals(lower(name));
CREATE INDEX IF NOT EXISTS idx_evals_status ON evals(status);

CREATE TABLE IF NOT EXISTS eval_runs (
  id TEXT PRIMARY KEY,
  eval_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  score DOUBLE PRECISION,
  results TEXT NOT NULL DEFAULT '[]',
  error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  scored_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_eval_runs_eval ON eval_runs(eval_id);
CREATE INDEX IF NOT EXISTS idx_eval_runs_run ON eval_runs(run_id);

-- agent-management 1차 (paperclip spec): SQLite판과 동일 구조
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  identifier TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'backlog',
  priority TEXT NOT NULL DEFAULT 'medium',
  assignee_agent_id TEXT,
  reviewer_agent_id TEXT,
  parent_id TEXT,
  current_run_id TEXT,
  checkout_agent_id TEXT,
  checkout_at TEXT,
  created_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_assignee ON tasks(assignee_agent_id);
CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_id);
CREATE INDEX IF NOT EXISTS idx_tasks_checkout ON tasks(checkout_agent_id);

CREATE TABLE IF NOT EXISTS agent_events (
  id TEXT PRIMARY KEY,
  idempotency_key TEXT NOT NULL UNIQUE,
  event_type TEXT NOT NULL,
  agent_id TEXT,
  task_id TEXT,
  run_id TEXT,
  actor TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT '',
  detail TEXT NOT NULL DEFAULT '{}',
  occurred_at TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_events_agent ON agent_events(agent_id, created_at);
CREATE INDEX IF NOT EXISTS idx_agent_events_task ON agent_events(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_agent_events_run ON agent_events(run_id, created_at);

CREATE TABLE IF NOT EXISTS task_runs (
  task_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  attempt INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  PRIMARY KEY (task_id, run_id)
);
CREATE INDEX IF NOT EXISTS idx_task_runs_run ON task_runs(run_id);

-- agent-management 2차 (paperclip spec): SQLite판과 동일 구조 —
-- 정수 컬럼은 BIGINT로 PG 정수 범위를 확보한다.
CREATE TABLE IF NOT EXISTS cost_events (
  id TEXT PRIMARY KEY,
  idempotency_key TEXT NOT NULL UNIQUE,
  agent_id TEXT NOT NULL DEFAULT '',
  task_id TEXT,
  run_id TEXT,
  stage_run_id TEXT,
  provider TEXT NOT NULL DEFAULT '',
  model TEXT NOT NULL DEFAULT '',
  billing_type TEXT NOT NULL DEFAULT 'unknown',
  cost_status TEXT NOT NULL DEFAULT 'unpriced',
  input_tokens BIGINT NOT NULL DEFAULT 0,
  cached_input_tokens BIGINT NOT NULL DEFAULT 0,
  output_tokens BIGINT NOT NULL DEFAULT 0,
  cost_microusd BIGINT,
  pricing_source TEXT NOT NULL DEFAULT '',
  pricing_version TEXT NOT NULL DEFAULT '',
  occurred_at TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cost_events_agent ON cost_events(agent_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_cost_events_task ON cost_events(task_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_cost_events_run ON cost_events(run_id);
CREATE INDEX IF NOT EXISTS idx_cost_events_stage ON cost_events(stage_run_id);
CREATE INDEX IF NOT EXISTS idx_cost_events_occurred ON cost_events(occurred_at);
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


class Store(
    RunStoreMixin,
    CommsStoreMixin,
    RadarStoreMixin,
    UserStoreMixin,
    AgentSessionStoreMixin,
    UsageStoreMixin,
    InboxStoreMixin,
    AdminStoreMixin,
    BuilderStoreMixin,
    ProjectStoreMixin,
    BudgetStoreMixin,
    WakeupStoreMixin,
    AgentStoreMixin,
    GoalStoreMixin,
    EvalStoreMixin,
    ManagementStoreMixin,
    CostStoreMixin,
):
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
        self._migrate_budget_incident_index()
        self._migrate_wakeup_idempotency_index()
        self._migrate_agent_name_index()
        self._migrate_goal_columns()
        self._migrate_deferred_wakeup_scopes()
        self._migrate_workflow_version_columns()
        self._migrate_agent_management_columns()
        self._migrate_cost_ledger_columns()
        # DSN/경로 원문은 자격증명을 포함할 수 있으므로 백엔드 종류만 기록한다
        logger.info("store opened backend=%s", "postgresql" if self._pg else "sqlite")

    def _ensure_columns(self, table: str, wanted: dict[str, str]) -> None:
        """CREATE TABLE IF NOT EXISTS는 기존 테이블을 변경하지 않으므로
        나중에 추가된 컬럼은 ALTER로 보강한다.
        wanted: {컬럼명: 컬럼 DDL} — 테이블/컬럼명은 내부 상수만 사용."""
        try:
            if self._pg:
                rows = self._conn.execute(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_name=? AND table_schema = current_schema()",
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
        """기존 DB의 runs에 created_by/project/agent_id 컬럼 보강."""
        self._ensure_columns(
            "runs",
            {
                "created_by": "TEXT NOT NULL DEFAULT ''",
                "project": "TEXT NOT NULL DEFAULT ''",
                "agent_id": "TEXT",
            },
        )

    def _migrate_budget_incident_index(self) -> None:
        """open 인시던트 (policy, window, threshold) 부분 UNIQUE 인덱스.

        SCHEMA에 넣지 않고 여기서 생성하는 이유: 기존 DB에 중복 open
        인시던트가 있으면 executescript 전체가 실패해 기동이 멈춘다.
        여기서는 중복을 먼저 정리하고, 그래도 실패하면 경고만 남긴다 —
        has_open_budget_incident 사전 검사가 논리적 중복을 계속 막는다."""
        try:
            rows = self._conn.execute(
                "SELECT id, policy_id, window_start, threshold_type"
                " FROM budget_incidents WHERE status='open'"
                " ORDER BY created_at, id"
            ).fetchall()
            seen: set[tuple[Any, Any, Any]] = set()
            for r in rows:
                key = (r["policy_id"], r["window_start"], r["threshold_type"])
                if key in seen:
                    self.resolve_budget_incident(
                        str(r["id"]), "resolved", note="dedupe before unique index"
                    )
                else:
                    seen.add(key)
            with self.tx() as c:
                c.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_budget_incidents_open"
                    " ON budget_incidents(policy_id, window_start, threshold_type)"
                    " WHERE status='open'"
                )
        except Exception as e:
            logger.warning("budget_incidents open-unique index skipped: %s", e)

    def _migrate_wakeup_idempotency_index(self) -> None:
        """idempotency_key 부분 UNIQUE 인덱스 — 동시 동일 키 요청의 이중 run 방지.

        SCHEMA에 넣지 않는 이유는 _migrate_budget_incident_index와 동일 —
        기존 DB에 중복 키가 있으면 executescript 전체가 실패해 기동이 멈춘다.
        여기서는 키를 비워 최신 행만 남기고(감사 행은 보존) 인덱스를 만든다.
        인덱스 생성마저 실패하면 경고만 남기고 진행한다 —
        admit_run의 replay 사전 검사와 IntegrityError 재조회가 계속 동작한다."""
        try:
            rows = self._conn.execute(
                "SELECT id, idempotency_key FROM wakeup_requests"
                " WHERE idempotency_key <> '' ORDER BY created_at DESC, id DESC"
            ).fetchall()
            seen: set[str] = set()
            dup_ids: list[str] = []
            for r in rows:
                key = str(r["idempotency_key"])
                if key in seen:
                    dup_ids.append(str(r["id"]))
                else:
                    seen.add(key)
            if dup_ids:
                with self.tx() as c:
                    for did in dup_ids:
                        c.execute(
                            "UPDATE wakeup_requests SET idempotency_key='' WHERE id=?", (did,)
                        )
                logger.info(
                    "wakeup_requests idempotency dedupe: cleared %d duplicate keys",
                    len(dup_ids),
                )
            with self.tx() as c:
                c.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_wakeup_idem_uniq"
                    " ON wakeup_requests(idempotency_key) WHERE idempotency_key <> ''"
                )
        except Exception as e:
            logger.warning("wakeup_requests idempotency unique index skipped: %s", e)

    def _migrate_agent_name_index(self) -> None:
        """agents.lower(name) UNIQUE 인덱스 — 앱 레벨 ci 중복 검사의 레이스 백스톱.

        SCHEMA에 넣지 않는 이유는 _migrate_budget_incident_index와 동일 —
        기존 DB에 대소문자만 다른 중복 이름이 있으면 executescript 전체가
        실패해 기동이 멈춘다. 여기서는 최신 행만 이름을 유지하고 나머지는
        id 접미를 붙여 보존한다(runs.agent_id/reports_to는 id 참조라 영향
        없음). 인덱스 생성마저 실패하면 경고만 남기고 진행한다 —
        find_agent_by_name 사전 검사와 IntegrityError→409 매핑이 계속 동작."""
        try:
            rows = self._conn.execute(
                "SELECT id, name, updated_at, created_at FROM agents"
                " ORDER BY updated_at DESC, created_at DESC, id DESC"
            ).fetchall()
            seen: set[str] = set()
            renames: list[tuple[str, str]] = []
            for r in rows:
                key = str(r["name"] or "").strip().lower()
                if not key:
                    continue
                if key in seen:
                    # 최신이 아닌 중복 — 이름에 id 접미를 붙여 유일성 확보
                    new_name = f"{r['name']}·dup·{r['id']}"[:100]
                    renames.append((new_name, str(r["id"])))
                else:
                    seen.add(key)
            if renames:
                with self.tx() as c:
                    for new_name, aid in renames:
                        c.execute(
                            "UPDATE agents SET name=?, updated_at=? WHERE id=?",
                            (new_name, _iso(_now()), aid),
                        )
                logger.info("agents name dedupe: renamed %d duplicate rows", len(renames))
            with self.tx() as c:
                c.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_agents_name_lower ON agents(lower(name))"
                )
        except Exception as e:
            logger.warning("agents name unique index skipped: %s", e)

    def _migrate_goal_columns(self) -> None:
        """기존 DB의 runs/projects에 goal_id 컬럼 보강 (Phase-3 goals)."""
        self._ensure_columns("runs", {"goal_id": "TEXT"})
        self._ensure_columns("projects", {"goal_id": "TEXT"})

    def _migrate_deferred_wakeup_scopes(self) -> None:
        """goal_id 도입으로 바뀐 scope_key 형식에 맞춰 deferred wakeup 재계산.

        scope_key_for의 digest가 (agent_id, goal_id, input)을 포함하도록
        바뀌면서, goal_id 미포함 구형식으로 저장된 status='deferred' 행은
        on_run_finished의 scope 조회와 영원히 매칭되지 않아 고착된다.
        payload(input/agent_id/goal_id)에서 scope_key를 재계산해 갱신하고,
        payload에 goal_id가 없으면 프로젝트 폴백 규칙(resolve_run_goal_id와
        동일: input.goal_id → project→projects.goal_id, archived 제외)으로
        보정해 payload에도 기록한다 — promote 경로의 scope 계산과 일치시키기
        위함. payload를 해석할 수 없는 행은 재시도해도 복구되지 않으므로
        failed로 마감한다. 행 단위 best-effort — 실패는 경고만 남긴다
        (_migrate_* 패턴과 동일)."""
        try:
            from ..wakeups import scope_key_for  # 지연 import — 순환 방지
        except Exception as e:
            logger.warning("deferred wakeup scope migration skipped: %s", e)
            return
        try:
            rows = self._conn.execute(
                "SELECT id, scope_key, payload FROM wakeup_requests WHERE status='deferred'"
            ).fetchall()
        except Exception as e:
            logger.warning("deferred wakeup scope scan failed: %s", e)
            return
        fixed = failed = 0
        for r in rows:
            wid = str(r["id"])
            try:
                payload = json.loads(str(r["payload"] or ""))
            except (ValueError, TypeError):
                payload = None
            inp = payload.get("input") if isinstance(payload, dict) else None
            if not isinstance(payload, dict) or not isinstance(inp, dict):
                # promote 경로도 같은 이유로 failed 마감한다 — 여기서 미리
                # 마감해 고착 deferred를 남기지 않는다.
                try:
                    self.fail_wakeup(wid)
                    failed += 1
                except Exception as e:
                    logger.warning("deferred wakeup %s fail-mark failed: %s", wid, e)
                continue
            try:
                # scope_key = "{workflow_key}:{digest}" — digest는 마지막
                # ':' 뒤 16hex. workflow_key는 ':'를 포함하지 않는다.
                wf = str(r["scope_key"] or "").rsplit(":", 1)[0]
                agent_id = str(payload.get("agent_id") or "")
                gid = str(payload.get("goal_id") or "").strip()[:64]
                payload_changed = False
                if not gid:
                    # admit_run과 동일한 폴백 해석 — project 태그의 goal.
                    gid = self.resolve_run_goal_id(dict(inp), "")
                    if gid and not self._goal_usable(gid):
                        gid = ""
                    if gid:
                        payload["goal_id"] = gid
                        payload_changed = True
                new_scope = scope_key_for(wf, inp, agent_id, gid)
                if new_scope == str(r["scope_key"] or "") and not payload_changed:
                    continue
                new_payload = json.dumps(payload, ensure_ascii=False, default=str)
                with self.tx() as c:
                    c.execute(
                        "UPDATE wakeup_requests SET scope_key=?, payload=?"
                        " WHERE id=? AND status='deferred'",
                        (new_scope, new_payload, wid),
                    )
                fixed += 1
            except Exception as e:
                failed += 1
                logger.warning("deferred wakeup %s scope recompute failed: %s", wid, e)
        if fixed or failed:
            logger.info(
                "deferred wakeup scope migration: fixed=%d failed=%d total=%d",
                fixed,
                failed,
                len(rows),
            )

    def _migrate_agent_management_columns(self) -> None:
        """agent-management 1차 — 기존 agents 테이블에 신규 컬럼 보강.

        kind는 실행 모델(workflow|external), adapter_config는 provider별
        실행 설정 JSON, last_seen_at은 머신 heartbeat 시각,
        monthly_budget_microusd는 2차 비용 원장이 쓸 저장 전용 예산.
        status 어휘는 건드리지 않는다 — 'archived'는 terminated의
        레거시 별칭으로 계속 유효(models.management 참조)."""
        # PG는 fresh schema(SCHEMA_PG)와 타입을 맞춰 BIGINT — INTEGER로
        # 추가하면 마이그레이션 DB와 신규 DB의 컬럼 타입이 어긋난다.
        # SQLite는 INTEGER affinity라 BIGINT도 동일하게 동작한다.
        self._ensure_columns(
            "agents",
            {
                "title": "TEXT",
                "kind": "TEXT NOT NULL DEFAULT 'workflow'",
                "adapter_config": "TEXT NOT NULL DEFAULT '{}'",
                "last_seen_at": "TEXT",
                "monthly_budget_microusd": (
                    "BIGINT NOT NULL DEFAULT 0" if self._pg else "INTEGER NOT NULL DEFAULT 0"
                ),
            },
        )
        # kind 인덱스는 컬럼 보강 후에만 생성 가능 — SCHEMA의 executescript는
        # 기존 DB에서 컬럼 부재로 실패하므로 여기서 만든다(실패해도 경고만).
        try:
            with self.tx() as c:
                c.execute("CREATE INDEX IF NOT EXISTS idx_agents_kind ON agents(kind)")
        except Exception as e:
            logger.warning("agents kind index skipped: %s", e)

    def _migrate_cost_ledger_columns(self) -> None:
        """agent-management 2차 — budget_policies/budget_incidents의
        microusd·unpriced 컬럼 보강 + amount_usd → amount_microusd 백필.

        CREATE TABLE IF NOT EXISTS는 기존 테이블을 변경하지 않으므로
        컬럼은 _ensure_columns로 추가한다. 기존 *_usd REAL 컬럼은 호환용으로
        유지하고, microusd 컬럼이 신규 권위 경로다(paperclip spec 2차 B).
        amount_microusd 백필은 ROUND(amount_usd * 1e6) — float 읽기를
        정수로 변환하는 1회성 마이그레이션이며 런타임 금액 계산에는
        float를 쓰지 않는다."""
        big = "BIGINT" if self._pg else "INTEGER"
        self._ensure_columns(
            "budget_policies",
            {
                "amount_microusd": f"{big} NOT NULL DEFAULT 0",
                "reservation_microusd": f"{big} NOT NULL DEFAULT 0",
                "unpriced_usage_policy": "TEXT NOT NULL DEFAULT 'allow'",
            },
        )
        self._ensure_columns(
            "budget_incidents",
            {
                "observed_spend_microusd": big,
                "unpriced_event_count": "INTEGER NOT NULL DEFAULT 0",
                "reason_code": "TEXT NOT NULL DEFAULT ''",
            },
        )
        # amount_usd > 0 이고 amount_microusd가 아직 0인 행만 백필 —
        # 명시적으로 0을 쓴 정책(비활성 의미)과 이미 마이그레이션된 행은
        # 건드리지 않는다. 마이그레이션은 기동 시 1회 실행된다.
        try:
            cast = "BIGINT" if self._pg else "INTEGER"
            with self.tx() as c:
                cur = c.execute(
                    "UPDATE budget_policies SET amount_microusd ="  # noqa: S608 - cast는 'BIGINT'/'INTEGER' 고정 리터럴
                    f" CAST(ROUND(amount_usd * 1000000) AS {cast})"
                    " WHERE amount_microusd = 0 AND amount_usd > 0"
                )
            if getattr(cur, "rowcount", 0):
                logger.info("budget_policies microusd backfill: %d rows", cur.rowcount)
        except Exception as e:
            logger.warning("budget_policies microusd backfill failed: %s", e)

    def _migrate_workflow_version_columns(self) -> None:
        """custom_workflows에 latest_revision/updated_at 보강 + 기존 spec 백필.

        CREATE TABLE IF NOT EXISTS는 기존 테이블을 변경하지 않으므로 컬럼은
        _ensure_columns로 추가하고, 리비전 이력이 없는 기존 행은 현재 spec을
        revision 1(note='backfill')로 복원한다 — 이후 모든 저장이
        latest+1 append-only 리비전으로 이어진다."""
        self._ensure_columns(
            "custom_workflows",
            {
                "latest_revision": "INTEGER NOT NULL DEFAULT 0",
                "updated_at": "TEXT",
            },
        )
        self._backfill_workflow_revisions()

    def _backfill_workflow_revisions(self) -> None:
        """workflow_revisions가 비어 있는 custom_workflows 행에 revision 1 생성.

        관용적 마이그레이션 — 실패해도 기동을 막지 않고 경고만 남긴다
        (_migrate_* 패턴과 동일). latest_revision도 함께 동기화한다."""
        try:
            rows = self._conn.execute(
                "SELECT name, spec, created_at FROM custom_workflows"
            ).fetchall()
        except Exception as e:
            logger.warning("workflow_revisions backfill scan failed: %s", e)
            return
        for r in rows:
            # 행 단위 best-effort — 깨진 spec/이름 하나가 나머지 백필을
            # 중단시키지 않게 한다.
            try:
                self._backfill_workflow_revision_row(r)
            except Exception as e:
                logger.warning(
                    "workflow_revisions backfill row %s failed: %s",
                    r["name"] if hasattr(r, "keys") else "?",
                    e,
                )

    def _backfill_workflow_revision_row(self, r: Any) -> None:
        name = str(r["name"])
        rev_row = self._conn.execute(
            "SELECT COALESCE(MAX(revision),0) AS m FROM workflow_revisions WHERE name=?",
            (name,),
        ).fetchone()
        max_rev = int(rev_row["m"] or 0) if rev_row is not None else 0
        if max_rev == 0:
            now = _iso(_now())
            with self.tx() as c:
                c.execute(
                    "INSERT INTO workflow_revisions"
                    "(id,name,revision,spec,note,created_by,created_at)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (
                        uuid.uuid4().hex[:12],
                        name,
                        1,
                        str(r["spec"]),
                        "backfill",
                        "",
                        now,
                    ),
                )
                c.execute(
                    "UPDATE custom_workflows SET latest_revision=1,"
                    " updated_at=COALESCE(updated_at, created_at) WHERE name=?",
                    (name,),
                )
            logger.info("workflow_revisions backfill: %s -> revision 1", name)
        else:
            # 리비전은 있으나 latest_revision 포인터가 뒤처진 경우 동기화
            with self.tx() as c:
                c.execute(
                    "UPDATE custom_workflows SET latest_revision=?"
                    " WHERE name=? AND (latest_revision IS NULL"
                    " OR latest_revision < ?)",
                    (max_rev, name, max_rev),
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

    # ---- stage_runs ----

    # ---- approvals ----

    # ---- audit ----

    # ---- event_links (gmail_calendar 멱등) ----

    # ---- notifications ----

    # ---- automation state ----

    # ---- 범용 상태 (automation_state key/value) ----

    # ---- message_extractions (멀티채널 수신함) ----

    # 처리 중 메시지 리스 — claim 시 retry_at을 미래로 밀어 다른 worker가
    # 같은 행을 집지 않게 한다. 프로세스 크래시면 리스 만료 후 재claim된다.
    # gmail_calendar run은 LLM 다단계라 수 분 걸릴 수 있어 리스를 넉넉히 둔다.
    # 크래시 시 이 시간만큼 처리가 지연될 뿐 중복 처리는 없다.
    _MESSAGE_LEASE_SECONDS = 1800

    # ---- scheduled_events ----

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

    # ---- notification_jobs ----

    # ---- radar (skillRadar) ----

    # ---- push_subscriptions ----

    # ---- users / auth_sessions ----

    # ---- email_verifications (가입 이메일 인증) ----

    # ---- email_summaries (3줄 요약 카드) ----

    # ---- agent_sessions (로컬 에이전트 브릿지 push 세션) ----
    _SESSION_EVENT_DEDUP_SECONDS = 60.0
    _WORKING_GAP_CAP_SECONDS = 3600.0

    # ---- llm_usage (LLM 토큰 사용량) ----

    # ---- workflow_schedules (주기적 workflow run) ----

    # ---- run stats (GET /runs/stats) ----
    _STATS_FAILED = {"failed", "rejected", "budget_exhausted", "needs_human", "cancelled"}
    _STATS_TERMINAL = _STATS_FAILED | {"completed"}

    # ---- inbox_items (통합 인박스) ----

    # ---- session_activity (세션 활동 녹화/리플레이) ----
    _ACTIVITY_THROTTLE_SECONDS = 60.0
    _ACTIVITY_RETENTION_DAYS = 7

    # ---- reports (daily/weekly 리포트) ----

    # ---- llm_usage 상세 (비용 대시보드용 행 단위 조회) ----

    # ---- agent_sessions 브릿지/헬스 집계 ----

    # ---- users 관리 ----

    # ---- playbooks (원클릭 작업 템플릿) ----

    # ---- run_comments (run/diff 토론) ----

    # ---- connectors (외부 데이터 소스) ----

    # ---- custom_workflows (비주얼 빌더 WorkflowSpec) ----

    # ---- triggers (세션 상태 전이 자동화) ----

    # ---- share_links (공개 스냅샷 공유) ----

    # ---- dashboard_cards (사용자 등록 JSON 위젯) ----

    # ---- 감사 로그 뷰 (audit_events 전역 조회) ----

    # ---- team 뷰 (다중 사용자 활동 집계) ----

    # ---- export 헬퍼 ----

    # ---- projects (팀장 프로젝트 마스터/태깅) ----

    # ---- 프로젝트 태깅 (runs.project / agent_sessions.project) ----

    # ---- 리드 대시보드 집계용 raw row 조회 ----

    # ---- SSE 스트림용 증분 조회 ----

    # ---- db 메타 (헬스 상세) ----
