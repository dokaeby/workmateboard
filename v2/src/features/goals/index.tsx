/** goals — /goals 목표 트리(parent_id) + 등록/편집/취소.
 *  API 계약: GET/POST /goals, PATCH/DELETE /goals/{id}.
 *  3차 spec §3차 A: status 어휘는 active|paused|achieved|cancelled —
 *  종료 상태는 PATCH {status:"cancelled"}로 명시한다(구 archive/DELETE는
 *  레거시 호환용으로 남겨두고 UI에서는 cancelled 전이를 사용).
 *  goal 필드: owner_agent_id, success_metrics(JSON), target_at. */
import { useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { apiGet, apiPost, apiPatch, ApiError } from "../../shared/api/client";
import { useI18n } from "../../shared/i18n";
import { PageHead, Card, Empty, Spinner, ErrorBox, Badge, Button } from "../../shared/ui";
import { fmtTs, jsonSummary } from "../../shared/lib/format";
import { buildForest, type Goal, type GoalNode, type GoalsResponse } from "./tree";
import "./i18n";
import "./goals.css";

const STATUSES = ["active", "paused", "achieved", "cancelled"];
/** 레거시 데이터에 남아 있을 수 있는 종결 상태 — 트리/필터에서 동일 취급. */
const TERMINAL_STATUSES = new Set(["cancelled", "archived"]);

const STATUS_TONE: Record<string, "ok" | "warn" | "muted" | "accent"> = {
  active: "ok",
  paused: "warn",
  achieved: "accent",
  cancelled: "muted",
  // 레거시 별칭
  done: "accent",
  archived: "muted",
};

const STATUS_LABEL_KEY: Record<string, string> = {
  active: "goals.statusActive",
  paused: "goals.statusPaused",
  achieved: "goals.statusAchieved",
  cancelled: "goals.statusCancelled",
  done: "goals.statusDone",
  archived: "goals.statusArchived",
};

function statusLabel(t: (k: string, f?: string) => string, st: string): string {
  return t(STATUS_LABEL_KEY[st] || "", st);
}

function statusOf(g: Goal): string {
  return String(g.status || "active").toLowerCase();
}

const inputCls =
  "w-full rounded-lg border border-border bg-surface px-2.5 py-1.5 text-sm outline-none focus:border-accent";

/** success_metrics JSON 텍스트 → 전송값. 빈 문자열은 null(해제),
 *  파싱 실패 시 에러 — 임의 문자열을 JSON 컬럼에 넣지 않는다. */
function parseMetrics(raw: string): { ok: true; value: unknown } | { ok: false } {
  const s = raw.trim();
  if (!s) return { ok: true, value: null };
  try {
    return { ok: true, value: JSON.parse(s) };
  } catch {
    return { ok: false };
  }
}

function metricsText(v: unknown): string {
  if (v == null || v === "") return "";
  if (typeof v === "string") return v;
  try {
    return JSON.stringify(v, null, 2);
  } catch {
    return String(v);
  }
}

interface MgmtAgentOption { id: string; name?: string }

/** 목표 등록/편집 폼 — initial이 있으면 PATCH, 없으면 POST.
 *  presetParent는 "하위 목표 추가"에서 상위 목표를 미리 채운다. */
function GoalForm({
  goals,
  initial,
  presetParent,
  onDone,
}: {
  goals: Goal[];
  initial?: Goal | null;
  presetParent?: string | null;
  onDone: (saved?: Goal) => void;
}) {
  const { t } = useI18n();
  const qc = useQueryClient();
  const [title, setTitle] = useState(initial?.title || "");
  const [desc, setDesc] = useState(initial?.description || "");
  const [parent, setParent] = useState<string>(
    presetParent ?? initial?.parent_id ?? "",
  );
  const [status, setStatus] = useState<string>(initial ? statusOf(initial) : "active");
  const [owner, setOwner] = useState(initial?.owner_agent_id || "");
  const [metrics, setMetrics] = useState(metricsText(initial?.success_metrics));
  const [targetAt, setTargetAt] = useState((initial?.target_at || "").slice(0, 10));
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  // owner_agent_id 후보 — 관리 API는 lead 이상. member에게는 실패하므로
  // retry 없이 시도하고, 실패 시 자유 입력(datalist 없이)으로 둔다.
  const agentsQ = useQuery({
    queryKey: ["mgmt-agents-options"],
    queryFn: () => apiGet<{ items?: MgmtAgentOption[] }>("/management/agents"),
    staleTime: 60000,
    retry: false,
  });
  const agentOptions = useMemo(
    () => (Array.isArray(agentsQ.data?.items) ? agentsQ.data.items : []),
    [agentsQ.data],
  );

  // 상위 목표 후보 — 본인·종결 목표 제외. 현재 선택값이 후보에 없으면 유지.
  const parentOptions = useMemo(() => {
    const list = goals.filter(
      (g) => g.id !== initial?.id && !TERMINAL_STATUSES.has(statusOf(g)),
    );
    if (parent && !list.some((g) => g.id === parent)) {
      const cur = goals.find((g) => g.id === parent);
      if (cur) return [cur, ...list];
    }
    return list;
  }, [goals, initial?.id, parent]);

  const submit = async () => {
    if (!title.trim()) {
      setErr(t("goals.needTitle", "제목을 입력하세요"));
      return;
    }
    const parsed = parseMetrics(metrics);
    if (!parsed.ok) {
      setErr(t("goals.badMetrics", "성공 지표는 유효한 JSON이어야 합니다"));
      return;
    }
    const body: Record<string, unknown> = {
      title: title.trim(),
      status,
    };
    if (desc.trim()) body.description = desc.trim();
    else if (initial) body.description = null;
    if (initial) {
      // 편집: 명시적 null로 상위 목표/담당/지표/기한 해제를 지원한다.
      body.parent_id = parent || null;
      body.owner_agent_id = owner.trim() || null;
      body.success_metrics = parsed.value;
      body.target_at = targetAt || null;
    } else {
      if (parent) body.parent_id = parent;
      if (owner.trim()) body.owner_agent_id = owner.trim();
      if (parsed.value != null) body.success_metrics = parsed.value;
      if (targetAt) body.target_at = targetAt;
    }

    setBusy(true);
    setErr("");
    try {
      let saved: Goal | undefined;
      if (initial?.id) {
        saved = await apiPatch<Goal>(`/goals/${encodeURIComponent(initial.id)}`, body);
      } else {
        saved = await apiPost<Goal>("/goals", body);
      }
      await qc.invalidateQueries({ queryKey: ["goals"] });
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
        <label className="block">
          <span className="mb-1 block text-xs font-medium text-muted">{t("goals.titleField", "제목")}</span>
          <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder={t("goals.titlePh", "예: 분기 매출 목표")} className={inputCls} />
        </label>
        <label className="block">
          <span className="mb-1 block text-xs font-medium text-muted">{t("goals.parentSelect", "상위 목표 (선택)")}</span>
          <select value={parent} onChange={(e) => setParent(e.target.value)} className={inputCls}>
            <option value="">{t("goals.noParent", "없음 (최상위)")}</option>
            {parentOptions.map((g) => (
              <option key={g.id} value={g.id}>{g.title || g.id}</option>
            ))}
          </select>
        </label>
        <label className="block">
          <span className="mb-1 block text-xs font-medium text-muted">{t("goals.statusField", "상태")}</span>
          <select value={status} onChange={(e) => setStatus(e.target.value)} className={inputCls}>
            {STATUSES.map((s) => (
              <option key={s} value={s}>{statusLabel(t, s)}</option>
            ))}
          </select>
        </label>
        <label className="block">
          <span className="mb-1 block text-xs font-medium text-muted">{t("goals.ownerAgent", "담당 에이전트 (선택)")}</span>
          <input
            value={owner}
            onChange={(e) => setOwner(e.target.value)}
            placeholder={t("goals.ownerAgentPh", "에이전트 ID")}
            list="goal-owner-agents"
            className={inputCls}
          />
          <datalist id="goal-owner-agents">
            {agentOptions.map((a) => (
              <option key={a.id} value={a.id}>{a.name || a.id}</option>
            ))}
          </datalist>
        </label>
        <label className="block">
          <span className="mb-1 block text-xs font-medium text-muted">{t("goals.targetAt", "목표 기한 (선택)")}</span>
          <input value={targetAt} onChange={(e) => setTargetAt(e.target.value)} type="date" className={inputCls} />
        </label>
      </div>
      <label className="block">
        <span className="mb-1 block text-xs font-medium text-muted">{t("goals.description", "설명")}</span>
        <input value={desc} onChange={(e) => setDesc(e.target.value)} placeholder={t("goals.descriptionPh", "설명 (선택)")} className={inputCls} />
      </label>
      <label className="block">
        <span className="mb-1 block text-xs font-medium text-muted">{t("goals.successMetrics", "성공 지표 (JSON, 선택)")}</span>
        <textarea
          value={metrics}
          onChange={(e) => setMetrics(e.target.value)}
          rows={3}
          placeholder='{"metric": "value"}'
          className={`${inputCls} font-mono text-xs`}
        />
      </label>
      {err && <p className="text-xs text-err">{err}</p>}
      <div className="flex items-center justify-end gap-2">
        <Button variant="secondary" className="px-3 py-1 text-xs" onClick={() => onDone()} disabled={busy}>
          {t("common.cancel", "취소")}
        </Button>
        <Button className="px-3 py-1 text-xs" disabled={busy} onClick={submit}>
          {busy ? t("common.saving", "저장 중…") : t("common.save", "저장")}
        </Button>
      </div>
    </div>
  );
}

/** 트리 노드 카드. */
function GoalCard({
  node,
  selected,
  onSelect,
}: {
  node: GoalNode;
  selected: boolean;
  onSelect: (g: Goal) => void;
}) {
  const { t } = useI18n();
  const g = node.goal;
  const st = statusOf(g);
  const preview = (g.description || "").trim();
  const terminal = TERMINAL_STATUSES.has(st);
  return (
    <button
      type="button"
      onClick={() => onSelect(g)}
      className={`w-52 rounded-xl border bg-surface p-3 text-left shadow-sm transition-colors hover:border-accent-border ${
        selected ? "border-accent ring-1 ring-accent" : "border-border"
      } ${terminal ? "opacity-60" : ""}`}
    >
      <div className="flex items-center gap-1.5">
        <span className="min-w-0 flex-1 truncate text-sm font-semibold text-ink">
          {g.title || g.id}
        </span>
      </div>
      <div className="mt-1.5 flex flex-wrap items-center gap-1">
        <Badge tone={STATUS_TONE[st] ?? "muted"}>
          {statusLabel(t, st)}
        </Badge>
      </div>
      {preview && (
        <div className="mt-1 line-clamp-2 text-xs text-muted">{preview}</div>
      )}
      <div className="mt-1.5 text-[11px] text-muted">
        {g.created_at ? fmtTs(g.created_at) : "-"}
      </div>
    </button>
  );
}

function GoalNodeView({
  node,
  selectedId,
  onSelect,
}: {
  node: GoalNode;
  selectedId?: string;
  onSelect: (g: Goal) => void;
}) {
  const g = node.goal;
  return (
    <li>
      <GoalCard
        node={node}
        selected={g.id === selectedId}
        onSelect={onSelect}
      />
      {node.children.length > 0 && (
        <ul className="gld-list gld-kids">
          {node.children.map((c) => (
            <GoalNodeView key={c.goal.id} node={c} selectedId={selectedId} onSelect={onSelect} />
          ))}
        </ul>
      )}
    </li>
  );
}

/** 상세/편집 사이드 패널. */
function GoalDrawer({
  goal,
  goals,
  onClose,
  onChanged,
}: {
  goal: Goal;
  goals: Goal[];
  onClose: () => void;
  onChanged: () => void;
}) {
  const { t } = useI18n();
  const qc = useQueryClient();
  const [mode, setMode] = useState<"view" | "edit" | "addSub">("view");
  const [busy, setBusy] = useState(false);

  const st = statusOf(goal);
  const byId = useMemo(() => new Map(goals.map((x) => [x.id, x])), [goals]);
  const parent = goal.parent_id ? byId.get(goal.parent_id) : undefined;
  const terminal = TERMINAL_STATUSES.has(st);

  // 목표 취소 — spec 3차 상태 어휘의 종결 상태는 cancelled.
  // (레거시 DELETE는 서버가 archived로 소프트 삭제하던 경로 — 명시 PATCH를 우선 사용)
  const cancel = async () => {
    const msg = t("goals.confirmCancel", "'{name}' 목표를 취소 처리할까요? 트리에서 숨겨집니다.")
      .replace("{name}", goal.title || goal.id);
    if (!confirm(msg)) return;
    setBusy(true);
    try {
      await apiPatch(`/goals/${encodeURIComponent(goal.id)}`, { status: "cancelled" });
      await qc.invalidateQueries({ queryKey: ["goals"] });
      onChanged();
    } catch (e) {
      alert(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50">
      <div className="absolute inset-0 bg-black/30" onClick={onClose} />
      <aside
        role="dialog"
        aria-modal="true"
        aria-label={goal.title || goal.id}
        className="absolute inset-y-0 right-0 flex w-full max-w-md flex-col overflow-y-auto border-l border-border bg-surface shadow-xl"
      >
        <div className="flex items-center gap-2 border-b border-border px-4 py-3">
          <h2 className="min-w-0 flex-1 truncate text-base font-bold text-ink">
            {goal.title || goal.id}
          </h2>
          <button
            type="button"
            onClick={onClose}
            aria-label={t("goals.close", "닫기")}
            className="rounded-md px-2 py-1 text-muted hover:bg-surface-hover"
          >
            ✕
          </button>
        </div>

        <div className="flex-1 p-4">
          {mode !== "view" ? (
            <GoalForm
              goals={goals}
              initial={mode === "edit" ? goal : null}
              presetParent={mode === "addSub" ? goal.id : null}
              onDone={() => {
                setMode("view");
                onChanged();
              }}
            />
          ) : (
            <div className="space-y-4">
              <div className="flex flex-wrap items-center gap-1.5">
                <Badge tone={STATUS_TONE[st] ?? "muted"}>
                  {statusLabel(t, st)}
                </Badge>
              </div>

              {goal.description && (
                <div>
                  <div className="mb-1 text-xs font-bold text-muted">{t("goals.description", "설명")}</div>
                  <p className="whitespace-pre-wrap text-sm text-ink-2">{goal.description}</p>
                </div>
              )}

              <dl className="grid grid-cols-1 gap-x-4 gap-y-2 text-sm sm:grid-cols-2">
                <div>
                  <dt className="text-xs text-muted">{t("goals.parent", "상위 목표")}</dt>
                  <dd className="text-ink">{parent ? parent.title || parent.id : t("goals.noParent", "없음 (최상위)")}</dd>
                </div>
                <div>
                  <dt className="text-xs text-muted">{t("goals.ownerAgent", "담당 에이전트")}</dt>
                  <dd className="text-ink">{goal.owner_agent_id || "-"}</dd>
                </div>
                <div>
                  <dt className="text-xs text-muted">{t("goals.targetAt", "목표 기한")}</dt>
                  <dd className="text-ink">{goal.target_at ? String(goal.target_at).slice(0, 10) : "-"}</dd>
                </div>
                <div>
                  <dt className="text-xs text-muted">{t("goals.createdAt", "등록일")}</dt>
                  <dd className="text-ink">{fmtTs(goal.created_at)}</dd>
                </div>
                <div>
                  <dt className="text-xs text-muted">{t("goals.updatedAt", "수정일")}</dt>
                  <dd className="text-ink">{fmtTs(goal.updated_at)}</dd>
                </div>
              </dl>

              {goal.success_metrics != null && goal.success_metrics !== "" && (
                <div>
                  <div className="mb-1 text-xs font-bold text-muted">{t("goals.successMetrics", "성공 지표")}</div>
                  <pre className="max-h-48 overflow-auto rounded-md bg-surface-hover p-2 text-xs text-ink-2">
                    {jsonSummary(goal.success_metrics, 2000)}
                  </pre>
                </div>
              )}

              <div className="flex flex-wrap gap-2 border-t border-border pt-3">
                <Button variant="secondary" className="px-3 py-1.5 text-xs" onClick={() => setMode("edit")}>
                  {t("goals.edit", "편집")}
                </Button>
                <Button variant="secondary" className="px-3 py-1.5 text-xs" onClick={() => setMode("addSub")}>
                  {t("goals.addSub", "하위 목표 추가")}
                </Button>
                {!terminal && (
                  <Button variant="danger" className="px-3 py-1.5 text-xs" disabled={busy} onClick={cancel}>
                    {t("goals.cancelGoal", "목표 취소")}
                  </Button>
                )}
              </div>
            </div>
          )}
        </div>
      </aside>
    </div>
  );
}

function GoalsBody() {
  const { t } = useI18n();
  const qc = useQueryClient();
  const [q, setQ] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");
  const [showForm, setShowForm] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["goals"],
    queryFn: () => apiGet<GoalsResponse>("/goals"),
    refetchInterval: 60000,
  });

  const all = useMemo(() => (Array.isArray(data?.items) ? data.items : []), [data]);
  const byId = useMemo(() => new Map(all.map((g) => [g.id, g])), [all]);
  const selected = selectedId ? byId.get(selectedId) : undefined;

  // 상태 필터 + 제목/설명 검색 → 매칭 노드와 그 조상(문맥 유지)을 표시.
  const forest = useMemo(() => {
    const needle = q.trim().toLowerCase();
    const matches = all.filter((g) => {
      if (statusFilter !== "all" && statusOf(g) !== statusFilter) return false;
      if (!needle) return true;
      return [g.title, g.description].some((v) => (v || "").toLowerCase().includes(needle));
    });
    const visible = new Set(matches.map((g) => g.id));
    for (const m of matches) {
      let cur = m.parent_id || "";
      let guard = 0;
      while (cur && guard++ < 64) {
        if (visible.has(cur)) break;
        const p = byId.get(cur);
        if (!p) break;
        visible.add(p.id);
        cur = p.parent_id || "";
      }
    }
    return buildForest(all.filter((g) => visible.has(g.id)));
  }, [all, q, statusFilter, byId]);

  return (
    <div className="mx-auto max-w-6xl p-4">
      <PageHead
        title={t("goals.title", "목표")}
        desc={t("goals.desc", "목표 트리 — 상위/하위 목표와 진행 상태")}
      >
        <select
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
          aria-label={t("goals.statusAll", "전체 상태")}
          className="rounded-lg border border-border bg-surface px-2 py-1.5 text-sm"
        >
          <option value="all">{t("goals.statusAll", "전체 상태")}</option>
          {STATUSES.map((s) => (
            <option key={s} value={s}>{statusLabel(t, s)}</option>
          ))}
        </select>
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder={t("goals.searchPh", "제목·설명 검색")}
          className="w-40 rounded-lg border border-border bg-surface px-2.5 py-1.5 text-sm outline-none focus:border-accent"
        />
        <Button variant="secondary" onClick={() => setShowForm((v) => !v)}>
          {showForm ? t("common.close", "닫기") : "+ " + t("goals.add", "목표 추가")}
        </Button>
        <Button variant="secondary" onClick={() => refetch()} className="px-3 py-1.5 text-xs">
          {t("common.refresh", "새로고침")}
        </Button>
      </PageHead>

      {showForm && (
        <Card className="mb-3" title={t("goals.add", "목표 추가")}>
          <GoalForm
            goals={all}
            onDone={() => {
              setShowForm(false);
              qc.invalidateQueries({ queryKey: ["goals"] });
            }}
          />
        </Card>
      )}

      <Card>
        {isLoading && <Spinner />}
        {error && (
          <ErrorBox message={error instanceof Error ? error.message : String(error)} onRetry={() => refetch()} />
        )}
        {data && !all.length && (
          <Empty>
            <div>{t("goals.empty", "등록된 목표가 없습니다")}</div>
            <div className="mt-1 text-xs">{t("goals.emptyHint", "첫 목표를 추가해 트리를 시작하세요")}</div>
          </Empty>
        )}
        {data && all.length > 0 && forest.length === 0 && (
          <Empty>{t("goals.noMatch", "조건에 맞는 목표가 없습니다")}</Empty>
        )}
        {forest.length > 0 && (
          <div className="gld-scroll">
            <ul className="gld-tree">
              {forest.map((n) => (
                <GoalNodeView key={n.goal.id} node={n} selectedId={selectedId ?? undefined} onSelect={(g) => setSelectedId(g.id)} />
              ))}
            </ul>
          </div>
        )}
      </Card>

      {selected && (
        <GoalDrawer
          key={selected.id}
          goal={selected}
          goals={all}
          onClose={() => setSelectedId(null)}
          onChanged={() => qc.invalidateQueries({ queryKey: ["goals"] })}
        />
      )}
    </div>
  );
}

export default function GoalsPage() {
  return <GoalsBody />;
}
