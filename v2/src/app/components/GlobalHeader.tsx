import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { apiGet } from "../../shared/api/client";
import type { Me } from "../../shared/api/types";
import { useI18n } from "../../shared/i18n";
import { useSseStatus } from "../../shared/stream";
import { LangToggle } from "./LangToggle";
import { HeaderActions } from "./HeaderActions";

/** 10초마다 재렌더 — "마지막 수신" 상대시각 표시용. */
function useNow(intervalMs = 10000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), intervalMs);
    return () => window.clearInterval(id);
  }, [intervalMs]);
  return now;
}

function agoLabel(ts: number | null, now: number, t: (k: string, f?: string) => string): string {
  if (!ts) return t("hdr.noData", "-");
  const s = Math.max(0, Math.floor((now - ts) / 1000));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  return `${Math.floor(s / 3600)}h`;
}

/**
 * Global Header (spec 010 US2, 가이드 §7-A) — 데스크톱 전용 48px 상태바.
 * 로고/워크스페이스 + 연결 상태등(SYSTEM ONLINE/DEGRADED/OFFLINE) +
 * 마지막 데이터 수신 시각 + 실시간 모드 표시.
 * 상태 판정: /health 실패 → OFFLINE, SSE online → ONLINE, 그 외 → DEGRADED.
 * 상태는 색상+텍스트 라벨 병행 (가이드 §4).
 */
export function GlobalHeader() {
  const { t } = useI18n();
  const sse = useSseStatus();
  const now = useNow();

  // API 생존 확인 — /health 는 공개 엔드포인트, 30초 폴링.
  const health = useQuery({
    queryKey: ["health"],
    queryFn: () => apiGet<{ status?: string }>("/health"),
    refetchInterval: 30000,
    retry: 1,
  });
  // ["me"] 쿼리는 App.tsx가 fetchMe로 이미 구독 중 — 캐시만 읽는다
  // (enabled:false로 자체 fetch 방지: 네트워크 오류 시 null 반환으로
  //  App의 needsAuth 판정을 오염시키지 않기 위함).
  const me = useQuery<Me | null>({
    queryKey: ["me"],
    queryFn: () => Promise.resolve(null),
    enabled: false,
    staleTime: Infinity,
  });

  const apiDown = health.isError || (health.isSuccess && health.data?.status !== "ok");
  const level: "online" | "degraded" | "offline" = apiDown
    ? "offline"
    : sse.state === "online"
      ? "online"
      : "degraded";

  const statusCls = {
    online: "text-ok",
    degraded: "text-warn",
    offline: "text-err",
  }[level];
  const dotCls = {
    online: "bg-ok",
    degraded: "bg-warn",
    offline: "bg-err",
  }[level];
  const statusLabel = t(`hdr.status.${level}`, level);
  const live = sse.state === "online";
  const workspace = me.data?.display_name || me.data?.email || me.data?.name || "-";

  return (
    <header className="sticky top-0 z-30 hidden h-12 items-center gap-4 border-b border-border bg-surface/90 px-4 backdrop-blur lg:flex">
      <div className="flex items-center gap-2">
        <span className="text-sm font-bold tracking-wide text-accent">jobMate</span>
        <span className="text-[11px] font-medium uppercase tracking-widest text-muted">
          {t("app.tagline", "Agent Office")}
        </span>
      </div>

      <div className="flex items-center gap-1.5 text-xs text-muted">
        <span className="uppercase tracking-wider">{t("hdr.workspace", "워크스페이스")}</span>
        <span className="max-w-40 truncate font-medium text-ink-2" title={workspace}>
          {workspace}
        </span>
      </div>

      {/* 연결 상태등 — 색상+텍스트 라벨 병행 */}
      <div className={`flex items-center gap-1.5 text-xs font-semibold ${statusCls}`}>
        <span className={`inline-block h-2 w-2 rounded-full ${dotCls} ${level === "online" ? "glow-green" : ""}`} />
        <span className="tracking-wide">{statusLabel}</span>
      </div>

      <div className="flex items-center gap-1.5 text-xs text-muted">
        <span className={`rounded border px-1.5 py-0.5 text-[10px] font-bold tracking-wider ${
          live ? "border-ok-border bg-ok-soft text-ok" : "border-border bg-surface-hover text-muted"
        }`}>
          {t("hdr.live", "실시간")}
        </span>
        <span className="font-mono text-[11px]">
          {t("hdr.lastData", "마지막 수신")} {agoLabel(sse.lastEventAt, now, t)}
        </span>
      </div>

      <div className="ml-auto flex items-center gap-0.5">
        <LangToggle />
        <HeaderActions />
      </div>
    </header>
  );
}
