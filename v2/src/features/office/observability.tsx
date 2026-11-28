/** 관측 센터 확장 섹션 — spec 011 US2.
 *  /observability/{notifications,funnel,agents/live,swarm} 실데이터 렌더.
 *  백엔드 병렬 개발 중이라 모든 쿼리는 retry:false, 403/404는 섹션 단위 강등. */
import { useMemo, useState, type ReactNode } from "react";
import { ApiError } from "../../shared/api/client";
import { useI18n } from "../../shared/i18n";
import { Badge, Button, Card, Empty, Spinner, ErrorBox } from "../../shared/ui";
import { fmtElapsed, fmtTs, usd } from "../../shared/lib/format";
import { useAppStore, roleAllows } from "../../shared/store/appStore";
import { TrendArea } from "./charts";
import { StatusBadge } from "./sections";
import {
  useObsFunnel, useObsLiveAgents, useObsNotifications, useObsSwarm, useObsToolStats,
} from "./queries";
import type {
  ObsLiveAgent, ObsSwarmEdge, ObsSwarmNode,
} from "./types";

const DAYS = ["7", "30", "90"];

function isForbidden(e: unknown): boolean {
  return e instanceof ApiError && e.status === 403;
}

function errMsg(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

function DaysSelect({ days, setDays, label }: { days: string; setDays: (v: string) => void; label: string }) {
  const { t } = useI18n();
  return (
    <select
      value={days}
      onChange={(e) => setDays(e.target.value)}
      aria-label={label}
      className="rounded-md border border-border bg-surface px-2 py-1 text-xs"
    >
      {DAYS.map((d) => (
        <option key={d} value={d}>{t(`common.last${d}`, `최근 ${d}일`)}</option>
      ))}
    </select>
  );
}

/** 쿼리 상태 → 본문 렌더 공통 처리. children는 데이터 있을 때만 호출.
 *  blocked=true(role 미달로 쿼리 disabled)면 forbidden Empty를 표시한다 —
 *  disabled 쿼리는 data 없이 pending이라 Spinner로 빠지는 것을 방지. */
function QueryGate<T>({ q, forbidden, blocked, children }: {
  q: { isLoading: boolean; isFetching: boolean; error: unknown; data: T | undefined; refetch: () => unknown };
  forbidden: string;
  blocked?: boolean;
  children: (data: T) => ReactNode;
}) {
  const { t } = useI18n();
  if (blocked) {
    return <Empty>{forbidden || t("office.forbidden", "권한 부족 — 이 항목은 lead 역할에서 확인할 수 있습니다")}</Empty>;
  }
  if (q.isLoading || (q.isFetching && q.data == null)) return <Spinner />;
  if (q.error) {
    return isForbidden(q.error)
      ? <Empty>{forbidden || t("office.forbidden", "권한 부족 — 이 항목은 lead 역할에서 확인할 수 있습니다")}</Empty>
      : <ErrorBox message={errMsg(q.error)} onRetry={() => void q.refetch()} />;
  }
  if (q.data == null) return <Spinner />;
  return <>{children(q.data)}</>;
}

// ---------- 실행 퍼널 ----------

export function FunnelSection() {
  const { t } = useI18n();
  const [days, setDays] = useState("7");
  const q = useObsFunnel(Number(days));
  const setView = useAppStore((s) => s.setView);
  const role = useAppStore((s) => s.role);

  const stages = useMemo(
    () => (Array.isArray(q.data?.stages) ? q.data.stages : []),
    [q.data],
  );
  const first = stages[0]?.count ?? 0;
  const maxCount = Math.max(1e-9, ...stages.map((x) => Number(x.count) || 0));

  return (
    <Card
      title={t("office.funnel.title", "실행 퍼널")}
      actions={
        <div className="flex items-center gap-2">
          <DaysSelect days={days} setDays={setDays} label={t("office.funnel.daysAria", "조회 기간")} />
          <Button variant="ghost" className="px-2 py-0.5 text-xs" onClick={() => setView("runs")}>
            {t("office.funnel.runsLink", "실행 이력")}
          </Button>
        </div>
      }
    >
      <QueryGate q={q} forbidden={t("office.funnel.forbidden", "권한 부족 — 퍼널은 lead 역할에서 확인할 수 있습니다")} blocked={!roleAllows("lead", role)}>
        {() => (
          !stages.length ? <Empty>{t("office.funnel.empty", "퍼널 데이터가 없습니다")}</Empty> : (
            <div className="space-y-2">
              {stages.map((s, i) => {
                const count = Number(s.count) || 0;
                const pctOfFirst = first > 0 ? (count / first) * 100 : 0;
                const width = Math.max(1.5, (count / maxCount) * 100);
                return (
                  <div key={s.key || i}>
                    <div className="flex items-baseline justify-between gap-2 text-xs">
                      <span className="min-w-0 truncate font-medium text-ink">
                        {s.label || s.key || `stage ${i + 1}`}
                      </span>
                      <span className="flex-none tabular-nums text-muted">
                        {count.toLocaleString()}
                        <span className="ml-1 text-[10px]">({pctOfFirst.toFixed(0)}%)</span>
                      </span>
                    </div>
                    <div className="mt-0.5 h-2 overflow-hidden rounded-full bg-surface-hover">
                      <div
                        className={`h-full rounded-full ${i === stages.length - 1 ? "bg-ok/80" : "bg-accent/80"}`}
                        style={{ width: `${width}%` }}
                      />
                    </div>
                  </div>
                );
              })}
              <div className="pt-1 text-[10px] text-muted">
                {t("office.funnel.note", "괄호는 첫 단계 대비 비율")}
              </div>
            </div>
          )
        )}
      </QueryGate>
    </Card>
  );
}

// ---------- 알림 현황 ----------

export function NotificationsSection() {
  const { t } = useI18n();
  const [days, setDays] = useState("7");
  const q = useObsNotifications(Number(days));
  const setView = useAppStore((s) => s.setView);
  const role = useAppStore((s) => s.role);

  const series = useMemo(
    () => (Array.isArray(q.data?.series) ? q.data.series : [])
      .map((p) => ({ label: String(p.t || "").slice(5, 10), value: Number(p.count) || 0 }))
      .filter((p) => p.label),
    [q.data],
  );
  const recent = Array.isArray(q.data?.recent) ? q.data.recent : [];

  return (
    <Card
      title={t("office.notif.title", "알림 현황")}
      actions={
        <div className="flex items-center gap-2">
          <DaysSelect days={days} setDays={setDays} label={t("office.notif.daysAria", "조회 기간")} />
          <Button variant="ghost" className="px-2 py-0.5 text-xs" onClick={() => setView("inbox")}>
            {t("office.notif.inboxLink", "알림함")}
          </Button>
        </div>
      }
    >
      <QueryGate q={q} forbidden={t("office.notif.forbidden", "권한 부족 — 알림 현황은 lead 역할에서 확인할 수 있습니다")} blocked={!roleAllows("lead", role)}>
        {(d) => (
          <>
              <div className="mb-3 grid grid-cols-3 gap-2">
                {([
                  ["total", t("office.notif.total", "전체"), "muted"],
                  ["open", t("office.notif.open", "미해결"), "warn"],
                  ["resolved", t("office.notif.resolved", "해결"), "ok"],
                ] as const).map(([key, label, tone]) => (
                  <div key={key} className="rounded-lg border border-border bg-surface-hover px-2.5 py-1.5">
                    <div className="text-[10px] text-muted">{label}</div>
                    <div className={`text-base font-semibold tabular-nums ${
                      tone === "warn" ? "text-amber-600" : tone === "ok" ? "text-emerald-600" : "text-ink"
                    }`}>
                      {Number(d[key]) || 0}
                    </div>
                  </div>
                ))}
              </div>

              <div className="mb-1 text-[11px] font-medium text-muted">
                {t("office.notif.trend", "발생 추이")}
              </div>
              <TrendArea points={series} height={80} empty={t("office.notif.emptyTrend", "기간 내 알림이 없습니다")} />

              <div className="mb-1 mt-3 text-[11px] font-medium text-muted">
                {t("office.notif.recent", "최근 알림")}
              </div>
              {!recent.length ? (
                <Empty>{t("office.notif.empty", "알림이 없습니다")}</Empty>
              ) : (
                <ul className="max-h-48 space-y-1 overflow-y-auto">
                  {recent.slice(0, 10).map((n, i) => (
                    <li key={n.id || i} className="flex items-center justify-between gap-2 rounded-md border border-border px-2 py-1.5">
                      <span className="min-w-0 flex-1 truncate text-xs text-ink" title={n.title}>
                        {n.title || n.id || "-"}
                      </span>
                      <span className="flex flex-none items-center gap-1.5">
                        {n.status && <Badge tone={String(n.status) === "resolved" ? "ok" : "warn"}>{String(n.status)}</Badge>}
                        <span className="text-[10px] text-muted">{fmtTs(n.created_at)}</span>
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </>
        )}
      </QueryGate>
    </Card>
  );
}

// ---------- 라이브 에이전트 ----------

function fmtTokens(v: number | null | undefined): string {
  const n = Number(v);
  return v == null || !isFinite(n) ? "-" : n.toLocaleString();
}

function LiveAgentCard({ a }: { a: ObsLiveAgent }) {
  const { t } = useI18n();
  const setView = useAppStore((s) => s.setView);
  const pct = a.progress_pct != null && isFinite(Number(a.progress_pct))
    ? Math.max(0, Math.min(100, Number(a.progress_pct))) : null;
  const elapsed = a.elapsed_s != null && isFinite(Number(a.elapsed_s))
    ? `${Math.round(Number(a.elapsed_s))}s`
    : fmtElapsed(a.started_at);
  const go = a.run_id ? () => setView("runs")
    : a.session_id || a.session_key ? () => setView("sessionlog") : undefined;

  const body = (
    <div className="rounded-lg border border-border bg-surface p-3">
      <div className="flex items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          <StatusBadge status={a.status} />
          <span className="truncate text-sm font-medium text-ink">{a.name || a.id || "-"}</span>
        </div>
        {a.model && <span className="flex-none font-mono text-[10px] text-muted">{a.model}</span>}
      </div>
      {(a.role || a.current_task) && (
        <div className="mt-0.5 truncate text-[11px] text-muted" title={a.current_task || a.role}>
          {[a.role, a.current_task].filter(Boolean).join(" · ")}
        </div>
      )}
      {pct != null && (
        <div className="mt-2">
          <div className="flex items-baseline justify-between text-[10px] text-muted">
            <span>{t("office.liveAgents.progress", "진행률")}</span>
            <span className="tabular-nums">{pct.toFixed(0)}%</span>
          </div>
          <div className="mt-0.5 h-1.5 overflow-hidden rounded-full bg-surface-hover">
            <div className="h-full rounded-full bg-accent" style={{ width: `${pct}%` }} />
          </div>
        </div>
      )}
      <div className="mt-2 grid grid-cols-2 gap-x-3 gap-y-0.5 border-t border-border pt-2 text-[10px] text-muted sm:grid-cols-4">
        <span>{t("office.live.elapsed", "경과")} <b className="font-medium tabular-nums text-ink-2">{elapsed}</b></span>
        <span>{t("office.live.tokens", "토큰")} <b className="font-medium tabular-nums text-ink-2">{fmtTokens(a.tokens)}</b></span>
        <span>{t("office.col.cost", "비용")} <b className="font-medium tabular-nums text-ink-2">{a.cost_usd != null ? usd(a.cost_usd) : "-"}</b></span>
        <span>{t("office.liveAgents.tools", "도구")} <b className="font-medium tabular-nums text-ink-2">{a.tool_calls ?? "-"}{a.sub_agents != null ? ` · ${a.sub_agents}` : ""}</b></span>
      </div>
      {a.last_heartbeat_at && (
        <div className="mt-1 text-[10px] text-muted">
          {t("office.liveAgents.heartbeat", "하트비트")} {fmtTs(a.last_heartbeat_at)}
        </div>
      )}
    </div>
  );

  return go ? (
    <button type="button" onClick={go} className="block w-full text-left transition-opacity hover:opacity-80">
      {body}
    </button>
  ) : body;
}

export function LiveAgentsSection() {
  const { t } = useI18n();
  const q = useObsLiveAgents();
  const role = useAppStore((s) => s.role);
  const items = useMemo(
    () => (Array.isArray(q.data?.items) ? q.data.items : []),
    [q.data],
  );

  return (
    <Card
      title={t("office.liveAgents.title", "라이브 에이전트")}
      actions={q.data?.count != null ? <Badge tone="accent">{q.data.count}</Badge> : undefined}
    >
      <QueryGate q={q} forbidden={t("office.liveAgents.forbidden", "권한 부족 — 라이브 에이전트는 lead 역할에서 확인할 수 있습니다")} blocked={!roleAllows("lead", role)}>
        {() => (
          !items.length ? <Empty>{t("office.liveAgents.empty", "실행 중인 에이전트가 없습니다")}</Empty> : (
            <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
              {items.map((a, i) => <LiveAgentCard key={a.id || i} a={a} />)}
            </div>
          )
        )}
      </QueryGate>
    </Card>
  );
}

// ---------- 스웜 그래프 ----------

const SWARM_NODE_W = 176;
const SWARM_NODE_H = 46;
const SWARM_GAP_X = 72;
const SWARM_GAP_Y = 18;
const SWARM_PAD = 16;

/** kind → 대략적인 파이프라인 순서 (엣지 없는 고립 노드 배치용). */
const SWARM_KIND_ORDER = [
  "user_input", "input", "user", "planner", "reasoning", "agent", "main_agent",
  "research", "coding", "tool", "mcp", "database", "external_api", "llm",
  "reviewer", "testing", "security", "output",
];

interface PlacedNode { node: ObsSwarmNode; idx: number; x: number; y: number; layer: number }
interface SwarmLayout {
  placed: PlacedNode[];
  viewW: number;
  viewH: number;
  byId: Map<string, { node: ObsSwarmNode; idx: number }>;
}

function swarmStatusColor(status: string | undefined): string {
  const s = String(status || "");
  if (s === "running" || s === "started") return "var(--color-accent)";
  if (s === "succeeded" || s === "completed" || s === "done") return "var(--color-ok)";
  if (s === "failed" || s === "error") return "var(--color-err)";
  if (s === "waiting" || s === "waiting_approval" || s === "queued") return "var(--color-warn)";
  return "var(--color-border-strong)";
}

function isRunning(n: ObsSwarmNode): boolean {
  const s = String(n.status || "");
  return s === "running" || s === "started";
}

/** 노드 키 — id가 있으면 엣지 source/target과 매칭, 없으면 인덱스 키(엣지로 못 찾음=고립 취급). */
function swarmKey(nodes: ObsSwarmNode[], i: number): string {
  const id = nodes[i]?.id;
  return id != null && id !== "" ? String(id) : `__i${i}`;
}

/** 엣지 longest-path로 레이어 계산 + 고립 노드는 kind 순서로 분산. */
function layoutSwarm(nodes: ObsSwarmNode[], edges: ObsSwarmEdge[]): SwarmLayout {
  const byId = new Map<string, { node: ObsSwarmNode; idx: number }>();
  nodes.forEach((n, i) => byId.set(swarmKey(nodes, i), { node: n, idx: i }));
  const edgeList = edges.filter((e) => byId.has(String(e.source)) && byId.has(String(e.target)));

  const layerOf = new Map<string, number>();
  const visiting = new Set<string>();
  const getLayer = (id: string): number => {
    const cached = layerOf.get(id);
    if (cached !== undefined) return cached;
    if (visiting.has(id)) return 0; // 사이클 방어
    visiting.add(id);
    let l = 0;
    for (const e of edgeList) {
      if (String(e.target) === id) {
        l = Math.max(l, getLayer(String(e.source)) + 1);
      }
    }
    visiting.delete(id);
    layerOf.set(id, l);
    return l;
  };

  const hasEdge = new Set<string>();
  for (const e of edgeList) {
    hasEdge.add(String(e.source));
    hasEdge.add(String(e.target));
  }
  let maxLayer = 0;
  nodes.forEach((_n, i) => {
    const id = swarmKey(nodes, i);
    if (hasEdge.has(id)) {
      maxLayer = Math.max(maxLayer, getLayer(id));
    }
  });
  // 고립 노드(엣지에 안 잡힌 것): kind 순위를 0..maxLayer 레이어에 매핑
  nodes.forEach((n, i) => {
    const id = swarmKey(nodes, i);
    if (layerOf.has(id) || hasEdge.has(id)) return;
    const kind = String(n.kind || "").toLowerCase();
    const rank = SWARM_KIND_ORDER.indexOf(kind);
    const frac = rank >= 0 && SWARM_KIND_ORDER.length > 1
      ? rank / (SWARM_KIND_ORDER.length - 1)
      : 0.5;
    layerOf.set(id, Math.round(frac * Math.max(1, maxLayer)));
  });

  // 레이어별 그룹 → 컬럼 내 세로 배치 (가장 큰 컬럼 높이에 맞춰 중앙 정렬)
  const cols = new Map<number, string[]>();
  nodes.forEach((_n, i) => {
    const id = swarmKey(nodes, i);
    const l = layerOf.get(id) ?? 0;
    const arr = cols.get(l) || [];
    arr.push(id);
    cols.set(l, arr);
  });
  for (const arr of cols.values()) {
    arr.sort((a, b) => {
      const na = byId.get(a)!.node, nb = byId.get(b)!.node;
      const ra = isRunning(na) ? 0 : 1, rb = isRunning(nb) ? 0 : 1;
      return ra - rb || String(na.label || na.id || "").localeCompare(String(nb.label || nb.id || ""));
    });
  }

  const sortedLayers = [...cols.keys()].sort((a, b) => a - b);
  const colCount = Math.max(1, sortedLayers.length);
  const tallest = Math.max(1, ...[...cols.values()].map((c) => c.length));
  const viewW = SWARM_PAD * 2 + colCount * SWARM_NODE_W + Math.max(0, colCount - 1) * SWARM_GAP_X;
  const viewH = SWARM_PAD * 2 + tallest * SWARM_NODE_H + Math.max(0, tallest - 1) * SWARM_GAP_Y;

  const placed: PlacedNode[] = [];
  sortedLayers.forEach((l, colIdx) => {
    const ids = cols.get(l)!;
    const colH = ids.length * SWARM_NODE_H + Math.max(0, ids.length - 1) * SWARM_GAP_Y;
    const yOff = SWARM_PAD + Math.max(0, (viewH - SWARM_PAD * 2 - colH) / 2);
    // x는 정렬된 컬럼 인덱스 기준 — 레이어 값이 0부터 연속이 아니어도 클리핑 없음
    const x = SWARM_PAD + colIdx * (SWARM_NODE_W + SWARM_GAP_X);
    ids.forEach((id, i) => {
      const ref = byId.get(id)!;
      placed.push({ node: ref.node, idx: ref.idx, x, y: yOff + i * (SWARM_NODE_H + SWARM_GAP_Y), layer: l });
    });
  });
  return { placed, viewW, viewH, byId };
}

function SwarmGraph({ nodes, edges }: { nodes: ObsSwarmNode[]; edges: ObsSwarmEdge[] }) {
  const { t } = useI18n();
  const { placed, viewW, viewH, byId } = useMemo(() => layoutSwarm(nodes, edges), [nodes, edges]);
  const posOf = useMemo(() => {
    const m = new Map<string, { x: number; y: number }>();
    placed.forEach((p) => m.set(swarmKey(nodes, p.idx), { x: p.x, y: p.y }));
    return m;
  }, [placed, nodes]);

  return (
    <div>
      <svg
        viewBox={`0 0 ${viewW} ${viewH}`}
        className="w-full rounded-lg border border-border bg-surface-hover/40"
        role="img"
        aria-label={t("office.swarm.graphAria", "에이전트 스웜 그래프")}
        preserveAspectRatio="xMidYMid meet"
      >
        <defs>
          <marker id="jm-swarm-arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
            <path d="M0,0 L8,4 L0,8 z" fill="var(--color-border-strong)" />
          </marker>
        </defs>
        {/* 엣지 — 실행 중 노드가 포함된 연결선만 펄스 */}
        {edges.map((e, i) => {
          const s = posOf.get(String(e.source));
          const tt = posOf.get(String(e.target));
          if (!s || !tt) return null;
          const src = byId.get(String(e.source))?.node;
          const tgt = byId.get(String(e.target))?.node;
          const forward = tt.x >= s.x;
          const x1 = forward ? s.x + SWARM_NODE_W : s.x;
          const y1 = s.y + SWARM_NODE_H / 2;
          const x2 = forward ? tt.x : tt.x + SWARM_NODE_W;
          const y2 = tt.y + SWARM_NODE_H / 2;
          const live = (src ? isRunning(src) : false) || (tgt ? isRunning(tgt) : false);
          return (
            <line
              key={i}
              x1={x1} y1={y1} x2={x2} y2={y2}
              stroke={live ? "var(--color-accent)" : "var(--color-border-strong)"}
              strokeWidth={live ? 1.6 : 1.2}
              strokeOpacity={live ? 0.9 : 0.7}
              markerEnd="url(#jm-swarm-arrow)"
              className={live ? "motion-safe:animate-pulse" : undefined}
            >
              <title>{`${e.source} → ${e.target}${e.kind ? ` · ${e.kind}` : ""}`}</title>
            </line>
          );
        })}
        {/* 노드 */}
        {placed.map((p) => {
          const n = p.node;
          const id = swarmKey(nodes, p.idx);
          const running = isRunning(n);
          const color = swarmStatusColor(n.status);
          const pct = n.progress_pct != null && isFinite(Number(n.progress_pct))
            ? Math.max(0, Math.min(100, Number(n.progress_pct))) : null;
          const sub = [n.kind, n.agent].filter(Boolean).join(" · ");
          const detail = [
            n.label || id, n.kind, n.agent, n.status,
            pct != null ? `${t("office.swarm.progress", "진행률")} ${pct.toFixed(0)}%` : null,
            n.tokens != null ? `${t("office.live.tokens", "토큰")} ${Number(n.tokens).toLocaleString()}` : null,
            n.latency_ms != null ? `latency ${Number(n.latency_ms).toFixed(0)}ms` : null,
            n.error_count ? `errors ${n.error_count}` : null,
          ].filter(Boolean).join("\n");
          const label = String(n.label || n.agent || id);
          const short = label.length > 22 ? label.slice(0, 21) + "…" : label;
          return (
            <g key={id}>
              <rect
                x={p.x} y={p.y} width={SWARM_NODE_W} height={SWARM_NODE_H} rx={8}
                fill="var(--color-surface)"
                stroke={color}
                strokeWidth={running ? 2 : 1.5}
                className={running ? "motion-safe:animate-pulse" : undefined}
              />
              <text x={p.x + 10} y={p.y + 19} fontSize={11} fontWeight={600} fill="var(--color-ink)">
                {short}
              </text>
              <text x={p.x + 10} y={p.y + 34} fontSize={9} fill="var(--color-muted)">
                {sub ? sub.slice(0, 30) : String(n.status || "-")}
              </text>
              {pct != null && (
                <rect x={p.x + 10} y={p.y + SWARM_NODE_H - 7} width={SWARM_NODE_W - 20} height={3} rx={1.5} fill="var(--color-surface-hover)" />
              )}
              {pct != null && (
                <rect x={p.x + 10} y={p.y + SWARM_NODE_H - 7} width={(SWARM_NODE_W - 20) * (pct / 100)} height={3} rx={1.5} fill="var(--color-accent)" />
              )}
              <title>{detail}</title>
            </g>
          );
        })}
      </svg>
      {/* 상태 범례 */}
      <div className="mt-2 flex flex-wrap gap-3 text-[10px] text-muted">
        {(["running", "succeeded", "failed", "waiting"] as const).map((s) => (
          <span key={s} className="inline-flex items-center gap-1">
            <span className="inline-block h-2 w-2 rounded-full" style={{ background: swarmStatusColor(s) }} />
            {s}
          </span>
        ))}
        <span className="ml-auto tabular-nums">{t("office.swarm.nodes", "{n}개 노드").replace("{n}", String(nodes.length))}</span>
      </div>
    </div>
  );
}

export function SwarmSection() {
  const { t } = useI18n();
  const [days, setDays] = useState("7");
  const q = useObsSwarm(Number(days));
  const role = useAppStore((s) => s.role);
  const nodes = useMemo(
    () => (Array.isArray(q.data?.nodes) ? q.data.nodes : []),
    [q.data],
  );
  const edges = useMemo(
    () => (Array.isArray(q.data?.edges) ? q.data.edges : []),
    [q.data],
  );

  return (
    <Card
      title={t("office.swarm.title", "스웜 그래프")}
      actions={<DaysSelect days={days} setDays={setDays} label={t("office.swarm.daysAria", "조회 기간")} />}
    >
      <QueryGate q={q} forbidden={t("office.swarm.forbidden", "권한 부족 — 스웜 그래프는 lead 역할에서 확인할 수 있습니다")} blocked={!roleAllows("lead", role)}>
        {() => (
          !nodes.length ? <Empty>{t("office.swarm.empty", "표시할 스웜 데이터가 없습니다")}</Empty>
            : <SwarmGraph nodes={nodes} edges={edges} />
        )}
      </QueryGate>
    </Card>
  );
}

// ---------- Tool 호출 통계 (spec 012 US2) ----------

function fmtMs(v: number | null | undefined): string {
  const n = Number(v);
  if (v == null || !isFinite(n)) return "-";
  return n >= 1000 ? `${(n / 1000).toFixed(2)}s` : `${n.toFixed(1)}ms`;
}

/** error_rate는 계약상 비율(0~1). 백엔드가 %로 보내는 경우(>1)도 그대로 표시되도록
 *  1 이하면 ×100 해서 %로 환산한다. */
function errorRatePct(v: number | null | undefined): number | null {
  const n = Number(v);
  if (v == null || !isFinite(n)) return null;
  return n <= 1 ? n * 100 : n;
}

function fmtErrorRate(v: number | null | undefined): string {
  const pct = errorRatePct(v);
  return pct == null ? "-" : `${pct.toFixed(2)}%`;
}

export function ToolStatsSection() {
  const { t } = useI18n();
  const [days, setDays] = useState("7");
  const q = useObsToolStats(Number(days));
  const role = useAppStore((s) => s.role);

  const byKind = useMemo(
    () => (Array.isArray(q.data?.by_kind) ? q.data.by_kind : []),
    [q.data],
  );
  const byName = useMemo(
    () => (Array.isArray(q.data?.by_name) ? q.data.by_name : []),
    [q.data],
  );
  const kindMax = Math.max(1e-9, ...byKind.map((x) => Number(x.count) || 0));

  return (
    <Card
      title={t("office.toolStats.title", "Tool 호출 통계")}
      actions={<DaysSelect days={days} setDays={setDays} label={t("office.toolStats.daysAria", "조회 기간")} />}
    >
      <QueryGate q={q} forbidden={t("office.toolStats.forbidden", "권한 부족 — Tool 통계는 lead 역할에서 확인할 수 있습니다")} blocked={!roleAllows("lead", role)}>
        {(d) => (
          <>
            <div className="mb-3 grid grid-cols-2 gap-2">
              <div className="rounded-lg border border-border bg-surface-hover px-2.5 py-1.5">
                <div className="text-[10px] text-muted">{t("office.toolStats.totalCalls", "총 호출")}</div>
                <div className="text-base font-semibold tabular-nums text-ink">
                  {Number(d.total_calls ?? 0).toLocaleString()}
                </div>
              </div>
              <div className="rounded-lg border border-border bg-surface-hover px-2.5 py-1.5">
                <div className="text-[10px] text-muted">{t("office.toolStats.errorRate", "오류율")}</div>
                <div className={`text-base font-semibold tabular-nums ${
                  (errorRatePct(d.error_rate) ?? 0) > 5 ? "text-err" : "text-ink"
                }`}>
                  {fmtErrorRate(d.error_rate)}
                </div>
              </div>
            </div>

            {byKind.length > 0 && (
              <>
                <div className="mb-1 text-[11px] font-medium text-muted">
                  {t("office.toolStats.byKind", "유형별 비중")}
                </div>
                <div className="mb-3 space-y-1.5">
                  {byKind.map((k, i) => {
                    const count = Number(k.count) || 0;
                    const width = Math.max(1.5, (count / kindMax) * 100);
                    const label = k.kind || t("office.toolStats.kindEmpty", "(미분류)");
                    return (
                      <div key={k.kind || i} className="flex items-center gap-2">
                        <span className="w-20 flex-none truncate font-mono text-[10px] text-muted" title={label}>
                          {label}
                        </span>
                        <div className="h-2 min-w-0 flex-1 overflow-hidden rounded-full bg-surface-hover">
                          <div className="h-full rounded-full bg-accent/80" style={{ width: `${width}%` }} />
                        </div>
                        <span className="w-16 flex-none text-right text-[10px] tabular-nums text-muted">
                          {count.toLocaleString()}
                        </span>
                      </div>
                    );
                  })}
                </div>
              </>
            )}

            <div className="mb-1 text-[11px] font-medium text-muted">
              {t("office.toolStats.byName", "이름별 통계")}
            </div>
            {!byName.length ? (
              <Empty>{t("office.toolStats.empty", "기간 내 Tool 호출 데이터가 없습니다")}</Empty>
            ) : (
              <div className="max-h-72 overflow-y-auto">
                <table className="w-full text-xs">
                  <thead className="sticky top-0 bg-surface">
                    <tr className="text-left text-muted">
                      <th className="pb-1.5 pr-2">{t("office.toolStats.col.name", "이름")}</th>
                      <th className="pb-1.5 pr-2">{t("office.toolStats.col.kind", "유형")}</th>
                      <th className="pb-1.5 pr-2 text-right">{t("office.toolStats.col.count", "호출")}</th>
                      <th className="pb-1.5 pr-2 text-right">{t("office.toolStats.col.errors", "오류")}</th>
                      <th className="pb-1.5 text-right">{t("office.toolStats.col.avgMs", "평균 소요")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {byName.map((n, i) => {
                      const errs = Number(n.error_count) || 0;
                      return (
                        <tr key={n.name || i} className="border-t border-border">
                          <td className="max-w-48 truncate py-1.5 pr-2 font-mono text-ink" title={n.name || "-"}>
                            {n.name || "-"}
                          </td>
                          <td className="py-1.5 pr-2">
                            {n.kind
                              ? <Badge tone="accent">{n.kind}</Badge>
                              : <span className="text-muted">-</span>}
                          </td>
                          <td className="py-1.5 pr-2 text-right tabular-nums">{(Number(n.count) || 0).toLocaleString()}</td>
                          <td className={`py-1.5 pr-2 text-right tabular-nums ${errs > 0 ? "text-err" : "text-muted"}`}>
                            {errs.toLocaleString()}
                          </td>
                          <td className="py-1.5 text-right tabular-nums text-muted">{fmtMs(n.avg_duration_ms)}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </>
        )}
      </QueryGate>
    </Card>
  );
}
