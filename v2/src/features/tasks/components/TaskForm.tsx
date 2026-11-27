/** tasks — 작업 등록/수정 폼. project_id/goal_id는 기존 마스터에서 선택. */
import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { apiPost, apiPatch, ApiError } from "../../../shared/api/client";
import type { Task, Project, Goal } from "../../../shared/api/types";
import { useI18n } from "../../../shared/i18n";
import { Button } from "../../../shared/ui";
import {
  TASK_STATUSES, TASK_PRIORITIES, inputCls, smallBtn,
  statusLabel, priorityLabel, type MgmtAgentOption,
} from "../shared";

export function TaskForm({
  initial,
  projects,
  goals,
  agents,
  onDone,
}: {
  initial?: Task | null;
  projects: Project[];
  goals: Goal[];
  agents: MgmtAgentOption[];
  onDone: (saved?: Task) => void;
}) {
  const { t } = useI18n();
  const qc = useQueryClient();
  const [title, setTitle] = useState(initial?.title || "");
  const [desc, setDesc] = useState(initial?.description || "");
  const [status, setStatus] = useState<string>(initial?.status || "backlog");
  const [priority, setPriority] = useState<string>(initial?.priority || "medium");
  const [assignee, setAssignee] = useState(initial?.assignee_agent_id || "");
  const [reviewer, setReviewer] = useState(initial?.reviewer_agent_id || "");
  const [projectId, setProjectId] = useState(initial?.project_id || "");
  const [goalId, setGoalId] = useState(initial?.goal_id || "");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  const submit = async () => {
    if (!title.trim()) {
      setErr(t("tasks.needTitle", "제목을 입력하세요"));
      return;
    }
    const body: Record<string, unknown> = {
      title: title.trim(),
      status,
      priority,
      assignee_agent_id: assignee || null,
      reviewer_agent_id: reviewer || null,
      project_id: projectId || null,
      goal_id: goalId || null,
    };
    if (desc.trim()) body.description = desc.trim();
    else if (initial) body.description = null;

    setBusy(true);
    setErr("");
    try {
      let saved: Task | undefined;
      if (initial?.id) {
        saved = await apiPatch<Task>(`/management/tasks/${encodeURIComponent(initial.id)}`, body);
      } else {
        saved = await apiPost<Task>("/management/tasks", body);
      }
      await qc.invalidateQueries({ queryKey: ["mgmt-tasks"] });
      await qc.invalidateQueries({ queryKey: ["mgmt-task"] });
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
          <span className="mb-1 block text-xs font-medium text-muted">{t("tasks.titleField", "제목")}</span>
          <input value={title} onChange={(e) => setTitle(e.target.value)} className={inputCls} />
        </label>
        <label className="block">
          <span className="mb-1 block text-xs font-medium text-muted">{t("tasks.statusField", "상태")}</span>
          <select value={status} onChange={(e) => setStatus(e.target.value)} className={inputCls}>
            {TASK_STATUSES.map((s) => (
              <option key={s} value={s}>{statusLabel(t, s)}</option>
            ))}
          </select>
        </label>
        <label className="block">
          <span className="mb-1 block text-xs font-medium text-muted">{t("tasks.priorityField", "우선순위")}</span>
          <select value={priority} onChange={(e) => setPriority(e.target.value)} className={inputCls}>
            {TASK_PRIORITIES.map((p) => (
              <option key={p} value={p}>{priorityLabel(t, p)}</option>
            ))}
          </select>
        </label>
        <label className="block">
          <span className="mb-1 block text-xs font-medium text-muted">{t("tasks.assignee", "담당 에이전트")}</span>
          <select value={assignee} onChange={(e) => setAssignee(e.target.value)} className={inputCls}>
            <option value="">{t("tasks.unassigned", "미지정")}</option>
            {agents.map((a) => (
              <option key={a.id} value={a.id}>{a.name || a.id}</option>
            ))}
          </select>
        </label>
        <label className="block">
          <span className="mb-1 block text-xs font-medium text-muted">{t("tasks.reviewer", "리뷰어 (선택)")}</span>
          <select value={reviewer} onChange={(e) => setReviewer(e.target.value)} className={inputCls}>
            <option value="">{t("tasks.none", "없음")}</option>
            {agents.map((a) => (
              <option key={a.id} value={a.id}>{a.name || a.id}</option>
            ))}
          </select>
        </label>
        <label className="block">
          <span className="mb-1 block text-xs font-medium text-muted">{t("tasks.project", "프로젝트")}</span>
          <select value={projectId} onChange={(e) => setProjectId(e.target.value)} className={inputCls}>
            <option value="">{t("tasks.noProject", "없음")}</option>
            {projects.map((p) => (
              <option key={p.id} value={p.id}>{p.name || p.id}</option>
            ))}
          </select>
        </label>
        <label className="block">
          <span className="mb-1 block text-xs font-medium text-muted">{t("tasks.goal", "목표")}</span>
          <select value={goalId} onChange={(e) => setGoalId(e.target.value)} className={inputCls}>
            <option value="">{t("tasks.noGoal", "없음")}</option>
            {goals.map((g) => (
              <option key={g.id} value={g.id}>{g.title || g.id}</option>
            ))}
          </select>
        </label>
      </div>
      <label className="block">
        <span className="mb-1 block text-xs font-medium text-muted">{t("tasks.description", "설명")}</span>
        <textarea value={desc} onChange={(e) => setDesc(e.target.value)} rows={3} className={inputCls} />
      </label>
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
