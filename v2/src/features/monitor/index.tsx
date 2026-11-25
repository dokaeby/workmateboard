/** LLM 모니터 — LangSmith/Arize Phoenix 도입기 권고 지표 대시보드.
 * p95 latency, avg tokens, stage 호출·실패율, 모델/프롬프트 버전,
 * 승인 백로그(human review), 일별 시계열, 최근 실패 run.
 * 데이터: GET /api/v1/llm-monitor/stats?days= */
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { apiGet } from "../../shared/api/client";
import { useI18n } from "../../shared/i18n";
import { PageHead, Card, Empty, Spinner, ErrorBox, Button, Badge } from "../../shared/ui";
import { fmtTs, fmtAge } from "../../shared/lib/format";
import { useAppStore } from "../../shared/store/appStore";

interface LlmStats {
  days: number;
  runs_total: number;
  by_status: Record<string, number>;
  terminal_runs: number;
  failed_runs: number;
  error_rate_pct: number | null;
  latency_s: { p50: number | null; p95: number | null; p99: number | null; avg: number | null; max: number | null; samples: number };
  stage_latency_s: { p50: number | null; p95: number | null; avg: number | null };
  tokens: { total_in: number; total_out: number; total: number; avg_per_run: number | null; runs_with_tokens: number };
  models: { model: string; calls: number; tokens: number; share_pct: number }[];
  prompt_versions: string[];
  stage_calls: number;
  stage_failures: number;
  stage_failure_rate_pct: number | null;
  approval_backlog: { waiting: number; oldest_wait_s: number | null };
  daily: { date: string; runs: number; tokens: number; errors: number }[];
  recent_failures: { id: string; workflow: string; status: string; project?: string; error: string; updated_at: string }[];
  langsmith: { enabled: boolean; project: string; sample_rate: string };
}

function fmtSecs(v: number | null | undefined): string {
  if (v == null || !isFinite(v)) return "-";
  if (v < 60) return `${Math.round(v)}s`;
  if (v < 3600) return `${(v / 60).toFixed(1)}m`;
  return `${(v / 3600).toFixed(1)}h`;
}

function Kpi({ label, value, tone = "muted" }: { label: string; value: string; tone?: "ok" | "warn" | "err" | "accent" | "muted" }) {
  return (
    <div className="rounded-lg border border-border bg-surface px-3 py-2">
      <div className="text-[11px] text-muted">{label}</div>
      <div className={`text-lg font-semibold ${tone === "err" ? "text-red-500" : tone === "warn" ? "text-amber-500" : tone === "ok" ? "text-emerald-500" : tone === "accent" ? "text-accent" : "text-ink"}`}>
        {value}
      </div>
    </div>
  );
}

export default function MonitorPage() {
  const { t } = useI18n();
  const setView = useAppStore((s) => s.setView);
  const [days, setDays] = useState(30);
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["llm-monitor", days],
    queryFn: () => apiGet<LlmStats>(`/llm-monitor/stats?days=${days}`),
    refetchInterval: 30000,
  });

  const daily = data?.daily ?? [];
  const dMax = Math.max(1, ...daily.map((x) => x.runs || 0));

  return (
    <div className="space-y-4 p-4">
      <PageHead
        title={t("monitor.title", "Agent Insights")}
        desc={t("monitor.desc", "LangSmith 트레이스 + 실행 지표 — 지연시간·토큰·모델·실패율·승인 백로그")}
      >
        <select
          value={days}
          onChange={(e) => setDays(Number(e.target.value))}
          className="rounded-md border border-border bg-surface px-2 py-1 text-xs"
        >
          {[7, 14, 30, 90].map((d) => <option key={d} value={d}>{t("monitor.days", `최근 ${d}일`)}</option>)}
        </select>
        <Button variant="ghost" className="px-2 py-0.5 text-xs" onClick={() => refetch()}>
          {t("common.reload", "새로고침")}
        </Button>
      </PageHead>

      {isLoading && <Spinner />}
      {error && <ErrorBox message={error instanceof Error ? error.message : String(error)} onRetry={() => refetch()} />}

      {data && (
        <>
          {/* LangSmith 트레이싱 상태 — 블로그 권고: trace 수집 여부를 대시보드에서 확인 */}
          <Card title={t("monitor.tracing", "LangSmith 트레이싱")}>
            {data.langsmith.enabled ? (
              <div className="flex flex-wrap items-center gap-2 text-sm">
                <Badge tone="ok">{t("monitor.tracingOn", "활성")}</Badge>
                {data.langsmith.project && <span className="text-muted">project: <b className="text-ink">{data.langsmith.project}</b></span>}
                <span className="text-muted">sample rate: <b className="text-ink">{data.langsmith.sample_rate}</b></span>
                <span className="text-xs text-muted">{t("monitor.tracingHint", "상세 trace는 LangSmith 프로젝트에서 확인")}</span>
              </div>
            ) : (
              <div className="text-sm text-muted">
                {t("monitor.tracingOff", "비활성 — LANGSMITH_API_KEY를 설정하면 run/스테이지 trace가 기록됩니다.")}
              </div>
            )}
          </Card>

          {/* 핵심 지표 — 블로그 권고 대시보드 지표 */}
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
            <Kpi label={t("monitor.runs", "실행")} value={String(data.runs_total)} />
            <Kpi label={t("monitor.errorRate", "오류율")} value={data.error_rate_pct != null ? `${data.error_rate_pct}%` : "-"} tone={data.error_rate_pct && data.error_rate_pct > 10 ? "err" : data.error_rate_pct && data.error_rate_pct > 0 ? "warn" : "ok"} />
            <Kpi label={t("monitor.p95", "p95 지연")} value={fmtSecs(data.latency_s.p95)} tone={data.latency_s.p95 != null && data.latency_s.p95 > 600 ? "warn" : "muted"} />
            <Kpi label={t("monitor.avgTokens", "평균 토큰/run")} value={data.tokens.avg_per_run != null ? data.tokens.avg_per_run.toLocaleString() : "-"} />
            <Kpi label={t("monitor.stageCalls", "스테이지 호출")} value={String(data.stage_calls)} />
            <Kpi label={t("monitor.stageFail", "스테이지 실패율")} value={data.stage_failure_rate_pct != null ? `${data.stage_failure_rate_pct}%` : "-"} tone={data.stage_failure_rate_pct && data.stage_failure_rate_pct > 10 ? "err" : "muted"} />
          </div>

          <div className="grid gap-4 lg:grid-cols-2">
            {/* 지연시간 백분위 */}
            <Card title={t("monitor.latency", "지연시간 (run 종단)")}>
              <dl className="grid grid-cols-3 gap-2 text-center text-sm">
                {([["p50", data.latency_s.p50], ["p95", data.latency_s.p95], ["p99", data.latency_s.p99]] as const).map(([k, v]) => (
                  <div key={k} className="rounded-md bg-surface-hover px-2 py-3">
                    <dt className="text-[11px] text-muted">{k}</dt>
                    <dd className="text-base font-semibold text-ink">{fmtSecs(v)}</dd>
                  </div>
                ))}
              </dl>
              <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-1 text-xs text-muted">
                <dt>{t("monitor.avg", "평균")}</dt><dd className="text-ink">{fmtSecs(data.latency_s.avg)}</dd>
                <dt>{t("monitor.max", "최대")}</dt><dd className="text-ink">{fmtSecs(data.latency_s.max)}</dd>
                <dt>{t("monitor.stageLatency", "스테이지 평균/p95")}</dt>
                <dd className="text-ink">{fmtSecs(data.stage_latency_s.avg)} / {fmtSecs(data.stage_latency_s.p95)}</dd>
                <dt>{t("monitor.samples", "표본")}</dt><dd className="text-ink">{data.latency_s.samples}</dd>
              </dl>
            </Card>

            {/* 토큰 + 승인 백로그 */}
            <Card title={t("monitor.tokens", "토큰 사용량")}>
              <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm">
                <dt className="text-muted">{t("monitor.tokIn", "입력")}</dt>
                <dd className="text-right font-medium text-ink">{data.tokens.total_in.toLocaleString()}</dd>
                <dt className="text-muted">{t("monitor.tokOut", "출력")}</dt>
                <dd className="text-right font-medium text-ink">{data.tokens.total_out.toLocaleString()}</dd>
                <dt className="text-muted">{t("monitor.tokTotal", "합계")}</dt>
                <dd className="text-right font-medium text-ink">{data.tokens.total.toLocaleString()}</dd>
              </dl>
              <div className="mt-3 border-t border-border pt-3">
                <h3 className="mb-1 text-xs font-semibold uppercase text-muted">{t("monitor.backlog", "승인 백로그 (human review)")}</h3>
                <div className="flex items-center gap-2 text-sm">
                  <Badge tone={data.approval_backlog.waiting > 0 ? "warn" : "ok"}>
                    {data.approval_backlog.waiting} {t("monitor.waiting", "대기")}
                  </Badge>
                  {data.approval_backlog.oldest_wait_s != null && (
                    <span className="text-xs text-muted">{t("monitor.oldest", "최장 대기")} {fmtAge(data.approval_backlog.oldest_wait_s)}</span>
                  )}
                </div>
              </div>
            </Card>
          </div>

          <div className="grid gap-4 lg:grid-cols-2">
            {/* 모델별 사용량 */}
            <Card title={t("monitor.models", "모델별 토큰")}>
              {data.models.length === 0 ? <Empty>{t("monitor.noData", "데이터 없음")}</Empty> : (
                <table className="w-full text-xs">
                  <thead>
                    <tr className="text-left text-muted">
                      <th className="pb-1">{t("monitor.model", "모델")}</th>
                      <th className="pb-1 text-right">{t("monitor.calls", "호출")}</th>
                      <th className="pb-1 text-right">{t("monitor.tokens", "토큰")}</th>
                      <th className="pb-1 text-right">{t("monitor.share", "비중")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.models.map((m) => (
                      <tr key={m.model} className="border-t border-border">
                        <td className="py-1 font-mono">{m.model}</td>
                        <td className="py-1 text-right">{m.calls.toLocaleString()}</td>
                        <td className="py-1 text-right">{m.tokens.toLocaleString()}</td>
                        <td className="py-1 text-right text-muted">{m.share_pct}%</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </Card>

            {/* 프롬프트 버전 + 상태 분포 */}
            <Card title={t("monitor.versions", "프롬프트 버전 / 상태 분포")}>
              <h3 className="mb-1 text-xs font-semibold uppercase text-muted">{t("monitor.promptVersions", "프롬프트 버전")}</h3>
              <div className="mb-3 flex flex-wrap gap-1">
                {data.prompt_versions.length === 0
                  ? <span className="text-xs text-muted">-</span>
                  : data.prompt_versions.map((v) => <Badge key={v} tone="accent">{v}</Badge>)}
              </div>
              <h3 className="mb-1 text-xs font-semibold uppercase text-muted">{t("monitor.byStatus", "상태별 실행")}</h3>
              <div className="flex flex-wrap gap-1">
                {Object.entries(data.by_status).map(([st, n]) => (
                  <Badge key={st} tone={st.includes("fail") || st === "rejected" ? "err" : st === "completed" || st === "succeeded" ? "ok" : st === "waiting_approval" ? "warn" : "muted"}>
                    {st} {n}
                  </Badge>
                ))}
              </div>
            </Card>
          </div>

          {/* 일별 실행/오류 차트 */}
          <Card title={t("monitor.daily", "일별 실행 / 오류")}>
            {daily.length === 0 ? <Empty>{t("monitor.noData", "데이터 없음")}</Empty> : (
              <>
                <div className="flex h-20 items-end gap-[2px]">
                  {daily.map((x) => (
                    <div key={x.date} className="flex flex-1 flex-col justify-end" title={`${x.date} · ${x.runs}회 · 오류 ${x.errors} · 토큰 ${x.tokens.toLocaleString()}`}>
                      <div className="rounded-t bg-red-400/80" style={{ height: `${x.runs ? Math.max(2, Math.round(x.errors / dMax * 100)) : 0}%` }} />
                      <div className="rounded-t bg-accent/70" style={{ height: `${Math.max(4, Math.round(x.runs / dMax * 100))}%` }} />
                    </div>
                  ))}
                </div>
                <div className="mt-1 flex justify-between text-[10px] text-muted">
                  <span>{daily[0]?.date}</span><span>{daily[daily.length - 1]?.date}</span>
                </div>
              </>
            )}
          </Card>

          {/* 최근 실패 run — 클릭 시 실행 이력 뷰로 */}
          <Card title={t("monitor.recentFailures", "최근 실패 run")}>
            {data.recent_failures.length === 0 ? <Empty>{t("monitor.noFailures", "실패 없음")}</Empty> : (
              <table className="w-full text-xs">
                <thead>
                  <tr className="text-left text-muted">
                    <th className="pb-1">{t("monitor.time", "시각")}</th>
                    <th className="pb-1">{t("monitor.workflow", "워크플로")}</th>
                    <th className="pb-1">{t("monitor.status", "상태")}</th>
                    <th className="pb-1">{t("monitor.project", "프로젝트")}</th>
                    <th className="pb-1">{t("monitor.error", "오류")}</th>
                  </tr>
                </thead>
                <tbody>
                  {data.recent_failures.map((r) => (
                    <tr
                      key={r.id}
                      className="cursor-pointer border-t border-border hover:bg-surface-hover"
                      onClick={() => setView("runs")}
                    >
                      <td className="py-1 whitespace-nowrap text-muted">{fmtTs(r.updated_at)}</td>
                      <td className="py-1 font-mono">{r.workflow}</td>
                      <td className="py-1"><Badge tone="err">{r.status}</Badge></td>
                      <td className="py-1 text-muted">{r.project || "-"}</td>
                      <td className="max-w-[280px] truncate py-1 text-muted" title={r.error}>{r.error || "-"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Card>
        </>
      )}
    </div>
  );
}
