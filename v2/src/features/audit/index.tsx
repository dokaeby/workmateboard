/** 감사 로그 — /audit 테이블 (admin 전용). 시각/actor/action/target/detail. */
import { useQuery } from "@tanstack/react-query";
import { apiGet } from "../../shared/api/client";
import { useAppStore, roleAllows } from "../../shared/store/appStore";
import { useI18n, registerI18n } from "../../shared/i18n";
import { PageHead, Card, Empty, Spinner, ErrorBox, Button } from "../../shared/ui";
import { fmtTs, jsonSummary } from "../../shared/lib/format";
import { STR } from "./i18n";

registerI18n(STR);

interface AuditItem {
  ts?: string;
  actor_email?: string;
  action?: string;
  target?: string;
  detail?: unknown;
  run_id?: string;
}

interface AuditResponse { items?: AuditItem[] }

function AuditBody() {
  const { t } = useI18n();
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["audit"],
    queryFn: () => apiGet<AuditResponse>("/audit?limit=200"),
    refetchInterval: 60000,
  });

  const items = (data?.items || [])
    .slice()
    .sort((a, b) => String(b.ts || "").localeCompare(String(a.ts || "")));

  return (
    <div className="mx-auto max-w-6xl p-4">
      <PageHead
        title={t("audit.title", "감사 로그")}
        desc={t("audit.desc", "관리자·권한 변경 등 감사 이벤트 — 최신순, 최대 200건")}
      >
        <Button variant="secondary" className="text-xs" onClick={() => refetch()}>
          {t("common.refresh", "새로고침")}
        </Button>
      </PageHead>
      <Card>
        {isLoading && <Spinner />}
        {error && <ErrorBox message={error instanceof Error ? error.message : String(error)} onRetry={() => refetch()} />}
        {data && !items.length && <Empty>{t("audit.empty", "감사 로그가 없습니다")}</Empty>}
        {items.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border text-left text-xs text-muted">
                  <th className="py-1.5 pr-3 font-medium whitespace-nowrap">{t("audit.ts", "시각")}</th>
                  <th className="py-1.5 pr-3 font-medium">{t("audit.actor", "행위자")}</th>
                  <th className="py-1.5 pr-3 font-medium">{t("audit.action", "행동")}</th>
                  <th className="py-1.5 pr-3 font-medium">{t("audit.target", "대상")}</th>
                  <th className="py-1.5 font-medium">{t("audit.detail", "상세")}</th>
                </tr>
              </thead>
              <tbody>
                {items.map((a, i) => (
                  <tr key={`${a.ts}-${i}`} className="border-b border-border align-top last:border-0">
                    <td className="py-2 pr-3 text-xs text-muted whitespace-nowrap">{fmtTs(a.ts)}</td>
                    <td className="py-2 pr-3 text-xs">{a.actor_email || "-"}</td>
                    <td className="py-2 pr-3 text-xs font-medium">{a.action || "-"}</td>
                    <td className="max-w-[200px] truncate py-2 pr-3 font-mono text-xs" title={a.target || ""}>
                      {a.target || "-"}
                    </td>
                    <td className="max-w-[280px] break-all py-2 text-xs text-muted">
                      {a.detail == null ? "-" : typeof a.detail === "string" ? a.detail : jsonSummary(a.detail, 300)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}

export default function AuditPage() {
  const role = useAppStore((s) => s.role);
  const { t } = useI18n();
  if (role != null && !roleAllows("admin", role)) {
    return (
      <div className="mx-auto max-w-6xl p-4">
        <PageHead title={t("audit.title", "감사 로그")} />
        <Empty>{t("audit.adminOnly", "감사 로그는 관리자(admin) 전용입니다.")}</Empty>
      </div>
    );
  }
  return <AuditBody />;
}
