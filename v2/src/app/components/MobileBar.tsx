import { useQuery } from "@tanstack/react-query";
import { apiGet } from "../../shared/api/client";
import { useI18n } from "../../shared/i18n";
import { useAppStore, roleAllows } from "../../shared/store/appStore";

/** 모바일 하단바 — 승인 대기/오늘 실행/활성 세션/월 비용 요약. */
export function MobileBar() {
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
