/** 트레이스 — run 선택 → GET /runs/{id}/timeline stage 워터폴 + 순차 재생(Replay).
 *  spec 007 US1/US5. timeline 필드가 일부 없을 수 있어 방어적으로 렌더한다. */
import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { apiGet } from "../../shared/api/client";
import type { RunListItem } from "../../shared/api/types";
import { useI18n } from "../../shared/i18n";
import { PageHead, Card, Empty, Spinner, ErrorBox, Button, Badge } from "../../shared/ui";
import { fmtTs, usd } from "../../shared/lib/format";
import "./i18n";

interface TimelineStage {
  stage_run_id?: string;
  stage_key?: string;
  attempt?: number;
  status?: string;
  model?: string;
  prompt_version?: string;
  tokens_in?: number | null;
  tokens_out?: number | null;
  cost_usd?: number | null;
  started_at?: string;
  finished_at?: string;
  duration_ms?: number | null;
  error?: string;
  error_class?: string;
  input_summary?: string;
  output_summary?: string;
}
interface RunTimeline {
  run_id?: string;
  stages?: TimelineStage[];
}

const REPLAY_STEP_MS = 800;

const STATUS_TONE: Record<string, "ok" | "warn" | "err" | "accent" | "muted"> = {
  succeeded: "ok", completed: "ok", success: "ok",
  running: "accent", queued: "muted", pending: "muted",
  waiting_approval: "warn", waiting_review: "warn",
  failed: "err", error: "err", rejected: "err",
};

function StatusBadge({ status }: { status?: string }) {
  const s = String(status || "");
  return <Badge tone={STATUS_TONE[s] ?? "muted"}>{s || "-"}</Badge>;
}

function fmtMs(ms: number | null | undefined): string {
  const n = Number(ms);
  if (!isFinite(n) || n < 0) return "-";
  if (n < 1000) return `${Math.round(n)}ms`;
  if (n < 60000) return `${(n / 1000).toFixed(1)}s`;
  if (n < 3600000) return `${Math.floor(n / 60000)}m ${Math.round((n % 60000) / 1000)}s`;
  return `${(n / 3600000).toFixed(1)}h`;
}

function fmtDurSec(sec: number | null | undefined): string {
  const s = Number(sec);
  if (!isFinite(s) || s < 0) return "-";
  if (s < 60) return `${Math.round(s)}s`;
  if (s < 3600) return `${(s / 60).toFixed(1)}m`;
  return `${(s / 3600).toFixed(1)}h`;
}

const RUN_FILTERS = [
  { key: "", label: "tr.statusAll" },
  { key: "completed", label: "tr.stCompleted" },
  { key: "failed", label: "tr.stFailed" },
  { key: "running", label: "tr.stRunning" },
  { key: "waiting_approval", label: "tr.stWaiting" },
  { key: "waiting_review", label: "tr.stWaitingReview" },
] as const;

/** 워터폴 바 위치 — started/finished ISO를 전체 span 대비 %로 변환. */
function barGeom(stages: TimelineStage[]) {
  const times = stages
    .map((s) => ({ start: s.started_at ? Date.parse(s.started_at) : NaN, end: s.finished_at ? Date.parse(s.finished_at) : NaN }))
    .filter((x) => isFinite(x.start));
  if (!times.length) return null;
  const t0 = Math.min(...times.map((x) => x.start));
  const t1 = Math.max(...times.map((x) => (isFinite(x.end) && x.end > x.start ? x.end : x.start + (x.end - x.start || 0))));
  const span = Math.max(1, t1 - t0);
  return (s: TimelineStage) => {
    const st = s.started_at ? Date.parse(s.started_at) : NaN;
    const en = s.finished_at ? Date.parse(s.finished_at) : NaN;
    if (!isFinite(st)) return null;
    const left = ((st - t0) / span) * 100;
    const dur = isFinite(en) && en > st ? en - st : (Number(s.duration_ms) || 0);
    const width = Math.max(1.5, (dur / span) * 100);
    return { left: Math.min(98, left), width: Math.min(100 - Math.min(98, left), width) };
  };
}

function StageDetail({ s }: { s: TimelineStage }) {
  const { t } = useI18n();
  const rows: [string, string][] = [
    [t("tr.model", "모델"), s.model || "-"],
    [t("tr.promptVersion", "프롬프트 버전"), s.prompt_version || "-"],
    [t("tr.tokens", "토큰"), `${(s.tokens_in ?? 0).toLocaleString()} in / ${(s.tokens_out ?? 0).toLocaleString()} out`],
    [t("tr.cost", "비용"), s.cost_usd != null ? usd(s.cost_usd) : t("tr.noValue", "-")],
    [t("tr.duration", "소요시간"), s.duration_ms != null ? fmtMs(s.duration_ms) : fmtDurSec(null)],
    [t("tr.started", "시작"), fmtTs(s.started_at)],
    [t("tr.finished", "종료"), fmtTs(s.finished_at)],
    [t("tr.errorClass", "에러 분류"), s.error_class || "-"],
  ];
  return (
    <div className="space-y-2 border-t border-border px-3 py-2 text-xs">
      <dl className="grid grid-cols-2 gap-x-4 gap-y-1 sm:grid-cols-4">
        {rows.map(([k, v]) => (
          <div key={k}>
            <dt className="text-muted">{k}</dt>
            <dd className="truncate font-medium text-ink" title={v}>{v}</dd>
          </div>
        ))}
      </dl>
      {s.error && (
        <div className="rounded-md border border-err-border bg-err-soft p-2 text-err">
          <span className="font-semibold">{t("tr.error", "오류")}:</span> {s.error}
        </div>
      )}
      <div className="grid gap-2 sm:grid-cols-2">
        <div>
          <div className="mb-0.5 font-semibold text-muted">{t("tr.input", "입력")}</div>
          <pre className="max-h-48 overflow-auto rounded-md bg-surface-hover p-2 text-[11px] leading-snug whitespace-pre-wrap">
            {s.input_summary ? String(s.input_summary) : t("tr.noValue", "-")}
          </pre>
        </div>
        <div>
          <div className="mb-0.5 font-semibold text-muted">{t("tr.output", "출력")}</div>
          <pre className="max-h-48 overflow-auto rounded-md bg-surface-hover p-2 text-[11px] leading-snug whitespace-pre-wrap">
            {s.output_summary ? String(s.output_summary) : t("tr.noValue", "-")}
          </pre>
        </div>
      </div>
    </div>
  );
}

function Timeline({ runId }: { runId: string }) {
  const { t } = useI18n();
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["run-timeline", runId],
    queryFn: () => apiGet<RunTimeline>(`/runs/${encodeURIComponent(runId)}/timeline`),
    refetchInterval: 15000,
  });
  const stages = useMemo(() => data?.stages || [], [data]);
  const geom = useMemo(() => barGeom(stages), [stages]);

  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [playing, setPlaying] = useState(false);
  const [playIdx, setPlayIdx] = useState(-1);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  // run 변경 시 재생 상태 리셋
  useEffect(() => {
    setPlaying(false);
    setPlayIdx(-1);
    setExpanded(new Set());
  }, [runId]);

  useEffect(() => {
    if (!playing) {
      if (timer.current) { clearInterval(timer.current); timer.current = null; }
      return;
    }
    timer.current = setInterval(() => {
      setPlayIdx((i) => {
        if (i + 1 >= stages.length) {
          setPlaying(false);
          return stages.length; // 완료 표시
        }
        return i + 1;
      });
    }, REPLAY_STEP_MS);
    return () => { if (timer.current) clearInterval(timer.current); };
  }, [playing, stages.length]);

  const toggle = (i: number) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(i)) next.delete(i); else next.add(i);
      return next;
    });
  };
  const setAll = (open: boolean) =>
    setExpanded(open ? new Set(stages.map((_, i) => i)) : new Set());

  const replayStage = playIdx >= 0 && playIdx < stages.length ? stages[playIdx] : null;
  const totalCost = stages.reduce((a, s) => a + (Number(s.cost_usd) || 0), 0);
  const totalTokens = stages.reduce((a, s) => a + (Number(s.tokens_in) || 0) + (Number(s.tokens_out) || 0), 0);
  const hasCost = stages.some((s) => s.cost_usd != null);

  if (isLoading) return <Spinner />;
  if (error) return <ErrorBox message={error instanceof Error ? error.message : String(error)} onRetry={() => refetch()} />;
  if (!stages.length) return <Empty>{t("tr.noStages", "이 run에 기록된 stage가 없습니다")}</Empty>;

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2 text-xs text-muted">
        <span>{t("tr.stagesCount", "stage {n}개").replace("{n}", String(stages.length))}</span>
        {hasCost && <span>· {t("tr.totalCost", "총 비용")} {usd(totalCost)}</span>}
        <span>· {t("tr.totalTokens", "총 토큰")} {totalTokens.toLocaleString()}</span>
        <span className="flex-1" />
        <Button variant="ghost" className="px-2 py-0.5 text-xs" onClick={() => setAll(expanded.size < stages.length)}>
          {expanded.size < stages.length ? t("tr.expandAll", "모두 펼치기") : t("tr.collapseAll", "모두 접기")}
        </Button>
        {!playing && playIdx < 0 && (
          <Button className="px-2.5 py-0.5 text-xs" onClick={() => { setPlayIdx(-1); setPlaying(true); }}>
            ▶ {t("tr.replay", "재생")}
          </Button>
        )}
        {playing && (
          <Button variant="secondary" className="px-2.5 py-0.5 text-xs" onClick={() => setPlaying(false)}>
            ⏸ {t("tr.replayStop", "정지")}
          </Button>
        )}
        {!playing && playIdx >= stages.length && (
          <Button variant="secondary" className="px-2.5 py-0.5 text-xs" onClick={() => { setPlayIdx(-1); setExpanded(new Set()); }}>
            ↺ {t("tr.replayReset", "처음부터")}
          </Button>
        )}
        {!playing && playIdx >= 0 && playIdx < stages.length && (
          <Button className="px-2.5 py-0.5 text-xs" onClick={() => setPlaying(true)}>
            ▶ {t("tr.replay", "재생")}
          </Button>
        )}
      </div>
      {playing && <div className="text-xs text-accent">{t("tr.replaying", "재생 중")} — {playIdx + 1}/{stages.length}</div>}
      {!playing && playIdx >= stages.length && <div className="text-xs text-ok">{t("tr.replayDone", "재생 완료")}</div>}

      {/* 재생 중 현재 단계 IO 패널 */}
      {replayStage && (
        <Card title={`${t("tr.stageIO", "단계 입출력")} — ${replayStage.stage_key || "?"} (${playIdx + 1}/${stages.length})`}>
          <StageDetail s={replayStage} />
          <div className="mt-1 text-[11px] text-muted">{t("tr.replayHint", "기록된 데이터를 순서대로 재생합니다 — 실제 실행이 아닙니다")}</div>
        </Card>
      )}

      {/* 워터폴 + 단계 목록 */}
      <Card title={t("tr.timeline", "Stage 타임라인")}>
        <div className="space-y-1">
          {stages.map((s, i) => {
            const g = geom ? geom(s) : null;
            const isCurrent = playing && i === playIdx;
            const isDone = playIdx >= stages.length ? true : playIdx > i;
            const open = expanded.has(i);
            const key = s.stage_run_id || `${s.stage_key}-${i}`;
            return (
              <div
                key={key}
                className={`rounded-md border ${isCurrent ? "border-accent bg-accent-soft" : isDone && playIdx >= 0 ? "border-ok-border bg-ok-soft/40" : "border-border"}`}
              >
                <button
                  onClick={() => toggle(i)}
                  className="flex w-full items-center gap-2 px-2 py-1.5 text-left text-xs hover:bg-surface-hover"
                >
                  <span className="w-4 shrink-0 text-muted">{open ? "▾" : "▸"}</span>
                  <StatusBadge status={s.status} />
                  <span className="min-w-0 flex-1 truncate font-medium text-ink">
                    {s.stage_key || "?"}
                    {s.attempt != null && s.attempt > 1 && <span className="ml-1 text-muted">#{s.attempt}</span>}
                  </span>
                  {s.model && <span className="hidden max-w-32 truncate font-mono text-muted sm:inline">{s.model}</span>}
                  {s.error_class && <Badge tone="err">{s.error_class}</Badge>}
                  <span className="shrink-0 text-muted">{s.duration_ms != null ? fmtMs(s.duration_ms) : "-"}</span>
                  {s.cost_usd != null && <span className="w-16 shrink-0 text-right text-ink">{usd(s.cost_usd)}</span>}
                </button>
                {/* Gantt 바 트랙 */}
                {g && (
                  <div className="relative mx-2 mb-1.5 h-2.5 overflow-hidden rounded bg-surface-hover">
                    <div
                      className={`absolute top-0 h-full rounded ${s.status === "failed" || s.status === "error" ? "bg-err/80" : s.status === "succeeded" || s.status === "completed" || s.status === "success" ? "bg-ok/70" : "bg-accent/70"}`}
                      style={{ left: `${g.left}%`, width: `${g.width}%` }}
                      title={`${s.stage_key || ""} · ${fmtTs(s.started_at)}`}
                    />
                    {isCurrent && g && (
                      <div
                        className="absolute top-0 h-full w-0.5 bg-accent"
                        style={{ left: `${g.left}%` }}
                      />
                    )}
                  </div>
                )}
                {open && <StageDetail s={s} />}
              </div>
            );
          })}
        </div>
      </Card>
    </div>
  );
}

export default function TracesPage() {
  const { t } = useI18n();
  const [statusFilter, setStatusFilter] = useState("");
  const [selected, setSelected] = useState<string | null>(null);

  const { data: runs, isLoading, error, refetch } = useQuery({
    queryKey: ["runs", "traces"],
    queryFn: () => apiGet<RunListItem[]>("/runs?limit=100"),
    refetchInterval: 15000,
  });

  const filtered = useMemo(() => {
    const list = runs || [];
    if (!statusFilter) return list;
    return list.filter((r) => r.status === statusFilter || r.status?.startsWith(statusFilter));
  }, [runs, statusFilter]);

  return (
    <div className="p-4">
      <PageHead title={t("tr.title", "트레이스")} desc={t("tr.desc", "run별 stage 타임라인 — 워터폴 + 재생")}>
        <Button variant="ghost" className="px-2 py-0.5 text-xs" onClick={() => refetch()}>
          {t("common.refresh", "새로고침")}
        </Button>
      </PageHead>

      <div className="grid gap-3 lg:grid-cols-[320px_1fr]">
        {/* run 선택 목록 */}
        <Card title={t("tr.pickRun", "실행 선택")}>
          <div className="mb-2 flex flex-wrap gap-1">
            {RUN_FILTERS.map((f) => (
              <button
                key={f.key}
                onClick={() => setStatusFilter(f.key)}
                className={`rounded-md px-2 py-0.5 text-[11px] ${statusFilter === f.key ? "bg-accent text-white" : "bg-surface-hover text-muted hover:text-ink"}`}
              >
                {t(f.label, f.key || "전체")}
              </button>
            ))}
          </div>
          {isLoading && <Spinner />}
          {error && <ErrorBox message={error instanceof Error ? error.message : String(error)} onRetry={() => refetch()} />}
          {runs && !filtered.length && <Empty>{t("tr.noRuns", "실행 기록이 없습니다")}</Empty>}
          <div className="max-h-[70vh] space-y-0.5 overflow-y-auto">
            {filtered.map((r) => (
              <button
                key={r.id}
                onClick={() => setSelected(r.id === selected ? null : r.id)}
                className={`flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-xs hover:bg-surface-hover ${selected === r.id ? "bg-accent-soft" : ""}`}
              >
                <StatusBadge status={r.status} />
                <div className="min-w-0 flex-1">
                  <div className="truncate font-medium text-ink">{r.workflow || r.id.slice(0, 8)}</div>
                  <div className="truncate text-muted">{r.id.slice(0, 8)} · {fmtTs(r.created_at)}</div>
                </div>
                {r.project && <Badge>{r.project}</Badge>}
              </button>
            ))}
          </div>
        </Card>

        {/* 타임라인 */}
        <div>
          {selected ? (
            <Timeline key={selected} runId={selected} />
          ) : (
            <Card><Empty>{t("tr.selectHint", "왼쪽에서 run을 선택하면 stage 타임라인이 표시됩니다")}</Empty></Card>
          )}
        </div>
      </div>
    </div>
  );
}
