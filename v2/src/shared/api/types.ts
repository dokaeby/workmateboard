/** API 공통 타입 — 백엔드 응답 스키마의 프론트 측 표현. */

export type Role = "member" | "lead" | "admin";
export type ViewKey =
  | "office" | "monitor" | "radar" | "gmail" | "calendar" | "runs" | "inbox"
  | "projects" | "sessionlog" | "risks" | "approvals" | "perf"
  | "insights" | "automation" | "system" | "archive" | "cost"
  | "developer" | "playbooks" | "connectors" | "triggers" | "builder"
  | "team" | "audit" | "settings" | "agents" | "goals" | "evals";

export interface Me {
  id?: string;
  email?: string;
  display_name?: string;
  name?: string;
  role?: Role;
  email_verified?: boolean;
}

export interface ApprovalItem {
  kind: string;          // "run" | "session" | "trigger" | ...
  id: string;
  title?: string;
  project?: string;
  status?: string;       // run: waiting_approval | waiting_review
  waiting_since?: string;
  age_seconds?: number;
  session_key?: string;
  tool?: string;
  title_key?: string;
  title_params?: Record<string, unknown>;
}
export interface ApprovalsResponse { items: ApprovalQueueItem[] }

/** 2차 범용 승인 요청 — /approvals 큐에 kind="approval_request"로 합류한다.
 *  결정은 POST /management/approvals/{id}/decision (pending 상태에서만). */
export interface ApprovalRequestItem {
  kind: "approval_request";
  id: string;
  action_type?: string;   // run_resume|budget_raise|agent_hire|agent_terminate|external_send|deploy|destructive_tool|recovery
  risk_level?: string;    // low|medium|high|critical
  status?: string;        // pending|approved|rejected|expired|cancelled
  summary?: string;
  resource_type?: string;
  resource_id?: string;
  safe_payload?: Record<string, unknown>;
  task_id?: string;
  run_id?: string;
  expires_at?: string | null;
  created_at?: string;
  decision_comment?: string | null;
  title?: string;
  title_key?: string;
  title_params?: Record<string, unknown>;
}
export type ApprovalQueueItem = ApprovalItem | ApprovalRequestItem;

export interface Run {
  id: string;
  workflow_key?: string;
  workflow?: string;
  version?: string;
  status: string;
  input?: Record<string, unknown>;
  output?: Record<string, unknown>;
  error?: string;
  created_at?: string;
  updated_at?: string;
  cost_usd?: number;
  approvals?: { stage_key?: string; decision?: string; decided_by?: string; decided_at?: string; comment?: string }[];
  audit?: unknown[];
}

export interface InboxItem {
  id?: string | number;
  kind?: string;
  title?: string;
  body?: string;
  link?: string;
  read?: boolean;
  created_at?: string;
}

export interface RunStats {
  workflow?: string | null;
  days?: number;
  runs?: number;
  succeeded?: number;
  failed?: number;
  running?: number;
  waiting_approval?: number;
  avg_duration_s?: number | null;
  total_tokens_in?: number;
  total_tokens_out?: number;
  stage_failures?: Record<string, number>;
  daily?: { date: string; runs: number; tokens: number }[];
}

export interface RunDiffFile {
  path?: string;
  status?: string;
  additions?: number;
  deletions?: number;
  diff?: string;
}
export interface RunDiff {
  files?: RunDiffFile[];
  summary?: string;
}

/** 2차 비용 원장 — 금액은 정수 microusd(1 USD = 1,000,000).
 *  unpriced 이벤트는 cost_microusd가 null이며 0으로 대체하지 않는다. */
export type CostStatus = "reported" | "estimated" | "unpriced" | (string & {});
export interface CostEvent {
  id: string;
  agent_id?: string;
  task_id?: string | null;
  run_id?: string | null;
  stage_run_id?: string | null;
  provider?: string;
  model?: string;
  billing_type?: string;
  cost_status?: CostStatus;
  input_tokens?: number;
  cached_input_tokens?: number;
  output_tokens?: number;
  cost_microusd?: number | null;
  pricing_source?: string | null;
  pricing_version?: string | null;
  occurred_at?: string;
  created_at?: string;
}
export interface CostEventsResponse { items?: CostEvent[] }

export interface CostSummaryAgent {
  agent_id?: string;
  total_microusd?: number;
  unpriced_count?: number;
}
export interface CostSummary {
  total_microusd?: number;
  unpriced_count?: number;
  by_agent?: CostSummaryAgent[];
  [k: string]: unknown;
}

/** 2차 예산 정책/인시던트 — amount_microusd가 authoritative, *_usd는 파생 레거시. */
export interface BudgetPolicy {
  id: string;
  scope_type?: string;
  scope_id?: string;
  amount_usd?: number | null;
  amount_microusd?: number | null;
  unpriced_usage_policy?: "block" | "allow" | (string & {});
  reservation_microusd?: number;
  warn_percent?: number;
  hard_stop_enabled?: boolean;
  is_active?: boolean;
  month_spend_usd?: number;
  month_spend_microusd?: number | null;
  used_pct?: number;
  status?: string;
  open_incidents?: number;
}
export interface BudgetOverview { policies?: BudgetPolicy[]; month?: string }

export interface BudgetIncident {
  id: string;
  policy_id?: string;
  scope_type?: string;
  scope_id?: string;
  window_start?: string;
  threshold_type?: "soft" | "hard" | (string & {});
  amount_limit?: number | null;
  amount_limit_microusd?: number | null;
  amount_observed?: number | null;
  observed_spend_microusd?: number | null;
  unpriced_event_count?: number;
  status?: string;
  note?: string;
  created_at?: string;
  resolved_at?: string | null;
}
export interface IncidentsResponse { items?: BudgetIncident[] }

export interface AuditEvent {
  id?: string;
  actor?: string;
  action?: string;
  object_ref?: string;
  detail?: Record<string, unknown> | string;
  created_at?: string;
}

export interface StageRun {
  id?: string;
  stage_key?: string;
  stage?: string;
  status?: string;
  attempt?: number;
  input?: Record<string, unknown>;
  output?: Record<string, unknown>;
  error?: string;
  started_at?: string;
  finished_at?: string;
}

export interface AgentSession {
  tool?: string;
  session_key?: string;
  project?: string;
  title?: string;
  status?: string;   // working | decision | rest | ended
  doing?: string;
  ask?: string;
  last_event_at?: string;
  updated_at?: string;
  machine?: string;
  tokens_in?: number;
  tokens_out?: number;
}

export interface Prefs {
  sidebar_menu?: Record<string, boolean>;
  radar_interval?: string;
  gmail_check_minutes?: number;
  monthly_budget_usd?: number;
  monthly_budget_microusd?: number;
  notify_rules?: unknown[];
  [k: string]: unknown;
}

/** /runs 목록 아이템 — office/types.ts·runs 뷰 공용. */
export interface RunListItem {
  id: string;
  workflow?: string;
  status: string;
  project?: string;
  created_at?: string;
  coalesced_count?: number;
}

/** /workflows 응답 — office/triggers/playbooks 공용 (필드는 뷰마다 부분집합 사용). */
export interface WorkflowInfo {
  key?: string;
  description?: string;
}

/** /playbooks 응답 — triggers는 {id,name} 부분집합만 사용. */
export interface Playbook {
  id: string;
  name?: string;
  workflow?: string;
  input?: Record<string, unknown>;
  created_by?: string;
  created_at?: string;
}
export interface PlaybooksResponse { items?: Playbook[] }

/** /workflows/custom 항목 — builder는 created_at까지 사용. */
export interface CustomWorkflow {
  name: string;
  spec?: unknown;
  created_at?: string;
  latest_revision?: number;
}

/** /health/detail — settings/system 공용 (머신 필드는 동일 구조). */
export interface HealthMachine {
  machine?: string;
  last_push_at?: string;
  sessions?: number;
}
export interface HealthDetail {
  api?: { uptime_s?: number };
  db?: { type?: string; path?: string | null; size_bytes?: number | null; [k: string]: unknown };
  bridge?: { last_push_at?: string; machines?: HealthMachine[] };
  cron?: { last_tick_at?: string; reaper?: Record<string, unknown>; [k: string]: unknown };
  version?: string;
}
