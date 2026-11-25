/** 팀 — /team 멤버별 run/세션 집계 (lead+). admin은 역할 변경 가능. */
import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { apiGet, apiPatch, ApiError } from "../../shared/api/client";
import type { Me, Role } from "../../shared/api/types";
import { useAppStore, roleAllows } from "../../shared/store/appStore";
import { useI18n } from "../../shared/i18n";
import { PageHead, Card, Empty, Spinner, ErrorBox, Badge } from "../../shared/ui";
import { fmtTs } from "../../shared/lib/format";
import "./i18n";


interface TeamMember {
  id?: string;
  email?: string;
  name?: string;
  role?: string;
  run_count?: number;
  session_count?: number;
  last_active_at?: string | null;
}

interface TeamResponse { items?: TeamMember[] }
interface MeResponse { user?: Me }

const ROLE_TONE: Record<string, "muted" | "accent" | "warn"> = {
  member: "muted", lead: "accent", admin: "warn",
};

function normRole(r: string | undefined): string {
  const v = String(r || "").toLowerCase();
  return v === "user" ? "member" : v;
}

function TeamBody() {
  const { t } = useI18n();
  const role = useAppStore((s) => s.role);
  const isAdmin = roleAllows("admin", role);
  const qc = useQueryClient();
  const [changing, setChanging] = useState<string | null>(null);

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["team"],
    queryFn: () => apiGet<TeamResponse>("/team"),
    refetchInterval: 60000,
  });
  const me = useQuery({
    queryKey: ["auth-me-full"],
    queryFn: () => apiGet<MeResponse>("/auth/me"),
    staleTime: 60000,
    retry: false,
  });

  const items = data?.items || [];
  const meEmail = (me.data?.user?.email || "").toLowerCase();

  const changeRole = async (u: TeamMember, next: string) => {
    if (!u.id) { alert(t("team.noId", "사용자 ID가 없습니다")); return; }
    setChanging(u.id);
    try {
      await apiPatch(`/users/${encodeURIComponent(u.id)}/role`, { role: next });
      await qc.invalidateQueries({ queryKey: ["team"] });
    } catch (e) {
      alert(e instanceof ApiError ? e.message : String(e));
      refetch();
    } finally {
      setChanging(null);
    }
  };

  return (
    <div className="mx-auto max-w-5xl p-4">
      <PageHead
        title={t("team.title", "팀")}
        desc={t("team.desc", "멤버별 실행·세션 활동 — 60초마다 자동 갱신")}
      >
        <button
          className="rounded-lg border border-border bg-surface px-3 py-1.5 text-sm font-medium text-ink-2 hover:bg-surface-hover"
          onClick={() => refetch()}
        >
          {t("common.refresh", "새로고침")}
        </button>
      </PageHead>
      <Card>
        {isLoading && <Spinner />}
        {error && <ErrorBox message={error instanceof Error ? error.message : String(error)} onRetry={() => refetch()} />}
        {data && !items.length && <Empty>{t("team.empty", "팀 멤버가 없습니다")}</Empty>}
        {items.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border text-left text-xs text-muted">
                  <th className="py-1.5 pr-3 font-medium">{t("team.email", "이메일")}</th>
                  <th className="py-1.5 pr-3 font-medium">{t("team.name", "이름")}</th>
                  <th className="py-1.5 pr-3 font-medium">{t("team.role", "역할")}</th>
                  <th className="py-1.5 pr-3 font-medium">{t("team.runs", "실행")}</th>
                  <th className="py-1.5 pr-3 font-medium">{t("team.sessions", "세션")}</th>
                  <th className="py-1.5 font-medium">{t("team.lastActive", "최근 활동")}</th>
                </tr>
              </thead>
              <tbody>
                {items.map((u, i) => {
                  const isMe = meEmail && (u.email || "").toLowerCase() === meEmail;
                  const uRole = normRole(u.role);
                  return (
                    <tr key={u.id || u.email || i} className="border-b border-border last:border-0">
                      <td className="py-2 pr-3">
                        {u.email || "-"}
                        {isMe && <Badge tone="accent">{t("team.me", "나")}</Badge>}
                      </td>
                      <td className="py-2 pr-3 text-xs text-muted">{u.name || "-"}</td>
                      <td className="py-2 pr-3">
                        {isAdmin ? (
                          <select
                            aria-label={t("team.changeRole", "역할 변경")}
                            disabled={changing === u.id}
                            value={uRole}
                            onChange={(e) => changeRole(u, e.target.value)}
                            className="rounded-md border border-border bg-surface px-1.5 py-1 text-xs"
                          >
                            {(["member", "lead", "admin"] as Role[]).map((r) => (
                              <option key={r} value={r}>{r}</option>
                            ))}
                          </select>
                        ) : (
                          <Badge tone={ROLE_TONE[uRole] ?? "muted"}>{uRole}</Badge>
                        )}
                      </td>
                      <td className="py-2 pr-3 text-xs">{u.run_count ?? 0}</td>
                      <td className="py-2 pr-3 text-xs">{u.session_count ?? 0}</td>
                      <td className="py-2 text-xs text-muted">{fmtTs(u.last_active_at)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}

export default function TeamPage() {
  const role = useAppStore((s) => s.role);
  const { t } = useI18n();
  if (role != null && !roleAllows("lead", role)) {
    return (
      <div className="mx-auto max-w-5xl p-4">
        <PageHead title={t("team.title", "팀")} />
        <Empty>{t("team.leadOnly", "이 화면은 리드(lead) 이상 전용입니다.")}</Empty>
      </div>
    );
  }
  return <TeamBody />;
}
