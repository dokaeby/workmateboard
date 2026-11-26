/** runs feature React Query 훅 — index.tsx(Fast Refresh)와 분리해 컴포넌트 파일을 순수하게 유지. */
import { useQuery } from "@tanstack/react-query";
import { apiGet } from "../../shared/api/client";
import type { Run, RunDiff, AuditEvent, RunListItem } from "../../shared/api/types";

export function useRunsList() {
  return useQuery({
    queryKey: ["runs"],
    queryFn: () => apiGet<RunListItem[]>("/runs?limit=200"),
    refetchInterval: 10000,
  });
}

export function useRunDetail(id: string | null) {
  return useQuery({
    queryKey: ["run", id],
    queryFn: () => apiGet<Run>(`/runs/${encodeURIComponent(id!)}`),
    enabled: !!id,
    refetchInterval: (q) => {
      const s = (q.state.data as Run | undefined)?.status;
      return s === "running" || s === "queued" ? 3000 : false;
    },
  });
}

export function useRunDiff(id: string | null) {
  return useQuery({
    queryKey: ["run-diff", id],
    queryFn: () => apiGet<RunDiff>(`/runs/${encodeURIComponent(id!)}/diff`),
    enabled: !!id,
  });
}

export function useRunAudit(id: string | null) {
  return useQuery({
    queryKey: ["run-audit", id],
    queryFn: () => apiGet<AuditEvent[]>(`/runs/${encodeURIComponent(id!)}/audit`),
    enabled: !!id,
    refetchInterval: 3000,
    retry: 1,
  });
}
