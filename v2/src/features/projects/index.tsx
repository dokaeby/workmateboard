/** 프로젝트 — /projects/overview 카드 그리드 + /projects/{id}/detail 펼침 상세 + 생성/수정/삭제.
 *  3차 spec §3차 A 필드: status, repository_url/ref, workspace_path, owner_agent_id. */
import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { apiGet, apiPost, apiPatch, apiDelete, ApiError } from "../../shared/api/client";
import type { Me, Project, ProjectStatus, ProjectsResponse } from "../../shared/api/types";
import { useI18n } from "../../shared/i18n";
import { PageHead, Card, Empty, Spinner, ErrorBox, Button, Badge } from "../../shared/ui";
import { fmtTs, usd, safeUrl } from "../../shared/lib/format";
import { roleAllows, useAppStore } from "../../shared/store/appStore";
import "./i18n";


interface ProjectOverviewItem {
  id: string;
  name?: string;
  color?: string;
  description?: string;
  goal?: string;
  target_count?: number;
  created_by?: string;
  // 3차 spec §3차 A — overview 응답에 포함되면 표시, 없으면 /projects 상세에서 보완
  goal_id?: string | null;
  status?: ProjectStatus;
  owner_agent_id?: string | null;
  repository_url?: string | null;
  repository_ref?: string | null;
  workspace_path?: string | null;
  session_count?: number;
  active_sessions?: number;
  run_count?: number;
  success_rate?: number | null;
  avg_seconds?: number | null;
  total_cost_usd?: number;
  open_approvals?: number;
  recent_activity?: { at?: string; kind?: string; text?: string }[];
}
interface OverviewResponse { items?: ProjectOverviewItem[] }

/** /goals 항목 — 프로젝트 생성 폼의 목표(goal_id) 선택용 부분 타입. */
interface GoalOption { id: string; title?: string }

/** 프로젝트 상태 어휘 — spec 3차 A: planned|active|paused|completed|cancelled. */
const PROJECT_STATUSES: ProjectStatus[] = ["planned", "active", "paused", "completed", "cancelled"];
const PROJ_STATUS_TONE: Record<string, "ok" | "warn" | "muted" | "accent"> = {
  planned: "muted",
  active: "ok",
  paused: "warn",
  completed: "accent",
  cancelled: "muted",
};
const PROJ_STATUS_LABEL: Record<string, string> = {
  planned: "proj.statusPlanned",
  active: "proj.statusActive",
  paused: "proj.statusPaused",
  completed: "proj.statusCompleted",
  cancelled: "proj.statusCancelled",
};

function projStatusLabel(t: (k: string, f?: string) => string, st: string): string {
  return t(PROJ_STATUS_LABEL[st] || "", st);
}

interface ProjectDetail {
  project?: { id?: string; name?: string };
  sessions?: unknown[];
  runs?: { id?: string; workflow?: string; status?: string; task?: string; created_at?: string }[];
  timeline?: { at?: string; kind?: string; text?: string }[];
  stage_failures?: Record<string, number> | { stage?: string; stage_key?: string; count?: number }[];
  cost_by_day?: { day?: string; usd?: number; date?: string; cost_usd?: number }[];
}

function stageFailureRows(sf: ProjectDetail["stage_failures"]): [string, number][] {
  if (!sf) return [];
  if (Array.isArray(sf)) {
    return sf.map((x) => [x.stage || x.stage_key || "?", Number(x.count || 0)]);
  }
  return Object.entries(sf).map(([k, v]) => [k, Number(v) || 0]);
}

function ProjectDetailView({ pid }: { pid: string }) {
  const { t } = useI18n();
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["project-detail", pid],
    queryFn: () => apiGet<ProjectDetail>(`/projects/${encodeURIComponent(pid)}/detail`),
  });
  if (isLoading) return <Spinner label={t("common.loading", "로딩…")} />;
  if (error) return <ErrorBox message={error instanceof Error ? error.message : String(error)} onRetry={() => refetch()} />;
  if (!data) return null;

  const sessions = Array.isArray(data.sessions) ? data.sessions : [];
  const runs = Array.isArray(data.runs) ? data.runs : [];
  const tl = Array.isArray(data.timeline) ? data.timeline : [];
  const fails = stageFailureRows(data.stage_failures);
  const cbd = Array.isArray(data.cost_by_day) ? data.cost_by_day : [];
  const maxFail = Math.max(1, ...fails.map(([, c]) => c));
  const maxCost = Math.max(1e-9, ...cbd.map((x) => Number(x.usd ?? x.cost_usd) || 0));

  return (
    <div className="space-y-2 text-xs">
      <div className="text-muted">
        {t("proj.sessions", "세션")} {sessions.length} · {t("proj.runs", "실행")} {runs.length}
      </div>
      {tl.length > 0 && (
        <div>
          <div className="mb-1 font-semibold text-muted">{t("proj.timeline", "타임라인")}</div>
          {tl.slice(-15).map((x, i) => (
            <div key={i} className="text-muted">
              {fmtTs(x.at)} {x.kind ? `[${x.kind}] ` : ""}{x.text || ""}
            </div>
          ))}
        </div>
      )}
      {fails.length > 0 && (
        <div>
          <div className="mb-1 font-semibold text-muted">{t("proj.stageFails", "단계별 실패")}</div>
          {fails.map(([k, c]) => (
            <div key={k} className="flex items-center gap-2 py-0.5">
              <span className="w-28 truncate">{k}</span>
              <span className="h-2 flex-1 overflow-hidden rounded bg-surface-hover">
                <span className="block h-full bg-err" style={{ width: `${Math.max(4, Math.round((c / maxFail) * 100))}%` }} />
              </span>
              <span className="w-8 text-right font-medium text-err">{c}</span>
            </div>
          ))}
        </div>
      )}
      {cbd.length > 0 && (
        <div>
          <div className="mb-1 font-semibold text-muted">{t("proj.costByDay", "일별 비용")}</div>
          {cbd.slice(-14).map((x, i) => {
            const c = Number(x.usd ?? x.cost_usd) || 0;
            const day = x.day || x.date || "";
            return (
              <div key={i} className="flex items-center gap-2 py-0.5">
                <span className="w-20 text-muted">{day}</span>
                <span className="h-2 flex-1 overflow-hidden rounded bg-surface-hover">
                  <span className="block h-full bg-ok" style={{ width: `${Math.max(2, Math.round((c / maxCost) * 100))}%` }} />
                </span>
                <span className="w-16 text-right">{usd(c)}</span>
              </div>
            );
          })}
        </div>
      )}
      {!tl.length && !fails.length && !cbd.length && (
        <Empty>{t("proj.noDetail", "상세 데이터가 없습니다")}</Empty>
      )}
    </div>
  );
}

/** 프로젝트 등록/수정 공용 폼 — initial이 있으면 PATCH /projects/{id}, 없으면 POST.
 *  3차 spec §3차 A 필드(status, repository_url/ref, workspace_path, owner_agent_id)를 포함한다. */
function ProjectForm({ initial, onDone }: { initial?: Project | ProjectOverviewItem | null; onDone: () => void }) {
  const { t } = useI18n();
  const qc = useQueryClient();
  const isEdit = !!initial?.id;
  const [name, setName] = useState(initial?.name || "");
  const [color, setColor] = useState(initial?.color || "");
  const [desc, setDesc] = useState(initial?.description || "");
  const [goal, setGoal] = useState(initial?.goal || "");
  const [goalId, setGoalId] = useState(initial?.goal_id || "");
  const [target, setTarget] = useState(initial?.target_count != null ? String(initial.target_count) : "");
  const [status, setStatus] = useState<string>(initial?.status || "planned");
  const [repoUrl, setRepoUrl] = useState(initial?.repository_url || "");
  const [repoRef, setRepoRef] = useState(initial?.repository_ref || "");
  const [wsPath, setWsPath] = useState(initial?.workspace_path || "");
  const [owner, setOwner] = useState(initial?.owner_agent_id || "");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  // 목표 트리에서 goal_id로 연결할 목표 목록 (없으면 선택 UI를 숨긴다).
  const goals = useQuery({
    queryKey: ["goals"],
    queryFn: () => apiGet<{ items?: GoalOption[] }>("/goals"),
    staleTime: 60000,
    retry: false,
  });
  const goalOptions = (goals.data?.items || []).filter((g) => g && g.id);
  // owner_agent_id 후보 — 관리 API는 lead 이상. 실패 시 자유 입력으로 둔다.
  const agents = useQuery({
    queryKey: ["mgmt-agents-options"],
    queryFn: () => apiGet<{ items?: { id: string; name?: string }[] }>("/management/agents"),
    staleTime: 60000,
    retry: false,
  });
  const agentOptions = agents.data?.items || [];

  const inputCls = "rounded-md border border-border bg-surface px-2 py-1.5 text-sm";

  const submit = async () => {
    if (!name.trim()) return;
    setBusy(true); setMsg("");
    const body: Record<string, unknown> = {
      name: name.trim(),
      color: color.trim(),
      description: desc.trim(),
      goal: goal.trim(),
      goal_id: goalId || null,
      target_count: Number(target) || 0,
      status,
      repository_url: repoUrl.trim() || null,
      repository_ref: repoRef.trim() || null,
      workspace_path: wsPath.trim() || null,
      owner_agent_id: owner.trim() || null,
    };
    try {
      if (isEdit && initial?.id) {
        await apiPatch(`/projects/${encodeURIComponent(initial.id)}`, body);
      } else {
        await apiPost("/projects", body);
      }
      setName(""); setColor(""); setDesc(""); setGoal(""); setGoalId(""); setTarget("");
      setStatus("planned"); setRepoUrl(""); setRepoRef(""); setWsPath(""); setOwner("");
      qc.invalidateQueries({ queryKey: ["projects"] });
      qc.invalidateQueries({ queryKey: ["project-master"] });
      onDone();
    } catch (e) {
      setMsg(e instanceof ApiError ? e.message : String(e));
    } finally { setBusy(false); }
  };
  return (
    <Card title={isEdit ? t("proj.edit", "프로젝트 수정") : t("proj.new", "프로젝트 추가")} className="mb-3">
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder={t("proj.name", "이름")}
          className={inputCls} />
        <input value={color} onChange={(e) => setColor(e.target.value)} placeholder={t("proj.color", "색상 (#hex)")}
          className={inputCls} />
        <select value={status} onChange={(e) => setStatus(e.target.value)}
          aria-label={t("proj.status", "상태")}
          className={inputCls}>
          {PROJECT_STATUSES.map((s) => (
            <option key={s} value={s}>{projStatusLabel(t, s)}</option>
          ))}
        </select>
        <input value={desc} onChange={(e) => setDesc(e.target.value)} placeholder={t("proj.description", "설명")}
          className={inputCls} />
        <input value={goal} onChange={(e) => setGoal(e.target.value)} placeholder={t("proj.goal", "목표")}
          className={inputCls} />
        {goalOptions.length > 0 && (
          <select value={goalId} onChange={(e) => setGoalId(e.target.value)}
            aria-label={t("proj.linkedGoal", "연결된 목표")}
            className={inputCls}>
            <option value="">{t("proj.noLinkedGoal", "목표 연결 없음")}</option>
            {goalOptions.map((g) => (
              <option key={g.id} value={g.id}>{g.title || g.id}</option>
            ))}
          </select>
        )}
        <input value={target} onChange={(e) => setTarget(e.target.value)} placeholder={t("proj.target", "목표 세션 수")}
          type="number" className={inputCls} />
        <input value={repoUrl} onChange={(e) => setRepoUrl(e.target.value)} placeholder={t("proj.repositoryUrl", "저장소 URL")}
          className={inputCls} />
        <input value={repoRef} onChange={(e) => setRepoRef(e.target.value)} placeholder={t("proj.repositoryRef", "저장소 ref (브랜치/태그)")}
          className={inputCls} />
        <input value={wsPath} onChange={(e) => setWsPath(e.target.value)} placeholder={t("proj.workspacePath", "워크스페이스 경로")}
          className={`${inputCls} sm:col-span-2`} />
        <input value={owner} onChange={(e) => setOwner(e.target.value)} placeholder={t("proj.ownerAgent", "담당 에이전트 ID")}
          list="proj-owner-agents" className={inputCls} />
        <datalist id="proj-owner-agents">
          {agentOptions.map((a) => (
            <option key={a.id} value={a.id}>{a.name || a.id}</option>
          ))}
        </datalist>
      </div>
      <div className="mt-2 flex items-center justify-end gap-2">
        {msg && <span className="text-xs text-err">{msg}</span>}
        <Button variant="secondary" className="px-3 py-1 text-xs" onClick={onDone}>{t("common.cancel", "취소")}</Button>
        <Button className="px-3 py-1 text-xs" disabled={busy || !name.trim()} onClick={submit}>
          {isEdit ? t("common.save", "저장") : t("proj.add", "추가")}
        </Button>
      </div>
    </Card>
  );
}

/** 편집 폼용 마스터 데이터 — overview 항목에 3차 필드가 없을 수 있어
 *  GET /projects 원본 행을 id로 찾아 폼을 초기화한다. */
function ProjectEditSection({ item, onDone }: { item: ProjectOverviewItem; onDone: () => void }) {
  const { t } = useI18n();
  const master = useQuery({
    queryKey: ["project-master"],
    queryFn: () => apiGet<ProjectsResponse>("/projects"),
    staleTime: 60000,
    retry: false,
  });
  if (master.isLoading) return <Spinner label={t("common.loading", "로딩…")} />;
  // overview에 3차 필드가 없을 수 있어 /projects 원본 행을 우선 사용한다.
  const proj = (master.data?.items || []).find((p) => p.id === item.id) || item;
  return <ProjectForm key={proj.id} initial={proj} onDone={onDone} />;
}

export default function ProjectsPage() {
  const { t } = useI18n();
  const role = useAppStore((s) => s.role);
  const qc = useQueryClient();
  const [openId, setOpenId] = useState<string | null>(null);
  const [editId, setEditId] = useState<string | null>(null);
  const [showNew, setShowNew] = useState(false);

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["projects"],
    queryFn: () => apiGet<OverviewResponse>("/projects/overview"),
    refetchInterval: 60000,
  });
  const me = useQuery({ queryKey: ["me"], queryFn: () => apiGet<Me>("/auth/me"), staleTime: 60000, retry: false });

  const items = data?.items || [];
  const myEmail = String(me.data?.email || "").toLowerCase();
  const isLead = roleAllows("lead", role ?? "member");

  const remove = async (p: ProjectOverviewItem) => {
    if (!confirm(t("proj.confirmDel", "이 프로젝트를 삭제할까요?"))) return;
    try {
      await apiDelete(`/projects/${encodeURIComponent(p.id)}`);
      qc.invalidateQueries({ queryKey: ["projects"] });
    } catch (e) {
      alert(e instanceof ApiError ? e.message : String(e));
    }
  };

  return (
    <div className="mx-auto max-w-5xl p-4">
      <PageHead title={t("proj.title", "프로젝트")} desc={t("proj.desc", "프로젝트별 세션·실행·승인·비용 집계")}>
        {isLead && (
          <Button className="text-xs" onClick={() => setShowNew((v) => !v)}>
            {showNew ? t("common.close", "닫기") : "+ " + t("proj.new", "프로젝트 추가")}
          </Button>
        )}
        <Button variant="secondary" onClick={() => refetch()} className="text-xs">{t("common.refresh", "새로고침")}</Button>
      </PageHead>
      {showNew && isLead && <ProjectForm onDone={() => setShowNew(false)} />}
      {isLoading && <Spinner />}
      {error && <ErrorBox message={error instanceof Error ? error.message : String(error)} onRetry={() => refetch()} />}
      {data && !items.length && <Empty>{t("proj.empty", "프로젝트가 없습니다 — 위 폼에서 추가하세요")}</Empty>}
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
        {items.map((p) => {
          const target = Number(p.target_count) || 0;
          const sess = Number(p.session_count) || 0;
          const pct = target > 0 ? Math.min(100, Math.round((sess / target) * 100)) : null;
          const owner = String(p.created_by || "").toLowerCase();
          const canDel = role === "admin" || !owner || owner === myEmail;
          const open = openId === p.id;
          const editing = editId === p.id;
          const pstatus = String(p.status || "").toLowerCase();
          return (
            <Card key={p.id} className="flex flex-col">
              <div className="flex items-center gap-2">
                <span className="inline-block h-3 w-3 rounded-full" style={{ background: p.color || "#94a3b8" }} />
                <span className="text-sm font-semibold text-ink">{p.name || t("proj.other", "(기타)")}</span>
                {pstatus && (
                  <Badge tone={PROJ_STATUS_TONE[pstatus] ?? "muted"}>
                    {projStatusLabel(t, pstatus)}
                  </Badge>
                )}
                {p.goal && <span className="truncate text-xs text-muted">{p.goal}</span>}
                <span className="ml-auto flex gap-1">
                  <Button variant="ghost" className="px-2 py-0.5 text-xs" onClick={() => setOpenId(open ? null : p.id)}>
                    {open ? t("common.close", "닫기") : t("proj.detail", "상세")}
                  </Button>
                  {isLead && (
                    <Button variant="ghost" className="px-2 py-0.5 text-xs" onClick={() => setEditId(editing ? null : p.id)}>
                      {editing ? t("common.close", "닫기") : t("proj.edit", "수정")}
                    </Button>
                  )}
                  {isLead && canDel && (
                    <Button variant="ghost" className="px-2 py-0.5 text-xs text-err" onClick={() => remove(p)}>
                      {t("common.delete", "삭제")}
                    </Button>
                  )}
                </span>
              </div>
              {(p.repository_url || p.workspace_path || p.owner_agent_id) && (
                <div className="mt-1.5 space-y-0.5 text-xs text-muted">
                  {p.repository_url && (
                    <div className="truncate">
                      {t("proj.repository", "저장소")}:{" "}
                      <a
                        href={safeUrl(p.repository_url)}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="text-accent hover:underline"
                      >
                        {p.repository_url}
                      </a>
                      {p.repository_ref && <span className="ml-1">@ {p.repository_ref}</span>}
                    </div>
                  )}
                  {p.workspace_path && (
                    <div className="truncate font-mono" title={p.workspace_path}>
                      {t("proj.workspace", "워크스페이스")} {p.workspace_path}
                    </div>
                  )}
                  {p.owner_agent_id && (
                    <div className="truncate">
                      {t("proj.ownerAgent", "담당 에이전트")} {p.owner_agent_id}
                    </div>
                  )}
                </div>
              )}
              {pct != null && (
                <div className="mt-2">
                  <div className="h-2 overflow-hidden rounded bg-surface-hover">
                    <div className="h-full bg-accent" style={{ width: `${pct}%` }} />
                  </div>
                  <div className="mt-0.5 text-xs text-muted">
                    {t("proj.sessionsTarget", "세션")} {sess}/{target} ({pct}%)
                  </div>
                </div>
              )}
              <div className="mt-2 flex flex-wrap gap-1">
                <Badge>{t("proj.activeSess", "활성 세션")} {p.active_sessions ?? 0}</Badge>
                <Badge>{t("proj.runs", "실행")} {p.run_count ?? 0}</Badge>
                <Badge>{t("proj.successRate", "성공률")} {p.success_rate ?? "-"}%</Badge>
                <Badge>{usd(p.total_cost_usd)}</Badge>
                {!!p.open_approvals && <Badge tone="warn">{t("appr.waiting", "대기")} {p.open_approvals}</Badge>}
              </div>
              {(p.recent_activity || []).length > 0 && (
                <div className="mt-2 space-y-0.5 text-xs text-muted">
                  {(p.recent_activity || []).slice(0, 3).map((a, i) => (
                    <div key={i} className="truncate">
                      <span>{fmtTs(a.at)}</span> {a.text || a.kind || ""}
                    </div>
                  ))}
                </div>
              )}
              {open && (
                <div className="mt-3 border-t border-border pt-2">
                  <ProjectDetailView pid={p.id} />
                </div>
              )}
              {editing && isLead && (
                <div className="mt-3 border-t border-border pt-2">
                  <ProjectEditSection
                    item={p}
                    onDone={() => {
                      setEditId(null);
                      qc.invalidateQueries({ queryKey: ["projects"] });
                    }}
                  />
                </div>
              )}
            </Card>
          );
        })}
      </div>
    </div>
  );
}
