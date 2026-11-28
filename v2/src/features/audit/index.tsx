/** 감사 로그 — /audit 테이블 (admin 전용). spec 014 US2: 터미널 스타일
 * 타임라인(ts | 레벨/액션 배지 | actor | action | target | detail).
 * 필터: 기간/행위자/액션 유형/키워드 — 전부 로드된 항목에 클라이언트 적용.
 * 페이지네이션: 백엔드 keyset cursor(next_cursor)로 "더 보기". */
import { useMemo, useState } from "react";
import { useInfiniteQuery } from "@tanstack/react-query";
import { apiGet } from "../../shared/api/client";
import { useAppStore, roleAllows } from "../../shared/store/appStore";
import { useI18n } from "../../shared/i18n";
import { PageHead, Card, Empty, Spinner, ErrorBox, Button, Badge } from "../../shared/ui";
import { fmtTs, jsonSummary } from "../../shared/lib/format";
import { maskSecrets } from "../../shared/lib/redact";
import "./i18n";


interface AuditItem {
  id?: string;
  ts?: string;
  actor_email?: string;
  action?: string;
  target?: string;
  detail?: unknown;
  run_id?: string;
}

interface AuditResponse { items?: AuditItem[]; next_cursor?: string | null }

const PAGE_SIZE = 100;
const PERIOD_DAYS: Record<string, number> = { all: 0, "7": 7, "30": 30, "90": 90 };

/** action 문자열 → 터미널 레벨 톤 (표시용 분류 — 실제 severity 필드는 없음). */
function actionTone(action: string): "err" | "warn" | "ok" | "accent" | "muted" {
  const a = action.toLowerCase();
  if (/fail|error|denied|reject|delete|remove|revoke|block/.test(a)) return "err";
  if (/warn|expire|stale|retry/.test(a)) return "warn";
  if (/login|logout|auth|token/.test(a)) return "ok";
  if (/create|update|grant|approve|register|enable/.test(a)) return "accent";
  return "muted";
}

function inPeriod(ts: string | undefined, days: number): boolean {
  if (!days || !ts) return true;
  const t = new Date(ts).getTime();
  if (isNaN(t)) return true;
  return Date.now() - t <= days * 86400_000;
}

function AuditBody() {
  const { t } = useI18n();
  const [period, setPeriod] = useState("all");
  const [actor, setActor] = useState("");
  const [actionSel, setActionSel] = useState("all");
  const [kw, setKw] = useState("");

  const q = useInfiniteQuery({
    queryKey: ["audit"],
    queryFn: ({ pageParam }) => {
      const cursor = typeof pageParam === "string" && pageParam ? `&cursor=${encodeURIComponent(pageParam)}` : "";
      return apiGet<AuditResponse>(`/audit?limit=${PAGE_SIZE}${cursor}`);
    },
    initialPageParam: "",
    getNextPageParam: (last) => last.next_cursor || undefined,
    refetchInterval: 60000,
  });

  const pages = useMemo(() => q.data?.pages || [], [q.data]);
  const allItems = useMemo(() => pages.flatMap((p) => p.items || []), [pages]);

  const actions = useMemo(
    () => [...new Set(allItems.map((a) => String(a.action || "")).filter(Boolean))].sort(),
    [allItems],
  );

  const items = useMemo(() => {
    const days = PERIOD_DAYS[period] ?? 0;
    const actorNeedle = actor.trim().toLowerCase();
    const kwNeedle = kw.trim().toLowerCase();
    return allItems.filter((a) => {
      if (!inPeriod(a.ts, days)) return false;
      if (actionSel !== "all" && String(a.action || "") !== actionSel) return false;
      if (actorNeedle && !String(a.actor_email || "").toLowerCase().includes(actorNeedle)) return false;
      if (kwNeedle) {
        const hay = [a.action, a.target, a.actor_email, a.detail == null ? "" : jsonSummary(a.detail, 300)]
          .map((x) => String(x || "").toLowerCase()).join(" ");
        if (!hay.includes(kwNeedle)) return false;
      }
      return true;
    });
  }, [allItems, period, actor, actionSel, kw]);

  const selCls = "rounded-md border border-border bg-surface px-2 py-1 text-xs";

  return (
    <div className="mx-auto max-w-6xl p-4">
      <PageHead
        title={t("audit.title", "활동 로그")}
        desc={t("audit.desc", "관리자·권한 변경 등 감사 이벤트 — 최신순")}
      >
        <Button variant="secondary" className="text-xs" onClick={() => q.refetch()}>
          {t("common.refresh", "새로고침")}
        </Button>
      </PageHead>

      <Card title={t("audit.terminal", "이벤트 타임라인")}>
        <div className="mb-2 flex flex-wrap items-center gap-1.5">
          <select value={period} onChange={(e) => setPeriod(e.target.value)} aria-label={t("audit.fPeriod", "기간 필터")} className={selCls}>
            <option value="all">{t("audit.pAll", "전체 기간")}</option>
            <option value="7">{t("common.last7", "최근 7일")}</option>
            <option value="30">{t("common.last30", "최근 30일")}</option>
            <option value="90">{t("common.last90", "최근 90일")}</option>
          </select>
          <select value={actionSel} onChange={(e) => setActionSel(e.target.value)} aria-label={t("audit.fAction", "액션 필터")} className={selCls}>
            <option value="all">{t("audit.aAll", "전체 액션")}</option>
            {actions.map((a) => <option key={a} value={a}>{a}</option>)}
          </select>
          <input
            value={actor}
            onChange={(e) => setActor(e.target.value)}
            placeholder={t("audit.fActor", "행위자 이메일")}
            autoComplete="off"
            className="w-44 rounded-md border border-border bg-surface px-2 py-1 text-xs"
          />
          <input
            value={kw}
            onChange={(e) => setKw(e.target.value)}
            placeholder={t("audit.fKw", "키워드 검색")}
            autoComplete="off"
            className="min-w-36 flex-1 rounded-md border border-border bg-surface px-2 py-1 text-xs"
          />
          <span className="text-[11px] tabular-nums text-muted">
            {t("audit.fCount", "{shown}/{total}건")
              .replace("{shown}", String(items.length))
              .replace("{total}", String(allItems.length))}
          </span>
        </div>

        {q.isLoading && <Spinner />}
        {q.error && <ErrorBox message={q.error instanceof Error ? q.error.message : String(q.error)} onRetry={() => q.refetch()} />}
        {q.data && !items.length && (
          <Empty>{allItems.length ? t("audit.fEmpty", "필터에 맞는 로그가 없습니다") : t("audit.empty", "감사 로그가 없습니다")}</Empty>
        )}

        {items.length > 0 && (
          <div className="overflow-x-auto rounded-lg border border-border bg-surface-hover/40">
            {/* 터미널 헤더 */}
            <div className="flex items-center gap-2 border-b border-border px-3 py-1.5 font-mono text-[10px] tracking-wider text-muted uppercase">
              <span className="h-2 w-2 rounded-full bg-err/70" />
              <span className="h-2 w-2 rounded-full bg-warn/70" />
              <span className="h-2 w-2 rounded-full bg-ok/70" />
              <span className="ml-1">{t("audit.terminalTitle", "audit.log — tail -f")}</span>
            </div>
            <div className="max-h-[32rem] overflow-y-auto px-1 py-1 font-mono text-[11px] leading-5">
              {items.map((a, i) => {
                const action = String(a.action || "-");
                const tone = actionTone(action);
                const detail = a.detail == null ? "" : maskSecrets(typeof a.detail === "string" ? a.detail : jsonSummary(a.detail, 200));
                return (
                  <div
                    key={String(a.id ?? `${a.ts}-${i}`)}
                    className="flex items-baseline gap-2 rounded px-2 py-0.5 hover:bg-surface-hover"
                  >
                    <span className="flex-none text-muted tabular-nums">{fmtTs(a.ts)}</span>
                    <Badge tone={tone}>{action}</Badge>
                    <span className="w-40 flex-none truncate text-ink-2" title={String(a.actor_email || "")}>
                      {a.actor_email || "-"}
                    </span>
                    <span className="min-w-0 flex-1 truncate text-muted" title={maskSecrets(String(a.target || ""))}>
                      {a.target || "-"}
                      {detail ? <span className="text-disabled"> · {detail}</span> : null}
                    </span>
                    {a.run_id ? (
                      <span className="flex-none text-[10px] text-disabled" title={String(a.run_id)}>
                        {String(a.run_id).slice(0, 8)}
                      </span>
                    ) : null}
                  </div>
                );
              })}
            </div>
          </div>
        )}

        {q.hasNextPage && (
          <div className="mt-3 flex justify-center">
            <Button
              variant="secondary"
              className="px-3 py-1 text-xs"
              disabled={q.isFetchingNextPage}
              onClick={() => q.fetchNextPage()}
            >
              {q.isFetchingNextPage
                ? t("audit.loadingMore", "불러오는 중…")
                : t("audit.more", "더 보기")}
            </Button>
          </div>
        )}
      </Card>
    </div>
  );
}

export default function AuditPage() {
  const role = useAppStore((s) => s.role);
  const { t } = useI18n();
  if (role != null && !roleAllows("admin", role)) {
    return (
      <div className="mx-auto max-w-6xl p-4">
        <PageHead title={t("audit.title", "감사 로그")} />
        <Empty>{t("audit.adminOnly", "감사 로그는 관리자(admin) 전용입니다.")}</Empty>
      </div>
    );
  }
  return <AuditBody />;
}
