/** 리스크 — GET /risks 라이브 탐지 결과를 심각도순 카드로 표시 (lead 이상).
 * 레거시 index.html의 loadRisks/jmGoRiskRef를 React Query로 이행. */
import { useQuery } from "@tanstack/react-query";
import { apiGet } from "../../shared/api/client";
import { useAppStore } from "../../shared/store/appStore";
import { useI18n, registerI18n, tx, type TFn } from "../../shared/i18n";
import { PageHead, Card, Empty, Spinner, ErrorBox, Button, Badge } from "../../shared/ui";
import { fmtAge, fmtTs } from "../../shared/lib/format";

registerI18n({
  ko: {
    "risks.title": "리스크",
    "risks.desc": "반복 실패·승인 지연·비용 급증 등 라이브 탐지 — 1분마다 갱신",
    "risks.empty": "감지된 리스크 없음",
    "risks.viewRef": "보기",
    "sev.high": "높음",
    "sev.medium": "중간",
    "sev.low": "낮음",
  },
  en: {
    "risks.title": "Risks",
    "risks.desc": "Live detection: repeated failures, stale approvals, cost spikes — refreshes every minute",
    "risks.empty": "No risks detected",
    "risks.viewRef": "View",
    "sev.high": "High",
    "sev.medium": "Medium",
    "sev.low": "Low",
    // 백엔드 detect_risks의 i18n 메타 — ko는 원문 fallback 사용
    "risk.stage_failure.title": "{wf} · {stage} stage repeatedly failing",
    "risk.stage_failure.detail": "Failed in {n} runs over the last 7 days.",
    "risk.stale_approval.title": "Approval pending too long: {wf}",
    "risk.stale_approval.detail": "{task} — waiting for approval for {dur}.",
    "risk.idle_session.title": "Session {health}: {tool} {key}",
    "risk.idle_session.detail": "Status {status} — last event {dur} ago.",
    "risk.cost_spike.title": "LLM cost spike today",
    "risk.cost_spike.detail": "Today's est. cost ${today} — over 3× the 7-day daily avg of ${avg}.",
    "risk.trigger_failure.title": "Trigger error: {id}",
    "risk.trigger_failure.detail": "Error on last trigger — {err}",
    "risk.budget.title": "Monthly budget exceeded",
    "risk.budget.detail": "This month's est. cost ${mtd} > budget ${budget}.",
  },
});

interface RiskI18n {
  title_key?: string;
  title_params?: Record<string, unknown>;
  detail_key?: string;
  detail_params?: Record<string, unknown>;
}

interface RiskItem {
  type?: string;
  severity?: string;
  title?: string;
  detail?: string;
  ref_kind?: string;
  ref_id?: string;
  at?: string;
  age_seconds?: number | null;
  i18n?: RiskI18n;
}
interface RisksResponse { items?: RiskItem[] }

// 레거시 RISK_SEV_RANK 대응 (critical도 최상위)
const SEV_RANK: Record<string, number> = { high: 0, critical: 0, med: 1, medium: 1, low: 2 };

function sevTone(sev: string): "err" | "warn" | "muted" {
  const s = sev.toLowerCase();
  if (s === "high" || s === "critical") return "err";
  if (s === "med" || s === "medium") return "warn";
  return "muted";
}

function sevLabel(sev: string, t: (k: string, f?: string) => string): string {
  const s = sev.toLowerCase();
  if (s === "high" || s === "critical") return t("sev.high", "높음");
  if (s === "med" || s === "medium") return t("sev.medium", "중간");
  return t("sev.low", "낮음");
}

function fmtDurS(sec: number, en: boolean): string {
  if (!Number.isFinite(sec)) return "-";
  const s = Math.max(0, Math.round(sec));
  if (en) {
    if (s < 60) return `${s}s`;
    const m = Math.floor(s / 60);
    if (m < 60) return `${m}m`;
    const h = Math.floor(m / 60);
    if (h < 24) return m % 60 ? `${h}h ${m % 60}m` : `${h}h`;
    const d = Math.floor(h / 24);
    return h % 24 ? `${d}d ${h % 24}h` : `${d}d`;
  }
  if (s < 60) return `${s}초`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}분`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}시간`;
  return `${Math.floor(h / 24)}일`;
}

/** 백엔드 i18n 메타가 있으면 언어별 템플릿으로 렌더, 없으면 원문 그대로. */
function riskText(r: RiskItem, field: "title" | "detail", t: TFn, en: boolean): string {
  const raw = String(r[field] ?? "");
  const meta = r.i18n;
  if (!meta) return raw;
  const params = { ...(meta[`${field}_params`] || {}) };
  if (params.dur_s != null) {
    params.dur = fmtDurS(Number(params.dur_s), en);
    delete params.dur_s;
  }
  if (params.health != null) params.health = String(params.health).replaceAll("_", " ");
  return tx(t, meta[`${field}_key`], params, raw);
}

// 레거시 jmGoRiskRef — ref_kind별 대상 뷰로 이동
function goToRef(kind: string | undefined, setView: (v: "runs" | "sessionlog" | "triggers" | "inbox") => void) {
  const k = String(kind || "").toLowerCase();
  if (k.includes("run")) setView("runs");
  else if (k.includes("session")) setView("sessionlog");
  else if (k.includes("trigger")) setView("triggers");
  else setView("inbox");
}

export default function RisksPage() {
  const { t, lang } = useI18n();
  const setView = useAppStore((s) => s.setView);

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["risks"],
    queryFn: () => apiGet<RisksResponse>("/risks"),
    refetchInterval: 60000,
    retry: 1, // lead 권한 없으면 403 — 반복 재시도하지 않음
  });

  const items = (data?.items || [])
    .slice()
    .sort((a, b) => {
      const ra = SEV_RANK[String(a.severity || "").toLowerCase()] ?? 3;
      const rb = SEV_RANK[String(b.severity || "").toLowerCase()] ?? 3;
      if (ra !== rb) return ra - rb;
      return (a.age_seconds ?? 1e18) - (b.age_seconds ?? 1e18); // 최신 우선
    });

  return (
    <div className="mx-auto max-w-4xl p-4">
      <PageHead
        title={t("risks.title", "리스크")}
        desc={t("risks.desc", "반복 실패·승인 지연·비용 급증 등 라이브 탐지")}
      >
        <Button variant="secondary" onClick={() => refetch()} className="text-xs">
          {t("common.refresh", "새로고침")}
        </Button>
      </PageHead>
      {isLoading && <Spinner />}
      {error && (
        <ErrorBox message={error instanceof Error ? error.message : String(error)} onRetry={() => refetch()} />
      )}
      {data && !items.length && !error && <Empty>{t("risks.empty", "감지된 리스크 없음")}</Empty>}
      <div className="space-y-3">
        {items.map((r, i) => {
          const sev = String(r.severity || "low");
          return (
            <Card key={`${r.type}:${r.ref_kind}:${r.ref_id}:${i}`} className="border-l-4">
              <div className="flex flex-wrap items-center gap-2">
                <Badge tone={sevTone(sev)}>{sevLabel(sev, t)}</Badge>
                <span className="text-sm font-semibold text-ink">
                  {riskText(r, "title", t, lang === "en") || r.type || t("risks.title", "리스크")}
                </span>
                <span className="ml-auto text-xs text-muted">
                  {r.type ? <span className="mr-1 rounded bg-surface-hover px-1">{r.type}</span> : null}
                  {fmtAge(r.age_seconds)} · {fmtTs(r.at)}
                </span>
              </div>
              {r.detail || r.i18n?.detail_key ? (
                <div className="mt-2 text-sm text-ink-2">{riskText(r, "detail", t, lang === "en")}</div>
              ) : null}
              {r.ref_kind && r.ref_id ? (
                <div className="mt-2">
                  <Button
                    variant="secondary"
                    className="px-2.5 py-1 text-xs"
                    onClick={() => goToRef(r.ref_kind, setView)}
                  >
                    {r.ref_kind} {t("risks.viewRef", "보기")}
                  </Button>
                </div>
              ) : null}
            </Card>
          );
        })}
      </div>
    </div>
  );
}
