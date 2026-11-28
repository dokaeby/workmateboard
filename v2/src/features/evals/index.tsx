/** evals — /evals 평가 케이스(test-run harness) 목록 + 등록/편집 + 실행/이력.
 *  페이지 셸만 담당 — 타입/헬퍼는 types.ts, 체크 빌더는 checks.tsx,
 *  폼은 form.tsx, 실행 이력은 runs.tsx, 상세는 detail.tsx. */
import { useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { apiGet, apiPatch, apiDelete, ApiError } from "../../shared/api/client";
import { useI18n } from "../../shared/i18n";
import { PageHead, Card, Empty, Spinner, ErrorBox, Badge, Button } from "../../shared/ui";
import type { WorkflowInfo, CustomWorkflow } from "../../shared/api/types";
import {
  ago, statusOf, inputCls,
  type EvalItem, type EvalsResponse,
} from "./types";
import { EvalForm } from "./form";
import { ScoreBadge } from "./runs";
import { EvalDetail } from "./detail";
import { OnlineEvals } from "./online";
import "./i18n";

/** 워크플로 키 후보 — 내장 /workflows + 커스텀 /workflows/custom 병합 (triggers와 동일 패턴). */
function useWorkflowKeys() {
  return useQuery({
    queryKey: ["eval-workflow-keys"],
    staleTime: 300000,
    queryFn: async (): Promise<string[]> => {
      const keys = new Set<string>();
      try {
        const wfs = await apiGet<WorkflowInfo[]>("/workflows");
        for (const w of wfs || []) if (w.key) keys.add(w.key);
      } catch { /* 실패 시 커스텀만 */ }
      try {
        const d = await apiGet<{ items?: CustomWorkflow[] }>("/workflows/custom");
        for (const c of d.items || []) {
          const n = c.name || "";
          if (n) keys.add(n);
        }
      } catch { /* 무시 */ }
      if (!keys.size) keys.add("dev_review");
      return [...keys].sort();
    },
  });
}

export default function EvalsPage() {
  const { t } = useI18n();
  const qc = useQueryClient();
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState("active"); // active | archived | all
  const [expanded, setExpanded] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);

  const workflowKeys = useWorkflowKeys();

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["evals", statusFilter],
    queryFn: () =>
      apiGet<EvalsResponse>(
        statusFilter === "all" ? "/evals" : `/evals?status=${encodeURIComponent(statusFilter)}`,
      ),
    refetchInterval: 30000,
  });

  const items = useMemo(() => {
    const q = search.trim().toLowerCase();
    const list = data?.items || [];
    if (!q) return list;
    return list.filter(
      (e) =>
        String(e.name || "").toLowerCase().includes(q) ||
        String(e.workflow_key || "").toLowerCase().includes(q),
    );
  }, [data, search]);

  const archive = async (e: EvalItem) => {
    const archived = statusOf(e) === "archived";
    if (!archived && !window.confirm(t("evals.confirmArchive", "'{name}' 평가를 보관 처리할까요? 목록에서 숨겨집니다.").replace("{name}", e.name || e.id))) {
      return;
    }
    setBusyId(e.id);
    try {
      if (archived) {
        // 보관 해제 — PATCH status=active (DELETE는 보관 전용)
        await apiPatch(`/evals/${encodeURIComponent(e.id)}`, { status: "active" });
      } else {
        await apiDelete(`/evals/${encodeURIComponent(e.id)}`);
      }
      await qc.invalidateQueries({ queryKey: ["evals"] });
    } catch (err) {
      window.alert(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusyId(null);
    }
  };

  return (
    <div className="mx-auto max-w-4xl p-4">
      <PageHead title={t("evals.title", "평가")} desc={t("evals.desc", "워크플로 평가 케이스 — 실행 후 체크 결과와 점수로 품질을 측정합니다")}>
        <Button className="text-xs" onClick={() => setShowForm((v) => !v)}>
          {showForm ? t("evals.close", "닫기") : "+ " + t("evals.add", "평가 추가")}
        </Button>
      </PageHead>

      {showForm && (
        <Card className="mb-3">
          <EvalForm
            workflowKeys={workflowKeys.data || []}
            onDone={() => setShowForm(false)}
            onCancel={() => setShowForm(false)}
          />
        </Card>
      )}

      <div className="mb-3 flex flex-wrap items-center gap-2">
        <input
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder={t("evals.searchPh", "이름·워크플로 검색")}
          className={`${inputCls} max-w-xs flex-1`}
        />
        <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)} className={`${inputCls} w-auto`}>
          <option value="active">{t("evals.statusActive", "사용 중")}</option>
          <option value="archived">{t("evals.statusArchived", "보관됨")}</option>
          <option value="all">{t("evals.statusAll", "전체 상태")}</option>
        </select>
      </div>

      {isLoading && <Spinner />}
      {error && <ErrorBox message={error instanceof Error ? error.message : String(error)} onRetry={() => refetch()} />}
      {data && !items.length && (
        <Empty>
          <div>{search || statusFilter !== "active" ? t("evals.noMatch", "조건에 맞는 평가가 없습니다") : t("evals.empty", "등록된 평가가 없습니다")}</div>
          {!search && statusFilter === "active" && <div className="mt-1">{t("evals.emptyHint", "첫 평가 케이스를 추가해 워크플로 품질을 측정하세요")}</div>}
        </Empty>
      )}

      <div className="space-y-2">
        {items.map((e) => {
          const archived = statusOf(e) === "archived";
          const open = expanded === e.id;
          return (
            <Card key={e.id} className="!p-3">
              <button
                onClick={() => setExpanded(open ? null : e.id)}
                className="flex w-full flex-wrap items-center gap-x-3 gap-y-1 text-left"
              >
                <span className="min-w-0 flex-1 truncate text-sm font-medium text-ink">{e.name || e.id}</span>
                <Badge tone="accent">{e.workflow_key || "-"}</Badge>
                <Badge tone="muted">
                  {(e.checks?.length ?? 0) === 1
                    ? t("evals.checksOne", "체크 1개")
                    : t("evals.checks", "{n}개 체크").replace("{n}", String(e.checks?.length ?? 0))}
                </Badge>
                <span className="flex items-center gap-1 text-xs text-muted">
                  {t("evals.lastScore", "최근 점수")} <ScoreBadge score={e.last_score} />
                </span>
                <span className="text-xs text-muted">
                  {t("evals.lastRun", "최근 실행")} {e.last_run_at ? ago(e.last_run_at) : t("evals.neverRun", "실행 기록 없음")}
                </span>
                {archived && <Badge tone="muted">{t("evals.statusArchived", "보관됨")}</Badge>}
              </button>
              <div className="mt-2 flex flex-wrap items-center gap-1.5">
                <Button variant="ghost" className="px-2 py-1 text-xs" onClick={() => setExpanded(open ? null : e.id)}>
                  {open ? t("evals.close", "닫기") : t("evals.detail", "상세")}
                </Button>
                <Button
                  variant="ghost"
                  className="px-2 py-1 text-xs text-err"
                  disabled={busyId === e.id}
                  onClick={() => void archive(e)}
                >
                  {archived ? t("evals.unarchive", "복원") : t("evals.archive", "보관")}
                </Button>
              </div>
              {open && (
                <div className="mt-3 border-t border-border pt-3">
                  <EvalDetail item={e} workflowKeys={workflowKeys.data || []} />
                </div>
              )}
            </Card>
          );
        })}
      </div>

      {/* spec 008 US2 — 온라인 평가 스케줄 */}
      <OnlineEvals workflowKeys={workflowKeys.data || []} />
    </div>
  );
}
