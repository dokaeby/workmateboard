/** 예산 정책 + 예산 인시던트 — GET/PUT/DELETE /budgets/*, GET/POST /budget-incidents/*. */
import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, apiGet, apiDelete, ApiError } from "../../shared/api/client";
import type {
  BudgetPolicy,
  BudgetOverview,
  BudgetIncident,
  IncidentsResponse,
} from "../../shared/api/types";
import { useI18n } from "../../shared/i18n";
import { Card, Empty, Spinner, ErrorBox, Button, Badge } from "../../shared/ui";
import { usd, usdPreferMicro, fmtTs } from "../../shared/lib/format";

const SCOPE_LABEL: Record<string, string> = {
  global: "budg.scopeGlobal",
  project: "budg.scopeProject",
  workflow: "budg.scopeWorkflow",
  agent: "budg.scopeAgent",
  task: "budg.scopeTask",
};
const SCOPE_FALLBACK: Record<string, string> = {
  global: "전체",
  project: "프로젝트",
  workflow: "워크플로",
  agent: "에이전트",
  task: "태스크",
};
const STATUS_TONE: Record<string, "ok" | "warn" | "err" | "muted"> = {
  ok: "ok",
  warning: "warn",
  hard_stop: "err",
  disabled: "muted",
};
const STATUS_LABEL: Record<string, string> = {
  ok: "budg.statusOk",
  warning: "budg.statusWarning",
  hard_stop: "budg.statusHardStop",
  disabled: "budg.statusDisabled",
};
const STATUS_FALLBACK: Record<string, string> = {
  ok: "정상",
  warning: "경고",
  hard_stop: "하드 스톱",
  disabled: "비활성",
};

/** 백엔드가 warn_percent를 float(80.0)로 줄 수 있어 표시용 문자열로 정규화. */
function pctText(v: number | null | undefined, fallback = 80): string {
  const n = Number(v);
  return Number.isFinite(n) ? String(Math.round(n * 100) / 100) : String(fallback);
}

function scopeText(p: { scope_type?: string; scope_id?: string }, t: (k: string, f?: string) => string): string {
  const st = p.scope_type || "";
  const sid = p.scope_id || "";
  if (st === "global") return t(SCOPE_LABEL.global, SCOPE_FALLBACK.global);
  const label = t(SCOPE_LABEL[st] || "", SCOPE_FALLBACK[st] || st || "-");
  return sid ? `${label}: ${sid}` : label;
}

function policyPath(scopeType: string, scopeId: string): string {
  return `/budgets/policies/${encodeURIComponent(scopeType)}/${encodeURIComponent(scopeId)}`;
}

/** USD 입력값 → 정수 microusd (표시 계층 변환, 내부는 정수 유지). */
function microFromUsdInput(v: string): number {
  const n = Number(v);
  return Number.isFinite(n) ? Math.round(n * 1_000_000) : 0;
}
function usdFromMicroInput(v: number | null | undefined): string {
  return v == null ? "" : String(Math.round((v / 1_000_000) * 1e6) / 1e6);
}

/** 정책 1행 — 금액/경고% 입력 + 미산정 정책·예약 + 하드스톱·활성 토글 + 삭제. */
function PolicyRow({ p, onChanged }: { p: BudgetPolicy; onChanged: () => void }) {
  const { t } = useI18n();
  const [amount, setAmount] = useState(p.amount_usd == null ? "" : String(p.amount_usd));
  const [warn, setWarn] = useState(pctText(p.warn_percent));
  const [unpriced, setUnpriced] = useState<string>(p.unpriced_usage_policy || "allow");
  const [reservation, setReservation] = useState(usdFromMicroInput(p.reservation_microusd));
  const [busy, setBusy] = useState(false);

  const dirty =
    (p.amount_usd == null ? "" : String(p.amount_usd)) !== amount ||
    pctText(p.warn_percent) !== warn ||
    (p.unpriced_usage_policy || "allow") !== unpriced ||
    usdFromMicroInput(p.reservation_microusd) !== reservation;

  const save = async () => {
    setBusy(true);
    try {
      const body: Record<string, unknown> = {
        warn_percent: Number(warn),
        unpriced_usage_policy: unpriced,
        reservation_microusd: microFromUsdInput(reservation),
      };
      if (amount.trim() === "") {
        body.amount_usd = null;
        body.amount_microusd = null;
      } else {
        body.amount_usd = Number(amount);
        body.amount_microusd = microFromUsdInput(amount);
      }
      await api(policyPath(p.scope_type || "global", p.scope_id || "global"), { method: "PUT", json: body });
      onChanged();
    } catch (e) {
      alert(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const toggle = async (field: "hard_stop_enabled" | "is_active", value: boolean) => {
    setBusy(true);
    try {
      await api(policyPath(p.scope_type || "global", p.scope_id || "global"), {
        method: "PUT",
        json: { [field]: value },
      });
      onChanged();
    } catch (e) {
      alert(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const del = async () => {
    if (!confirm(t("budg.confirmDelete", "'{scope}' 예산 정책을 삭제할까요?").replace("{scope}", scopeText(p, t)))) return;
    setBusy(true);
    try {
      await apiDelete(policyPath(p.scope_type || "global", p.scope_id || "global"));
      onChanged();
    } catch (e) {
      alert(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const pct = Math.round(Number(p.used_pct) || 0);
  const barTone =
    p.status === "hard_stop" ? "bg-err"
    : p.status === "warning" ? "bg-amber-500"
    : p.status === "disabled" ? "bg-slate-400"
    : "bg-emerald-500";

  return (
    <div className="border-b border-border py-3 last:border-0">
      <div className="flex flex-wrap items-center gap-2">
        <span className="min-w-0 flex-1 truncate text-sm font-medium text-ink">{scopeText(p, t)}</span>
        <Badge tone={STATUS_TONE[p.status || ""] || "muted"}>
          {t(STATUS_LABEL[p.status || ""] || "", STATUS_FALLBACK[p.status || ""] || p.status)}
        </Badge>
        {(p.open_incidents ?? 0) > 0 && <Badge tone="warn">{t("budg.incidents", "{n}건").replace("{n}", String(p.open_incidents))}</Badge>}
      </div>
      <div className="mt-1.5 flex flex-wrap items-center gap-2 text-xs text-muted">
        <span>{t("budg.spent", "이번 달")}</span>
        <b className="text-ink">{usdPreferMicro(p.month_spend_usd, p.month_spend_microusd)}</b>
        <span>/</span>
        <input
          value={amount}
          onChange={(e) => setAmount(e.target.value)}
          type="number" min="0" step="0.01" placeholder={t("budg.noLimit", "제한 없음")}
          aria-label={t("budg.limitAria", "월 한도 (USD)")}
          className="w-24 rounded-md border border-border bg-surface px-2 py-1 text-xs"
        />
        <span>{t("budg.warnAt", "경고")}</span>
        <input
          value={warn}
          onChange={(e) => setWarn(e.target.value)}
          type="number" min="1" max="100" step="1"
          aria-label={t("budg.warnAria", "경고 비율 (%)")}
          className="w-14 rounded-md border border-border bg-surface px-2 py-1 text-xs"
        />
        <span>%</span>
        <select
          value={unpriced}
          onChange={(e) => setUnpriced(e.target.value)}
          aria-label={t("budg.unpricedPolicyAria", "미산정 사용량 정책")}
          className="rounded-md border border-border bg-surface px-1.5 py-1 text-xs"
        >
          <option value="allow">{t("budg.unpricedAllow", "허용")}</option>
          <option value="block">{t("budg.unpricedBlock", "차단")}</option>
        </select>
        <input
          value={reservation}
          onChange={(e) => setReservation(e.target.value)}
          type="number" min="0" step="0.01" placeholder="0"
          aria-label={t("budg.reservationAria", "런 시작 예약액 (USD)")}
          title={t("budg.reservationHint", "새 실행 시작 전 예약으로 잡히는 금액 — 실제 비용으로 기록되지 않습니다")}
          className="w-20 rounded-md border border-border bg-surface px-2 py-1 text-xs"
        />
        <Button variant="secondary" className="px-2 py-0.5 text-xs" disabled={busy || !dirty} onClick={save}>
          {t("common.save", "저장")}
        </Button>
        <label className="ml-1 flex items-center gap-1">
          <input
            type="checkbox"
            checked={p.hard_stop_enabled}
            disabled={busy}
            onChange={(e) => toggle("hard_stop_enabled", e.target.checked)}
          />
          {t("budg.hardStop", "하드 스톱")}
        </label>
        <label className="flex items-center gap-1">
          <input
            type="checkbox"
            checked={p.is_active}
            disabled={busy}
            onChange={(e) => toggle("is_active", e.target.checked)}
          />
          {t("budg.active", "활성")}
        </label>
        <Button variant="secondary" className="px-2 py-0.5 text-xs" disabled={busy} onClick={del}>
          {t("common.delete", "삭제")}
        </Button>
      </div>
      <div className="mt-1.5 h-2 overflow-hidden rounded-full bg-surface-hover">
        <div className={`h-full rounded-full ${barTone}`} style={{ width: `${Math.min(100, pct)}%` }} />
      </div>
      <div className="mt-0.5 text-[11px] text-muted">{pct}%</div>
    </div>
  );
}

/** 새 정책 추가 폼. */
function NewPolicyForm({ onDone }: { onDone: () => void }) {
  const { t } = useI18n();
  const qc = useQueryClient();
  const [scopeType, setScopeType] = useState<"global" | "project" | "workflow" | "agent" | "task">("global");
  const [scopeId, setScopeId] = useState("");
  const [amount, setAmount] = useState("");
  const [warn, setWarn] = useState("80");
  const [hardStop, setHardStop] = useState(true);
  const [unpriced, setUnpriced] = useState("allow");
  const [reservation, setReservation] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ text: string; err?: boolean }>({ text: "" });

  const submit = async () => {
    if (scopeType !== "global" && !scopeId.trim()) {
      setMsg({ text: t("budg.needScopeId", "프로젝트/워크플로 범위는 이름을 입력하세요"), err: true });
      return;
    }
    // 백엔드 라우트가 /budgets/policies/{scope_type}/{scope_id} 단일 세그먼트라
    // scope_id에 '/'가 있으면 %2F 인코딩도 라우팅되지 않아 404가 난다.
    if (scopeType !== "global" && scopeId.includes("/")) {
      setMsg({ text: t("budg.noSlash", "범위 이름에 '/'는 사용할 수 없습니다"), err: true });
      return;
    }
    if (amount.trim() === "" || !(Number(amount) > 0)) {
      setMsg({ text: t("budg.needAmount", "월 한도를 입력하세요"), err: true });
      return;
    }
    setBusy(true);
    setMsg({ text: t("common.saving", "저장 중…") });
    try {
      await api(policyPath(scopeType, scopeType === "global" ? "global" : scopeId.trim()), {
        method: "PUT",
        json: {
          amount_usd: Number(amount),
          amount_microusd: microFromUsdInput(amount),
          warn_percent: Number(warn) || 80,
          hard_stop_enabled: hardStop,
          unpriced_usage_policy: unpriced,
          reservation_microusd: microFromUsdInput(reservation || "0"),
          is_active: true,
        },
      });
      setMsg({ text: t("common.saved", "저장했습니다") });
      qc.invalidateQueries({ queryKey: ["budget-overview"] });
      qc.invalidateQueries({ queryKey: ["budget-incidents"] });
      onDone();
    } catch (e) {
      setMsg({ text: e instanceof ApiError ? e.message : String(e), err: true });
    } finally {
      setBusy(false);
    }
  };

  const inputCls = "rounded-md border border-border bg-surface px-2 py-1.5 text-sm";

  return (
    <div className="mb-3 space-y-2 rounded-lg border border-border bg-surface-hover p-3">
      <div className="flex flex-wrap items-center gap-2">
        <select
          value={scopeType}
          onChange={(e) => setScopeType(e.target.value as typeof scopeType)}
          aria-label={t("budg.scopeAria", "적용 범위")}
          className={inputCls}
        >
          <option value="global">{t("budg.scopeGlobal", "전체")}</option>
          <option value="project">{t("budg.scopeProject", "프로젝트")}</option>
          <option value="workflow">{t("budg.scopeWorkflow", "워크플로")}</option>
          <option value="agent">{t("budg.scopeAgent", "에이전트")}</option>
          <option value="task">{t("budg.scopeTask", "태스크")}</option>
        </select>
        {scopeType !== "global" && (
          // scope_id는 백엔드 ID 그대로 — agent는 agents.id(uuid),
          // task는 tasks.id(uuid; 'WMB-n' identifier는 조회만 되고
          // 원장 매칭은 uuid 기준).
          <input
            value={scopeId}
            onChange={(e) => setScopeId(e.target.value)}
            placeholder={
              scopeType === "project"
                ? t("budg.scopeIdProjectPh", "프로젝트 이름")
                : scopeType === "workflow"
                  ? t("budg.scopeIdWorkflowPh", "워크플로 키")
                  : scopeType === "agent"
                    ? t("budg.scopeIdAgentPh", "에이전트 ID")
                    : t("budg.scopeIdTaskPh", "태스크 ID")
            }
            className={`${inputCls} min-w-40 flex-1`}
          />
        )}
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <label className="flex items-center gap-1 text-xs text-muted">
          {t("budg.limitUsd", "월 한도 (USD)")}
          <input
            value={amount}
            onChange={(e) => setAmount(e.target.value)}
            type="number" min="0" step="0.01" required
            className={`${inputCls} w-28`}
          />
        </label>
        <label className="flex items-center gap-1 text-xs text-muted">
          {t("budg.warnAt", "경고")}
          <input
            value={warn}
            onChange={(e) => setWarn(e.target.value)}
            type="number" min="1" max="100" step="1"
            className={`${inputCls} w-16`}
          />
          %
        </label>
        <label className="flex items-center gap-1 text-xs text-muted">
          {t("budg.unpricedPolicy", "미산정 사용량")}
          <select value={unpriced} onChange={(e) => setUnpriced(e.target.value)} className={inputCls}>
            <option value="allow">{t("budg.unpricedAllow", "허용")}</option>
            <option value="block">{t("budg.unpricedBlock", "차단")}</option>
          </select>
        </label>
        <label className="flex items-center gap-1 text-xs text-muted">
          {t("budg.reservationUsd", "예약액 (USD)")}
          <input
            value={reservation}
            onChange={(e) => setReservation(e.target.value)}
            type="number" min="0" step="0.01" placeholder="0"
            className={`${inputCls} w-24`}
          />
        </label>
        <label className="flex items-center gap-1 text-xs text-muted">
          <input type="checkbox" checked={hardStop} onChange={(e) => setHardStop(e.target.checked)} />
          {t("budg.hardStop", "하드 스톱")}
        </label>
      </div>
      <div className="flex items-center justify-end gap-2">
        {msg.text && <span className={`text-xs ${msg.err ? "text-err" : "text-ok"}`}>{msg.text}</span>}
        <Button variant="secondary" className="px-3 py-1 text-xs" onClick={onDone}>
          {t("common.cancel", "취소")}
        </Button>
        <Button className="px-3 py-1 text-xs" disabled={busy} onClick={submit}>
          {busy ? t("common.saving", "저장 중…") : t("common.save", "저장")}
        </Button>
      </div>
    </div>
  );
}

/** 예산 정책 카드 + 열린 예산 인시던트 카드. */
export function BudgetSection() {
  const { t } = useI18n();
  const qc = useQueryClient();
  const [showNew, setShowNew] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [raiseFor, setRaiseFor] = useState<string | null>(null);
  const [raiseAmount, setRaiseAmount] = useState("");

  const overview = useQuery({
    queryKey: ["budget-overview"],
    queryFn: () => apiGet<BudgetOverview>("/budgets/overview"),
    refetchInterval: 60000,
  });
  const incidents = useQuery({
    queryKey: ["budget-incidents"],
    queryFn: () => apiGet<IncidentsResponse>("/budget-incidents?status=open"),
    refetchInterval: 60000,
  });

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["budget-overview"] });
    qc.invalidateQueries({ queryKey: ["budget-incidents"] });
  };

  const policies = overview.data?.policies || [];
  const openIncidents = incidents.data?.items || [];

  const resolve = async (inc: BudgetIncident, action: "keep_paused" | "raise_budget_and_resume") => {
    const body: Record<string, unknown> = { action };
    if (action === "raise_budget_and_resume") {
      const amt = Number(raiseAmount);
      if (!(amt > 0)) {
        alert(t("budg.needRaiseAmount", "상향할 월 한도(USD)를 입력하세요"));
        return;
      }
      body.amount_usd = amt;
      body.amount_microusd = microFromUsdInput(raiseAmount);
    }
    const msg = action === "keep_paused"
      ? t("budg.confirmKeep", "이 인시던트를 '유지'로 처리할까요? 실행은 계속 일시정지 상태로 남습니다.")
      : t("budg.confirmRaise", "예산을 상향하고 실행을 재개할까요?").replace("{amt}", usd(Number(body.amount_usd)));
    if (!confirm(msg)) return;
    setBusyId(inc.id);
    try {
      await api(`/budget-incidents/${encodeURIComponent(inc.id)}/resolve`, { method: "POST", json: body });
      setRaiseFor(null);
      setRaiseAmount("");
      refresh();
    } catch (e) {
      alert(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusyId(null);
    }
  };

  const startRaise = (inc: BudgetIncident) => {
    setRaiseFor(inc.id);
    const obsUsd = inc.observed_spend_microusd != null
      ? inc.observed_spend_microusd / 1_000_000
      : Number(inc.amount_observed) || 0;
    setRaiseAmount(String(Math.ceil(obsUsd * 1.2 * 100) / 100));
  };

  return (
    <>
      <Card
        title={t("budg.policiesTitle", "예산 정책")}
        actions={
          <div className="flex items-center gap-2">
            {overview.data?.month && <span className="text-xs text-muted">{overview.data.month}</span>}
            <Button variant="secondary" className="px-2 py-0.5 text-xs" onClick={() => setShowNew((v) => !v)}>
              {showNew ? t("common.close", "닫기") : "+ " + t("budg.add", "정책 추가")}
            </Button>
          </div>
        }
        className="mt-3"
      >
        {showNew && <NewPolicyForm onDone={() => setShowNew(false)} />}
        {overview.isLoading && <Spinner />}
        {overview.error && (
          <ErrorBox message={overview.error instanceof Error ? overview.error.message : String(overview.error)} onRetry={() => overview.refetch()} />
        )}
        {overview.data && !policies.length && <Empty>{t("budg.empty", "등록된 예산 정책이 없습니다")}</Empty>}
        {policies.map((p) => (
          <PolicyRow key={p.id} p={p} onChanged={refresh} />
        ))}
      </Card>

      <Card title={t("budg.incidentsTitle", "열린 예산 인시던트")} className="mt-3">
        {incidents.isLoading && <Spinner />}
        {incidents.error && (
          <ErrorBox message={incidents.error instanceof Error ? incidents.error.message : String(incidents.error)} onRetry={() => incidents.refetch()} />
        )}
        {incidents.data && !openIncidents.length && <Empty>{t("budg.noIncidents", "열린 인시던트가 없습니다")}</Empty>}
        {openIncidents.map((inc) => (
          <div key={inc.id} className="border-b border-border py-3 last:border-0">
            <div className="flex flex-wrap items-center gap-2">
              <span className="min-w-0 flex-1 truncate text-sm font-medium text-ink">{scopeText(inc, t)}</span>
              <Badge tone={inc.threshold_type === "hard" ? "err" : "warn"}>
                {inc.threshold_type === "hard"
                  ? t("budg.thresholdHard", "하드 한도 초과")
                  : t("budg.thresholdSoft", "경고 한도 도달")}
              </Badge>
              {(inc.unpriced_event_count ?? 0) > 0 && (
                <Badge tone="warn">
                  {t("budg.unpricedEvents", "미산정 {n}건").replace("{n}", String(inc.unpriced_event_count))}
                </Badge>
              )}
            </div>
            <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted">
              <span>
                {usdPreferMicro(inc.amount_observed, inc.observed_spend_microusd)} /{" "}
                {usdPreferMicro(inc.amount_limit, inc.amount_limit_microusd)}
              </span>
              <span>{fmtTs(inc.created_at)}</span>
              {inc.note && <span className="truncate">{inc.note}</span>}
            </div>
            <div className="mt-2 flex flex-wrap items-center gap-2">
              <Button
                variant="secondary"
                className="px-2.5 py-1 text-xs"
                disabled={busyId === inc.id}
                onClick={() => resolve(inc, "keep_paused")}
              >
                {t("budg.keepPaused", "유지 (일시정지 유지)")}
              </Button>
              {raiseFor === inc.id ? (
                <span className="flex flex-wrap items-center gap-1.5">
                  <input
                    value={raiseAmount}
                    onChange={(e) => setRaiseAmount(e.target.value)}
                    type="number" min="0" step="0.01"
                    aria-label={t("budg.raiseAmountAria", "상향할 월 한도 (USD)")}
                    className="w-24 rounded-md border border-border bg-surface px-2 py-1 text-xs"
                  />
                  <Button
                    className="px-2.5 py-1 text-xs"
                    disabled={busyId === inc.id}
                    onClick={() => resolve(inc, "raise_budget_and_resume")}
                  >
                    {t("budg.confirmRaiseBtn", "상향 후 재개")}
                  </Button>
                  <Button variant="ghost" className="px-2 py-1 text-xs" onClick={() => setRaiseFor(null)}>
                    {t("common.cancel", "취소")}
                  </Button>
                </span>
              ) : (
                <Button
                  variant="secondary"
                  className="px-2.5 py-1 text-xs"
                  disabled={busyId === inc.id}
                  onClick={() => startRaise(inc)}
                >
                  {t("budg.raiseResume", "예산 상향 후 재개")}
                </Button>
              )}
            </div>
          </div>
        ))}
      </Card>
    </>
  );
}

/** 예산 경고/하드스톱 배너 — 비용 페이지 상단에 정책 상태를 요약 표시.
 *  budget-overview 쿼리는 BudgetSection과 키를 공유해 중복 fetch가 없다. */
export function BudgetBanner() {
  const { t } = useI18n();
  const overview = useQuery({
    queryKey: ["budget-overview"],
    queryFn: () => apiGet<BudgetOverview>("/budgets/overview"),
    refetchInterval: 60000,
  });
  const policies = overview.data?.policies || [];
  const hard = policies.filter((p) => p.status === "hard_stop");
  const warn = policies.filter((p) => p.status === "warning");
  if (!hard.length && !warn.length) return null;
  const isHard = hard.length > 0;
  const names = [...hard, ...warn].map((p) => scopeText(p, t)).join(", ");
  return (
    <div
      className={`mb-3 rounded-lg border p-3 text-sm ${
        isHard
          ? "border-err-border bg-err-soft text-err"
          : "border-warn-border bg-warn-soft text-warn"
      }`}
    >
      <b>
        {isHard
          ? t("budg.bannerHard", "예산 하드 스톱 발생")
          : t("budg.bannerWarn", "예산 경고")}
      </b>
      {" — "}
      {isHard
        ? t("budg.bannerHardDesc", "한도에 도달해 실행이 중지되었습니다")
        : t("budg.bannerWarnDesc", "예산 사용률이 경고 기준을 넘었습니다")}
      <span className="ml-1 text-xs opacity-80">({names})</span>
    </div>
  );
}
