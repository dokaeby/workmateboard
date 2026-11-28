/** 관측 센터 — 상용 관측 대시보드 (spec 007 US6).
 *  스프라이트/3D 보드 제거 후 run·세션·비용·오류·승인 실데이터 중심 재구성.
 *  SSE(/api/v1/stream) 이벤트가 runs/run/approvals/agent-sessions queryKey를
 *  무효화하고, 각 쿼리는 refetchInterval로 폴링 백업한다. */
import { useEffect, useMemo, useState } from "react";
import { useI18n } from "../../shared/i18n";
import { Button, PageHead } from "../../shared/ui";
import { usd } from "../../shared/lib/format";
import { useOfficeData } from "./queries";
import {
  AttentionStrip, CostSection, ErrorSection, KpiCard, LiveRuns, SessionsSection,
} from "./sections";
import "./i18n";

function fmtSecs(v: number | null | undefined): string {
  if (v == null || !isFinite(v)) return "-";
  if (v < 60) return `${Math.round(v)}s`;
  if (v < 3600) return `${(v / 60).toFixed(1)}m`;
  return `${(v / 3600).toFixed(1)}h`;
}

function pct1(v: number | null | undefined): string {
  return v == null || !isFinite(v) ? "-" : `${v.toFixed(1)}%`;
}

function scrollTo(id: string) {
  document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "start" });
}

export default function OfficePage() {
  const { t } = useI18n();
  const d = useOfficeData();
  // 경과 시간 표시용 분 단위 리렌더 틱
  const [, setTick] = useState(0);
  useEffect(() => {
    const id = window.setInterval(() => setTick((n) => n + 1), 30000);
    return () => window.clearInterval(id);
  }, []);

  const kpis = useMemo(() => {
    const stats = d.statsQ.data;
    const llm = d.llmQ.data;
    const usage = d.usageQ.data;

    // 오늘 실행 — /runs/stats daily는 UTC 날짜 키 (로컬 TZ와 9시간 어긋남)
    const today = new Date().toISOString().slice(0, 10);
    const runsToday = stats?.daily
      ? (stats.daily.find((x) => x.date === today)?.runs ?? 0)
      : null;

    const succeeded = Number(stats?.succeeded) || 0;
    const failed = Number(stats?.failed) || 0;
    const terminal = succeeded + failed;
    const successRate = terminal > 0 ? (succeeded / terminal) * 100 : null;

    const errorRate = llm?.error_rate_pct ?? (stats?.runs ? (failed / Number(stats.runs)) * 100 : null);

    const waitingRuns = (d.runsQ.data || []).filter((r) => r.status === "waiting_approval").length;
    const backlog =
      d.approvalsQ.data?.stats?.waiting ?? llm?.approval_backlog?.waiting ?? (waitingRuns || null);

    return {
      runsToday,
      successRate,
      errorRate,
      p95: llm?.latency_s?.p95 ?? null,
      // "월 누적"은 UTC 이번 달 by_day 합산 — total_cost_usd는 30일 윈도우 합계
      costMtd: (() => {
        const ym = new Date().toISOString().slice(0, 7);
        const days = usage?.by_day || [];
        if (!days.length) return usage?.total_cost_usd ?? null;
        return days
          .filter((x) => String(x.date || "").startsWith(ym))
          .reduce((a, x) => a + (Number(x.cost_usd) || 0), 0);
      })(),
      activeSessions: d.sessionList.length,
      backlog,
    };
  }, [d.statsQ.data, d.llmQ.data, d.usageQ.data, d.approvalsQ.data, d.runsQ.data, d.sessionList]);

  const refreshAll = () => {
    void d.runsQ.refetch();
    void d.statsQ.refetch();
    void d.llmQ.refetch();
    void d.usageQ.refetch();
    void d.sessionsQ.refetch();
    void d.costsQ.refetch();
    void d.approvalsQ.refetch();
    void d.errorsQ.refetch();
    void d.anomaliesQ.refetch();
  };

  return (
    <div className="w-full space-y-4 p-4">
      <PageHead title={t("office.title", "관측 센터")} desc={t("office.desc", "run·세션·비용·오류 실시간 관측")}>
        <Button variant="ghost" className="px-2 py-0.5 text-xs" onClick={refreshAll}>
          {t("common.reload", "새로고침")}
        </Button>
      </PageHead>

      {/* 상단 KPI 행 — 클릭 시 해당 섹션으로 스크롤 */}
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 xl:grid-cols-7">
        <KpiCard
          label={t("office.kpi.runsToday", "오늘 실행")}
          value={kpis.runsToday != null ? String(kpis.runsToday) : "-"}
          onClick={() => scrollTo("office-live")}
        />
        <KpiCard
          label={t("office.kpi.successRate", "성공률")}
          value={pct1(kpis.successRate)}
          tone={kpis.successRate != null && kpis.successRate >= 95 ? "ok" : kpis.successRate != null && kpis.successRate < 80 ? "err" : "muted"}
          onClick={() => scrollTo("office-live")}
        />
        <KpiCard
          label={t("office.kpi.errorRate", "오류율")}
          value={pct1(kpis.errorRate)}
          tone={kpis.errorRate != null && kpis.errorRate > 10 ? "err" : kpis.errorRate != null && kpis.errorRate > 0 ? "warn" : "ok"}
          onClick={() => scrollTo("office-errors")}
        />
        <KpiCard
          label={t("office.kpi.p95", "p95 지연")}
          value={fmtSecs(kpis.p95)}
          tone={kpis.p95 != null && kpis.p95 > 600 ? "warn" : "muted"}
          onClick={() => scrollTo("office-live")}
        />
        <KpiCard
          label={t("office.kpi.costMtd", "월 누적 비용")}
          value={kpis.costMtd != null ? usd(kpis.costMtd) : "-"}
          onClick={() => scrollTo("office-cost")}
        />
        <KpiCard
          label={t("office.kpi.activeSessions", "활성 세션")}
          value={String(kpis.activeSessions)}
          tone={kpis.activeSessions > 0 ? "accent" : "muted"}
          onClick={() => scrollTo("office-sessions")}
        />
        <KpiCard
          label={t("office.kpi.approvalBacklog", "승인 대기")}
          value={kpis.backlog != null ? String(kpis.backlog) : "-"}
          tone={kpis.backlog != null && kpis.backlog > 0 ? "warn" : "ok"}
          onClick={() => scrollTo("office-attention")}
        />
      </div>

      {/* 주의 필요 — 승인 대기 / 최근 실패 / 이상 징후 */}
      <div id="office-attention" className="scroll-mt-4">
        <AttentionStrip d={d} />
      </div>

      {/* 라이브 run 보드 */}
      <div id="office-live" className="scroll-mt-4">
        <LiveRuns d={d} />
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <div id="office-cost" className="scroll-mt-4">
          <CostSection d={d} />
        </div>
        <div id="office-errors" className="scroll-mt-4">
          <ErrorSection d={d} />
        </div>
      </div>

      {/* 활성 세션 */}
      <div id="office-sessions" className="scroll-mt-4">
        <SessionsSection d={d} />
      </div>
    </div>
  );
}
