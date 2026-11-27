/** tasks — task 상세 사이드 패널. */
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { apiGet } from "../../../shared/api/client";
import type { Task, Project, Goal } from "../../../shared/api/types";
import { useI18n } from "../../../shared/i18n";
import { Spinner, ErrorBox, Badge, Button } from "../../../shared/ui";
import { fmtTs } from "../../../shared/lib/format";
import {
  STATUS_TONE, PRIORITY_TONE,
  statusLabel, priorityLabel, type MgmtAgentOption,
} from "../shared";
import { TaskForm } from "./TaskForm";
import { RunHistorySection } from "./RunHistorySection";
import { CommentsSection } from "./CommentsSection";
import { ArtifactsSection } from "./ArtifactsSection";

export function TaskDrawer({
  taskId,
  projects,
  goals,
  agents,
  isLead,
  onClose,
  onChanged,
}: {
  taskId: string;
  projects: Project[];
  goals: Goal[];
  agents: MgmtAgentOption[];
  isLead: boolean;
  onClose: () => void;
  onChanged: () => void;
}) {
  const { t } = useI18n();
  const [mode, setMode] = useState<"view" | "edit">("view");
  // 목록 행은 요약일 수 있어 상세를 다시 조회한다 — comments/artifacts와
  // 함께 runs/task_runs 내장 필드도 이 응답에서 온다.
  const detail = useQuery({
    queryKey: ["mgmt-task", taskId],
    queryFn: () => apiGet<Task>(`/management/tasks/${encodeURIComponent(taskId)}`),
  });
  const task = detail.data;
  if (detail.isLoading) return <Spinner />;
  if (detail.error || !task) {
    return (
      <div className="fixed inset-0 z-50">
        <div className="absolute inset-0 bg-black/30" onClick={onClose} />
        <aside className="absolute inset-y-0 right-0 w-full max-w-lg border-l border-border bg-surface p-4 shadow-xl">
          <ErrorBox
            message={detail.error instanceof Error ? detail.error.message : t("tasks.notFound", "작업을 찾을 수 없습니다")}
            onRetry={() => detail.refetch()}
          />
        </aside>
      </div>
    );
  }

  const st = String(task.status || "backlog").toLowerCase();
  const project = task.project_id ? projects.find((p) => p.id === task.project_id) : undefined;
  // effective goal — spec §3차 A: task.goal_id → project.goal_id → none
  const effectiveGoalId = task.goal_id || project?.goal_id || "";
  const effectiveGoal = effectiveGoalId ? goals.find((g) => g.id === effectiveGoalId) : undefined;

  return (
    <div className="fixed inset-0 z-50">
      <div className="absolute inset-0 bg-black/30" onClick={onClose} />
      <aside
        role="dialog"
        aria-modal="true"
        aria-label={task.title || task.id}
        className="absolute inset-y-0 right-0 flex w-full max-w-lg flex-col overflow-y-auto border-l border-border bg-surface shadow-xl"
      >
        <div className="flex items-center gap-2 border-b border-border px-4 py-3">
          <h2 className="min-w-0 flex-1 truncate text-base font-bold text-ink">
            {task.identifier ? `${task.identifier} ` : ""}{task.title || task.id}
          </h2>
          <button
            type="button"
            onClick={onClose}
            aria-label={t("tasks.close", "닫기")}
            className="rounded-md px-2 py-1 text-muted hover:bg-surface-hover"
          >
            ✕
          </button>
        </div>

        <div className="flex-1 space-y-4 p-4">
          {mode === "edit" ? (
            <TaskForm
              initial={task}
              projects={projects}
              goals={goals}
              agents={agents}
              onDone={() => {
                setMode("view");
                onChanged();
              }}
            />
          ) : (
            <>
              <div className="flex flex-wrap items-center gap-1.5">
                <Badge tone={STATUS_TONE[st] ?? "muted"}>{statusLabel(t, st)}</Badge>
                <Badge tone={PRIORITY_TONE[String(task.priority || "")] ?? "muted"}>
                  {priorityLabel(t, String(task.priority || "medium"))}
                </Badge>
                {task.assignee_agent_id && <Badge tone="accent">{task.assignee_agent_id}</Badge>}
              </div>

              {task.description && (
                <p className="whitespace-pre-wrap text-sm text-ink-2">{task.description}</p>
              )}

              <dl className="grid grid-cols-1 gap-x-4 gap-y-2 text-sm sm:grid-cols-2">
                <div>
                  <dt className="text-xs text-muted">{t("tasks.project", "프로젝트")}</dt>
                  <dd className="text-ink">{project ? project.name || project.id : "-"}</dd>
                </div>
                <div>
                  <dt className="text-xs text-muted">{t("tasks.effectiveGoal", "유효 목표")}</dt>
                  <dd className="text-ink">
                    {effectiveGoal ? effectiveGoal.title || effectiveGoal.id : "-"}
                    {task.goal_id && !project?.goal_id && (
                      <span className="ml-1 text-[11px] text-muted">({t("tasks.direct", "직접 지정")})</span>
                    )}
                    {!task.goal_id && project?.goal_id && (
                      <span className="ml-1 text-[11px] text-muted">({t("tasks.viaProject", "프로젝트 경유")})</span>
                    )}
                  </dd>
                </div>
                <div>
                  <dt className="text-xs text-muted">{t("tasks.reviewer", "리뷰어")}</dt>
                  <dd className="text-ink">{task.reviewer_agent_id || "-"}</dd>
                </div>
                <div>
                  <dt className="text-xs text-muted">{t("tasks.checkout", "체크아웃")}</dt>
                  <dd className="text-ink">{task.checkout_agent_id || "-"}</dd>
                </div>
                <div>
                  <dt className="text-xs text-muted">{t("tasks.createdAt", "생성")}</dt>
                  <dd className="text-ink">{fmtTs(task.created_at)}</dd>
                </div>
                <div>
                  <dt className="text-xs text-muted">{t("tasks.updatedAt", "수정")}</dt>
                  <dd className="text-ink">{fmtTs(task.updated_at)}</dd>
                </div>
              </dl>

              {isLead && (
                <div className="flex flex-wrap gap-2 border-t border-border pt-3">
                  <Button variant="secondary" className="px-3 py-1.5 text-xs" onClick={() => setMode("edit")}>
                    {t("tasks.edit", "편집")}
                  </Button>
                </div>
              )}

              <RunHistorySection task={task} />
              <CommentsSection taskId={task.id} />
              <ArtifactsSection taskId={task.id} />
            </>
          )}
        </div>
      </aside>
    </div>
  );
}
