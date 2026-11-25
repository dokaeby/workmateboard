/** 개발자 상태 — /developer/status: 연동/인증 구성 bool·노트만 (admin 전용). */
import { useQuery } from "@tanstack/react-query";
import { apiGet } from "../../shared/api/client";
import { useAppStore, roleAllows } from "../../shared/store/appStore";
import { useI18n, registerI18n } from "../../shared/i18n";
import { STR } from "./i18n";
import { PageHead, Card, Empty, Spinner, ErrorBox, Button, Badge } from "../../shared/ui";
import { fmtTs } from "../../shared/lib/format";

registerI18n(STR);

interface Integration {
  name?: string;
  configured?: boolean;
  note?: string;
}

interface DeveloperStatus {
  integrations?: Integration[];
  auth?: {
    google?: boolean;
    password?: boolean;
    api_key?: boolean;
    admin_emails_configured?: boolean;
  };
  generated_at?: string;
}

function YnBadge({ v, on, off }: { v: boolean | undefined; on: string; off: string }) {
  return v ? <Badge tone="ok">{on}</Badge> : <Badge tone="err">{off}</Badge>;
}

function DeveloperBody() {
  const { t } = useI18n();
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["developer-status"],
    queryFn: () => apiGet<DeveloperStatus>("/developer/status"),
    refetchInterval: 60000,
  });

  const integrations = data?.integrations || [];
  const auth = data?.auth || {};

  return (
    <div className="mx-auto max-w-4xl space-y-3 p-4">
      <PageHead
        title={t("dev.title", "개발자")}
        desc={t("dev.desc", "연동·인증 구성 상태 — 값이 아닌 설정 여부만 표시")}
      >
        <Button variant="secondary" className="text-xs" onClick={() => refetch()}>
          {t("common.refresh", "새로고침")}
        </Button>
      </PageHead>

      {isLoading && <Spinner />}
      {error && <ErrorBox message={error instanceof Error ? error.message : String(error)} onRetry={() => refetch()} />}

      {data && (
        <>
          <Card title={t("dev.integrations", "연동")}>
            {!integrations.length && <Empty>{t("dev.none", "연동 정보가 없습니다")}</Empty>}
            {integrations.length > 0 && (
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-border text-left text-xs text-muted">
                      <th className="py-1.5 pr-3 font-medium">{t("dev.name", "이름")}</th>
                      <th className="py-1.5 pr-3 font-medium">{t("dev.status", "상태")}</th>
                      <th className="py-1.5 font-medium">{t("dev.note", "메모")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {integrations.map((i, idx) => (
                      <tr key={i.name || idx} className="border-b border-border last:border-0">
                        <td className="py-2 pr-3 font-mono text-xs">{i.name || "-"}</td>
                        <td className="py-2 pr-3">
                          <YnBadge v={i.configured} on={t("dev.configured", "설정됨")} off={t("dev.notConfigured", "미설정")} />
                        </td>
                        <td className="py-2 text-xs text-muted">{i.note || "-"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Card>

          <Card title={t("dev.auth", "인증")}>
            <dl className="grid gap-x-6 gap-y-1.5 text-sm sm:grid-cols-2">
              <div className="flex justify-between gap-2 border-b border-border py-1">
                <dt className="text-muted">{t("dev.google", "Google 로그인")}</dt>
                <dd><YnBadge v={auth.google} on={t("dev.configured", "설정됨")} off={t("dev.notConfigured", "미설정")} /></dd>
              </div>
              <div className="flex justify-between gap-2 border-b border-border py-1">
                <dt className="text-muted">{t("dev.password", "비밀번호 로그인")}</dt>
                <dd><YnBadge v={auth.password} on={t("dev.on", "사용")} off={t("dev.off", "미사용")} /></dd>
              </div>
              <div className="flex justify-between gap-2 border-b border-border py-1">
                <dt className="text-muted">{t("dev.apiKey", "API 키")}</dt>
                <dd><YnBadge v={auth.api_key} on={t("dev.configured", "설정됨")} off={t("dev.notConfigured", "미설정")} /></dd>
              </div>
              <div className="flex justify-between gap-2 border-b border-border py-1">
                <dt className="text-muted">{t("dev.adminEmails", "관리자 이메일 설정")}</dt>
                <dd><YnBadge v={auth.admin_emails_configured} on={t("dev.configured", "설정됨")} off={t("dev.notConfigured", "미설정")} /></dd>
              </div>
              <div className="flex justify-between gap-2 border-b border-border py-1 sm:col-span-2">
                <dt className="text-muted">{t("dev.generatedAt", "생성 시각")}</dt>
                <dd>{fmtTs(data.generated_at)}</dd>
              </div>
            </dl>
          </Card>
        </>
      )}
    </div>
  );
}

export default function DeveloperPage() {
  const role = useAppStore((s) => s.role);
  const { t } = useI18n();
  if (role != null && !roleAllows("admin", role)) {
    return (
      <div className="mx-auto max-w-4xl p-4">
        <PageHead title={t("dev.title", "개발자")} />
        <Empty>{t("dev.adminOnly", "이 화면은 관리자(admin) 전용입니다.")}</Empty>
      </div>
    );
  }
  return <DeveloperBody />;
}
