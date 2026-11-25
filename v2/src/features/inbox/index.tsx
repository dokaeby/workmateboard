/** 알림 인박스 — GET /inbox 목록 + 읽음 처리 + unread-count 폴링.
 * 레거시 index.html의 loadInboxList/updateInboxBadge를 React Query로 이행. */
import { useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { apiGet, apiPost, apiDelete, ApiError } from "../../shared/api/client";
import type { InboxItem, ViewKey } from "../../shared/api/types";
import { useAppStore } from "../../shared/store/appStore";
import { useI18n, registerI18n } from "../../shared/i18n";
import { PageHead, Card, Empty, Spinner, ErrorBox, Button, Badge } from "../../shared/ui";
import { safeUrl } from "../../shared/lib/format";

registerI18n({
  ko: {
    "inbox.title": "알림",
    "inbox.desc": "세션 주의·승인·일정·리포트 알림 — 읽지 않은 알림만 보기 가능",
    "inbox.unread": "읽지 않음",
    "inbox.all": "전체",
    "inbox.readAll": "모두 읽음",
    "inbox.empty": "알림이 없습니다",
    "inbox.emptyUnread": "읽지 않은 알림이 없습니다",
    "inbox.noTitle": "(제목 없음)",
    "inbox.open": "열기",
    "k.notification": "알림",
    "k.session_attention": "세션 주의",
    "k.approval": "승인",
    "k.schedule": "일정",
    "k.report": "리포트",
    "inbox.ask": "자연어 쿼리",
    "inbox.askHint": "예: 오늘 실패한 run 보여줘",
    "inbox.askBtn": "질문",
    "inbox.askBusy": "답변 생성 중…",
    "inbox.askEmpty": "(빈 응답)",
    "inbox.reports": "리포트",
    "inbox.rptDaily": "일일",
    "inbox.rptWeekly": "주간",
    "inbox.rptExecDaily": "경영 일일",
    "inbox.rptExecWeekly": "경영 주간",
    "inbox.rptGenerate": "생성",
    "inbox.rptGenerating": "리포트 생성 중… (시간이 걸릴 수 있습니다)",
    "inbox.rptEmpty": "리포트를 불러오려면 버튼을 누르세요",
    "inbox.rptNoSections": "리포트 섹션이 없습니다",
    "inbox.cards": "카드",
    "inbox.cardEmpty": "등록된 카드가 없습니다",
    "inbox.cardOn": "사용",
    "inbox.cardOff": "중지",
    "inbox.cardRefresh": "새로고침",
    "inbox.cardDelete": "삭제",
    "inbox.cardDeleteConfirm": "이 카드를 삭제할까요?",
    "inbox.cardDisabled": "비활성화된 카드입니다",
    "inbox.rptGenerated": "생성",
    "inbox.cardErr": "오류",
    "inbox.countUnit": "건",
    "inbox.rptSec.runs": "실행",
    "inbox.rptSec.sessions": "세션",
    "inbox.rptSec.llm": "LLM 비용",
    "inbox.rptSec.approvals": "승인",
    "inbox.rptSec.radar": "Radar",
    "inbox.rptSec.notable_failures": "주요 실패",
    "inbox.rptSec.narrative": "요약",
    "inbox.rptSec.highlights": "주요 사항",
    "inbox.rptSec.risks": "리스크",
  },
  en: {
    "inbox.title": "Inbox",
    "inbox.desc": "Session attention, approvals, schedules, reports — filter unread",
    "inbox.unread": "Unread",
    "inbox.all": "All",
    "inbox.readAll": "Mark all read",
    "inbox.empty": "No notifications",
    "inbox.emptyUnread": "No unread notifications",
    "inbox.noTitle": "(no title)",
    "inbox.open": "Open",
    "k.notification": "Notice",
    "k.session_attention": "Session",
    "k.approval": "Approval",
    "k.schedule": "Schedule",
    "k.report": "Report",
    "inbox.ask": "Natural-language query",
    "inbox.askHint": "e.g. show me today's failed runs",
    "inbox.askBtn": "Ask",
    "inbox.askBusy": "Generating answer…",
    "inbox.askEmpty": "(empty response)",
    "inbox.reports": "Reports",
    "inbox.rptDaily": "Daily",
    "inbox.rptWeekly": "Weekly",
    "inbox.rptExecDaily": "Exec daily",
    "inbox.rptExecWeekly": "Exec weekly",
    "inbox.rptGenerate": "Generate",
    "inbox.rptGenerating": "Generating report… (may take a while)",
    "inbox.rptEmpty": "Press a button to load a report",
    "inbox.rptNoSections": "No report sections",
    "inbox.cards": "Cards",
    "inbox.cardEmpty": "No cards registered",
    "inbox.cardOn": "On",
    "inbox.cardOff": "Off",
    "inbox.cardRefresh": "Refresh",
    "inbox.cardDelete": "Delete",
    "inbox.cardDeleteConfirm": "Delete this card?",
    "inbox.cardDisabled": "This card is disabled",
    "inbox.rptGenerated": "Generated",
    "inbox.cardErr": "Error",
    "inbox.countUnit": " items",
    "inbox.rptSec.runs": "Runs",
    "inbox.rptSec.sessions": "Sessions",
    "inbox.rptSec.llm": "LLM cost",
    "inbox.rptSec.approvals": "Approvals",
    "inbox.rptSec.radar": "Radar",
    "inbox.rptSec.notable_failures": "Notable failures",
    "inbox.rptSec.narrative": "Summary",
    "inbox.rptSec.highlights": "Highlights",
    "inbox.rptSec.risks": "Risks",
  },
});

// 레거시 INBOX_KIND_LABEL/CSS 톤 대응
const KIND_TONE: Record<string, "muted" | "ok" | "warn" | "err" | "accent"> = {
  notification: "muted",
  session_attention: "warn",
  approval: "accent",
  schedule: "ok",
  report: "ok",
};

// 링크의 ?view= 파라미터 → ViewKey (레거시 VIEW_ALIASES: reports→inbox)
const VIEW_ALIASES: Record<string, ViewKey> = { reports: "inbox" };
const VALID_VIEWS = new Set<ViewKey>([
  "office", "radar", "gmail", "calendar", "runs", "inbox", "projects",
  "sessionlog", "risks", "approvals", "perf", "insights", "automation",
  "system", "archive", "cost", "developer", "playbooks", "connectors",
  "triggers", "builder", "team", "audit", "settings",
]);

function kindLabel(kind: string, t: (k: string, f?: string) => string): string {
  const key = `k.${kind || "notification"}`;
  const known = ["notification", "session_attention", "approval", "schedule", "report"];
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

export default function InboxPage() {
  const { t } = useI18n();
  const setView = useAppStore((s) => s.setView);
  const qc = useQueryClient();
  const [unreadOnly, setUnreadOnly] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);

  const listQ = useQuery({
    queryKey: ["inbox", unreadOnly],
    queryFn: () =>
      apiGet<InboxItem[]>(`/inbox?limit=50${unreadOnly ? "&unread_only=true" : ""}`),
    refetchInterval: 30000,
  });
  const countQ = useQuery({
    queryKey: ["inbox-unread-count"],
    queryFn: () => apiGet<{ count: number }>("/inbox/unread-count"),
    refetchInterval: 15000,
  });

  const items = Array.isArray(listQ.data) ? listQ.data : [];
  const unreadCount = countQ.data?.count ?? items.filter((i) => !i.read).length;

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
        title={t("inbox.title", "알림")}
        desc={t("inbox.desc", "세션 주의·승인·일정·리포트 알림")}
      >
        <Badge tone={unreadCount > 0 ? "accent" : "muted"}>
          {t("inbox.unread", "읽지 않음")} {unreadCount}{t("inbox.countUnit", "건")}
        </Badge>
        <Button
          variant={unreadOnly ? "primary" : "secondary"}
          className="text-xs"
          onClick={() => setUnreadOnly((v) => !v)}
        >
          {unreadOnly ? t("inbox.unread", "읽지 않음") : t("inbox.all", "전체")}
        </Button>
        <Button variant="secondary" className="text-xs" onClick={readAll}>
          {t("inbox.readAll", "모두 읽음")}
        </Button>
        <Button variant="ghost" className="text-xs" onClick={() => listQ.refetch()}>
          {t("common.refresh", "새로고침")}
        </Button>
      </PageHead>
      <Card>
        {listQ.isLoading && <Spinner />}
        {listQ.error && (
          <ErrorBox
            message={listQ.error instanceof Error ? listQ.error.message : String(listQ.error)}
            onRetry={() => listQ.refetch()}
          />
        )}
        {listQ.data && !items.length && (
          <Empty>{unreadOnly ? t("inbox.emptyUnread", "읽지 않은 알림이 없습니다") : t("inbox.empty", "알림이 없습니다")}</Empty>
        )}
        {items.map((it) => {
          const kind = it.kind || "notification";
          const when = (it.created_at || "").slice(0, 16).replace("T", " ");
          return (
            <button
              key={String(it.id)}
              type="button"
              onClick={() => openItem(it)}
              disabled={busyId === String(it.id)}
              className={`block w-full border-b border-border px-1 py-3 text-left last:border-0 transition-colors hover:bg-surface-hover ${
                it.read ? "" : "bg-accent-soft/40"
              }`}
            >
              <div className="flex items-center gap-2">
                {!it.read && <span className="h-2 w-2 shrink-0 rounded-full bg-accent" />}
                <Badge tone={KIND_TONE[kind] || "muted"}>{kindLabel(kind, t)}</Badge>
                <span className={`min-w-0 flex-1 truncate text-sm ${it.read ? "text-ink-2" : "font-semibold text-ink"}`}>
                  {it.title || t("inbox.noTitle", "(제목 없음)")}
                </span>
                <span className="shrink-0 text-xs text-muted">{when}</span>
              </div>
              {it.body ? <div className="mt-1 line-clamp-2 text-xs text-muted">{it.body}</div> : null}
              {it.link ? (
                <div className="mt-1 flex items-center gap-1 text-xs text-accent">
                  {t("inbox.open", "열기")}
                  <span className="truncate">{it.link}</span>
                </div>
              ) : null}
            </button>
          );
        })}
      </Card>
      <AskCard />
      <ReportsCard />
      <CardsCard />
    </div>
  );
}
