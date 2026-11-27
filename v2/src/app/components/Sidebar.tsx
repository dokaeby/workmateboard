import { useQuery } from "@tanstack/react-query";
import { apiGet } from "../../shared/api/client";
import { useI18n } from "../../shared/i18n";
import { useAppStore, roleAllows } from "../../shared/store/appStore";
import { visibleNavItems } from "../nav";
import { LangToggle } from "./LangToggle";

export function Sidebar({ onNavigate }: { onNavigate?: () => void }) {
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
