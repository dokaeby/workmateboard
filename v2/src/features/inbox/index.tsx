/** 알림 인박스 — GET /inbox 목록 + 읽음 처리 + unread-count 폴링.
 * 레거시 index.html의 loadInboxList/updateInboxBadge를 React Query로 이행. */
import { useMemo, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { apiGet, apiPost, apiDelete, ApiError } from "../../shared/api/client";
import type { InboxItem, ViewKey } from "../../shared/api/types";
import { useAppStore, roleAllows } from "../../shared/store/appStore";
import { useI18n, tx, type TFn } from "../../shared/i18n";
import { PageHead, Card, Empty, Spinner, ErrorBox, Button, Badge } from "../../shared/ui";
import { safeUrl } from "../../shared/lib/format";
import { TrendArea } from "../office/charts";
import { useObsNotifications } from "../office/queries";
import ReportSubsCard from "./subscriptions";
import "./i18n";


// 레거시 INBOX_KIND_LABEL/CSS 톤 대응
const KIND_TONE: Record<string, "muted" | "ok" | "warn" | "err" | "accent"> = {
  notification: "muted",
  session_attention: "warn",
  approval: "accent",
  schedule: "ok",
  report: "ok",
  budget_alert: "warn",
  budget: "warn",
  recovery: "warn",
  eval: "warn", // 온라인 평가 불합격 (spec 008)
};

// 링크의 ?view= 파라미터 → ViewKey (레거시 VIEW_ALIASES: reports→inbox)
const VIEW_ALIASES: Record<string, ViewKey> = { reports: "inbox" };
const VALID_VIEWS = new Set<ViewKey>([
  "office", "radar", "gmail", "calendar", "runs", "inbox", "projects",
  "sessionlog", "risks", "approvals", "perf", "insights", "automation",
  "system", "archive", "cost", "developer", "playbooks", "connectors",
  "triggers", "builder", "team", "audit", "settings", "agents", "goals", "evals",
]);

const RPT_KIND_EN: Record<string, string> = {
  daily: "Daily",
  weekly: "Weekly",
  exec_daily: "Exec daily",
  exec_weekly: "Exec weekly",
};

/** DB에 저장된 한국어 인박스 제목을 언어별로 렌더 — 알려진 패턴만 번역, 나머지 원문. */
function inboxItemTitle(it: InboxItem, t: TFn): string {
  const title = String(it.title || "");
  const kind = it.kind || "notification";
  let m: RegExpMatchArray | null;
  if ((m = title.match(/^승인 대기: (.+)$/))) return tx(t, "inbox.t.approval", { wf: m[1] }, title);
  if ((m = title.match(/^스케줄 실행: (.+)$/))) return tx(t, "inbox.t.scheduleRun", { wf: m[1] }, title);
  if ((m = title.match(/^스케줄 실패: (.+)$/))) return tx(t, "inbox.t.scheduleFail", { wf: m[1] }, title);
  if ((m = title.match(/^승인 필요: (.+)$/))) return tx(t, "inbox.t.attention", { what: m[1] }, title);
  if (
    kind === "budget_alert" &&
    (m = title.match(/^LLM 토큰 예산 (\d+(?:\.\d+)?)% 사용 \(([^)]+)\)$/))
  ) {
    return tx(t, "inbox.t.budget", { pct: m[1], month: m[2] }, title);
  }
  if ((m = title.match(/^온라인 평가 불합격: (.+)$/))) {
    return tx(t, "inbox.t.evalFail", { name: m[1] }, title);
  }
  if (kind === "report") {
    if ((m = title.match(/^(.+?) 리포트 \(트리거\)$/))) {
      return tx(t, "inbox.t.reportTrigger", { kind: RPT_KIND_EN[m[1]] || m[1] }, title);
    }
    if ((m = title.match(/^(.+?) 리포트 생성됨$/))) {
      return tx(t, "inbox.t.reportDone", { kind: RPT_KIND_EN[m[1]] || m[1] }, title);
    }
  }
  return title;
}

function kindLabel(kind: string, t: (k: string, f?: string) => string): string {
  const key = `k.${kind || "notification"}`;
  const known = ["notification", "session_attention", "approval", "schedule", "report", "budget_alert", "budget", "recovery", "eval"];
  return known.includes(kind) ? t(key, kind) : kind || t("k.notification", "알림");
}

function viewFromLink(link: string | undefined): ViewKey | null {
  if (!link) return null;
  try {
    const u = new URL(link, window.location.origin);
    const v0 = u.searchParams.get("view");
    if (!v0) return null;
    const v = (VIEW_ALIASES[v0] || v0) as ViewKey;
    return VALID_VIEWS.has(v) ? v : null;
  } catch {
    return null;
  }
}

/* ── 자연어 쿼리 (POST /ask) — 레거시 ask-form 이행 ── */
function AskCard() {
  const { t } = useI18n();
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState(false);
  const [answer, setAnswer] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const query = q.trim();
    if (!query || busy) return;
    setBusy(true);
    setErr(null);
    try {
      // 백엔드 AskRequest 필드는 question (레거시와 동일)
      const d = await apiPost<{ answer?: string }>("/ask", { question: query });
      setAnswer(d.answer || t("inbox.askEmpty", "(빈 응답)"));
    } catch (ex) {
      setErr(ex instanceof ApiError ? ex.message : String(ex));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card title={t("inbox.ask", "자연어 쿼리")} className="mb-3">
      <form onSubmit={submit} className="flex gap-2">
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder={t("inbox.askHint", "예: 오늘 실패한 run 보여줘")}
          autoComplete="off"
          className="min-w-0 flex-1 rounded-md border border-border bg-surface px-2 py-1.5 text-sm"
        />
        <Button type="submit" disabled={busy || !q.trim()} className="px-3 py-1 text-xs">
          {busy ? t("inbox.askBusy", "답변 생성 중…") : t("inbox.askBtn", "질문")}
        </Button>
      </form>
      {err && <div className="mt-2 text-xs text-err">{err}</div>}
      {answer !== null && (
        <div className="mt-3 whitespace-pre-wrap rounded-lg border border-border bg-surface-hover p-3 text-sm text-ink">
          {answer}
        </div>
      )}
    </Card>
  );
}

/* ── 리포트 (GET/POST /reports) — 레거시 rpt-btn/rpt-generate 이행 ── */
const RPT_KINDS = [
  { kind: "daily", labelKey: "inbox.rptDaily", fallback: "일일" },
  { kind: "weekly", labelKey: "inbox.rptWeekly", fallback: "주간" },
  { kind: "exec_daily", labelKey: "inbox.rptExecDaily", fallback: "경영 일일" },
  { kind: "exec_weekly", labelKey: "inbox.rptExecWeekly", fallback: "경영 주간" },
] as const;

// 레거시 _RPT_SECTION_LABEL/_RPT_META_KEYS 대응
const RPT_SECTION_LABEL: Record<string, string> = {
  runs: "실행", sessions: "세션", llm: "LLM 비용", approvals: "승인",
  radar: "Radar", notable_failures: "주요 실패",
  narrative: "요약", highlights: "주요 사항", risks: "리스크",
};
const RPT_META_KEYS = new Set(["kind", "generated_at", "period"]);

function renderReportBody(r: unknown, t: (k: string, f?: string) => string): ReactNode {
  // GET /reports 응답 = build_report() 결과 — 최상위 키가 곧 섹션.
  // content가 문자열(내러티브)이면 그대로 렌더 (레거시 _renderReportSections와 동일).
  const obj = (r && typeof r === "object" ? r : {}) as Record<string, unknown>;
  const content = obj.content;
  if (typeof content === "string" && content.trim()) {
    return <div className="whitespace-pre-wrap text-sm text-ink">{content}</div>;
  }
  const body = (content && typeof content === "object" ? content : obj) as Record<string, unknown>;
  const secs = Object.fromEntries(Object.entries(body).filter(([k]) => !RPT_META_KEYS.has(k)));
  const keys = Object.keys(secs);
  if (!keys.length) return <Empty>{t("inbox.rptNoSections", "리포트 섹션이 없습니다")}</Empty>;
  return (
    <div className="space-y-3">
      {keys.map((k) => {
        const v = secs[k];
        return (
          <div key={k} className="rounded-lg border border-border p-3">
            <h4 className="mb-1 text-sm font-semibold text-ink">
              {t(`inbox.rptSec.${k}`, RPT_SECTION_LABEL[k] || k)}
            </h4>
            {Array.isArray(v) ? (
              v.length ? (
                <ul className="list-disc space-y-0.5 pl-5 text-xs text-ink-2">
                  {v.map((x, i) => (
                    <li key={i}>{typeof x === "object" ? JSON.stringify(x) : String(x)}</li>
                  ))}
                </ul>
              ) : (
                <span className="text-xs text-muted">-</span>
              )
            ) : v && typeof v === "object" ? (
              <pre className="overflow-x-auto rounded bg-surface-hover p-2 text-xs text-ink-2">
                {JSON.stringify(v, null, 2).slice(0, 4000)}
              </pre>
            ) : (
              <div className="text-sm text-ink-2">{String(v ?? "-")}</div>
            )}
          </div>
        );
      })}
    </div>
  );
}

function ReportsCard() {
  const { t } = useI18n();
  const [rptKind, setRptKind] = useState<string>("daily");
  const [genBusy, setGenBusy] = useState(false);
  const [genErr, setGenErr] = useState<string | null>(null);
  const rptQ = useQuery({
    queryKey: ["report", rptKind],
    queryFn: () => apiGet<Record<string, unknown>>(`/reports?kind=${encodeURIComponent(rptKind)}`),
  });

  const generate = async () => {
    setGenBusy(true);
    setGenErr(null);
    try {
      await apiPost("/reports/generate", { kind: rptKind });
      await rptQ.refetch();
    } catch (e) {
      setGenErr(e instanceof ApiError ? e.message : String(e));
    } finally {
      setGenBusy(false);
    }
  };

  const rpt = Array.isArray(rptQ.data) ? rptQ.data[0] : rptQ.data;
  const generatedAt =
    rpt && typeof rpt === "object" ? String((rpt as Record<string, unknown>).generated_at || "") : "";

  return (
    <Card
      title={t("inbox.reports", "리포트")}
      className="mb-3"
      actions={
        <Button variant="secondary" className="px-3 py-1 text-xs" disabled={genBusy} onClick={generate}>
          {genBusy ? t("inbox.rptGenerating", "리포트 생성 중…") : t("inbox.rptGenerate", "생성")}
        </Button>
      }
    >
      <div className="mb-2 flex flex-wrap items-center gap-1.5">
        {RPT_KINDS.map((k) => (
          <Button
            key={k.kind}
            variant={rptKind === k.kind ? "primary" : "secondary"}
            className="px-3 py-1 text-xs"
            onClick={() => setRptKind(k.kind)}
          >
            {t(k.labelKey, k.fallback)}
          </Button>
        ))}
        {generatedAt && (
          <span className="ml-auto text-xs text-muted">
            {t("inbox.rptGenerated", "생성")} {generatedAt.slice(0, 16).replace("T", " ")}
          </span>
        )}
      </div>
      {genErr && <div className="mb-2 text-xs text-err">{genErr}</div>}
      {rptQ.isLoading && <Spinner />}
      {rptQ.error && (
        <ErrorBox
          message={rptQ.error instanceof Error ? rptQ.error.message : String(rptQ.error)}
          onRetry={() => rptQ.refetch()}
        />
      )}
      {rptQ.data !== undefined && !genBusy && (
        rpt && typeof rpt === "object"
          ? renderReportBody(rpt, t)
          : <Empty>{t("inbox.rptEmpty", "리포트를 불러오려면 버튼을 누르세요")}</Empty>
      )}
    </Card>
  );
}

/* ── 커스텀 카드 (/cards) — 레거시 loadCards 이행 ──
 * TODO: 카드 생성/수정 폼(레거시 card-form)은 미이행 — 필요 시 추가. */
interface CardItem {
  id?: string;
  title?: string;
  url?: string;
  json_path?: string;
  enabled?: boolean;
}

function cardValueText(v: unknown): string {
  if (v && typeof v === "object") return JSON.stringify(v);
  return String(v ?? "-");
}

function CardWidget({ card }: { card: CardItem }) {
  const { t } = useI18n();
  const qc = useQueryClient();
  const id = String(card.id ?? "");
  const enabled = card.enabled !== false;
  const [busy, setBusy] = useState(false);
  const dataQ = useQuery({
    queryKey: ["card-data", id],
    queryFn: () =>
      apiGet<Record<string, unknown>>(`/cards/${encodeURIComponent(id)}/data`),
    enabled: enabled && !!id,
    retry: false,
  });

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["cards"] });
    qc.invalidateQueries({ queryKey: ["card-data", id] });
  };

  const toggle = async () => {
    setBusy(true);
    try {
      // ScheduleToggleRequest.enabled 필수 — 현재 상태의 반대값 전송
      await apiPost(`/cards/${encodeURIComponent(id)}/toggle`, { enabled: !enabled });
      invalidate();
    } catch (e) {
      alert(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const del = async () => {
    if (!window.confirm(t("inbox.cardDeleteConfirm", "이 카드를 삭제할까요?"))) return;
    setBusy(true);
    try {
      await apiDelete(`/cards/${encodeURIComponent(id)}`);
      invalidate();
    } catch (e) {
      alert(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const raw = dataQ.data;
  const v = raw && typeof raw === "object" && "value" in raw
    ? (raw as Record<string, unknown>).value
    : raw;

  return (
    <div className="mb-3 rounded-lg border border-border p-3 last:mb-0">
      <div className="flex items-center gap-2">
        <span className="min-w-0 flex-1 truncate text-sm font-medium text-ink">
          {card.title || t("inbox.noTitle", "(제목 없음)")}
        </span>
        <Badge tone={enabled ? "ok" : "muted"}>
          {enabled ? t("inbox.cardOn", "사용") : t("inbox.cardOff", "중지")}
        </Badge>
      </div>
      {(card.url || card.json_path) && (
        <div className="mt-1 truncate font-mono text-[11px] text-muted">
          {card.url}{card.json_path ? ` · ${card.json_path}` : ""}
        </div>
      )}
      <div className="mt-2 min-h-5 text-sm text-ink-2">
        {!enabled ? (
          <span className="text-xs text-muted">{t("inbox.cardDisabled", "비활성화된 카드입니다")}</span>
        ) : dataQ.isLoading ? (
          <span className="text-xs text-muted">…</span>
        ) : dataQ.error ? (
          <span className="text-xs text-err">
            {t("inbox.cardErr", "오류")}: {dataQ.error instanceof Error ? dataQ.error.message : String(dataQ.error)}
          </span>
        ) : (
          <span className="break-all">{cardValueText(v)}</span>
        )}
      </div>
      <div className="mt-2 flex gap-1.5">
        <Button variant="secondary" className="px-2.5 py-1 text-xs" disabled={busy} onClick={toggle}>
          {enabled ? t("inbox.cardOff", "중지") : t("inbox.cardOn", "사용")}
        </Button>
        <Button variant="secondary" className="px-2.5 py-1 text-xs" disabled={!enabled} onClick={() => dataQ.refetch()}>
          {t("inbox.cardRefresh", "새로고침")}
        </Button>
        <Button variant="ghost" className="px-2.5 py-1 text-xs" disabled={busy} onClick={del}>
          {t("inbox.cardDelete", "삭제")}
        </Button>
      </div>
    </div>
  );
}

function CardsCard() {
  const { t } = useI18n();
  const cardsQ = useQuery({
    queryKey: ["cards"],
    queryFn: () => apiGet<{ items?: CardItem[] }>("/cards"),
    refetchInterval: 60000, // 레거시 60초 자동 갱신 대응
  });
  const items = cardsQ.data?.items || [];

  return (
    <Card title={t("inbox.cards", "카드")} className="mb-3">
      {cardsQ.isLoading && <Spinner />}
      {cardsQ.error && (
        <ErrorBox
          message={cardsQ.error instanceof Error ? cardsQ.error.message : String(cardsQ.error)}
          onRetry={() => cardsQ.refetch()}
        />
      )}
      {cardsQ.data && !items.length && (
        <Empty>{t("inbox.cardEmpty", "등록된 카드가 없습니다")}</Empty>
      )}
      {items.map((c, i) => <CardWidget key={String(c.id ?? i)} card={c} />)}
    </Card>
  );
}

/* ── spec 014 US1 — KPI 행 + 발생 추이 + 필터 가능한 알림 테이블 ──
 * lead: /observability/notifications?days=7 집계(읽음=open/resolved 아님 —
 *       알림 kind='notification'만, resolved=읽음). member: 불러온 inbox
 *       항목에서 클라이언트 집계(표본=최근 50건, KPI sub에 명시). */
function InboxKpis({ items }: { items: InboxItem[] }) {
  const { t } = useI18n();
  const role = useAppStore((s) => s.role);
  const isLead = roleAllows("lead", role);
  const obsQ = useObsNotifications(7);

  const memberStats = useMemo(() => {
    const open = items.filter((i) => !i.read).length;
    return { total: items.length, open, resolved: items.length - open };
  }, [items]);

  const stats = isLead && obsQ.data
    ? { total: Number(obsQ.data.total) || 0, open: Number(obsQ.data.open) || 0, resolved: Number(obsQ.data.resolved) || 0 }
    : memberStats;
  const sub = isLead
    ? t("inbox.kpi.subObs", "최근 7일 · 알림 kind 기준")
    : t("inbox.kpi.subList", "불러온 항목 기준 (최대 50건)");

  const cards: { key: string; label: string; value: number; tone: "muted" | "warn" | "ok" }[] = [
    { key: "total", label: t("inbox.kpi.total", "전체"), value: stats.total, tone: "muted" },
    { key: "open", label: t("inbox.kpi.open", "미해결"), value: stats.open, tone: "warn" },
    { key: "resolved", label: t("inbox.kpi.resolved", "해결"), value: stats.resolved, tone: "ok" },
  ];
  const toneCls = { muted: "text-ink", warn: "text-warn", ok: "text-ok" } as const;

  return (
    <div className="mb-3 grid grid-cols-3 gap-2">
      {cards.map((c) => (
        <div key={c.key} className="rounded-lg border border-border bg-surface-hover px-2.5 py-1.5">
          <div className="text-[10px] text-muted">{c.label}</div>
          <div className={`text-base font-semibold tabular-nums ${toneCls[c.tone]}`}>{c.value.toLocaleString()}</div>
          <div className="truncate text-[10px] text-muted">{sub}</div>
        </div>
      ))}
    </div>
  );
}

function InboxTrend({ items }: { items: InboxItem[] }) {
  const { t } = useI18n();
  const role = useAppStore((s) => s.role);
  const isLead = roleAllows("lead", role);
  const obsQ = useObsNotifications(7);

  const points = useMemo(() => {
    if (isLead && obsQ.data?.series) {
      return (Array.isArray(obsQ.data.series) ? obsQ.data.series : [])
        .map((p) => ({ label: String(p.t || "").slice(5, 10), value: Number(p.count) || 0 }))
        .filter((p) => p.label);
    }
    // member — 불러온 항목의 created_at 일자 버킷
    const buckets = new Map<string, number>();
    for (const it of items) {
      const d = String(it.created_at || "").slice(0, 10);
      if (d) buckets.set(d, (buckets.get(d) || 0) + 1);
    }
    return [...buckets.entries()]
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([d, n]) => ({ label: d.slice(5), value: n }));
  }, [isLead, obsQ.data, items]);

  return (
    <div className="mb-3 rounded-lg border border-border bg-surface-hover/50 px-3 py-2">
      <div className="mb-1 text-[11px] font-medium text-muted">
        {t("inbox.trend", "발생 추이")}
        <span className="ml-1 font-normal">
          {isLead ? t("inbox.trendSubObs", "최근 7일") : t("inbox.trendSubList", "불러온 항목 기준")}
        </span>
      </div>
      {isLead && obsQ.isLoading ? <Spinner /> : (
        <TrendArea
          points={points}
          height={72}
          empty={t("inbox.trendEmpty", "기간 내 알림이 없습니다")}
        />
      )}
    </div>
  );
}

export default function InboxPage() {
  const { t } = useI18n();
  const setView = useAppStore((s) => s.setView);
  const qc = useQueryClient();
  const [statusFilter, setStatusFilter] = useState<"all" | "unread" | "read">("all");
  const [kindFilter, setKindFilter] = useState("all");
  const [kw, setKw] = useState("");
  const [busyId, setBusyId] = useState<string | null>(null);

  const listQ = useQuery({
    queryKey: ["inbox"],
    queryFn: () => apiGet<InboxItem[]>("/inbox?limit=50"),
    refetchInterval: 30000,
  });
  const countQ = useQuery({
    queryKey: ["inbox-unread-count"],
    queryFn: () => apiGet<{ count: number }>("/inbox/unread-count"),
    refetchInterval: 15000,
  });

  const items = useMemo(() => (Array.isArray(listQ.data) ? listQ.data : []), [listQ.data]);
  const unreadCount = countQ.data?.count ?? items.filter((i) => !i.read).length;

  const kinds = useMemo(
    () => [...new Set(items.map((i) => String(i.kind || "notification")))].sort(),
    [items],
  );
  const filtered = useMemo(() => {
    const needle = kw.trim().toLowerCase();
    return items.filter((it) => {
      if (statusFilter === "unread" && it.read) return false;
      if (statusFilter === "read" && !it.read) return false;
      if (kindFilter !== "all" && String(it.kind || "notification") !== kindFilter) return false;
      if (needle) {
        const hay = [it.title, it.body, it.kind, it.link].map((x) => String(x || "").toLowerCase()).join(" ");
        if (!hay.includes(needle)) return false;
      }
      return true;
    });
  }, [items, statusFilter, kindFilter, kw]);

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["inbox"] });
    qc.invalidateQueries({ queryKey: ["inbox-unread-count"] });
  };

  const openItem = async (it: InboxItem) => {
    const id = String(it.id ?? "");
    if (id && !it.read) {
      setBusyId(id);
      try {
        await apiPost(`/inbox/${encodeURIComponent(id)}/read`);
      } catch {
        /* 읽음 처리 실패해도 이동은 허용 (레거시와 동일) */
      } finally {
        setBusyId(null);
        invalidate();
      }
    }
    const link = it.link || "";
    const view = viewFromLink(link);
    if (view) {
      setView(view);
      return;
    }
    const safe = safeUrl(link);
    if (safe && safe !== "#") {
      // 외부 http(s) 링크는 새 탭, 동일 출처 경로는 현재 탭 이동
      try {
        const u = new URL(safe, window.location.origin);
        if (u.origin === window.location.origin) window.location.href = u.href;
        else window.open(u.href, "_blank", "noopener");
      } catch { /* 무시 */ }
    }
  };

  const readAll = async () => {
    try {
      await apiPost("/inbox/read-all");
      invalidate();
    } catch (e) {
      alert(e instanceof ApiError ? e.message : String(e));
    }
  };

  return (
    <div className="mx-auto max-w-3xl p-4">
      <PageHead
        title={t("inbox.title", "알림 & 공지")}
        desc={t("inbox.desc", "세션 주의·승인·일정·리포트 알림")}
      >
        <Badge tone={unreadCount > 0 ? "accent" : "muted"}>
          {t("inbox.unread", "읽지 않음")} {unreadCount}{t("inbox.countUnit", "건")}
        </Badge>
        <Button variant="secondary" className="text-xs" onClick={readAll}>
          {t("inbox.readAll", "모두 읽음")}
        </Button>
        <Button variant="ghost" className="text-xs" onClick={() => listQ.refetch()}>
          {t("common.refresh", "새로고침")}
        </Button>
      </PageHead>

      <InboxKpis items={items} />
      <InboxTrend items={items} />

      <Card>
        <div className="mb-2 flex flex-wrap items-center gap-1.5">
          <select
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value as "all" | "unread" | "read")}
            aria-label={t("inbox.fStatus", "상태 필터")}
            className="rounded-md border border-border bg-surface px-2 py-1 text-xs"
          >
            <option value="all">{t("inbox.fAll", "전체 상태")}</option>
            <option value="unread">{t("inbox.unread", "읽지 않음")}</option>
            <option value="read">{t("inbox.fRead", "읽음")}</option>
          </select>
          <select
            value={kindFilter}
            onChange={(e) => setKindFilter(e.target.value)}
            aria-label={t("inbox.fKind", "유형 필터")}
            className="rounded-md border border-border bg-surface px-2 py-1 text-xs"
          >
            <option value="all">{t("inbox.fAllKind", "전체 유형")}</option>
            {kinds.map((k) => (
              <option key={k} value={k}>{kindLabel(k, t)}</option>
            ))}
          </select>
          <input
            value={kw}
            onChange={(e) => setKw(e.target.value)}
            placeholder={t("inbox.fKw", "제목·본문 검색")}
            autoComplete="off"
            className="min-w-40 flex-1 rounded-md border border-border bg-surface px-2 py-1 text-xs"
          />
          <span className="text-[11px] tabular-nums text-muted">
            {t("inbox.fCount", "{shown}/{total}건")
              .replace("{shown}", String(filtered.length))
              .replace("{total}", String(items.length))}
          </span>
        </div>
        {listQ.isLoading && <Spinner />}
        {listQ.error && (
          <ErrorBox
            message={listQ.error instanceof Error ? listQ.error.message : String(listQ.error)}
            onRetry={() => listQ.refetch()}
          />
        )}
        {listQ.data && !filtered.length && (
          <Empty>
            {items.length
              ? t("inbox.fEmpty", "필터에 맞는 알림이 없습니다")
              : t("inbox.empty", "알림이 없습니다")}
          </Empty>
        )}
        {filtered.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border text-left text-[11px] text-muted">
                  <th className="w-16 py-1.5 pr-3 font-medium">{t("inbox.colStatus", "상태")}</th>
                  <th className="w-24 py-1.5 pr-3 font-medium">{t("inbox.colKind", "유형")}</th>
                  <th className="py-1.5 pr-3 font-medium">{t("inbox.colTitle", "제목")}</th>
                  <th className="w-36 py-1.5 font-medium whitespace-nowrap">{t("inbox.colTime", "시각")}</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((it) => {
                  const kind = it.kind || "notification";
                  const when = (it.created_at || "").slice(0, 16).replace("T", " ");
                  return (
                    <tr
                      key={String(it.id)}
                      onClick={() => openItem(it)}
                      className={`cursor-pointer border-b border-border align-top last:border-0 transition-colors hover:bg-surface-hover ${
                        it.read ? "" : "bg-accent-soft/30"
                      } ${busyId === String(it.id) ? "opacity-50" : ""}`}
                    >
                      <td className="py-2 pr-3">
                        <Badge tone={it.read ? "muted" : "accent"}>
                          {it.read ? t("inbox.fRead", "읽음") : t("inbox.unread", "읽지 않음")}
                        </Badge>
                      </td>
                      <td className="py-2 pr-3">
                        <Badge tone={KIND_TONE[kind] || "muted"}>{kindLabel(kind, t)}</Badge>
                      </td>
                      <td className="max-w-0 py-2 pr-3">
                        <div className={`truncate text-sm ${it.read ? "text-ink-2" : "font-semibold text-ink"}`}>
                          {!it.read && <span className="mr-1 inline-block h-1.5 w-1.5 rounded-full bg-accent align-middle" />}
                          {inboxItemTitle(it, t) || t("inbox.noTitle", "(제목 없음)")}
                        </div>
                        {it.body ? <div className="mt-0.5 line-clamp-1 text-xs text-muted">{it.body}</div> : null}
                        {it.link ? (
                          <div className="mt-0.5 flex items-center gap-1 text-[11px] text-accent">
                            {t("inbox.open", "열기")}
                            <span className="truncate">{it.link}</span>
                          </div>
                        ) : null}
                      </td>
                      <td className="py-2 text-right text-[11px] whitespace-nowrap text-muted tabular-nums">{when}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>
      <AskCard />
      <ReportsCard />
      <ReportSubsCard />
      <CardsCard />
    </div>
  );
}
