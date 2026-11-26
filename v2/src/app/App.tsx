import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { apiGet, apiPost, ApiError } from "../shared/api/client";
import type { Me, Prefs, ViewKey } from "../shared/api/types";
import { useAppStore, roleAllows } from "../shared/store/appStore";
import { useI18n, setLang, getLang, registerI18n, type Lang } from "../shared/i18n";
import { NAV_ITEMS, visibleNavItems } from "./nav";
import { STR as APP_I18N } from "./i18n";
import { ViewLoader } from "./views";
import { useSseInvalidation } from "../shared/stream";

registerI18n(APP_I18N, { core: true });

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
              {t(`nav.${it.view}`, it.label)}
              {it.view === "approvals" && badgeCount > 0 && (
                <span className="ml-auto rounded-full bg-accent px-1.5 py-0.5 text-[10px] font-bold leading-none text-white">
                  {badgeCount > 99 ? "99+" : badgeCount}
                </span>
              )}
            </button>
          ))}
        </nav>
        <div className="border-t border-border p-3 text-xs text-muted">
          <a href="/static/privacy.html" target="_blank" rel="noopener" className="hover:text-accent">{t("app.privacy", "개인정보처리방침")}</a>
          {" · "}
          <a href="/static/terms.html" target="_blank" rel="noopener" className="hover:text-accent">{t("app.terms", "이용약관")}</a>
        </div>
      </aside>
    </>
  );
}

function urlBase64ToUint8Array(base64: string): Uint8Array<ArrayBuffer> {
  const padding = "=".repeat((4 - (base64.length % 4)) % 4);
  const b64 = (base64 + padding).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(b64);
  const out = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out;
}

/** 헤더 푸시 토글 — 레거시 #push-toggle 이식 (PushSection과 동일 플로우). */
function PushButton() {
  const { t } = useI18n();
  const [on, setOn] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let dead = false;
    (async () => {
      if (!("serviceWorker" in navigator) || !("PushManager" in window)) {
        if (!dead) setOn(false);
        return;
      }
      try {
        const reg = await navigator.serviceWorker.getRegistration("/");
        const sub = reg ? await reg.pushManager.getSubscription() : null;
        if (!dead) setOn(!!sub);
      } catch { if (!dead) setOn(false); }
    })();
    return () => { dead = true; };
  }, []);

  const toggle = async () => {
    if (busy) return;
    setBusy(true);
    try {
      if (on) {
        const reg = await navigator.serviceWorker.getRegistration("/");
        const sub = reg ? await reg.pushManager.getSubscription() : null;
        if (sub) {
          try { await apiPost("/push/unsubscribe", { endpoint: sub.endpoint }); }
          finally { await sub.unsubscribe(); }
        }
        setOn(false);
      } else {
        const reg = await navigator.serviceWorker.register("/sw.js");
        const { public_key } = await apiGet<{ public_key?: string }>("/push/vapid-key");
        if (!public_key) { alert(t("hdr.pushNoKey", "서버에 VAPID 키가 없습니다")); return; }
        const sub = await reg.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: urlBase64ToUint8Array(public_key),
        });
        await apiPost("/push/subscribe", sub.toJSON());
        setOn(true);
      }
    } catch (e) {
      alert(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <button
      className={`rounded-md p-2 transition-colors hover:bg-surface-hover ${on ? "text-accent" : "text-muted"}`}
      onClick={toggle}
      disabled={busy}
      title={t("hdr.push", "알림")}
      aria-label={t("hdr.push", "알림")}
    >
      <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/></svg>
    </button>
  );
}

function LogoutButton() {
  const { t } = useI18n();
  const logout = async () => {
    try { await apiPost("/auth/logout"); } catch { /* 이미 폐기됨 */ }
    try { localStorage.removeItem("jobmate_api_key"); } catch { /* noop */ }
    window.location.href = "/";
  };
  return (
    <button
      className="flex items-center gap-1 rounded-md px-2 py-1.5 text-muted transition-colors hover:bg-surface-hover"
      onClick={logout}
      title={t("hdr.logout", "로그아웃")}
      aria-label={t("hdr.logout", "로그아웃")}
    >
      {/* 로그인(화살표+문)과 구분 — 출구 아이콘 + 텍스트 라벨 병기 */}
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></svg>
      <span className="text-xs font-medium">{t("hdr.logout", "로그아웃")}</span>
    </button>
  );
}

/** 데스크톱 상단바 액션 — 레거시 .header-actions (푸시/설정/로그아웃) 이식. */
function HeaderActions() {
  const { t } = useI18n();
  const { setView } = useAppStore();
  return (
    <>
      <PushButton />
      <button
        className="rounded-md p-2 text-muted transition-colors hover:bg-surface-hover"
        onClick={() => setView("settings")}
        title={t("hdr.settings", "설정")}
        aria-label={t("hdr.settings", "설정")}
      >
        <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33h.01a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51h.01a1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82v.01a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
      </button>
      <LogoutButton />
    </>
  );
}

/** 모바일 상단바 — 레거시 app-header의 모바일 대응 (☰ + 브랜드 + 액션 아이콘). */
function Header() {
  const { toggleSidebar } = useAppStore();
  return (
    <header className="sticky top-0 z-20 flex h-14 items-center gap-2 border-b border-border bg-surface/90 px-3 backdrop-blur lg:hidden">
      <button className="rounded-md p-2 hover:bg-surface-hover" onClick={toggleSidebar} aria-label="menu">☰</button>
      <span className="font-bold text-accent">jobMate</span>
      <div className="ml-auto flex items-center gap-0.5"><LangToggle /><HeaderActions /></div>
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
              alert(t("app.approvalLeadOnly", "승인 대기열은 리드 이상 권한이 필요합니다"));
              return;
            }
            setView(it.view);
          }}
          className="flex flex-col items-center py-2 text-[11px] text-muted"
        >
          <b className="text-sm text-ink">{it.value}</b>
          <span>{t(`nav.${it.view}`, it.label)}</span>
        </button>
      ))}
    </nav>
  );
}

/** 미인증 화면 — Google OAuth + 이메일 회원가입/로그인 (세션 쿠키 인증).
 *  X로 닫으면 앱 셸은 유지하고 하단 배너로 재로그인을 유도한다(레거시 auth-close 패리티). */
function LoginOverlay({ authError, onClose }: { authError?: string | null; onClose: () => void }) {
  const { t } = useI18n();
  const [mode, setMode] = useState<"login" | "signup">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [name, setName] = useState("");
  const [agree, setAgree] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [notice, setNotice] = useState("");
  const [needVerify, setNeedVerify] = useState(false);
  const [googleEnabled, setGoogleEnabled] = useState(false);

  useEffect(() => {
    apiGet<{ google_enabled?: boolean }>("/auth/config")
      .then((d) => setGoogleEnabled(d.google_enabled === true))
      .catch(() => setGoogleEnabled(false));
  }, []);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (busy || !email.trim() || !password) return;
    setBusy(true);
    setErr("");
    setNotice("");
    setNeedVerify(false);
    try {
      if (mode === "login") {
        await apiPost("/auth/login", { email: email.trim(), password });
        window.location.reload();
        return;
      }
      const res = await apiPost<{ email_verification_pending?: boolean; verify_email_sent?: boolean }>(
        "/auth/signup",
        { email: email.trim(), password, display_name: name.trim(), agree_terms: agree },
      );
      if (res?.email_verification_pending) {
        // 세션 미발급 — 인증 메일 확인 후 로그인해야 한다.
        // 메일 발송 실패(verify_email_sent=false)면 재발송 경로를 바로 연다.
        setNeedVerify(true);
        setNotice(
          res.verify_email_sent === false
            ? t("auth.verifySendFail", "인증 메일 발송에 실패했습니다 — 아래 버튼으로 재발송하세요")
            : t("auth.verifySent", "인증 메일을 보냈습니다 — 메일의 링크를 클릭한 뒤 로그인하세요"),
        );
        setMode("login");
      } else {
        window.location.reload();
      }
    } catch (e2) {
      if (e2 instanceof ApiError && e2.code === "email_not_verified") setNeedVerify(true);
      setErr(e2 instanceof Error ? e2.message : String(e2));
    } finally {
      setBusy(false);
    }
  };

  const resendVerify = async () => {
    try {
      const res = await apiPost<{ sent?: boolean }>("/auth/verify/resend", { email: email.trim() });
      setNotice(
        res?.sent === false
          ? t("auth.resendNone", "발송되지 않았습니다 — 이미 인증됐거나 가입된 이메일이 아닐 수 있습니다")
          : t("auth.resendDone", "인증 메일을 재발송했습니다"),
      );
    } catch (e2) {
      setErr(e2 instanceof Error ? e2.message : String(e2));
    }
  };

  const inputCls =
    "w-full rounded-[10px] border border-border bg-bg px-3 py-2 text-sm text-ink outline-none focus:border-accent";

  return (
    <div className="flex h-dvh items-center justify-center bg-bg p-4">
      <div className="relative w-full max-w-sm rounded-2xl border border-border bg-surface p-8 text-center shadow-sm">
        <button
          type="button"
          onClick={onClose}
          title={t("auth.close", "닫기")}
          aria-label={t("auth.close", "닫기")}
          className="absolute right-3 top-3 border-none bg-transparent px-1.5 text-lg leading-none text-muted hover:text-ink"
        >
          ✕
        </button>
        <div className="mb-1 text-xl font-bold text-accent">jobMate</div>
        <p className="mb-6 text-sm text-muted">
          {t("auth.required", "로그인이 필요합니다 — 세션이 만료됐거나 로그인이 필요해요.")}
        </p>
        {authError && (
          <p className="mb-4 rounded-lg border border-err-border bg-err-soft px-3 py-2 text-xs text-err">
            {t(`auth.err.${authError}`, authError)}
          </p>
        )}
        {googleEnabled && (
          <a
            href="/api/v1/auth/google"
            className="mb-4 block rounded-[10px] border border-border-strong bg-surface px-4 py-2.5 font-semibold text-ink no-underline transition-colors hover:bg-surface-hover"
          >
            {t("auth.google", "Google로 로그인")}
          </a>
        )}
        <form onSubmit={submit} className="text-left">
          {mode === "signup" && (
            <>
              <label className="mb-1 block text-xs font-medium text-muted" htmlFor="jm-name">
                {t("auth.displayName", "표시 이름 (선택)")}
              </label>
              <input
                id="jm-name"
                type="text"
                value={name}
                onChange={(e) => setName(e.target.value)}
                autoComplete="nickname"
                className={inputCls + " mb-3"}
              />
            </>
          )}
          <label className="mb-1 block text-xs font-medium text-muted" htmlFor="jm-email">
            {t("auth.email", "이메일")}
          </label>
          <input
            id="jm-email"
            type="email"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            autoComplete="email"
            className={inputCls + " mb-3"}
          />
          <label className="mb-1 block text-xs font-medium text-muted" htmlFor="jm-password">
            {t("auth.password", "비밀번호")}
          </label>
          <input
            id="jm-password"
            type="password"
            required
            minLength={mode === "signup" ? 8 : undefined}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete={mode === "signup" ? "new-password" : "current-password"}
            className={inputCls + " mb-3"}
          />
          {mode === "signup" && (
            <label className="mb-3 flex items-start gap-2 text-xs text-muted">
              <input
                type="checkbox"
                checked={agree}
                onChange={(e) => setAgree(e.target.checked)}
                className="mt-0.5"
              />
              <span>{t("auth.agreeTerms", "이용약관 및 개인정보처리방침에 동의합니다")}</span>
            </label>
          )}
          {err && <p className="mb-2 text-xs text-err">{err}</p>}
          {notice && <p className="mb-2 text-xs text-accent">{notice}</p>}
          {needVerify && (
            <button
              type="button"
              onClick={resendVerify}
              className="mb-2 text-xs font-medium text-accent underline"
            >
              {t("auth.resendVerify", "인증 메일 재발송")}
            </button>
          )}
          <button
            type="submit"
            disabled={busy || !email.trim() || !password || (mode === "signup" && !agree)}
            className="w-full rounded-[10px] border border-border-strong bg-accent px-4 py-2.5 font-semibold text-white transition-opacity hover:opacity-90 disabled:opacity-50"
          >
            {busy
              ? t("common.loading", "로딩…")
              : mode === "login"
                ? t("auth.login", "로그인")
                : t("auth.signup", "회원가입")}
          </button>
        </form>
        <button
          type="button"
          onClick={() => { setMode(mode === "login" ? "signup" : "login"); setErr(""); setNotice(""); setNeedVerify(false); }}
          className="mt-4 text-xs text-muted underline"
        >
          {mode === "login"
            ? t("auth.toSignup", "계정이 없나요? 회원가입")
            : t("auth.toLogin", "이미 계정이 있나요? 로그인")}
        </button>
      </div>
    </div>
  );
}

export default function App() {
  const { view, setView, role, setRole, setPrefs } = useAppStore();
  const [unauth, setUnauth] = useState(false);
  // 로그인 오버레이를 X로 닫은 상태 — 앱 셸은 유지하고 배너로 재유도
  const [authDismissed, setAuthDismissed] = useState(false);
  // ?auth_error= OAuth 실패 코드는 마운트 시 1회만 읽고 URL에서 제거한다 —
  // 남겨두면 이후 무관한 로그인 화면에 같은 오류가 반복 표시된다.
  const [authError] = useState<string | null>(() => {
    const q = new URLSearchParams(window.location.search);
    const code = q.get("auth_error");
    if (code) {
      q.delete("auth_error");
      const qs = q.toString();
      window.history.replaceState(null, "", window.location.pathname + (qs ? `?${qs}` : ""));
    }
    return code;
  });

  // 딥링크 파라미터 — /?view=X 리다이렉트 복원
  useEffect(() => {
    const q = new URLSearchParams(window.location.search);
    const v = q.get("view");
    if (v && NAV_ITEMS.some((n) => n.view === v)) setView(v as ViewKey);
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
  const { t } = useI18n();

  // /auth/me가 401로 null을 반환했거나 세션 만료 이벤트가 오면 로그인 화면
  // (X로 닫은 authDismissed 상태에서는 오버레이 대신 하단 배너만 표시)
  const needsAuth = unauth || (me.isFetched && me.data === null);
  if (needsAuth && !authDismissed) {
    return <LoginOverlay authError={authError} onClose={() => setAuthDismissed(true)} />;
  }

  return (
    <div className="flex h-dvh flex-col">
      <Header />
      <div className="flex min-h-0 flex-1">
        <Sidebar />
        <div className="flex min-w-0 flex-1 flex-col">
          {/* 데스크톱 상단바 — 레거시 .app-header (푸시/설정/로그아웃) */}
          <header className="sticky top-0 z-20 hidden h-14 items-center gap-1 border-b border-border bg-surface/90 px-4 backdrop-blur lg:flex">
            <span className="text-sm font-semibold text-ink">{t("app.tagline", "Agent Office")}</span>
            <div className="ml-auto flex items-center gap-0.5"><LangToggle /><HeaderActions /></div>
          </header>
          <main className="min-w-0 flex-1 overflow-y-auto pb-16 lg:pb-0">
            <ViewLoader view={view} />
          </main>
        </div>
      </div>
      <MobileBar />
      {/* 로그인 오버레이를 닫은 상태 — 하단 배너로 재로그인 유도 (레거시 #auth-needed 패리티) */}
      {needsAuth && authDismissed && (
        <div className="fixed inset-x-0 bottom-0 z-50 flex items-center justify-center gap-3 border-t border-err-border bg-err-soft px-4 py-2.5 text-sm text-err">
          <span>{t("auth.banner", "로그인이 필요합니다 — 일부 기능을 사용할 수 없습니다")}</span>
          <button
            type="button"
            onClick={() => setAuthDismissed(false)}
            className="rounded-md border border-err-border bg-surface px-3 py-1 text-xs font-semibold text-ink hover:bg-surface-hover"
          >
            {t("auth.reopen", "로그인")}
          </button>
        </div>
      )}
    </div>
  );
}
