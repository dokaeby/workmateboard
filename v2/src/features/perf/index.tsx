/** 성과 — /perf/compare: 워크플로 선택 + model/agent/tool 그룹별 성공률·비용 비교 + 세션 tool 통계. */
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { apiGet } from "../../shared/api/client";
import { useI18n, registerI18n } from "../../shared/i18n";
import { STR } from "./i18n";
import { PageHead, Card, Empty, Spinner, ErrorBox, Button } from "../../shared/ui";
import { usd } from "../../shared/lib/format";

registerI18n(STR);

type GroupBy = "model" | "agent" | "tool";

interface PerfItem {
  group?: string;
  run_count?: number;
  success_rate?: number | null; // 0~100
  avg_seconds?: number | null;
  total_tokens?: number;
  total_cost_usd?: number;
  failures_by_stage?: Record<string, number>;
}
interface SessionStat {
  tool?: string;
  session_count?: number;
  active?: number;
  ended?: number;
  avg_work_seconds?: number;
}
interface PerfCompare {
  workflow?: string;
  group_by?: string;
  items?: PerfItem[];
  sessions?: { items?: SessionStat[] };
}
interface WorkflowKey { key?: string }
interface CustomWorkflows { items?: { name?: string; spec?: { key?: string } }[] }

function fmtSecs(s: number | null | undefined): string {
  const n = Math.max(0, Math.round(Number(s) || 0));
  if (n < 60) return `${n}s`;
  const m = Math.floor(n / 60), r = n % 60;
  if (m < 60) return `${m}m${r ? ` ${r}s` : ""}`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}

function topStages(failures: Record<string, number> | undefined, n = 3): string {
  if (!failures) return "-";
  return Object.entries(failures)
    .sort((a, b) => (b[1] || 0) - (a[1] || 0))
    .slice(0, n)
    .map(([k, c]) => `${k}:${c}`)
    .join(" · ") || "-";
}

export default function PerfPage() {
  const { t } = useI18n();
  const [workflow, setWorkflow] = useState("");
  const [groupBy, setGroupBy] = useState<GroupBy>("model");
  const [queried, setQueried] = useState<{ workflow: string; groupBy: GroupBy } | null>(null);

  const wfQuery = useQuery({
    queryKey: ["workflows"],
    queryFn: () => apiGet<WorkflowKey[]>("/workflows"),
    staleTime: 300000,
  });
  const customQuery = useQuery({
    queryKey: ["workflows-custom"],
    queryFn: () => apiGet<CustomWorkflows>("/workflows/custom"),
    staleTime: 300000,
    retry: false,
  });

  const wfKeys: string[] = [];
  for (const w of wfQuery.data || []) if (w.key && !wfKeys.includes(w.key)) wfKeys.push(w.key);
  for (const c of customQuery.data?.items || []) {
    const n = c.name || c.spec?.key || "";
    if (n && !wfKeys.includes(n)) wfKeys.push(n);
  }

  const q = queried ?? { workflow: "", groupBy };
  const compare = useQuery({
    queryKey: ["perf-compare", q.workflow, q.groupBy],
    queryFn: () =>
      apiGet<PerfCompare>(
        `/perf/compare?workflow=${encodeURIComponent(q.workflow)}&group_by=${encodeURIComponent(q.groupBy)}`,
      ),
    enabled: queried !== null,
    refetchInterval: 60000,
  });

  const items = compare.data?.items || [];
  const sessItems = compare.data?.sessions?.items || [];
  const maxRate = Math.max(1, ...items.map((x) => Number(x.success_rate) || 0));

  return (
    <div className="mx-auto max-w-5xl p-4">
      <PageHead title={t("perf.title", "성과")} desc={t("perf.desc", "워크플로 실행을 모델/에이전트/도구별로 비교")}>
        <select
          value={workflow}
          onChange={(e) => setWorkflow(e.target.value)}
          aria-label={t("th.workflow", "워크플로")}
          className="rounded-md border border-border bg-surface px-2 py-1.5 text-sm"
        >
          <option value="">{t("perf.allWf", "전체 워크플로")}</option>
          {wfKeys.map((k) => <option key={k} value={k}>{k}</option>)}
        </select>
        <select
          value={groupBy}
          onChange={(e) => setGroupBy(e.target.value as GroupBy)}
          aria-label={t("perf.groupAria", "그룹 기준")}
          className="rounded-md border border-border bg-surface px-2 py-1.5 text-sm"
        >
          <option value="model">{t("perf.byModel", "모델별")}</option>
          <option value="agent">{t("perf.byAgent", "에이전트별")}</option>
          <option value="tool">{t("perf.byTool", "도구별")}</option>
        </select>
        <Button className="px-3 py-1 text-xs" onClick={() => setQueried({ workflow, groupBy })}>
          {t("perf.query", "조회")}
        </Button>
        {queried && (
          <Button variant="secondary" className="px-3 py-1 text-xs" onClick={() => compare.refetch()}>
            {t("common.refresh", "새로고침")}
          </Button>
        )}
      </PageHead>

      {queried === null && <Empty>{t("perf.empty", "조건을 선택하고 조회를 누르세요")}</Empty>}
      {queried !== null && compare.isLoading && <Spinner />}
      {queried !== null && compare.error && (
        <ErrorBox message={compare.error instanceof Error ? compare.error.message : String(compare.error)} onRetry={() => compare.refetch()} />
      )}
      {compare.data && !items.length && !sessItems.length && (
        <Empty>{t("perf.noData", "비교 데이터가 없습니다")}</Empty>
      )}

      {items.length > 0 && (
        <Card
          title={`${compare.data?.workflow || t("perf.allWf", "전체")} — ${t(`perf.group.${q.groupBy}`, groupBy)} ${t("perf.compareSuffix", "별 비교")}`}
        >
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border text-left text-xs text-muted">
                  <th className="py-1.5 pr-3">{t("perf.group", "그룹")}</th>
                  <th className="py-1.5 pr-3">{t("perf.runs", "실행")}</th>
                  <th className="py-1.5 pr-3">{t("perf.successRate", "성공률")}</th>
                  <th className="py-1.5 pr-3">{t("perf.avgTime", "평균 시간")}</th>
                  <th className="py-1.5 pr-3">{t("perf.tokens", "토큰")}</th>
                  <th className="py-1.5 pr-3">{t("perf.cost", "비용")}</th>
                  <th className="py-1.5">{t("perf.topFails", "실패 단계 (상위)")}</th>
                </tr>
              </thead>
              <tbody>
                {items.map((x, i) => {
                  const rate = Number(x.success_rate) || 0;
                  return (
                    <tr key={i} className="border-b border-border last:border-0">
                      <td className="py-1.5 pr-3 font-medium text-ink">{x.group ?? "-"}</td>
                      <td className="py-1.5 pr-3">{x.run_count ?? 0}</td>
                      <td className="py-1.5 pr-3">
                        <span className="mr-1 inline-block h-2 rounded bg-accent align-middle" style={{ width: `${Math.max(2, Math.round((rate / maxRate) * 60))}px` }} />
                        {x.success_rate == null ? "-" : `${rate}%`}
                      </td>
                      <td className="py-1.5 pr-3">{fmtSecs(x.avg_seconds)}</td>
                      <td className="py-1.5 pr-3">{Number(x.total_tokens ?? 0).toLocaleString()}</td>
                      <td className="py-1.5 pr-3">{usd(x.total_cost_usd)}</td>
                      <td className="max-w-48 truncate py-1.5 text-xs text-muted">{topStages(x.failures_by_stage)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </Card>
      )}

      {sessItems.length > 0 && (
        <Card title={t("perf.sessStats", "도구별 세션 통계")} className="mt-3">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border text-left text-xs text-muted">
                  <th className="py-1.5 pr-3">{t("perf.tool", "도구")}</th>
                  <th className="py-1.5 pr-3">{t("perf.sessCount", "세션 수")}</th>
                  <th className="py-1.5 pr-3">{t("perf.active", "활성")}</th>
                  <th className="py-1.5 pr-3">{t("perf.ended", "종료")}</th>
                  <th className="py-1.5">{t("perf.avgWork", "평균 작업 시간")}</th>
                </tr>
              </thead>
              <tbody>
                {sessItems.map((x, i) => (
                  <tr key={i} className="border-b border-border last:border-0">
                    <td className="py-1.5 pr-3 font-medium text-ink">{x.tool ?? "-"}</td>
                    <td className="py-1.5 pr-3">{x.session_count ?? 0}</td>
                    <td className="py-1.5 pr-3">{x.active ?? 0}</td>
                    <td className="py-1.5 pr-3">{x.ended ?? 0}</td>
                    <td className="py-1.5">{fmtSecs(x.avg_work_seconds)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}
    </div>
  );
}
