/** agents — /agents 에이전트 조직도(reports_to 트리) + 등록/편집/보관.
 *  API 계약: GET/POST /agents, PATCH/DELETE /agents/{id} (DELETE는 보관 처리). */
import { useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { apiGet, apiPost, apiPatch, apiDelete, ApiError } from "../../shared/api/client";
import { useI18n } from "../../shared/i18n";
import { useAppStore, roleAllows } from "../../shared/store/appStore";
import { PageHead, Card, Empty, Spinner, ErrorBox, Badge, Button } from "../../shared/ui";
import { usd, fmtTs, fmtAge } from "../../shared/lib/format";
import { buildForest, type Agent, type AgentNode, type AgentsResponse } from "./tree";
import "./i18n";
import "./agents.css";

const ADAPTER_TYPES = ["claude", "codex", "gemini", "devin", "custom"];
const STATUSES = ["active", "paused", "archived"];

const STATUS_TONE: Record<string, "ok" | "warn" | "muted"> = {
  active: "ok",
  paused: "warn",
  archived: "muted",
};

const STATUS_LABEL_KEY: Record<string, string> = {
  active: "agents.statusActive",
  paused: "agents.statusPaused",
  archived: "agents.statusArchived",
};

function statusLabel(t: (k: string, f?: string) => string, st: string): string {
  return t(STATUS_LABEL_KEY[st] || "", st);
}

function statusOf(a: Agent): string {
  return String(a.status || "active").toLowerCase();
}

/** last_run_at → 상대 시간 (없으면 "-"). */
function lastRunAgo(iso: string | null | undefined): string {
  if (!iso) return "-";
  const ms = Date.now() - new Date(iso).getTime();
  if (!isFinite(ms) || ms < 0) return "-";
  return fmtAge(ms / 1000);
}

const inputCls =
  "w-full rounded-lg border border-border bg-surface px-2.5 py-1.5 text-sm outline-none focus:border-accent";

/** 에이전트 등록/편집 폼 — initial이 있으면 PATCH, 없으면 POST.
 *  presetReportsTo는 "하위 에이전트 추가"에서 보고 대상을 미리 채운다. */
function AgentForm({
  agents,
  initial,
  presetReportsTo,
  onDone,
}: {
  agents: Agent[];
  initial?: Agent | null;
  presetReportsTo?: string | null;
  onDone: (saved?: Agent) => void;
}) {
  const { t } = useI18n();
  const qc = useQueryClient();
  const [name, setName] = useState(initial?.name || "");
  const [adapter, setAdapter] = useState(initial?.adapter_type || "claude");
  const [role, setRole] = useState(initial?.role || "");
  const [desc, setDesc] = useState(initial?.description || "");
  const [caps, setCaps] = useState((initial?.capabilities || []).join(", "));
  const [parent, setParent] = useState<string>(
    presetReportsTo ?? initial?.reports_to ?? "",
  );
  const [budget, setBudget] = useState(
    initial?.monthly_budget_usd == null ? "" : String(initial.monthly_budget_usd),
  );
  const [status, setStatus] = useState<string>(initial ? statusOf(initial) : "active");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  // 보고 대상 후보 — 본인·보관된 에이전트 제외. 현재 선택값이 후보에 없으면 유지.
  const parentOptions = useMemo(() => {
    const list = agents.filter(
      (a) => a.id !== initial?.id && statusOf(a) !== "archived",
    );
    if (parent && !list.some((a) => a.id === parent)) {
      const cur = agents.find((a) => a.id === parent);
      if (cur) return [cur, ...list];
    }
    return list;
  }, [agents, initial?.id, parent]);

  const submit = async () => {
    if (!name.trim()) {
      setErr(t("agents.needName", "이름을 입력하세요"));
      return;
    }
    if (!ADAPTER_TYPES.includes(adapter)) {
      setErr(t("agents.needAdapter", "어댑터 유형을 선택하세요"));
      return;
    }
    let budgetNum: number | null | undefined;
    if (budget.trim() === "") {
      budgetNum = initial ? null : undefined; // 편집 시 빈 값 = 한도 해제
    } else {
      const n = Number(budget);
      if (!isFinite(n) || n < 0) {
        setErr(t("agents.needBudget", "월 예산은 0 이상의 숫자로 입력하세요"));
        return;
      }
      budgetNum = n;
    }
    const capsArr = caps
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);

    const body: Record<string, unknown> = {
      name: name.trim(),
      adapter_type: adapter,
      status,
    };
    if (role.trim()) body.role = role.trim();
    else if (initial) body.role = null;
    if (desc.trim()) body.description = desc.trim();
    else if (initial) body.description = null;
    body.capabilities = capsArr;
    if (initial) {
      // 편집: 명시적 null로 보고 대상/예산 해제를 지원한다.
      body.reports_to = parent || null;
      body.monthly_budget_usd = budgetNum === undefined ? null : budgetNum;
    } else {
      if (parent) body.reports_to = parent;
      if (budgetNum !== undefined) body.monthly_budget_usd = budgetNum;
    }

    setBusy(true);
    setErr("");
    try {
      let saved: Agent | undefined;
      if (initial?.id) {
        saved = await apiPatch<Agent>(`/agents/${encodeURIComponent(initial.id)}`, body);
      } else {
        saved = await apiPost<Agent>("/agents", body);
      }
      await qc.invalidateQueries({ queryKey: ["agents"] });
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
          <span className="mb-1 block text-xs font-medium text-muted">{t("agents.name", "이름")}</span>
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder={t("agents.namePh", "예: 코드 리뷰 봇")} className={inputCls} />
        </label>
        <label className="block">
          <span className="mb-1 block text-xs font-medium text-muted">{t("agents.adapterType", "어댑터 유형")}</span>
          <select value={adapter} onChange={(e) => setAdapter(e.target.value)} className={inputCls}>
            {ADAPTER_TYPES.map((x) => (
              <option key={x} value={x}>{x}</option>
            ))}
          </select>
        </label>
        <label className="block">
          <span className="mb-1 block text-xs font-medium text-muted">{t("agents.role", "역할")}</span>
          <input value={role} onChange={(e) => setRole(e.target.value)} placeholder={t("agents.rolePh", "예: 리뷰어")} className={inputCls} />
        </label>
        <label className="block">
          <span className="mb-1 block text-xs font-medium text-muted">{t("agents.reportsToSelect", "보고 대상 (선택)")}</span>
          <select value={parent} onChange={(e) => setParent(e.target.value)} className={inputCls}>
            <option value="">{t("agents.noParent", "없음 (최상위)")}</option>
            {parentOptions.map((a) => (
              <option key={a.id} value={a.id}>{a.name || a.id}</option>
            ))}
          </select>
        </label>
        <label className="block">
          <span className="mb-1 block text-xs font-medium text-muted">{t("agents.budgetPh", "월 예산 USD (선택)")}</span>
          <input value={budget} onChange={(e) => setBudget(e.target.value)} type="number" min="0" step="0.01" placeholder="0.00" className={inputCls} />
        </label>
        {initial && (
          <label className="block">
            <span className="mb-1 block text-xs font-medium text-muted">{t("agents.statusField", "상태")}</span>
            <select value={status} onChange={(e) => setStatus(e.target.value)} className={inputCls}>
              {STATUSES.map((s) => (
                <option key={s} value={s}>{statusLabel(t, s)}</option>
              ))}
            </select>
          </label>
        )}
      </div>
      <label className="block">
        <span className="mb-1 block text-xs font-medium text-muted">{t("agents.description", "설명")}</span>
        <input value={desc} onChange={(e) => setDesc(e.target.value)} placeholder={t("agents.descriptionPh", "설명 (선택)")} className={inputCls} />
      </label>
      <label className="block">
        <span className="mb-1 block text-xs font-medium text-muted">{t("agents.capabilities", "기능")}</span>
        <input value={caps} onChange={(e) => setCaps(e.target.value)} placeholder={t("agents.capabilitiesPh", "기능 (쉼표로 구분, 예: 리뷰, 테스트)")} className={inputCls} />
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

/** 조직도 노드 카드. */
function AgentCard({
  node,
  selected,
  onSelect,
  lastRunLabel,
}: {
  node: AgentNode;
  selected: boolean;
  onSelect: (a: Agent) => void;
  lastRunLabel: string;
}) {
  const { t } = useI18n();
  const a = node.agent;
  const st = statusOf(a);
  return (
    <button
      type="button"
      onClick={() => onSelect(a)}
      className={`w-52 rounded-xl border bg-surface p-3 text-left shadow-sm transition-colors hover:border-accent-border ${
        selected ? "border-accent ring-1 ring-accent" : "border-border"
      } ${st === "archived" ? "opacity-60" : ""}`}
    >
      <div className="flex items-center gap-1.5">
        <span className="min-w-0 flex-1 truncate text-sm font-semibold text-ink">
          {a.name || a.id}
        </span>
      </div>
      <div className="mt-1.5 flex flex-wrap items-center gap-1">
        <Badge tone="accent">{a.adapter_type || "custom"}</Badge>
        <Badge tone={STATUS_TONE[st] ?? "muted"}>
          {statusLabel(t, st)}
        </Badge>
      </div>
      {a.role && <div className="mt-1 truncate text-xs text-muted">{a.role}</div>}
      <div className="mt-1.5 text-[11px] text-muted">
        {t("agents.runs30", "30일 실행")} {a.runs_30d ?? 0} · {usd(a.cost_30d_usd ?? 0)}
      </div>
      <div className="text-[11px] text-muted">
        {t("agents.lastRun", "최근 실행")} {lastRunLabel}
      </div>
    </button>
  );
}

function OrgNode({
  node,
  selectedId,
  onSelect,
}: {
  node: AgentNode;
  selectedId?: string;
  onSelect: (a: Agent) => void;
}) {
  const { t } = useI18n();
  const a = node.agent;
  return (
    <li>
      <AgentCard
        node={node}
        selected={a.id === selectedId}
        onSelect={onSelect}
        lastRunLabel={a.last_run_at ? lastRunAgo(a.last_run_at) : t("agents.neverRun", "실행 기록 없음")}
      />
      {node.children.length > 0 && (
        <ul className="agt-list agt-kids">
          {node.children.map((c) => (
            <OrgNode key={c.agent.id} node={c} selectedId={selectedId} onSelect={onSelect} />
          ))}
        </ul>
      )}
    </li>
  );
}

/** 상세/편집 사이드 패널. */
function AgentDrawer({
  agent,
  agents,
  onClose,
  onChanged,
}: {
  agent: Agent;
  agents: Agent[];
  onClose: () => void;
  onChanged: () => void;
}) {
  const { t } = useI18n();
  const qc = useQueryClient();
  const [mode, setMode] = useState<"view" | "edit" | "addReport">("view");
  const [busy, setBusy] = useState(false);

  const st = statusOf(agent);
  const byId = useMemo(() => new Map(agents.map((x) => [x.id, x])), [agents]);
  const parent = agent.reports_to ? byId.get(agent.reports_to) : undefined;
  const caps = Array.isArray(agent.capabilities) ? agent.capabilities : [];

  const archive = async () => {
    const msg = t("agents.confirmArchive", "'{name}' 에이전트를 보관 처리할까요? 조직도에서 숨겨지고 실행이 중단됩니다.")
      .replace("{name}", agent.name || agent.id);
    if (!confirm(msg)) return;
    setBusy(true);
    try {
      await apiDelete(`/agents/${encodeURIComponent(agent.id)}`);
      await qc.invalidateQueries({ queryKey: ["agents"] });
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
        aria-label={agent.name || agent.id}
        className="absolute inset-y-0 right-0 flex w-full max-w-md flex-col overflow-y-auto border-l border-border bg-surface shadow-xl"
      >
        <div className="flex items-center gap-2 border-b border-border px-4 py-3">
          <h2 className="min-w-0 flex-1 truncate text-base font-bold text-ink">
            {agent.name || agent.id}
          </h2>
          <button
            type="button"
            onClick={onClose}
            aria-label={t("agents.close", "닫기")}
            className="rounded-md px-2 py-1 text-muted hover:bg-surface-hover"
          >
            ✕
          </button>
        </div>

        <div className="flex-1 p-4">
          {mode !== "view" ? (
            <AgentForm
              agents={agents}
              initial={mode === "edit" ? agent : null}
              presetReportsTo={mode === "addReport" ? agent.id : null}
              onDone={() => {
                setMode("view");
                onChanged();
              }}
            />
          ) : (
            <div className="space-y-4">
              <div className="flex flex-wrap items-center gap-1.5">
                <Badge tone="accent">{agent.adapter_type || "custom"}</Badge>
                <Badge tone={STATUS_TONE[st] ?? "muted"}>
                  {statusLabel(t, st)}
                </Badge>
                {agent.role && <Badge tone="muted">{agent.role}</Badge>}
              </div>

              {agent.description && (
                <div>
                  <div className="mb-1 text-xs font-bold text-muted">{t("agents.description", "설명")}</div>
                  <p className="text-sm text-ink-2">{agent.description}</p>
                </div>
              )}

              {caps.length > 0 && (
                <div>
                  <div className="mb-1 text-xs font-bold text-muted">{t("agents.capabilities", "기능")}</div>
                  <div className="flex flex-wrap gap-1">
                    {caps.map((c, i) => (
                      <Badge key={i} tone="accent">{c}</Badge>
                    ))}
                  </div>
                </div>
              )}

              <dl className="grid grid-cols-1 gap-x-4 gap-y-2 text-sm sm:grid-cols-2">
                <div>
                  <dt className="text-xs text-muted">{t("agents.monthlyBudget", "월 예산")}</dt>
                  <dd className="text-ink">{agent.monthly_budget_usd != null ? usd(agent.monthly_budget_usd) : t("agents.noBudget", "제한 없음")}</dd>
                </div>
                <div>
                  <dt className="text-xs text-muted">{t("agents.reportsTo", "보고 대상")}</dt>
                  <dd className="text-ink">{parent ? parent.name || parent.id : t("agents.noParent", "없음 (최상위)")}</dd>
                </div>
                <div>
                  <dt className="text-xs text-muted">{t("agents.runs30", "30일 실행")}</dt>
                  <dd className="text-ink">{agent.runs_30d ?? 0} · {usd(agent.cost_30d_usd ?? 0)}</dd>
                </div>
                <div>
                  <dt className="text-xs text-muted">{t("agents.lastRun", "최근 실행")}</dt>
                  <dd className="text-ink">{agent.last_run_at ? `${fmtTs(agent.last_run_at)} (${lastRunAgo(agent.last_run_at)})` : t("agents.neverRun", "실행 기록 없음")}</dd>
                </div>
                <div>
                  <dt className="text-xs text-muted">{t("agents.createdAt", "등록일")}</dt>
                  <dd className="text-ink">{fmtTs(agent.created_at)}</dd>
                </div>
                <div>
                  <dt className="text-xs text-muted">{t("agents.updatedAt", "수정일")}</dt>
                  <dd className="text-ink">{fmtTs(agent.updated_at)}</dd>
                </div>
              </dl>

              <div className="flex flex-wrap gap-2 border-t border-border pt-3">
                <Button variant="secondary" className="px-3 py-1.5 text-xs" onClick={() => setMode("edit")}>
                  {t("agents.edit", "편집")}
                </Button>
                <Button variant="secondary" className="px-3 py-1.5 text-xs" onClick={() => setMode("addReport")}>
                  {t("agents.addReport", "하위 에이전트 추가")}
                </Button>
                {st !== "archived" && (
                  <Button variant="danger" className="px-3 py-1.5 text-xs" disabled={busy} onClick={archive}>
                    {t("agents.archive", "보관")}
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

function AgentsBody() {
  const { t } = useI18n();
  const qc = useQueryClient();
  const [q, setQ] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");
  const [showForm, setShowForm] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["agents"],
    queryFn: () => apiGet<AgentsResponse>("/agents"),
    refetchInterval: 60000,
  });

  const all = useMemo(() => (Array.isArray(data?.items) ? data.items : []), [data]);
  const byId = useMemo(() => new Map(all.map((a) => [a.id, a])), [all]);
  const selected = selectedId ? byId.get(selectedId) : undefined;

  // 상태 필터 + 이름/역할 검색 → 매칭 노드와 그 조상(문맥 유지)을 표시.
  const forest = useMemo(() => {
    const needle = q.trim().toLowerCase();
    const matches = all.filter((a) => {
      if (statusFilter !== "all" && statusOf(a) !== statusFilter) return false;
      if (!needle) return true;
      return [a.name, a.role].some((v) => (v || "").toLowerCase().includes(needle));
    });
    const visible = new Set(matches.map((a) => a.id));
    for (const m of matches) {
      let cur = m.reports_to || "";
      let guard = 0;
      while (cur && guard++ < 64) {
        if (visible.has(cur)) break;
        const p = byId.get(cur);
        if (!p) break;
        visible.add(p.id);
        cur = p.reports_to || "";
      }
    }
    return buildForest(all.filter((a) => visible.has(a.id)));
  }, [all, q, statusFilter, byId]);

  return (
    <div className="mx-auto max-w-6xl p-4">
      <PageHead
        title={t("agents.title", "에이전트")}
        desc={t("agents.desc", "에이전트 조직도 — 보고 관계와 최근 30일 실행·비용")}
      >
        <select
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
          aria-label={t("agents.statusAll", "전체 상태")}
          className="rounded-lg border border-border bg-surface px-2 py-1.5 text-sm"
        >
          <option value="all">{t("agents.statusAll", "전체 상태")}</option>
          {STATUSES.map((s) => (
            <option key={s} value={s}>{statusLabel(t, s)}</option>
          ))}
        </select>
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder={t("agents.searchPh", "이름·역할 검색")}
          className="w-40 rounded-lg border border-border bg-surface px-2.5 py-1.5 text-sm outline-none focus:border-accent"
        />
        <Button variant="secondary" onClick={() => setShowForm((v) => !v)}>
          {showForm ? t("common.close", "닫기") : "+ " + t("agents.add", "에이전트 추가")}
        </Button>
        <Button variant="secondary" onClick={() => refetch()} className="px-3 py-1.5 text-xs">
          {t("common.refresh", "새로고침")}
        </Button>
      </PageHead>

      {showForm && (
        <Card className="mb-3" title={t("agents.add", "에이전트 추가")}>
          <AgentForm
            agents={all}
            onDone={() => {
              setShowForm(false);
              qc.invalidateQueries({ queryKey: ["agents"] });
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
            <div>{t("agents.empty", "등록된 에이전트가 없습니다")}</div>
            <div className="mt-1 text-xs">{t("agents.emptyHint", "첫 에이전트를 추가해 조직도를 시작하세요")}</div>
          </Empty>
        )}
        {data && all.length > 0 && forest.length === 0 && (
          <Empty>{t("agents.noMatch", "조건에 맞는 에이전트가 없습니다")}</Empty>
        )}
        {forest.length > 0 && (
          <div className="agt-scroll">
            <ul className="agt-tree">
              {forest.map((n) => (
                <OrgNode key={n.agent.id} node={n} selectedId={selectedId ?? undefined} onSelect={(a) => setSelectedId(a.id)} />
              ))}
            </ul>
          </div>
        )}
      </Card>

      {selected && (
        <AgentDrawer
          key={selected.id}
          agent={selected}
          agents={all}
          onClose={() => setSelectedId(null)}
          onChanged={() => qc.invalidateQueries({ queryKey: ["agents"] })}
        />
      )}
    </div>
  );
}

export default function AgentsPage() {
  const role = useAppStore((s) => s.role);
  const { t } = useI18n();
  if (role != null && !roleAllows("lead", role)) {
    return (
      <div className="mx-auto max-w-6xl p-4">
        <PageHead title={t("agents.title", "에이전트")} />
        <Empty>{t("agents.leadOnly", "이 화면은 리드(lead) 이상 전용입니다.")}</Empty>
      </div>
    );
  }
  return <AgentsBody />;
}
