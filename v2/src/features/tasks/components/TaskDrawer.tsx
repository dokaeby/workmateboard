/** tasks — task 상세 사이드 패널. */
import { useEffect, useRef, useState, type MouseEvent, type PointerEvent } from "react";
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

// 네이티브 <dialog> 사이드 패널 — 우측 도킹·전체 높이. UA dialog의
// 중앙정렬(inset/margin auto)과 max 제약을 덮어쓴다.
const DRAWER_DIALOG_CLS =
  "start-auto end-0 top-0 bottom-0 m-0 h-dvh w-full max-w-lg max-h-none overflow-y-auto " +
  "border-l border-border bg-surface text-ink shadow-xl open:flex flex-col " +
  "[&:modal]:overscroll-contain [&::backdrop]:overflow-hidden " +
  "[&::backdrop]:overscroll-contain [&::backdrop]:bg-black/30";

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

  const dialogRef = useRef<HTMLDialogElement>(null);
  const pressOutside = useRef(false);
  // 매 렌더 후 ref가 붙어 있으면 showModal() — 'close'(Esc 등) 시 onClose 동기화.
  useEffect(() => {
    const d = dialogRef.current;
    if (!d) return;
    if (!d.open) d.showModal();
    const sync = () => onClose();
    d.addEventListener("close", sync);
    return () => d.removeEventListener("close", sync);
  });

  // 프로그램적 닫기 — d.close()가 'close' 이벤트를 발생시켜 위 리스너가
  // onClose로 React 상태를 동기화한다(브라우저가 포커스도 invoker로 복귀).
  const closeDialog = () => {
    const d = dialogRef.current;
    if (d) d.close();
    else onClose();
  };

  // 라이트 디스미스 — ::backdrop 클릭은 dialog 자신이 타겟이 된다.
  // press 시작·click 종료 모두 타겟이 dialog이고 좌표가 패널 rect 밖일
  // 때만 닫는다 — 텍스트 선택 드래그·select 옵션 클릭 등 버블 클릭 오인 방지.
  const outsideDialog = (e: { clientX: number; clientY: number; currentTarget: HTMLDialogElement }) => {
    const r = e.currentTarget.getBoundingClientRect();
    return e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom;
  };
  const onBackdropPointerDown = (e: PointerEvent<HTMLDialogElement>) => {
    pressOutside.current = outsideDialog(e);
  };
  const onBackdropClick = (e: MouseEvent<HTMLDialogElement>) => {
    const dismiss = pressOutside.current && e.target === e.currentTarget && outsideDialog(e);
    pressOutside.current = false;
    if (dismiss) closeDialog();
  };

  const st = task ? String(task.status || "backlog").toLowerCase() : "";
  const project = task?.project_id ? projects.find((p) => p.id === task.project_id) : undefined;
  // effective goal — spec §3차 A: task.goal_id → project.goal_id → none
  const effectiveGoalId = task ? task.goal_id || project?.goal_id || "" : "";
  const effectiveGoal = effectiveGoalId ? goals.find((g) => g.id === effectiveGoalId) : undefined;

  // 로딩/에러도 같은 dialog 셸 안에 렌더 — 열린 상태에서 언마운트/재마운트가
  // 없도록 항상 <dialog>를 반환하고 내용만 분기한다.
  return (
    <dialog
      ref={dialogRef}
      aria-label={task ? task.title || task.id : taskId}
      onPointerDown={onBackdropPointerDown}
      onClick={onBackdropClick}
      className={`${DRAWER_DIALOG_CLS} p-0`}
    >
      <div className="flex items-center gap-2 border-b border-border px-4 py-3">
        <h2 className="min-w-0 flex-1 truncate text-base font-bold text-ink">
          {task ? `${task.identifier ? `${task.identifier} ` : ""}${task.title || task.id}` : taskId}
        </h2>
        <button
          type="button"
          onClick={closeDialog}
          aria-label={t("tasks.close", "닫기")}
          className="rounded-md px-2 py-1 text-muted hover:bg-surface-hover"
        >
          ✕
        </button>
      </div>

      <div className="flex-1 space-y-4 p-4">
        {detail.isLoading ? (
          <div className="flex justify-center py-10"><Spinner /></div>
        ) : detail.error || !task ? (
          <ErrorBox
            message={detail.error instanceof Error ? detail.error.message : t("tasks.notFound", "작업을 찾을 수 없습니다")}
            onRetry={() => detail.refetch()}
          />
        ) : mode === "edit" ? (
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
    </dialog>
  );
}
