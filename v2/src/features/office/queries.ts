/** office 관측 대시보드 데이터 레이어 — 모든 쿼리는 이 훅에서 정의.
 *  queryKey prefix는 shared/stream.ts의 SSE 무효화 목록("runs"/"run"/
 *  "approvals"/"agent-sessions")과 맞춰 실시간 갱신을 받는다.
 *  lead 전용 엔드포인트는 retry:false + 에러 시 해당 섹션만 강등 표시. */
import { useQuery } from "@tanstack/react-query";
import { apiGet } from "../../shared/api/client";
import type {
  LlmStats, ObsAnomaliesResponse, ObsApprovalsResponse, ObsCostsResponse,
  ObsErrorsResponse, RunListItem, RunStats, SessionRow, UsageSummary,
} from "./types";

const opt = { retry: false } as const;

export function useOfficeData() {
  const runsQ = useQuery({
    queryKey: ["runs", "office", "list"],
    queryFn: () => apiGet<RunListItem[]>("/runs?limit=50"),
    refetchInterval: 8000,
  });
  const statsQ = useQuery({
    queryKey: ["runs", "office", "stats"],
    queryFn: () => apiGet<RunStats>("/runs/stats?days=7"),
    refetchInterval: 30000,
    ...opt,
  });
  const llmQ = useQuery({
    queryKey: ["runs", "office", "llm"],
    queryFn: () => apiGet<LlmStats>("/llm-monitor/stats?days=7"),
    refetchInterval: 30000,
    ...opt,
  });
  const usageQ = useQuery({
    queryKey: ["usage", "office", "summary"],
    queryFn: () => apiGet<UsageSummary>("/usage/summary?days=30"),
    refetchInterval: 60000,
    ...opt,
  });
  const sessionsQ = useQuery({
    queryKey: ["agent-sessions", "office"],
    queryFn: () => apiGet<SessionRow[] | { sessions?: SessionRow[]; agent_sessions?: SessionRow[] }>(
      "/agent-sessions?limit=50&include_ended=false",
    ),
    refetchInterval: 10000,
  });
  const costsQ = useQuery({
    queryKey: ["runs", "office", "costs"],
    queryFn: () => apiGet<ObsCostsResponse>("/observability/costs?days=30&group_by=day"),
    refetchInterval: 60000,
    ...opt,
  });
  const approvalsQ = useQuery({
    queryKey: ["approvals", "office"],
    queryFn: () => apiGet<ObsApprovalsResponse>("/observability/approvals"),
    refetchInterval: 15000,
    ...opt,
  });
  const errorsQ = useQuery({
    queryKey: ["runs", "office", "errors"],
    queryFn: () => apiGet<ObsErrorsResponse>("/observability/errors?days=7"),
    refetchInterval: 60000,
    ...opt,
  });
  const anomaliesQ = useQuery({
    queryKey: ["runs", "office", "anomalies"],
    queryFn: () => apiGet<ObsAnomaliesResponse>("/observability/anomalies"),
    refetchInterval: 60000,
    ...opt,
  });

  const sessionList = Array.isArray(sessionsQ.data)
    ? sessionsQ.data
    : sessionsQ.data?.sessions || sessionsQ.data?.agent_sessions || [];

  return {
    runsQ, statsQ, llmQ, usageQ, sessionsQ, costsQ, approvalsQ, errorsQ, anomaliesQ,
    sessionList,
  };
}

export type OfficeData = ReturnType<typeof useOfficeData>;
