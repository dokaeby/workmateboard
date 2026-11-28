/** runs feature React Query 훅 — index.tsx(Fast Refresh)와 분리해 컴포넌트 파일을 순수하게 유지. */
import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { apiGet, ApiError } from "../../shared/api/client";
import { useAppStore, roleAllows } from "../../shared/store/appStore";
import type { Run, RunDiff, AuditEvent, RunListItem } from "../../shared/api/types";

export interface RunsFilter {
  status?: string;
  days?: string;
  q?: string;
  agent?: string;
}

/** spec 012 US3 — GET /runs?limit=&status=&days=&q=&agent= (모두 선택, AND 조건).
 *  빈 값은 파라미터에서 제외한다. queryKey가 필터를 포함하므로
 *  ["runs"] prefix 무효화(SSE/새 실행)는 그대로 동작한다. */
export function useRunsList(filter: RunsFilter = {}) {
  const params = new URLSearchParams({ limit: "200" });
  if (filter.status) params.set("status", filter.status);
  if (filter.days) params.set("days", filter.days);
  if (filter.q) params.set("q", filter.q);
  if (filter.agent) params.set("agent", filter.agent);
  const qs = params.toString();
  return useQuery({
    queryKey: ["runs", "list", filter.status || "", filter.days || "", filter.q || "", filter.agent || ""],
    queryFn: () => apiGet<RunListItem[]>(`/runs?${qs}`),
    refetchInterval: 10000,
  });
}

/** 입력 디바운스 — runs 필터 q/agent에 사용 (300ms). */
export function useDebounced<T>(value: T, ms = 300): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const id = window.setTimeout(() => setV(value), ms);
    return () => window.clearTimeout(id);
  }, [value, ms]);
  return v;
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
  // /runs/{id}/audit은 lead+ — member는 비활성, 403 수신 시 폴링 중지
  const role = useAppStore((s) => s.role);
  return useQuery({
    queryKey: ["run-audit", id],
    queryFn: () => apiGet<AuditEvent[]>(`/runs/${encodeURIComponent(id!)}/audit`),
    enabled: !!id && roleAllows("lead", role),
    refetchInterval: (q) =>
      q.state.error instanceof ApiError && q.state.error.status === 403 ? false : 3000,
    retry: 1,
  });
}
