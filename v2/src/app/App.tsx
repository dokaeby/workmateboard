import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { apiGet, ApiError } from "../shared/api/client";
import type { Me, Prefs, ViewKey } from "../shared/api/types";
import { useAppStore, roleAllows } from "../shared/store/appStore";
import { useI18n, getLang, setLang, registerI18n, type Lang } from "../shared/i18n";
import { NAV_ITEMS } from "./nav";
import { STR as APP_I18N } from "./i18n";
import { ViewLoader } from "./views";
import { useSseInvalidation } from "../shared/stream";
import { Sidebar } from "./components/Sidebar";
import { Header } from "./components/Header";
import { GlobalHeader } from "./components/GlobalHeader";
import { MobileBar } from "./components/MobileBar";
import { LoginOverlay } from "./components/LoginOverlay";

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
  // spec 006 FR-028 — 서버에 저장된 언어가 있으면 localStorage 감지값보다 우선
  useEffect(() => {
    const loc = me.data?.locale;
    if ((loc === "ko" || loc === "en") && loc !== getLang()) setLang(loc);
  }, [me.data]);
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
      {/* spec 010 US2 — 데스크톱 Global Header (모바일은 Header가 대체) */}
      <GlobalHeader />
      <Header />
      <div className="flex min-h-0 flex-1">
        <Sidebar />
        <div className="flex min-w-0 flex-1 flex-col">
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
