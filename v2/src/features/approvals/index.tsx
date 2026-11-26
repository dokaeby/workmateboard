/** 승인 대기 — run waiting_approval 개별/전체 승인 + 2차 범용 승인 요청
 * (kind="approval_request") + 세션/트리거 항목 네비게이션. */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { apiGet, apiPost, ApiError } from "../../shared/api/client";
import type {
  ApprovalsResponse,
  ApprovalItem,
  ApprovalRequestItem,
  ApprovalQueueItem,
  ViewKey,
} from "../../shared/api/types";
import { useAppStore } from "../../shared/store/appStore";
import { useI18n, tx } from "../../shared/i18n";
import { PageHead, Card, Empty, Spinner, ErrorBox, Button, Badge } from "../../shared/ui";
import { fmtAge, fmtTs } from "../../shared/lib/format";
import { useState } from "react";
import "./i18n";


type TFn = (k: string, f?: string) => string;

function kindLabel(kind: string, t: TFn): string {
  const k = kind.toLowerCase();
  if (k === "approval_request") return t("appr.kindRequest", "승인 요청");
  if (k.includes("run")) return t("appr.kindRun", "실행");
  if (k.includes("session")) return t("appr.kindSession", "세션");
  if (k.includes("trigger")) return t("appr.kindTrigger", "트리거");
  return kind || t("appr.kindOther", "승인");
}

const ACTION_LABEL: Record<string, string> = {
  run_resume: "appr.actRunResume",
  budget_raise: "appr.actBudgetRaise",
  agent_hire: "appr.actAgentHire",
  agent_terminate: "appr.actAgentTerminate",
  external_send: "appr.actExternalSend",
  deploy: "appr.actDeploy",
  destructive_tool: "appr.actDestructiveTool",
  recovery: "appr.actRecovery",
};
const ACTION_FALLBACK: Record<string, string> = {
  run_resume: "실행 재개",
  budget_raise: "예산 상향",
  agent_hire: "에이전트 채용",
  agent_terminate: "에이전트 해지",
  external_send: "외부 발송",
  deploy: "배포",
  destructive_tool: "파괴적 작업",
  recovery: "복구",
};

function actionLabel(actionType: string | undefined, t: TFn): string {
  const k = String(actionType || "");
  return t(ACTION_LABEL[k] || "", ACTION_FALLBACK[k] || k || t("appr.kindOther", "승인"));
}

const RISK_TONE: Record<string, "muted" | "ok" | "warn" | "err" | "accent"> = {
  low: "muted",
  medium: "accent",
  high: "warn",
  critical: "err",
};
const RISK_LABEL: Record<string, string> = {
  low: "appr.riskLow",
  medium: "appr.riskMedium",
  high: "appr.riskHigh",
  critical: "appr.riskCritical",
};
const RISK_FALLBACK: Record<string, string> = {
  low: "낮음",
  medium: "보통",
  high: "높음",
  critical: "치명",
};

const REQ_STATUS_TONE: Record<string, "muted" | "ok" | "warn" | "err" | "accent"> = {
  pending: "warn",
  approved: "ok",
  rejected: "err",
  expired: "muted",
  cancelled: "muted",
};
const REQ_STATUS_LABEL: Record<string, string> = {
  pending: "appr.stPending",
  approved: "appr.stApproved",
  rejected: "appr.stRejected",
  expired: "appr.stExpired",
  cancelled: "appr.stCancelled",
};
const REQ_STATUS_FALLBACK: Record<string, string> = {
  pending: "대기",
  approved: "승인됨",
  rejected: "거절됨",
  expired: "만료",
  cancelled: "취소됨",
};

function reqStatusLabel(status: string | undefined, t: TFn): string {
  const s = String(status || "pending");
  return t(REQ_STATUS_LABEL[s] || "", REQ_STATUS_FALLBACK[s] || s);
}

function isApprovalRequest(a: ApprovalQueueItem): a is ApprovalRequestItem {
  return a.kind === "approval_request";
}

/** pending이고 만료 시각이 지나지 않은 요청만 결정 버튼을 연다. */
function reqDecidable(a: ApprovalRequestItem): boolean {
  if (a.status !== "pending") return false;
  if (a.expires_at) {
    const ts = Date.parse(a.expires_at);
    if (!isNaN(ts) && ts <= Date.now()) return false;
  }
  return true;
}

/** 정렬용 경과 초 — approval_request는 age_seconds가 없어 created_at으로 환산. */
function itemAgeSec(a: ApprovalQueueItem): number {
  if (typeof (a as ApprovalItem).age_seconds === "number") {
    return (a as ApprovalItem).age_seconds || 0;
  }
  const iso = (a as ApprovalRequestItem).created_at;
  if (iso) {
    const ts = Date.parse(iso);
    if (!isNaN(ts)) return Math.max(0, (Date.now() - ts) / 1000);
  }
  return 0;
}

function goToItem(item: ApprovalQueueItem, setView: (v: ViewKey) => void) {
  if (isApprovalRequest(item)) {
    const at = String(item.action_type || "");
    if (at === "run_resume") setView("runs");
    else if (at === "budget_raise") setView("cost");
    else if (at === "agent_hire" || at === "agent_terminate") setView("agents");
    else if (at === "recovery") setView("inbox");
    else setView("office");
    return;
  }
  const k = String(item.kind || "").toLowerCase();
  if (k.includes("run")) setView("runs");
  else if (k.includes("session")) setView("sessionlog");
  else if (k.includes("trigger")) setView("triggers");
  else setView("office");
}

/** 2차 범용 승인 요청 1행 — action/risk/status 배지 + 요약 + 만료 힌트. */
function ApprovalRequestRow({
  a,
  busy,
  onDecide,
  onGo,
}: {
  a: ApprovalRequestItem;
  busy: boolean;
  onDecide: (decision: "approve" | "reject") => void;
  onGo: () => void;
}) {
  const { t } = useI18n();
  const decidable = reqDecidable(a);
  const risk = String(a.risk_level || "low").toLowerCase();
  const restricted = risk === "high" || risk === "critical";
  return (
    <div className="flex flex-wrap items-center gap-3 border-b border-border py-3 last:border-0">
      <Badge tone="accent">{kindLabel(a.kind, t)}</Badge>
      <Badge tone={RISK_TONE[risk] || "muted"}>
        {t("appr.riskLabel", "위험") + " " + t(RISK_LABEL[risk] || "", RISK_FALLBACK[risk] || risk)}
      </Badge>
      <Badge tone={REQ_STATUS_TONE[String(a.status || "pending")] || "muted"}>
        {reqStatusLabel(a.status, t)}
      </Badge>
      <div className="min-w-0 flex-1">
        <div className="truncate text-sm font-medium text-ink">
          {actionLabel(a.action_type, t)}
          {a.summary ? <span className="ml-1 font-normal text-muted">— {a.summary}</span> : null}
        </div>
        <div className="text-xs text-muted">
          {a.resource_type || a.resource_id ? (
            <span className="mr-1 rounded bg-surface-hover px-1">
              {[a.resource_type, a.resource_id].filter(Boolean).join(": ")}
            </span>
          ) : null}
          {t("appr.created", "요청")} {fmtTs(a.created_at)} · {fmtAge(itemAgeSec(a))}
          {a.expires_at ? <> · {t("appr.expiresAt", "만료")} {fmtTs(a.expires_at)}</> : null}
          {restricted ? <> · <span className="text-warn">{t("appr.riskRestricted", "높은 위험 — 리드 이상 권한 필요")}</span></> : null}
        </div>
        {a.safe_payload && Object.keys(a.safe_payload).length > 0 ? (
          <div className="mt-0.5 truncate font-mono text-[11px] text-muted">
            {t("appr.payload", "페이로드")}:{" "}
            {JSON.stringify(a.safe_payload).slice(0, 200)}
          </div>
        ) : null}
      </div>
      <div className="flex gap-1.5">
        {decidable && (
          <>
            <Button className="px-2.5 py-1 text-xs" disabled={busy} onClick={() => onDecide("approve")}>
              {t("appr.approve", "승인")}
            </Button>
            <Button variant="secondary" className="px-2.5 py-1 text-xs" disabled={busy} onClick={() => onDecide("reject")}>
              {t("appr.reject", "거절")}
            </Button>
          </>
        )}
        <Button variant="ghost" className="px-2.5 py-1 text-xs" onClick={onGo}>
          {decidable ? t("appr.detail", "상세") : t("appr.go", "처리하러 가기")}
        </Button>
      </div>
    </div>
  );
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
    .sort((a, b) => itemAgeSec(a) - itemAgeSec(b));

  const decide = async (item: ApprovalQueueItem, decision: "approve" | "reject") => {
    const key = `${item.kind}:${item.id}`;
    setBusyId(key);
    try {
      if (isApprovalRequest(item)) {
        // 2차 범용 승인 — 서버가 pending/high·critical 권한을 검증한다.
        await apiPost(`/management/approvals/${encodeURIComponent(item.id)}/decision`, {
          decision,
          comment: "",
        });
      } else {
        await apiPost(`/runs/${encodeURIComponent(item.id)}/approve`, { decision, comment: "" });
      }
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
          if (isApprovalRequest(a)) {
            const key = `approval_request:${a.id}`;
            return (
              <ApprovalRequestRow
                key={key}
                a={a}
                busy={busyId === key}
                onDecide={(decision) => decide(a, decision)}
                onGo={() => goToItem(a, setView)}
              />
            );
          }
          const key = `${a.kind}:${a.id}`;
          const canDecide = a.kind === "run" && a.status === "waiting_approval";
          return (
            <div key={key} className="flex flex-wrap items-center gap-3 border-b border-border py-3 last:border-0">
              <Badge tone={a.kind?.includes("run") ? "accent" : a.kind?.includes("session") ? "warn" : "muted"}>
                {kindLabel(a.kind || "", t)}
              </Badge>
              <div className="min-w-0 flex-1">
                <div className="truncate text-sm font-medium text-ink">
                  {tx(t, a.title_key, a.title_params, a.title) || a.id}
                </div>
                <div className="text-xs text-muted">
                  {a.project ? <span className="mr-1 rounded bg-surface-hover px-1">{a.project}</span> : null}
                  {t("appr.waiting", "대기")} {fmtAge(a.age_seconds)} · {fmtTs(a.waiting_since)}
                </div>
              </div>
              <div className="flex gap-1.5">
                {canDecide && (
                  <>
                    <Button className="px-2.5 py-1 text-xs" disabled={busyId === key} onClick={() => decide(a, "approve")}>
                      {t("appr.approve", "승인")}
                    </Button>
                    <Button variant="secondary" className="px-2.5 py-1 text-xs" disabled={busyId === key} onClick={() => decide(a, "reject")}>
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
