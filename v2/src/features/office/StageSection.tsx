/** Agent-Office 3D 보드 섹션 — run 상세를 StageBoard에 연결.
 * 레거시 index.html의 office-stage 보드 + 승인/거절 + diff 미리보기에 해당. */
import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { apiGet, apiPost, ApiError } from "../../shared/api/client";
import type { RunDiff } from "../../shared/api/types";
import { StageBoard } from "./board";
import type { BoardRun, BoardDiff, BoardStageRun } from "./board";
import { useOfficeDemo } from "./demoState";
import { liveSessionsState, type LiveSession } from "./liveSessions";

interface OfficeRunDetail extends BoardRun {
  stages?: BoardStageRun[];
}

function useOfficeRunDetail(id: string | null) {
  return useQuery({
    queryKey: ["office-run", id],
    queryFn: () => apiGet<OfficeRunDetail>(`/runs/${encodeURIComponent(id!)}`),
    enabled: !!id,
    refetchInterval: (q) => {
      const s = q.state.data?.status;
      return s === "running" || s === "queued" || s === "waiting_approval" ? 5000 : 10000;
    },
  });
}

/** 3D 보드 모드 — run이 없으면 demo 토글에 따라 가상 run을 보여준다. */
export function OfficeStageBoard({ runId }: { runId: string | null }) {
  const qc = useQueryClient();
  // 레거시 localStorage 'jobmate.officeDemo' — 스프라이트 모드와 공유 상태.
  // 토글 버튼은 OfficePage 모드 토글 옆 공용 버튼 하나로 통합.
  const [demo] = useOfficeDemo();
  const { data: run, isPending: runPending } = useOfficeRunDetail(runId);
  const waiting = run?.status === "waiting_approval";

  // 레거시 officeLiveSessionsState — 데모 OFF + run 없음 + 활성 세션 있음이면
  // 실제 세션을 dev_review 모양의 가상 run으로 보드에 띄운다.
  // SessionsPanel·스프라이트 사무실과 같은 queryKey 라 요청이 합쳐진다.
  const liveEnabled = !demo && !run && !(runId && runPending);
  const [, setLiveTick] = useState(0);
  useEffect(() => {
    if (!liveEnabled) return;
    // 단계 순환(DEMO_STAGE_SEC=8s)이 매끄럽게 보이도록 1초마다 재합성
    const id = setInterval(() => setLiveTick((n) => n + 1), 1000);
    return () => clearInterval(id);
  }, [liveEnabled]);
  const sessionsQ = useQuery({
    queryKey: ["office-sessions"],
    queryFn: () => apiGet<LiveSession[]>("/agent-sessions?limit=50&include_ended=false"),
    refetchInterval: 10000,
  });
  const live = liveEnabled ? liveSessionsState(sessionsQ.data || []) : null;

  const diff = useQuery({
    queryKey: ["run-diff", runId],
    queryFn: () => apiGet<RunDiff>(`/runs/${encodeURIComponent(runId!)}/diff`),
    enabled: !!runId && !!waiting,
  });

  const approve = async (decision: "approve" | "reject") => {
    if (!runId) return;
    try {
      await apiPost(`/runs/${encodeURIComponent(runId)}/approve`, { decision, comment: "" });
      qc.invalidateQueries({ queryKey: ["office-run", runId] });
      qc.invalidateQueries({ queryKey: ["office-runs"] });
      qc.invalidateQueries({ queryKey: ["run-diff", runId] });
    } catch (e) {
      alert(e instanceof ApiError ? e.message : String(e));
    }
  };

  return (
    <div>
      {/* 데모 토글은 OfficePage 모드 토글 옆 공용 버튼 하나로 통합 (보드/스프라이트 공용) */}
      <StageBoard
        run={run ?? live?.run ?? null}
        stageRuns={run?.stages ?? live?.stageRuns}
        demo={demo}
        onApprove={approve}
        diff={(diff.data as BoardDiff | undefined) ?? null}
        diffLoading={diff.isLoading}
      />
    </div>
  );
}
