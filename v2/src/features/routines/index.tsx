/** routines — /management/routines 반복 작업 스케줄러 (spec §3차 C/F).
 *  API 계약: CRUD /management/routines — GET 목록, POST 생성,
 *  GET/PATCH/DELETE /management/routines/{id}.
 *  routine_runs 이력은 spec이 전용 목록 엔드포인트를 정의하지 않아
 *  routine detail 응답에 내장된 runs/routine_runs 필드를 렌더한다.
 *  schedule JSON 형태는 spec이 필드만 규정 — UI는 아래 형태를 사용한다:
 *   interval    → {interval_minutes}
 *   cron_daily  → {time: "HH:MM"}
 *   cron_weekly → {time: "HH:MM", weekday: 0-6 (월=0 — backend date.weekday() 규약)} */
import { useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { apiGet, apiPost, apiPatch, apiDelete, ApiError } from "../../shared/api/client";
import type {
  Routine, RoutinesResponse, RoutineRun, Project, ProjectsResponse,
} from "../../shared/api/types";
import { useI18n } from "../../shared/i18n";
import { PageHead, Card, Empty, Spinner, ErrorBox, Badge, Button } from "../../shared/ui";
import { fmtTs } from "../../shared/lib/format";
import { roleAllows, useAppStore } from "../../shared/store/appStore";
import "./i18n";

const SCHEDULE_KINDS = ["interval", "cron_daily", "cron_weekly"];
/** backend services/routines.py는 Python date.weekday() 규약 — 0=월..6=일. */
const WEEKDAYS = [0, 1, 2, 3, 4, 5, 6];
/** 흔한 IANA 타임존 datalist — 전체 목록은 브라우저/OS가 제공하므로
 *  여기서는 대표값만 제안한다. */
const TZ_SUGGESTIONS = [
  "Asia/Seoul", "Asia/Tokyo", "Asia/Shanghai", "Asia/Singapore",
  "Asia/Kolkata", "Europe/London", "Europe/Berlin", "Europe/Paris",
  "America/New_York", "America/Chicago", "America/Los_Angeles",
  "America/Sao_Paulo", "Australia/Sydney", "UTC",
];

const RUN_TONE: Record<string, "ok" | "warn" | "err" | "muted" | "accent"> = {
  pending: "muted",
  created: "ok",
  skipped: "warn",
  failed: "err",
};

const inputCls =
  "w-full rounded-lg border border-border bg-surface px-2.5 py-1.5 text-sm outline-none focus:border-accent";
const smallBtn = "px-3 py-1 text-xs";

interface MgmtAgentOption { id: string; name?: string }

function scheduleSummary(t: (k: string, f?: string) => string, r: Routine): string {
  const kind = String(r.schedule_kind || "");
  const s = (r.schedule || {}) as Record<string, unknown>;
  if (kind === "interval") {
    const m = Number(s.interval_minutes ?? s.every_minutes ?? 0);
    return m > 0 ? t("routines.everyMinutes", "{n}분마다").replace("{n}", String(m)) : kind;
  }
  if (kind === "cron_daily") {
    return `${t("routines.daily", "매일")} ${String(s.time || "")}`.trim();
  }
  if (kind === "cron_weekly") {
    const wd = Number(s.weekday ?? s.wday ?? 0);
    const day = Number.isInteger(wd) && wd >= 0 && wd <= 6
      ? t(`routines.wd.${wd}`, String(s.weekday ?? ""))
      : String(s.weekday ?? "");
    return `${t("routines.weekly", "매주")} ${day} ${String(s.time || "")}`.trim();
  }
  return kind || "-";
}

function runStatusLabel(t: (k: string, f?: string) => string, st: string): string {
  return t(`routines.runSt.${st}`, st);
}

/** 스케줄 파라미터 → schedule JSON. */
function buildSchedule(kind: string, intervalMin: string, time: string, weekday: string): Record<string, unknown> | null {
  if (kind === "interval") {
    const n = Number(intervalMin);
    return n > 0 ? { interval_minutes: Math.floor(n) } : null;
  }
  if (kind === "cron_daily") {
    return /^\d{1,2}:\d{2}$/.test(time.trim()) ? { time: time.trim() } : null;
  }
  if (kind === "cron_weekly") {
    const wd = Number(weekday);
    return /^\d{1,2}:\d{2}$/.test(time.trim()) && Number.isInteger(wd) && wd >= 0 && wd <= 6
      ? { time: time.trim(), weekday: wd }
      : null;
  }
  return null;
}

/** routine 등록/수정 폼 — task_template 필드 포함. */
function RoutineForm({
  initial,
  projects,
  agents,
  onDone,
}: {
  initial?: Routine | null;
  projects: Project[];
  agents: MgmtAgentOption[];
  onDone: (saved?: Routine) => void;
}) {
  const { t } = useI18n();
  const qc = useQueryClient();
  const sched = (initial?.schedule || {}) as Record<string, unknown>;
  const tpl = (initial?.task_template || {}) as Record<string, unknown>;
  const [name, setName] = useState(initial?.name || "");
  const [desc, setDesc] = useState(initial?.description || "");
  const [projectId, setProjectId] = useState(initial?.project_id || "");
  const [enabled, setEnabled] = useState(initial ? initial.enabled !== false : true);
  const [kind, setKind] = useState<string>(initial?.schedule_kind || "interval");
  const [intervalMin, setIntervalMin] = useState(String(sched.interval_minutes ?? sched.every_minutes ?? "60"));
  const [time, setTime] = useState(String(sched.time || "09:00"));
  const [weekday, setWeekday] = useState(String(sched.weekday ?? "1"));
  const [tz, setTz] = useState(initial?.timezone || Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC");
  const [maxConcurrent, setMaxConcurrent] = useState(String(initial?.max_concurrent ?? 1));
  const [tplTitle, setTplTitle] = useState(String(tpl.title || ""));
  const [tplDesc, setTplDesc] = useState(String(tpl.description || ""));
  const [tplPriority, setTplPriority] = useState(String(tpl.priority || "medium"));
  const [assignee, setAssignee] = useState(initial?.assignee_agent_id || "");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  const submit = async () => {
    if (!name.trim()) {
      setErr(t("routines.needName", "이름을 입력하세요"));
      return;
    }
    if (!tplTitle.trim()) {
      setErr(t("routines.needTplTitle", "생성할 작업 제목을 입력하세요"));
      return;
    }
    const schedule = buildSchedule(kind, intervalMin, time, weekday);
    if (!schedule) {
      setErr(t("routines.badSchedule", "스케줄 파라미터를 확인하세요 (시간 HH:MM, 주기 양의 정수)"));
      return;
    }
    const body: Record<string, unknown> = {
      name: name.trim(),
      description: desc.trim(),
      enabled,
      project_id: projectId || null,
      assignee_agent_id: assignee || null,
      schedule_kind: kind,
      schedule,
      timezone: tz.trim() || "UTC",
      max_concurrent: Math.max(1, Math.floor(Number(maxConcurrent) || 1)),
      task_template: {
        title: tplTitle.trim(),
        description: tplDesc.trim(),
        priority: tplPriority,
        ...(assignee ? { assignee_agent_id: assignee } : {}),
      },
    };

    setBusy(true);
    setErr("");
    try {
      let saved: Routine | undefined;
      if (initial?.id) {
        saved = await apiPatch<Routine>(`/management/routines/${encodeURIComponent(initial.id)}`, body);
      } else {
        saved = await apiPost<Routine>("/management/routines", body);
      }
      await qc.invalidateQueries({ queryKey: ["mgmt-routines"] });
      await qc.invalidateQueries({ queryKey: ["mgmt-routine"] });
      onDone(saved);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-2.5">
      <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-2">
        <label className="block sm:col-span-2">
          <span className="mb-1 block text-xs font-medium text-muted">{t("routines.name", "이름")}</span>
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder={t("routines.namePh", "예: 매일 아침 리포트")} className={inputCls} />
        </label>
        <label className="block">
          <span className="mb-1 block text-xs font-medium text-muted">{t("routines.project", "프로젝트 (선택)")}</span>
          <select value={projectId} onChange={(e) => setProjectId(e.target.value)} className={inputCls}>
            <option value="">{t("routines.noProject", "없음")}</option>
            {projects.map((p) => (
              <option key={p.id} value={p.id}>{p.name || p.id}</option>
            ))}
          </select>
        </label>
        <label className="block">
          <span className="mb-1 block text-xs font-medium text-muted">{t("routines.assignee", "담당 에이전트 (선택)")}</span>
          <select value={assignee} onChange={(e) => setAssignee(e.target.value)} className={inputCls}>
            <option value="">{t("routines.unassigned", "미지정")}</option>
            {agents.map((a) => (
              <option key={a.id} value={a.id}>{a.name || a.id}</option>
            ))}
          </select>
        </label>
      </div>

      <div className="rounded-lg border border-border bg-surface-hover p-3">
        <div className="mb-2 text-xs font-semibold text-muted">{t("routines.scheduleSection", "스케줄")}</div>
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
          <label className="block">
            <span className="mb-1 block text-xs font-medium text-muted">{t("routines.kind", "스케줄 종류")}</span>
            <select value={kind} onChange={(e) => setKind(e.target.value)} className={inputCls}>
              {SCHEDULE_KINDS.map((k) => (
                <option key={k} value={k}>
                  {k === "interval" ? t("routines.kindInterval", "주기 (분)")
                    : k === "cron_daily" ? t("routines.kindDaily", "매일")
                    : t("routines.kindWeekly", "매주")}
                </option>
              ))}
            </select>
          </label>
          {kind === "interval" && (
            <label className="block">
              <span className="mb-1 block text-xs font-medium text-muted">{t("routines.intervalMin", "간격 (분)")}</span>
              <input value={intervalMin} onChange={(e) => setIntervalMin(e.target.value)} type="number" min="1" className={inputCls} />
            </label>
          )}
          {kind !== "interval" && (
            <label className="block">
              <span className="mb-1 block text-xs font-medium text-muted">{t("routines.time", "시각 (HH:MM)")}</span>
              <input value={time} onChange={(e) => setTime(e.target.value)} placeholder="09:00" className={inputCls} />
            </label>
          )}
          {kind === "cron_weekly" && (
            <label className="block">
              <span className="mb-1 block text-xs font-medium text-muted">{t("routines.weekday", "요일")}</span>
              <select value={weekday} onChange={(e) => setWeekday(e.target.value)} className={inputCls}>
                {WEEKDAYS.map((d) => (
                  <option key={d} value={d}>{t(`routines.wd.${d}`, String(d))}</option>
                ))}
              </select>
            </label>
          )}
          <label className="block">
            <span className="mb-1 block text-xs font-medium text-muted">{t("routines.timezone", "타임존 (IANA)")}</span>
            <input value={tz} onChange={(e) => setTz(e.target.value)} list="routine-tz" placeholder="Asia/Seoul" className={inputCls} />
            <datalist id="routine-tz">
              {TZ_SUGGESTIONS.map((z) => <option key={z} value={z} />)}
            </datalist>
          </label>
          <label className="block">
            <span className="mb-1 block text-xs font-medium text-muted">{t("routines.maxConcurrent", "최대 동시 실행")}</span>
            <input value={maxConcurrent} onChange={(e) => setMaxConcurrent(e.target.value)} type="number" min="1" className={inputCls} />
          </label>
        </div>
      </div>

      <div className="rounded-lg border border-border bg-surface-hover p-3">
        <div className="mb-2 text-xs font-semibold text-muted">{t("routines.taskTemplate", "생성할 작업 템플릿")}</div>
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
          <label className="block sm:col-span-2">
            <span className="mb-1 block text-xs font-medium text-muted">{t("routines.tplTitle", "작업 제목")}</span>
            <input value={tplTitle} onChange={(e) => setTplTitle(e.target.value)} className={inputCls} />
          </label>
          <label className="block sm:col-span-2">
            <span className="mb-1 block text-xs font-medium text-muted">{t("routines.tplDesc", "작업 설명")}</span>
            <textarea value={tplDesc} onChange={(e) => setTplDesc(e.target.value)} rows={2} className={inputCls} />
          </label>
          <label className="block">
            <span className="mb-1 block text-xs font-medium text-muted">{t("routines.tplPriority", "우선순위")}</span>
            <select value={tplPriority} onChange={(e) => setTplPriority(e.target.value)} className={inputCls}>
              {["low", "medium", "high", "urgent"].map((p) => (
                <option key={p} value={p}>{t(`tasks.pri.${p}`, p)}</option>
              ))}
            </select>
          </label>
          <label className="flex items-end gap-2 pb-1 text-sm text-ink-2">
            <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
            {t("routines.enabled", "활성화")}
          </label>
        </div>
        <label className="mt-2 block">
          <span className="mb-1 block text-xs font-medium text-muted">{t("routines.description", "설명 (선택)")}</span>
          <input value={desc} onChange={(e) => setDesc(e.target.value)} className={inputCls} />
        </label>
      </div>

      {err && <p className="text-xs text-err">{err}</p>}
      <div className="flex items-center justify-end gap-2">
        <Button variant="secondary" className={smallBtn} onClick={() => onDone()} disabled={busy}>
          {t("common.cancel", "취소")}
        </Button>
        <Button className={smallBtn} disabled={busy} onClick={submit}>
          {busy ? t("common.saving", "저장 중…") : t("common.save", "저장")}
        </Button>
      </div>
    </div>
  );
}

/** routine_runs 이력 — detail 응답의 runs/routine_runs 내장 필드만 사용
 *  (spec §3차 F에 전용 목록 엔드포인트가 없다). */
function RoutineRunsSection({ routineId, embedded }: { routineId: string; embedded?: Routine }) {
  const { t } = useI18n();
  // 내장 필드가 없으면 detail을 한 번 더 조회해 확인한다.
  const detail = useQuery({
    queryKey: ["mgmt-routine", routineId],
    queryFn: () => apiGet<Routine>(`/management/routines/${encodeURIComponent(routineId)}`),
    enabled: !(embedded?.runs?.length || embedded?.routine_runs?.length),
  });
  const src = embedded?.routine_runs?.length ? embedded.routine_runs
    : embedded?.runs?.length ? embedded.runs
    : detail.data?.routine_runs?.length ? detail.data.routine_runs
    : detail.data?.runs || [];
  const runs: RoutineRun[] = Array.isArray(src) ? src : [];

  return (
    <div className="mt-2">
      <div className="mb-1 text-xs font-semibold text-muted">{t("routines.runs", "실행 이력")}</div>
      {detail.isLoading && <div className="text-xs text-muted">{t("common.loading", "로딩…")}</div>}
      {!runs.length && !detail.isLoading && (
        <div className="text-xs text-muted">{t("routines.noRuns", "실행 기록이 없습니다")}</div>
      )}
      <ul className="max-h-56 space-y-1 overflow-y-auto">
        {runs.map((r) => (
          <li key={r.id} className="flex flex-wrap items-center gap-2 rounded-md border border-border bg-surface-hover px-2.5 py-1.5 text-xs">
            <Badge tone={RUN_TONE[String(r.status || "")] ?? "muted"}>
              {runStatusLabel(t, String(r.status || ""))}
            </Badge>
            <span className="text-muted">{t("routines.scheduledFor", "예정")} {fmtTs(r.scheduled_for)}</span>
            {r.task_id && <span className="font-mono text-ink">task:{String(r.task_id).slice(0, 8)}</span>}
            {r.error_code && <span className="text-err">{r.error_code}</span>}
            <span className="ml-auto text-[11px] text-muted">{fmtTs(r.finished_at || r.created_at)}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function RoutineCard({
  routine,
  projects,
  agents,
  isLead,
  onChanged,
}: {
  routine: Routine;
  projects: Project[];
  agents: MgmtAgentOption[];
  isLead: boolean;
  onChanged: () => void;
}) {
  const { t } = useI18n();
  const qc = useQueryClient();
  const [mode, setMode] = useState<"view" | "edit">("view");
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const r = routine;
  const project = r.project_id ? projects.find((p) => p.id === r.project_id) : undefined;
  const tpl = (r.task_template || {}) as Record<string, unknown>;

  const toggle = async () => {
    setBusy(true);
    try {
      await apiPatch(`/management/routines/${encodeURIComponent(r.id)}`, { enabled: !r.enabled });
      await qc.invalidateQueries({ queryKey: ["mgmt-routines"] });
      onChanged();
    } catch (e) {
      alert(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    if (!confirm(t("routines.confirmDelete", "'{name}' 루틴을 삭제할까요?").replace("{name}", r.name || r.id))) return;
    setBusy(true);
    try {
      await apiDelete(`/management/routines/${encodeURIComponent(r.id)}`);
      await qc.invalidateQueries({ queryKey: ["mgmt-routines"] });
      onChanged();
    } catch (e) {
      alert(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  if (mode === "edit" && isLead) {
    return (
      <Card className="mb-3">
        <RoutineForm initial={r} projects={projects} agents={agents} onDone={() => { setMode("view"); onChanged(); }} />
      </Card>
    );
  }

  return (
    <Card className="mb-3">
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          className="min-w-0 flex-1 text-left"
          aria-expanded={open}
        >
          <span className="text-sm font-semibold text-ink">{r.name || r.id}</span>
          <span className="ml-2 text-xs text-muted">{scheduleSummary(t, r)}</span>
        </button>
        <Badge tone={r.enabled ? "ok" : "muted"}>
          {r.enabled ? t("routines.on", "활성") : t("routines.off", "비활성")}
        </Badge>
        {project && <Badge>{project.name || project.id}</Badge>}
        {isLead && (
          <span className="flex gap-1">
            <Button variant="secondary" className="px-2 py-0.5 text-[11px]" disabled={busy} onClick={toggle}>
              {r.enabled ? t("routines.disable", "비활성화") : t("routines.enable", "활성화")}
            </Button>
            <Button variant="ghost" className="px-2 py-0.5 text-[11px]" onClick={() => setMode("edit")}>
              {t("routines.edit", "편집")}
            </Button>
            <Button variant="ghost" className="px-2 py-0.5 text-[11px] text-err" disabled={busy} onClick={remove}>
              {t("common.delete", "삭제")}
            </Button>
          </span>
        )}
      </div>
      <div className="mt-1.5 flex flex-wrap gap-x-4 gap-y-0.5 text-xs text-muted">
        <span>{t("routines.timezone", "타임존")} {r.timezone || "UTC"}</span>
        <span>{t("routines.maxConcurrent", "최대 동시 실행")} {r.max_concurrent ?? 1}</span>
        <span>{t("routines.nextRun", "다음 실행")} {fmtTs(r.next_run_at)}</span>
        <span>{t("routines.lastRun", "마지막 실행")} {fmtTs(r.last_run_at)}</span>
        {tpl.title != null && (
          <span className="truncate">
            {t("routines.tpl", "작업")} {String(tpl.title)}
            {tpl.priority ? ` (${String(tpl.priority)})` : ""}
          </span>
        )}
      </div>
      {r.description && <p className="mt-1 text-xs text-muted">{r.description}</p>}
      {open && <RoutineRunsSection routineId={r.id} embedded={r} />}
    </Card>
  );
}

function RoutinesBody() {
  const { t } = useI18n();
  const role = useAppStore((s) => s.role);
  const isLead = roleAllows("lead", role ?? "member");
  const [showForm, setShowForm] = useState(false);
  const [enabledOnly, setEnabledOnly] = useState(false);

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["mgmt-routines"],
    queryFn: () => apiGet<RoutinesResponse>("/management/routines"),
    refetchInterval: 60000,
  });
  const items = useMemo(() => {
    const all = Array.isArray(data?.items) ? data.items : [];
    return enabledOnly ? all.filter((r) => r.enabled !== false) : all;
  }, [data, enabledOnly]);

  const projectsQ = useQuery({
    queryKey: ["projects"],
    queryFn: () => apiGet<ProjectsResponse>("/projects"),
    staleTime: 60000,
    retry: false,
  });
  const projects = useMemo(() => (Array.isArray(projectsQ.data?.items) ? projectsQ.data.items : []), [projectsQ.data]);
  const agentsQ = useQuery({
    queryKey: ["mgmt-agents"],
    queryFn: () => apiGet<{ items?: MgmtAgentOption[] }>("/management/agents"),
    staleTime: 60000,
    retry: false,
  });
  const agents = useMemo(() => (Array.isArray(agentsQ.data?.items) ? agentsQ.data.items : []), [agentsQ.data]);

  const qc = useQueryClient();

  return (
    <div className="mx-auto max-w-4xl p-4">
      <PageHead
        title={t("routines.title", "루틴")}
        desc={t("routines.desc", "예약 작업 — 스케줄에 따라 task를 자동 생성")}
      >
        <label className="flex items-center gap-1.5 text-sm text-muted">
          <input type="checkbox" checked={enabledOnly} onChange={(e) => setEnabledOnly(e.target.checked)} />
          {t("routines.enabledOnly", "활성만")}
        </label>
        {isLead && (
          <Button variant="secondary" onClick={() => setShowForm((v) => !v)}>
            {showForm ? t("common.close", "닫기") : "+ " + t("routines.add", "루틴 추가")}
          </Button>
        )}
        <Button variant="secondary" onClick={() => refetch()} className="px-3 py-1.5 text-xs">
          {t("common.refresh", "새로고침")}
        </Button>
      </PageHead>

      {showForm && isLead && (
        <Card className="mb-3" title={t("routines.add", "루틴 추가")}>
          <RoutineForm
            projects={projects}
            agents={agents}
            onDone={() => {
              setShowForm(false);
              qc.invalidateQueries({ queryKey: ["mgmt-routines"] });
            }}
          />
        </Card>
      )}

      {isLoading && <Spinner />}
      {error && (
        <ErrorBox message={error instanceof Error ? error.message : String(error)} onRetry={() => refetch()} />
      )}
      {data && !items.length && <Empty>{t("routines.empty", "등록된 루틴이 없습니다")}</Empty>}
      {items.map((r) => (
        <RoutineCard
          key={r.id}
          routine={r}
          projects={projects}
          agents={agents}
          isLead={isLead}
          onChanged={() => qc.invalidateQueries({ queryKey: ["mgmt-routines"] })}
        />
      ))}
    </div>
  );
}

export default function RoutinesPage() {
  return <RoutinesBody />;
}
