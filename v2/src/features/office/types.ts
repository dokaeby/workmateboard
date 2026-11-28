/** office 관측 대시보드 로컬 타입 — shared/api/types.ts는 건드리지 않는다.
 *  신규 /observability/* 엔드포인트는 계약 기준으로 정의 (백엔드 병렬 개발). */
import type { AgentSession, RunListItem } from "../../shared/api/types";

export type { RunListItem };

export interface RunStage {
  id?: string;
  stage_key?: string;
  stage?: string;
  status?: string;
  attempt?: number;
  model?: string;
  tokens_in?: number;
  tokens_out?: number;
  error?: string;
  started_at?: string;
  finished_at?: string;
}

export interface RunDetail {
  id: string;
  workflow?: string;
  status?: string;
  error?: string;
  project?: string;
  created_at?: string;
  updated_at?: string;
  cost_usd?: number | null;
  stages?: RunStage[];
}

export interface RunStatsDaily { date?: string; runs?: number; tokens?: number }
export interface RunStats {
  runs?: number;
  succeeded?: number;
  failed?: number;
  running?: number;
  waiting_approval?: number;
  avg_duration_s?: number | null;
  total_tokens_in?: number;
  total_tokens_out?: number;
  daily?: RunStatsDaily[];
}

export interface LlmLatency { p50?: number | null; p95?: number | null; p99?: number | null; avg?: number | null }
export interface LlmRecentFailure {
  id: string;
  workflow?: string;
  status?: string;
  project?: string;
  error?: string;
  updated_at?: string;
}
export interface LlmStats {
  error_rate_pct?: number | null;
  latency_s?: LlmLatency;
  approval_backlog?: { waiting?: number; oldest_wait_s?: number | null; total_wait_s?: number | null };
  daily?: { date?: string; runs?: number; tokens?: number; errors?: number }[];
  recent_failures?: LlmRecentFailure[];
  tokens?: { total_in?: number; total_out?: number; total?: number };
}

export interface UsageByModel { model?: string; cost_usd?: number; tokens?: number; calls?: number }
export interface UsageSummary {
  total_cost_usd?: number;
  by_model?: UsageByModel[];
  by_day?: { date?: string; cost_usd?: number; tokens?: number }[];
}

export interface ObsCostItem {
  key?: string;
  cost_usd?: number;
  tokens_in?: number;
  tokens_out?: number;
  calls?: number;
  runs?: number;
}
export interface ObsCostsResponse { items?: ObsCostItem[] }

export interface ObsApprovalWaiting {
  run_id?: string;
  id?: string;
  workflow?: string;
  project?: string;
  stage_key?: string;
  waited_s?: number | null;
}
export interface ObsApprovalsResponse {
  waiting?: ObsApprovalWaiting[];
  stats?: { waiting?: number; oldest_wait_s?: number | null; total_wait_s?: number | null };
}

export interface ObsErrorClass {
  error_class?: string;
  count?: number;
  pct?: number | null;
  samples?: (string | { run_id?: string; stage_key?: string; error?: string; error_class?: string; at?: string })[];
}
export interface ObsErrorsResponse {
  total?: number;
  by_class?: ObsErrorClass[];
}

/** /observability/anomalies — 항목 스키마가 백엔드와 협의 중이라 관대하게 읽는다. */
export interface ObsAnomaly {
  id?: string;
  kind?: string;
  type?: string;
  severity?: string;
  title?: string;
  message?: string;
  description?: string;
  metric?: string;
  value?: number | string;
  threshold?: number | string;
  created_at?: string;
  detected_at?: string;
  [k: string]: unknown;
}
export interface ObsAnomaliesResponse { items?: ObsAnomaly[] }

export interface SessionRow extends AgentSession {
  health?: string;
  est_cost_usd?: number | null;
  title?: string;
  created_at?: string;
  last_event_at?: string;
  updated_at?: string;
}

/** spec 009 — OTLP 외부 텔레메트리 (external_metrics / external_logs). */
export interface ExternalMetric {
  id?: string | number;
  metric_name?: string;
  metric_type?: string;
  value?: number | string | null;
  unit?: string;
  attributes?: Record<string, unknown> | null;
  timestamp?: string;
}
export interface ExternalMetricsResponse { items?: ExternalMetric[] }

export interface ExternalLog {
  id?: string | number;
  trace_id?: string | null;
  span_id?: string | null;
  severity?: string | null;
  severity_text?: string | null;
  severity_number?: number | null;
  body?: string | null;
  attributes?: Record<string, unknown> | null;
  timestamp?: string;
}
export interface ExternalLogsResponse { items?: ExternalLog[] }
