/** 실행 이력 — run 목록/상세/승인/신규 실행. 서버 상태는 React Query, UI 상태는 로컬 state. */
import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { apiGet, apiPost, ApiError } from "../../shared/api/client";
import type { StageRun, RunStats } from "../../shared/api/types";
import { useI18n, type TFn } from "../../shared/i18n";
import "./i18n";
import { PageHead, Card, Empty, Spinner, ErrorBox, Button, Badge } from "../../shared/ui";
import { fmtTs, jsonSummary } from "../../shared/lib/format";
import { useAppStore } from "../../shared/store/appStore";
import { useRunsList, useRunDetail, useRunDiff, useRunAudit } from "./hooks";



const STATUS_TONE: Record<string, "ok" | "warn" | "err" | "accent" | "muted"> = {
  succeeded: "ok", completed: "ok", running: "accent", queued: "muted",
  waiting_approval: "warn", waiting_review: "warn", failed: "err", rejected: "err",
};

function StatusBadge({ status }: { status: string }) {
  return <Badge tone={STATUS_TONE[status] ?? "muted"}>{status}</Badge>;
}

function fmtSecs(sec: number | null | undefined, t: TFn): string {
  const s = Number(sec);
  if (!isFinite(s) || s < 0) return "-";
  if (s < 60) return t("runs.sec", "{n}초").replace("{n}", String(Math.round(s)));
  if (s < 3600) {
    return t("runs.min", "{m}분 {s}초")
      .replace("{m}", String(Math.floor(s / 60)))
      .replace("{s}", String(Math.round(s % 60)));
  }
  return t("runs.hour", "{h}시간 {m}분")
    .replace("{h}", String(Math.floor(s / 3600)))
    .replace("{m}", String(Math.floor((s % 3600) / 60)));
}

function diffCap200(diff: string | undefined, t: TFn): string {
  const s = String(diff ?? "");
  const lines = s.split("\n");
  return lines.length > 200
    ? lines.slice(0, 200).join("\n") + "\n" + t("runs.diffTruncated", "… (이하 생략)")
    : s;
}

// ── 실행 통계 (GET /runs/stats?days=30&workflow=) — details 열 때만 로드 ──
function RunStatsCard() {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const [wf, setWf] = useState("");
  const { data: workflows } = useQuery({
    queryKey: ["workflows"],
    queryFn: () => apiGet<{ key: string }[]>("/workflows"),
    staleTime: 300000,
    enabled: open,
  });
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["runs-stats", wf],
    queryFn: () => apiGet<RunStats>(
      `/runs/stats?days=30${wf ? `&workflow=${encodeURIComponent(wf)}` : ""}`),
    enabled: open,
    staleTime: 60000,
  });

  const chips: [string, number | undefined, "muted" | "ok" | "warn" | "err" | "accent"][] = data ? [
    [t("runs.statTotal", "총 실행"), data.runs, "muted"],
    [t("runs.statOk", "성공"), data.succeeded, "ok"],
    [t("runs.statFail", "실패"), data.failed, "err"],
    [t("runs.statRun", "진행중"), data.running, "accent"],
    [t("runs.statWait", "승인대기"), data.waiting_approval, "warn"],
  ] : [];

  const daily = data?.daily || [];
  const dMax = Math.max(1, ...daily.map((x) => x.runs || 0));
  const sf = data?.stage_failures || {};
  const sfMax = Math.max(1, ...Object.values(sf));

  return (
    <details
      className="rounded-xl border border-border bg-surface p-4 shadow-sm"
      onToggle={(e) => setOpen(e.currentTarget.open)}
    >
      <summary className="cursor-pointer text-base font-semibold text-ink">
        {t("runs.stats", "실행 통계")}
        <span className="ml-2 text-xs font-normal text-muted">({t("runs.statsDays", "최근 30일")})</span>
      </summary>
      <div className="mt-3 space-y-3">
        <div className="flex items-center gap-2">
          <select
            value={wf}
            onChange={(e) => setWf(e.target.value)}
            className="rounded-md border border-border bg-surface px-2 py-1 text-xs"
          >
            <option value="">{t("runs.allWorkflows", "전체 워크플로")}</option>
            {(workflows || []).map((w) => (
              <option key={w.key} value={w.key}>{w.key}</option>
            ))}
          </select>
          <Button variant="ghost" className="px-2 py-0.5 text-xs" onClick={() => refetch()}>
            {t("common.reload", "새로고침")}
          </Button>
        </div>
        {isLoading && <Spinner />}
        {error && <ErrorBox message={error instanceof Error ? error.message : String(error)} onRetry={() => refetch()} />}
        {data && (
          <>
            <div className="flex flex-wrap gap-1.5">
              {chips.map(([k, v, tone]) => (
                <Badge key={k} tone={tone}>{k} <b>{v ?? 0}</b></Badge>
              ))}
            </div>
            <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs text-muted">
              <dt>{t("runs.avgDuration", "평균 소요시간")}</dt>
              <dd className="text-ink">{fmtSecs(data.avg_duration_s, t)}</dd>
              <dt>{t("runs.totalTokens", "총 토큰")}</dt>
              <dd className="text-ink">
                in {(data.total_tokens_in || 0).toLocaleString()} / out {(data.total_tokens_out || 0).toLocaleString()}
              </dd>
            </dl>
            {daily.length > 0 && (
              <div>
                <h3 className="mb-1 text-xs font-semibold uppercase text-muted">{t("runs.dailyRuns", "일별 실행")}</h3>
                <div className="flex h-16 items-end gap-[2px]">
                  {daily.map((x) => (
                    <div
                      key={x.date}
                      className="flex-1 rounded-t bg-accent/70"
                      style={{ height: `${Math.max(4, Math.round((x.runs || 0) / dMax * 100))}%` }}
                      title={t("runs.dailyTitle", "{date} · {runs}회 · 토큰 {tokens}")
                        .replace("{date}", x.date)
                        .replace("{runs}", String(x.runs ?? 0))
                        .replace("{tokens}", (x.tokens || 0).toLocaleString())}
                    />
                  ))}
                </div>
                <div className="mt-0.5 flex justify-between text-[10px] text-muted">
                  <span>{daily[0]?.date}</span><span>{daily[daily.length - 1]?.date}</span>
                </div>
              </div>
            )}
            <div>
              <h3 className="mb-1 text-xs font-semibold uppercase text-muted">{t("runs.stageFailures", "단계별 실패")}</h3>
              {Object.keys(sf).length ? Object.entries(sf).map(([k, c]) => (
                <div key={k} className="flex items-center gap-2 py-0.5 text-xs">
                  <span className="w-28 truncate text-muted">{k}</span>
                  <span className="h-2 flex-1 overflow-hidden rounded bg-surface-hover">
                    <span
                      className="block h-full rounded bg-err"
                      style={{ width: `${Math.max(4, Math.round(c / sfMax * 100))}%` }}
                    />
                  </span>
                  <b className="text-ink">{c}</b>
                </div>
              )) : <div className="text-xs text-muted">{t("runs.noStageFailures", "단계 실패 기록 없음")}</div>}
            </div>
          </>
        )}
      </div>
    </details>
  );
}

// ── 승인 대상 diff 미리보기 (GET /runs/{id}/diff) ──
function RunDiffSection({ runId }: { runId: string }) {
  const { t } = useI18n();
  const { data, isLoading, error } = useRunDiff(runId);
  const files = data?.files || [];

  return (
    <div>
      <h3 className="mb-1 text-xs font-semibold uppercase text-muted">
        {t("runs.diff", "변경 사항 (diff)")}
      </h3>
      {isLoading && <div className="text-xs text-muted">{t("common.loading", "로딩…")}</div>}
      {error && <div className="text-xs text-muted">{t("runs.diffFail", "diff를 불러올 수 없습니다")} — {error instanceof Error ? error.message : String(error)}</div>}
      {data && !files.length && <div className="text-xs text-muted">{t("runs.noDiff", "변경된 파일이 없습니다")}</div>}
      {data?.summary && (
        <div className="mb-1 text-xs text-muted">{jsonSummary(data.summary, 400)}</div>
      )}
      <div className="space-y-1">
        {files.map((f, i) => (
          <details key={f.path || i} className="rounded-md border border-border">
            <summary className="flex cursor-pointer items-center gap-2 px-2 py-1.5 text-xs">
              <span className="min-w-0 flex-1 truncate font-mono text-ink">{f.path}</span>
              <Badge tone={f.status === "added" ? "ok" : f.status === "deleted" ? "err" : "muted"}>
                {f.status || "modified"}
              </Badge>
              <span className="shrink-0 text-muted">+{f.additions || 0} / −{f.deletions || 0}</span>
            </summary>
            <pre className="max-h-72 overflow-auto border-t border-border bg-surface-hover p-2 text-[11px] leading-snug">
              {diffCap200(f.diff, t)}
            </pre>
          </details>
        ))}
      </div>
    </div>
  );
}

// ── 감사 로그 (GET /runs/{id}/audit, 3초 폴링, 최신 위) ──
function RunAuditSection({ runId }: { runId: string }) {
  const { t } = useI18n();
  const { data, isLoading, error } = useRunAudit(runId);
  const events = data || [];

  return (
    <div>
      <h3 className="mb-1 text-xs font-semibold uppercase text-muted">
        {t("runs.audit", "감사 로그")}
      </h3>
      {isLoading && <div className="text-xs text-muted">{t("common.loading", "로딩…")}</div>}
      {error && <div className="text-xs text-muted">{t("runs.auditFail", "감사 로그를 불러올 수 없습니다")} — {error instanceof Error ? error.message : String(error)}</div>}
      {data && !events.length && <div className="text-xs text-muted">{t("runs.noAudit", "감사 기록 없음")}</div>}
      <div className="max-h-64 space-y-1 overflow-y-auto">
        {events.map((a, i) => {
          const raw = a.detail && typeof a.detail === "object"
            ? JSON.stringify(a.detail)
            : String(a.detail || "");
          const det = raw ? raw.slice(0, 160) : "";
          return (
            <div key={a.id || i} className="flex flex-wrap items-baseline gap-x-2 text-xs">
              <span className="shrink-0 text-muted">{fmtTs(a.created_at)}</span>
              <span className="font-medium text-ink">{a.actor || "-"}</span>
              <span className="text-accent">{a.action || "-"}</span>
              {a.object_ref && <span className="truncate text-muted">{a.object_ref}</span>}
              {det && <span className="truncate text-muted">{det}</span>}
            </div>
          );
        })}
      </div>
    </div>
  );
}

function RunDetail({ runId, onClose }: { runId: string; onClose: () => void }) {
  const { t } = useI18n();
  const { data: run, isLoading, error, refetch } = useRunDetail(runId);
  const qc = useQueryClient();
  const [busy, setBusy] = useState(false);

  const decide = async (decision: "approve" | "reject") => {
    setBusy(true);
    try {
      await apiPost(`/runs/${encodeURIComponent(runId)}/approve`, { decision, comment: "" });
      qc.invalidateQueries({ queryKey: ["run", runId] });
      qc.invalidateQueries({ queryKey: ["approvals"] });
      refetch();
    } catch (e) {
      alert(e instanceof ApiError ? e.message : String(e));
    } finally { setBusy(false); }
  };

  if (isLoading) return <Spinner />;
  if (error) return <ErrorBox message={error instanceof Error ? error.message : String(error)} onRetry={() => refetch()} />;
  if (!run) return <Empty>{t("runs.notFound", "run을 찾을 수 없습니다")}</Empty>;

  const stages = (run as unknown as { stages?: StageRun[] }).stages || [];
  const approvals = run.approvals || [];

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <StatusBadge status={run.status} />
          <span className="text-sm font-medium">{run.workflow || run.workflow_key}</span>
          {((run as unknown as { coalesced_count?: number }).coalesced_count ?? 0) > 0 && (
            <Badge tone="muted">
              {t("runs.coalesced", "{n}건 병합").replace("{n}", String((run as unknown as { coalesced_count?: number }).coalesced_count))}
            </Badge>
          )}
        </div>
        <Button variant="ghost" className="px-2 py-1 text-xs" onClick={onClose}>✕</Button>
      </div>
      <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs text-muted">
        <dt>ID</dt><dd className="truncate font-mono">{run.id}</dd>
        <dt>{t("runs.created", "생성")}</dt><dd>{fmtTs(run.created_at)}</dd>
        <dt>{t("runs.updated", "갱신")}</dt><dd>{fmtTs(run.updated_at)}</dd>
        {run.project_id && <><dt>{t("runs.project", "프로젝트")}</dt><dd className="truncate font-mono">{run.project_id}</dd></>}
        {run.agent_id && <><dt>{t("runs.agent", "에이전트")}</dt><dd className="truncate font-mono">{run.agent_id}</dd></>}
        {run.goal_id && <><dt>{t("runs.goal", "목표")}</dt><dd className="truncate font-mono">{run.goal_id}</dd></>}
        {run.error && <><dt>{t("runs.error", "오류")}</dt><dd className="text-err">{run.error}</dd></>}
      </dl>
      {run.status === "waiting_approval" && (
        <div className="flex gap-2">
          <Button className="px-3 py-1 text-xs" disabled={busy} onClick={() => decide("approve")}>
            {t("appr.approve", "승인")}
          </Button>
          <Button variant="secondary" className="px-3 py-1 text-xs" disabled={busy} onClick={() => decide("reject")}>
            {t("appr.reject", "거절")}
          </Button>
        </div>
      )}
      {stages.length > 0 && (
        <div>
          <h3 className="mb-1 text-xs font-semibold uppercase text-muted">{t("runs.stages", "단계")}</h3>
          <div className="space-y-1">
            {stages.map((s, i) => (
              <div key={s.id || i} className="flex items-center gap-2 rounded-md bg-surface-hover px-2 py-1.5 text-xs">
                <StatusBadge status={s.status || ""} />
                <span className="font-medium">{s.stage_key || s.stage}</span>
                {s.attempt != null && s.attempt > 1 && <span className="text-muted">#{s.attempt}</span>}
                {s.error && <span className="truncate text-err">{s.error}</span>}
              </div>
            ))}
          </div>
        </div>
      )}
      {approvals.length > 0 && (
        <div>
          <h3 className="mb-1 text-xs font-semibold uppercase text-muted">{t("runs.approvals", "승인 기록")}</h3>
          {approvals.map((a, i) => (
            <div key={i} className="text-xs text-muted">
              {a.decision} · {a.stage_key} · {a.decided_by} · {fmtTs(a.decided_at)}
              {a.comment ? ` — ${a.comment}` : ""}
            </div>
          ))}
        </div>
      )}
      <RunDiffSection runId={runId} />
      <RunAuditSection runId={runId} />
      <details className="text-xs">
        <summary className="cursor-pointer text-muted">{t("runs.rawOutput", "원시 출력")}</summary>
        <pre className="mt-1 max-h-64 overflow-auto rounded-md bg-surface-hover p-2">{jsonSummary(run.output ?? run.input, 2000)}</pre>
      </details>
    </div>
  );
}

function NewRunForm({ onDone }: { onDone: () => void }) {
  const { t } = useI18n();
  const qc = useQueryClient();
  const [workflow, setWorkflow] = useState("dev_review");
  const [task, setTask] = useState("");
  const [busy, setBusy] = useState(false);
  const { data: workflows } = useQuery({
    queryKey: ["workflows"],
    queryFn: () => apiGet<{ key: string; description?: string }[]>("/workflows"),
    staleTime: 300000,
  });
  const submit = async () => {
    if (!task.trim()) return;
    setBusy(true);
    try {
      await apiPost("/runs", { workflow, task: task.trim() });
      setTask("");
      qc.invalidateQueries({ queryKey: ["runs"] });
      onDone();
    } catch (e) {
      alert(e instanceof ApiError ? e.message : String(e));
    } finally { setBusy(false); }
  };
  return (
    <div className="mb-3 flex flex-col gap-2 rounded-lg border border-border bg-surface-hover p-3">
      <select value={workflow} onChange={(e) => setWorkflow(e.target.value)} className="rounded-md border border-border bg-surface px-2 py-1.5 text-sm">
        {(workflows || []).map((w) => <option key={w.key} value={w.key}>{w.key}</option>)}
        {!workflows?.length && <option value="dev_review">dev_review</option>}
      </select>
      <textarea
        value={task} onChange={(e) => setTask(e.target.value)} rows={3}
        placeholder={t("runs.taskPlaceholder", "실행할 작업을 입력하세요")}
        className="rounded-md border border-border bg-surface px-2 py-1.5 text-sm"
      />
      <div className="flex justify-end gap-2">
        <Button variant="secondary" className="px-3 py-1 text-xs" onClick={onDone}>{t("common.cancel", "취소")}</Button>
        <Button className="px-3 py-1 text-xs" disabled={busy || !task.trim()} onClick={submit}>
          {t("runs.start", "실행")}
        </Button>
      </div>
    </div>
  );
}

export default function RunsPage() {
  const { t } = useI18n();
  // ?run=<id> 딥링크(예: evals 뷰의 실행 링크) — 마운트 시 1회만 읽는다.
  const [selected, setSelected] = useState<string | null>(
    () => new URLSearchParams(window.location.search).get("run"),
  );
  const [showNew, setShowNew] = useState(false);
  const { data, isLoading, error, refetch } = useRunsList();
  const role = useAppStore((s) => s.role);

  return (
    <div className="mx-auto max-w-4xl p-4">
      <PageHead title={t("runs.title", "실행 이력")} desc={t("runs.desc", "워크플로 실행 기록 — 10초마다 자동 갱신")}>
        {roleAllows2(role, "lead") && (
          <Button className="text-xs" onClick={() => setShowNew((v) => !v)}>
            {showNew ? t("common.close", "닫기") : "+ " + t("runs.new", "새 실행")}
          </Button>
        )}
      </PageHead>
      {showNew && <NewRunForm onDone={() => setShowNew(false)} />}
      <RunStatsCard />
      <div className="mt-3 grid gap-3 lg:grid-cols-[1fr_360px]">
        <Card>
          {isLoading && <Spinner />}
          {error && <ErrorBox message={error instanceof Error ? error.message : String(error)} onRetry={() => refetch()} />}
          {data && !data.length && <Empty>{t("runs.empty", "실행 기록이 없습니다")}</Empty>}
          {data?.map((r) => (
            <button
              key={r.id}
              onClick={() => setSelected(r.id === selected ? null : r.id)}
              className={`flex w-full items-center gap-3 border-b border-border py-2.5 text-left last:border-0 hover:bg-surface-hover ${selected === r.id ? "bg-accent-soft" : ""}`}
            >
              <StatusBadge status={r.status} />
              <div className="min-w-0 flex-1">
                <div className="truncate text-sm font-medium text-ink">{r.workflow}</div>
                <div className="text-xs text-muted">{r.id.slice(0, 8)} · {fmtTs(r.created_at)}</div>
              </div>
              {r.project && <Badge>{r.project}</Badge>}
              {(r.coalesced_count ?? 0) > 0 && (
                <Badge tone="muted">{t("runs.coalesced", "{n}건 병합").replace("{n}", String(r.coalesced_count))}</Badge>
              )}
            </button>
          ))}
        </Card>
        {selected && (
          <Card title={t("runs.detail", "실행 상세")}>
            <RunDetail runId={selected} onClose={() => setSelected(null)} />
          </Card>
        )}
      </div>
    </div>
  );
}

function roleAllows2(role: string | null, min: "lead" | "admin") {
  const rank = { member: 0, lead: 1, admin: 2 } as Record<string, number>;
  return (rank[role ?? "member"] ?? 0) >= rank[min];
}
