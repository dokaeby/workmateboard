/** 성과 — spec 014 US3: 상단 실시간 요약(KPI + 일별 실행/오류 듀얼 차트 +
 * 모델별 토큰 비중) + 기존 /perf/compare 워크플로 비교. */
import { useId, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { apiGet } from "../../shared/api/client";
import { useI18n } from "../../shared/i18n";
import "./i18n";
import { PageHead, Card, Empty, Spinner, ErrorBox, Button } from "../../shared/ui";
import { usd } from "../../shared/lib/format";
import { ShareBars, type BarRow } from "../office/charts";


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

// ---------- spec 014 US3 — 기간 요약 (실데이터만) ----------

interface LlmDailyPoint { date?: string; runs?: number; tokens?: number; errors?: number }
interface LlmModelShare { model?: string; calls?: number; tokens?: number; share_pct?: number }
interface LlmStageStat {
  stage_key?: string; calls?: number; failures?: number;
  failure_rate_pct?: number | null; tokens?: number;
  latency_s?: { p50?: number | null; p95?: number | null; avg?: number | null };
}
interface LlmPerfStats {
  days?: number;
  runs_total?: number;
  terminal_runs?: number;
  failed_runs?: number;
  error_rate_pct?: number | null;
  latency_s?: { p50?: number | null; p95?: number | null; p99?: number | null; avg?: number | null; max?: number | null; samples?: number };
  stage_latency_s?: { p50?: number | null; p95?: number | null; avg?: number | null };
  daily?: LlmDailyPoint[];
  models?: LlmModelShare[];
  by_stage?: LlmStageStat[];
  by_workflow?: { workflow?: string; runs?: number; failed?: number; avg_duration_s?: number | null; total_tokens?: number }[];
}
interface AgentSessionToolStat { sessions?: number; working_seconds?: number; decisions?: number; ended?: number; last_active?: string }
interface AgentSessionStats { tools?: Record<string, AgentSessionToolStat>; hourly?: { hour?: number; working_seconds?: number }[] }

function fmtSecs1(v: number | null | undefined): string {
  if (v == null || !isFinite(v)) return "-";
  if (v < 60) return `${v.toFixed(v < 10 ? 1 : 0)}s`;
  return `${(v / 60).toFixed(1)}m`;
}

/** 일별 실행(영역) + 오류(선) 듀얼 라인 — 같은 날짜 축, 각자 최대값으로 정규화. */
function DualTrend({ runs, errors, height = 150 }: {
  runs: { label: string; value: number }[];
  errors: { label: string; value: number }[];
  height?: number;
}) {
  const gid = `perf-dual-${useId().replace(/[^a-zA-Z0-9]/g, "")}`;
  const W = 640;
  const H = height;
  const pad = 6;
  const n = Math.max(runs.length, errors.length);
  if (!n) return null;
  const step = n > 1 ? (W - pad * 2) / (n - 1) : W - pad * 2;
  const mk = (pts: { label: string; value: number }[], color: string, fill: boolean) => {
    if (!pts.length) return null;
    const max = Math.max(1e-9, ...pts.map((p) => p.value));
    const coords = pts.map((p, i) => ({
      x: pad + i * step,
      y: H - pad - (p.value / max) * (H - pad * 2),
      p,
    }));
    const line = coords.map((c, i) => `${i === 0 ? "M" : "L"}${c.x.toFixed(1)},${c.y.toFixed(1)}`).join(" ");
    const area = fill ? `${line} L${coords[coords.length - 1].x.toFixed(1)},${H} L${coords[0].x.toFixed(1)},${H} Z` : "";
    return (
      <g key={color}>
        {fill && (
          <>
            <defs>
              <linearGradient id={`${gid}-${color}`} x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={color} stopOpacity="0.3" />
                <stop offset="100%" stopColor={color} stopOpacity="0" />
              </linearGradient>
            </defs>
            <path d={area} fill={`url(#${gid}-${color})`} />
          </>
        )}
        <path d={line} fill="none" stroke={color} strokeWidth="1.5" vectorEffect="non-scaling-stroke" />
        {coords.map((c, i) => (
          <circle key={i} cx={c.x} cy={c.y} r="2" fill={color}>
            <title>{`${c.p.label} · ${c.p.value}`}</title>
          </circle>
        ))}
      </g>
    );
  };
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img" aria-label="daily runs/errors" preserveAspectRatio="none">
      {mk(runs, "var(--color-accent, #00e5ff)", true)}
      {mk(errors, "var(--color-err, #ff475d)", false)}
    </svg>
  );
}

function PerfSummary() {
  const { t } = useI18n();
  const days = 30;
  const llmQ = useQuery({
    queryKey: ["perf", "llm-stats", days],
    queryFn: () => apiGet<LlmPerfStats>(`/llm-monitor/stats?days=${days}`),
    refetchInterval: 60000,
    retry: false,
  });
  const sessQ = useQuery({
    queryKey: ["perf", "agent-session-stats"],
    queryFn: () => apiGet<AgentSessionStats>("/agent-sessions/stats"),
    refetchInterval: 60000,
    retry: false,
  });

  const d = llmQ.data;
  const sessionCount = useMemo(() => {
    const tools = sessQ.data?.tools;
    if (!tools || typeof tools !== "object") return null;
    return Object.values(tools).reduce((a, x) => a + (Number(x?.sessions) || 0), 0);
  }, [sessQ.data]);

  const runsTotal = Number(d?.runs_total) || 0;
  const terminal = Number(d?.terminal_runs) || 0;
  const failed = Number(d?.failed_runs) || 0;
  const successRate = terminal > 0 ? Math.round(((terminal - failed) / terminal) * 1000) / 10 : null;
  const avgRunS = d?.latency_s?.avg ?? null;
  const avgPerDay = runsTotal > 0 ? Math.round((runsTotal / days) * 10) / 10 : null;

  const daily = useMemo(() => {
    const pts = Array.isArray(d?.daily) ? d.daily : [];
    return pts.map((p) => ({
      label: String(p.date || "").slice(5),
      runs: Number(p.runs) || 0,
      errors: Number(p.errors) || 0,
    }));
  }, [d]);

  const shareRows = useMemo<BarRow[]>(() => {
    return (Array.isArray(d?.models) ? d.models : [])
      .slice(0, 8)
      .map((m) => ({
        label: String(m.model || "-"),
        value: Number(m.share_pct) || 0,
        display: `${Number(m.share_pct ?? 0).toFixed(1)}% · ${(Number(m.tokens) || 0).toLocaleString()}`,
        hint: `${m.calls ?? 0} calls`,
        tone: "accent" as const,
      }));
  }, [d]);

  const kpis: { label: string; value: string; sub?: string }[] = [
    { label: t("perf.kpi.sessions", "에이전트 세션"), value: sessionCount != null ? sessionCount.toLocaleString() : "-", sub: t("perf.kpi.sessionsSub", "agent_sessions 전체") },
    { label: t("perf.kpi.runs", "실행"), value: runsTotal ? runsTotal.toLocaleString() : "-", sub: t("perf.kpi.runsSub", `최근 ${days}일`) },
    { label: t("perf.kpi.success", "성공률"), value: successRate != null ? `${successRate}%` : "-", sub: t("perf.kpi.successSub", "종료된 run 기준") },
    { label: t("perf.kpi.avgRun", "평균 실행 시간"), value: avgRunS != null ? fmtSecs1(avgRunS) : "-", sub: t("perf.kpi.avgRunSub", "run 생성→종료") },
    { label: t("perf.kpi.perDay", "일평균 실행"), value: avgPerDay != null ? String(avgPerDay) : "-", sub: t("perf.kpi.perDaySub", "RPS는 미수집 — 일 단위 집계") },
  ];

  return (
    <>
      <div className="mb-3 grid grid-cols-2 gap-2 sm:grid-cols-3 xl:grid-cols-5">
        {kpis.map((k) => (
          <div key={k.label} className="rounded-lg border border-border bg-surface-hover px-2.5 py-1.5">
            <div className="text-[10px] text-muted">{k.label}</div>
            <div className="text-base font-semibold tabular-nums text-ink">{k.value}</div>
            {k.sub && <div className="truncate text-[10px] text-muted">{k.sub}</div>}
          </div>
        ))}
      </div>

      <div className="mb-3 grid gap-3 lg:grid-cols-5">
        <Card
          title={t("perf.trend.title", "일별 실행 / 오류")}
          className="lg:col-span-3"
          actions={<span className="text-[10px] text-muted">{t("perf.trend.note", "최근 30일 · run 생성일 기준")}</span>}
        >
          {llmQ.isLoading ? <Spinner /> : (
            <>
              {!daily.some((p) => p.runs > 0 || p.errors > 0) ? (
                <Empty>{t("perf.trend.empty", "기간 내 실행 데이터가 없습니다")}</Empty>
              ) : (
                <>
                  <div className="mb-1 flex items-center gap-3 text-[10px] text-muted">
                    <span className="flex items-center gap-1"><span className="inline-block h-1.5 w-3 rounded bg-accent" />{t("perf.trend.runs", "실행")}</span>
                    <span className="flex items-center gap-1"><span className="inline-block h-1.5 w-3 rounded bg-err" />{t("perf.trend.errors", "오류")}</span>
                  </div>
                  <DualTrend
                    runs={daily.map((p) => ({ label: p.label, value: p.runs }))}
                    errors={daily.map((p) => ({ label: p.label, value: p.errors }))}
                  />
                  <div className="mt-1 flex justify-between text-[10px] text-muted">
                    <span>{daily[0]?.label}</span>
                    <span>{daily[daily.length - 1]?.label}</span>
                  </div>
                </>
              )}
            </>
          )}
          {llmQ.error && (
            <div className="mt-2 text-xs text-muted">
              {t("perf.trend.unavailable", "요약 데이터를 불러오지 못했습니다")}
            </div>
          )}
        </Card>

        <Card
          title={t("perf.share.title", "모델별 토큰 비중")}
          className="lg:col-span-2"
          actions={<span className="text-[10px] text-muted">{t("perf.share.note", "작업 유형 분해 데이터 없음 — 모델 기준")}</span>}
        >
          {llmQ.isLoading ? <Spinner /> : (
            <ShareBars rows={shareRows} empty={t("perf.share.empty", "모델 사용 데이터가 없습니다")} />
          )}
        </Card>
      </div>
    </>
  );
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

      <PerfSummary />

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
