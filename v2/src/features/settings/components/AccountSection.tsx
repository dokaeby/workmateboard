/** 계정 섹션 — /auth/me 표시, 비밀번호 변경, 세션 목록/폐기, 데이터 내보내기, 계정 삭제. */
import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { apiGet, apiPost, apiDelete, apiHeaders, ApiError } from "../../../shared/api/client";
import { useI18n } from "../../../shared/i18n";
import { Spinner, ErrorBox, Button, Badge } from "../../../shared/ui";
import { fmtTsShort, normalizeRole } from "../lib";
import type { AuthSession, MeUser, SessionsResponse } from "../types";

const inputCls =
  "w-full rounded-lg border border-border bg-surface px-3 py-2 text-sm text-ink placeholder:text-muted focus:border-accent focus:outline-none";

function Status({ msg, tone }: { msg: string; tone?: "ok" | "err" }) {
  if (!msg) return null;
  return <span className={`text-xs ${tone === "ok" ? "text-ok" : tone === "err" ? "text-err" : "text-muted"}`}>{msg}</span>;
}

function PasswordChange() {
  const { t } = useI18n();
  const [cur, setCur] = useState("");
  const [nw, setNw] = useState("");
  const [cf, setCf] = useState("");
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<{ msg: string; tone?: "ok" | "err" }>({ msg: "" });

  const submit = async () => {
    if (!cur || !nw || !cf) {
      setStatus({ msg: t("settings.pw.need_fields", "모든 항목을 입력하세요"), tone: "err" });
      return;
    }
    if (nw !== cf) {
      setStatus({ msg: t("settings.pw.mismatch", "새 비밀번호가 서로 다릅니다"), tone: "err" });
      return;
    }
    setBusy(true);
    setStatus({ msg: t("settings.pw.working", "변경 중…") });
    try {
      await apiPost("/auth/password", { current_password: cur, new_password: nw });
      setStatus({ msg: t("settings.pw.done", "변경되었습니다 — 다시 로그인하세요"), tone: "ok" });
      setCur(""); setNw(""); setCf("");
      // 서버가 모든 세션을 폐기 — 대시보드로 돌아가 재로그인 유도
      setTimeout(() => { window.location.href = "/"; }, 1500);
    } catch (e) {
      setStatus({ msg: e instanceof Error ? e.message : String(e), tone: "err" });
      setBusy(false);
    }
  };

  return (
    <div>
      <h3 className="text-sm font-bold text-ink">{t("settings.pw.title", "비밀번호 변경")}</h3>
      <p className="mb-2 text-xs text-muted">{t("settings.pw.desc", "변경하면 모든 기기의 세션이 로그아웃됩니다.")}</p>
      <div className="flex flex-wrap gap-2">
        <input type="password" autoComplete="current-password" value={cur} onChange={(e) => setCur(e.target.value)}
          placeholder={t("settings.pw.current", "현재 비밀번호")} className={`${inputCls} min-w-44 flex-1`} />
        <input type="password" autoComplete="new-password" value={nw} onChange={(e) => setNw(e.target.value)}
          placeholder={t("settings.pw.new", "새 비밀번호 (8자 이상)")} className={`${inputCls} min-w-44 flex-1`} />
        <input type="password" autoComplete="new-password" value={cf} onChange={(e) => setCf(e.target.value)}
          placeholder={t("settings.pw.confirm", "새 비밀번호 확인")} className={`${inputCls} min-w-44 flex-1`} />
      </div>
      <div className="mt-2 flex items-center gap-3">
        <Button variant="secondary" className="px-3 py-1 text-xs" disabled={busy} onClick={submit}>
          {t("settings.pw.btn", "변경")}
        </Button>
        <Status msg={status.msg} tone={status.tone} />
      </div>
    </div>
  );
}

function SessionList() {
  const { t } = useI18n();
  const qc = useQueryClient();
  const [revoking, setRevoking] = useState<string | null>(null);
  const [status, setStatus] = useState("");

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["auth-sessions"],
    queryFn: () => apiGet<SessionsResponse>("/auth/sessions"),
    staleTime: 30000,
  });
  const sessions: AuthSession[] = Array.isArray(data?.sessions) ? data!.sessions! : [];

  const revoke = async (s: AuthSession) => {
    setRevoking(s.id);
    try {
      await apiDelete(`/auth/sessions/${encodeURIComponent(s.id)}`);
      setStatus(t("settings.sess.revoke_done", "세션을 폐기했습니다"));
      await qc.invalidateQueries({ queryKey: ["auth-sessions"] });
    } catch (e) {
      alert(e instanceof ApiError ? e.message : String(e));
    } finally {
      setRevoking(null);
    }
  };

  return (
    <div>
      <h3 className="text-sm font-bold text-ink">{t("settings.sess.title", "활성 세션")}</h3>
      <p className="mb-2 text-xs text-muted">{t("settings.sess.desc", "로그인된 기기 목록입니다. 개별 세션을 폐기할 수 있습니다.")}</p>
      {isLoading && <Spinner />}
      {error instanceof ApiError && error.status === 401 ? (
        <div className="py-2 text-sm text-muted">
          {t("settings.sess.api_key_only", "API 키 인증에는 세션 목록이 없습니다 — 브라우저 로그인 세션에서만 표시됩니다.")}
        </div>
      ) : error ? (
        <ErrorBox message={error instanceof Error ? error.message : String(error)} onRetry={() => refetch()} />
      ) : null}
      {data && !sessions.length && <div className="py-2 text-sm text-muted">—</div>}
      <div className="flex flex-col">
        {sessions.map((s) => (
          <div key={s.id} className="flex items-center gap-3 border-b border-border py-2.5 text-sm last:border-0">
            <span className="flex-none font-mono text-xs text-muted">{fmtTsShort(s.created_at)}</span>
            <span className="min-w-0 flex-1 text-xs text-muted">
              {s.current ? (
                <Badge tone="accent">{t("settings.sess.current", "현재 세션")}</Badge>
              ) : (
                <>{t("settings.sess.revoke", "폐기")} · {fmtTsShort(s.expires_at)}</>
              )}
            </span>
            {!s.current && (
              <Button variant="secondary" className="px-2.5 py-1 text-xs"
                disabled={revoking === s.id} onClick={() => revoke(s)}>
                {t("settings.sess.revoke", "폐기")}
              </Button>
            )}
          </div>
        ))}
      </div>
      <Status msg={status} />
    </div>
  );
}

function DataExport() {
  const { t } = useI18n();
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<{ msg: string; tone?: "ok" | "err" }>({ msg: "" });

  const download = async () => {
    setBusy(true);
    setStatus({ msg: t("settings.export.working", "준비 중…") });
    try {
      const r = await fetch("/api/v1/auth/export", { headers: apiHeaders() });
      if (r.status === 401) {
        // API 키 인증은 계정 export 대상이 아님 — 로그아웃시키지 않고 안내만 표시
        setStatus({
          msg: t("settings.export.api_key_only", "API 키 인증에는 계정 데이터 내보내기를 사용할 수 없습니다 — 브라우저 로그인 후 이용하세요."),
          tone: "err",
        });
        return;
      }
      if (!r.ok) {
        const j = await r.json().catch(() => ({})) as { error?: { message?: string } | string };
        const msg = typeof j.error === "object" ? j.error?.message : j.error;
        throw new Error(msg || t("settings.export.fail", "내보내기에 실패했습니다"));
      }
      const blob = await r.blob();
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = "jobmate-data-export.json";
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 5000);
      setStatus({ msg: "" });
    } catch (e) {
      setStatus({ msg: e instanceof Error ? e.message : String(e), tone: "err" });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div>
      <h3 className="text-sm font-bold text-ink">{t("settings.export.title", "데이터 내보내기")}</h3>
      <p className="mb-2 text-xs text-muted">{t("settings.export.desc", "본인 계정에 귀속된 데이터를 JSON으로 다운로드합니다.")}</p>
      <div className="flex items-center gap-3">
        <Button variant="secondary" className="px-3 py-1.5 text-xs" disabled={busy} onClick={download}>
          ⭳ {t("settings.export.btn", "내 데이터 다운로드 (JSON)")}
        </Button>
        <Status msg={status.msg} tone={status.tone} />
      </div>
    </div>
  );
}

export default function AccountSection({ me }: { me: MeUser | null }) {
  const { t } = useI18n();
  const role = normalizeRole(me?.role);
  return (
    <section className="rounded-xl border border-border bg-surface p-4 shadow-sm">
      <h2 className="text-base font-semibold text-ink">{t("settings.sec.account", "계정")}</h2>
      <p className="mb-3 text-sm text-muted">{t("settings.sec.account.desc", "로그인 정보, 비밀번호, 활성 세션과 데이터 내보내기를 관리합니다.")}</p>
      <div className="grid gap-4 sm:grid-cols-3">
        <div>
          <div className="mb-1 text-xs font-semibold text-muted">{t("settings.acct.email", "이메일")}</div>
          <div className="font-mono text-sm text-ink">{me?.email || "-"}</div>
        </div>
        <div>
          <div className="mb-1 text-xs font-semibold text-muted">{t("settings.acct.name", "이름")}</div>
          <div className="text-sm text-ink">{me?.display_name || me?.name || "-"}</div>
        </div>
        <div>
          <div className="mb-1 text-xs font-semibold text-muted">{t("settings.acct.role", "역할")}</div>
          <Badge tone={role === "admin" ? "err" : role === "lead" ? "accent" : "muted"}>{role}</Badge>
        </div>
      </div>
      <div className="mt-4 space-y-5 border-t border-border pt-4">
        <PasswordChange />
        <SessionList />
        <DataExport />
      </div>
    </section>
  );
}
