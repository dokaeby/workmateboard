/** evals — 체크 편집 행 (CheckDraft 렌더링). */
import { useI18n } from "../../shared/i18n";
import { Button } from "../../shared/ui";
import { CHECK_TYPES, inputCls, checkLabel, type CheckType } from "./types";
import type { CheckDraft } from "./checkModel";

export function CheckRow({
  draft,
  onChange,
  onRemove,
  canRemove,
}: {
  draft: CheckDraft;
  onChange: (d: CheckDraft) => void;
  onRemove: () => void;
  canRemove: boolean;
}) {
  const { t } = useI18n();
  const set = (patch: Partial<CheckDraft>) => onChange({ ...draft, ...patch });
  return (
    <div className="rounded-lg border border-border bg-surface-hover/40 p-2.5">
      <div className="flex flex-wrap items-center gap-2">
        <select
          value={draft.type}
          onChange={(e) => set({ type: e.target.value as CheckType })}
          className={`${inputCls} w-auto`}
          aria-label={t("evals.checkType", "타입")}
        >
          {CHECK_TYPES.map((ct) => (
            <option key={ct} value={ct}>{checkLabel(t, ct)}</option>
          ))}
        </select>
        <div className="min-w-0 flex-1" />
        {canRemove && (
          <Button variant="ghost" className="px-2 py-1 text-xs text-err" onClick={onRemove}>
            {t("evals.removeCheck", "삭제")}
          </Button>
        )}
      </div>
      <div className="mt-2 grid grid-cols-1 gap-2 sm:grid-cols-2">
        {draft.type === "stage_completed" && (
          <label className="block">
            <span className="mb-1 block text-xs font-medium text-muted">{t("evals.checkStage", "스테이지")}</span>
            <input value={draft.stage} onChange={(e) => set({ stage: e.target.value })} placeholder="analysis" className={inputCls} />
          </label>
        )}
        {(draft.type === "output_contains" || draft.type === "output_not_contains") && (
          <label className="block">
            <span className="mb-1 block text-xs font-medium text-muted">{t("evals.checkValue", "값")}</span>
            <input value={draft.value} onChange={(e) => set({ value: e.target.value })} className={inputCls} />
          </label>
        )}
        {draft.type === "output_regex" && (
          <label className="block">
            <span className="mb-1 block text-xs font-medium text-muted">{t("evals.checkPattern", "정규식")}</span>
            <input value={draft.pattern} onChange={(e) => set({ pattern: e.target.value })} placeholder="PASS|FAIL" className={inputCls} />
          </label>
        )}
        {draft.type === "max_cost_usd" && (
          <label className="block">
            <span className="mb-1 block text-xs font-medium text-muted">{t("evals.checkMaxCost", "최대 비용 (USD)")}</span>
            <input type="number" min="0" step="0.01" value={draft.value} onChange={(e) => set({ value: e.target.value })} placeholder="0.50" className={inputCls} />
          </label>
        )}
        {draft.type === "json_path" && (
          <>
            <label className="block">
              <span className="mb-1 block text-xs font-medium text-muted">{t("evals.checkPath", "JSON 경로")}</span>
              <input value={draft.path} onChange={(e) => set({ path: e.target.value })} placeholder="result.score" className={inputCls} />
            </label>
            <div className="grid grid-cols-[auto_1fr] items-end gap-2">
              <label className="block">
                <span className="mb-1 block text-xs font-medium text-muted">{t("evals.checkMode", "비교 방식")}</span>
                <select value={draft.jsonMode} onChange={(e) => set({ jsonMode: e.target.value as "equals" | "contains" })} className={`${inputCls} w-auto`}>
                  <option value="equals">{t("evals.checkEquals", "equals")}</option>
                  <option value="contains">{t("evals.checkContains", "contains")}</option>
                </select>
              </label>
              <label className="block">
                <span className="mb-1 block text-xs font-medium text-muted">
                  {draft.jsonMode === "equals" ? "equals (JSON)" : "contains"}
                </span>
                <input
                  value={draft.jsonValue}
                  onChange={(e) => set({ jsonValue: e.target.value })}
                  placeholder={draft.jsonMode === "equals" ? '{"ok": true}' : "substring"}
                  className={inputCls}
                />
              </label>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
