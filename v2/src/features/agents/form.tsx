/** agents — 에이전트 등록/편집 폼 — initial이 있으면 PATCH, 없으면 POST.
 *  presetReportsTo는 "하위 에이전트 추가"에서 보고 대상을 미리 채운다. */
import { useMemo, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { apiPost, apiPatch, ApiError } from "../../shared/api/client";
import { useI18n } from "../../shared/i18n";
import { Button } from "../../shared/ui";
import type { Agent } from "./tree";
import {
  ADAPTER_TYPES, DISPLAY_STATUSES, inputCls,
  statusLabel, statusOf,
} from "./shared";

export function AgentForm({
  agents,
  initial,
  presetReportsTo,
  onDone,
}: {
  agents: Agent[];
  initial?: Agent | null;
  presetReportsTo?: string | null;
  onDone: (saved?: Agent) => void;
}) {
  const { t } = useI18n();
  const qc = useQueryClient();
  const [name, setName] = useState(initial?.name || "");
  const [adapter, setAdapter] = useState(initial?.adapter_type || "claude");
  const [role, setRole] = useState(initial?.role || "");
  const [desc, setDesc] = useState(initial?.description || "");
  const [caps, setCaps] = useState((initial?.capabilities || []).join(", "));
  const [parent, setParent] = useState<string>(
    presetReportsTo ?? initial?.reports_to ?? "",
  );
  // microusd(정수)가 있으면 authoritative — USD 입력창용으로만 환산한다.
  const [budget, setBudget] = useState(() => {
    if (initial?.monthly_budget_microusd != null) {
      return String(Number((initial.monthly_budget_microusd / 1_000_000).toFixed(6)));
    }
    return initial?.monthly_budget_usd == null ? "" : String(initial.monthly_budget_usd);
  });
  const [status, setStatus] = useState<string>(initial ? statusOf(initial) : "active");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  // 보고 대상 후보 — 본인·보관된 에이전트 제외. 현재 선택값이 후보에 없으면 유지.
  const parentOptions = useMemo(() => {
    const list = agents.filter(
      (a) => a.id !== initial?.id && statusOf(a) !== "archived",
    );
    if (parent && !list.some((a) => a.id === parent)) {
      const cur = agents.find((a) => a.id === parent);
      if (cur) return [cur, ...list];
    }
    return list;
  }, [agents, initial?.id, parent]);

  const submit = async () => {
    if (!name.trim()) {
      setErr(t("agents.needName", "이름을 입력하세요"));
      return;
    }
    if (!ADAPTER_TYPES.includes(adapter)) {
      setErr(t("agents.needAdapter", "어댑터 유형을 선택하세요"));
      return;
    }
    let budgetNum: number | null | undefined;
    if (budget.trim() === "") {
      budgetNum = initial ? null : undefined; // 편집 시 빈 값 = 한도 해제
    } else {
      const n = Number(budget);
      if (!isFinite(n) || n < 0) {
        setErr(t("agents.needBudget", "월 예산은 0 이상의 숫자로 입력하세요"));
        return;
      }
      budgetNum = n;
    }
    const capsArr = caps
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);

    const body: Record<string, unknown> = {
      name: name.trim(),
      adapter_type: adapter,
      // status는 canonical 값도 허용 — PATCH /agents가 서버에서 전이
      // allowlist를 적용한다(동일 상태는 no-op). 생성 시에는 'active' 고정.
      status,
    };
    if (role.trim()) body.role = role.trim();
    else if (initial) body.role = null;
    if (desc.trim()) body.description = desc.trim();
    else if (initial) body.description = null;
    body.capabilities = capsArr;
    if (initial) {
      // 편집: 명시적 null로 보고 대상/예산 해제를 지원한다.
      body.reports_to = parent || null;
      body.monthly_budget_usd = budgetNum === undefined ? null : budgetNum;
      body.monthly_budget_microusd =
        budgetNum == null ? null : Math.round(budgetNum * 1_000_000);
    } else {
      if (parent) body.reports_to = parent;
      if (budgetNum != null) {
        body.monthly_budget_usd = budgetNum;
        body.monthly_budget_microusd = Math.round(budgetNum * 1_000_000);
      }
    }

    setBusy(true);
    setErr("");
    try {
      let saved: Agent | undefined;
      if (initial?.id) {
        saved = await apiPatch<Agent>(`/agents/${encodeURIComponent(initial.id)}`, body);
      } else {
        saved = await apiPost<Agent>("/agents", body);
      }
      await qc.invalidateQueries({ queryKey: ["agents"] });
      onDone(saved);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-2.5">
      <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-2">
        <label className="block">
          <span className="mb-1 block text-xs font-medium text-muted">{t("agents.name", "이름")}</span>
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder={t("agents.namePh", "예: 코드 리뷰 봇")} className={inputCls} />
        </label>
        <label className="block">
          <span className="mb-1 block text-xs font-medium text-muted">{t("agents.adapterType", "어댑터 유형")}</span>
          <select value={adapter} onChange={(e) => setAdapter(e.target.value)} className={inputCls}>
            {ADAPTER_TYPES.map((x) => (
              <option key={x} value={x}>{x}</option>
            ))}
          </select>
        </label>
        <label className="block">
          <span className="mb-1 block text-xs font-medium text-muted">{t("agents.role", "역할")}</span>
          <input value={role} onChange={(e) => setRole(e.target.value)} placeholder={t("agents.rolePh", "예: 리뷰어")} className={inputCls} />
        </label>
        <label className="block">
          <span className="mb-1 block text-xs font-medium text-muted">{t("agents.reportsToSelect", "보고 대상 (선택)")}</span>
          <select value={parent} onChange={(e) => setParent(e.target.value)} className={inputCls}>
            <option value="">{t("agents.noParent", "없음 (최상위)")}</option>
            {parentOptions.map((a) => (
              <option key={a.id} value={a.id}>{a.name || a.id}</option>
            ))}
          </select>
        </label>
        <label className="block">
          <span className="mb-1 block text-xs font-medium text-muted">{t("agents.budgetPh", "월 예산 USD (선택)")}</span>
          <input value={budget} onChange={(e) => setBudget(e.target.value)} type="number" min="0" step="0.01" placeholder="0.00" className={inputCls} />
        </label>
        {initial && (
          <label className="block">
            <span className="mb-1 block text-xs font-medium text-muted">{t("agents.statusField", "상태")}</span>
            <select value={status} onChange={(e) => setStatus(e.target.value)} className={inputCls}>
              {DISPLAY_STATUSES.map((s) => (
                <option key={s} value={s}>{statusLabel(t, s)}</option>
              ))}
            </select>
          </label>
        )}
      </div>
      <label className="block">
        <span className="mb-1 block text-xs font-medium text-muted">{t("agents.description", "설명")}</span>
        <input value={desc} onChange={(e) => setDesc(e.target.value)} placeholder={t("agents.descriptionPh", "설명 (선택)")} className={inputCls} />
      </label>
      <label className="block">
        <span className="mb-1 block text-xs font-medium text-muted">{t("agents.capabilities", "기능")}</span>
        <input value={caps} onChange={(e) => setCaps(e.target.value)} placeholder={t("agents.capabilitiesPh", "기능 (쉼표로 구분, 예: 리뷰, 테스트)")} className={inputCls} />
      </label>
      {err && <p className="text-xs text-err">{err}</p>}
      <div className="flex items-center justify-end gap-2">
        <Button variant="secondary" className="px-3 py-1 text-xs" onClick={() => onDone()} disabled={busy}>
          {t("common.cancel", "취소")}
        </Button>
        <Button className="px-3 py-1 text-xs" disabled={busy} onClick={submit}>
          {busy ? t("common.saving", "저장 중…") : t("common.save", "저장")}
        </Button>
      </div>
    </div>
  );
}
