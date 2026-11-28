/** 알림 규칙 — GET/POST/PATCH/DELETE /alert-rules + /alert-fires 이력.
 *  spec 007 US4. lead 이상 게이트는 nav/서버에서 강제 — UI는 CRUD만 담당. */
import { useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { apiGet, apiPost, apiPatch, apiDelete, ApiError } from "../../shared/api/client";
import { useI18n } from "../../shared/i18n";
import { PageHead, Card, Empty, Spinner, ErrorBox, Button, Badge } from "../../shared/ui";
import { fmtTs, fmtAge } from "../../shared/lib/format";
import "./i18n";

// ── 타입 (백엔드 스키마: services/alert_rules.py CONDITION_FIELDS 기준) ──
type ChannelEntry = string | { type?: string; channel?: string; to?: string; target?: string; status?: string };

interface AlertCondition {
  field?: string;
  op?: string;
  value?: unknown;
}
interface AlertRule {
  id: string;
  name?: string;
  enabled?: boolean;
  condition?: AlertCondition;
  channels?: ChannelEntry[];
  cooldown_minutes?: number;
  last_fired_at?: string | null;
  created_by?: string;
  created_at?: string;
}
interface AlertRulesResponse { items?: AlertRule[] }
interface AlertFire {
  id: string;
  rule_id?: string;
  event_fingerprint?: string;
  status?: string;
  channels?: ChannelEntry[];
  created_at?: string;
}
interface AlertFiresResponse { items?: AlertFire[] }

const FIELD_GROUPS: { group: string; fields: string[] }[] = [
  { group: "al.grpRun", fields: ["run.status", "run.workflow", "run.project", "run.cost_usd", "run.duration_ms", "run.latency_ms"] },
  { group: "al.grpStage", fields: ["stage.status", "stage.key"] },
  { group: "al.grpBudget", fields: ["budget.used_pct", "budget.scope", "budget.threshold"] },
  { group: "al.grpSchedule", fields: ["schedule.missed", "schedule.id", "schedule.workflow"] },
  { group: "al.grpError", fields: ["error.error_class", "error.rate_pct"] },
  { group: "al.grpSession", fields: ["session.stale_count"] },
  { group: "al.grpCost", fields: ["cost.delta_pct"] },
];

const OPS = ["eq", "ne", "gt", "gte", "lt", "lte", "in", "contains"] as const;
const NUMERIC_OPS = new Set(["gt", "gte", "lt", "lte"]);
const CHANNELS = ["inbox", "email", "slack", "telegram", "push"] as const;

const OP_LABEL: Record<string, string> = {
  eq: "al.opEq", ne: "al.opNe", gt: "al.opGt", gte: "al.opGte",
  lt: "al.opLt", lte: "al.opLte", in: "al.opIn", contains: "al.opContains",
};

function channelName(c: ChannelEntry): string {
  // rule channels: {type,to}; fire channels: {channel,status}
  if (typeof c === "string") return c;
  return String(c?.type || c?.channel || "");
}
function channelTarget(c: ChannelEntry): string {
  if (typeof c !== "object" || !c) return "";
  return String(c.to || c.target || "");
}
function channelLabel(c: ChannelEntry): string {
  const n = channelName(c);
  const to = channelTarget(c);
  return to ? `${n}:${to}` : n;
}

function condSummary(cond: AlertCondition | undefined): string {
  if (!cond?.field) return "-";
  const v = Array.isArray(cond.value) ? cond.value.join(",") : String(cond.value ?? "");
  return `${cond.field} ${cond.op || "eq"} ${v}`;
}

/** 폼 입력 문자열 → condition.value 타입 복원. */
function parseValue(raw: string, op: string): unknown {
  const s = raw.trim();
  if (op === "in") {
    return s.split(",").map((x) => x.trim()).filter(Boolean);
  }
  if (NUMERIC_OPS.has(op)) {
    const n = Number(s);
    return isNaN(n) ? s : n;
  }
  if (op === "contains") return s;
  if (/^-?\d+(\.\d+)?$/.test(s)) return Number(s);
  if (s === "true") return true;
  if (s === "false") return false;
  return s;
}

function valueToInput(v: unknown): string {
  if (Array.isArray(v)) return v.join(", ");
  if (v == null) return "";
  return String(v);
}

interface FormState {
  name: string;
  field: string;
  op: string;
  value: string;
  channels: Set<string>;
  /** 채널별 기존 수신 대상({type,to}) — 편집 시 보존 */
  channelTargets: Record<string, string>;
  /** 신규 채널 공통 대상 입력 (비어 있으면 기존 값/기본값 사용) */
  channelTo: string;
  cooldown: string;
  enabled: boolean;
}

function RuleForm({ initial, onDone }: { initial: AlertRule | null; onDone: () => void }) {
  const { t } = useI18n();
  const qc = useQueryClient();
  const [f, setF] = useState<FormState>(() => ({
    name: initial?.name || "",
    field: initial?.condition?.field || "run.status",
    op: initial?.condition?.op || "eq",
    value: valueToInput(initial?.condition?.value),
    channels: new Set((initial?.channels || []).map(channelName).filter(Boolean)),
    channelTargets: Object.fromEntries(
      (initial?.channels || [])
        .map((c) => [channelName(c), channelTarget(c)] as const)
        .filter(([n, to]) => n && to),
    ),
    channelTo: "",
    cooldown: String(initial?.cooldown_minutes ?? 60),
    enabled: initial?.enabled !== false,
  }));
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  const set = <K extends keyof FormState>(k: K, v: FormState[K]) => setF((p) => ({ ...p, [k]: v }));
  const toggleCh = (c: string) => {
    setF((p) => {
      const next = new Set(p.channels);
      if (next.has(c)) next.delete(c); else next.add(c);
      return { ...p, channels: next };
    });
  };

  const submit = async () => {
    if (!f.name.trim()) { setErr(t("al.needName", "규칙 이름을 입력하세요")); return; }
    if (!f.field) { setErr(t("al.needField", "조건 필드를 선택하세요")); return; }
    if (!f.value.trim()) { setErr(t("al.needValue", "조건 값을 입력하세요")); return; }
    setBusy(true);
    setErr("");
    try {
      // {type,to} 대상 보존 — 새 입력값 > 기존 채널 대상 > 없음(문자열 채널)
      const toInput = f.channelTo.trim();
      const channels = [...f.channels].map((name) => {
        const to = toInput || f.channelTargets[name] || "";
        return to && name !== "inbox" ? { type: name, to } : name;
      });
      const body = {
        name: f.name.trim(),
        condition: { field: f.field, op: f.op, value: parseValue(f.value, f.op) },
        channels,
        cooldown_minutes: Math.max(0, Math.min(10080, parseInt(f.cooldown, 10) || 0)),
        enabled: f.enabled,
      };
      if (initial?.id) {
        await apiPatch(`/alert-rules/${encodeURIComponent(initial.id)}`, body);
      } else {
        await apiPost("/alert-rules", body);
      }
      qc.invalidateQueries({ queryKey: ["alert-rules"] });
      qc.invalidateQueries({ queryKey: ["alert-fires"] });
      onDone();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-3 rounded-lg border border-border bg-surface-hover p-3">
      {err && <div className="rounded-md border border-err-border bg-err-soft p-2 text-xs text-err">{err}</div>}
      <div className="grid gap-2 sm:grid-cols-2">
        <label className="block">
          <span className="mb-0.5 block text-xs text-muted">{t("al.name", "이름")}</span>
          <input
            value={f.name}
            onChange={(e) => set("name", e.target.value)}
            placeholder={t("al.namePh", "규칙 이름")}
            className="w-full rounded-md border border-border bg-surface px-2 py-1.5 text-sm"
          />
        </label>
        <label className="block">
          <span className="mb-0.5 block text-xs text-muted">{t("al.cooldown", "쿨다운 (분)")}</span>
          <input
            type="number" min={0} max={10080}
            value={f.cooldown}
            onChange={(e) => set("cooldown", e.target.value)}
            className="w-full rounded-md border border-border bg-surface px-2 py-1.5 text-sm"
          />
        </label>
      </div>

      <div>
        <span className="mb-0.5 block text-xs text-muted">{t("al.condition", "조건")}</span>
        <div className="flex flex-wrap items-center gap-2">
          <select
            value={f.field}
            onChange={(e) => set("field", e.target.value)}
            className="rounded-md border border-border bg-surface px-2 py-1.5 text-sm"
          >
            {FIELD_GROUPS.map((g) => (
              <optgroup key={g.group} label={t(g.group, g.group)}>
                {g.fields.map((fld) => <option key={fld} value={fld}>{fld}</option>)}
              </optgroup>
            ))}
          </select>
          <select
            value={f.op}
            onChange={(e) => set("op", e.target.value)}
            className="rounded-md border border-border bg-surface px-2 py-1.5 text-sm"
          >
            {OPS.map((op) => <option key={op} value={op}>{t(OP_LABEL[op], op)}</option>)}
          </select>
          <input
            value={f.value}
            onChange={(e) => set("value", e.target.value)}
            placeholder={t("al.valuePh", "값 (예: failed, 0.8, 5000)")}
            className="min-w-40 flex-1 rounded-md border border-border bg-surface px-2 py-1.5 text-sm"
          />
        </div>
        {f.op === "in" && (
          <div className="mt-1 text-[11px] text-muted">{t("al.inHint", "in 연산자는 쉼표로 구분해 여러 값을 입력하세요 (예: failed,error)")}</div>
        )}
      </div>

      <div>
        <span className="mb-0.5 block text-xs text-muted">{t("al.channels", "채널")}</span>
        <div className="flex flex-wrap gap-2">
          {CHANNELS.map((c) => (
            <label key={c} className="flex items-center gap-1 text-xs text-ink">
              <input type="checkbox" checked={f.channels.has(c)} onChange={() => toggleCh(c)} />
              {t(`al.channel${c[0].toUpperCase()}${c.slice(1)}`, c)}
            </label>
          ))}
        </div>
        <input
          type="text"
          value={f.channelTo}
          onChange={(e) => set("channelTo", e.target.value)}
          placeholder={t("al.channelToPh", "채널 대상 (예: ops@corp.com, #alerts) — 비우면 기본 수신자")}
          className="mt-1.5 w-full max-w-md rounded-md border border-border bg-surface px-2 py-1 text-xs"
        />
      </div>

      <label className="flex items-center gap-1.5 text-xs text-ink">
        <input type="checkbox" checked={f.enabled} onChange={(e) => set("enabled", e.target.checked)} />
        {t("al.enabled", "활성")}
      </label>

      <div className="flex justify-end gap-2">
        <Button variant="secondary" className="px-3 py-1 text-xs" onClick={onDone} disabled={busy}>
          {t("al.cancel", "취소")}
        </Button>
        <Button className="px-3 py-1 text-xs" disabled={busy} onClick={submit}>
          {busy ? t("al.saving", "저장 중…") : t("al.save", "저장")}
        </Button>
      </div>
    </div>
  );
}

const FIRE_TONE: Record<string, "ok" | "warn" | "err" | "accent" | "muted"> = {
  fired: "accent", sent: "ok", partial: "warn", failed: "err", error: "err",
};

export default function AlertsPage() {
  const { t } = useI18n();
  const qc = useQueryClient();
  const [editing, setEditing] = useState<AlertRule | null | "new">(null);

  const rules = useQuery({
    queryKey: ["alert-rules"],
    queryFn: () => apiGet<AlertRulesResponse>("/alert-rules"),
    refetchInterval: 30000,
  });
  const fires = useQuery({
    queryKey: ["alert-fires"],
    queryFn: () => apiGet<AlertFiresResponse>("/alert-fires?limit=50"),
    refetchInterval: 30000,
  });

  const ruleById = useMemo(() => {
    const m = new Map<string, AlertRule>();
    for (const r of rules.data?.items || []) if (r.id) m.set(r.id, r);
    return m;
  }, [rules.data]);

  const toggleEnabled = async (r: AlertRule) => {
    try {
      await apiPatch(`/alert-rules/${encodeURIComponent(r.id)}`, { enabled: !r.enabled });
      qc.invalidateQueries({ queryKey: ["alert-rules"] });
    } catch (e) {
      alert(e instanceof ApiError ? e.message : String(e));
    }
  };

  const remove = async (r: AlertRule) => {
    if (!confirm(t("al.confirmDelete", "알림 규칙 '{name}'을(를) 삭제할까요?").replace("{name}", r.name || r.id))) return;
    try {
      await apiDelete(`/alert-rules/${encodeURIComponent(r.id)}`);
      qc.invalidateQueries({ queryKey: ["alert-rules"] });
      qc.invalidateQueries({ queryKey: ["alert-fires"] });
    } catch (e) {
      alert(e instanceof ApiError ? e.message : String(e));
    }
  };

  const items = rules.data?.items || [];
  const fireItems = fires.data?.items || [];

  return (
    <div className="mx-auto max-w-5xl p-4">
      <PageHead title={t("al.title", "알림 규칙")} desc={t("al.desc", "alert rule CRUD + 발화 이력 — 리드 이상 권한")}>
        <Button variant="secondary" className="px-3 py-1 text-xs" onClick={() => rules.refetch()}>
          {t("common.refresh", "새로고침")}
        </Button>
        {editing === null && (
          <Button className="px-3 py-1 text-xs" onClick={() => setEditing("new")}>
            + {t("al.add", "규칙 추가")}
          </Button>
        )}
      </PageHead>

      {editing !== null && (
        <Card title={editing === "new" ? t("al.add", "규칙 추가") : t("al.edit", "수정")} className="mb-3">
          <RuleForm
            initial={editing === "new" ? null : editing}
            onDone={() => setEditing(null)}
          />
        </Card>
      )}

      <Card title={t("al.rules", "규칙")}>
        {rules.isLoading && <Spinner />}
        {rules.error && (
          <ErrorBox message={rules.error instanceof Error ? rules.error.message : String(rules.error)} onRetry={() => rules.refetch()} />
        )}
        {rules.data && !items.length && <Empty>{t("al.empty", "등록된 알림 규칙이 없습니다")}</Empty>}
        {items.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border text-left text-xs text-muted">
                  <th className="py-1.5 pr-3">{t("al.enabled", "활성")}</th>
                  <th className="py-1.5 pr-3">{t("al.name", "이름")}</th>
                  <th className="py-1.5 pr-3">{t("al.condition", "조건")}</th>
                  <th className="py-1.5 pr-3">{t("al.channels", "채널")}</th>
                  <th className="py-1.5 pr-3">{t("al.lastFired", "최근 발화")}</th>
                  <th className="py-1.5 pr-3 text-right">{t("al.fires", "발화")}</th>
                  <th className="py-1.5 pr-3">{t("al.createdBy", "생성자")}</th>
                  <th className="py-1.5" />
                </tr>
              </thead>
              <tbody>
                {items.map((r) => (
                  <tr key={r.id} className="border-b border-border last:border-0">
                    <td className="py-1.5 pr-3">
                      <input
                        type="checkbox"
                        checked={!!r.enabled}
                        onChange={() => toggleEnabled(r)}
                        aria-label={t("al.enabled", "활성")}
                      />
                    </td>
                    <td className="py-1.5 pr-3 font-medium text-ink">{r.name || r.id}</td>
                    <td className="py-1.5 pr-3">
                      <code className="rounded bg-surface-hover px-1 text-[11px]">{condSummary(r.condition)}</code>
                    </td>
                    <td className="py-1.5 pr-3">
                      <div className="flex flex-wrap gap-1">
                        {(r.channels || []).map((c, i) => (
                          <Badge key={i} tone="muted">{channelLabel(c)}</Badge>
                        ))}
                        {!(r.channels || []).length && <span className="text-muted">-</span>}
                      </div>
                    </td>
                    <td className="py-1.5 pr-3 text-xs text-muted">
                      {r.last_fired_at ? `${fmtTs(r.last_fired_at)} (${fmtAge(Math.max(0, (Date.now() - Date.parse(r.last_fired_at)) / 1000))})` : t("al.never", "없음")}
                    </td>
                    <td className="py-1.5 pr-3 text-right text-xs text-ink">
                      {/* fires count 필드는 서버가 제공하면 표시, 없으면 - */}
                      {(r as unknown as { fires_count?: number }).fires_count ?? "-"}
                    </td>
                    <td className="py-1.5 pr-3 text-xs text-muted">{r.created_by || "-"}</td>
                    <td className="py-1.5">
                      <div className="flex justify-end gap-1">
                        <Button variant="ghost" className="px-2 py-0.5 text-xs" onClick={() => setEditing(r)}>
                          {t("al.edit", "수정")}
                        </Button>
                        <Button variant="ghost" className="px-2 py-0.5 text-xs text-err" onClick={() => remove(r)}>
                          {t("al.delete", "삭제")}
                        </Button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <Card title={t("al.firesTitle", "최근 발화 이력")} className="mt-3">
        {fires.isLoading && <Spinner />}
        {fires.error && (
          <ErrorBox message={fires.error instanceof Error ? fires.error.message : String(fires.error)} onRetry={() => fires.refetch()} />
        )}
        {fires.data && !fireItems.length && <Empty>{t("al.noFires", "발화 이력이 없습니다")}</Empty>}
        {fireItems.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border text-left text-xs text-muted">
                  <th className="py-1.5 pr-3">{t("al.firedAt", "발화 시각")}</th>
                  <th className="py-1.5 pr-3">{t("al.ruleCol", "규칙")}</th>
                  <th className="py-1.5 pr-3">{t("al.fireStatus", "상태")}</th>
                  <th className="py-1.5 pr-3">{t("al.channels", "채널")}</th>
                  <th className="py-1.5">{t("al.fingerprint", "지문")}</th>
                </tr>
              </thead>
              <tbody>
                {fireItems.map((f) => (
                  <tr key={f.id} className="border-b border-border last:border-0">
                    <td className="py-1.5 pr-3 text-xs text-muted">{fmtTs(f.created_at)}</td>
                    <td className="py-1.5 pr-3 text-ink">
                      {f.rule_id ? (ruleById.get(f.rule_id)?.name || f.rule_id.slice(0, 8)) : "-"}
                    </td>
                    <td className="py-1.5 pr-3">
                      <Badge tone={FIRE_TONE[String(f.status || "")] || "muted"}>{f.status || "-"}</Badge>
                    </td>
                    <td className="py-1.5 pr-3">
                      <div className="flex flex-wrap gap-1">
                        {(f.channels || []).map((c, i) => (
                          <Badge key={i} tone="muted">{channelLabel(c)}</Badge>
                        ))}
                      </div>
                    </td>
                    <td className="py-1.5 font-mono text-[11px] text-muted">{f.event_fingerprint ? f.event_fingerprint.slice(0, 12) : "-"}</td>
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
