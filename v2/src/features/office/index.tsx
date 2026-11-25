/** Agent Office — dev_review run 파이프라인 + 활성 에이전트 세션 + 활동 피드 + 스프라이트 사무실.
 * 레거시 index.html의 loadOffice/officeState 기능을 React Query 기반으로 이행.
 * 상단 스프라이트 보드는 레거시 office/(office-view/life/world/path/atlas + jobmate-bridge)
 * 의 2D 캔버스 사무실을 sprites/ 모듈로 이식한 것. */
import { useCallback, useEffect, useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { apiGet, apiPost, apiDelete, ApiError } from "../../shared/api/client";
import type { Run, StageRun, AgentSession } from "../../shared/api/types";
import { useI18n, registerI18n } from "../../shared/i18n";
import { useAppStore, roleAllows } from "../../shared/store/appStore";
import { PageHead, Card, Empty, Spinner, ErrorBox, Button, Badge } from "../../shared/ui";
import { fmtTs, fmtAge, fmtElapsed, jsonSummary } from "../../shared/lib/format";
import SpriteBoard from "./SpriteBoard";
import { OfficeSidePanel } from "./OfficeSidePanel";
import { OfficeStageBoard } from "./StageSection";
import { useOfficeDemo } from "./demoState";
import { modeOf, sessionsToPeople } from "./sprites/bridge";
import { demoPeople } from "./sprites/demo-people";
import * as I18N from "./sprites/office-i18n";
import type { LifeEvent } from "./sprites/types";
import "./office.css";

registerI18n({
  ko: {
    "office.title": "Agent Office",
    "office.desc": "dev_review 워크플로 진행 상황과 활성 에이전트 세션 — 자동 갱신",
    "office.pipeline": "단계 진행",
    "office.runDetail": "실행 상세",
    "office.runs": "실행 목록",
    "office.sessions": "활성 세션",
    "office.activity": "활동 기록",
    "office.auditLog": "이벤트 로그",
    "office.noRun": "실행된 dev_review 워크플로가 없습니다",
    "office.noSessions": "활성 세션이 없습니다",
    "office.noActivity": "활동 기록이 없습니다",
    "office.noAudit": "이벤트 없음",
    "office.approve": "승인",
    "office.reject": "거절",
    "office.input": "입력",
    "office.approvals": "승인 기록",
    "office.error": "오류",
    "office.elapsed": "경과",
    "office.tokens": "토큰",
    "office.waiting": "승인 대기",
    "office.pickRun": "실행을 선택하세요",
    "office.schedTitle": "스케줄 관리",
    "office.schedMeta": "주기적으로 워크플로를 자동 실행",
    "office.schedAdd": "스케줄 추가",
    "office.schedAdding": "추가 중…",
    "office.schedAdded": "추가했습니다",
    "office.schedEmpty": "등록된 스케줄이 없습니다",
    "office.schedInterval": "간격(분)",
    "office.schedTask": "작업 내용 (선택)",
    "office.schedWf": "스케줄 워크플로",
    "office.schedNext": "다음",
    "office.schedLast": "마지막",
    "office.schedMin": "분",
    "office.schedOn": "켜짐",
    "office.schedOff": "꺼짐",
    "office.schedAdminOnly": "스케줄 관리(추가·토글·삭제)는 관리자(admin) 전용입니다.",
    "office.schedConfirmDelete": "이 스케줄을 삭제할까요?",
    "office.statsTitle": "세션 통계",
    "office.statsWork": "오늘 working",
    "office.statsDecisions": "decision",
    "office.statsEnded": "ended",
    "office.statsLastEnded": "최근 종료",
    "office.statsNoData": "세션 통계가 없습니다",
    "office.mode.board": "3D 보드",
    "office.mode.sprites": "스프라이트 사무실",
    "office.demo": "데모",
  },
  en: {
    "office.title": "Agent Office",
    "office.desc": "dev_review workflow progress and active agent sessions — auto-refresh",
    "office.pipeline": "Pipeline",
    "office.runDetail": "Run detail",
    "office.runs": "Runs",
    "office.sessions": "Active sessions",
    "office.activity": "Activity",
    "office.auditLog": "Event log",
    "office.noRun": "No dev_review workflow runs",
    "office.noSessions": "No active sessions",
    "office.noActivity": "No activity",
    "office.noAudit": "No events",
    "office.approve": "Approve",
    "office.reject": "Reject",
    "office.input": "Input",
    "office.approvals": "Approvals",
    "office.error": "Error",
    "office.elapsed": "Elapsed",
    "office.tokens": "Tokens",
    "office.waiting": "Waiting approval",
    "office.pickRun": "Select a run",
    "office.schedTitle": "Schedules",
    "office.schedMeta": "Automatically run workflows on an interval",
    "office.schedAdd": "Add schedule",
    "office.schedAdding": "Adding…",
    "office.schedAdded": "Added",
    "office.schedEmpty": "No schedules registered",
    "office.schedInterval": "Interval (min)",
    "office.schedTask": "Task (optional)",
    "office.schedWf": "Schedule workflow",
    "office.schedNext": "next",
    "office.schedLast": "last",
    "office.schedMin": "min",
    "office.schedOn": "on",
    "office.schedOff": "off",
    "office.schedAdminOnly": "Schedule management (add/toggle/delete) is admin-only.",
    "office.schedConfirmDelete": "Delete this schedule?",
    "office.statsTitle": "Session stats",
    "office.statsWork": "today working",
    "office.statsDecisions": "decisions",
    "office.statsEnded": "ended",
    "office.statsLastEnded": "last ended",
    "office.statsNoData": "No session stats",
    "office.mode.board": "3D Board",
    "office.mode.sprites": "Sprite Office",
    "office.demo": "Demo",
  },
});

// dev_review stage_key → 표시 순서 (레거시 OFFICE_STAGES와 동일)
const STAGE_ORDER: { key: string; label: string }[] = [
  { key: "analysis", label: "요구사항 분석" },
  { key: "implement", label: "구현" },
  { key: "auto_checks", label: "자동 검사" },
  { key: "security", label: "보안 검토" },
  { key: "code_review", label: "코드 리뷰" },
  { key: "qa", label: "최종 검수" },
  { key: "approval", label: "사람 승인" },
];
const STAGE_LABEL: Record<string, string> = Object.fromEntries(STAGE_ORDER.map((s) => [s.key, s.label]));

// 레거시 OFFICE_ACTIVE와 동일
const ACTIVE_STATUSES = new Set(["running", "waiting_approval", "waiting_review", "queued", "needs_human"]);

interface RunListItem {
  id: string;
  workflow?: string;
  status: string;
  project?: string;
  created_at?: string;
}

interface AuditEvent {
  id?: string;
  actor?: string;
  action?: string;
  object_ref?: string;
  detail?: Record<string, unknown>;
  created_at?: string;
}

interface ApprovalRecord {
  stage_key?: string;
  decision?: string;
  decided_by?: string;
  decided_at?: string;
  comment?: string;
}

interface OfficeStageRun extends StageRun {
  tokens_in?: number;
  tokens_out?: number;
  model?: string;
}

interface RunDetail extends Run {
  project?: string;
  stages?: OfficeStageRun[];
  approvals?: ApprovalRecord[];
  audit?: AuditEvent[];
  metadata?: Record<string, unknown>;
}

interface SessionView extends AgentSession {
  health?: string;
  est_cost_usd?: number;
  ended_summary?: string;
  artifacts?: string | unknown[];   // TEXT JSON 컬럼 — 문자열 또는 배열
}

interface Schedule {
  id: string;
  workflow?: string;
  input?: Record<string, unknown> | string | null;
  interval_minutes?: number;
  enabled?: boolean;
  next_run_at?: string | null;
  last_run_at?: string | null;
}

interface WorkflowInfo {
  key?: string;
}

interface SessionStatsTool {
  sessions?: number;
  working_seconds?: number;
  decisions?: number;
  ended?: number;
  last_active?: string;
}

interface SessionStatsResponse {
  tools?: Record<string, SessionStatsTool>;
  hourly?: { hour?: number; working_seconds?: number }[];
}

interface EndedSessionsResponse {
  sessions?: SessionView[];
  agent_sessions?: SessionView[];
}

/** 레거시 _sessionArtifacts — artifacts가 JSON 문자열일 수 있음 */
function sessionArtifacts(s: SessionView | undefined): string[] {
  let a = s?.artifacts;
  if (typeof a === "string") {
    try { a = JSON.parse(a); } catch { return []; }
  }
  return Array.isArray(a) ? a.filter((x): x is string => typeof x === "string") : [];
}

function localDateStr(): string {
  const n = new Date();
  const p = (x: number) => String(x).padStart(2, "0");
  return `${n.getFullYear()}-${p(n.getMonth() + 1)}-${p(n.getDate())}`;
}

function scheduleInputText(input: Schedule["input"]): string {
  if (input && typeof input === "object") {
    const task = (input as Record<string, unknown>).task;
    if (typeof task === "string" && task) return task;
    const summary = (input as Record<string, unknown>).summary;
    if (typeof summary === "string" && summary) return summary;
    try { return JSON.stringify(input).slice(0, 80); } catch { return String(input).slice(0, 80); }
  }
  if (input) return String(input).slice(0, 80);
  return "-";
}

interface ActivityItem {
  at?: string;
  status?: string;
  doing?: string;
  project?: string;
}

const RUN_STATUS_TONE: Record<string, "ok" | "warn" | "err" | "accent" | "muted"> = {
  succeeded: "ok", completed: "ok", running: "accent", queued: "muted",
  pending: "muted", waiting_approval: "warn", waiting_review: "warn",
  failed: "err", rejected: "err", skipped: "muted", cancelled: "muted",
  budget_exhausted: "err", needs_human: "warn",
};

const SESSION_STATUS_TONE: Record<string, "ok" | "warn" | "err" | "accent" | "muted"> = {
  working: "accent", decision: "warn", rest: "muted", ended: "muted",
};

function StatusBadge({ status }: { status?: string }) {
  return <Badge tone={RUN_STATUS_TONE[status || ""] ?? "muted"}>{status || "-"}</Badge>;
}

/** stage_key별 최신 attempt — 레거시 officeState.stages와 동일 규칙 */
function latestStageRuns(stages: OfficeStageRun[] | undefined): Map<string, OfficeStageRun> {
  const map = new Map<string, OfficeStageRun>();
  for (const s of stages || []) {
    const key = s.stage_key || s.stage || "";
    if (!key) continue;
    const cur = map.get(key);
    if (!cur || (s.attempt || 1) > (cur.attempt || 1)) map.set(key, s);
  }
  return map;
}

/** 레거시 officePickRun — 활성 상태 우선(최신 created_at), 없으면 전체 최신 */
function pickRun(runs: RunListItem[]): RunListItem | null {
  const dr = (runs || []).filter((r) => (r.workflow || "") === "dev_review");
  if (!dr.length) return null;
  const byCreated = (a: RunListItem, b: RunListItem) =>
    String(b.created_at || "").localeCompare(String(a.created_at || ""));
  const active = dr.filter((r) => ACTIVE_STATUSES.has(r.status)).sort(byCreated);
  if (active.length) return active[0];
  return dr.slice().sort(byCreated)[0];
}

function stageTone(status?: string): "ok" | "warn" | "err" | "accent" | "muted" {
  return RUN_STATUS_TONE[status || ""] ?? "muted";
}

// ── 파이프라인 스테이지 카드 ──
function PipelineCard({ runId }: { runId: string | null }) {
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
                  {STAGE_LABEL[key] || key}
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
function RunDetailPanel({ runId }: { runId: string | null }) {
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
function AuditFeed({ runId }: { runId: string | null }) {
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

// ── 활성 세션 목록 + 활동 기록 ──
function SessionsPanel() {
  const { t } = useI18n();
  const [selKey, setSelKey] = useState<string | null>(null);
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["office-sessions"],
    queryFn: () => apiGet<SessionView[]>("/agent-sessions?limit=50&include_ended=false"),
    refetchInterval: 10000,
  });
  const sessions = (data || []).filter((s) => s.status !== "ended");

  const selected = sessions.find(
    (s) => `${s.tool}:${s.session_key}` === selKey,
  );
  const activity = useQuery({
    queryKey: ["office-session-activity", selected?.tool, selected?.session_key],
    queryFn: () =>
      apiGet<{ activity: ActivityItem[] }>(
        `/agent-sessions/${encodeURIComponent(selected!.tool || "")}/${encodeURIComponent(selected!.session_key || "")}/activity?limit=200`,
      ),
    enabled: !!selected?.tool && !!selected?.session_key,
    refetchInterval: 5000,
  });

  return (
    <div className="grid gap-3 lg:grid-cols-[1fr_320px]">
      <Card title={t("office.sessions", "활성 세션")}>
        {isLoading && <Spinner />}
        {error && <ErrorBox message={error instanceof Error ? error.message : String(error)} onRetry={() => refetch()} />}
        {data && !sessions.length && <Empty>{t("office.noSessions", "활성 세션이 없습니다")}</Empty>}
        <div className="space-y-1.5">
          {sessions.map((s) => {
            const key = `${s.tool}:${s.session_key}`;
            const isSel = key === selKey;
            const staleWorking = s.status === "working" && s.health === "stale";
            return (
              <button
                key={key}
                onClick={() => setSelKey(isSel ? null : key)}
                className={`w-full rounded-lg border px-3 py-2 text-left transition-colors ${
                  isSel
                    ? "border-accent bg-accent-soft"
                    : s.status === "decision"
                      ? "border-warn-border bg-warn-soft"
                      : "border-border bg-surface-hover hover:bg-surface"
                }`}
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="truncate text-sm font-medium text-ink">
                    {s.title || s.project || s.session_key || s.tool}
                  </span>
                  <Badge tone={SESSION_STATUS_TONE[s.status || ""] ?? "muted"}>
                    {staleWorking ? "rest" : s.status || "-"}
                  </Badge>
                </div>
                <div className="mt-0.5 flex flex-wrap items-center gap-x-2 text-xs text-muted">
                  <span>{s.tool}</span>
                  {s.project && <span className="rounded bg-surface px-1">{s.project}</span>}
                  {s.last_event_at && <span>{fmtAge(Math.max(0, (Date.now() - new Date(s.last_event_at).getTime()) / 1000))}</span>}
                  {s.est_cost_usd ? <span>${s.est_cost_usd.toFixed(4)}</span> : null}
                </div>
                {(s.doing || s.ask) && (
                  <div className="mt-1 truncate text-xs">
                    {s.status === "decision" && s.ask ? (
                      <span className="text-warn">❓ {s.ask}</span>
                    ) : (
                      <span className="text-muted">{s.doing || s.ask}</span>
                    )}
                  </div>
                )}
              </button>
            );
          })}
        </div>
      </Card>

      <Card title={t("office.activity", "활동 기록")} className="max-h-[480px] overflow-y-auto">
        {!selected && <Empty>{t("office.noActivity", "활동 기록이 없습니다")}</Empty>}
        {selected && activity.isLoading && <Spinner />}
        {selected && activity.error && (
          <ErrorBox message={activity.error instanceof Error ? activity.error.message : String(activity.error)} />
        )}
        {selected && activity.data && !activity.data.activity?.length && (
          <Empty>{t("office.noActivity", "활동 기록이 없습니다")}</Empty>
        )}
        {selected && activity.data?.activity && (
          <div className="space-y-1.5">
            {activity.data.activity.map((a, i) => (
              <div key={i} className="flex flex-wrap items-baseline gap-x-2 text-xs">
                <span className="shrink-0 text-muted">{fmtTs(a.at)}</span>
                <Badge tone={SESSION_STATUS_TONE[a.status || ""] ?? "muted"}>{a.status || "-"}</Badge>
                {a.doing && <span className="truncate text-ink">{a.doing}</span>}
                {a.project && <span className="text-muted">{a.project}</span>}
              </div>
            ))}
          </div>
        )}
      </Card>
    </div>
  );
}

// ── 세션 통계 (레거시 officeUpdateSessionStats/#jmo-stats 이식) ──
function SessionStatsCard() {
  const { t } = useI18n();
  const since = localDateStr();
  const stats = useQuery({
    queryKey: ["office-session-stats", since],
    queryFn: () => apiGet<SessionStatsResponse>(`/agent-sessions/stats?since=${since}`),
    refetchInterval: 30000,
  });
  const ended = useQuery({
    queryKey: ["office-ended-sessions"],
    queryFn: () => apiGet<EndedSessionsResponse | SessionView[]>("/agent-sessions?limit=20&include_ended=true"),
    refetchInterval: 30000,
  });

  const tools = stats.data?.tools || {};
  let workSec = 0, decisions = 0, endedN = 0;
  for (const tool of Object.values(tools)) {
    workSec += tool.working_seconds || 0;
    decisions += tool.decisions || 0;
    endedN += tool.ended || 0;
  }

  // 레거시: ended 세션 중 ended_summary 또는 artifacts가 있는 최근 1건
  const endedList = Array.isArray(ended.data)
    ? ended.data
    : ended.data?.sessions || ended.data?.agent_sessions || [];
  const lastEnded = endedList.find(
    (s) => s.status === "ended" && (s.ended_summary || sessionArtifacts(s).length),
  );
  const lastEndedText = lastEnded
    ? `${lastEnded.project || lastEnded.session_key || ""} — ${
        lastEnded.ended_summary || sessionArtifacts(lastEnded).join(", ")
      }`
    : "";

  // 시간대별 히트맵 (레거시 _renderHourly)
  const hourly = (stats.data?.hourly || [])
    .filter((h) => h && Number.isFinite(Number(h.hour)))
    .slice()
    .sort((a, b) => Number(a.hour) - Number(b.hour));
  const maxSec = Math.max(...hourly.map((h) => Number(h.working_seconds) || 0), 1);

  if (stats.isLoading && ended.isLoading) return <Spinner />;
  return (
    <Card title={t("office.statsTitle", "세션 통계")}>
      {stats.error && <ErrorBox message={stats.error instanceof Error ? stats.error.message : String(stats.error)} />}
      {ended.error && <ErrorBox message={ended.error instanceof Error ? ended.error.message : String(ended.error)} />}
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted">
        <span>{t("office.statsWork", "오늘 working")} <b className="text-ink">{Math.round(workSec / 60)}분</b></span>
        <span>{t("office.statsDecisions", "decision")} <b className="text-ink">{decisions}회</b></span>
        <span>{t("office.statsEnded", "ended")} <b className="text-ink">{endedN}</b></span>
        {lastEndedText && (
          <span className="min-w-0 truncate">
            {t("office.statsLastEnded", "최근 종료")}: <span className="text-ink">{lastEndedText}</span>
          </span>
        )}
        {!Object.keys(tools).length && !lastEndedText && (
          <span>{t("office.statsNoData", "세션 통계가 없습니다")}</span>
        )}
      </div>
      {hourly.length > 0 && (
        <div className="mt-3">
          <div className="flex h-12 items-end gap-px" aria-label="hourly working seconds">
            {hourly.map((h, i) => {
              const sec = Math.max(0, Number(h.working_seconds) || 0);
              const pct = sec > 0 ? Math.max(6, Math.round((sec / maxSec) * 100)) : 4;
              const hh = String(Math.floor(Number(h.hour)) % 24).padStart(2, "0");
              return (
                <div
                  key={i}
                  className={`flex-1 rounded-sm ${sec ? "bg-accent" : "bg-surface-hover"}`}
                  style={{ height: `${pct}%` }}
                  title={`${hh}시 ${Math.round(sec / 60)}분`}
                />
              );
            })}
          </div>
          <div className="mt-0.5 flex justify-between text-[10px] text-muted">
            <span>00시</span><span>12시</span><span>23시</span>
          </div>
        </div>
      )}
    </Card>
  );
}

// ── 스케줄 관리 (레거시 #office-sched-card 이식, admin 전용 쓰기) ──
const schedSelectCls = "rounded-md border border-border bg-surface px-2 py-1.5 text-sm";
const schedInputCls = "rounded-md border border-border bg-surface px-2 py-1.5 text-sm";

function ScheduleCard() {
  const { t } = useI18n();
  const qc = useQueryClient();
  const role = useAppStore((s) => s.role);
  const isAdmin = roleAllows("admin", role);
  const [open, setOpen] = useState(false);
  const [wf, setWf] = useState("");
  const [intervalMin, setIntervalMin] = useState(60);
  const [task, setTask] = useState("");
  const [msg, setMsg] = useState<{ text: string; err?: boolean }>({ text: "" });
  const [busy, setBusy] = useState(false);

  const scheds = useQuery({
    queryKey: ["office-schedules"],
    queryFn: async () => {
      const d = await apiGet<Schedule[] | { schedules?: Schedule[] }>("/schedules");
      return Array.isArray(d) ? d : d.schedules || [];
    },
    enabled: open,
    refetchInterval: open ? 30000 : false,
  });
  const workflows = useQuery({
    queryKey: ["office-sched-workflows"],
    staleTime: 300000,
    enabled: open && isAdmin,
    queryFn: async (): Promise<string[]> => {
      const wfs = await apiGet<WorkflowInfo[]>("/workflows");
      const keys = (wfs || []).map((w) => w.key || "").filter(Boolean);
      return keys.length ? keys : ["dev_review"];
    },
  });

  const items = scheds.data || [];
  const wfKeys = workflows.data || [];
  const effectiveWf = wf || wfKeys[0] || "";

  const invalidate = () => qc.invalidateQueries({ queryKey: ["office-schedules"] });

  const submit = async () => {
    if (!effectiveWf) { setMsg({ text: t("office.schedWf", "스케줄 워크플로"), err: true }); return; }
    setBusy(true);
    setMsg({ text: t("office.schedAdding", "추가 중…") });
    try {
      await apiPost("/schedules", {
        workflow: effectiveWf,
        input: task.trim() ? { task: task.trim() } : {},
        interval_minutes: Number(intervalMin) || 60,
      });
      setTask("");
      setMsg({ text: t("office.schedAdded", "추가했습니다") });
      invalidate();
    } catch (e) {
      setMsg({ text: e instanceof ApiError ? e.message : String(e), err: true });
    } finally {
      setBusy(false);
    }
  };

  const toggle = async (s: Schedule) => {
    try {
      await apiPost(`/schedules/${encodeURIComponent(s.id)}/toggle`, { enabled: !s.enabled });
      invalidate();
    } catch (e) {
      alert(e instanceof ApiError ? e.message : String(e));
    }
  };

  const del = async (s: Schedule) => {
    if (!confirm(t("office.schedConfirmDelete", "이 스케줄을 삭제할까요?"))) return;
    try {
      await apiDelete(`/schedules/${encodeURIComponent(s.id)}`);
      invalidate();
    } catch (e) {
      alert(e instanceof ApiError ? e.message : String(e));
    }
  };

  return (
    <details
      className="rounded-xl border border-border bg-surface p-4 shadow-sm"
      onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)}
    >
      <summary className="cursor-pointer text-base font-semibold text-ink">
        {t("office.schedTitle", "스케줄 관리")}
        <span className="ml-2 text-xs font-normal text-muted">{t("office.schedMeta", "주기적으로 워크플로를 자동 실행")}</span>
      </summary>
      <div className="mt-3">
        {open && scheds.isLoading && <Spinner />}
        {open && scheds.error && (
          <ErrorBox
            message={scheds.error instanceof ApiError && scheds.error.status === 403
              ? t("office.schedAdminOnly", "스케줄 관리(추가·토글·삭제)는 관리자(admin) 전용입니다.")
              : scheds.error instanceof Error ? scheds.error.message : String(scheds.error)}
            onRetry={() => scheds.refetch()}
          />
        )}
        {open && scheds.data && !items.length && <Empty>{t("office.schedEmpty", "등록된 스케줄이 없습니다")}</Empty>}
        {open && items.map((s) => (
          <div key={s.id} className="mb-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 rounded-lg border border-border bg-surface-hover px-3 py-2 last:mb-0">
            <span className="font-mono text-xs text-ink">{s.workflow || "-"}</span>
            <span className="min-w-[120px] flex-1 truncate text-xs text-muted">{scheduleInputText(s.input)}</span>
            <span className="text-xs text-muted">{s.interval_minutes ?? "-"}{t("office.schedMin", "분")}</span>
            {isAdmin ? (
              <>
                <Button
                  variant="secondary"
                  className={`px-2.5 py-1 text-xs ${s.enabled ? "text-ok" : "text-muted"}`}
                  disabled={busy}
                  onClick={() => toggle(s)}
                >
                  {s.enabled ? t("office.schedOn", "켜짐") : t("office.schedOff", "꺼짐")}
                </Button>
                <Button variant="secondary" className="px-2.5 py-1 text-xs" onClick={() => del(s)}>
                  {t("common.delete", "삭제")}
                </Button>
              </>
            ) : (
              <Badge tone={s.enabled ? "ok" : "muted"}>{s.enabled ? t("office.schedOn", "켜짐") : t("office.schedOff", "꺼짐")}</Badge>
            )}
            <span className="text-xs text-muted">{t("office.schedNext", "다음")} {fmtTs(s.next_run_at)}</span>
            <span className="text-xs text-muted">{t("office.schedLast", "마지막")} {fmtTs(s.last_run_at)}</span>
          </div>
        ))}
        {open && isAdmin && (
          <form
            className="mt-3 flex flex-wrap items-center gap-2"
            onSubmit={(e) => { e.preventDefault(); submit(); }}
          >
            <select
              value={effectiveWf}
              onChange={(e) => setWf(e.target.value)}
              className={schedSelectCls}
              aria-label={t("office.schedWf", "스케줄 워크플로")}
            >
              {wfKeys.map((k) => <option key={k} value={k}>{k}</option>)}
              {!wfKeys.length && <option value="">{t("office.schedWf", "스케줄 워크플로")}</option>}
            </select>
            <input
              type="number" min={5} step={1} value={intervalMin}
              onChange={(e) => setIntervalMin(Number(e.target.value))}
              placeholder={t("office.schedInterval", "간격(분)")}
              className={`${schedInputCls} w-28`}
            />
            <input
              value={task}
              onChange={(e) => setTask(e.target.value)}
              placeholder={t("office.schedTask", "작업 내용 (선택)")}
              autoComplete="off"
              className={`${schedInputCls} min-w-40 flex-1`}
            />
            <Button type="submit" className="px-3 py-1 text-xs" disabled={busy}>
              {busy ? t("office.schedAdding", "추가 중…") : t("office.schedAdd", "스케줄 추가")}
            </Button>
            {msg.text && <span className={`text-xs ${msg.err ? "text-err" : "text-ok"}`}>{msg.text}</span>}
          </form>
        )}
        {open && role != null && !isAdmin && (
          <p className="mt-3 text-xs text-muted">{t("office.schedAdminOnly", "스케줄 관리(추가·토글·삭제)는 관리자(admin) 전용입니다.")}</p>
        )}
      </div>
    </details>
  );
}

// ── 스프라이트 사무실 (레거시 #jmo-wrap — 2D 캔버스 + 사람들 패널 + 활동 로그) ──
// paused: 보드 모드일 때 시뮬레이션만 멈추고 마운트는 유지 — 토글해도 상태가 보존된다(레거시와 동일)

// 세션 필터 상태 — 레거시 localStorage 키 그대로 (officeTool/officeEnded/officeProject)
const OFFICE_TOOLS = ["codex", "claude", "gemini", "devin", "all"] as const;
type OfficeTool = (typeof OFFICE_TOOLS)[number];

function lsGet(key: string): string | null {
  try { return localStorage.getItem(key); } catch { return null; }
}
function lsSet(key: string, v: string) {
  try { localStorage.setItem(key, v); } catch { /* noop */ }
}

/** 세션 고정 핀 — 레거시 jobmate.officePins (JSON 배열, "tool:session_key") */
function useOfficePins(): [Set<string>, (id: string) => void] {
  const [pins, setPins] = useState<Set<string>>(() => {
    try {
      const p = JSON.parse(localStorage.getItem("jobmate.officePins") || "[]");
      return new Set(Array.isArray(p) ? p : []);
    } catch { return new Set(); }
  });
  const togglePin = useCallback((id: string) => {
    setPins((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      try { localStorage.setItem("jobmate.officePins", JSON.stringify([...next])); } catch { /* noop */ }
      return next;
    });
  }, []);
  return [pins, togglePin];
}

function OfficeSpriteSection({ paused = false }: { paused?: boolean }) {
  const { lang, t } = useI18n();
  const [selId, setSelId] = useState<string | null>(null);
  const [lifeEvents, setLifeEvents] = useState<LifeEvent[]>([]);
  const [imgFailed, setImgFailed] = useState(false);
  // 데모 캐스트 전이용 틱 — 레거시 office-view.js provider 폴링(POLL_MS=3s)에 해당
  const [demoTick, setDemoTick] = useState(0);
  useEffect(() => {
    const id = setInterval(() => setDemoTick((n) => n + 1), 3000);
    return () => clearInterval(id);
  }, []);

  // 세션 필터 — 레거시 office-tool-filter / office-ended-btn / project 셀렉트 / 검색
  const [tool, setTool] = useState<OfficeTool>(() => {
    const v = lsGet("jobmate.officeTool");
    return (OFFICE_TOOLS as readonly string[]).includes(v || "") ? (v as OfficeTool) : "all";
  });
  const [ended, setEnded] = useState(() => lsGet("jobmate.officeEnded") === "1" ? "1" : "0");
  const [project, setProject] = useState(() => lsGet("jobmate.officeProject") || "");
  const [query, setQuery] = useState("");
  const [demo] = useOfficeDemo();
  const [pins, togglePin] = useOfficePins();

  const pickTool = (v: OfficeTool) => { setTool(v); lsSet("jobmate.officeTool", v); };
  const pickEnded = (v: string) => { setEnded(v); lsSet("jobmate.officeEnded", v); };
  const pickProject = (v: string) => { setProject(v); lsSet("jobmate.officeProject", v); };

  // include_ended=true — 활성/종료 필터는 클라이언트에서 적용 (SessionsPanel과 캐시 공유)
  const { data, isLoading, error } = useQuery({
    queryKey: ["office-sessions"],
    queryFn: () => apiGet<SessionView[]>("/agent-sessions?limit=50&include_ended=true"),
    refetchInterval: 10000,
  });

  const endedMode = ended === "1";
  // 레거시 jmSessionProvider와 동일한 필터 순서: tool → ended → project → query
  const baseList = useMemo(
    () => (data || []).filter((s) =>
      (tool === "all" || s.tool === tool) &&
      (endedMode ? s.status === "ended" : s.status !== "ended")),
    [data, tool, endedMode],
  );

  // 프로젝트 셀렉트 옵션 — tool+ended 필터까지 적용된 목록의 distinct project (레거시 _syncOfficeProjects)
  const projectOptions = useMemo(() => {
    const set = new Set<string>();
    for (const s of baseList) {
      if (s.project) set.add(String(s.project));
    }
    return [...set].sort();
  }, [baseList]);

  const people = useMemo(() => {
    let list = baseList;
    if (project) list = list.filter((s) => String(s.project || "") === project);
    const q = query.trim().toLowerCase();
    if (q) {
      list = list.filter((s) =>
        [s.tool, s.project, s.session_key, s.title, s.doing, s.ask]
          .some((v) => String(v || "").toLowerCase().includes(q)));
    }
    const real = sessionsToPeople(list as AgentSession[], { includeEnded: endedMode })
      .map((p) => ({ ...p, pinned: pins.has(p.id) }));
    // 레거시 jmSessionProvider: 핀 고정 세션을 먼저(안정 정렬) + officeDemo ON이면 가상 캐스트를 뒤에
    real.sort((a, b) => Number(!!b.pinned) - Number(!!a.pinned));
    return demo ? [...real, ...demoPeople(demoTick)] : real;
  }, [baseList, project, query, endedMode, pins, demo, demoTick]);

  const onEvents = useCallback((evs: LifeEvent[]) => {
    setLifeEvents((prev) => [...prev, ...evs].slice(-300));
  }, []);

  // 패널 클릭 — keep=true 면 강제 선택(로그 줄), 아니면 같은 id 재클릭 시 해제(레거시 select 와 동일)
  const pick = useCallback((id: string | null, keep?: boolean) => {
    setSelId((prev) => (id && (keep || id !== prev) ? id : null));
  }, []);

  const summary = useMemo(() => {
    const c = (m: string) => people.filter((p) => modeOf(p) === m).length;
    const w = c("working"), d = c("decision"), r = c("rest");
    const subs = people.reduce((n, p) => n + (p.subCount || 0), 0);
    return I18N.t("sum", { w, d, r }) + (subs ? I18N.t("sum.subs", { n: subs }) : "");
    // lang 은 useI18n 구독용 — 언어가 바뀌면 I18N.t 가 새 언어로 다시 만든다
  }, [people, lang]);

  // 레거시 bar(): 그림 로드 실패 → 연결 실패 → 로딩 순으로 안내 줄을 채운다
  const notice = [
    imgFailed ? I18N.t("note.img") : "",
    error ? I18N.t("note.net") : (!data && isLoading ? I18N.t("note.loading") : ""),
  ].filter(Boolean).join(" · ");

  // 세그먼트 버튼 공통 스타일 — 레거시 .office-mode-btn[aria-pressed] 대응
  const segBtn = (on: boolean) =>
    `rounded-md px-3 py-1 text-xs transition-colors ${
      on ? "bg-accent font-semibold text-white" : "text-muted hover:text-ink"
    }`;
  const segWrap = "flex items-center gap-0.5 rounded-lg border border-border bg-surface p-0.5";
  const pinCount = people.filter((p) => p.pinned).length;

  return (
    <section className="jmo-office rounded-xl border border-border bg-surface p-3 shadow-sm">
      <div className="jmo-notice mb-2 flex flex-wrap items-baseline gap-x-3">
        <span>{summary}</span>
        {demo && <span className="jmo-demo">{I18N.t("demo")}</span>}
        {notice && <span className="text-warn">{notice}</span>}
      </div>
      {/* 세션 필터 바 — 레거시 #office-tool-filter + #office-session-filters */}
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <div className={segWrap} role="group" aria-label={t("office.toolFilter", "세션 도구 필터")}>
          {OFFICE_TOOLS.map((v) => (
            <button
              key={v}
              type="button"
              aria-pressed={tool === v}
              onClick={() => pickTool(v)}
              className={segBtn(tool === v)}
            >
              {v === "all" ? t("common.all", "전체") : v[0].toUpperCase() + v.slice(1)}
            </button>
          ))}
        </div>
        <div className={segWrap} role="group" aria-label={t("office.sessFilter", "세션 필터")}>
          <button type="button" aria-pressed={!endedMode} onClick={() => pickEnded("0")} className={segBtn(!endedMode)}>
            {t("office.active", "활성")}
          </button>
          <button type="button" aria-pressed={endedMode} onClick={() => pickEnded("1")} className={segBtn(endedMode)}>
            {t("office.ended", "종료")}
          </button>
          <select
            aria-label={t("office.projFilter", "프로젝트 필터")}
            value={project}
            onChange={(e) => pickProject(e.target.value)}
            className="rounded-md border border-border bg-surface px-2 py-1 text-xs text-ink-2"
          >
            <option value="">{t("office.allProjects", "전체 프로젝트")}</option>
            {projectOptions.map((p) => <option key={p} value={p}>{p}</option>)}
          </select>
          <input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={t("office.sessSearch", "세션 검색…")}
            aria-label={t("office.sessSearch", "세션 검색…")}
            autoComplete="off"
            className="w-36 rounded-md border border-border bg-surface px-2 py-1 text-xs text-ink outline-none focus:border-accent"
          />
          {pinCount > 0 && (
            <span className="rounded-md border border-border px-2 py-1 text-xs text-muted">📌 {pinCount}</span>
          )}
        </div>
      </div>
      <div className="jmo-main">
        <div className="jmo-stage">
          <SpriteBoard
            className="jmo-box"
            people={people}
            selectedId={selId}
            onSelect={setSelId}
            onEvents={onEvents}
            onReady={(info) => setImgFailed(info.failedImage)}
            paused={paused}
          />
        </div>
        <OfficeSidePanel
          people={people}
          selectedId={selId}
          onPick={pick}
          events={lifeEvents}
          onTogglePin={togglePin}
        />
      </div>
    </section>
  );
}

function useRunDetail(id: string | null) {
  return useQuery({
    queryKey: ["office-run", id],
    queryFn: () => apiGet<RunDetail>(`/runs/${encodeURIComponent(id!)}`),
    enabled: !!id,
    refetchInterval: (q) => {
      const s = (q.state.data as RunDetail | undefined)?.status;
      return s === "running" || s === "queued" || s === "waiting_approval" ? 5000 : 10000;
    },
  });
}

export default function OfficePage() {
  const { t } = useI18n();
  const [selRunId, setSelRunId] = useState<string | null>(null);
  const [officeMode, setOfficeMode] = useState<"board" | "sprites">("board");
  // 레거시 #office-demo-btn — 보드/스프라이트 공용 데모 캐스트 토글
  const [demo, setDemo] = useOfficeDemo();

  const { data: runs, isLoading, error, refetch } = useQuery({
    queryKey: ["office-runs"],
    queryFn: () => apiGet<RunListItem[]>("/runs?workflow=dev_review&limit=50"),
    refetchInterval: 8000,
  });

  const picked = useMemo(() => pickRun(runs || []), [runs]);
  // 기본 선택: 자동 선택된 run. 사용자가 목록에서 다른 run을 고르면 유지.
  const activeRunId = selRunId || picked?.id || null;

  // 목록에서 사라진 run을 선택 중이면 자동 선택으로 복귀
  useEffect(() => {
    if (selRunId && runs && !runs.some((r) => r.id === selRunId)) setSelRunId(null);
  }, [runs, selRunId]);

  return (
    <div className="w-full space-y-4 p-4">
      <PageHead title={t("office.title", "Agent Office")} desc={t("office.desc", "dev_review 워크플로 진행 상황과 활성 에이전트 세션 — 자동 갱신")} />

      {/* 관제 모드 토글 — 레거시 #office-view-toggle (3D 보드 / 스프라이트 사무실) */}
      <div className="flex items-center justify-end gap-2">
        <div className="flex rounded-lg border border-border bg-surface p-0.5" role="group" aria-label={t("office.modeLabel", "관제 모드")}>
          {(["board", "sprites"] as const).map((m) => (
            <button
              key={m}
              type="button"
              aria-pressed={officeMode === m}
              onClick={() => setOfficeMode(m)}
              className={`rounded-md px-3 py-1 text-xs transition-colors ${
                officeMode === m ? "bg-accent-soft font-semibold text-accent" : "text-muted hover:text-ink"
              }`}
            >
              {m === "board" ? t("office.mode.board", "3D 보드") : t("office.mode.sprites", "스프라이트 사무실")}
            </button>
          ))}
        </div>
        <button
          type="button"
          aria-pressed={demo}
          onClick={() => setDemo(!demo)}
          className={`rounded-md px-3 py-1 text-xs transition-colors ${
            demo ? "bg-accent font-semibold text-white" : "text-muted hover:text-ink"
          }`}
        >
          {t("office.demo", "데모")}
        </button>
      </div>

      {/* 두 모드 모두 마운트 유지 — 토글 시 스프라이트 시뮬레이션/보드 상태가 보존된다 (레거시와 동일) */}
      <div hidden={officeMode !== "board"}>
        <OfficeStageBoard runId={activeRunId} />
      </div>
      <div hidden={officeMode !== "sprites"}>
        <OfficeSpriteSection paused={officeMode !== "sprites"} />
      </div>

      <div className="grid gap-4 lg:grid-cols-[240px_1fr]">
        {/* 실행 목록 */}
        <Card title={t("office.runs", "실행 목록")} className="max-h-[560px] overflow-y-auto">
          {isLoading && <Spinner />}
          {error && <ErrorBox message={error instanceof Error ? error.message : String(error)} onRetry={() => refetch()} />}
          {runs && !runs.length && <Empty>{t("office.noRun", "실행된 dev_review 워크플로가 없습니다")}</Empty>}
          {(runs || []).map((r) => (
            <button
              key={r.id}
              onClick={() => setSelRunId(r.id === selRunId ? null : r.id)}
              className={`mb-1 flex w-full items-center gap-2 rounded-md border px-2 py-1.5 text-left last:mb-0 ${
                r.id === activeRunId ? "border-accent bg-accent-soft" : "border-border bg-surface-hover hover:bg-surface"
              }`}
            >
              <StatusBadge status={r.status} />
              <div className="min-w-0 flex-1">
                <div className="truncate text-xs font-medium text-ink">{r.id.slice(0, 8)}</div>
                <div className="text-[11px] text-muted">{fmtTs(r.created_at)}</div>
              </div>
            </button>
          ))}
        </Card>

        {/* 파이프라인 + 상세 */}
        <div className="space-y-4">
          <Card>
            <PipelineCard runId={activeRunId} />
          </Card>
          <div className="grid gap-4 lg:grid-cols-2">
            <Card title={t("office.runDetail", "실행 상세")}>
              <RunDetailPanel runId={activeRunId} />
            </Card>
            <AuditFeed runId={activeRunId} />
          </div>
        </div>
      </div>

      {/* 하단 정보 — 세션 목록(넓게) | 통계+스케줄(세로 스택) 좌우 배치 */}
      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(320px,420px)]">
        <SessionsPanel />
        <div className="space-y-4">
          <SessionStatsCard />
          <ScheduleCard />
        </div>
      </div>
    </div>
  );
}
