import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { apiGet, apiPost, apiDelete, ApiError } from "../../shared/api/client";
import { useI18n } from "../../shared/i18n";
import { useAppStore, roleAllows } from "../../shared/store/appStore";
import { Card, Empty, Spinner, ErrorBox, Button, Badge } from "../../shared/ui";
import { fmtTs, fmtAge } from "../../shared/lib/format";
import type {
  SessionView,
  Schedule,
  WorkflowInfo,
  SessionStatsResponse,
  EndedSessionsResponse,
  ActivityItem,
} from "./types";
import { sessionArtifacts, localDateStr, scheduleInputText, SESSION_STATUS_TONE } from "./bits-helpers";

// ── 활성 세션 목록 + 활동 기록 + 통계/스케줄 — 하단 3열 그리드 ──
// 세션 선택 상태를 공유하기 위해 한 컴포넌트로 묶고, 카드 배치는
// 바깥 그리드(5:4:3)가 담당한다 — 이전 중첩 그리드는 박스 크기가
// 제각각이라 시각적으로 어긋나 보였다.
export function OfficeBottomGrid() {
  const { t } = useI18n();
  const [selKey, setSelKey] = useState<string | null>(null);
  // 세션 경과시간 표시용 현재 시각 — 10초 틱(세션 refetch 주기와 동일)
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 10000);
    return () => clearInterval(id);
  }, []);
  const { data, isLoading, error, refetch } = useQuery({
    // "active"/"all" 키 분리 — 같은 키에 다른 queryFn을 쓰면 React Query가
    // dedupe하면서 include_ended=false 결과가 모든 뷰에 캐시된다.
    queryKey: ["office-sessions", "active"],
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
    <div className="grid items-start gap-4 xl:grid-cols-[minmax(0,5fr)_minmax(0,4fr)_minmax(280px,3fr)]">
      <Card title={t("office.sessions", "활성 세션")} className="max-h-[480px] overflow-y-auto">
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
                  {s.last_event_at && <span>{fmtAge(Math.max(0, (now - new Date(s.last_event_at).getTime()) / 1000))}</span>}
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

      <div className="space-y-4">
        <SessionStatsCard />
        <ScheduleCard />
      </div>
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
        <span>{t("office.statsWork", "오늘 working")} <b className="text-ink">{Math.round(workSec / 60)}{t("office.minUnit", "분")}</b></span>
        <span>{t("office.statsDecisions", "decision")} <b className="text-ink">{decisions}{t("office.timesUnit", "회")}</b></span>
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
                  title={`${hh}${t("office.hourSuffix", "시")} ${Math.round(sec / 60)}${t("office.minuteSuffix", "분")}`}
                />
              );
            })}
          </div>
          <div className="mt-0.5 flex justify-between text-[10px] text-muted">
            <span>00{t("office.hourSuffix", "시")}</span><span>12{t("office.hourSuffix", "시")}</span><span>23{t("office.hourSuffix", "시")}</span>
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
