/** 시스템 상태 — GET /health/detail (admin 전용). active=true가 되면 lazy 로드. */
import type { ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { apiGet } from "../../../shared/api/client";
import { useI18n } from "../../../shared/i18n";
import { Spinner, ErrorBox, Button, Badge } from "../../../shared/ui";
import { fmtSecs, fmtTsShort } from "../lib";
import type { HealthDetail } from "../types";

function Kv({ k, v }: { k: string; v: ReactNode }) {
  return (
    <div className="flex gap-3 py-1.5 text-sm">
      <span className="w-32 flex-none text-muted">{k}</span>
      <span className="min-w-0 flex-1 break-all text-ink">{v}</span>
    </div>
  );
}

export default function SystemHealth({ active }: { active: boolean }) {
  const { t } = useI18n();
  const { data, isLoading, error, refetch, isFetching } = useQuery({
    queryKey: ["health-detail"],
    queryFn: () => apiGet<HealthDetail>("/health/detail"),
    enabled: active,
    staleTime: 30000,
  });

  if (!active) {
    return <div className="py-4 text-sm text-muted">{t("settings.system.loading", "불러오는 중…")}</div>;
  }
  if (isLoading) return <Spinner label={t("settings.system.loading", "불러오는 중…")} />;
  if (error) {
    return <ErrorBox message={error instanceof Error ? error.message : String(error)} onRetry={() => refetch()} />;
  }
  if (!data) return null;

  const uptime = data.api?.uptime_s != null ? fmtSecs(data.api.uptime_s) : "-";
  const dbTxt = data.db && Object.keys(data.db).length
    ? JSON.stringify(data.db).slice(0, 300)
    : "-";
  const lastPush = data.bridge?.last_push_at || "";
  const pushTs = Date.parse(lastPush);
  const stale = !isNaN(pushTs) && Date.now() - pushTs > 10 * 60 * 1000;
  const machines = Array.isArray(data.bridge?.machines) ? data.bridge!.machines! : [];
  const cronTs = data.cron?.last_tick_at;

  return (
    <div>
      <div className="divide-y divide-border">
        <Kv k={t("settings.system.api_uptime", "API 가동시간")} v={uptime} />
        <Kv k={t("settings.system.db", "DB")} v={<span className="font-mono text-xs">{dbTxt}</span>} />
        <Kv
          k={t("settings.system.bridge", "브리지 마지막 push")}
          v={
            <span>
              {fmtTsShort(lastPush)}
              {stale && (
                <Badge tone="warn"> {t("settings.system.stale", "10분 이상 지연")}</Badge>
              )}
            </span>
          }
        />
        {machines.length ? (
          machines.map((m, i) => (
            <Kv
              key={i}
              k={t("settings.system.machine", "머신")}
              v={`${m.machine || "-"} — ${fmtTsShort(m.last_push_at)} · ${t("settings.system.sessions", "세션")} ${m.sessions ?? 0}`}
            />
          ))
        ) : (
          <Kv k={t("settings.system.machine", "머신")} v="-" />
        )}
        <Kv k={t("settings.system.cron", "크론 마지막 틱")} v={fmtTsShort(cronTs)} />
        {data.version && <Kv k="version" v={data.version} />}
      </div>
      <Button
        variant="secondary"
        className="mt-3 px-2.5 py-1 text-xs"
        disabled={isFetching}
        onClick={() => refetch()}
      >
        ↻ {t("settings.system.refresh", "새로고침")}
      </Button>
    </div>
  );
}
