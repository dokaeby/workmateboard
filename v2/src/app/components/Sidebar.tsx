import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { apiGet } from "../../shared/api/client";
import type { HealthDetail } from "../../shared/api/types";
import { useI18n } from "../../shared/i18n";
import { useAppStore, roleAllows } from "../../shared/store/appStore";
import { visibleNavItems } from "../nav";
import { LangToggle } from "./LangToggle";

/** 사이드바 하단 인프라 박스 — Observer/Collector 상태, Backend 버전, CPU/Memory.
 *  데이터 소스: /health(공개, Observer), /health/detail(admin, 버전·브리지).
 *  CPU/Memory는 현재 백엔드에 수집 소스가 없어 "-" 표시 (spec 010 US3). */
function InfraBox() {
  const { t } = useI18n();
  const { role } = useAppStore();
  // staleness 판정용 현재 시각 — 30초마다 갱신 (렌더 중 Date.now() 호출 회피).
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 30000);
    return () => window.clearInterval(id);
  }, []);
  const health = useQuery({
    queryKey: ["health"],
    queryFn: () => apiGet<{ status?: string }>("/health"),
    refetchInterval: 30000,
    retry: 1,
  });
  const detail = useQuery({
    queryKey: ["health-detail"],
    queryFn: () => apiGet<HealthDetail>("/health/detail"),
    refetchInterval: 60000,
    retry: false,
    // /health/detail은 platform-admin 전용 — 비관리자 403 폴링 방지
    enabled: roleAllows("admin", role),
  });

  const observerOk = health.isSuccess && health.data?.status === "ok";
  // Collector = 브리지 머신의 마지막 push가 10분 이내인지 (시스템 뷰와 동일 기준).
  const lastPush = detail.data?.bridge?.last_push_at;
  const pushTs = lastPush ? Date.parse(lastPush) : NaN;
  const collectorOk = !isNaN(pushTs) && now - pushTs <= 10 * 60 * 1000;
  const collectorLabel = !lastPush ? "-" : collectorOk ? t("infra.online", "ONLINE") : t("infra.stale", "지연");
  const collectorTone = collectorOk ? "ok" : "muted";
  const version = detail.data?.version || "-";

  const row = (label: string, value: string, tone: "ok" | "warn" | "muted") => (
    <div className="flex items-center justify-between gap-2 py-0.5">
      <span className="text-muted">{label}</span>
      <span className={`font-mono text-[11px] font-semibold ${
        tone === "ok" ? "text-ok" : tone === "warn" ? "text-warn" : "text-muted"
      }`}>{value}</span>
    </div>
  );

  return (
    <div className="border-t border-border bg-sidebar/60 px-3 py-2.5 text-[11px]">
      <div className="mb-1 text-[10px] font-bold uppercase tracking-widest text-muted">
        {t("app.tagline", "Agent Office")} · Infra
      </div>
      {row(t("infra.observer", "Observer"), observerOk ? t("infra.online", "ONLINE") : t("infra.offline", "OFFLINE"), observerOk ? "ok" : "warn")}
      {row(t("infra.collector", "Collector"), collectorLabel, collectorTone)}
      {row(t("infra.version", "버전"), version, "muted")}
      <div className="flex items-center justify-between gap-2 py-0.5">
        <span className="text-muted">{t("infra.cpu", "CPU")} / {t("infra.mem", "Memory")}</span>
        <span className="font-mono text-[11px] text-disabled">- / -</span>
      </div>
    </div>
  );
}

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
        <div className="fixed inset-0 z-30 bg-black/50 lg:hidden" onClick={() => setSidebarOpen(false)} />
      )}
      <aside
        className={`fixed inset-y-0 left-0 z-40 flex w-[212px] flex-col border-r border-border bg-sidebar transition-transform lg:static lg:translate-x-0 ${
          sidebarOpen ? "translate-x-0" : "-translate-x-full"
        }`}
      >
        <div className="flex h-12 items-center gap-2 border-b border-border px-4">
          <span className="text-base font-bold text-accent">jobMate</span>
          <span className="text-[10px] font-medium uppercase tracking-widest text-muted">{t("app.tagline", "Agent Office")}</span>
          <div className="ml-auto"><LangToggle /></div>
        </div>
        <nav className="flex-1 overflow-y-auto p-2">
          {items.map((it) => {
            const active = view === it.view;
            return (
              <button
                key={it.view}
                data-view={it.view}
                onClick={() => { setView(it.view); onNavigate?.(); }}
                className={`relative mb-0.5 flex w-full items-center gap-2 rounded-md px-3 py-1.5 text-[13px] transition-colors ${
                  active ? "bg-accent-soft font-semibold text-accent" : "text-ink-2 hover:bg-surface-hover hover:text-ink"
                }`}
              >
                {/* 선택 메뉴 왼쪽 2px cyan 강조선 (가이드 §7-B) */}
                {active && (
                  <span className="absolute left-0 top-1/2 h-4 w-0.5 -translate-y-1/2 rounded-full bg-accent" aria-hidden="true" />
                )}
                <span className="w-5 text-center">{it.icon}</span>
                {t(`nav.${it.view}`, it.label)}
                {it.view === "approvals" && badgeCount > 0 && (
                  <span className="ml-auto rounded-full bg-accent px-1.5 py-0.5 text-[10px] font-bold leading-none text-on-accent">
                    {badgeCount > 99 ? "99+" : badgeCount}
                  </span>
                )}
              </button>
            );
          })}
        </nav>
        <InfraBox />
        <div className="border-t border-border px-3 py-2 text-[11px] text-muted">
          <a href="/static/privacy.html" target="_blank" rel="noopener" className="hover:text-accent">{t("app.privacy", "개인정보처리방침")}</a>
          {" · "}
          <a href="/static/terms.html" target="_blank" rel="noopener" className="hover:text-accent">{t("app.terms", "이용약관")}</a>
        </div>
      </aside>
    </>
  );
}
