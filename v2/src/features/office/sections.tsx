/** office 관측 대시보드 섹션 컴포넌트들 — 전부 실데이터, 빈 상태/권한 강등 처리 포함. */
import { useMemo, useState } from "react";
import { useQueries, useQueryClient } from "@tanstack/react-query";
import { apiGet, apiPost, ApiError } from "../../shared/api/client";
import { useI18n } from "../../shared/i18n";
import { Badge, Button, Card, Empty, Spinner, ErrorBox } from "../../shared/ui";
import { fmtAge, fmtElapsed, fmtTs, usd } from "../../shared/lib/format";
import { useAppStore, roleAllows } from "../../shared/store/appStore";
import { CostArea, ShareBars, type BarRow } from "./charts";
import type { OfficeData } from "./queries";
import type {
  ObsAnomaly, ObsApprovalWaiting, ObsCostItem, ObsErrorClass,
  RunDetail, RunListItem, RunStage, SessionRow,
} from "./types";

// ---------- 포맷/배지 헬퍼 ----------

function fmtSecs(v: number | null | undefined): string {
  if (v == null || !isFinite(v)) return "-";
  if (v < 60) return `${Math.round(v)}s`;
  if (v < 3600) return `${(v / 60).toFixed(1)}m`;
  return `${(v / 3600).toFixed(1)}h`;
}

function pct(v: number | null | undefined): string {
  return v == null || !isFinite(v) ? "-" : `${v.toFixed(1)}%`;
}

/** ISO 시각 → 경과 시간 문자열 (fmtAge는 초 단위 입력). */
function fmtRel(iso: string | null | undefined): string {
  if (!iso) return "-";
  const t = new Date(iso).getTime();
  if (isNaN(t)) return "-";
  return fmtAge(Math.max(0, (Date.now() - t) / 1000));
}

const STAGE_DONE = new Set(["succeeded", "completed", "failed", "skipped", "cancelled"]);
const RUN_ACTIVE = new Set(["running", "waiting_approval", "waiting_review", "queued"]);

const TOOL_LABEL: Record<string, string> = {
  claude: "Claude", codex: "Codex", gemini: "Gemini", devin: "Devin",
};

export function StatusBadge({ status }: { status?: string }) {
  const s = String(status || "");
  const tone =
    s === "succeeded" || s === "completed" ? "ok"
    : s === "failed" || s === "error" || s === "cancelled" ? "err"
    : s === "running" || s === "started" ? "accent"
    : s === "waiting_approval" ? "warn"
    : "muted";
  return <Badge tone={tone}>{s || "-"}</Badge>;
}

export function HealthBadge({ health, t }: { health?: string; t: (k: string, f?: string) => string }) {
  const h = String(health || "");
  const tone = h === "active" ? "ok" : h === "stale" ? "warn" : "muted";
  const key = ["active", "stale", "long_idle", "ended"].includes(h) ? `office.health.${h}` : "office.health.unknown";
  return <Badge tone={tone}>{t(key, h || "-")}</Badge>;
}

function StageDot({ status }: { status?: string }) {
  const s = String(status || "");
  const cls =
    s === "succeeded" || s === "completed" ? "bg-emerald-500"
    : s === "failed" || s === "error" ? "bg-red-500"
    : s === "running" || s === "started" ? "bg-accent animate-pulse"
    : s === "waiting_approval" || s === "waiting" ? "bg-amber-500"
    : "bg-surface-hover";
  return <span className={`inline-block h-1.5 w-1.5 flex-none rounded-full ${cls}`} />;
}

function isForbidden(e: unknown): boolean {
  return e instanceof ApiError && e.status === 403;
}

// ---------- KPI 카드 ----------

export function KpiCard({ id, label, value, sub, tone = "muted", onClick }: {
  id?: string; label: string; value: string; sub?: string;
  tone?: "ok" | "warn" | "err" | "accent" | "muted"; onClick?: () => void;
}) {
  const valCls =
    tone === "err" ? "text-red-500" : tone === "warn" ? "text-amber-500"
    : tone === "ok" ? "text-emerald-500" : tone === "accent" ? "text-accent" : "text-ink";
  return (
    <button
      id={id}
      type="button"
      onClick={onClick}
      className="rounded-lg border border-border bg-surface px-3 py-2 text-left transition-colors hover:bg-surface-hover"
    >
      <div className="text-[11px] text-muted">{label}</div>
      <div className={`text-lg font-semibold ${valCls}`}>{value}</div>
      {sub && <div className="truncate text-[10px] text-muted">{sub}</div>}
    </button>
  );
}

// ---------- 주의 필요 스트립 ----------

function anomalyText(a: ObsAnomaly): string {
  return String(a.title || a.message || a.description || a.kind || a.type || a.metric || "-");
}
function anomalyTime(a: ObsAnomaly): string {
  return String(a.detected_at || a.created_at || "");
}

export function AttentionStrip({ d }: { d: OfficeData }) {
  const { t } = useI18n();
  const qc = useQueryClient();
  const [busy, setBusy] = useState<string | null>(null);
  // 승인/거절은 lead+ 전용 — member에게 버튼을 보여주면 클릭 시 403
  const canDecide = roleAllows("lead", useAppStore((s) => s.role));

  // /observability/approvals 우선, 실패/누락분은 /runs 목록의 waiting_approval로 보강
  const waiting = useMemo(() => {
    const obs = d.approvalsQ.data?.waiting || [];
    const seen = new Set(obs.map((w) => String(w.run_id || w.id || "")).filter(Boolean));
    const merged: { runId: string; workflow?: string; project?: string; stage?: string; waited?: number | null }[] =
      obs.map((w: ObsApprovalWaiting) => ({
        runId: String(w.run_id || w.id || ""),
        workflow: w.workflow, project: w.project, stage: w.stage_key, waited: w.waited_s,
      }));
    for (const r of d.runsQ.data || []) {
      if (r.status === "waiting_approval" && !seen.has(r.id)) {
        merged.push({ runId: r.id, workflow: r.workflow, project: r.project });
      }
    }
    return merged.filter((w) => w.runId);
  }, [d.approvalsQ.data, d.runsQ.data]);

  const failures = (d.llmQ.data?.recent_failures || []).slice(0, 5);
  const anomalies = (d.anomaliesQ.data?.items || []).slice(0, 5);
  const oldestWait = d.approvalsQ.data?.stats?.oldest_wait_s ?? d.llmQ.data?.approval_backlog?.oldest_wait_s;

  const decide = async (runId: string, decision: "approve" | "reject") => {
    const key = `${runId}:${decision}`;
    setBusy(key);
    try {
      await apiPost(`/runs/${encodeURIComponent(runId)}/approve`, { decision, comment: "" });
      await Promise.all([
        qc.invalidateQueries({ queryKey: ["approvals"] }),
        qc.invalidateQueries({ queryKey: ["runs"] }),
      ]);
    } catch (e) {
      alert(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  const loading = d.approvalsQ.isLoading || d.llmQ.isLoading || d.anomaliesQ.isLoading;

  return (
    <div className="grid gap-3 lg:grid-cols-3">
      {/* 승인 대기 */}
      <Card
        title={t("office.attention.approvals", "승인 대기")}
        actions={waiting.length ? <Badge tone="warn">{waiting.length}</Badge> : undefined}
      >
        {loading && waiting.length === 0 && <Spinner />}
        {!waiting.length && !loading && <Empty>{t("office.noWaiting", "대기 중인 승인이 없습니다")}</Empty>}
        <ul className="max-h-56 space-y-1.5 overflow-y-auto">
          {waiting.map((w) => (
            <li key={w.runId} className="rounded-md border border-border bg-surface-hover px-2 py-1.5">
              <div className="flex items-center justify-between gap-2">
                <div className="min-w-0">
                  <div className="truncate text-xs font-medium text-ink">
                    {w.workflow || w.runId.slice(0, 8)}
                    {w.project && <span className="ml-1 text-muted">· {w.project}</span>}
                  </div>
                  <div className="text-[10px] text-muted">
                    {w.stage || w.runId.slice(0, 8)}
                    {w.waited != null && ` · ${t("office.waited", "{s} 대기").replace("{s}", fmtSecs(w.waited))}`}
                  </div>
                </div>
                {canDecide && (
                <div className="flex flex-none gap-1">
                  <Button
                    className="px-2 py-0.5 text-[11px]"
                    disabled={busy !== null}
                    onClick={() => decide(w.runId, "approve")}
                  >
                    {busy === `${w.runId}:approve` ? t("office.acting", "처리 중…") : t("office.approve", "승인")}
                  </Button>
                  <Button
                    variant="secondary"
                    className="px-2 py-0.5 text-[11px]"
                    disabled={busy !== null}
                    onClick={() => decide(w.runId, "reject")}
                  >
                    {busy === `${w.runId}:reject` ? t("office.acting", "처리 중…") : t("office.reject", "거절")}
                  </Button>
                </div>
                )}
              </div>
            </li>
          ))}
        </ul>
        {oldestWait != null && waiting.length > 0 && (
          <div className="mt-2 text-[10px] text-muted">
            {t("office.oldest", "최장 대기 {s}").replace("{s}", fmtAge(oldestWait))}
          </div>
        )}
      </Card>

      {/* 최근 실패 */}
      <Card title={t("office.attention.failures", "최근 실패")}>
        {d.llmQ.isLoading && <Spinner />}
        {!failures.length && !d.llmQ.isLoading && <Empty>{t("office.noFailures", "최근 실패가 없습니다")}</Empty>}
        <ul className="max-h-56 space-y-1 overflow-y-auto">
          {failures.map((f) => (
            <li key={f.id} className="rounded-md border border-border px-2 py-1.5">
              <div className="flex items-center justify-between gap-2">
                <span className="min-w-0 truncate font-mono text-xs text-ink">{f.id.slice(0, 8)}</span>
                <span className="flex-none text-[10px] text-muted">{fmtTs(f.updated_at)}</span>
              </div>
              <div className="truncate text-[11px] text-muted">
                {f.workflow}{f.project ? ` · ${f.project}` : ""}
              </div>
              {f.error && <div className="mt-0.5 truncate text-[11px] text-red-500" title={f.error}>{f.error}</div>}
            </li>
          ))}
        </ul>
      </Card>

      {/* 이상 징후 */}
      <Card title={t("office.attention.anomalies", "이상 징후")}>
        {d.anomaliesQ.isLoading && <Spinner />}
        {d.anomaliesQ.error && isForbidden(d.anomaliesQ.error) && (
          <div className="text-xs text-muted">{t("office.forbidden", "권한 부족 — 이 항목은 lead 역할에서 확인할 수 있습니다")}</div>
        )}
        {!anomalies.length && !d.anomaliesQ.isLoading && !d.anomaliesQ.error && (
          <Empty>{t("office.noAnomalies", "감지된 이상 징후가 없습니다")}</Empty>
        )}
        <ul className="max-h-56 space-y-1 overflow-y-auto">
          {anomalies.map((a, i) => (
            <li key={String(a.id || i)} className="rounded-md border border-border px-2 py-1.5">
              <div className="flex items-center gap-2">
                {a.severity && <Badge tone={String(a.severity) === "critical" || String(a.severity) === "high" ? "err" : String(a.severity) === "medium" ? "warn" : "muted"}>{String(a.severity)}</Badge>}
                <span className="min-w-0 flex-1 truncate text-xs text-ink">{anomalyText(a)}</span>
              </div>
              <div className="mt-0.5 flex flex-wrap gap-x-2 text-[10px] text-muted">
                {(a.metric || a.kind || a.type) ? <span>{String(a.metric || a.kind || a.type)}</span> : null}
                {(a.value !== undefined || a.threshold !== undefined) && (
                  <span>{String(a.value ?? "-")} / {String(a.threshold ?? "-")}</span>
                )}
                {anomalyTime(a) && <span>{t("office.anomaly.at", "감지")} {fmtTs(anomalyTime(a))}</span>}
              </div>
            </li>
          ))}
        </ul>
      </Card>
    </div>
  );
}

// ---------- 라이브 run 보드 ----------

function stageStats(stages: RunStage[] | undefined) {
  const list = stages || [];
  const done = list.filter((s) => s.finished_at || STAGE_DONE.has(String(s.status || ""))).length;
  const current = list.find((s) => s.status === "running" || s.status === "started")
    || [...list].reverse().find((s) => !s.finished_at && !STAGE_DONE.has(String(s.status || "")));
  const tokens = list.reduce((a, s) => a + (Number(s.tokens_in) || 0) + (Number(s.tokens_out) || 0), 0);
  return { done, total: list.length, current, tokens };
}

function RunCard({ run, detail, loading }: { run: RunListItem; detail?: RunDetail; loading?: boolean }) {
  const { t } = useI18n();
  const { done, total, current, tokens } = stageStats(detail?.stages);
  const elapsedFrom = detail?.created_at || run.created_at;
  return (
    <div className="rounded-lg border border-border bg-surface p-3">
      <div className="flex items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          <StatusBadge status={run.status} />
          <span className="truncate text-sm font-medium text-ink">{run.workflow || run.id.slice(0, 8)}</span>
        </div>
        <span className="flex-none font-mono text-[10px] text-muted">{run.id.slice(0, 8)}</span>
      </div>
      {run.project && <div className="mt-0.5 truncate text-[11px] text-muted">{run.project}</div>}

      {loading && !detail ? (
        <div className="mt-2 space-y-1">
          <div className="h-2 animate-pulse rounded bg-surface-hover" />
          <div className="h-2 w-2/3 animate-pulse rounded bg-surface-hover" />
        </div>
      ) : detail?.stages?.length ? (
        <>
          <div className="mt-2 flex items-center justify-between text-[11px] text-muted">
            <span>{t("office.live.progress", "{done}/{total} 단계").replace("{done}", String(done)).replace("{total}", String(total))}</span>
            {current && <span className="truncate text-accent">{current.stage_key || current.stage}</span>}
          </div>
          <div className="mt-1 flex flex-wrap gap-1">
            {detail.stages.map((s, i) => (
              <span
                key={s.id || i}
                title={`${s.stage_key || s.stage}${s.model ? ` · ${s.model}` : ""}${s.error ? ` · ${s.error}` : ""}`}
                className={`inline-flex items-center gap-1 rounded border px-1 py-0.5 text-[10px] ${
                  s.status === "failed" || s.status === "error"
                    ? "border-red-500/40 text-red-500"
                    : s.status === "running" || s.status === "started"
                      ? "border-accent/50 text-accent"
                      : "border-border text-muted"
                }`}
              >
                <StageDot status={s.status} />
                {s.stage_key || s.stage}
                {(s.attempt || 1) > 1 && <span className="text-muted">×{s.attempt}</span>}
              </span>
            ))}
          </div>
        </>
      ) : (
        <div className="mt-2 text-[11px] text-muted">{t("office.live.stages", "단계")} —</div>
      )}

      <div className="mt-2 flex items-center justify-between border-t border-border pt-2 text-[11px] text-muted">
        <span>{t("office.live.elapsed", "경과")} {fmtElapsed(elapsedFrom)}</span>
        <span>{t("office.live.tokens", "토큰")} {tokens.toLocaleString()}</span>
      </div>
    </div>
  );
}

export function LiveRuns({ d }: { d: OfficeData }) {
  const { t } = useI18n();
  const active = useMemo(
    () => (d.runsQ.data || []).filter((r) => RUN_ACTIVE.has(String(r.status || ""))).slice(0, 12),
    [d.runsQ.data],
  );

  const details = useQueries({
    queries: active.map((r) => ({
      queryKey: ["run", "office", r.id],
      queryFn: () => apiGet<RunDetail>(`/runs/${encodeURIComponent(r.id)}`),
      refetchInterval: 8000,
      retry: false,
    })),
  });
  const detailMap = useMemo(() => {
    const m = new Map<string, RunDetail>();
    active.forEach((r, i) => {
      const dd = details[i]?.data;
      if (dd) m.set(r.id, dd);
    });
    return m;
  }, [active, details]);

  return (
    <Card
      title={t("office.live.title", "실행 보드")}
      actions={active.length ? <Badge tone="accent">{active.length}</Badge> : undefined}
    >
      {d.runsQ.isLoading && <Spinner />}
      {d.runsQ.error && (
        <ErrorBox message={d.runsQ.error instanceof Error ? d.runsQ.error.message : String(d.runsQ.error)} onRetry={() => d.runsQ.refetch()} />
      )}
      {d.runsQ.data && !active.length && <Empty>{t("office.live.empty", "진행 중인 run이 없습니다")}</Empty>}
      {active.length > 0 && (
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {active.map((r, i) => (
            <RunCard key={r.id} run={r} detail={detailMap.get(r.id)} loading={details[i]?.isLoading} />
          ))}
        </div>
      )}
    </Card>
  );
}

// ---------- 비용 추이 ----------

function costPoints(items: ObsCostItem[] | undefined) {
  return (items || [])
    .map((it) => ({ label: String(it.key || ""), value: Number(it.cost_usd) || 0 }))
    .filter((p) => p.label)
    .sort((a, b) => a.label.localeCompare(b.label));
}

export function CostSection({ d }: { d: OfficeData }) {
  const { t } = useI18n();
  const points = useMemo(() => costPoints(d.costsQ.data?.items), [d.costsQ.data]);
  const models = (d.usageQ.data?.by_model || []).slice(0, 6);
  const total30 = points.reduce((a, p) => a + p.value, 0);

  return (
    <Card title={t("office.cost.title", "비용 추이")}>
      {d.costsQ.isLoading && <Spinner />}
      {d.costsQ.error && (
        <div className="text-xs text-muted">
          {isForbidden(d.costsQ.error)
            ? t("office.forbidden", "권한 부족 — 이 항목은 lead 역할에서 확인할 수 있습니다")
            : t("office.cost.empty", "비용 데이터가 없습니다")}
        </div>
      )}
      {d.costsQ.data && !points.length && <Empty>{t("office.cost.empty", "비용 데이터가 없습니다")}</Empty>}
      {points.length > 0 && (
        <>
          <div className="mb-1 text-xs text-muted">
            {t("office.cost.range", "최근 {n}일 일별 비용").replace("{n}", String(points.length))}
            {" · "}<span className="font-medium text-ink">${total30.toFixed(2)}</span>
          </div>
          <CostArea points={points} />
        </>
      )}

      {models.length > 0 && (
        <div className="mt-4 border-t border-border pt-3">
          <div className="mb-1 text-xs font-bold text-muted">{t("office.cost.topModels", "모델별 비용")}</div>
          <table className="w-full text-xs">
            <thead>
              <tr className="text-left text-muted">
                <th className="pb-1">{t("office.col.model", "모델")}</th>
                <th className="pb-1 text-right">{t("office.col.cost", "비용")}</th>
                <th className="pb-1 text-right">{t("office.col.tokens", "토큰")}</th>
              </tr>
            </thead>
            <tbody>
              {models.map((m, i) => (
                <tr key={i} className="border-t border-border">
                  <td className="py-1 font-mono">{m.model || "-"}</td>
                  <td className="py-1 text-right">{usd(m.cost_usd)}</td>
                  <td className="py-1 text-right">{Number(m.tokens || 0).toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {d.usageQ.error && !isForbidden(d.usageQ.error) && (
        <div className="mt-2 text-[10px] text-muted">usage summary unavailable</div>
      )}
    </Card>
  );
}

// ---------- 오류 분류 ----------

function errorRows(items: ObsErrorClass[] | undefined): BarRow[] {
  return (items || []).map((e) => ({
    label: String(e.error_class || "unknown"),
    value: Number(e.count) || 0,
    display: `${Number(e.count) || 0}${e.pct != null ? ` · ${pct(Number(e.pct))}` : ""}`,
    hint: (e.samples || [])
      .map((s) => (typeof s === "string" ? s : String(s?.error || s?.stage_key || "")))
      .filter(Boolean)
      .join("\n"),
    tone: (Number(e.pct) || 0) > 30 ? "err" : (Number(e.pct) || 0) > 10 ? "warn" : "accent",
  }));
}

export function ErrorSection({ d }: { d: OfficeData }) {
  const { t } = useI18n();
  const rows = useMemo(() => errorRows(d.errorsQ.data?.by_class), [d.errorsQ.data]);
  const total = d.errorsQ.data?.total ?? rows.reduce((a, r) => a + r.value, 0);

  return (
    <Card
      title={t("office.errors.title", "오류 분류")}
      actions={total ? <span className="text-xs text-muted">{t("office.errors.total", "총 {n}건").replace("{n}", String(total))}</span> : undefined}
    >
      {d.errorsQ.isLoading && <Spinner />}
      {d.errorsQ.error && (
        <div className="text-xs text-muted">
          {isForbidden(d.errorsQ.error)
            ? t("office.forbidden", "권한 부족 — 이 항목은 lead 역할에서 확인할 수 있습니다")
            : t("office.errors.empty", "오류 데이터가 없습니다")}
        </div>
      )}
      {d.errorsQ.data && !rows.length && <Empty>{t("office.errors.empty", "오류 데이터가 없습니다")}</Empty>}
      <ShareBars rows={rows} empty={t("office.errors.empty", "오류 데이터가 없습니다")} />
    </Card>
  );
}

// ---------- 활성 세션 ----------

const SESSION_STATUS_ORDER: Record<string, number> = { decision: 0, working: 1, ask: 2, blocked: 3, rest: 4 };

export function SessionsSection({ d }: { d: OfficeData }) {
  const { t } = useI18n();
  const rows = useMemo(() => {
    const list = [...d.sessionList];
    list.sort((a, b) =>
      (SESSION_STATUS_ORDER[String(a.status || "")] ?? 5) - (SESSION_STATUS_ORDER[String(b.status || "")] ?? 5),
    );
    return list.slice(0, 30);
  }, [d.sessionList]);

  return (
    <Card title={t("office.sessions.title", "활성 에이전트 세션")}>
      {d.sessionsQ.isLoading && <Spinner />}
      {d.sessionsQ.error && (
        <ErrorBox message={d.sessionsQ.error instanceof Error ? d.sessionsQ.error.message : String(d.sessionsQ.error)} onRetry={() => d.sessionsQ.refetch()} />
      )}
      {d.sessionsQ.data && !rows.length && <Empty>{t("office.sessions.empty", "활성 세션이 없습니다")}</Empty>}
      {rows.length > 0 && (
        <div className="max-h-96 overflow-y-auto">
          <table className="w-full text-xs">
            <thead className="sticky top-0 bg-surface">
              <tr className="text-left text-muted">
                <th className="pb-1.5 pr-2">{t("office.col.session", "세션")}</th>
                <th className="pb-1.5 pr-2">{t("office.col.doing", "작업")}</th>
                <th className="pb-1.5 pr-2">{t("office.col.lastEvent", "마지막 이벤트")}</th>
                <th className="pb-1.5 pr-2 text-right">{t("office.col.cost", "비용")}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((s: SessionRow, i) => (
                <tr key={`${s.tool || ""}:${s.session_key || i}`} className="border-t border-border">
                  <td className="py-1.5 pr-2">
                    <div className="flex items-center gap-1.5">
                      <HealthBadge health={s.health} t={t} />
                      <span className="font-medium text-ink">{TOOL_LABEL[String(s.tool || "")] || String(s.tool || "-")}</span>
                      {s.project && <span className="text-muted">· {s.project}</span>}
                    </div>
                    {(s.title || s.session_key) && (
                      <div className="mt-0.5 max-w-56 truncate font-mono text-[10px] text-muted" title={s.title || s.session_key}>
                        {s.title || s.session_key}
                      </div>
                    )}
                  </td>
                  <td className="max-w-64 truncate py-1.5 pr-2 text-muted" title={s.doing || s.ask || ""}>
                    {s.doing || s.ask || "-"}
                  </td>
                  <td className="py-1.5 pr-2 whitespace-nowrap text-muted">
                    {fmtRel(s.last_event_at || s.updated_at)}
                  </td>
                  <td className="py-1.5 text-right whitespace-nowrap">
                    {s.est_cost_usd != null ? usd(s.est_cost_usd) : <span className="text-muted">-</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}
