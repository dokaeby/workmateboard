/** 승인 대기 — run waiting_approval 개별/전체 승인 + 세션/트리거 항목 네비게이션. */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { apiGet, apiPost, ApiError } from "../../shared/api/client";
import type { ApprovalsResponse, ApprovalItem } from "../../shared/api/types";
import { useAppStore } from "../../shared/store/appStore";
import { useI18n, registerI18n } from "../../shared/i18n";
import { PageHead, Card, Empty, Spinner, ErrorBox, Button, Badge } from "../../shared/ui";
import { fmtAge, fmtTs } from "../../shared/lib/format";
import { useState } from "react";
import { STR } from "./i18n";

registerI18n(STR);

function kindLabel(kind: string, t: (k: string, f?: string) => string): string {
  const k = kind.toLowerCase();
  if (k.includes("run")) return t("appr.kindRun", "실행");
  if (k.includes("session")) return t("appr.kindSession", "세션");
  if (k.includes("trigger")) return t("appr.kindTrigger", "트리거");
  return kind || t("appr.kindOther", "승인");
}

function goToItem(item: ApprovalItem, setView: (v: "runs" | "sessionlog" | "triggers" | "office") => void) {
  const k = String(item.kind || "").toLowerCase();
  if (k.includes("run")) setView("runs");
  else if (k.includes("session")) setView("sessionlog");
  else if (k.includes("trigger")) setView("triggers");
  else setView("office");
}

export default function ApprovalsPage() {
  const { t } = useI18n();
  const setView = useAppStore((s) => s.setView);
  const qc = useQueryClient();
  const [busyId, setBusyId] = useState<string | null>(null);
  const [approveAllState, setApproveAllState] = useState<string | null>(null);

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["approvals"],
    queryFn: () => apiGet<ApprovalsResponse>("/approvals"),
    refetchInterval: 30000,
  });

  const items = (data?.items || [])
    .slice()
    .sort((a, b) => (a.age_seconds || 0) - (b.age_seconds || 0));

  const decide = async (id: string, decision: "approve" | "reject") => {
    setBusyId(id);
    try {
      await apiPost(`/runs/${encodeURIComponent(id)}/approve`, { decision, comment: "" });
      await qc.invalidateQueries({ queryKey: ["approvals"] });
    } catch (e) {
      alert(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusyId(null);
    }
  };

  const approveAll = async () => {
    const ids = items
      .filter((a) => a.kind === "run" && a.status === "waiting_approval")
      .map((a) => a.id);
    if (!ids.length) { alert(t("appr.noneApprovable", "승인 가능한 실행이 없습니다")); return; }
    if (!confirm(t("appr.confirmAll", "{n}개 실행을 모두 승인할까요?").replace("{n}", String(ids.length)))) return;
    setApproveAllState("0/" + ids.length);
    let ok = 0, fail = 0;
    for (let i = 0; i < ids.length; i++) {
      setApproveAllState(`${i + 1}/${ids.length}`);
      try {
        await apiPost(`/runs/${encodeURIComponent(ids[i])}/approve`, { decision: "approve", comment: "approve-all" });
        ok++;
      } catch { fail++; }
    }
    alert(t("appr.doneAll", "승인 완료 {ok}건").replace("{ok}", String(ok)) + (fail ? `, ${t("appr.failed", "실패")} ${fail}` : ""));
    setApproveAllState(null);
    qc.invalidateQueries({ queryKey: ["approvals"] });
  };

  return (
    <div className="mx-auto max-w-3xl p-4">
      <PageHead
        title={t("appr.title", "승인 대기")}
        desc={t("appr.desc", "실행·세션·트리거 승인을 오래된 순으로 — 30초마다 자동 갱신")}
      >
        <Button onClick={approveAll} disabled={approveAllState !== null} className="text-xs">
          {approveAllState ? t("appr.approving", "승인 중…") + " " + approveAllState : t("appr.approveAll", "전체 승인")}
        </Button>
        <Button variant="secondary" onClick={() => refetch()} className="text-xs">
          {t("common.refresh", "새로고침")}
        </Button>
      </PageHead>
      <Card>
        {isLoading && <Spinner />}
        {error && <ErrorBox message={error instanceof Error ? error.message : String(error)} onRetry={() => refetch()} />}
        {data && !items.length && <Empty>{t("appr.empty", "대기 중인 승인이 없습니다")}</Empty>}
        {items.map((a) => {
          const canDecide = a.kind === "run" && a.status === "waiting_approval";
          return (
            <div key={`${a.kind}:${a.id}`} className="flex flex-wrap items-center gap-3 border-b border-border py-3 last:border-0">
              <Badge tone={a.kind?.includes("run") ? "accent" : a.kind?.includes("session") ? "warn" : "muted"}>
                {kindLabel(a.kind || "", t)}
              </Badge>
              <div className="min-w-0 flex-1">
                <div className="truncate text-sm font-medium text-ink">{a.title || a.id}</div>
                <div className="text-xs text-muted">
                  {a.project ? <span className="mr-1 rounded bg-surface-hover px-1">{a.project}</span> : null}
                  {t("appr.waiting", "대기")} {fmtAge(a.age_seconds)} · {fmtTs(a.waiting_since)}
                </div>
              </div>
              <div className="flex gap-1.5">
                {canDecide && (
                  <>
                    <Button className="px-2.5 py-1 text-xs" disabled={busyId === a.id} onClick={() => decide(a.id, "approve")}>
                      {t("appr.approve", "승인")}
                    </Button>
                    <Button variant="secondary" className="px-2.5 py-1 text-xs" disabled={busyId === a.id} onClick={() => decide(a.id, "reject")}>
                      {t("appr.reject", "거절")}
                    </Button>
                  </>
                )}
                <Button variant="ghost" className="px-2.5 py-1 text-xs" onClick={() => goToItem(a, setView)}>
                  {canDecide ? t("appr.detail", "상세") : t("appr.go", "처리하러 가기")}
                </Button>
              </div>
            </div>
          );
        })}
      </Card>
    </div>
  );
}
