/** Agent Office — dev_review run 파이프라인 + 활성 에이전트 세션 + 활동 피드 + 스프라이트 사무실.
 * 레거시 index.html의 loadOffice/officeState 기능을 React Query 기반으로 이행.
 * 상단 스프라이트 보드는 레거시 office/(office-view/life/world/path/atlas + jobmate-bridge)
 * 의 2D 캔버스 사무실을 sprites/ 모듈로 이식한 것. */
import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { apiGet } from "../../shared/api/client";
import { useI18n, registerI18n } from "../../shared/i18n";
import { PageHead, Card, Empty, Spinner, ErrorBox } from "../../shared/ui";
import { fmtTs } from "../../shared/lib/format";
import { OfficeStageBoard } from "./StageSection";
import { useOfficeDemo } from "./demoState";
import { STR } from "./i18n";
import { pickRun, StatusBadge } from "./bits";
import { PipelineCard, RunDetailPanel, AuditFeed } from "./RunPanels";
import { OfficeBottomGrid } from "./BottomGrid";
import { OfficeSpriteSection } from "./SpriteSection";
import type { RunListItem } from "./types";
import "./office.css";

registerI18n(STR);

export default function OfficePage() {
  const { t } = useI18n();
  const [selRunId, setSelRunId] = useState<string | null>(null);
  const [officeMode, setOfficeMode] = useState<"board" | "sprites">("board");
  // 레거시 #office-demo-btn — 보드/스프라이트 공용 데모 캐스트 토글
  const [demo, setDemo] = useOfficeDemo();

  const { data: runs, isLoading, error, refetch } = useQuery({
    queryKey: ["office-runs"],
    queryFn: () => apiGet<RunListItem[]>("/runs?workflow=dev_review&limit=50"),
    refetchInterval: 8000,
  });

  const picked = useMemo(() => pickRun(runs || []), [runs]);
  // 기본 선택: 자동 선택된 run. 사용자가 목록에서 다른 run을 고르면 유지.
  const activeRunId = selRunId || picked?.id || null;

  // 목록에서 사라진 run을 선택 중이면 자동 선택으로 복귀
  useEffect(() => {
    if (selRunId && runs && !runs.some((r) => r.id === selRunId)) setSelRunId(null);
  }, [runs, selRunId]);

  return (
    <div className="w-full space-y-4 p-4">
      <PageHead title={t("office.title", "Agent Office")} desc={t("office.desc", "dev_review 워크플로 진행 상황과 활성 에이전트 세션 — 자동 갱신")} />

      {/* 관제 모드 토글 — 레거시 #office-view-toggle (3D 보드 / 스프라이트 사무실) */}
      <div className="flex items-center justify-end gap-2">
        <div className="flex rounded-lg border border-border bg-surface p-0.5" role="group" aria-label={t("office.modeLabel", "관제 모드")}>
          {(["board", "sprites"] as const).map((m) => (
            <button
              key={m}
              type="button"
              aria-pressed={officeMode === m}
              onClick={() => setOfficeMode(m)}
              className={`rounded-md px-3 py-1 text-xs transition-colors ${
                officeMode === m ? "bg-accent-soft font-semibold text-accent" : "text-muted hover:text-ink"
              }`}
            >
              {m === "board" ? t("office.mode.board", "3D 보드") : t("office.mode.sprites", "스프라이트 사무실")}
            </button>
          ))}
        </div>
        <button
          type="button"
          aria-pressed={demo}
          onClick={() => setDemo(!demo)}
          className={`rounded-md px-3 py-1 text-xs transition-colors ${
            demo ? "bg-accent font-semibold text-white" : "text-muted hover:text-ink"
          }`}
        >
          {t("office.demo", "데모")}
        </button>
      </div>

      {/* 두 모드 모두 마운트 유지 — 토글 시 스프라이트 시뮬레이션/보드 상태가 보존된다 (레거시와 동일) */}
      <div hidden={officeMode !== "board"}>
        <OfficeStageBoard runId={activeRunId} />
      </div>
      <div hidden={officeMode !== "sprites"}>
        <OfficeSpriteSection paused={officeMode !== "sprites"} />
      </div>

      <div className="grid gap-4 lg:grid-cols-[240px_1fr]">
        {/* 실행 목록 */}
        <Card title={t("office.runs", "실행 목록")} className="max-h-[560px] overflow-y-auto">
          {isLoading && <Spinner />}
          {error && <ErrorBox message={error instanceof Error ? error.message : String(error)} onRetry={() => refetch()} />}
          {runs && !runs.length && <Empty>{t("office.noRun", "실행된 dev_review 워크플로가 없습니다")}</Empty>}
          {(runs || []).map((r) => (
            <button
              key={r.id}
              onClick={() => setSelRunId(r.id === selRunId ? null : r.id)}
              className={`mb-1 flex w-full items-center gap-2 rounded-md border px-2 py-1.5 text-left last:mb-0 ${
                r.id === activeRunId ? "border-accent bg-accent-soft" : "border-border bg-surface-hover hover:bg-surface"
              }`}
            >
              <StatusBadge status={r.status} />
              <div className="min-w-0 flex-1">
                <div className="truncate text-xs font-medium text-ink">{r.id.slice(0, 8)}</div>
                <div className="text-[11px] text-muted">{fmtTs(r.created_at)}</div>
              </div>
            </button>
          ))}
        </Card>

        {/* 파이프라인 + 상세 */}
        <div className="space-y-4">
          <Card>
            <PipelineCard runId={activeRunId} />
          </Card>
          <div className="grid gap-4 lg:grid-cols-2">
            <Card title={t("office.runDetail", "실행 상세")}>
              <RunDetailPanel runId={activeRunId} />
            </Card>
            <AuditFeed runId={activeRunId} />
          </div>
        </div>
      </div>

      {/* 하단 정보 — 세션 | 활동 | 통계+스케줄 3열 (OfficeBottomGrid 내부 그리드) */}
      <OfficeBottomGrid />
    </div>
  );
}
