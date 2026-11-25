import { useQuery } from "@tanstack/react-query";
import { apiGet } from "../../shared/api/client";
import type { RunDetail } from "./types";

export function useRunDetail(id: string | null) {
  return useQuery({
    queryKey: ["office-run", id],
    queryFn: () => apiGet<RunDetail>(`/runs/${encodeURIComponent(id!)}`),
    enabled: !!id,
    refetchInterval: (q) => {
      const s = (q.state.data as RunDetail | undefined)?.status;
      return s === "running" || s === "queued" || s === "waiting_approval" ? 5000 : 10000;
    },
  });
}
