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
import { fmtTs } from "../../shared/lib/format";
import { roleAllows, useAppStore } from "../../shared/store/appStore";
import {
  TASK_STATUSES, STATUS_TONE, PRIORITY_TONE,
  statusLabel, priorityLabel, type MgmtAgentOption,
} from "./shared";
import { TaskForm } from "./components/TaskForm";
import { TaskDrawer } from "./components/TaskDrawer";
import "./i18n";

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

  // 필터 select 후보 — projects/goals는 member도 읽기 가능, agents는 lead+.
  const projectsQ = useQuery({
    queryKey: ["projects"],
    queryFn: () => apiGet<ProjectsResponse>("/projects"),
    staleTime: 60000,
    retry: false,
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
