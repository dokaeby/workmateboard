"""SQLite 스키마 — jobmate.store.db에서 분리 (스토어 계층 리팩토링)."""

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
  error_class TEXT NOT NULL DEFAULT '',
  metadata TEXT NOT NULL DEFAULT '{}',
  created_by TEXT NOT NULL DEFAULT '',
  project TEXT NOT NULL DEFAULT '',
  agent_id TEXT,
  goal_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  org_id TEXT NOT NULL DEFAULT ''

);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);
-- spec 007 관측성 집계/스윕용 — runs.created_at/updated_at, stage_runs.finished_at 범위 스캔
CREATE INDEX IF NOT EXISTS idx_runs_created ON runs(created_at);
CREATE INDEX IF NOT EXISTS idx_runs_updated ON runs(updated_at);

CREATE TABLE IF NOT EXISTS stage_runs (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id),
  stage_key TEXT NOT NULL,
  attempt INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL,
  input TEXT NOT NULL DEFAULT '{}',
  output TEXT NOT NULL DEFAULT '{}',
  error TEXT NOT NULL DEFAULT '',
  error_class TEXT NOT NULL DEFAULT '',
  model TEXT NOT NULL DEFAULT '',
  prompt_version TEXT NOT NULL DEFAULT '',
  tokens_in INTEGER NOT NULL DEFAULT 0,
  tokens_out INTEGER NOT NULL DEFAULT 0,
  started_at TEXT,
  finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_stage_runs_run ON stage_runs(run_id);
CREATE INDEX IF NOT EXISTS idx_stage_runs_finished ON stage_runs(finished_at);

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
  created_at TEXT NOT NULL,
  org_id TEXT NOT NULL DEFAULT ''

);
CREATE INDEX IF NOT EXISTS idx_audit_run ON audit_events(run_id);

CREATE TABLE IF NOT EXISTS event_links (
  id TEXT PRIMARY KEY,
  idempotency_key TEXT NOT NULL,
  gmail_message_id TEXT NOT NULL,
  connection_id TEXT NOT NULL DEFAULT 'default',
  status TEXT NOT NULL,
  calendar_event_id TEXT NOT NULL DEFAULT '',
  notification_id TEXT NOT NULL DEFAULT '',
  source_url TEXT NOT NULL DEFAULT '',
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  org_id TEXT NOT NULL DEFAULT '',
  UNIQUE(org_id, idempotency_key)

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
  sent_at TEXT,
  org_id TEXT NOT NULL DEFAULT ''

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
  org_id TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (source_provider, source_id, org_id)
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
  updated_at TEXT NOT NULL,
  org_id TEXT NOT NULL DEFAULT ''

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
  updated_at TEXT NOT NULL,
  org_id TEXT NOT NULL DEFAULT ''

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
  org_id TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (source_provider, source_id, org_id)
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
  last_login_ip TEXT NOT NULL DEFAULT '',
  last_login_fingerprint TEXT NOT NULL DEFAULT '',
  last_login_at TEXT NOT NULL DEFAULT '',
  locale TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- 세션 — 토큰 원문이 아닌 SHA-256 해시만 저장
CREATE TABLE IF NOT EXISTS auth_sessions (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  last_seen_at TEXT,
  current_org_id TEXT NOT NULL DEFAULT '',
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

-- 비밀번호 재설정 토큰 — 원문 금지, SHA-256 해시만 저장(email_verifications와 동형)
CREATE TABLE IF NOT EXISTS password_reset_tokens (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  email TEXT NOT NULL,
  token_hash TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  used_at TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_password_reset_tokens_email ON password_reset_tokens(email);
CREATE INDEX IF NOT EXISTS idx_password_reset_tokens_hash ON password_reset_tokens(token_hash);

-- MFA/TOTP — 비밀키는 Fernet 암호화(enc:v1:), 백업코드·티켓은 SHA-256 해시만 저장
CREATE TABLE IF NOT EXISTS mfa_secrets (
  user_id TEXT PRIMARY KEY,
  secret_enc TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  last_totp_step INTEGER,
  created_at TEXT NOT NULL,
  confirmed_at TEXT
);
CREATE TABLE IF NOT EXISTS mfa_backup_codes (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  code_hash TEXT NOT NULL,
  used_at TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mfa_backup_codes_user ON mfa_backup_codes(user_id);
CREATE TABLE IF NOT EXISTS mfa_tickets (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  ticket_hash TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  used_at TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mfa_tickets_hash ON mfa_tickets(ticket_hash);

-- 스코프드 API 키 — 원문 금지, SHA-256 해시 + 앞 8자 prefix만 저장
CREATE TABLE IF NOT EXISTS api_keys (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  owner_user_id TEXT NOT NULL DEFAULT '',
  key_hash TEXT NOT NULL,
  key_prefix TEXT NOT NULL DEFAULT '',
  scopes_json TEXT NOT NULL DEFAULT '[]',
  expires_at TEXT,
  revoked_at TEXT,
  last_used_at TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_api_keys_owner ON api_keys(owner_user_id);
CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys(key_hash);

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
  org_id TEXT NOT NULL DEFAULT '',
  UNIQUE(org_id, tool, session_key)
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
  created_at TEXT NOT NULL,
  org_id TEXT NOT NULL DEFAULT ''

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
  created_at TEXT NOT NULL,
  org_id TEXT NOT NULL DEFAULT ''

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
  created_at TEXT NOT NULL,
  org_id TEXT NOT NULL DEFAULT ''

);
CREATE INDEX IF NOT EXISTS idx_inbox_items_created ON inbox_items(created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_inbox_items_dedupe
  ON inbox_items(dedupe_key, org_id) WHERE dedupe_key != '';

-- session_activity: 에이전트 세션 활동 스냅샷 (리플레이용, 기본 30일 보관·설정 가능)
CREATE TABLE IF NOT EXISTS session_activity (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tool TEXT NOT NULL,
  session_key TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT '',
  doing TEXT,
  project TEXT,
  recorded_at TEXT NOT NULL,
  org_id TEXT NOT NULL DEFAULT ''

);
CREATE INDEX IF NOT EXISTS idx_session_activity_key
  ON session_activity(tool, session_key, recorded_at);

-- reports: 생성된 daily/weekly 리포트
CREATE TABLE IF NOT EXISTS reports (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  content TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  org_id TEXT NOT NULL DEFAULT ''

);
CREATE INDEX IF NOT EXISTS idx_reports_kind ON reports(kind, created_at);

-- playbooks: 원클릭 작업 템플릿
CREATE TABLE IF NOT EXISTS playbooks (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  workflow TEXT NOT NULL,
  input TEXT NOT NULL DEFAULT '{}',
  created_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  org_id TEXT NOT NULL DEFAULT ''

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
  created_at TEXT NOT NULL,
  org_id TEXT NOT NULL DEFAULT ''

);

-- custom_workflows: 비주얼 빌더로 만든 WorkflowSpec 영속화
-- latest_revision/updated_at은 workflow_revisions append-only 이력의
-- 현재 포인터 — spec 본문은 항상 최신 리비전과 동기화된다.
CREATE TABLE IF NOT EXISTS custom_workflows (
  name TEXT NOT NULL,
  spec TEXT NOT NULL,
  created_at TEXT NOT NULL,
  latest_revision INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT,
  org_id TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (name, org_id)

);

-- workflow_revisions: WorkflowSpec append-only 버전 이력 (Paperclip document
-- versioning 포팅) — 리비전은 절대 재작성하지 않고, 복원은 새 리비전으로
-- 추가한다. UNIQUE(org_id, name, revision)가 CAS 직렬화의 최후 보루.
CREATE TABLE IF NOT EXISTS workflow_revisions (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  revision INTEGER NOT NULL,
  spec TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  created_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  org_id TEXT NOT NULL DEFAULT '',
  UNIQUE(org_id, name, revision)
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
  last_error TEXT NOT NULL DEFAULT '',
  org_id TEXT NOT NULL DEFAULT ''

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
  created_at TEXT NOT NULL,
  org_id TEXT NOT NULL DEFAULT ''

);

-- dashboard_cards: 사용자 등록 webhook/JSON 위젯
CREATE TABLE IF NOT EXISTS dashboard_cards (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  url TEXT NOT NULL,
  json_path TEXT NOT NULL DEFAULT '',
  enabled INTEGER NOT NULL DEFAULT 1,
  created_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  org_id TEXT NOT NULL DEFAULT ''

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
  status TEXT NOT NULL DEFAULT 'active',
  owner_agent_id TEXT,
  repository_url TEXT,
  repository_ref TEXT,
  workspace_path TEXT,
  created_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT,
  org_id TEXT NOT NULL DEFAULT ''

);
CREATE UNIQUE INDEX IF NOT EXISTS idx_projects_name_lower ON projects(org_id, lower(name));

-- budget_policies: 스코프별 월간 USD 예산 (global/project/workflow)
CREATE TABLE IF NOT EXISTS budget_policies (
  id TEXT PRIMARY KEY,
  scope_type TEXT NOT NULL,
  scope_id TEXT NOT NULL DEFAULT '',
  window_kind TEXT NOT NULL DEFAULT 'calendar_month_utc',
  amount_usd REAL NOT NULL,
  warn_percent INTEGER NOT NULL DEFAULT 80,
  hard_stop_enabled INTEGER NOT NULL DEFAULT 1,
  threshold_action TEXT NOT NULL DEFAULT 'alert',
  is_active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  org_id TEXT NOT NULL DEFAULT '',
  UNIQUE(scope_type, scope_id, org_id)
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
  resolved_at TEXT,
  org_id TEXT NOT NULL DEFAULT ''

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
-- name 유일성은 컬럼 UNIQUE가 아니라 idx_agents_name_lower
-- (org_id, lower(name))가 조직별로 강제한다 — 인덱스는 기존 DB의
-- 중복 이름 정리 후 _migrate_agent_name_index가 생성한다.
CREATE TABLE IF NOT EXISTS agents (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
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
  updated_at TEXT NOT NULL,
  org_id TEXT NOT NULL DEFAULT ''

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
  owner_agent_id TEXT,
  success_metrics TEXT NOT NULL DEFAULT '{}',
  target_at TEXT,
  created_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  org_id TEXT NOT NULL DEFAULT ''

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
  updated_at TEXT NOT NULL,
  org_id TEXT NOT NULL DEFAULT ''

);
CREATE UNIQUE INDEX IF NOT EXISTS idx_evals_name_lower ON evals(org_id, lower(name));
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
  scored_at TEXT,
  org_id TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT 'manual',
  schedule_id TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_eval_runs_eval ON eval_runs(eval_id);
CREATE INDEX IF NOT EXISTS idx_eval_runs_run ON eval_runs(run_id);
-- 온라인 평가 dedupe — (schedule_id, run_id)당 eval_run 1건. 수동 실행
-- (schedule_id='')은 대상 아님. 기존 DB는 _migrate_reporting_ext_columns가 생성.
CREATE UNIQUE INDEX IF NOT EXISTS idx_eval_runs_online_dedupe
  ON eval_runs(schedule_id, run_id) WHERE schedule_id != '';

-- agent-management 1차 (paperclip spec): 영속 작업 티켓.
-- task=업무 소유권, run=실행 시도 — task_runs가 둘을 연결한다.
-- identifier는 'WMB-<n>' 사람용 유일 키(Store.create_task가 채번).
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  identifier TEXT NOT NULL,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'backlog',
  priority TEXT NOT NULL DEFAULT 'medium',
  assignee_agent_id TEXT,
  reviewer_agent_id TEXT,
  parent_id TEXT,
  project_id TEXT,
  goal_id TEXT,
  current_run_id TEXT,
  checkout_agent_id TEXT,
  checkout_at TEXT,
  created_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  org_id TEXT NOT NULL DEFAULT '',
  UNIQUE(org_id, identifier)

);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_assignee ON tasks(assignee_agent_id);
CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_id);
CREATE INDEX IF NOT EXISTS idx_tasks_checkout ON tasks(checkout_agent_id);

-- agent-management 1차: 통합 에이전트 이벤트 — UNIQUE(org_id,idempotency_key)로
-- 조직별 재전송/중복 기록을 막는다(동일 키 append는 기존 행 반환).
CREATE TABLE IF NOT EXISTS agent_events (
  id TEXT PRIMARY KEY,
  idempotency_key TEXT NOT NULL,
  event_type TEXT NOT NULL,
  agent_id TEXT,
  task_id TEXT,
  run_id TEXT,
  actor TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT '',
  detail TEXT NOT NULL DEFAULT '{}',
  occurred_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  org_id TEXT NOT NULL DEFAULT '',
  UNIQUE(org_id, idempotency_key)

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
  idempotency_key TEXT NOT NULL,
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
  created_at TEXT NOT NULL,
  org_id TEXT NOT NULL DEFAULT '',
  UNIQUE(org_id, idempotency_key)

);
CREATE INDEX IF NOT EXISTS idx_cost_events_agent ON cost_events(agent_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_cost_events_task ON cost_events(task_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_cost_events_run ON cost_events(run_id);
CREATE INDEX IF NOT EXISTS idx_cost_events_stage ON cost_events(stage_run_id);
CREATE INDEX IF NOT EXISTS idx_cost_events_occurred ON cost_events(occurred_at);

-- agent-management 2차 (paperclip spec D): 범용 승인 요청 — 기존
-- approvals(run 승인)와 별개의 durable 승인함. safe_payload는 JSON
-- TEXT — secret-like key는 서비스 계층이 저장 전에 거부한다.
-- status: pending|approved|rejected|expired|cancelled.
CREATE TABLE IF NOT EXISTS approval_requests (
  id TEXT PRIMARY KEY,
  action_type TEXT NOT NULL,
  risk_level TEXT NOT NULL DEFAULT 'medium',
  status TEXT NOT NULL DEFAULT 'pending',
  requested_by_agent_id TEXT,
  requested_by TEXT NOT NULL DEFAULT '',
  task_id TEXT,
  run_id TEXT,
  resource_type TEXT NOT NULL DEFAULT '',
  resource_id TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT '',
  safe_payload TEXT NOT NULL DEFAULT '{}',
  expires_at TEXT,
  decided_by TEXT,
  decision_comment TEXT NOT NULL DEFAULT '',
  decided_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  org_id TEXT NOT NULL DEFAULT ''

);
CREATE INDEX IF NOT EXISTS idx_approval_requests_status ON approval_requests(status);
CREATE INDEX IF NOT EXISTS idx_approval_requests_run ON approval_requests(run_id);

-- agent-management 2차 (paperclip spec E): watchdog 복구 액션 —
-- source당 active|escalated 1건 제한은 부분 UNIQUE 인덱스
-- (_migrate_recovery_actions_index)가 담당한다. 스키마에 인라인하지
-- 않는 이유는 _migrate_budget_incident_index와 동일 — 더티 DB의 중복
-- 행이 executescript 전체를 실패시키는 것을 막기 위함.
CREATE TABLE IF NOT EXISTS recovery_actions (
  id TEXT PRIMARY KEY,
  source_type TEXT NOT NULL,
  source_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  attempt_count INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 3,
  next_attempt_at TEXT,
  timeout_at TEXT,
  owner_type TEXT NOT NULL DEFAULT 'system',
  outcome TEXT NOT NULL DEFAULT '',
  resolution_note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  resolved_at TEXT,
  org_id TEXT NOT NULL DEFAULT ''

);
CREATE INDEX IF NOT EXISTS idx_recovery_actions_status ON recovery_actions(status);
CREATE INDEX IF NOT EXISTS idx_recovery_actions_source
  ON recovery_actions(source_type, source_id);

-- agent-management 3차 (paperclip spec B): task 댓글 — author_type은
-- human|agent|system, content_type은 text|markdown. 길이/마크다운 검증과
-- HTML 비실행 표시는 서비스/라우터 계층이 담당한다.
CREATE TABLE IF NOT EXISTS task_comments (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  author_type TEXT NOT NULL,
  author_id TEXT NOT NULL DEFAULT '',
  body TEXT NOT NULL,
  content_type TEXT NOT NULL DEFAULT 'text',
  created_at TEXT NOT NULL,
  edited_at TEXT,
  org_id TEXT NOT NULL DEFAULT ''

);
CREATE INDEX IF NOT EXISTS idx_task_comments_task ON task_comments(task_id);

-- agent-management 3차 (paperclip spec B): task 산출물 — 메타데이터와
-- 안전한 URI만 저장한다. 서버가 URI를 fetch하지 않으며 file:///javascript:/
-- data:/UNC/경로 탈출은 서비스 계층이 거부한다.
CREATE TABLE IF NOT EXISTS task_artifacts (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  run_id TEXT,
  kind TEXT NOT NULL DEFAULT 'other',
  name TEXT NOT NULL DEFAULT '',
  uri TEXT NOT NULL DEFAULT '',
  checksum_sha256 TEXT,
  media_type TEXT NOT NULL DEFAULT '',
  size_bytes INTEGER,
  metadata TEXT NOT NULL DEFAULT '{}',
  created_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  org_id TEXT NOT NULL DEFAULT ''

);
CREATE INDEX IF NOT EXISTS idx_task_artifacts_task ON task_artifacts(task_id);
CREATE INDEX IF NOT EXISTS idx_task_artifacts_run ON task_artifacts(run_id);

-- agent-management 3차 (paperclip spec C): 반복 업무 루틴 —
-- schedule_kind(interval|cron_daily|cron_weekly) + timezone(IANA) +
-- schedule JSON. max_concurrent 기본 1, enabled는 INTEGER 플래그.
CREATE TABLE IF NOT EXISTS routines (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  project_id TEXT,
  task_template TEXT NOT NULL DEFAULT '{}',
  assignee_agent_id TEXT,
  schedule_kind TEXT NOT NULL DEFAULT 'interval',
  schedule TEXT NOT NULL DEFAULT '{}',
  timezone TEXT NOT NULL DEFAULT 'UTC',
  max_concurrent INTEGER NOT NULL DEFAULT 1,
  enabled INTEGER NOT NULL DEFAULT 1,
  next_run_at TEXT,
  last_run_at TEXT,
  created_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  org_id TEXT NOT NULL DEFAULT ''

);
CREATE INDEX IF NOT EXISTS idx_routines_enabled_next ON routines(enabled, next_run_at);

-- routine 1회 스케줄 실행 — idempotency_key UNIQUE로 동일
-- routine/scheduled_for의 중복 task 생성을 막는다.
CREATE TABLE IF NOT EXISTS routine_runs (
  id TEXT PRIMARY KEY,
  routine_id TEXT NOT NULL,
  scheduled_for TEXT NOT NULL,
  task_id TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  idempotency_key TEXT NOT NULL UNIQUE,
  error_code TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_routine_runs_routine ON routine_runs(routine_id);

-- agent-management 3차 (paperclip spec D): capability별 allow/deny 권한.
-- scope_id는 NOT NULL DEFAULT '' — SQLite UNIQUE는 NULL을 서로 다른
-- 값으로 취급해 중복 행을 허용하므로 빈 문자열로 정규화한다.
-- deny 우선/scope precedence 판정은 서비스 계층이 담당한다.
CREATE TABLE IF NOT EXISTS agent_permissions (
  id TEXT PRIMARY KEY,
  agent_id TEXT NOT NULL,
  capability TEXT NOT NULL,
  effect TEXT NOT NULL,
  scope_type TEXT NOT NULL DEFAULT 'global',
  scope_id TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  org_id TEXT NOT NULL DEFAULT '',
  UNIQUE(agent_id, capability, scope_type, scope_id, effect, org_id)
);
CREATE INDEX IF NOT EXISTS idx_agent_permissions_agent ON agent_permissions(agent_id);

-- spec 003 US4: 사용자 단위 capability 권한 — agent_permissions와 동일 어휘.
-- parent row가 없어 org_id는 요청 컨텍스트(ctx 조직)에서 스탬프한다.
CREATE TABLE IF NOT EXISTS user_permissions (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  capability TEXT NOT NULL,
  effect TEXT NOT NULL,
  scope_type TEXT NOT NULL DEFAULT 'global',
  scope_id TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  org_id TEXT NOT NULL DEFAULT '',
  UNIQUE(user_id, capability, scope_type, scope_id, effect, org_id)
);
CREATE INDEX IF NOT EXISTS idx_user_permissions_user ON user_permissions(user_id);

-- agent-management 3차 (paperclip spec E): 외부 오케스트레이터(Paperclip 등)
-- 읽기 전용 projection — provider+external_id UNIQUE. native
-- agents/tasks/runs와 ID 네임스페이스를 섞지 않고, 토큰/크리덴셜/
-- prompt·transcript 원문은 저장하지 않는다.
CREATE TABLE IF NOT EXISTS external_agents (
  id TEXT PRIMARY KEY,
  provider TEXT NOT NULL,
  external_id TEXT NOT NULL,
  company_id TEXT NOT NULL DEFAULT '',
  payload_summary TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(provider, company_id, external_id)
);
CREATE INDEX IF NOT EXISTS idx_external_agents_company ON external_agents(provider, company_id);

CREATE TABLE IF NOT EXISTS external_tasks (
  id TEXT PRIMARY KEY,
  provider TEXT NOT NULL,
  external_id TEXT NOT NULL,
  company_id TEXT NOT NULL DEFAULT '',
  assignee_external_id TEXT,
  status TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(provider, company_id, external_id)
);
CREATE INDEX IF NOT EXISTS idx_external_tasks_assignee
  ON external_tasks(provider, assignee_external_id);
-- idx_external_tasks_company는 company_id 컬럼이 있는 DB에서만 만들 수
-- 있으므로 SCHEMA가 아니라 _ensure_external_company_indexes()가 생성한다
-- (구 스키마 DB에서 executescript가 깨지는 것 방지).

CREATE TABLE IF NOT EXISTS external_runs (
  id TEXT PRIMARY KEY,
  provider TEXT NOT NULL,
  external_id TEXT NOT NULL,
  company_id TEXT NOT NULL DEFAULT '',
  task_external_id TEXT,
  agent_external_id TEXT,
  status TEXT NOT NULL DEFAULT '',
  token_cost_summary TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(provider, company_id, external_id)
);
CREATE INDEX IF NOT EXISTS idx_external_runs_task
  ON external_runs(provider, task_external_id);
-- idx_external_runs_company도 위와 동일 이유로 Python 측에서 생성.

-- provider+company별 동기화 커서 — 부분 실패 시 커서를 전진시키지 않는다.
CREATE TABLE IF NOT EXISTS external_sync_cursors (
  id TEXT PRIMARY KEY,
  provider TEXT NOT NULL,
  company_id TEXT NOT NULL DEFAULT '',
  cursor TEXT NOT NULL DEFAULT '',
  last_synced_at TEXT,
  last_error_code TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(provider, company_id)
);

-- 조직(Organization) — spec 003 US1. kind: personal|team, status: active|suspended|deleted
CREATE TABLE IF NOT EXISTS organizations (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  slug TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'team',
  status TEXT NOT NULL DEFAULT 'active',
  created_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
-- slug는 lower() 기준 전역 UNIQUE — deleted 행도 예약 유지(30일 유예 후 purge)
CREATE UNIQUE INDEX IF NOT EXISTS idx_organizations_slug_lower
  ON organizations(lower(slug));

-- 조직 멤버십 — role: org_owner|org_admin|member (앱 계층 검증)
CREATE TABLE IF NOT EXISTS organization_members (
  org_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  role TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  joined_at TEXT NOT NULL,
  PRIMARY KEY (org_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_org_members_user ON organization_members(user_id);
CREATE INDEX IF NOT EXISTS idx_org_members_role ON organization_members(org_id, role);


-- 조직 초대 — spec 003 US2. token_hash만 저장, 원문 미저장
CREATE TABLE IF NOT EXISTS org_invitations (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL,
  email TEXT NOT NULL,
  role TEXT NOT NULL,
  token_hash TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT 'pending',
  invited_by TEXT NOT NULL DEFAULT '',
  expires_at TEXT NOT NULL,
  used_at TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_org_invites_lookup
  ON org_invitations(org_id, email, status);
CREATE INDEX IF NOT EXISTS idx_org_invites_email ON org_invitations(email);


-- spec 004 US1 — 아웃바운드 웹훅 구독 (조직 스코프).
-- secret_enc는 Fernet 암호문만 저장, 원문 미저장(FR-005).
CREATE TABLE IF NOT EXISTS webhook_subscriptions (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL DEFAULT '',
  url TEXT NOT NULL,
  secret_enc TEXT NOT NULL,
  events TEXT NOT NULL DEFAULT '[]',
  enabled INTEGER NOT NULL DEFAULT 1,
  created_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  last_delivery_at TEXT
);

CREATE TABLE IF NOT EXISTS webhook_deliveries (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL DEFAULT '',
  subscription_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  event_id TEXT NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0,
  next_retry_at TEXT,
  last_http_status INTEGER,
  last_error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  delivered_at TEXT,
  UNIQUE(subscription_id, event_id)
);
CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_due
  ON webhook_deliveries(status, next_retry_at);
CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_sub
  ON webhook_deliveries(subscription_id, created_at);

-- spec 004 US3 — 백업 실행 이력 (플랫폼 레벨 — org 스코프 아님)
CREATE TABLE IF NOT EXISTS backup_runs (
  id TEXT PRIMARY KEY,
  backend TEXT NOT NULL,
  file_path TEXT NOT NULL DEFAULT '',
  size_bytes INTEGER,
  status TEXT NOT NULL,
  verify_status TEXT NOT NULL DEFAULT 'pending',
  error TEXT NOT NULL DEFAULT '',
  started_at TEXT NOT NULL,
  finished_at TEXT,
  verified_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_backup_runs_started ON backup_runs(started_at);



-- spec 005 US1 — 데이터 보존 정책 override + purge 실행 이력 (플랫폼 레벨)
CREATE TABLE IF NOT EXISTS retention_policies (
  data_type TEXT PRIMARY KEY,
  ttl_days INTEGER NOT NULL,
  mode TEXT NOT NULL DEFAULT 'hard_delete',
  updated_by TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS purge_runs (
  id TEXT PRIMARY KEY,
  dry_run INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'running',
  counts_json TEXT NOT NULL DEFAULT '{}',
  started_at TEXT NOT NULL,
  finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_purge_runs_started ON purge_runs(started_at);

-- spec 005 US2 — 동의 이력(추가 전용) + 계정 삭제 영수증(해시 식별자만)
CREATE TABLE IF NOT EXISTS consent_records (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  terms_version TEXT NOT NULL,
  privacy_version TEXT NOT NULL,
  agreed_at TEXT NOT NULL,
  ip TEXT NOT NULL DEFAULT '',
  user_agent TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_consent_records_user ON consent_records(user_id, created_at);

CREATE TABLE IF NOT EXISTS deletion_receipts (
  id TEXT PRIMARY KEY,
  user_id_hash TEXT NOT NULL,
  requested_at TEXT NOT NULL,
  completed_at TEXT,
  counts_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'completed'
);
CREATE INDEX IF NOT EXISTS idx_deletion_receipts_hash ON deletion_receipts(user_id_hash);




-- spec 006 US1 — 알림 규칙 (조직 리소스)
CREATE TABLE IF NOT EXISTS alert_rules (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL DEFAULT '',
  name TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  condition_json TEXT NOT NULL DEFAULT '{}',
  channels_json TEXT NOT NULL DEFAULT '[]',
  cooldown_minutes INTEGER NOT NULL DEFAULT 60,
  last_fired_at TEXT,
  created_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alert_rules_org ON alert_rules(org_id, enabled);

CREATE TABLE IF NOT EXISTS alert_rule_fires (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL DEFAULT '',
  rule_id TEXT NOT NULL,
  event_fingerprint TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'fired',
  channels_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alert_rule_fires_dedup
  ON alert_rule_fires(rule_id, event_fingerprint, created_at);

-- spec 006 US5 — 사용자별 대시보드 설정/저장 뷰 (사용자 레벨)
CREATE TABLE IF NOT EXISTS user_dashboard_prefs (
  user_id TEXT PRIMARY KEY,
  cards_json TEXT NOT NULL DEFAULT '{}',
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_views (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  name TEXT NOT NULL,
  layout_json TEXT NOT NULL DEFAULT '{}',
  is_default INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_user_views_user_name ON user_views(user_id, name);
CREATE INDEX IF NOT EXISTS idx_user_views_user ON user_views(user_id);

-- spec 006 US6 — 온보딩 상태 (사용자 레벨)
CREATE TABLE IF NOT EXISTS user_onboarding (
  user_id TEXT PRIMARY KEY,
  dismissed INTEGER NOT NULL DEFAULT 0,
  completed_at TEXT,
  checklist_json TEXT NOT NULL DEFAULT '{}',
  updated_at TEXT NOT NULL
);

-- spec 007 US7 — 이상 탐지 이벤트 (조직 리소스).
-- fingerprint에 kind+metric+날짜를 넣어 같은 이상이 24h 내 재알림되지
-- 않게 하고, 다음 날 동일 패턴은 새 이벤트로 기록된다.
CREATE TABLE IF NOT EXISTS anomaly_events (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL,
  metric TEXT NOT NULL DEFAULT '',
  value REAL NOT NULL DEFAULT 0,
  baseline REAL NOT NULL DEFAULT 0,
  message TEXT NOT NULL DEFAULT '',
  fingerprint TEXT NOT NULL DEFAULT '',
  detected_at TEXT NOT NULL,
  UNIQUE(org_id, fingerprint)
);
CREATE INDEX IF NOT EXISTS idx_anomaly_events_detected
  ON anomaly_events(org_id, detected_at);


-- spec 008 US1 — OTLP 인제스트 토큰(조직 리소스). token_hash만 저장, 원문 미보관.
CREATE TABLE IF NOT EXISTS otlp_ingest_tokens (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL DEFAULT '',
  name TEXT NOT NULL,
  token_hash TEXT NOT NULL,
  created_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  last_used_at TEXT,
  revoked_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_otlp_tokens_hash ON otlp_ingest_tokens(token_hash);
CREATE INDEX IF NOT EXISTS idx_otlp_tokens_org ON otlp_ingest_tokens(org_id);

-- spec 008 US1 — 외부 OTLP 스팬(jobMate 밖 에이전트/LLM 호출).
-- run_id는 attributes의 jobmate.run_id가 유효할 때만 링크.
CREATE TABLE IF NOT EXISTS external_spans (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL DEFAULT '',
  trace_id TEXT NOT NULL,
  span_id TEXT NOT NULL,
  parent_span_id TEXT NOT NULL DEFAULT '',
  name TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL DEFAULT '',
  started_at TEXT NOT NULL,
  ended_at TEXT,
  attributes TEXT NOT NULL DEFAULT '{}',
  run_id TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_external_spans_trace ON external_spans(org_id, trace_id);
CREATE INDEX IF NOT EXISTS idx_external_spans_run ON external_spans(org_id, run_id);
CREATE INDEX IF NOT EXISTS idx_external_spans_started ON external_spans(org_id, started_at);

-- spec 008 US2 — 온라인 평가 스케줄(조직 리소스). run/stage 완료 시 sample_rate로 eval 실행.
CREATE TABLE IF NOT EXISTS eval_schedules (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL DEFAULT '',
  name TEXT NOT NULL,
  target TEXT NOT NULL DEFAULT 'run_completed',
  workflow_key TEXT NOT NULL DEFAULT '',
  sample_rate REAL NOT NULL DEFAULT 1.0,
  eval_name TEXT NOT NULL DEFAULT '',
  enabled INTEGER NOT NULL DEFAULT 1,
  created_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_eval_schedules_org ON eval_schedules(org_id, enabled);

-- spec 008 US3 — 리포트 구독(조직 리소스). daily/weekly 리포트의 email/slack/webhook 전달.
CREATE TABLE IF NOT EXISTS report_subscriptions (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL,
  channel TEXT NOT NULL,
  target TEXT NOT NULL DEFAULT '',
  enabled INTEGER NOT NULL DEFAULT 1,
  last_sent_at TEXT,
  created_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  secret_enc TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_report_subs_org ON report_subscriptions(org_id, enabled);

"""
