import type { Run, StageRun, AgentSession } from "../../shared/api/types";

// 공유 API 타입 재수출 — office 모듈들은 "./types"에서 import
export type { RunListItem, WorkflowInfo } from "../../shared/api/types";

export interface AuditEvent {
  id?: string;
  actor?: string;
  action?: string;
  object_ref?: string;
  detail?: Record<string, unknown>;
  created_at?: string;
}

export interface ApprovalRecord {
  stage_key?: string;
  decision?: string;
  decided_by?: string;
  decided_at?: string;
  comment?: string;
}

export interface OfficeStageRun extends StageRun {
  tokens_in?: number;
  tokens_out?: number;
  model?: string;
}

export interface RunDetail extends Run {
  project?: string;
  stages?: OfficeStageRun[];
  approvals?: ApprovalRecord[];
  audit?: AuditEvent[];
  metadata?: Record<string, unknown>;
}

export interface SessionView extends AgentSession {
  health?: string;
  est_cost_usd?: number;
  ended_summary?: string;
  artifacts?: string | unknown[];   // TEXT JSON 컬럼 — 문자열 또는 배열
}

export interface Schedule {
  id: string;
  workflow?: string;
  input?: Record<string, unknown> | string | null;
  interval_minutes?: number;
  enabled?: boolean;
  next_run_at?: string | null;
  last_run_at?: string | null;
}

export interface SessionStatsTool {
  sessions?: number;
  working_seconds?: number;
  decisions?: number;
  ended?: number;
  last_active?: string;
}

export interface SessionStatsResponse {
  tools?: Record<string, SessionStatsTool>;
  hourly?: { hour?: number; working_seconds?: number }[];
}

export interface EndedSessionsResponse {
  sessions?: SessionView[];
  agent_sessions?: SessionView[];
}

export interface ActivityItem {
  at?: string;
  status?: string;
  doing?: string;
  project?: string;
}
