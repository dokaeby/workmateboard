/** 알림함 — 리포트 구독 (spec 008 US3).
 *  GET/POST/PATCH/DELETE /reports/subscriptions + POST /{id}/test — 모두 lead+ 게이트.
 *  webhook 채널은 생성/시크릿 재발급 PATCH 응답에 `secret`이 1회 포함된다 —
 *  즉시 복사 블록으로 보여주고 이후 상태에서는 절대 다시 표시하지 않는다.
 *  target 표시는 이메일만 원문, slack/webhook은 호스트만 노출한다
 *  (URL 자격증명·경로는 DOM/tooltip 어디에도 넣지 않는다). */
import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { apiGet, apiPost, apiPatch, apiDelete, ApiError } from "../../shared/api/client";
import { useI18n } from "../../shared/i18n";
import { useAppStore, roleAllows } from "../../shared/store/appStore";
import { Card, Empty, Spinner, ErrorBox, Badge, Button } from "../../shared/ui";
import { fmtTs } from "../../shared/lib/format";

interface ReportSubscription {
  id: string;
  kind?: string; // "daily" | "weekly"
  channel?: string; // "email" | "slack" | "webhook"
  target?: string;
  enabled?: boolean;
  last_sent_at?: string | null;
  created_at?: string;
  updated_at?: string;
}
interface SubsResponse { items?: ReportSubscription[] }
interface SubMutationResponse { secret?: string }
interface TestResult { ok?: boolean; detail?: string }

const inputCls =
  "rounded-lg border border-border bg-surface px-2.5 py-1.5 text-sm outline-none focus:border-accent";
const KINDS = ["daily", "weekly"] as const;
const CHANNELS = ["email", "slack", "webhook"] as const;

/** 표시용 target — 이메일은 원문, slack/webhook은 호스트만.
 *  파싱 실패 시 원문을 노출하지 않고 "-"를 반환한다. */
function targetLabel(s: ReportSubscription): string {
  const target = String(s.target || "");
  if (s.channel === "email") return target || "-";
  if (s.channel === "slack" || s.channel === "webhook") {
    try {
      return new URL(target).host || "-";
    } catch {
      return "-";
    }
  }
  return "-";
}

function channelLabel(t: (k: string, f?: string) => string, ch?: string): string {
  switch (ch) {
    case "email": return t("inbox.sub.chEmail", "이메일");
    case "slack": return "Slack";
    case "webhook": return "Webhook";
    default: return ch || "-";
  }
}

function kindLabel(t: (k: string, f?: string) => string, kind?: string): string {
  switch (kind) {
    case "daily": return t("inbox.rptDaily", "일일");
    case "weekly": return t("inbox.rptWeekly", "주간");
    default: return kind || "-";
  }
}

/** 구독 생성/편집 폼 — PATCH는 kind/channel/target/enabled를 함께 보낸다.
 *  응답에 secret이 있으면(웹훅 채널) onSecret으로 올려 1회 표시한다. */
function SubForm({
  initial,
  onDone,
  onCancel,
  onSecret,
}: {
  initial?: ReportSubscription | null;
  onDone: () => void;
  onCancel: () => void;
  onSecret: (secret: string) => void;
}) {
  const { t } = useI18n();
  const [kind, setKind] = useState<string>(initial?.kind || "daily");
  const [channel, setChannel] = useState<string>(initial?.channel || "email");
  const [target, setTarget] = useState(initial?.target || "");
  const [enabled, setEnabled] = useState(initial?.enabled !== false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const isEdit = !!initial?.id;

  const submit = async () => {
    if (!target.trim() || busy) return;
    setBusy(true);
    setErr("");
    const body = {
      kind,
      channel,
      target: target.trim(),
      enabled,
    };
    try {
      const res = isEdit
        ? await apiPatch<SubMutationResponse>(
            `/reports/subscriptions/${encodeURIComponent(initial!.id)}`, body)
        : await apiPost<SubMutationResponse>("/reports/subscriptions", body);
      if (res?.secret) onSecret(res.secret);
      onDone();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mb-3 space-y-2 rounded-lg border border-border p-3">
      <div className="flex flex-wrap items-center gap-2">
        <select value={kind} onChange={(e) => setKind(e.target.value)} className={inputCls} aria-label={t("inbox.sub.kind", "주기")}>
          {KINDS.map((k) => <option key={k} value={k}>{kindLabel(t, k)}</option>)}
        </select>
        <select value={channel} onChange={(e) => setChannel(e.target.value)} className={inputCls} aria-label={t("inbox.sub.channel", "채널")}>
          {CHANNELS.map((c) => <option key={c} value={c}>{channelLabel(t, c)}</option>)}
        </select>
        <input
          value={target}
          onChange={(e) => setTarget(e.target.value)}
          placeholder={channel === "email" ? t("inbox.sub.targetEmail", "name@example.com") : t("inbox.sub.targetUrl", "https://hooks.example.com/...")}
          autoComplete="off"
          className={`${inputCls} min-w-48 flex-1`}
          onKeyDown={(e) => { if (e.key === "Enter") void submit(); }}
        />
        <Button className="px-3 py-1 text-xs" disabled={busy || !target.trim()} onClick={submit}>
          {busy
            ? t("inbox.saving", "저장 중…")
            : isEdit ? t("common.save", "저장") : t("inbox.sub.create", "추가")}
        </Button>
        <Button variant="secondary" className="px-3 py-1 text-xs" onClick={onCancel}>
          {t("common.cancel", "취소")}
        </Button>
      </div>
      <label className="flex items-center gap-2 text-xs text-muted">
        <input
          type="checkbox"
          className="accent-accent"
          checked={enabled}
          onChange={(e) => setEnabled(e.target.checked)}
        />
        {t("inbox.sub.enabledField", "활성화")}
      </label>
      {err && <div className="text-xs text-err">{err}</div>}
    </div>
  );
}

export default function ReportSubsCard() {
  const { t } = useI18n();
  const qc = useQueryClient();
  const role = useAppStore((s) => s.role);
  const canLead = roleAllows("lead", role);
  const [showForm, setShowForm] = useState(false);
  const [editing, setEditing] = useState<ReportSubscription | null>(null);
  const [secretOnce, setSecretOnce] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [busyTestId, setBusyTestId] = useState<string | null>(null);
  const [testMsg, setTestMsg] = useState<Record<string, string>>({});

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["report-subs"],
    queryFn: () => apiGet<SubsResponse>("/reports/subscriptions"),
    enabled: canLead, // GET 자체가 lead+ 게이트 — member는 카드 자체를 렌더하지 않는다
    refetchInterval: 60000,
  });
  const items = data?.items || [];

  const invalidate = () => qc.invalidateQueries({ queryKey: ["report-subs"] });

  const copySecret = async () => {
    if (!secretOnce) return;
    try {
      await navigator.clipboard.writeText(secretOnce);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  };

  const toggle = async (s: ReportSubscription) => {
    try {
      await apiPatch(`/reports/subscriptions/${encodeURIComponent(s.id)}`, { enabled: !(s.enabled !== false) });
      await invalidate();
    } catch (e) {
      alert(e instanceof ApiError ? e.message : String(e));
    }
  };

  const test = async (s: ReportSubscription) => {
    if (busyTestId) return; // in-flight 중복 호출 방지
    setBusyTestId(s.id);
    setTestMsg((m) => ({ ...m, [s.id]: "…" }));
    try {
      const r = await apiPost<TestResult>(`/reports/subscriptions/${encodeURIComponent(s.id)}/test`);
      setTestMsg((m) => ({
        ...m,
        [s.id]: r?.ok ? t("inbox.sub.testOk", "테스트 전송 성공") : (r?.detail || t("inbox.sub.testFail", "테스트 전송 실패")),
      }));
    } catch (e) {
      setTestMsg((m) => ({ ...m, [s.id]: e instanceof ApiError ? e.message : String(e) }));
    } finally {
      setBusyTestId(null);
    }
  };

  const remove = async (s: ReportSubscription) => {
    if (!window.confirm(t("inbox.sub.confirmDelete", "이 구독을 삭제할까요?"))) return;
    try {
      await apiDelete(`/reports/subscriptions/${encodeURIComponent(s.id)}`);
      await invalidate();
    } catch (e) {
      alert(e instanceof ApiError ? e.message : String(e));
    }
  };

  // member 이하 — 모든 엔드포인트가 lead+ 게이트이므로 카드 자체를 숨긴다.
  if (!canLead) return null;

  return (
    <Card
      title={t("inbox.sub.title", "리포트 구독")}
      className="mb-3"
      actions={
        <Button
          variant="secondary"
          className="px-3 py-1 text-xs"
          onClick={() => { setEditing(null); setShowForm((v) => !v); }}
        >
          {showForm && !editing ? t("inbox.close", "닫기") : "+ " + t("inbox.sub.add", "구독 추가")}
        </Button>
      }
    >
      <p className="mb-3 text-sm text-muted">
        {t("inbox.sub.desc", "생성된 일일/주간 리포트를 이메일·Slack·웹훅으로 자동 전달합니다.")}
      </p>

      {/* webhook 시크릿 — 생성/재발급 응답에만 1회 포함. 닫으면 폐기. */}
      {secretOnce && (
        <div className="mb-3 rounded-lg border border-warn-border bg-warn-soft p-3">
          <div className="mb-1 text-xs font-semibold text-warn">
            {t("inbox.sub.secretOnce", "웹훅 서명 시크릿은 지금 한 번만 표시됩니다 — 안전한 곳에 보관하세요")}
          </div>
          <div className="flex items-center gap-2">
            <code className="min-w-0 flex-1 overflow-x-auto rounded bg-surface px-2 py-1.5 font-mono text-xs text-ink">
              {secretOnce}
            </code>
            <Button variant="secondary" className="shrink-0 px-2.5 py-1 text-xs" onClick={copySecret}>
              {copied ? t("inbox.sub.copied", "복사됨") : t("inbox.sub.copy", "복사")}
            </Button>
          </div>
          <button
            className="mt-2 text-xs text-muted underline-offset-2 hover:underline"
            onClick={() => { setSecretOnce(null); setCopied(false); }}
          >
            {t("inbox.sub.secretDismiss", "확인했습니다 — 닫기")}
          </button>
        </div>
      )}

      {(showForm || editing) && (
        <SubForm
          key={editing?.id ?? "new"}
          initial={editing}
          onSecret={(s) => { setSecretOnce(s); setCopied(false); }}
          onDone={() => { setShowForm(false); setEditing(null); }}
          onCancel={() => { setShowForm(false); setEditing(null); }}
        />
      )}

      {isLoading && <Spinner />}
      {error && <ErrorBox message={error instanceof Error ? error.message : String(error)} onRetry={() => refetch()} />}
      {data && !items.length && <Empty>{t("inbox.sub.empty", "등록된 구독이 없습니다")}</Empty>}

      <div className="space-y-1.5">
        {items.map((s) => {
          const enabled = s.enabled !== false;
          const label = targetLabel(s);
          return (
            <div key={s.id} className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-md border border-border px-2.5 py-1.5 text-xs">
              <Badge tone="accent">{kindLabel(t, s.kind)}</Badge>
              <Badge tone="muted">{channelLabel(t, s.channel)}</Badge>
              {/* target 원문은 절대 DOM/tooltip에 넣지 않는다 — 마스킹된 label만 표시 */}
              <span className="min-w-0 flex-1 truncate font-medium text-ink">
                {label}
              </span>
              <span className="text-muted">
                {t("inbox.sub.lastSent", "마지막 전송")} {s.last_sent_at ? fmtTs(s.last_sent_at) : "-"}
              </span>
              {testMsg[s.id] && <span className="text-muted">{testMsg[s.id]}</span>}
              <label className="inline-flex items-center gap-1 text-muted">
                <input
                  type="checkbox"
                  className="accent-accent"
                  checked={enabled}
                  onChange={() => void toggle(s)}
                />
                {enabled ? t("inbox.cardOn", "사용") : t("inbox.cardOff", "중지")}
              </label>
              <Button
                variant="ghost"
                className="px-2 py-0.5 text-xs"
                disabled={busyTestId === s.id}
                onClick={() => void test(s)}
              >
                {busyTestId === s.id ? "…" : t("inbox.sub.test", "테스트")}
              </Button>
              <Button
                variant="ghost"
                className="px-2 py-0.5 text-xs"
                onClick={() => { setEditing(s); setShowForm(false); }}
              >
                {t("inbox.sub.edit", "편집")}
              </Button>
              <Button variant="ghost" className="px-2 py-0.5 text-xs text-err" onClick={() => void remove(s)}>
                {t("common.delete", "삭제")}
              </Button>
            </div>
          );
        })}
      </div>
    </Card>
  );
}
