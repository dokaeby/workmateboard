/** 인사이트 — /insights?days=: 세션/실행/비용/실패 집계 카드 3종. */
import { useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { apiGet } from "../../shared/api/client";
import { useI18n } from "../../shared/i18n";
import { PageHead, Card, Empty, Spinner, ErrorBox, Button } from "../../shared/ui";
import { usd } from "../../shared/lib/format";

interface Insights {
  days?: number;
  sessions?: {
    by_tool?: { tool?: string; sessions?: number; working_seconds?: number; decisions?: number }[];
    by_project?: { project?: string; sessions?: number; working_seconds?: number }[];
    total_sessions?: number;
    total_working_seconds?: number;
  };
  runs?: {
    total?: number;
    succeeded?: number;
    failed?: number;
    running?: number;
    success_rate?: number | null; // 0~1
    avg_duration_s?: number | null;
    by_workflow?: { workflow?: string; runs?: number; success_rate?: number | null }[];
  };
  cost?: { total_cost_usd?: number; llm_tokens_in?: number; llm_tokens_out?: number };
  top_failures?: { stage_key?: string; count?: number }[];
}

function fmtSecs(s: number | null | undefined): string {
  const n = Math.max(0, Math.round(Number(s) || 0));
  if (n < 60) return `${n}s`;
  const m = Math.floor(n / 60), r = n % 60;
  if (m < 60) return `${m}m${r ? ` ${r}s` : ""}`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}

const pct = (v: number | null | undefined): string => {
  if (v == null) return "-";
  const n = v <= 1 ? v * 100 : v;
  return `${Math.round(n)}%`;
};

function Num({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="rounded-lg bg-surface-hover px-3 py-2">
      <div className="text-lg font-bold text-ink">{children}</div>
      <div className="text-xs text-muted">{label}</div>
    </div>
  );
}

function MiniTable({ heads, rows }: { heads: string[]; rows: ReactNode[][] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-border text-left text-xs text-muted">
            {heads.map((h) => <th key={h} className="py-1.5 pr-3">{h}</th>)}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i} className="border-b border-border last:border-0">
              {r.map((c, j) => <td key={j} className="py-1.5 pr-3">{c}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const DAYS = ["7", "30", "90"];

export default function InsightsPage() {
  const { t } = useI18n();
  const [days, setDays] = useState("30");
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["insights", days],
    queryFn: () => apiGet<Insights>(`/insights?days=${encodeURIComponent(days)}`),
    refetchInterval: 60000,
  });

  const s = data?.sessions || {};
  const r = data?.runs || {};
  const cst = data?.cost || {};
  const toolRows = (s.by_tool || []).map((x) => [
    <span key="t" className="font-mono text-xs">{x.tool || "-"}</span>,
    x.sessions ?? 0,
    Math.round((x.working_seconds || 0) / 60),
    x.decisions ?? 0,
  ]);
  const projRows = (s.by_project || []).map((x) => [
    <span key="p" className="font-mono text-xs">{x.project || "-"}</span>,
    x.sessions ?? 0,
    Math.round((x.working_seconds || 0) / 60),
  ]);
  const wfRows = (r.by_workflow || []).map((x) => [
    <span key="w" className="font-mono text-xs">{x.workflow || "-"}</span>,
    x.runs ?? 0,
    pct(x.success_rate),
  ]);
  const fails = data?.top_failures || [];

  return (
    <div className="mx-auto max-w-5xl p-4">
      <PageHead title={t("ins.title", "인사이트")} desc={t("ins.desc", "세션 · 실행 · 비용 통계")}>
        <select
          value={days}
          onChange={(e) => setDays(e.target.value)}
          aria-label={t("ins.daysAria", "통계 기간")}
          className="rounded-md border border-border bg-surface px-2 py-1.5 text-sm"
        >
          {DAYS.map((d) => (
            <option key={d} value={d}>{t(`common.last${d}`, `최근 ${d}일`)}</option>
          ))}
        </select>
        <Button variant="secondary" onClick={() => refetch()} className="px-3 py-1 text-xs">
          {t("common.refresh", "새로고침")}
        </Button>
      </PageHead>

      {isLoading && <Spinner />}
      {error && <ErrorBox message={error instanceof Error ? error.message : String(error)} onRetry={() => refetch()} />}
      {data && (
        <div className="grid gap-3 lg:grid-cols-3">
          <Card title={t("ins.sessions", "세션")}>
            <div className="mb-2 grid grid-cols-2 gap-2">
              <Num label={t("ins.totalSessions", "총 세션")}>{s.total_sessions ?? 0}</Num>
              <Num label={t("ins.totalWork", "총 작업 시간")}>{fmtSecs(s.total_working_seconds)}</Num>
            </div>
            <div className="mb-1 text-xs font-bold text-muted">{t("ins.byTool", "툴별")}</div>
            {toolRows.length ? (
              <MiniTable heads={[t("ins.tool", "툴"), t("ins.sessions", "세션"), t("ins.workMin", "작업(분)"), t("ins.decisions", "결정")]} rows={toolRows} />
            ) : <Empty>{t("ins.noData", "데이터 없음")}</Empty>}
            <div className="mb-1 mt-3 text-xs font-bold text-muted">{t("ins.byProject", "프로젝트별")}</div>
            {projRows.length ? (
              <MiniTable heads={[t("ins.project", "프로젝트"), t("ins.sessions", "세션"), t("ins.workMin", "작업(분)")]} rows={projRows} />
            ) : <Empty>{t("ins.noData", "데이터 없음")}</Empty>}
          </Card>

          <Card title={t("ins.runs", "실행")}>
            <div className="mb-2 grid grid-cols-2 gap-2">
              <Num label={t("ins.totalRuns", "총 실행")}>{r.total ?? 0}</Num>
              <Num label={t("ins.succeeded", "성공")}>{r.succeeded ?? 0}</Num>
              <Num label={t("ins.failed", "실패")}>{r.failed ?? 0}</Num>
              <Num label={t("ins.running", "진행 중")}>{r.running ?? 0}</Num>
              <Num label={t("ins.successRate", "성공률")}>{pct(r.success_rate)}</Num>
              <Num label={t("ins.avgDuration", "평균 소요")}>{fmtSecs(r.avg_duration_s)}</Num>
            </div>
            <div className="mb-1 text-xs font-bold text-muted">{t("ins.byWorkflow", "워크플로별")}</div>
            {wfRows.length ? (
              <MiniTable heads={[t("ins.workflow", "워크플로"), t("ins.runs", "실행"), t("ins.successRate", "성공률")]} rows={wfRows} />
            ) : <Empty>{t("ins.noData", "데이터 없음")}</Empty>}
          </Card>

          <Card title={t("ins.cost", "비용")}>
            <div className="mb-2 grid grid-cols-3 gap-2">
              <Num label={t("ins.totalCost", "총 비용")}>{usd(cst.total_cost_usd)}</Num>
              <Num label={t("ins.tokensIn", "입력 토큰")}>{Number(cst.llm_tokens_in || 0).toLocaleString()}</Num>
              <Num label={t("ins.tokensOut", "출력 토큰")}>{Number(cst.llm_tokens_out || 0).toLocaleString()}</Num>
            </div>
            {fails.length > 0 && (
              <>
                <div className="mb-1 text-xs font-bold text-muted">{t("ins.topFails", "실패 상위 단계")}</div>
                {fails.slice(0, 10).map((f, i) => (
                  <div key={i} className="flex items-center justify-between py-0.5 text-xs">
                    <span className="font-mono">{f.stage_key || "-"}</span>
                    <span className="font-medium text-err">{f.count ?? 0}{t("ins.times", "회")}</span>
                  </div>
                ))}
              </>
            )}
          </Card>
        </div>
      )}
    </div>
  );
}
