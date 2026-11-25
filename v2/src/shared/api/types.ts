/** API 공통 타입 — 백엔드 응답 스키마의 프론트 측 표현. */

export type Role = "member" | "lead" | "admin";
export type ViewKey =
  | "office" | "monitor" | "radar" | "gmail" | "calendar" | "runs" | "inbox"
  | "projects" | "sessionlog" | "risks" | "approvals" | "perf"
  | "insights" | "automation" | "system" | "archive" | "cost"
  | "developer" | "playbooks" | "connectors" | "triggers" | "builder"
  | "team" | "audit" | "settings";

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
export interface ApprovalsResponse { items: ApprovalItem[] }

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
  notify_rules?: unknown[];
  [k: string]: unknown;
}
