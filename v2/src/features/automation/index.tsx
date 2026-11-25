/** 자동화 허브 — 스케줄/알림 규칙/cron/메신저/리포트 상태 개요 (admin 전용). */
import { useQuery } from "@tanstack/react-query";
import { apiGet } from "../../shared/api/client";
import { useAppStore, roleAllows } from "../../shared/store/appStore";
import { useI18n, registerI18n } from "../../shared/i18n";
import { STR } from "./i18n";
import { PageHead, Card, Empty, Spinner, ErrorBox, Button, Badge } from "../../shared/ui";
import { fmtTs, jsonSummary } from "../../shared/lib/format";

registerI18n(STR);

interface ScheduleItem {
  id?: string;
  workflow?: string;
  interval_minutes?: number;
  enabled?: boolean;
  last_run_at?: string;
  next_run_at?: string;
  input?: Record<string, unknown>;
}

interface AutomationOverview {
  schedules?: ScheduleItem[];
  notify_rules?: unknown[];
  cron?: { last_tick_at?: string };
  messenger?: { slack?: boolean; telegram?: boolean };
  reports?: { last_daily?: string | null; last_weekly?: string | null };
  generated_at?: string;
}

function RuleText({ rule }: { rule: unknown }) {
  // 레거시 규칙: {tool, project, statuses} 조합 — 없으면 원문 JSON
  const r = (rule || {}) as Record<string, unknown>;
  const parts = [r.tool, r.project, r.statuses].filter(Boolean);
  if (!parts.length) return <span className="text-muted">{jsonSummary(rule, 200)}</span>;
  return (
    <span>
      {parts.map((p, i) => (
        <span key={i}>
          {i > 0 && <span className="text-muted"> · </span>}
          {Array.isArray(p) ? p.join("/") : String(p)}
        </span>
      ))}
    </span>
  );
}

function AutomationBody() {
  const { t } = useI18n();
  const setView = useAppStore((s) => s.setView);
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["automation-overview"],
    queryFn: () => apiGet<AutomationOverview>("/automation/overview"),
    refetchInterval: 60000,
  });

  const schedules = data?.schedules || [];
  const rules = Array.isArray(data?.notify_rules) ? data!.notify_rules! : [];
  const messenger = data?.messenger || {};
  const reports = data?.reports || {};

  return (
    <div className="mx-auto max-w-5xl space-y-3 p-4">
      <PageHead
        title={t("auto.title", "자동화")}
        desc={t("auto.desc", "스케줄·알림 규칙·메신저·리포트 상태 — 60초마다 자동 갱신")}
      >
        <Button variant="secondary" className="text-xs" onClick={() => setView("office")}>
          {t("auto.manageInOffice", "오피스에서 관리")}
        </Button>
        <Button variant="secondary" className="text-xs" onClick={() => setView("settings")}>
          {t("auto.settings", "설정")}
        </Button>
        <Button variant="secondary" className="text-xs" onClick={() => refetch()}>
          {t("common.refresh", "새로고침")}
        </Button>
      </PageHead>

      {isLoading && <Spinner />}
      {error && <ErrorBox message={error instanceof Error ? error.message : String(error)} onRetry={() => refetch()} />}

      {data && (
        <>
          <Card title={t("auto.schedules", "스케줄")}>
            {!schedules.length && <Empty>{t("auto.noSchedules", "등록된 스케줄이 없습니다")}</Empty>}
            {schedules.length > 0 && (
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-border text-left text-xs text-muted">
                      <th className="py-1.5 pr-3 font-medium">{t("auto.workflow", "워크플로")}</th>
                      <th className="py-1.5 pr-3 font-medium">{t("auto.interval", "간격")}</th>
                      <th className="py-1.5 pr-3 font-medium">{t("auto.status", "상태")}</th>
                      <th className="py-1.5 pr-3 font-medium">{t("auto.nextRun", "다음 실행")}</th>
                      <th className="py-1.5 pr-3 font-medium">{t("auto.lastRun", "마지막 실행")}</th>
                      <th className="py-1.5 font-medium">{t("auto.input", "입력")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {schedules.map((s, i) => (
                      <tr key={s.id || i} className="border-b border-border last:border-0">
                        <td className="py-2 pr-3 font-mono text-xs">{s.workflow || "-"}</td>
                        <td className="py-2 pr-3 text-xs">{s.interval_minutes != null ? t("auto.minFmt", "{n}분").replace("{n}", String(s.interval_minutes)) : "-"}</td>
                        <td className="py-2 pr-3">
                          {s.enabled
                            ? <Badge tone="ok">{t("auto.on", "사용")}</Badge>
                            : <Badge tone="err">{t("auto.off", "중지")}</Badge>}
                        </td>
                        <td className="py-2 pr-3 text-xs text-muted">{fmtTs(s.next_run_at)}</td>
                        <td className="py-2 pr-3 text-xs text-muted">{fmtTs(s.last_run_at)}</td>
                        <td className="max-w-[220px] truncate py-2 text-xs text-muted">
                          {s.input && Object.keys(s.input).length ? jsonSummary(s.input, 120) : "-"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Card>

          <Card title={t("auto.notifyRules", "알림 규칙")}>
            {!rules.length && <span className="text-sm text-muted">{t("auto.noRules", "규칙 없음")}</span>}
            <div className="space-y-1.5">
              {rules.map((r, i) => (
                <div key={i} className="flex gap-2 text-sm">
                  <span className="shrink-0 text-xs font-semibold text-muted">{t("auto.rule", "규칙")}</span>
                  <RuleText rule={r} />
                </div>
              ))}
            </div>
          </Card>

          <Card title={t("auto.system", "시스템")}>
            <dl className="grid gap-x-6 gap-y-1.5 text-sm sm:grid-cols-2">
              <div className="flex justify-between gap-2 border-b border-border py-1">
                <dt className="text-muted">{t("auto.cronTick", "크론 마지막 틱")}</dt>
                <dd>{fmtTs(data.cron?.last_tick_at)}</dd>
              </div>
              <div className="flex justify-between gap-2 border-b border-border py-1">
                <dt className="text-muted">Slack</dt>
                <dd>{messenger.slack
                  ? <Badge tone="ok">{t("auto.connected", "연결됨")}</Badge>
                  : <Badge tone="err">{t("auto.disconnected", "미연결")}</Badge>}</dd>
              </div>
              <div className="flex justify-between gap-2 border-b border-border py-1">
                <dt className="text-muted">Telegram</dt>
                <dd>{messenger.telegram
                  ? <Badge tone="ok">{t("auto.connected", "연결됨")}</Badge>
                  : <Badge tone="err">{t("auto.disconnected", "미연결")}</Badge>}</dd>
              </div>
              <div className="flex justify-between gap-2 border-b border-border py-1">
                <dt className="text-muted">{t("auto.lastDaily", "최근 일일 리포트")}</dt>
                <dd>{fmtTs(reports.last_daily)}</dd>
              </div>
              <div className="flex justify-between gap-2 border-b border-border py-1">
                <dt className="text-muted">{t("auto.lastWeekly", "최근 주간 리포트")}</dt>
                <dd>{fmtTs(reports.last_weekly)}</dd>
              </div>
              <div className="flex justify-between gap-2 border-b border-border py-1">
                <dt className="text-muted">{t("auto.generatedAt", "갱신 시각")}</dt>
                <dd>{fmtTs(data.generated_at)}</dd>
              </div>
            </dl>
          </Card>
        </>
      )}
    </div>
  );
}

export default function AutomationPage() {
  const role = useAppStore((s) => s.role);
  const { t } = useI18n();
  if (role != null && !roleAllows("admin", role)) {
    return (
      <div className="mx-auto max-w-5xl p-4">
        <PageHead title={t("auto.title", "자동화")} />
        <Empty>{t("auto.adminOnly", "이 화면은 관리자(admin) 전용입니다.")}</Empty>
      </div>
    );
  }
  return <AutomationBody />;
}
