/** tasks — /management/tasks 작업 목록 + 상세(댓글/산출물/실행 이력).
 *  spec §3차 F/B API 계약:
 *   - GET /management/tasks?status&assignee&project_id&goal_id
 *   - GET/PATCH /management/tasks/{id}, POST /management/tasks (lead+)
 *   - GET/POST /management/tasks/{id}/comments
 *   - GET/POST /management/tasks/{id}/artifacts
 *  author_type/author_id는 서버가 결정 — 클라이언트는 body만 보낸다.
 *  artifact URI는 백엔드가 검증하지만 렌더링도 safeUrl로 한 번 더 거른다.
 *  페이지 셸만 담당 — 상수/라벨은 shared.ts, 폼·섹션·드로어는 components/. */
import { useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { apiGet } from "../../shared/api/client";
import type {
  TasksResponse, GoalsResponse, ProjectsResponse,
} from "../../shared/api/types";
import { useI18n } from "../../shared/i18n";
import { PageHead, Card, Empty, Spinner, ErrorBox, Badge, Button } from "../../shared/ui";
import { fmtElapsed, fmtTs } from "../../shared/lib/format";
import { roleAllows, useAppStore } from "../../shared/store/appStore";
import { useObsFunnel } from "../office/queries";
import type { ObsFunnelStage } from "../office/types";
import {
  TASK_STATUSES, STATUS_TONE, PRIORITY_TONE,
  statusLabel, priorityLabel, type MgmtAgentOption,
} from "./shared";
import { TaskForm } from "./components/TaskForm";
import { TaskDrawer } from "./components/TaskDrawer";
import "./i18n";

const ACTIVE_STATUSES = new Set(["in_progress", "in_review", "waiting_approval"]);

/* ── spec 014 US4 — KPI 행 (필터 무관 전체 집계, 최근 200건) ── */
function TasksKpis() {
  const { t } = useI18n();
  const { data, isLoading } = useQuery({
    queryKey: ["mgmt-tasks", "kpi"],
    queryFn: () => apiGet<TasksResponse>("/management/tasks?limit=200"),
    refetchInterval: 60000,
    retry: false,
  });
  const counts = useMemo(() => {
    const items = Array.isArray(data?.items) ? data.items : [];
    const c = { total: items.length, active: 0, attention: 0, done: 0 };
    for (const task of items) {
      const st = String(task.status || "").toLowerCase();
      if (ACTIVE_STATUSES.has(st)) c.active += 1;
      if (st === "waiting_approval" || st === "blocked") c.attention += 1;
      if (st === "done") c.done += 1;
    }
    return c;
  }, [data]);

  const cards = [
    { label: t("tasks.kpi.total", "전체 작업"), value: counts.total, tone: "text-ink" },
    { label: t("tasks.kpi.active", "진행 중"), value: counts.active, tone: "text-accent" },
    { label: t("tasks.kpi.attention", "승인·차단"), value: counts.attention, tone: "text-warn" },
    { label: t("tasks.kpi.done", "완료"), value: counts.done, tone: "text-ok" },
  ];

  if (isLoading) return <div className="mb-3"><Spinner /></div>;
  if (!data) return null; // 권한/오류 시 KPI 행 자체를 숨긴다 — 가짜 0 금지

  return (
    <div className="mb-3 grid grid-cols-2 gap-2 sm:grid-cols-4">
      {cards.map((c) => (
        <div key={c.label} className="rounded-lg border border-border bg-surface-hover px-2.5 py-1.5">
          <div className="text-[10px] text-muted">{c.label}</div>
          <div className={`text-base font-semibold tabular-nums ${c.tone}`}>{c.value.toLocaleString()}</div>
          <div className="truncate text-[10px] text-muted">{t("tasks.kpi.sub", "최근 200건 기준")}</div>
        </div>
      ))}
    </div>
  );
}

/* ── spec 014 US4 — 실행 퍼널 (lead 전용, /observability/funnel) ── */
function TasksFunnel() {
  const { t } = useI18n();
  const q = useObsFunnel(30);
  const stages = useMemo(
    () => (Array.isArray(q.data?.stages) ? q.data.stages : []),
    [q.data],
  );
  const first = stages[0]?.count ?? 0;
  const maxCount = Math.max(1e-9, ...stages.map((x: ObsFunnelStage) => Number(x.count) || 0));

  return (
    <Card
      title={t("tasks.funnel.title", "실행 퍼널")}
      className="mb-3"
      actions={<span className="text-[10px] text-muted">{t("tasks.funnel.note", "최근 30일 · received→executed")}</span>}
    >
      {q.isLoading ? <Spinner /> : q.error ? (
        <div className="text-xs text-muted">{t("tasks.funnel.unavailable", "퍼널 데이터를 불러오지 못했습니다")}</div>
      ) : !stages.length ? (
        <Empty>{t("tasks.funnel.empty", "퍼널 데이터가 없습니다")}</Empty>
      ) : (
        <div className="space-y-2">
          {stages.map((s: ObsFunnelStage, i: number) => {
            const count = Number(s.count) || 0;
            const pctOfFirst = first > 0 ? (count / first) * 100 : 0;
            const width = Math.max(1.5, (count / maxCount) * 100);
            return (
              <div key={s.key || i}>
                <div className="flex items-baseline justify-between gap-2 text-xs">
                  <span className="min-w-0 truncate font-medium text-ink">{s.label || s.key || `stage ${i + 1}`}</span>
                  <span className="flex-none tabular-nums text-muted">
                    {count.toLocaleString()}
                    <span className="ml-1 text-[10px]">({pctOfFirst.toFixed(0)}%)</span>
                  </span>
                </div>
                <div className="mt-0.5 h-2 overflow-hidden rounded-full bg-surface-hover">
                  <div className={`h-full rounded-full ${i === stages.length - 1 ? "bg-ok/80" : "bg-accent/80"}`} style={{ width: `${width}%` }} />
                </div>
              </div>
            );
          })}
        </div>
      )}
    </Card>
  );
}

/* ── spec 014 US4 — 진행 중 작업 카드 (상태+경과만 — 진행률 데이터 없음) ── */
function ActiveTaskCards({ items, onOpen }: {
  items: NonNullable<TasksResponse["items"]>;
  onOpen: (id: string) => void;
}) {
  const { t } = useI18n();
  const active = items.filter((task) => ACTIVE_STATUSES.has(String(task.status || "").toLowerCase()));
  if (!active.length) return null;
  return (
    <Card title={t("tasks.active.title", "진행 중 작업")} className="mb-3">
      <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
        {active.map((task) => {
          const st = String(task.status || "").toLowerCase();
          return (
            <button
              key={task.id}
              type="button"
              onClick={() => onOpen(task.id)}
              className="rounded-lg border border-border bg-surface p-2.5 text-left transition-colors hover:bg-surface-hover"
            >
              <div className="flex items-center gap-1.5">
                <Badge tone={STATUS_TONE[st] ?? "muted"}>{statusLabel(t, st)}</Badge>
                <span className="min-w-0 flex-1 truncate text-xs font-medium text-ink">
                  {task.identifier ? `${task.identifier} ` : ""}{task.title || task.id}
                </span>
              </div>
              <div className="mt-1.5 flex items-center justify-between text-[10px] text-muted">
                <span className="truncate">{task.assignee_agent_id || t("tasks.unassigned", "미지정")}</span>
                <span className="flex-none tabular-nums">{t("tasks.active.elapsed", "경과")} {fmtElapsed(task.updated_at || task.created_at)}</span>
              </div>
            </button>
          );
        })}
      </div>
    </Card>
  );
}

function TasksBody() {
  const { t } = useI18n();
  const qc = useQueryClient();
  const role = useAppStore((s) => s.role);
  const isLead = roleAllows("lead", role ?? "member");
  const [statusFilter, setStatusFilter] = useState("all");
  const [assigneeFilter, setAssigneeFilter] = useState("");
  const [projectFilter, setProjectFilter] = useState("");
  const [goalFilter, setGoalFilter] = useState("");
  const [showForm, setShowForm] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const params = useMemo(() => {
    const q = new URLSearchParams();
    q.set("limit", "200");
    if (statusFilter !== "all") q.set("status", statusFilter);
    if (assigneeFilter) {
      // spec §3차 F는 파라미터명을 규정하지 않는다 — 기존 backend의
      // `assignee`와 3차 명세 추정명 `assignee_agent_id`를 함께 보낸다.
      q.set("assignee", assigneeFilter);
      q.set("assignee_agent_id", assigneeFilter);
    }
    if (projectFilter) q.set("project_id", projectFilter);
    if (goalFilter) q.set("goal_id", goalFilter);
    return q.toString();
  }, [statusFilter, assigneeFilter, projectFilter, goalFilter]);

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["mgmt-tasks", params],
    queryFn: () => apiGet<TasksResponse>(`/management/tasks?${params}`),
    refetchInterval: 60000,
  });
  const items = useMemo(() => (Array.isArray(data?.items) ? data.items : []), [data]);

  // spec 014 US4 — KPI/진행 중 카드는 현재 필터와 무관한 전체 목록 기준.
  // TasksKpis와 동일 queryKey라 React Query가 요청을 공유한다.
  const allTasksQ = useQuery({
    queryKey: ["mgmt-tasks", "kpi"],
    queryFn: () => apiGet<TasksResponse>("/management/tasks?limit=200"),
    refetchInterval: 60000,
    retry: false,
  });
  const allItems = useMemo(
    () => (Array.isArray(allTasksQ.data?.items) ? allTasksQ.data.items : []),
    [allTasksQ.data],
  );

  // 필터 select 후보 — projects/goals는 member도 읽기 가능, agents는 lead+.
  // /projects·/management/agents는 lead+ — member는 비활성(403 폴링 방지)
  const projectsQ = useQuery({
    queryKey: ["projects"],
    queryFn: () => apiGet<ProjectsResponse>("/projects"),
    staleTime: 60000,
    retry: false,
    enabled: isLead,
  });
  const goalsQ = useQuery({
    queryKey: ["goals"],
    queryFn: () => apiGet<GoalsResponse>("/goals"),
    staleTime: 60000,
    retry: false,
  });
  const agentsQ = useQuery({
    queryKey: ["mgmt-agents"],
    queryFn: () => apiGet<{ items?: MgmtAgentOption[] }>("/management/agents"),
    staleTime: 60000,
    retry: false,
    enabled: isLead,
  });
  const projects = useMemo(() => (Array.isArray(projectsQ.data?.items) ? projectsQ.data.items : []), [projectsQ.data]);
  const goals = useMemo(() => (Array.isArray(goalsQ.data?.items) ? goalsQ.data.items : []), [goalsQ.data]);
  const agents = useMemo(() => (Array.isArray(agentsQ.data?.items) ? agentsQ.data.items : []), [agentsQ.data]);

  const selCls = "rounded-lg border border-border bg-surface px-2 py-1.5 text-sm";

  return (
    <div className="mx-auto max-w-6xl p-4">
      <PageHead
        title={t("tasks.title", "작업")}
        desc={t("tasks.desc", "관리 작업 — 상태·담당·프로젝트·목표별 목록과 상세")}
      >
        <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)} aria-label={t("tasks.statusAll", "전체 상태")} className={selCls}>
          <option value="all">{t("tasks.statusAll", "전체 상태")}</option>
          {TASK_STATUSES.map((s) => (
            <option key={s} value={s}>{statusLabel(t, s)}</option>
          ))}
        </select>
        <select value={assigneeFilter} onChange={(e) => setAssigneeFilter(e.target.value)} aria-label={t("tasks.assigneeAll", "전체 담당")} className={selCls}>
          <option value="">{t("tasks.assigneeAll", "전체 담당")}</option>
          {agents.map((a) => (
            <option key={a.id} value={a.id}>{a.name || a.id}</option>
          ))}
        </select>
        <select value={projectFilter} onChange={(e) => setProjectFilter(e.target.value)} aria-label={t("tasks.projectAll", "전체 프로젝트")} className={selCls}>
          <option value="">{t("tasks.projectAll", "전체 프로젝트")}</option>
          {projects.map((p) => (
            <option key={p.id} value={p.id}>{p.name || p.id}</option>
          ))}
        </select>
        <select value={goalFilter} onChange={(e) => setGoalFilter(e.target.value)} aria-label={t("tasks.goalAll", "전체 목표")} className={selCls}>
          <option value="">{t("tasks.goalAll", "전체 목표")}</option>
          {goals.map((g) => (
            <option key={g.id} value={g.id}>{g.title || g.id}</option>
          ))}
        </select>
        {isLead && (
          <Button variant="secondary" onClick={() => setShowForm((v) => !v)}>
            {showForm ? t("common.close", "닫기") : "+ " + t("tasks.add", "작업 추가")}
          </Button>
        )}
        <Button variant="secondary" onClick={() => refetch()} className="px-3 py-1.5 text-xs">
          {t("common.refresh", "새로고침")}
        </Button>
      </PageHead>

      <TasksKpis />
      {isLead && <TasksFunnel />}
      <ActiveTaskCards items={allItems} onOpen={setSelectedId} />

      {showForm && isLead && (
        <Card className="mb-3" title={t("tasks.add", "작업 추가")}>
          <TaskForm
            projects={projects}
            goals={goals}
            agents={agents}
            onDone={() => {
              setShowForm(false);
              qc.invalidateQueries({ queryKey: ["mgmt-tasks"] });
            }}
          />
        </Card>
      )}

      <Card>
        {isLoading && <Spinner />}
        {error && (
          <ErrorBox message={error instanceof Error ? error.message : String(error)} onRetry={() => refetch()} />
        )}
        {data && !items.length && <Empty>{t("tasks.empty", "조건에 맞는 작업이 없습니다")}</Empty>}
        {items.length > 0 && (
          <ul className="divide-y divide-border">
            {items.map((task) => {
              const st = String(task.status || "").toLowerCase();
              const proj = task.project_id ? projects.find((p) => p.id === task.project_id) : undefined;
              const goal = task.goal_id ? goals.find((g) => g.id === task.goal_id) : undefined;
              return (
                <li key={task.id}>
                  <button
                    type="button"
                    onClick={() => setSelectedId(task.id === selectedId ? null : task.id)}
                    className={`flex w-full items-center gap-2 py-2.5 text-left hover:bg-surface-hover ${selectedId === task.id ? "bg-accent-soft" : ""}`}
                  >
                    <Badge tone={STATUS_TONE[st] ?? "muted"}>{statusLabel(t, st)}</Badge>
                    <Badge tone={PRIORITY_TONE[String(task.priority || "")] ?? "muted"}>
                      {priorityLabel(t, String(task.priority || "medium"))}
                    </Badge>
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-sm font-medium text-ink">
                        {task.identifier ? `${task.identifier} ` : ""}{task.title || task.id}
                      </div>
                      <div className="truncate text-xs text-muted">
                        {task.assignee_agent_id || t("tasks.unassigned", "미지정")}
                        {proj ? ` · ${proj.name || proj.id}` : ""}
                        {goal ? ` · ${goal.title || goal.id}` : ""}
                      </div>
                    </div>
                    <span className="shrink-0 text-[11px] text-muted">{fmtTs(task.updated_at || task.created_at)}</span>
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </Card>

      {selectedId && (
        <TaskDrawer
          key={selectedId}
          taskId={selectedId}
          projects={projects}
          goals={goals}
          agents={agents}
          isLead={isLead}
          onClose={() => setSelectedId(null)}
          onChanged={() => qc.invalidateQueries({ queryKey: ["mgmt-tasks"] })}
        />
      )}
    </div>
  );
}

export default function TasksPage() {
  return <TasksBody />;
}
