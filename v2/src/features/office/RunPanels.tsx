import { useMemo, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { apiPost, ApiError } from "../../shared/api/client";
import { useI18n } from "../../shared/i18n";
import { Card, Empty, Spinner, ErrorBox, Button, Badge } from "../../shared/ui";
import { fmtTs, fmtElapsed, jsonSummary } from "../../shared/lib/format";
import type { AuditEvent } from "./types";
import {
  STAGE_ORDER,
  STAGE_LABEL,
  latestStageRuns,
  stageTone,
  StatusBadge,
} from "./bits";
import { useRunDetail } from "./hooks";

// ── 파이프라인 스테이지 카드 ──
export function PipelineCard({ runId }: { runId: string | null }) {
  const { t } = useI18n();
  const { data: run, isLoading, error, refetch } = useRunDetail(runId);

  const stages = useMemo(() => latestStageRuns(run?.stages), [run]);
  const orderedKeys = useMemo(() => {
    const keys = STAGE_ORDER.map((s) => s.key);
    for (const k of stages.keys()) if (!keys.includes(k)) keys.push(k);
    return keys;
  }, [stages]);
  const done = orderedKeys.filter((k) => stages.get(k)?.status === "succeeded").length;

  if (!runId) return <Empty>{t("office.pickRun", "실행을 선택하세요")}</Empty>;
  if (isLoading) return <Spinner />;
  if (error) return <ErrorBox message={error instanceof Error ? error.message : String(error)} onRetry={() => refetch()} />;
  if (!run) return <Empty>{t("office.noRun", "실행된 dev_review 워크플로가 없습니다")}</Empty>;

  return (
    <div>
      <div className="mb-2 flex items-center justify-between text-xs text-muted">
        <span>{t("office.pipeline", "단계 진행")}</span>
        <span>{Math.round((done / Math.max(1, orderedKeys.length)) * 100)}%</span>
      </div>
      <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
        {orderedKeys.map((key) => {
          const sr = stages.get(key);
          const status = sr?.status || "pending";
          return (
            <div
              key={key}
              className={`rounded-lg border p-2.5 ${
                status === "running"
                  ? "border-accent bg-accent-soft"
                  : status === "failed"
                    ? "border-err-border bg-err-soft"
                    : "border-border bg-surface-hover"
              }`}
            >
              <div className="flex items-center justify-between gap-1">
                <span className="truncate text-xs font-semibold text-ink">
                  {t(`office.stage.${key}`, STAGE_LABEL[key] || key)}
                </span>
                <Badge tone={stageTone(status)}>{status}</Badge>
              </div>
              <div className="mt-1 flex flex-wrap gap-x-2 text-[11px] text-muted">
                {(sr?.attempt || 0) > 1 && <span>#{sr?.attempt}</span>}
                {sr?.started_at && <span>{fmtTs(sr.started_at)}</span>}
                {(sr?.tokens_in || sr?.tokens_out) ? (
                  <span>
                    {t("office.tokens", "토큰")} {(sr?.tokens_in || 0) + (sr?.tokens_out || 0)}
                  </span>
                ) : null}
              </div>
              {sr?.error && <div className="mt-1 truncate text-[11px] text-err">{sr.error}</div>}
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ── run 상세 + 승인 ──
export function RunDetailPanel({ runId }: { runId: string | null }) {
  const { t } = useI18n();
  const qc = useQueryClient();
  const { data: run } = useRunDetail(runId);
  const [busy, setBusy] = useState(false);

  const decide = async (decision: "approve" | "reject") => {
    if (!runId || busy) return;
    setBusy(true);
    try {
      await apiPost(`/runs/${encodeURIComponent(runId)}/approve`, { decision, comment: "" });
      qc.invalidateQueries({ queryKey: ["office-run", runId] });
      qc.invalidateQueries({ queryKey: ["office-runs"] });
    } catch (e) {
      alert(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  if (!run) {
    return <Empty>{t("office.pickRun", "실행을 선택하세요")}</Empty>;
  }

  const approvals = run.approvals || [];
  const inputTask = typeof run.input?.task === "string" ? run.input.task : jsonSummary(run.input, 200);

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <StatusBadge status={run.status} />
        <span className="text-sm font-medium text-ink">{run.workflow || "dev_review"}</span>
        {run.project && <Badge>{run.project}</Badge>}
        {run.status === "waiting_approval" && <Badge tone="warn">{t("office.waiting", "승인 대기")}</Badge>}
      </div>
      <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs text-muted">
        <dt className="truncate font-mono">{run.id}</dt>
        <dt />
        <dt>{t("office.elapsed", "경과")}</dt>
        <dd>{fmtElapsed(run.created_at, run.updated_at)}</dd>
        <dt>{t("office.input", "입력")}</dt>
        <dd className="truncate">{inputTask || "-"}</dd>
        {run.error && (
          <>
            <dt className="text-err">{t("office.error", "오류")}</dt>
            <dd className="text-err">{run.error}</dd>
          </>
        )}
      </dl>
      {run.status === "waiting_approval" && (
        <div className="flex gap-2">
          <Button className="px-3 py-1 text-xs" disabled={busy} onClick={() => decide("approve")}>
            {t("office.approve", "승인")}
          </Button>
          <Button variant="secondary" className="px-3 py-1 text-xs" disabled={busy} onClick={() => decide("reject")}>
            {t("office.reject", "거절")}
          </Button>
        </div>
      )}
      {approvals.length > 0 && (
        <div>
          <h3 className="mb-1 text-xs font-semibold uppercase text-muted">{t("office.approvals", "승인 기록")}</h3>
          {approvals.map((a, i) => (
            <div key={i} className="text-xs text-muted">
              <Badge tone={a.decision === "approve" ? "ok" : "err"}>{a.decision}</Badge>{" "}
              {a.stage_key} · {a.decided_by} · {fmtTs(a.decided_at)}
              {a.comment ? ` — ${a.comment}` : ""}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ── 이벤트 로그 (run.audit 시간순) ──
export function AuditFeed({ runId }: { runId: string | null }) {
  const { t } = useI18n();
  const { data: run } = useRunDetail(runId);
  const audit = useMemo(
    () => ((run?.audit || []) as AuditEvent[]).slice().sort((a, b) =>
      String(a.created_at || "").localeCompare(String(b.created_at || ""))),
    [run],
  );

  if (!runId || !run) return null;
  return (
    <Card title={t("office.auditLog", "이벤트 로그")} className="max-h-72 overflow-y-auto">
      {!audit.length && <Empty>{t("office.noAudit", "이벤트 없음")}</Empty>}
      <div className="space-y-1">
        {audit.map((a, i) => (
          <div key={a.id || i} className="flex flex-wrap items-baseline gap-x-2 text-xs">
            <span className="shrink-0 text-muted">{fmtTs(a.created_at)}</span>
            <span className="font-medium text-ink">{a.actor}</span>
            <span className="text-accent">{a.action}</span>
            {a.object_ref && <span className="truncate text-muted">{a.object_ref}</span>}
            {a.detail && Object.keys(a.detail).length > 0 && (
              <span className="truncate text-muted">{jsonSummary(a.detail, 160)}</span>
            )}
          </div>
        ))}
      </div>
    </Card>
  );
}
