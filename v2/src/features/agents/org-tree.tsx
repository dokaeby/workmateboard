/** agents — 조직도 노드 카드 + 재귀 트리 렌더. */
import { useI18n } from "../../shared/i18n";
import { Badge } from "../../shared/ui";
import { usd } from "../../shared/lib/format";
import type { Agent, AgentNode } from "./tree";
import { STATUS_TONE, statusLabel, statusOf, lastRunAgo } from "./shared";

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

export function OrgNode({
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
