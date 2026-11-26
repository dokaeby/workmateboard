/** tasks — /management/tasks 작업 목록 + 상세(댓글/산출물/실행 이력).
 *  spec §3차 F/B API 계약:
 *   - GET /management/tasks?status&assignee&project_id&goal_id
 *   - GET/PATCH /management/tasks/{id}, POST /management/tasks (lead+)
 *   - GET/POST /management/tasks/{id}/comments
 *   - GET/POST /management/tasks/{id}/artifacts
 *  author_type/author_id는 서버가 결정 — 클라이언트는 body만 보낸다.
 *  artifact URI는 백엔드가 검증하지만 렌더링도 safeUrl로 한 번 더 거른다. */
import { useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  apiGet, apiPost, apiPatch, ApiError,
} from "../../shared/api/client";
import type {
  Task, TasksResponse, TaskComment, TaskCommentsResponse,
  TaskArtifact, TaskArtifactsResponse, Goal, GoalsResponse,
  Project, ProjectsResponse,
} from "../../shared/api/types";
import { useI18n } from "../../shared/i18n";
import { PageHead, Card, Empty, Spinner, ErrorBox, Badge, Button } from "../../shared/ui";
import { fmtTs, safeUrl } from "../../shared/lib/format";
import { roleAllows, useAppStore } from "../../shared/store/appStore";
import "./i18n";

const TASK_STATUSES = [
  "backlog", "todo", "in_progress", "in_review",
  "waiting_approval", "blocked", "done", "cancelled",
];
const TASK_PRIORITIES = ["low", "medium", "high", "urgent"];
const ARTIFACT_KINDS = [
  "plan", "diff", "test_report", "security_report",
  "review", "screenshot", "deployment", "other",
];

const STATUS_TONE: Record<string, "ok" | "warn" | "err" | "muted" | "accent"> = {
  backlog: "muted",
  todo: "muted",
  in_progress: "accent",
  in_review: "warn",
  waiting_approval: "warn",
  blocked: "err",
  done: "ok",
  cancelled: "muted",
};
const PRIORITY_TONE: Record<string, "ok" | "warn" | "err" | "muted" | "accent"> = {
  low: "muted",
  medium: "muted",
  high: "warn",
  urgent: "err",
};

const inputCls =
  "w-full rounded-lg border border-border bg-surface px-2.5 py-1.5 text-sm outline-none focus:border-accent";
const smallBtn = "px-3 py-1 text-xs";

interface MgmtAgentOption { id: string; name?: string; role?: string }

function statusLabel(t: (k: string, f?: string) => string, st: string): string {
  return t(`tasks.st.${st}`, st);
}
function priorityLabel(t: (k: string, f?: string) => string, p: string): string {
  return t(`tasks.pri.${p}`, p);
}
function kindLabel(t: (k: string, f?: string) => string, k: string): string {
  return t(`tasks.kind.${k}`, k);
}

function fmtBytes(n: number | null | undefined): string {
  const v = Number(n);
  if (!isFinite(v) || v < 0) return "-";
  if (v < 1024) return `${v} B`;
  if (v < 1024 * 1024) return `${(v / 1024).toFixed(1)} KB`;
  return `${(v / (1024 * 1024)).toFixed(1)} MB`;
}

/** task 등록/수정 폼 — project_id/goal_id는 기존 마스터에서 선택. */
function TaskForm({
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

/** 댓글 섹션 — GET/POST /management/tasks/{id}/comments.
 *  body는 plain text로 렌더(whitespace-pre-wrap) — HTML 원문 실행 금지(spec B). */
function CommentsSection({ taskId }: { taskId: string }) {
  const { t } = useI18n();
  const qc = useQueryClient();
  const [body, setBody] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["task-comments", taskId],
    queryFn: () => apiGet<TaskCommentsResponse>(`/management/tasks/${encodeURIComponent(taskId)}/comments`),
  });
  const items = useMemo(() => (Array.isArray(data?.items) ? data.items : []), [data]);

  const submit = async () => {
    if (!body.trim()) return;
    setBusy(true);
    setErr("");
    try {
      await apiPost(`/management/tasks/${encodeURIComponent(taskId)}/comments`, { body: body.trim() });
      setBody("");
      await qc.invalidateQueries({ queryKey: ["task-comments", taskId] });
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div>
      <h3 className="mb-1 text-xs font-semibold uppercase text-muted">{t("tasks.comments", "댓글")}</h3>
      {isLoading && <div className="text-xs text-muted">{t("common.loading", "로딩…")}</div>}
      {error && <div className="text-xs text-muted">{error instanceof Error ? error.message : String(error)}</div>}
      {data && !items.length && <div className="text-xs text-muted">{t("tasks.noComments", "댓글이 없습니다")}</div>}
      <ul className="space-y-2">
        {items.map((c: TaskComment) => (
          <li key={c.id} className="rounded-md border border-border bg-surface-hover px-2.5 py-2">
            <div className="mb-0.5 flex flex-wrap items-center gap-1.5 text-[11px] text-muted">
              <Badge tone={c.author_type === "agent" ? "accent" : c.author_type === "system" ? "muted" : "ok"}>
                {c.author_type || "human"}
              </Badge>
              <span className="font-medium text-ink-2">{c.author_id || "-"}</span>
              <span>{fmtTs(c.created_at)}</span>
              {c.edited_at && <span>({t("tasks.edited", "수정됨")})</span>}
            </div>
            <p className="whitespace-pre-wrap text-sm text-ink-2">{c.body}</p>
          </li>
        ))}
      </ul>
      <div className="mt-2 space-y-1.5">
        <textarea
          value={body}
          onChange={(e) => setBody(e.target.value)}
          rows={2}
          placeholder={t("tasks.commentPh", "댓글 작성…")}
          className={inputCls}
        />
        {err && <p className="text-xs text-err">{err}</p>}
        <div className="flex justify-end">
          <Button className={smallBtn} disabled={busy || !body.trim()} onClick={submit}>
            {busy ? t("common.saving", "저장 중…") : t("tasks.addComment", "댓글 추가")}
          </Button>
        </div>
      </div>
      <button type="button" className="mt-1 text-[11px] text-muted hover:text-ink" onClick={() => refetch()}>
        {t("common.refresh", "새로고침")}
      </button>
    </div>
  );
}

/** 산출물 섹션 — GET/POST /management/tasks/{id}/artifacts.
 *  URI는 <a> 링크로만 렌더한다(서버가 https/승인된 repo-relative만 허용).
 *  체크섬은 앞 12자만 표시. */
function ArtifactsSection({ taskId }: { taskId: string }) {
  const { t } = useI18n();
  const qc = useQueryClient();
  const [showForm, setShowForm] = useState(false);
  const [kind, setKind] = useState("other");
  const [name, setName] = useState("");
  const [uri, setUri] = useState("");
  const [mediaType, setMediaType] = useState("");
  const [checksum, setChecksum] = useState("");
  const [sizeBytes, setSizeBytes] = useState("");
  const [runId, setRunId] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  const { data, isLoading, error } = useQuery({
    queryKey: ["task-artifacts", taskId],
    queryFn: () => apiGet<TaskArtifactsResponse>(`/management/tasks/${encodeURIComponent(taskId)}/artifacts`),
  });
  const items = useMemo(() => (Array.isArray(data?.items) ? data.items : []), [data]);

  const submit = async () => {
    if (!name.trim() || !uri.trim()) {
      setErr(t("tasks.artNeedFields", "이름과 URI를 입력하세요"));
      return;
    }
    const body: Record<string, unknown> = {
      kind,
      name: name.trim(),
      uri: uri.trim(),
    };
    if (mediaType.trim()) body.media_type = mediaType.trim();
    if (checksum.trim()) body.checksum_sha256 = checksum.trim();
    if (sizeBytes.trim() && Number(sizeBytes) >= 0) body.size_bytes = Number(sizeBytes);
    if (runId.trim()) body.run_id = runId.trim();

    setBusy(true);
    setErr("");
    try {
      await apiPost(`/management/tasks/${encodeURIComponent(taskId)}/artifacts`, body);
      setShowForm(false);
      setName(""); setUri(""); setMediaType(""); setChecksum(""); setSizeBytes(""); setRunId("");
      await qc.invalidateQueries({ queryKey: ["task-artifacts", taskId] });
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div>
      <div className="mb-1 flex items-center justify-between">
        <h3 className="text-xs font-semibold uppercase text-muted">{t("tasks.artifacts", "산출물")}</h3>
        <Button variant="secondary" className="px-2 py-0.5 text-[11px]" onClick={() => setShowForm((v) => !v)}>
          {showForm ? t("common.close", "닫기") : "+ " + t("tasks.addArtifact", "산출물 추가")}
        </Button>
      </div>
      {isLoading && <div className="text-xs text-muted">{t("common.loading", "로딩…")}</div>}
      {error && <div className="text-xs text-muted">{error instanceof Error ? error.message : String(error)}</div>}
      {data && !items.length && <div className="text-xs text-muted">{t("tasks.noArtifacts", "등록된 산출물이 없습니다")}</div>}
      <ul className="space-y-1.5">
        {items.map((a: TaskArtifact) => (
          <li key={a.id} className="rounded-md border border-border bg-surface-hover px-2.5 py-2">
            <div className="flex flex-wrap items-center gap-1.5">
              <Badge tone="accent">{kindLabel(t, String(a.kind || "other"))}</Badge>
              <span className="min-w-0 flex-1 truncate text-sm font-medium text-ink">{a.name || a.id}</span>
              <span className="text-[11px] text-muted">{fmtTs(a.created_at)}</span>
            </div>
            <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-xs">
              {a.uri && (
                <a
                  href={safeUrl(a.uri)}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="max-w-full truncate text-accent hover:underline"
                >
                  {a.uri}
                </a>
              )}
              {a.checksum_sha256 && (
                <span className="font-mono text-muted" title={a.checksum_sha256}>
                  sha256:{String(a.checksum_sha256).slice(0, 12)}…
                </span>
              )}
              {a.size_bytes != null && <span className="text-muted">{fmtBytes(a.size_bytes)}</span>}
              {a.media_type && <span className="text-muted">{a.media_type}</span>}
              {a.run_id && <span className="text-muted">run:{String(a.run_id).slice(0, 8)}</span>}
            </div>
          </li>
        ))}
      </ul>
      {showForm && (
        <div className="mt-2 space-y-2 rounded-lg border border-border bg-surface-hover p-3">
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
            <label className="block">
              <span className="mb-1 block text-xs font-medium text-muted">{t("tasks.artKind", "종류")}</span>
              <select value={kind} onChange={(e) => setKind(e.target.value)} className={inputCls}>
                {ARTIFACT_KINDS.map((k) => (
                  <option key={k} value={k}>{kindLabel(t, k)}</option>
                ))}
              </select>
            </label>
            <label className="block">
              <span className="mb-1 block text-xs font-medium text-muted">{t("tasks.artName", "이름")}</span>
              <input value={name} onChange={(e) => setName(e.target.value)} className={inputCls} />
            </label>
            <label className="block sm:col-span-2">
              <span className="mb-1 block text-xs font-medium text-muted">{t("tasks.artUri", "URI (https 또는 repo-relative 경로)")}</span>
              <input value={uri} onChange={(e) => setUri(e.target.value)} placeholder="https://…" className={inputCls} />
            </label>
            <label className="block">
              <span className="mb-1 block text-xs font-medium text-muted">{t("tasks.artMediaType", "미디어 타입 (선택)")}</span>
              <input value={mediaType} onChange={(e) => setMediaType(e.target.value)} placeholder="text/markdown" className={inputCls} />
            </label>
            <label className="block">
              <span className="mb-1 block text-xs font-medium text-muted">{t("tasks.artChecksum", "SHA-256 (선택)")}</span>
              <input value={checksum} onChange={(e) => setChecksum(e.target.value)} className={`${inputCls} font-mono text-xs`} />
            </label>
            <label className="block">
              <span className="mb-1 block text-xs font-medium text-muted">{t("tasks.artSize", "크기 bytes (선택)")}</span>
              <input value={sizeBytes} onChange={(e) => setSizeBytes(e.target.value)} type="number" min="0" className={inputCls} />
            </label>
            <label className="block">
              <span className="mb-1 block text-xs font-medium text-muted">{t("tasks.artRunId", "연결 run ID (선택)")}</span>
              <input value={runId} onChange={(e) => setRunId(e.target.value)} className={`${inputCls} font-mono text-xs`} />
            </label>
          </div>
          {err && <p className="text-xs text-err">{err}</p>}
          <div className="flex justify-end gap-2">
            <Button variant="secondary" className={smallBtn} onClick={() => setShowForm(false)} disabled={busy}>
              {t("common.cancel", "취소")}
            </Button>
            <Button className={smallBtn} disabled={busy} onClick={submit}>
              {busy ? t("common.saving", "저장 중…") : t("common.save", "저장")}
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}

/** 실행 이력 — task detail 응답이 내장하는 runs/task_runs/current_run_id를 렌더.
 *  spec §3차 F는 task→run 전용 목록 엔드포인트를 정의하지 않아
 *  detail 응답 필드만 사용한다(없으면 current_run_id만 표시). */
function RunHistorySection({ task }: { task: Task }) {
  const { t } = useI18n();
  const embedded = useMemo(() => {
    const rows: { runId: string; status?: string; attempt?: number; at?: string }[] = [];
    const seen = new Set<string>();
    const push = (rid: unknown, status?: unknown, attempt?: unknown, at?: unknown) => {
      const id = String(rid || "");
      if (!id || seen.has(id)) return;
      seen.add(id);
      rows.push({
        runId: id,
        status: status ? String(status) : undefined,
        attempt: typeof attempt === "number" ? attempt : undefined,
        at: at ? String(at) : undefined,
      });
    };
    for (const r of task.task_runs || []) {
      push(r.run_id || r.id, r.status, r.attempt, r.created_at);
    }
    for (const r of task.runs || []) {
      push(r.run_id || r.id, r.status, r.attempt, r.created_at);
    }
    if (task.current_run_id) push(task.current_run_id);
    return rows;
  }, [task]);

  return (
    <div>
      <h3 className="mb-1 text-xs font-semibold uppercase text-muted">{t("tasks.runHistory", "실행 이력")}</h3>
      {!embedded.length && <div className="text-xs text-muted">{t("tasks.noRuns", "연결된 실행이 없습니다")}</div>}
      <ul className="space-y-1">
        {embedded.map((r) => (
          <li key={r.runId} className="flex flex-wrap items-center gap-2 rounded-md border border-border bg-surface-hover px-2.5 py-1.5 text-xs">
            <span className="font-mono text-ink">{r.runId.slice(0, 8)}</span>
            {r.status && <Badge tone={STATUS_TONE[r.status] ?? "muted"}>{r.status}</Badge>}
            {r.attempt != null && r.attempt > 1 && <span className="text-muted">#{r.attempt}</span>}
            {r.at && <span className="text-muted">{fmtTs(r.at)}</span>}
          </li>
        ))}
      </ul>
    </div>
  );
}

/** task 상세 사이드 패널. */
function TaskDrawer({
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
