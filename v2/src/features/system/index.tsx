/** 시스템 상태 — /health/detail: API 가동시간/DB/브리지 머신/cron 틱 (admin 전용). */
import { useQuery } from "@tanstack/react-query";
import { apiGet } from "../../shared/api/client";
import { useAppStore, roleAllows } from "../../shared/store/appStore";
import { useI18n } from "../../shared/i18n";
import { PageHead, Card, Empty, Spinner, ErrorBox, Button, Badge } from "../../shared/ui";
import { fmtTs, jsonSummary } from "../../shared/lib/format";

interface BridgeMachine {
  machine?: string;
  last_push_at?: string;
  sessions?: number;
}

interface HealthDetail {
  api?: { uptime_s?: number };
  db?: { type?: string; path?: string | null; size_bytes?: number | null };
  bridge?: { last_push_at?: string; machines?: BridgeMachine[] };
  cron?: { last_tick_at?: string; reaper?: Record<string, unknown> };
  version?: string;
}

function fmtSecs(sec: number | null | undefined): string {
  const s = Number(sec);
  if (!isFinite(s) || s < 0) return "-";
  if (s < 60) return `${Math.floor(s)}초`;
  if (s < 3600) return `${Math.floor(s / 60)}분`;
  if (s < 86400) return `${Math.floor(s / 3600)}시간 ${Math.floor((s % 3600) / 60)}분`;
  return `${Math.floor(s / 86400)}일 ${Math.floor((s % 86400) / 3600)}시간`;
}

function dbLabel(db: HealthDetail["db"]): string {
  if (!db || !db.type) return "-";
  const parts = [db.type];
  if (db.path) parts.push(db.path);
  if (db.size_bytes != null) parts.push(`${(db.size_bytes / 1048576).toFixed(1)}MB`);
  return parts.join(" · ");
}

function stale(lastPush: string | undefined): boolean {
  if (!lastPush) return false;
  const ts = Date.parse(lastPush);
  return !isNaN(ts) && Date.now() - ts > 10 * 60 * 1000;
}

function SystemBody() {
  const { t } = useI18n();
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["health-detail"],
    queryFn: () => apiGet<HealthDetail>("/health/detail"),
    refetchInterval: 30000,
  });

  const machines = data?.bridge?.machines || [];
  const reaper = data?.cron?.reaper;

  return (
    <div className="mx-auto max-w-5xl space-y-3 p-4">
      <PageHead
        title={t("sys.title", "시스템")}
        desc={t("sys.desc", "API·DB·브리지·cron 상태 — 30초마다 자동 갱신")}
      >
        <Button variant="secondary" className="text-xs" onClick={() => refetch()}>
          {t("common.refresh", "새로고침")}
        </Button>
      </PageHead>

      {isLoading && <Spinner />}
      {error && <ErrorBox message={error instanceof Error ? error.message : String(error)} onRetry={() => refetch()} />}

      {data && (
        <>
          <Card title={t("sys.overview", "개요")}>
            <dl className="grid gap-x-6 gap-y-1.5 text-sm sm:grid-cols-2">
              <div className="flex justify-between gap-2 border-b border-border py-1">
                <dt className="text-muted">{t("sys.uptime", "API 가동시간")}</dt>
                <dd>{fmtSecs(data.api?.uptime_s)}</dd>
              </div>
              <div className="flex justify-between gap-2 border-b border-border py-1">
                <dt className="text-muted">{t("sys.version", "버전")}</dt>
                <dd className="font-mono text-xs">{data.version || "-"}</dd>
              </div>
              <div className="flex justify-between gap-2 border-b border-border py-1 sm:col-span-2">
                <dt className="text-muted">DB</dt>
                <dd className="truncate font-mono text-xs" title={dbLabel(data.db)}>{dbLabel(data.db)}</dd>
              </div>
              <div className="flex justify-between gap-2 border-b border-border py-1">
                <dt className="text-muted">{t("sys.cronTick", "크론 마지막 틱")}</dt>
                <dd>{fmtTs(data.cron?.last_tick_at)}</dd>
              </div>
              {reaper && Object.keys(reaper).length > 0 && (
                <div className="flex justify-between gap-2 border-b border-border py-1">
                  <dt className="text-muted">{t("sys.reaper", "리퍼")}</dt>
                  <dd className="font-mono text-xs">{jsonSummary(reaper, 120)}</dd>
                </div>
              )}
            </dl>
          </Card>

          <div>
            <h2 className="mb-2 text-xs font-bold uppercase text-muted">{t("sys.machines", "브리지 머신")}</h2>
            {!machines.length && (
              <Card>
                <dl className="grid gap-x-6 gap-y-1.5 text-sm sm:grid-cols-2">
                  <div className="flex justify-between gap-2 border-b border-border py-1">
                    <dt className="text-muted">{t("sys.machines", "브리지 머신")}</dt>
                    <dd>-</dd>
                  </div>
                  <div className="flex justify-between gap-2 border-b border-border py-1">
                    <dt className="text-muted">{t("sys.lastPush", "브리지 마지막 push")}</dt>
                    <dd>{fmtTs(data.bridge?.last_push_at)}</dd>
                  </div>
                </dl>
              </Card>
            )}
            <div className="grid gap-2 sm:grid-cols-2">
              {machines.map((m, i) => {
                const isStale = stale(m.last_push_at);
                return (
                  <Card key={m.machine || i}>
                    <div className="mb-2 flex items-center justify-between gap-2">
                      <span className="font-mono text-sm font-semibold">{m.machine || "-"}</span>
                      {isStale
                        ? <Badge tone="warn">{t("sys.stale", "10분+ 지연")}</Badge>
                        : <Badge tone="ok">{t("sys.ok", "정상")}</Badge>}
                    </div>
                    <dl className="grid gap-y-1 text-xs">
                      <div className="flex justify-between gap-2">
                        <dt className="text-muted">{t("sys.lastPush", "마지막 push")}</dt>
                        <dd>{fmtTs(m.last_push_at)}</dd>
                      </div>
                      <div className="flex justify-between gap-2">
                        <dt className="text-muted">{t("sys.sessions", "세션 수")}</dt>
                        <dd>{m.sessions ?? 0}</dd>
                      </div>
                    </dl>
                  </Card>
                );
              })}
            </div>
          </div>
        </>
      )}
    </div>
  );
}

export default function SystemPage() {
  const role = useAppStore((s) => s.role);
  const { t } = useI18n();
  if (role != null && !roleAllows("admin", role)) {
    return (
      <div className="mx-auto max-w-5xl p-4">
        <PageHead title={t("sys.title", "시스템")} />
        <Empty>{t("sys.adminOnly", "이 화면은 관리자(admin) 전용입니다.")}</Empty>
      </div>
    );
  }
  return <SystemBody />;
}
