/** Agent Insights — LLM/agent trace 기반 속도·비용 최적화 대시보드.
 * 지연시간 백분위, 토큰, 모델/프롬프트 버전, stage 병목, 모델 라우팅,
 * 재시도 낭비, 워크플로 단가, 월 예산, 승인 백로그, 최근 실패 run.
 * 데이터: GET /api/v1/llm-monitor/stats?days= */
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { apiGet } from "../../shared/api/client";
import { useI18n } from "../../shared/i18n";
import "./i18n";
import { PageHead, Card, Empty, Spinner, ErrorBox, Button, Badge } from "../../shared/ui";
import { fmtTs, fmtAge } from "../../shared/lib/format";
import { useAppStore } from "../../shared/store/appStore";


interface StageAgg {
  stage_key: string; calls: number; failures: number;
  failure_rate_pct: number | null; tokens: number;
  latency_s: { p50: number | null; p95: number | null; avg: number | null };
  models: { model: string; calls: number; tokens: number; failures: number }[];
}
interface ModelStageRow { stage_key: string; model: string; calls: number; tokens: number; failures: number; failure_rate_pct: number | null }
interface PvStat { version: string; calls: number; tokens: number; failures: number }
interface WfStat { workflow: string; runs: number; failed: number; avg_duration_s: number | null; total_tokens: number; avg_tokens_per_run: number | null }

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
  prompt_version_stats: PvStat[];
  stage_model_matrix: ModelStageRow[];
  by_stage: StageAgg[];
  by_workflow: WfStat[];
  stage_calls: number;
  stage_failures: number;
  stage_failure_rate_pct: number | null;
  retry: { calls: number; tokens: number; token_pct: number | null; failed_stage_tokens: number; attempts_hist: Record<string, number> };
  tokens_ratio: { input_pct: number | null; output_pct: number | null };
  budget: { month: string; monthly_tokens: number; budget_max: number; used_pct: number; alert: boolean } | null;
  approval_backlog: { waiting: number; oldest_wait_s: number | null; total_wait_s: number | null };
  daily: { date: string; runs: number; tokens: number; errors: number }[];
  recent_failures: { id: string; workflow: string; status: string; project?: string; error: string; updated_at: string; trace_id?: string }[];
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

function failTone(pct: number | null | undefined): "err" | "warn" | "ok" | "muted" {
  if (pct == null) return "muted";
  if (pct > 20) return "err";
  if (pct > 5) return "warn";
  return "ok";
}

function CopyBtn({ text, label }: { text: string; label: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      className="ml-1 rounded border border-border px-1 text-[10px] text-muted hover:bg-surface-hover"
      onClick={(e) => { e.stopPropagation(); navigator.clipboard?.writeText(text).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1200); }); }}
    >
      {copied ? "✓" : label}
    </button>
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

  // 주간 인사이트 — 데이터에서 자동 도출
  const insights: string[] = [];
  if (data) {
    const topStage = data.by_stage[0];
    if (topStage?.latency_s.p95 != null && topStage.latency_s.p95 > 0) {
      insights.push(t("monitor.insight.slowest", "가장 느린 stage: {stage} (p95 {lat}, 호출 {calls}회)")
        .replace("{stage}", topStage.stage_key)
        .replace("{lat}", fmtSecs(topStage.latency_s.p95))
        .replace("{calls}", String(topStage.calls)));
    }
    if (data.retry.token_pct != null && data.retry.token_pct > 0) {
      insights.push(t("monitor.insight.retryTokens", "재시도로 소모한 토큰: 전체의 {pct}% ({tok} tok, {calls}회)")
        .replace("{pct}", String(data.retry.token_pct))
        .replace("{tok}", data.retry.tokens.toLocaleString())
        .replace("{calls}", String(data.retry.calls)));
    }
    const topTokStage = [...(data.by_stage || [])].sort((a, b) => b.tokens - a.tokens)[0];
    if (topTokStage && data.tokens.total > 0) {
      insights.push(t("monitor.insight.topTok", "토큰 소비 1위 stage: {stage} ({pct}%)")
        .replace("{stage}", topTokStage.stage_key)
        .replace("{pct}", ((topTokStage.tokens / data.tokens.total) * 100).toFixed(0)));
    }
    if (data.budget?.alert) {
      insights.push(t("monitor.insight.budget", "월 토큰 예산 {pct}% 사용 — 모델 다운그레이드 또는 샘플링 강화 검토")
        .replace("{pct}", String(data.budget.used_pct)));
    }
    if (data.approval_backlog.waiting > 0 && data.approval_backlog.oldest_wait_s != null) {
      insights.push(t("monitor.insight.backlog", "승인 대기 {n}건 — 최장 {oldest}, 총 대기 {total}")
        .replace("{n}", String(data.approval_backlog.waiting))
        .replace("{oldest}", fmtAge(data.approval_backlog.oldest_wait_s))
        .replace("{total}", fmtAge(data.approval_backlog.total_wait_s)));
    }
    const worstModel = data.stage_model_matrix.find((r) => (r.failure_rate_pct ?? 0) > 20);
    if (worstModel) {
      insights.push(t("monitor.insight.badCombo", "높은 실패율 조합: {stage} × {model} ({pct}%)")
        .replace("{stage}", worstModel.stage_key)
        .replace("{model}", worstModel.model)
        .replace("{pct}", String(worstModel.failure_rate_pct)));
    }
  }

  return (
    <div className="space-y-4 p-4">
      <PageHead
        title={t("monitor.title", "Agent Insights")}
        desc={t("monitor.desc", "LLM/agent trace 분석 — 속도·비용 최적화 지표")}
      >
        <select
          value={days}
          onChange={(e) => setDays(Number(e.target.value))}
          className="rounded-md border border-border bg-surface px-2 py-1 text-xs"
        >
          {[7, 14, 30, 90].map((d) => <option key={d} value={d}>{t("monitor.days", "최근 {n}일").replace("{n}", String(d))}</option>)}
        </select>
        <Button variant="ghost" className="px-2 py-0.5 text-xs" onClick={() => refetch()}>
          {t("common.reload", "새로고침")}
        </Button>
      </PageHead>

      {isLoading && <Spinner />}
      {error && <ErrorBox message={error instanceof Error ? error.message : String(error)} onRetry={() => refetch()} />}

      {data && (
        <>
          {/* LangSmith 트레이싱 상태 */}
          <Card title={t("monitor.tracing", "LangSmith 트레이싱")}>
            {data.langsmith.enabled ? (
              <div className="flex flex-wrap items-center gap-2 text-sm">
                <Badge tone="ok">{t("monitor.tracingOn", "활성")}</Badge>
                {data.langsmith.project && <span className="text-muted">project: <b className="text-ink">{data.langsmith.project}</b></span>}
                <span className="text-muted">sample rate: <b className="text-ink">{data.langsmith.sample_rate}</b></span>
                <span className="text-xs text-muted">{t("monitor.tracingHint", "상세 trace는 LangSmith 프로젝트에서 확인 (metadata: jobmate.run_id)")}</span>
              </div>
            ) : (
              <div className="text-sm text-muted">
                {t("monitor.tracingOff", "비활성 — LANGSMITH_API_KEY를 설정하면 run/스테이지 trace가 기록됩니다.")}
              </div>
            )}
          </Card>

          {/* 자동 인사이트 — 속도/비용 최적화 액션 아이템 */}
          {insights.length > 0 && (
            <Card title={t("monitor.insights", "최적화 인사이트")}>
              <ul className="list-inside list-disc space-y-1 text-sm text-ink">
                {insights.map((s, i) => <li key={i}>{s}</li>)}
              </ul>
            </Card>
          )}

          {/* 월 예산 */}
          {data.budget && (
            <Card title={t("monitor.budget", "월 토큰 예산")}>
              <div className="space-y-2">
                <div className="flex items-center justify-between text-sm">
                  <span className="text-muted">{data.budget.month}</span>
                  <span className={data.budget.alert ? "font-semibold text-red-500" : "text-ink"}>
                    {data.budget.monthly_tokens.toLocaleString()} / {data.budget.budget_max.toLocaleString()}
                    {" "}({data.budget.used_pct}%)
                  </span>
                </div>
                <div className="h-3 overflow-hidden rounded-full bg-surface-hover">
                  <div
                    className={`h-full rounded-full ${data.budget.used_pct >= 100 ? "bg-red-500" : data.budget.used_pct >= 80 ? "bg-amber-500" : "bg-emerald-500"}`}
                    style={{ width: `${Math.min(100, data.budget.used_pct)}%` }}
                  />
                </div>
                {data.budget.alert && <Badge tone="err">{t("monitor.budgetAlert", "예산 80% 초과 — 알림함에 등록됨")}</Badge>}
              </div>
            </Card>
          )}

          {/* 핵심 지표 */}
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
            <Kpi label={t("monitor.runs", "실행")} value={String(data.runs_total)} />
            <Kpi label={t("monitor.errorRate", "오류율")} value={data.error_rate_pct != null ? `${data.error_rate_pct}%` : "-"} tone={data.error_rate_pct && data.error_rate_pct > 10 ? "err" : data.error_rate_pct && data.error_rate_pct > 0 ? "warn" : "ok"} />
            <Kpi label={t("monitor.p95", "p95 지연")} value={fmtSecs(data.latency_s.p95)} tone={data.latency_s.p95 != null && data.latency_s.p95 > 600 ? "warn" : "muted"} />
            <Kpi label={t("monitor.avgTokens", "평균 토큰/run")} value={data.tokens.avg_per_run != null ? data.tokens.avg_per_run.toLocaleString() : "-"} />
            <Kpi label={t("monitor.stageCalls", "스테이지 호출")} value={String(data.stage_calls)} />
            <Kpi label={t("monitor.stageFail", "스테이지 실패율")} value={data.stage_failure_rate_pct != null ? `${data.stage_failure_rate_pct}%` : "-"} tone={data.stage_failure_rate_pct && data.stage_failure_rate_pct > 10 ? "err" : "muted"} />
          </div>

          <div className="grid gap-4 lg:grid-cols-2">
            {/* 지연시간 */}
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
            <Card title={t("monitor.tokenUsage", "토큰 사용량")}>
              <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm">
                <dt className="text-muted">{t("monitor.tokIn", "입력")}</dt>
                <dd className="text-right font-medium text-ink">{data.tokens.total_in.toLocaleString()}</dd>
                <dt className="text-muted">{t("monitor.tokOut", "출력")}</dt>
                <dd className="text-right font-medium text-ink">{data.tokens.total_out.toLocaleString()}</dd>
                <dt className="text-muted">{t("monitor.tokTotal", "합계")}</dt>
                <dd className="text-right font-medium text-ink">{data.tokens.total.toLocaleString()}</dd>
              </dl>
              {(data.tokens_ratio.input_pct != null) && (
                <div className="mt-2">
                  <div className="flex h-2 overflow-hidden rounded-full bg-surface-hover">
                    <div className="bg-accent/80" style={{ width: `${data.tokens_ratio.input_pct}%` }} />
                    <div className="bg-emerald-400/80" style={{ width: `${data.tokens_ratio.output_pct}%` }} />
                  </div>
                  <div className="mt-1 flex justify-between text-[10px] text-muted">
                    <span>in {data.tokens_ratio.input_pct}%</span>
                    <span>out {data.tokens_ratio.output_pct}%</span>
                  </div>
                </div>
              )}
              <div className="mt-3 border-t border-border pt-3">
                <h3 className="mb-1 text-xs font-semibold uppercase text-muted">{t("monitor.backlog", "승인 백로그 (human review)")}</h3>
                <div className="flex flex-wrap items-center gap-2 text-sm">
                  <Badge tone={data.approval_backlog.waiting > 0 ? "warn" : "ok"}>
                    {data.approval_backlog.waiting} {t("monitor.waiting", "대기")}
                  </Badge>
                  {data.approval_backlog.oldest_wait_s != null && (
                    <span className="text-xs text-muted">{t("monitor.oldest", "최장 대기")} {fmtAge(data.approval_backlog.oldest_wait_s)}</span>
                  )}
                  {data.approval_backlog.total_wait_s != null && (
                    <span className="text-xs text-muted">{t("monitor.totalWait", "총 대기 시간")} {fmtAge(data.approval_backlog.total_wait_s)}</span>
                  )}
                </div>
              </div>
            </Card>
          </div>

          {/* Stage 병목 — 속도 최적화 핵심 */}
          <Card title={t("monitor.stageBottleneck", "Stage 병목 (p95 지연 순)")}>
            {data.by_stage.length === 0 ? <Empty>{t("monitor.noData", "데이터 없음")}</Empty> : (
              <div className="overflow-x-auto">
                <table className="w-full text-xs">
                  <thead>
                    <tr className="text-left text-muted">
                      <th className="pb-1">{t("monitor.stage", "Stage")}</th>
                      <th className="pb-1 text-right">{t("monitor.calls", "호출")}</th>
                      <th className="pb-1 text-right">p50</th>
                      <th className="pb-1 text-right">p95</th>
                      <th className="pb-1 text-right">{t("monitor.tokens", "토큰")}</th>
                      <th className="pb-1 text-right">{t("monitor.failRate", "실패율")}</th>
                      <th className="pb-1">{t("monitor.topModels", "주요 모델")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.by_stage.map((s) => (
                      <tr key={s.stage_key} className="border-t border-border">
                        <td className="py-1 font-mono">{s.stage_key}</td>
                        <td className="py-1 text-right">{s.calls}</td>
                        <td className="py-1 text-right">{fmtSecs(s.latency_s.p50)}</td>
                        <td className="py-1 text-right font-medium">{fmtSecs(s.latency_s.p95)}</td>
                        <td className="py-1 text-right">{s.tokens.toLocaleString()}</td>
                        <td className="py-1 text-right"><Badge tone={failTone(s.failure_rate_pct)}>{s.failure_rate_pct ?? "-"}%</Badge></td>
                        <td className="py-1 text-muted">{s.models.slice(0, 2).map((m) => m.model).join(", ") || "-"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Card>

          <div className="grid gap-4 lg:grid-cols-2">
            {/* 모델 라우팅 — stage×model 비용/실패율 */}
            <Card title={t("monitor.modelRouting", "모델 라우팅 분석 (stage × model)")}>
              {data.stage_model_matrix.length === 0 ? <Empty>{t("monitor.noData", "데이터 없음")}</Empty> : (
                <div className="max-h-80 overflow-y-auto">
                  <table className="w-full text-xs">
                    <thead>
                      <tr className="text-left text-muted">
                        <th className="pb-1">{t("monitor.stage", "Stage")}</th>
                        <th className="pb-1">{t("monitor.model", "모델")}</th>
                        <th className="pb-1 text-right">{t("monitor.calls", "호출")}</th>
                        <th className="pb-1 text-right">{t("monitor.tokens", "토큰")}</th>
                        <th className="pb-1 text-right">{t("monitor.failRate", "실패율")}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {data.stage_model_matrix.slice(0, 20).map((r, i) => (
                        <tr key={i} className="border-t border-border">
                          <td className="py-1 font-mono">{r.stage_key}</td>
                          <td className="py-1 font-mono">{r.model}</td>
                          <td className="py-1 text-right">{r.calls}</td>
                          <td className="py-1 text-right">{r.tokens.toLocaleString()}</td>
                          <td className="py-1 text-right"><Badge tone={failTone(r.failure_rate_pct)}>{r.failure_rate_pct ?? "-"}%</Badge></td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </Card>

            {/* 프롬프트 버전 비교 + 재시도 낭비 */}
            <div className="space-y-4">
              <Card title={t("monitor.pvCompare", "프롬프트 버전 비교")}>
                {data.prompt_version_stats.length === 0 ? <Empty>{t("monitor.noData", "데이터 없음")}</Empty> : (
                  <table className="w-full text-xs">
                    <thead>
                      <tr className="text-left text-muted">
                        <th className="pb-1">{t("monitor.version", "버전")}</th>
                        <th className="pb-1 text-right">{t("monitor.calls", "호출")}</th>
                        <th className="pb-1 text-right">{t("monitor.tokens", "토큰")}</th>
                        <th className="pb-1 text-right">{t("monitor.failRate", "실패율")}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {data.prompt_version_stats.map((p) => (
                        <tr key={p.version} className="border-t border-border">
                          <td className="py-1"><Badge tone="accent">{p.version}</Badge></td>
                          <td className="py-1 text-right">{p.calls}</td>
                          <td className="py-1 text-right">{p.tokens.toLocaleString()}</td>
                          <td className="py-1 text-right">
                            <Badge tone={failTone(p.calls ? Math.round(p.failures / p.calls * 1000) / 10 : null)}>
                              {p.calls ? `${Math.round(p.failures / p.calls * 1000) / 10}%` : "-"}
                            </Badge>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </Card>

              <Card title={t("monitor.retryWaste", "재시도 토큰 낭비")}>
                <div className="grid grid-cols-2 gap-2 text-sm sm:grid-cols-4">
                  <Kpi label={t("monitor.retryCalls", "재시도 호출")} value={String(data.retry.calls)} />
                  <Kpi label={t("monitor.retryTokens", "재시도 토큰")} value={data.retry.tokens.toLocaleString()} />
                  <Kpi label={t("monitor.retryPct", "전체 대비")} value={data.retry.token_pct != null ? `${data.retry.token_pct}%` : "-"} tone={data.retry.token_pct && data.retry.token_pct > 15 ? "warn" : "muted"} />
                  <Kpi label={t("monitor.failedTok", "실패 stage 토큰")} value={data.retry.failed_stage_tokens.toLocaleString()} tone={data.retry.failed_stage_tokens > 0 ? "err" : "ok"} />
                </div>
                {Object.keys(data.retry.attempts_hist).length > 0 && (
                  <div className="mt-2 flex flex-wrap gap-1">
                    {Object.entries(data.retry.attempts_hist).map(([a, n]) => (
                      <Badge key={a} tone={Number(a) > 1 ? "warn" : "muted"}>{t("monitor.attempts", "attempt {a}: {n}회").replace("{a}", a).replace("{n}", String(n))}</Badge>
                    ))}
                  </div>
                )}
              </Card>
            </div>
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

            {/* 워크플로별 단가 + 상태 분포 */}
            <div className="space-y-4">
              <Card title={t("monitor.wfCost", "워크플로별 단가")}>
                {data.by_workflow.length === 0 ? <Empty>{t("monitor.noData", "데이터 없음")}</Empty> : (
                  <table className="w-full text-xs">
                    <thead>
                      <tr className="text-left text-muted">
                        <th className="pb-1">{t("monitor.workflow", "워크플로")}</th>
                        <th className="pb-1 text-right">{t("monitor.runs", "실행")}</th>
                        <th className="pb-1 text-right">{t("monitor.tokensPerRun", "토큰/run")}</th>
                        <th className="pb-1 text-right">{t("monitor.totalTokens", "총 토큰")}</th>
                        <th className="pb-1 text-right">{t("monitor.avgDur", "평균 시간")}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {data.by_workflow.map((w) => (
                        <tr key={w.workflow} className="border-t border-border">
                          <td className="py-1 font-mono">{w.workflow}</td>
                          <td className="py-1 text-right">{w.runs}</td>
                          <td className="py-1 text-right">{w.avg_tokens_per_run?.toLocaleString() ?? "-"}</td>
                          <td className="py-1 text-right">{w.total_tokens.toLocaleString()}</td>
                          <td className="py-1 text-right">{fmtSecs(w.avg_duration_s)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </Card>

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
          </div>

          {/* 일별 실행/오류 차트 */}
          <Card title={t("monitor.daily", "일별 실행 / 오류")}>
            {daily.length === 0 ? <Empty>{t("monitor.noData", "데이터 없음")}</Empty> : (
              <>
                <div className="flex h-20 items-end gap-[2px]">
                  {daily.map((x) => (
                    <div key={x.date} className="flex flex-1 flex-col justify-end" title={t("monitor.dailyTitle", "{date} · {runs}회 · 오류 {errors} · 토큰 {tokens}")
                      .replace("{date}", x.date)
                      .replace("{runs}", String(x.runs))
                      .replace("{errors}", String(x.errors))
                      .replace("{tokens}", x.tokens.toLocaleString())}>
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

          {/* 최근 실패 run */}
          <Card title={t("monitor.recentFailures", "최근 실패 run")}>
            {data.recent_failures.length === 0 ? <Empty>{t("monitor.noFailures", "실패 없음")}</Empty> : (
              <div className="overflow-x-auto">
                <table className="w-full text-xs">
                  <thead>
                    <tr className="text-left text-muted">
                      <th className="pb-1">{t("monitor.time", "시각")}</th>
                      <th className="pb-1">{t("monitor.workflow", "워크플로")}</th>
                      <th className="pb-1">{t("monitor.status", "상태")}</th>
                      <th className="pb-1">{t("monitor.project", "프로젝트")}</th>
                      <th className="pb-1">{t("monitor.trace", "Trace")}</th>
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
                        <td className="py-1 font-mono text-muted">
                          {r.trace_id ? <>{r.trace_id.slice(0, 8)}…<CopyBtn text={r.trace_id} label="copy" /></> : "-"}
                        </td>
                        <td className="max-w-[240px] truncate py-1 text-muted" title={r.error}>{r.error || "-"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Card>
        </>
      )}
    </div>
  );
}
