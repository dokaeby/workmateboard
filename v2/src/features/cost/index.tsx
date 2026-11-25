/** 비용 — /usage/summary?days= + /usage/llm?days=7: 월 비용/모델별 비용/일별 시계열/LLM 토큰. */
import { useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { apiGet, apiHeaders } from "../../shared/api/client";
import { useI18n, registerI18n } from "../../shared/i18n";
import { PageHead, Card, Empty, Spinner, ErrorBox, Button } from "../../shared/ui";
import { usd } from "../../shared/lib/format";
import { STR } from "./i18n";

registerI18n(STR);

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

const DAYS = ["7", "30", "90"];

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
      <PageHead title={t("cost.title", "비용")} desc={t("cost.desc", "LLM/세션 비용 상세 — 월 예산은 설정에서 관리")}>
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
