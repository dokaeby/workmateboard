/** evals — 점수 배지 + 실행 이력(GET /evals/{id}/runs, 폴링 포함). */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { apiGet } from "../../shared/api/client";
import { useI18n, type TFn } from "../../shared/i18n";
import { Badge, Button, Empty, Spinner, ErrorBox } from "../../shared/ui";
import { fmtTs } from "../../shared/lib/format";
import {
  scoreTone, checkLabel,
  type EvalCheck, type EvalRunsResponse, type EvalRun,
} from "./types";

export function ScoreBadge({ score }: { score?: number | null }) {
  const { t } = useI18n();
  if (score == null || !isFinite(Number(score))) {
    return <Badge tone="muted">{t("evals.noScore", "점수 없음")}</Badge>;
  }
  return <Badge tone={scoreTone(score)}>{Math.round(Number(score))}</Badge>;
}

const RUN_STATUS_TONE: Record<string, "muted" | "ok" | "warn" | "err" | "accent"> = {
  pending: "warn",
  scored: "ok",
  error: "err",
};

function runStatusLabel(t: TFn, st?: string): string {
  switch (st) {
    case "pending": return t("evals.runStatusPending", "대기");
    case "scored": return t("evals.runStatusScored", "채점 완료");
    case "error": return t("evals.runStatusError", "오류");
    default: return st || "?";
  }
}

export function EvalRuns({ evalId }: { evalId: string }) {
  const { t } = useI18n();
  const qc = useQueryClient();
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["eval-runs", evalId],
    queryFn: () => apiGet<EvalRunsResponse>(`/evals/${encodeURIComponent(evalId)}/runs`),
    // 평가 실행은 비동기 — pending이 있으면 10초, 아니면 30초 폴링.
    refetchInterval: (q) =>
      (q.state.data?.items || []).some((r) => r.status === "pending") ? 10000 : 30000,
  });
  const items = data?.items || [];

  return (
    <div>
      <div className="mb-1.5 flex items-center justify-between">
        <span className="text-xs font-medium text-muted">{t("evals.history", "실행 이력")}</span>
        <Button variant="ghost" className="px-2 py-0.5 text-xs" onClick={() => { void refetch(); void qc.invalidateQueries({ queryKey: ["eval-runs", evalId] }); }}>
          {t("common.refresh", "새로고침")}
        </Button>
      </div>
      {isLoading && <Spinner />}
      {error && <ErrorBox message={error instanceof Error ? error.message : String(error)} onRetry={() => refetch()} />}
      {data && !items.length && <Empty>{t("evals.historyEmpty", "아직 실행 이력이 없습니다")}</Empty>}
      <div className="space-y-2">
        {items.map((r: EvalRun) => (
          <div key={r.id} className="rounded-lg border border-border p-2.5">
            <div className="flex flex-wrap items-center gap-2">
              <Badge tone={RUN_STATUS_TONE[r.status || ""] || "muted"}>{runStatusLabel(t, r.status)}</Badge>
              <ScoreBadge score={r.score} />
              <span className="text-xs text-muted">{fmtTs(r.created_at)}</span>
              {r.scored_at && <span className="text-xs text-muted">· {fmtTs(r.scored_at)}</span>}
              <span className="min-w-0 flex-1" />
              {r.run_id && (
                <a
                  href={`/?view=runs&run=${encodeURIComponent(r.run_id)}`}
                  className="text-xs font-medium text-accent hover:underline"
                >
                  {t("evals.viewRun", "실행 보기")}
                </a>
              )}
            </div>
            {r.error && <div className="mt-1.5 text-xs text-err">{r.error}</div>}
            {!!r.results?.length && (
              <div className="mt-1.5 flex flex-wrap gap-1">
                {r.results.map((res, i) => {
                  // check_type 우선, 없으면 원본 check 객체/문자열에서 타입 추출
                  // (레거시 응답 방어).
                  const rawType =
                    res.check_type ??
                    (res.check && typeof res.check === "object"
                      ? String((res.check as EvalCheck).type ?? "")
                      : typeof res.check === "string"
                        ? res.check
                        : "");
                  const label = checkLabel(t, rawType || undefined);
                  const checkJson =
                    res.check != null && typeof res.check === "object"
                      ? JSON.stringify(res.check)
                      : "";
                  const verdict = res.detail || (res.passed ? t("evals.checkPassed", "통과") : t("evals.checkFailed", "실패"));
                  return (
                    <span
                      key={i}
                      title={`${label}: ${verdict}${checkJson ? `\n${checkJson}` : ""}`}
                      className={`inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-xs font-medium ${
                        res.passed ? "bg-ok-soft text-ok" : "bg-err-soft text-err"
                      }`}
                    >
                      {res.passed ? "✓" : "✗"} {label}
                    </span>
                  );
                })}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
