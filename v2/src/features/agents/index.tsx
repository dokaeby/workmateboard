/** agents — /agents 에이전트 조직도(reports_to 트리) + 등록/편집/보관.
 *  API 계약: GET/POST /agents, PATCH/DELETE /agents/{id} (DELETE는 보관 처리).
 *  페이지 셸만 담당 — 상수/라벨은 shared.ts, 폼은 form.tsx,
 *  조직도는 org-tree.tsx, 상세 드로어는 drawer.tsx. */
import { useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { apiGet } from "../../shared/api/client";
import { useI18n } from "../../shared/i18n";
import { useAppStore, roleAllows } from "../../shared/store/appStore";
import { PageHead, Card, Empty, Spinner, ErrorBox, Button } from "../../shared/ui";
import { buildForest, type AgentsResponse } from "./tree";
import { DISPLAY_STATUSES, statusLabel, statusOf } from "./shared";
import { AgentForm } from "./form";
import { OrgNode } from "./org-tree";
import { AgentDrawer } from "./drawer";
import "./i18n";
import "./agents.css";

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
          {DISPLAY_STATUSES.map((s) => (
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
