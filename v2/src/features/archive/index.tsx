/** 아카이브 — /archive 검색(q+kind) 결과 리스트 + /export 내보내기. */
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { apiGet, apiPost, apiHeaders, ApiError } from "../../shared/api/client";
import type { ViewKey } from "../../shared/api/types";
import { useI18n, registerI18n } from "../../shared/i18n";
import { PageHead, Card, Empty, Spinner, ErrorBox, Button, Badge } from "../../shared/ui";
import { safeUrl } from "../../shared/lib/format";
import { useAppStore } from "../../shared/store/appStore";

interface ArchiveItem {
  kind?: string;
  id?: string;
  title?: string;
  snippet?: string;
  created_at?: string;
  link?: string;
}
interface ArchiveResponse { items?: ArchiveItem[]; query?: string }

registerI18n({
  ko: {
    "arch.title": "아카이브",
    "arch.desc": "리포트·실행·종료 세션 통합 검색",
    "arch.placeholder": "제목/내용 검색…",
    "arch.allKinds": "전체 종류",
    "arch.kindReport": "리포트",
    "arch.kindRun": "실행",
    "arch.kindSession": "세션",
    "arch.search": "검색",
    "arch.hint": "검색어를 입력하고 검색을 누르세요",
    "arch.empty": "검색 결과가 없습니다",
    "arch.noTitle": "(제목 없음)",
    "arch.exportTitle": "내보내기",
    "arch.expRuns": "실행",
    "arch.expSessions": "세션",
    "arch.expUsage": "사용량",
    "arch.expReports": "리포트",
    "arch.exporting": "내보내는 중…",
    "arch.download": "다운로드",
    "arch.exportFail": "내보내기 실패",
    "arch.share": "공유 링크",
    "arch.shareDesc": "공유 링크는 로그인 없이 열리는 읽기 전용 URL입니다 (30일 만료).",
    "arch.shareKind": "종류",
    "arch.shareRun": "실행",
    "arch.shareReport": "리포트",
    "arch.shareInsights": "인사이트",
    "arch.shareRefId": "참조 ID (선택 — run id 등)",
    "arch.shareRefPh": "비우면 최신 항목",
    "arch.shareCreate": "링크 생성",
    "arch.shareCreating": "생성 중…",
    "arch.shareCopy": "복사",
    "arch.shareCopied": "복사됨",
    "arch.shareOpen": "열기",
    "arch.shareExpires": "만료",
  },
  en: {
    "arch.title": "Archive",
    "arch.desc": "Unified search across reports, runs, and ended sessions",
    "arch.placeholder": "Search title/content…",
    "arch.allKinds": "All kinds",
    "arch.kindReport": "Report",
    "arch.kindRun": "Run",
    "arch.kindSession": "Session",
    "arch.search": "Search",
    "arch.hint": "Enter a query and press search",
    "arch.empty": "No results",
    "arch.noTitle": "(no title)",
    "arch.exportTitle": "Export",
    "arch.expRuns": "Runs",
    "arch.expSessions": "Sessions",
    "arch.expUsage": "Usage",
    "arch.expReports": "Reports",
    "arch.exporting": "Exporting…",
    "arch.download": "Download",
    "arch.exportFail": "Export failed",
    "arch.share": "Share link",
    "arch.shareDesc": "Share links are read-only URLs that open without login (expires in 30 days).",
    "arch.shareKind": "Kind",
    "arch.shareRun": "Run",
    "arch.shareReport": "Report",
    "arch.shareInsights": "Insights",
    "arch.shareRefId": "Reference ID (optional — run id etc.)",
    "arch.shareRefPh": "Empty = latest",
    "arch.shareCreate": "Create link",
    "arch.shareCreating": "Creating…",
    "arch.shareCopy": "Copy",
    "arch.shareCopied": "Copied",
    "arch.shareOpen": "Open",
    "arch.shareExpires": "Expires",
  },
});

const KIND_LABEL_KEY: Record<string, string> = {
  report: "arch.kindReport", run: "arch.kindRun", session: "arch.kindSession",
};
const KIND_LABEL_FALLBACK: Record<string, string> = {
  report: "리포트", run: "실행", session: "세션",
};
/** 백엔드 link 값 → 뷰 키 (레거시 VIEW_ALIASES: reports→inbox). */
const LINK_TO_VIEW: Record<string, ViewKey> = { reports: "inbox", inbox: "inbox", runs: "runs", office: "office", sessionlog: "sessionlog" };

function fmtTs(s: string | undefined): string {
  return (s || "").slice(0, 16).replace("T", " ");
}

/* ── 공유 링크 (POST /share) — 레거시 share-dialog 이행 ── */
const SHARE_KINDS = [
  { kind: "run", labelKey: "arch.shareRun", fallback: "실행" },
  { kind: "report", labelKey: "arch.shareReport", fallback: "리포트" },
  { kind: "insights", labelKey: "arch.shareInsights", fallback: "인사이트" },
] as const;

function archiveGo(link: string | undefined, setView: (v: ViewKey) => void) {
  const s = String(link || "").trim();
  if (!s) return;
  let v = "";
  const qi = s.indexOf("?");
  if (qi >= 0) v = new URLSearchParams(s.slice(qi + 1)).get("view") || "";
  if (!v) {
    const m = /(?:^|[/?&])view=([^&#]+)/.exec(s);
    if (m) v = decodeURIComponent(m[1]);
  }
  if (!v) v = s.replace(/^[/?]+/, "").split(/[?#]/)[0];
  const view = LINK_TO_VIEW[v] as ViewKey | undefined;
  if (view) { setView(view); return; }
  const su = safeUrl(s);
  if (su && su !== "#" && (/^https?:\/\//.test(su) || su.startsWith("/"))) {
    // 동일 출처만 현재 탭 — 외부 링크는 새 탭(noopener)으로 피싱/이탈 방지
    try {
      const u = new URL(su, window.location.origin);
      if (u.origin === window.location.origin) window.location.href = u.href;
      else window.open(u.href, "_blank", "noopener");
    } catch { /* 무시 */ }
  }
}

export default function ArchivePage() {
  const { t } = useI18n();
  const setView = useAppStore((s) => s.setView);
  const [q, setQ] = useState("");
  const [kind, setKind] = useState("");
  const [submitted, setSubmitted] = useState<{ q: string; kind: string } | null>(null);
  const [expKind, setExpKind] = useState("runs");
  const [expFormat, setExpFormat] = useState("csv");
  const [expDays, setExpDays] = useState("30");
  const [exporting, setExporting] = useState(false);
  const [shareKind, setShareKind] = useState<string>("report");
  const [shareRef, setShareRef] = useState("");
  const [shareBusy, setShareBusy] = useState(false);
  const [shareUrl, setShareUrl] = useState("");
  const [shareAbs, setShareAbs] = useState("");
  const [shareExp, setShareExp] = useState("");
  const [shareErr, setShareErr] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const sq = submitted ?? { q: "", kind: "" };
  const { data, isLoading, error, refetch, isFetching } = useQuery({
    queryKey: ["archive", sq.q, sq.kind],
    queryFn: () =>
      apiGet<ArchiveResponse>(
        `/archive?q=${encodeURIComponent(sq.q)}&kind=${encodeURIComponent(sq.kind)}&limit=50`,
      ),
    enabled: submitted !== null,
  });

  const items = data?.items || [];

  const doExport = async () => {
    setExporting(true);
    try {
      const qs = `kind=${encodeURIComponent(expKind)}&format=${encodeURIComponent(expFormat)}&days=${encodeURIComponent(expDays)}`;
      const r = await fetch(`/api/v1/export?${qs}`, { headers: apiHeaders() });
      if (!r.ok) {
        const j = (await r.json().catch(() => ({}))) as { error?: { message?: string } };
        alert(`${t("arch.exportFail", "내보내기 실패")}: ${j.error?.message || r.status}`);
        return;
      }
      const blob = await r.blob();
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `jobmate-${expKind}-${expDays}d.${expFormat === "json" ? "json" : "csv"}`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 5000);
    } catch (e) {
      alert(e instanceof Error ? e.message : String(e));
    } finally {
      setExporting(false);
    }
  };

  const createShare = async () => {
    setShareBusy(true);
    setShareErr(null);
    setShareUrl("");
    setShareAbs("");
    setCopied(false);
    try {
      const body: { kind: string; ref_id?: string } = { kind: shareKind };
      const ref = shareRef.trim();
      if (ref) body.ref_id = ref;
      const d = await apiPost<{ url?: string; expires_at?: string }>("/share", body);
      const path = String(d.url || "");
      // 레거시와 동일 — /share/<token> 상대경로를 절대 URL로 표시
      const abs = path.startsWith("http") ? path : window.location.origin + path;
      setShareUrl(path);
      setShareAbs(abs);
      setShareExp(d.expires_at || "");
    } catch (e) {
      setShareErr(e instanceof ApiError ? e.message : String(e));
    } finally {
      setShareBusy(false);
    }
  };

  const copyShare = async () => {
    try {
      await navigator.clipboard.writeText(shareAbs);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      setCopied(false);
    }
  };

  return (
    <div className="mx-auto max-w-3xl p-4">
      <PageHead title={t("arch.title", "아카이브")} desc={t("arch.desc", "리포트·실행·종료 세션 통합 검색")} />

      <Card className="mb-3">
        <form
          className="flex flex-wrap items-center gap-2"
          onSubmit={(e) => { e.preventDefault(); setSubmitted({ q: q.trim(), kind }); }}
        >
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder={t("arch.placeholder", "제목/내용 검색…")}
            className="min-w-48 flex-1 rounded-md border border-border bg-surface px-2 py-1.5 text-sm"
          />
          <select value={kind} onChange={(e) => setKind(e.target.value)} className="rounded-md border border-border bg-surface px-2 py-1.5 text-sm">
            <option value="">{t("arch.allKinds", "전체 종류")}</option>
            <option value="report">{t("arch.kindReport", "리포트")}</option>
            <option value="run">{t("arch.kindRun", "실행")}</option>
            <option value="session">{t("arch.kindSession", "세션")}</option>
          </select>
          <Button type="submit" className="px-3 py-1 text-xs">{t("arch.search", "검색")}</Button>
          {submitted && (
            <Button type="button" variant="secondary" className="px-3 py-1 text-xs" onClick={() => { void refetch(); }} disabled={isFetching}>
              {t("common.refresh", "새로고침")}
            </Button>
          )}
        </form>
      </Card>

      {submitted === null && <Empty>{t("arch.hint", "검색어를 입력하고 검색을 누르세요")}</Empty>}
      {submitted !== null && isLoading && <Spinner />}
      {submitted !== null && error && (
        <ErrorBox message={error instanceof Error ? error.message : String(error)} onRetry={() => refetch()} />
      )}
      {submitted !== null && data && !items.length && <Empty>{t("arch.empty", "검색 결과가 없습니다")}</Empty>}

      {items.map((it, i) => (
        <button
          key={`${it.kind}:${it.id}:${i}`}
          onClick={() => archiveGo(it.link, setView)}
          className="mb-2 block w-full rounded-xl border border-border bg-surface p-4 text-left shadow-sm transition-colors hover:bg-surface-hover"
        >
          <div className="mb-1 flex items-center gap-2">
            <Badge tone={it.kind === "run" ? "accent" : it.kind === "session" ? "warn" : "muted"}>
              {it.kind && KIND_LABEL_KEY[it.kind]
                ? t(KIND_LABEL_KEY[it.kind], KIND_LABEL_FALLBACK[it.kind] || it.kind)
                : it.kind || "-"}
            </Badge>
            <span className="ml-auto text-xs text-muted">{fmtTs(it.created_at)}</span>
          </div>
          <div className="text-sm font-medium text-ink">{it.title || t("arch.noTitle", "(제목 없음)")}</div>
          {it.snippet && <div className="mt-1 line-clamp-2 text-xs text-muted">{it.snippet}</div>}
        </button>
      ))}

      <Card title={t("arch.exportTitle", "내보내기")} className="mt-4">
        <div className="flex flex-wrap items-center gap-2">
          <select value={expKind} onChange={(e) => setExpKind(e.target.value)} className="rounded-md border border-border bg-surface px-2 py-1.5 text-sm">
            <option value="runs">{t("arch.expRuns", "실행")}</option>
            <option value="sessions">{t("arch.expSessions", "세션")}</option>
            <option value="usage">{t("arch.expUsage", "사용량")}</option>
            <option value="reports">{t("arch.expReports", "리포트")}</option>
          </select>
          <select value={expFormat} onChange={(e) => setExpFormat(e.target.value)} className="rounded-md border border-border bg-surface px-2 py-1.5 text-sm">
            <option value="csv">CSV</option>
            <option value="json">JSON</option>
          </select>
          <select value={expDays} onChange={(e) => setExpDays(e.target.value)} className="rounded-md border border-border bg-surface px-2 py-1.5 text-sm">
            {["7", "30", "90", "365"].map((x) => <option key={x} value={x}>{t(`common.last${x}`, `최근 ${x}일`)}</option>)}
          </select>
          <Button className="px-3 py-1 text-xs" disabled={exporting} onClick={doExport}>
            {exporting ? t("arch.exporting", "내보내는 중…") : t("arch.download", "다운로드")}
          </Button>
        </div>
      </Card>

      <Card title={t("arch.share", "공유 링크")} className="mt-4">
        <div className="mb-2 text-xs text-muted">{t("arch.shareDesc", "공유 링크는 로그인 없이 열리는 읽기 전용 URL입니다 (30일 만료).")}</div>
        <div className="flex flex-wrap items-center gap-2">
          <select
            value={shareKind}
            onChange={(e) => setShareKind(e.target.value)}
            aria-label={t("arch.shareKind", "종류")}
            className="rounded-md border border-border bg-surface px-2 py-1.5 text-sm"
          >
            {SHARE_KINDS.map((k) => (
              <option key={k.kind} value={k.kind}>{t(k.labelKey, k.fallback)}</option>
            ))}
          </select>
          <input
            value={shareRef}
            onChange={(e) => setShareRef(e.target.value)}
            placeholder={t("arch.shareRefPh", "비우면 최신 항목")}
            aria-label={t("arch.shareRefId", "참조 ID (선택 — run id 등)")}
            autoComplete="off"
            className="min-w-40 flex-1 rounded-md border border-border bg-surface px-2 py-1.5 text-sm"
          />
          <Button className="px-3 py-1 text-xs" disabled={shareBusy} onClick={createShare}>
            {shareBusy ? t("arch.shareCreating", "생성 중…") : t("arch.shareCreate", "링크 생성")}
          </Button>
        </div>
        {shareErr && <div className="mt-2 text-xs text-err">{shareErr}</div>}
        {shareUrl && (
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <input
              readOnly
              value={shareAbs}
              onClick={(e) => (e.target as HTMLInputElement).select()}
              className="min-w-48 flex-1 rounded-md border border-border bg-surface px-2 py-1.5 font-mono text-xs"
            />
            <Button variant="secondary" className="px-3 py-1 text-xs" onClick={copyShare}>
              {copied ? t("arch.shareCopied", "복사됨") : t("arch.shareCopy", "복사")}
            </Button>
            <Button
              variant="secondary"
              className="px-3 py-1 text-xs"
              onClick={() => {
                const su = safeUrl(shareUrl);
                if (su) window.open(su.startsWith("http") ? su : window.location.origin + su, "_blank", "noopener");
              }}
            >
              {t("arch.shareOpen", "열기")}
            </Button>
            {shareExp && (
              <span className="text-xs text-muted">
                {t("arch.shareExpires", "만료")} {fmtTs(shareExp)}
              </span>
            )}
          </div>
        )}
      </Card>
    </div>
  );
}
