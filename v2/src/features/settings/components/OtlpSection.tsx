/** OTLP 인제스트 토큰 — spec 008 US1.
 *  GET/POST /otlp/tokens, POST /otlp/tokens/{id}/revoke.
 *  토큰 원문은 발급 응답에만 1회 포함 — 생성 즉시 복사 블록으로 보여주고
 *  목록에는 prefix만 표시한다(토큰 원문은 절대 다시 조회/로깅하지 않는다). */
import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { apiGet, apiPost, ApiError } from "../../../shared/api/client";
import { useI18n } from "../../../shared/i18n";
import { useAppStore, roleAllows } from "../../../shared/store/appStore";
import { Card, Button, Badge, Spinner, ErrorBox, Empty } from "../../../shared/ui";
import { fmtTs } from "../../../shared/lib/format";

interface OtlpToken {
  id: string;
  name?: string;
  prefix?: string;
  created_at?: string;
  last_used_at?: string | null;
  revoked_at?: string | null;
}
interface OtlpTokensResponse { items?: OtlpToken[] }
interface CreatedToken extends OtlpToken { token?: string }

const inputCls =
  "rounded-lg border border-border bg-surface px-3 py-2 text-sm text-ink focus:border-accent focus:outline-none";

export default function OtlpSection() {
  const { t } = useI18n();
  const qc = useQueryClient();
  const role = useAppStore((s) => s.role);
  const canLead = roleAllows("lead", role); // /otlp/tokens는 lead+ 게이트
  const [name, setName] = useState("");
  const [busyCreate, setBusyCreate] = useState(false);
  const [busyRevoke, setBusyRevoke] = useState(false);
  const [err, setErr] = useState("");
  const [created, setCreated] = useState<CreatedToken | null>(null);
  const [copied, setCopied] = useState(false);
  const [pendingRevoke, setPendingRevoke] = useState<OtlpToken | null>(null);
  const dialogRef = useRef<HTMLDialogElement>(null);

  const tokensQ = useQuery({
    queryKey: ["otlp-tokens"],
    queryFn: () => apiGet<OtlpTokensResponse>("/otlp/tokens"),
    enabled: canLead,
    staleTime: 30000,
  });
  const items = tokensQ.data?.items || [];

  // 네이티브 <dialog> — showModal()이 inert/포커스/Esc/포커스 복귀를 처리한다.
  useEffect(() => {
    const d = dialogRef.current;
    if (!d) return;
    if (pendingRevoke && !d.open) d.showModal();
    if (!pendingRevoke && d.open) d.close();
    const sync = () => setPendingRevoke(null);
    d.addEventListener("close", sync);
    return () => d.removeEventListener("close", sync);
  }, [pendingRevoke]);

  const invalidate = () => qc.invalidateQueries({ queryKey: ["otlp-tokens"] });

  const create = async () => {
    const n = name.trim();
    if (!n || busyCreate) return;
    setBusyCreate(true);
    setErr("");
    try {
      const res = await apiPost<CreatedToken>("/otlp/tokens", { name: n });
      setCreated(res);
      setCopied(false);
      setName("");
      await invalidate();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusyCreate(false);
    }
  };

  const copyToken = async () => {
    const tok = created?.token;
    if (!tok) return;
    try {
      await navigator.clipboard.writeText(tok);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  };

  const doRevoke = async () => {
    const target = pendingRevoke;
    if (!target || busyRevoke) return;
    setBusyRevoke(true);
    try {
      await apiPost(`/otlp/tokens/${encodeURIComponent(target.id)}/revoke`);
      dialogRef.current?.close();
      await invalidate();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e));
      dialogRef.current?.close();
    } finally {
      setBusyRevoke(false);
    }
  };

  // member 이하 — 토큰 발급/조회/폐기가 모두 lead+ 게이트이므로 섹션을 숨긴다.
  if (!canLead) return null;

  return (
    <Card title={t("settings.sec.otlp", "OTLP 인제스트")} className="mb-4">
      <p className="mb-3 text-sm text-muted">
        {t("settings.sec.otlp.desc", "외부 에이전트/LLM 호출을 OTLP로 수집할 때 사용하는 인제스트 토큰입니다. 토큰 원문은 발급 시 한 번만 표시됩니다.")}
      </p>

      {tokensQ.isLoading && <Spinner />}
      {tokensQ.error && (
        <ErrorBox message={tokensQ.error instanceof Error ? tokensQ.error.message : String(tokensQ.error)} onRetry={() => tokensQ.refetch()} />
      )}
      {tokensQ.data && !items.length && <Empty>{t("settings.otlp.empty", "발급된 토큰이 없습니다")}</Empty>}

      {items.length > 0 && (
        <div className="mb-3 overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border text-left text-xs text-muted">
                <th className="py-1.5 pr-3">{t("settings.otlp.name", "이름")}</th>
                <th className="py-1.5 pr-3">{t("settings.otlp.prefix", "접두사")}</th>
                <th className="py-1.5 pr-3">{t("settings.otlp.created", "발급일")}</th>
                <th className="py-1.5 pr-3">{t("settings.otlp.lastUsed", "최근 사용")}</th>
                <th className="py-1.5 pr-3">{t("settings.otlp.status", "상태")}</th>
                <th className="py-1.5" />
              </tr>
            </thead>
            <tbody>
              {items.map((tk) => {
                const revoked = !!tk.revoked_at;
                return (
                  <tr key={tk.id} className="border-b border-border last:border-0">
                    <td className="py-1.5 pr-3 font-medium text-ink">{tk.name || tk.id}</td>
                    <td className="py-1.5 pr-3 font-mono text-xs text-muted">{tk.prefix || "-"}</td>
                    <td className="py-1.5 pr-3 text-xs text-muted">{fmtTs(tk.created_at)}</td>
                    <td className="py-1.5 pr-3 text-xs text-muted">{fmtTs(tk.last_used_at)}</td>
                    <td className="py-1.5 pr-3">
                      {revoked
                        ? <Badge tone="err">{t("settings.otlp.revoked", "폐기됨")}</Badge>
                        : <Badge tone="ok">{t("settings.otlp.active", "사용 중")}</Badge>}
                    </td>
                    <td className="py-1.5 text-right">
                      {!revoked && (
                        <Button variant="ghost" className="px-2 py-0.5 text-xs text-err"
                          onClick={() => setPendingRevoke(tk)}>
                          {t("settings.otlp.revoke", "폐기")}
                        </Button>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {/* 발급 폼 */}
      <div className="flex flex-wrap items-center gap-2">
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder={t("settings.otlp.namePh", "토큰 이름 (예: prod-agent)")}
          autoComplete="off"
          className={`${inputCls} min-w-48 flex-1`}
          onKeyDown={(e) => { if (e.key === "Enter") void create(); }}
        />
        <Button className="px-3 py-1.5 text-xs" disabled={busyCreate || !name.trim()} onClick={create}>
          {busyCreate ? t("settings.saving", "저장 중…") : t("settings.otlp.create", "토큰 발급")}
        </Button>
      </div>
      {err && <div className="mt-2 text-xs text-err">{err}</div>}

      {/* 발급 직후 — 토큰 원문 1회 표시 */}
      {created?.token && (
        <div className="mt-3 rounded-lg border border-warn-border bg-warn-soft p-3">
          <div className="mb-1 text-xs font-semibold text-warn">
            {t("settings.otlp.onceWarn", "토큰 원문은 지금 한 번만 표시됩니다 — 안전한 곳에 보관하세요")}
          </div>
          <div className="flex items-center gap-2">
            <code className="min-w-0 flex-1 overflow-x-auto rounded bg-surface px-2 py-1.5 font-mono text-xs text-ink">
              {created.token}
            </code>
            <Button variant="secondary" className="shrink-0 px-2.5 py-1 text-xs" onClick={copyToken}>
              {copied ? t("settings.otlp.copied", "복사됨") : t("settings.otlp.copy", "복사")}
            </Button>
          </div>
          <button
            className="mt-2 text-xs text-muted underline-offset-2 hover:underline"
            onClick={() => setCreated(null)}
          >
            {t("settings.otlp.dismiss", "확인했습니다 — 닫기")}
          </button>
        </div>
      )}

      {/* 폐기 확인 — 네이티브 dialog */}
      <dialog
        ref={dialogRef}
        aria-labelledby="otlp-revoke-title"
        className="rounded-xl border border-border bg-surface p-5 text-ink backdrop:bg-black/30"
      >
        <h3 id="otlp-revoke-title" className="mb-2 text-sm font-semibold">
          {t("settings.otlp.revokeTitle", "OTLP 토큰을 폐기할까요?")}
        </h3>
        <p className="mb-4 text-sm text-muted">
          {t("settings.otlp.revokeBody", "'{name}' 토큰이 즉시 무효화됩니다. 이 작업은 되돌릴 수 없습니다.")
            .replace("{name}", pendingRevoke?.name || pendingRevoke?.prefix || "")}
        </p>
        <div className="flex justify-end gap-2">
          <Button variant="secondary" className="px-3 py-1 text-xs" onClick={() => dialogRef.current?.close()}>
            {t("settings.danger.cancel", "취소")}
          </Button>
          <Button variant="danger" className="px-3 py-1 text-xs" disabled={busyRevoke} onClick={doRevoke}>
            {busyRevoke ? t("settings.saving", "저장 중…") : t("settings.otlp.revoke", "폐기")}
          </Button>
        </div>
      </dialog>
    </Card>
  );
}
