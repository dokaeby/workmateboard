/** 사용자 관리 — GET /users + PATCH /users/{id}/role (admin 전용). */
import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { apiGet, apiPatch, ApiError } from "../../../shared/api/client";
import { useI18n } from "../../../shared/i18n";
import { Spinner, ErrorBox, Badge } from "../../../shared/ui";
import { fmtTs } from "../../../shared/lib/format";
import { normalizeRole } from "../lib";
import type { UsersResponse, UserRow } from "../types";

const ROLES = ["member", "lead", "admin"] as const;
const inputCls =
  "rounded-lg border border-border bg-surface px-2 py-1 text-xs text-ink focus:border-accent focus:outline-none";

function roleTone(role: string): "muted" | "accent" | "err" {
  if (role === "admin") return "err";
  if (role === "lead") return "accent";
  return "muted";
}

export default function UsersAdmin({ meId }: { meId?: string }) {
  const { t } = useI18n();
  const qc = useQueryClient();
  const [busyId, setBusyId] = useState<string | null>(null);

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["users"],
    queryFn: () => apiGet<UsersResponse>("/users"),
    staleTime: 30000,
  });

  const users: UserRow[] = Array.isArray(data?.users) ? data!.users! : [];

  const changeRole = async (u: UserRow, role: string) => {
    if (!u.id || role === normalizeRole(u.role)) return;
    setBusyId(u.id);
    try {
      await apiPatch(`/users/${encodeURIComponent(u.id)}/role`, { role });
      await qc.invalidateQueries({ queryKey: ["users"] });
      await qc.invalidateQueries({ queryKey: ["me"] });
    } catch (e) {
      alert(e instanceof ApiError ? e.message : String(e));
      refetch();
    } finally {
      setBusyId(null);
    }
  };

  return (
    <div className="mt-5 border-t border-border pt-4">
      <h3 className="text-sm font-bold text-ink">{t("settings.sec.users", "사용자 관리")}</h3>
      <p className="mb-2 text-xs text-muted">{t("settings.sec.users.desc", "관리자만 볼 수 있습니다. 역할 변경은 즉시 적용됩니다.")}</p>
      {isLoading && <Spinner />}
      {error && <ErrorBox message={error instanceof Error ? error.message : String(error)} onRetry={() => refetch()} />}
      {data && (
        <div className="overflow-x-auto">
          <table className="w-full border-collapse text-sm">
            <thead>
              <tr className="border-b border-border-strong text-left text-xs font-semibold text-muted">
                <th className="px-2 py-1.5">{t("settings.users.email", "이메일")}</th>
                <th className="px-2 py-1.5">{t("settings.users.name", "이름")}</th>
                <th className="px-2 py-1.5">{t("settings.users.role", "역할")}</th>
                <th className="px-2 py-1.5">{t("settings.users.created", "생성일")}</th>
              </tr>
            </thead>
            <tbody>
              {!users.length && (
                <tr><td colSpan={4} className="px-2 py-4 text-center text-muted">—</td></tr>
              )}
              {users.map((u) => {
                const role = normalizeRole(u.role);
                const isMe = !!meId && u.id === meId;
                return (
                  <tr key={u.id} className="border-b border-border last:border-0">
                    <td className="px-2 py-2 font-mono text-xs">
                      {u.email || "-"}
                      {isMe && <Badge tone="accent"> {t("settings.me", "나")}</Badge>}
                    </td>
                    <td className="px-2 py-2">{u.display_name || u.name || "-"}</td>
                    <td className="px-2 py-2">
                      <span className="mr-1.5 inline-flex items-center gap-1">
                        <Badge tone={roleTone(role)}>{role}</Badge>
                        <select
                          className={inputCls}
                          value={role}
                          disabled={busyId === u.id}
                          onChange={(e) => changeRole(u, e.target.value)}
                          aria-label={t("settings.users.role", "역할")}
                        >
                          {ROLES.map((r) => <option key={r} value={r}>{r}</option>)}
                        </select>
                      </span>
                    </td>
                    <td className="px-2 py-2 font-mono text-xs text-muted">{fmtTs(u.created_at).slice(0, 10)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
