/** office 외부 텔레메트리 섹션 — spec 009 OTLP metrics/logs 조회 (lead+ 전용).
 *  GET /observability/external-metrics, /observability/external-logs.
 *  member 역할에서는 쿼리를 발사하지 않고 권한 부족 안내만 표시한다. */
import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { apiGet, ApiError } from "../../shared/api/client";
import { useI18n } from "../../shared/i18n";
import { Badge, Button, Card, Empty, Spinner, ErrorBox } from "../../shared/ui";
import { fmtTs, jsonSummary } from "../../shared/lib/format";
import { useAppStore, roleAllows } from "../../shared/store/appStore";
import type {
  ExternalLog, ExternalLogsResponse, ExternalMetric, ExternalMetricsResponse,
} from "./types";

const DAYS = ["7", "30", "90"];
const BODY_MAX = 120;

function isForbidden(e: unknown): boolean {
  return e instanceof ApiError && e.status === 403;
}

function useDebounced<T>(value: T, ms = 400): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const id = window.setTimeout(() => setV(value), ms);
    return () => window.clearTimeout(id);
  }, [value, ms]);
  return v;
}

function errMsg(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

function fmtMetricValue(v: ExternalMetric["value"]): string {
  if (v == null || v === "") return "-";
  const n = Number(v);
  if (!isFinite(n)) return String(v);
  return n.toLocaleString("en-US", { maximumFractionDigits: 6 });
}

const METRIC_TYPE_TONE: Record<string, "accent" | "ok" | "muted"> = {
  gauge: "accent", sum: "ok", histogram: "muted",
};

// OTLP severityNumber 범위 → 라벨 (1-4 trace, 5-8 debug, 9-12 info, 13-16 warn, 17-20 error, 21-24 fatal)
function severityOf(log: ExternalLog): { label: string; tone: "err" | "warn" | "accent" | "muted" } {
  const text = String(log.severity_text || log.severity || "").toUpperCase();
  const num = Number(log.severity_number);
  let label = text;
  let tier = "";
  if (num >= 21) { label = label || "FATAL"; tier = "fatal"; }
  else if (num >= 17) { label = label || "ERROR"; tier = "error"; }
  else if (num >= 13) { label = label || "WARN"; tier = "warn"; }
  else if (num >= 9) { label = label || "INFO"; tier = "info"; }
  else if (num >= 5) { label = label || "DEBUG"; tier = "debug"; }
  else if (num >= 1) { label = label || "TRACE"; tier = "debug"; }
  if (!label) label = "-";
  const tone =
    tier === "fatal" || tier === "error" || text.includes("ERROR") || text.includes("FATAL") ? "err"
    : tier === "warn" || text.includes("WARN") ? "warn"
    : tier === "info" ? "accent"
    : "muted";
  return { label, tone };
}

function truncate(s: string, n = BODY_MAX): string {
  return s.length > n ? s.slice(0, n) + "…" : s;
}

function MetricTable({ items, isLoading, isFetching, error, onRetry }: {
  items: ExternalMetric[] | undefined; isLoading: boolean; isFetching: boolean; error: unknown; onRetry: () => void;
}) {
  const { t } = useI18n();
  if (isLoading || (isFetching && !items?.length)) return <Spinner />;
  if (error) {
    return isForbidden(error)
      ? <Empty>{t("office.forbidden", "권한 부족 — 이 항목은 lead 역할에서 확인할 수 있습니다")}</Empty>
      : <ErrorBox message={errMsg(error)} onRetry={onRetry} />;
  }
  if (!items?.length) return <Empty>{t("office.extTelemetry.emptyMetrics", "수집된 외부 메트릭이 없습니다")}</Empty>;
  return (
    <div className="max-h-[28rem] overflow-y-auto">
      <table className="w-full text-xs">
        <thead className="sticky top-0 bg-surface">
          <tr className="text-left text-muted">
            <th className="pb-1.5 pr-2">{t("office.extTelemetry.col.metric", "지표")}</th>
            <th className="pb-1.5 pr-2">{t("office.extTelemetry.col.type", "유형")}</th>
            <th className="pb-1.5 pr-2 text-right">{t("office.extTelemetry.col.value", "값")}</th>
            <th className="pb-1.5 whitespace-nowrap">{t("office.extTelemetry.col.time", "시각")}</th>
          </tr>
        </thead>
        <tbody>
          {items.map((m, i) => (
            <tr key={m.id ?? i} className="border-t border-border">
              <td className="max-w-64 truncate py-1.5 pr-2 font-mono text-ink"
                  title={`${m.metric_name || "-"}${m.attributes ? `\n${jsonSummary(m.attributes)}` : ""}`}>
                {m.metric_name || "-"}
              </td>
              <td className="py-1.5 pr-2">
                {m.metric_type
                  ? <Badge tone={METRIC_TYPE_TONE[String(m.metric_type)] || "muted"}>{String(m.metric_type)}</Badge>
                  : <span className="text-muted">-</span>}
              </td>
              <td className="py-1.5 pr-2 text-right whitespace-nowrap">
                {fmtMetricValue(m.value)}
                {m.unit && <span className="ml-1 text-muted">{m.unit}</span>}
              </td>
              <td className="py-1.5 whitespace-nowrap text-muted">{fmtTs(m.timestamp)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function LogTable({ items, isLoading, isFetching, error, onRetry }: {
  items: ExternalLog[] | undefined; isLoading: boolean; isFetching: boolean; error: unknown; onRetry: () => void;
}) {
  const { t } = useI18n();
  if (isLoading || (isFetching && !items?.length)) return <Spinner />;
  if (error) {
    return isForbidden(error)
      ? <Empty>{t("office.forbidden", "권한 부족 — 이 항목은 lead 역할에서 확인할 수 있습니다")}</Empty>
      : <ErrorBox message={errMsg(error)} onRetry={onRetry} />;
  }
  if (!items?.length) return <Empty>{t("office.extTelemetry.emptyLogs", "수집된 외부 로그가 없습니다")}</Empty>;
  return (
    <div className="max-h-[28rem] overflow-y-auto">
      <table className="w-full text-xs">
        <thead className="sticky top-0 bg-surface">
          <tr className="text-left text-muted">
            <th className="pb-1.5 pr-2">{t("office.extTelemetry.col.time", "시각")}</th>
            <th className="pb-1.5 pr-2">{t("office.extTelemetry.col.severity", "심각도")}</th>
            <th className="pb-1.5 pr-2">{t("office.extTelemetry.col.body", "본문")}</th>
            <th className="pb-1.5">{t("office.extTelemetry.col.trace", "트레이스")}</th>
          </tr>
        </thead>
        <tbody>
          {items.map((l, i) => {
            const sev = severityOf(l);
            const body = String(l.body ?? "-");
            const trace = String(l.trace_id || "");
            return (
              <tr key={l.id ?? i} className="border-t border-border align-top">
                <td className="py-1.5 pr-2 whitespace-nowrap text-muted">{fmtTs(l.timestamp)}</td>
                <td className="py-1.5 pr-2">
                  <Badge tone={sev.tone}>{sev.label}</Badge>
                </td>
                <td className="max-w-md py-1.5 pr-2 text-ink" title={body.length > BODY_MAX ? body : undefined}>
                  {truncate(body)}
                </td>
                <td className="py-1.5 font-mono text-[10px] text-muted" title={trace || undefined}>
                  {trace ? trace.slice(0, 12) : "-"}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export function ExternalTelemetry() {
  const { t } = useI18n();
  const role = useAppStore((s) => s.role);
  const canLead = roleAllows("lead", role);

  const [tab, setTab] = useState<"metrics" | "logs">("metrics");
  const [days, setDays] = useState("7");
  const [metricName, setMetricName] = useState("");
  const [traceId, setTraceId] = useState("");
  const [severity, setSeverity] = useState("");
  const debMetric = useDebounced(metricName.trim());
  const debTrace = useDebounced(traceId.trim());
  const debSeverity = useDebounced(severity);

  const metricsQ = useQuery({
    queryKey: ["observability", "external-metrics", days, debMetric],
    queryFn: () => apiGet<ExternalMetricsResponse>(
      `/observability/external-metrics?days=${encodeURIComponent(days)}&limit=100${debMetric ? `&metric_name=${encodeURIComponent(debMetric)}` : ""}`,
    ),
    enabled: canLead && tab === "metrics",
    staleTime: 30000,
    retry: false,
  });
  const logsQ = useQuery({
    queryKey: ["observability", "external-logs", days, debTrace, debSeverity],
    queryFn: () => apiGet<ExternalLogsResponse>(
      `/observability/external-logs?days=${encodeURIComponent(days)}&limit=100${debTrace ? `&trace_id=${encodeURIComponent(debTrace)}` : ""}${debSeverity ? `&severity=${encodeURIComponent(debSeverity)}` : ""}`,
    ),
    enabled: canLead && tab === "logs",
    staleTime: 30000,
    retry: false,
  });

  const metricItems = useMemo(() => metricsQ.data?.items || [], [metricsQ.data]);
  const logItems = useMemo(() => logsQ.data?.items || [], [logsQ.data]);

  const inputCls = "rounded-md border border-border bg-surface px-2 py-1 text-xs text-ink placeholder:text-muted";

  return (
    <Card
      title={t("office.extTelemetry.title", "외부 텔레메트리")}
      actions={
        <div className="flex flex-wrap items-center gap-2">
          <div className="flex rounded-md border border-border p-0.5">
            {(["metrics", "logs"] as const).map((k) => (
              <Button
                key={k}
                variant={tab === k ? "primary" : "ghost"}
                className="px-2.5 py-0.5 text-xs"
                onClick={() => setTab(k)}
              >
                {t(`office.extTelemetry.tab.${k}`, k === "metrics" ? "메트릭" : "로그")}
              </Button>
            ))}
          </div>
          <select
            value={days}
            onChange={(e) => setDays(e.target.value)}
            aria-label={t("office.extTelemetry.daysAria", "조회 기간")}
            className="rounded-md border border-border bg-surface px-2 py-1 text-xs"
          >
            {DAYS.map((d) => (
              <option key={d} value={d}>{t(`common.last${d}`, `최근 ${d}일`)}</option>
            ))}
          </select>
        </div>
      }
    >
      {!canLead ? (
        <Empty>{t("office.forbidden", "권한 부족 — 이 항목은 lead 역할에서 확인할 수 있습니다")}</Empty>
      ) : (
        <>
          {tab === "metrics" && (
            <>
              <div className="mb-2">
                <input
                  type="search"
                  value={metricName}
                  onChange={(e) => setMetricName(e.target.value)}
                  placeholder={t("office.extTelemetry.filterMetric", "메트릭 이름 필터")}
                  aria-label={t("office.extTelemetry.filterMetric", "메트릭 이름 필터")}
                  className={`${inputCls} w-64 max-w-full`}
                />
              </div>
              <MetricTable
                items={metricItems}
                isLoading={metricsQ.isLoading}
                isFetching={metricsQ.isFetching}
                error={metricsQ.error}
                onRetry={() => void metricsQ.refetch()}
              />
            </>
          )}
          {tab === "logs" && (
            <>
              <div className="mb-2 flex flex-wrap gap-2">
                <input
                  type="search"
                  value={traceId}
                  onChange={(e) => setTraceId(e.target.value)}
                  placeholder={t("office.extTelemetry.filterTrace", "trace_id 필터")}
                  aria-label={t("office.extTelemetry.filterTrace", "trace_id 필터")}
                  className={`${inputCls} w-56 max-w-full font-mono`}
                />
                <select
                  value={severity}
                  onChange={(e) => setSeverity(e.target.value)}
                  aria-label={t("office.extTelemetry.filterSeverity", "심각도")}
                  className={inputCls}
                >
                  <option value="">{t("common.all", "전체")}</option>
                  {["FATAL", "ERROR", "WARN", "INFO", "DEBUG"].map((s) => (
                    <option key={s} value={s}>{s}</option>
                  ))}
                </select>
              </div>
              <LogTable
                items={logItems}
                isLoading={logsQ.isLoading}
                isFetching={logsQ.isFetching}
                error={logsQ.error}
                onRetry={() => void logsQ.refetch()}
              />
            </>
          )}
        </>
      )}
    </Card>
  );
}
