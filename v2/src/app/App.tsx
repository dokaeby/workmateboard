import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { apiGet, ApiError } from "../shared/api/client";
import type { Me, Prefs, ViewKey } from "../shared/api/types";
import { useAppStore, roleAllows } from "../shared/store/appStore";
import { useI18n, setLang, getLang, type Lang } from "../shared/i18n";
import { NAV_ITEMS, visibleNavItems } from "./nav";
import { ViewLoader } from "./views";
import { useSseInvalidation } from "../shared/stream";

async function fetchMe(): Promise<Me | null> {
  try {
    // /auth/me 응답은 {user: {...}} 래퍼
    const d = await apiGet<{ user?: Me }>("/auth/me");
    return d.user ?? null;
  } catch (e) {
    // 401(미인증)만 null — 네트워크 오류 등은 쿼리 에러로 올려 로그인 화면 오표시 방지
    if (e instanceof ApiError && e.status === 401) return null;
    throw e;
  }
}
async function fetchPrefs(): Promise<Prefs> {
  try {
    return await apiGet<Prefs>("/settings");
  } catch {
    return {};
  }
}

function LangToggle() {
  const { lang, t } = useI18n();
  return (
    <button
      className="rounded-md border border-border px-2 py-1 text-xs text-muted hover:bg-surface-hover"
      onClick={() => setLang(lang === "ko" ? "en" : "ko")}
      aria-label={t("common.language", "Language")}
    >
      {lang === "ko" ? "EN" : "KO"}
    </button>
  );
}

function Sidebar({ onNavigate }: { onNavigate?: () => void }) {
  const { view, setView, role, prefs, sidebarOpen, setSidebarOpen } = useAppStore();
  const items = visibleNavItems(role, prefs);
  const { t } = useI18n();
  // 앱 뱃지 카운트 = 미읽음 인박스 + 승인 대기 run (레거시 /badge/count, 30초 폴링)
  const { data: badge } = useQuery({
    queryKey: ["badge-count"],
    queryFn: () => apiGet<{ count?: number }>("/badge/count"),
    refetchInterval: 30000,
    retry: false,
    enabled: roleAllows("lead", role),
  });
  const badgeCount = badge?.count ?? 0;
  return (
    <>
      {sidebarOpen && (
        <div className="fixed inset-0 z-30 bg-black/30 lg:hidden" onClick={() => setSidebarOpen(false)} />
      )}
      <aside
        className={`fixed inset-y-0 left-0 z-40 flex w-56 flex-col border-r border-border bg-surface transition-transform lg:static lg:translate-x-0 ${
          sidebarOpen ? "translate-x-0" : "-translate-x-full"
        }`}
      >
        <div className="flex h-14 items-center gap-2 border-b border-border px-4">
          <span className="text-lg font-bold text-accent">jobMate</span>
          <span className="text-xs text-muted">{t("app.tagline", "Agent Office")}</span>
          <div className="ml-auto"><LangToggle /></div>
        </div>
        <nav className="flex-1 overflow-y-auto p-2">
          {items.map((it) => (
            <button
              key={it.view}
              data-view={it.view}
              onClick={() => { setView(it.view); onNavigate?.(); }}
              className={`mb-0.5 flex w-full items-center gap-2 rounded-lg px-3 py-2 text-sm transition-colors ${
                view === it.view ? "bg-accent-soft font-semibold text-accent" : "text-ink-2 hover:bg-surface-hover"
              }`}
            >
              <span className="w-5 text-center">{it.icon}</span>
              {it.label}
              {it.view === "approvals" && badgeCount > 0 && (
                <span className="ml-auto rounded-full bg-accent px-1.5 py-0.5 text-[10px] font-bold leading-none text-white">
                  {badgeCount > 99 ? "99+" : badgeCount}
                </span>
              )}
            </button>
          ))}
        </nav>
        <div className="border-t border-border p-3 text-xs text-muted">
          <a href="/static/privacy.html" target="_blank" rel="noopener" className="hover:text-accent">개인정보처리방침</a>
          {" · "}
          <a href="/static/terms.html" target="_blank" rel="noopener" className="hover:text-accent">이용약관</a>
        </div>
      </aside>
    </>
  );
}

function Header() {
  const { toggleSidebar } = useAppStore();
  return (
    <header className="sticky top-0 z-20 flex h-14 items-center gap-2 border-b border-border bg-surface/90 px-3 backdrop-blur lg:hidden">
      <button className="rounded-md p-2 hover:bg-surface-hover" onClick={toggleSidebar} aria-label="menu">☰</button>
      <span className="font-bold text-accent">jobMate</span>
      <div className="ml-auto"><LangToggle /></div>
    </header>
  );
}

/** 모바일 하단바 — 승인 대기/오늘 실행/활성 세션/월 비용 요약. */
function MobileBar() {
  const { setView } = useAppStore();
  const { t } = useI18n();
  const { data: appr } = useQuery({
    queryKey: ["approvals"],
    queryFn: () => apiGet<{ items: unknown[] }>("/approvals"),
    refetchInterval: 60000,
    retry: false,
  });
  const { data: usage } = useQuery({
    queryKey: ["usage-summary"],
    queryFn: () => apiGet<{ total_cost_usd?: number }>("/usage/summary"),
    refetchInterval: 60000,
    retry: false,
  });
  const { data: sessions } = useQuery({
    queryKey: ["agent-sessions", "active"],
    queryFn: () => apiGet<unknown[]>("/agent-sessions?limit=100"),
    refetchInterval: 60000,
    retry: false,
  });
  const { data: runs } = useQuery({
    queryKey: ["runs", "today"],
    queryFn: () => apiGet<{ items?: unknown[] } | unknown[]>("/runs?limit=100"),
    refetchInterval: 60000,
    retry: false,
  });
  const apprCount = Array.isArray(appr?.items) ? appr.items.length : 0;
  const activeSessions = Array.isArray(sessions)
    ? sessions.filter((s) => (s as { status?: string }).status !== "ended").length : 0;
  const runList = Array.isArray(runs) ? runs : (runs as { items?: unknown[] })?.items ?? [];
  const todayCount = runList.filter((r) => {
    const at = (r as { created_at?: string }).created_at;
    return at && new Date(at).toDateString() === new Date().toDateString();
  }).length;
  const cost = usage?.total_cost_usd ?? 0;

  const items: { view: Parameters<typeof setView>[0]; label: string; value: string | number }[] = [
    { view: "approvals", label: t("mob.approvals", "승인 대기"), value: apprCount },
    { view: "runs", label: t("mob.runsToday", "오늘 실행"), value: todayCount },
    { view: "office", label: t("mob.sessions", "활성 세션"), value: activeSessions },
    { view: "cost", label: t("mob.cost", "월 비용"), value: `$${cost.toFixed(2)}` },
  ];
  return (
    <nav className="fixed inset-x-0 bottom-0 z-30 grid grid-cols-4 border-t border-border bg-surface/95 backdrop-blur lg:hidden">
      {items.map((it) => (
        <button
          key={it.view + it.label}
          onClick={() => {
            if (it.view === "approvals" && !roleAllows("lead", useAppStore.getState().role)) {
              alert("승인 대기열은 리드 이상 권한이 필요합니다");
              return;
            }
            setView(it.view);
          }}
          className="flex flex-col items-center py-2 text-[11px] text-muted"
        >
          <b className="text-sm text-ink">{it.value}</b>
          <span>{it.label}</span>
        </button>
      ))}
    </nav>
  );
}

/** 미인증 화면 — Google 로그인 + API 키 입력 (레거시 index.html 인증 오버레이 이식). */
function LoginOverlay({ authError }: { authError?: string | null }) {
  const { t } = useI18n();
  const [key, setKey] = useState("");
  const [keyErr, setKeyErr] = useState("");
  const [busy, setBusy] = useState(false);

  const submitKey = async (e: React.FormEvent) => {
    e.preventDefault();
    const k = key.trim();
    if (!k || busy) return;
    setBusy(true);
    setKeyErr("");
    localStorage.setItem("jobmate_api_key", k);
    try {
      // 키 검증 — /auth/me가 200이면 reload로 앱 진입
      await apiGet<{ user?: unknown }>("/auth/me");
      window.location.reload();
    } catch {
      localStorage.removeItem("jobmate_api_key");
      setKeyErr(t("auth.keyFail", "API 키가 올바르지 않습니다"));
      setBusy(false);
    }
  };

  return (
    <div className="flex h-dvh items-center justify-center bg-bg p-4">
      <div className="w-full max-w-sm rounded-2xl border border-border bg-surface p-8 text-center shadow-sm">
        <div className="mb-1 text-xl font-bold text-accent">jobMate</div>
        <p className="mb-6 text-sm text-muted">
          {t("auth.required", "로그인이 필요합니다 — 세션이 만료됐거나 로그인이 필요해요.")}
        </p>
        {authError && (
          <p className="mb-4 rounded-lg border border-err-border bg-err-soft px-3 py-2 text-xs text-err">
            {t(`auth.err.${authError}`, authError)}
          </p>
        )}
        <a
          href="/api/v1/auth/google"
          className="mb-4 block rounded-[10px] border border-border-strong bg-surface px-4 py-2.5 font-semibold text-ink no-underline transition-colors hover:bg-surface-hover"
        >
          {t("auth.google", "Google로 계속하기")}
        </a>
        <form onSubmit={submitKey} className="text-left">
          <label className="mb-1 block text-xs font-medium text-muted" htmlFor="jm-api-key">
            {t("auth.apiKey", "API 키")}
          </label>
          <div className="flex gap-2">
            <input
              id="jm-api-key"
              type="password"
              value={key}
              onChange={(e) => setKey(e.target.value)}
              placeholder={t("auth.apiKeyPh", "JOBMATE_API_KEY")}
              autoComplete="off"
              className="min-w-0 flex-1 rounded-[10px] border border-border bg-bg px-3 py-2 text-sm text-ink outline-none focus:border-accent"
            />
            <button
              type="submit"
              disabled={busy || !key.trim()}
              className="shrink-0 rounded-[10px] border border-border-strong bg-surface px-3 py-2 text-sm font-semibold text-ink transition-colors hover:bg-surface-hover disabled:opacity-50"
            >
              {t("auth.login", "로그인")}
            </button>
          </div>
          {keyErr && <p className="mt-2 text-xs text-err">{keyErr}</p>}
        </form>
      </div>
    </div>
  );
}

export default function App() {
  const { view, setView, role, setRole, setPrefs } = useAppStore();
  const [unauth, setUnauth] = useState(false);
  const [authError, setAuthError] = useState<string | null>(null);

  // 딥링크 파라미터 — /?view=X 리다이렉트·?auth_error= OAuth 실패를 복원
  useEffect(() => {
    const q = new URLSearchParams(window.location.search);
    const v = q.get("view");
    if (v && NAV_ITEMS.some((n) => n.view === v)) setView(v as ViewKey);
    setAuthError(q.get("auth_error"));
  }, [setView]);

  const me = useQuery({ queryKey: ["me"], queryFn: fetchMe, staleTime: 60000 });
  const settings = useQuery({ queryKey: ["settings"], queryFn: fetchPrefs, staleTime: 60000 });

  useEffect(() => {
    if (me.data !== undefined) setRole(me.data?.role ?? "member");
  }, [me.data, setRole]);
  useEffect(() => {
    if (settings.data) setPrefs(settings.data);
  }, [settings.data, setPrefs]);

  // role 확정 후 현재 뷰가 권한 밖이면 office로 복귀
  useEffect(() => {
    if (role == null) return;
    const item = NAV_ITEMS.find((n) => n.view === view);
    if (item?.minRole && !roleAllows(item.minRole, role)) setView("office");
  }, [role, view, setView]);

  // SSE 실시간 스트림 — run/stage/session 이벤트 시 관련 쿼리 무효화.
  // 미인증/로그아웃 상태에서는 연결하지 않는다 (401 재시도 루프 방지).
  useSseInvalidation(!unauth && me.data != null);

  // 401 이벤트 → role 초기화 + 로그인 화면 유도
  useEffect(() => {
    const onUnauth = () => { setRole(null); setUnauth(true); };
    window.addEventListener("jm:unauthorized", onUnauth);
    return () => window.removeEventListener("jm:unauthorized", onUnauth);
  }, [setRole]);

  const lang = getLang() as Lang;
  void lang;

  // /auth/me가 401로 null을 반환했거나 세션 만료 이벤트가 오면 로그인 화면
  if (unauth || (me.isFetched && me.data === null)) return <LoginOverlay authError={authError} />;

  return (
    <div className="flex h-dvh flex-col">
      <Header />
      <div className="flex min-h-0 flex-1">
        <Sidebar />
        <main className="min-w-0 flex-1 overflow-y-auto pb-16 lg:pb-0">
          <ViewLoader view={view} />
        </main>
      </div>
      <MobileBar />
    </div>
  );
}
