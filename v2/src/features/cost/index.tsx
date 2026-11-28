/** 비용 — /usage/summary?days= + /usage/llm?days=7: 월 비용/모델별 비용/일별 시계열/LLM 토큰. */
import { useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { apiGet, apiHeaders } from "../../shared/api/client";
import type { CostSummary, CostEventsResponse, CostEvent } from "../../shared/api/types";
import { useI18n } from "../../shared/i18n";
import { PageHead, Card, Empty, Spinner, ErrorBox, Button, Badge } from "../../shared/ui";
import { usd, usdFromMicro, fmtTs } from "../../shared/lib/format";
import { BudgetSection, BudgetBanner } from "./budgets";
import "./i18n";


interface UsageSummary {
  total_cost_usd?: number;
  total_tokens_in?: number;
  total_tokens_out?: number;
  by_model?: { model?: string; cost_usd?: number; tokens?: number }[];
  by_day?: { date?: string; cost_usd?: number; tokens?: number }[];
  top_sessions?: { tool?: string; session_key?: string; cost_usd?: number }[];
  budget_usd?: number | null;
  budget_exceeded?: boolean;
}
interface LlmUsage {
  days?: number;
  models?: Record<string, { calls?: number; tokens_in?: number; tokens_out?: number }>;
  daily?: { date?: string; tokens_in?: number; tokens_out?: number }[];
}

// spec 007 US2 — /observability/costs + /observability/usage
interface ObsCostItem {
  key?: string;
  cost_usd?: number | null;
  tokens_in?: number;
  tokens_out?: number;
  calls?: number;
  runs?: number;
}
interface ObsCosts {
  group_by?: string;
  days?: number;
  items?: ObsCostItem[];
}
interface ObsUsage {
  days?: number;
  models?: Record<string, { calls?: number; tokens_in?: number; tokens_out?: number }>;
  daily?: { date?: string; tokens_in?: number; tokens_out?: number; cost_usd?: number | null }[];
  sessions?: { total?: number; by_tool?: Record<string, number> };
}

const COST_STATUS_TONE: Record<string, "ok" | "warn" | "accent" | "muted"> = {
  reported: "ok",
  estimated: "accent",
  unpriced: "warn",
};
const COST_STATUS_LABEL: Record<string, string> = {
  reported: "cost.stReported",
  estimated: "cost.stEstimated",
  unpriced: "cost.stUnpriced",
};
const COST_STATUS_FALLBACK: Record<string, string> = {
  reported: "실측",
  estimated: "추정",
  unpriced: "미산정",
};

/** cost_status 배지 — 미등록 상태는 원문 표시. */
function costStatusBadge(status: string | undefined, t: (k: string, f?: string) => string) {
  const s = String(status || "");
  return (
    <Badge tone={COST_STATUS_TONE[s] || "muted"}>
      {t(COST_STATUS_LABEL[s] || "", COST_STATUS_FALLBACK[s] || s || "-")}
    </Badge>
  );
}

/** 2차 비용 원장 카드 — /management/costs/summary + /management/cost-events.
 *  금액은 microusd 정수로 받고 표시만 USD 문자열로 변환한다. */
function LedgerSection() {
  const { t } = useI18n();
  const summary = useQuery({
    queryKey: ["mgmt-cost-summary"],
    queryFn: () => {
      // "이번 달" = UTC 월 초부터 — 예산 window(UTC 월 경계)와 동일 기준.
      const d = new Date();
      d.setUTCDate(1);
      d.setUTCHours(0, 0, 0, 0);
      return apiGet<CostSummary>(
        `/management/costs/summary?scope=global&from=${encodeURIComponent(d.toISOString())}`,
      );
    },
    refetchInterval: 60000,
  });
  const events = useQuery({
    queryKey: ["mgmt-cost-events"],
    queryFn: () => apiGet<CostEventsResponse>("/management/cost-events?limit=20"),
    refetchInterval: 60000,
  });

  const s = summary.data;
  const byAgent = s?.by_agent || [];
  const evts = events.data?.items || [];

  return (
    <>
      <Card title={t("cost.ledgerTitle", "비용 원장 (관리)")} className="mt-3">
        {summary.isLoading && <Spinner />}
        {summary.error && (
          <ErrorBox message={summary.error instanceof Error ? summary.error.message : String(summary.error)} onRetry={() => summary.refetch()} />
        )}
        {s && (
          <>
            <div className="mb-3 flex flex-wrap items-center gap-2">
              <Num label={t("cost.monthTotal", "이번 달 총 비용")}>
                {s.total_microusd != null ? usdFromMicro(s.total_microusd) : "-"}
              </Num>
              {(s.unpriced_count ?? 0) > 0 && (
                <Badge tone="warn">
                  {t("cost.unpricedCount", "미산정 {n}건").replace("{n}", String(s.unpriced_count))}
                </Badge>
              )}
            </div>
            {byAgent.length > 0 && (
              <>
                <div className="mb-1 text-xs font-bold text-muted">{t("cost.byAgent", "에이전트별 비용")}</div>
                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="border-b border-border text-left text-xs text-muted">
                        <th className="py-1.5 pr-3">{t("cost.agent", "에이전트")}</th>
                        <th className="py-1.5 pr-3">{t("cost.cost", "비용")}</th>
                        <th className="py-1.5">{t("cost.unpriced", "미산정")}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {byAgent.map((a, i) => (
                        <tr key={a.agent_id || i} className="border-b border-border last:border-0">
                          <td className="py-1.5 pr-3 font-mono text-xs">{a.agent_id || "-"}</td>
                          <td className="py-1.5 pr-3">
                            {a.total_microusd != null ? usdFromMicro(a.total_microusd) : "-"}
                          </td>
                          <td className="py-1.5">
                            {(a.unpriced_count ?? 0) > 0 ? (
                              <Badge tone="warn">{String(a.unpriced_count)}</Badge>
                            ) : (
                              <span className="text-muted">0</span>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </>
            )}
          </>
        )}
      </Card>

      <Card title={t("cost.eventsTitle", "최근 비용 이벤트")} className="mt-3">
        {events.isLoading && <Spinner />}
        {events.error && (
          <ErrorBox message={events.error instanceof Error ? events.error.message : String(events.error)} onRetry={() => events.refetch()} />
        )}
        {events.data && !evts.length && <Empty>{t("cost.noEvents", "비용 이벤트가 없습니다")}</Empty>}
        {evts.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border text-left text-xs text-muted">
                  <th className="py-1.5 pr-3">{t("cost.when", "시각")}</th>
                  <th className="py-1.5 pr-3">{t("cost.agent", "에이전트")}</th>
                  <th className="py-1.5 pr-3">{t("cost.model", "모델")}</th>
                  <th className="py-1.5 pr-3">{t("cost.status", "상태")}</th>
                  <th className="py-1.5">{t("cost.cost", "비용")}</th>
                </tr>
              </thead>
              <tbody>
                {evts.map((ev: CostEvent) => (
                  <tr key={ev.id} className="border-b border-border last:border-0">
                    <td className="py-1.5 pr-3 text-xs text-muted">{fmtTs(ev.occurred_at || ev.created_at)}</td>
                    <td className="py-1.5 pr-3 font-mono text-xs">{ev.agent_id || "-"}</td>
                    <td className="py-1.5 pr-3 font-mono text-xs">{ev.model || "-"}</td>
                    <td className="py-1.5 pr-3">{costStatusBadge(ev.cost_status, t)}</td>
                    <td className="py-1.5">
                      {ev.cost_status === "unpriced" || ev.cost_microusd == null
                        ? <span className="text-muted">{t("cost.unpricedShort", "미산정")}</span>
                        : usdFromMicro(ev.cost_microusd)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </>
  );
}

const DAYS = ["7", "30", "90"];
const GROUP_BYS = ["model", "project", "workflow", "agent", "day"] as const;
type GroupBy = (typeof GROUP_BYS)[number];
const GROUP_LABEL: Record<GroupBy, string> = {
  model: "cost.grpModel",
  project: "cost.grpProject",
  workflow: "cost.grpWorkflow",
  agent: "cost.grpAgent",
  day: "cost.grpDay",
};

/** SVG 스파크라인 — 일별 비용 시계열 (차트 라이브러리 없이 polyline). */
function Sparkline({ values, height = 48 }: { values: number[]; height?: number }) {
  if (values.length < 2) return null;
  const w = 600;
  const max = Math.max(1e-9, ...values);
  const step = w / (values.length - 1);
  const pts = values
    .map((v, i) => `${(i * step).toFixed(1)},${(height - 4 - (v / max) * (height - 8)).toFixed(1)}`)
    .join(" ");
  return (
    <svg viewBox={`0 0 ${w} ${height}`} className="h-12 w-full" preserveAspectRatio="none" role="img">
      <polyline points={pts} fill="none" stroke="var(--color-accent)" strokeWidth="2" vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

/** spec 007 US2 — /observability/costs 그룹 집계 + /observability/usage 일별 비용·세션. */
function ObsCostSection() {
  const { t } = useI18n();
  const [days, setDays] = useState("30");
  const [groupBy, setGroupBy] = useState<GroupBy>("model");

  const costs = useQuery({
    queryKey: ["obs-costs", days, groupBy],
    queryFn: () => apiGet<ObsCosts>(`/observability/costs?days=${encodeURIComponent(days)}&group_by=${groupBy}`),
    refetchInterval: 60000,
  });
  const usage = useQuery({
    queryKey: ["obs-usage", days],
    queryFn: () => apiGet<ObsUsage>(`/observability/usage?days=${encodeURIComponent(days)}`),
    refetchInterval: 60000,
  });

  const items = (costs.data?.items || []).toSorted((a, b) => (Number(b.cost_usd) || 0) - (Number(a.cost_usd) || 0)).slice(0, 10);
  const maxCost = Math.max(1e-9, ...items.map((x) => Number(x.cost_usd) || 0));
  const daily = usage.data?.daily || [];
  const dailyCosts = daily.map((d) => Number(d.cost_usd) || 0);
  const sessions = usage.data?.sessions;
  const byTool = Object.entries(sessions?.by_tool || {}).sort((a, b) => b[1] - a[1]).slice(0, 8);

  return (
    <Card title={t("cost.obsTitle", "비용 집계 (관측)")} className="mt-3">
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <select
          value={groupBy}
          onChange={(e) => setGroupBy(e.target.value as GroupBy)}
          aria-label={t("cost.groupBy", "그룹 기준")}
          className="rounded-md border border-border bg-surface px-2 py-1.5 text-sm"
        >
          {GROUP_BYS.map((g) => <option key={g} value={g}>{t(GROUP_LABEL[g], g)}</option>)}
        </select>
        <select
          value={days}
          onChange={(e) => setDays(e.target.value)}
          aria-label={t("cost.daysAria", "비용 기간")}
          className="rounded-md border border-border bg-surface px-2 py-1.5 text-sm"
        >
          {["7", "30", "90"].map((x) => <option key={x} value={x}>{t(`common.last${x}`, `최근 ${x}일`)}</option>)}
        </select>
        <Button variant="ghost" className="px-2 py-0.5 text-xs" onClick={() => { costs.refetch(); usage.refetch(); }}>
          {t("common.refresh", "새로고침")}
        </Button>
      </div>

      {costs.isLoading && <Spinner />}
      {costs.error && (
        <ErrorBox message={costs.error instanceof Error ? costs.error.message : String(costs.error)} onRetry={() => costs.refetch()} />
      )}
      {costs.data && !items.length && <Empty>{t("cost.noItems", "해당 기간 비용 데이터가 없습니다")}</Empty>}
      {items.length > 0 && (
        <>
          <div className="mb-1 text-xs font-bold text-muted">{t("cost.topItems", "상위 항목")}</div>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border text-left text-xs text-muted">
                  <th className="py-1.5 pr-3">{t("cost.key", "키")}</th>
                  <th className="py-1.5 pr-3">{t("cost.cost", "비용")}</th>
                  <th className="py-1.5 pr-3">{t("cost.tokens", "토큰")}</th>
                  <th className="py-1.5 pr-3">{t("cost.calls", "호출")}</th>
                  <th className="py-1.5">{t("cost.runs", "실행")}</th>
                </tr>
              </thead>
              <tbody>
                {items.map((it, i) => {
                  const c = Number(it.cost_usd) || 0;
                  return (
                    <tr key={it.key || i} className="border-b border-border last:border-0">
                      <td className="max-w-48 truncate py-1.5 pr-3 font-mono text-xs" title={it.key || "-"}>{it.key || "-"}</td>
                      <td className="py-1.5 pr-3">
                        <div className="flex items-center gap-2">
                          <span className="h-1.5 w-16 overflow-hidden rounded bg-surface-hover">
                            <span className="block h-full bg-accent/80" style={{ width: `${Math.max(2, Math.round((c / maxCost) * 100))}%` }} />
                          </span>
                          {it.cost_usd != null ? usd(it.cost_usd) : "-"}
                        </div>
                      </td>
                      <td className="py-1.5 pr-3">{((Number(it.tokens_in) || 0) + (Number(it.tokens_out) || 0)).toLocaleString()}</td>
                      <td className="py-1.5 pr-3">{it.calls ?? 0}</td>
                      <td className="py-1.5">{it.runs ?? 0}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </>
      )}

      {dailyCosts.some((v) => v > 0) && (
        <div className="mt-4">
          <div className="mb-1 flex items-center justify-between text-xs font-bold text-muted">
            <span>{t("cost.dailyCost", "일별 비용 (스파크라인)")}</span>
            <span className="font-normal">{dailyCosts.reduce((a, b) => a + b, 0) > 0 ? usd(dailyCosts.reduce((a, b) => a + b, 0)) : ""}</span>
          </div>
          <Sparkline values={dailyCosts} />
          {daily.length > 0 && (
            <div className="mt-0.5 flex justify-between text-[10px] text-muted">
              <span>{daily[0]?.date}</span><span>{daily[daily.length - 1]?.date}</span>
            </div>
          )}
        </div>
      )}

      {sessions && (
        <div className="mt-4 border-t border-border pt-3">
          <div className="mb-1 text-xs font-bold text-muted">{t("cost.usageTitle", "사용량 요약")}</div>
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <Badge tone="accent">{t("cost.sessions", "세션")} {sessions.total ?? 0}</Badge>
            {byTool.map(([tool, n]) => (
              <Badge key={tool} tone="muted">{tool} {n}</Badge>
            ))}
          </div>
        </div>
      )}
    </Card>
  );
}

function Num({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="rounded-lg bg-surface-hover px-3 py-2">
      <div className="text-lg font-bold text-ink">{children}</div>
      <div className="text-xs text-muted">{label}</div>
    </div>
  );
}

/** div 기반 일별 바 차트 — 토큰/비용 시계열. */
function DailyBars<T extends { date?: string }>({ rows, valueOf, max }: {
  rows: T[]; valueOf: (r: T) => number; max: number;
}) {
  return (
    <div className="flex h-24 items-end gap-1">
      {rows.map((r, i) => {
        const v = valueOf(r);
        const pct = max > 0 ? Math.max(2, Math.round((v / max) * 100)) : 2;
        return (
          <div key={i} className="group flex min-w-0 flex-1 flex-col items-center justify-end gap-0.5" title={`${r.date ?? ""} · ${v.toLocaleString()}`}>
            <div className="w-full rounded-t bg-accent/80 transition-colors group-hover:bg-accent" style={{ height: `${pct}%` }} />
          </div>
        );
      })}
    </div>
  );
}

export default function CostPage() {
  const { t } = useI18n();
  const [days, setDays] = useState("30");
  const [expKind, setExpKind] = useState("runs");
  const [expFormat, setExpFormat] = useState("csv");
  const [expDays, setExpDays] = useState("30");
  const [exporting, setExporting] = useState(false);

  const summary = useQuery({
    queryKey: ["usage-summary", days],
    queryFn: () => apiGet<UsageSummary>(`/usage/summary?days=${encodeURIComponent(days)}`),
    refetchInterval: 60000,
  });
  const llm = useQuery({
    queryKey: ["usage-llm"],
    queryFn: () => apiGet<LlmUsage>("/usage/llm?days=7"),
    refetchInterval: 60000,
  });

  const d = summary.data;
  const budgetTxt = d?.budget_usd != null ? ` · ${t("cost.budget", "예산")} ${usd(d.budget_usd)}` : "";
  const bm = d?.by_model || [];
  const bd = d?.by_day || [];
  const ts = (d?.top_sessions || []).slice(0, 8);
  const maxCost = Math.max(1e-9, ...bd.map((x) => Number(x.cost_usd) || 0));

  const llmModels = Object.entries(llm.data?.models || {});
  const llmDaily = llm.data?.daily || [];
  const maxTok = Math.max(1, ...llmDaily.map((x) => (x.tokens_in || 0) + (x.tokens_out || 0)));

  const doExport = async () => {
    setExporting(true);
    try {
      const q = `kind=${encodeURIComponent(expKind)}&format=${encodeURIComponent(expFormat)}&days=${encodeURIComponent(expDays)}`;
      const r = await fetch(`/api/v1/export?${q}`, { headers: apiHeaders() });
      if (!r.ok) {
        const j = (await r.json().catch(() => ({}))) as { error?: { message?: string } };
        alert(j.error?.message || `${t("cost.exportFail", "내보내기 실패")}: ${r.status}`);
        return;
      }
      const blob = await r.blob();
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `jobmate-${expKind}-${expDays}d.${expFormat === "json" ? "json" : "csv"}`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 5000);
    } catch (e) {
      alert(e instanceof Error ? e.message : String(e));
    } finally {
      setExporting(false);
    }
  };

  return (
    <div className="mx-auto max-w-5xl p-4">
      <PageHead title={t("cost.title", "비용")} desc={t("cost.desc", "LLM/세션 비용 상세 — 예산 정책은 아래에서 관리")}>
        <select
          value={days}
          onChange={(e) => setDays(e.target.value)}
          aria-label={t("cost.daysAria", "비용 기간")}
          className="rounded-md border border-border bg-surface px-2 py-1.5 text-sm"
        >
          {DAYS.map((x) => <option key={x} value={x}>{t(`common.last${x}`, `최근 ${x}일`)}</option>)}
        </select>
        <Button variant="secondary" onClick={() => summary.refetch()} className="px-3 py-1 text-xs">
          {t("common.refresh", "새로고침")}
        </Button>
      </PageHead>

      <BudgetBanner />

      {summary.isLoading && <Spinner />}
      {summary.error && (
        <ErrorBox message={summary.error instanceof Error ? summary.error.message : String(summary.error)} onRetry={() => summary.refetch()} />
      )}

      {d && (
        <>
          {d.budget_exceeded && (
            <div className="mb-3 rounded-lg border border-err-border bg-err-soft p-3 text-sm text-err">
              <b>{t("cost.budgetExceeded", "월 예산 초과")}</b> — {t("cost.budget", "예산")} {usd(d.budget_usd)} / {t("cost.used", "사용")} {usd(d.total_cost_usd)}
            </div>
          )}
          <Card>
            <div className="mb-3 grid grid-cols-1 gap-2 sm:grid-cols-3">
              <Num label={`${t("cost.totalCost", "총 비용")} (${days}${t("common.days", "일")})${budgetTxt}`}>{usd(d.total_cost_usd)}</Num>
              <Num label={t("cost.tokensIn", "입력 토큰")}>{Number(d.total_tokens_in || 0).toLocaleString()}</Num>
              <Num label={t("cost.tokensOut", "출력 토큰")}>{Number(d.total_tokens_out || 0).toLocaleString()}</Num>
            </div>

            {bm.length > 0 && (
              <>
                <div className="mb-1 text-xs font-bold text-muted">{t("cost.byModel", "모델별 비용")}</div>
                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="border-b border-border text-left text-xs text-muted">
                        <th className="py-1.5 pr-3">{t("cost.model", "모델")}</th>
                        <th className="py-1.5 pr-3">{t("cost.cost", "비용")}</th>
                        <th className="py-1.5">{t("cost.tokens", "토큰")}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {bm.map((m, i) => (
                        <tr key={i} className="border-b border-border last:border-0">
                          <td className="py-1.5 pr-3 font-mono text-xs">{m.model || "-"}</td>
                          <td className="py-1.5 pr-3">{usd(m.cost_usd)}</td>
                          <td className="py-1.5">{Number(m.tokens || 0).toLocaleString()}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </>
            )}

            {bd.length > 0 && (
              <>
                <div className="mb-1 mt-3 text-xs font-bold text-muted">{t("cost.byDay", "일별 비용")}</div>
                <DailyBars rows={bd} max={maxCost} valueOf={(x) => Number(x.cost_usd) || 0} />
                <div className="mt-1 flex justify-between text-xs text-muted">
                  <span>{bd[0]?.date}</span><span>{bd[bd.length - 1]?.date}</span>
                </div>
              </>
            )}

            {ts.length > 0 && (
              <>
                <div className="mb-1 mt-3 text-xs font-bold text-muted">{t("cost.topSessions", "비용 상위 세션")}</div>
                {ts.map((s, i) => (
                  <div key={i} className="flex items-center gap-2 py-0.5 text-xs">
                    <span className="w-20 text-muted">{s.tool || "-"}</span>
                    <span className="min-w-0 flex-1 truncate font-mono">{s.session_key || "-"}</span>
                    <span className="flex-none">{usd(s.cost_usd)}</span>
                  </div>
                ))}
              </>
            )}
            <div className="mt-3 text-xs text-muted">
              {t("cost.budgetHint", "월 예산(monthly_budget_usd)은 설정에서 변경할 수 있습니다.")}
            </div>
          </Card>
        </>
      )}

      <ObsCostSection />

      <LedgerSection />

      <BudgetSection />

      <Card title={`${t("cost.llmTitle", "LLM 토큰 사용량")} (${llm.data?.days ?? 7}${t("common.days", "일")})`} className="mt-3">
        {llm.isLoading && <Spinner />}
        {llm.error && (
          <ErrorBox message={llm.error instanceof Error ? llm.error.message : String(llm.error)} onRetry={() => llm.refetch()} />
        )}
        {llm.data && !llmModels.length && !llmDaily.length && <Empty>{t("ins.noData", "데이터 없음")}</Empty>}
        {llmModels.length > 0 && (
          <div className="overflow-x-auto">
            <table className="mb-3 w-full text-sm">
              <thead>
                <tr className="border-b border-border text-left text-xs text-muted">
                  <th className="py-1.5 pr-3">{t("cost.model", "모델")}</th>
                  <th className="py-1.5 pr-3">{t("cost.calls", "호출")}</th>
                  <th className="py-1.5 pr-3">{t("cost.tokensIn", "입력 토큰")}</th>
                  <th className="py-1.5">{t("cost.tokensOut", "출력 토큰")}</th>
                </tr>
              </thead>
              <tbody>
                {llmModels.map(([name, m]) => (
                  <tr key={name} className="border-b border-border last:border-0">
                    <td className="py-1.5 pr-3 font-mono text-xs">{name}</td>
                    <td className="py-1.5 pr-3">{m.calls ?? 0}</td>
                    <td className="py-1.5 pr-3">{Number(m.tokens_in || 0).toLocaleString()}</td>
                    <td className="py-1.5">{Number(m.tokens_out || 0).toLocaleString()}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {llmDaily.length > 0 && (
          <>
            <div className="mb-1 text-xs font-bold text-muted">{t("cost.dailyTokens", "일별 토큰")}</div>
            <DailyBars rows={llmDaily} max={maxTok} valueOf={(x) => (x.tokens_in || 0) + (x.tokens_out || 0)} />
          </>
        )}
      </Card>

      <Card title={t("cost.export", "내보내기")} className="mt-3">
        <div className="flex flex-wrap items-center gap-2">
          <select value={expKind} onChange={(e) => setExpKind(e.target.value)} className="rounded-md border border-border bg-surface px-2 py-1.5 text-sm">
            <option value="runs">{t("cost.expRuns", "실행")}</option>
            <option value="sessions">{t("cost.expSessions", "세션")}</option>
            <option value="usage">{t("cost.expUsage", "사용량")}</option>
            <option value="reports">{t("cost.expReports", "리포트")}</option>
          </select>
          <select value={expFormat} onChange={(e) => setExpFormat(e.target.value)} className="rounded-md border border-border bg-surface px-2 py-1.5 text-sm">
            <option value="csv">CSV</option>
            <option value="json">JSON</option>
          </select>
          <select value={expDays} onChange={(e) => setExpDays(e.target.value)} className="rounded-md border border-border bg-surface px-2 py-1.5 text-sm">
            {["7", "30", "90", "365"].map((x) => <option key={x} value={x}>{t(`common.last${x}`, `최근 ${x}일`)}</option>)}
          </select>
          <Button className="px-3 py-1 text-xs" disabled={exporting} onClick={doExport}>
            {exporting ? t("cost.exporting", "내보내는 중…") : t("cost.download", "다운로드")}
          </Button>
        </div>
      </Card>
    </div>
  );
}
