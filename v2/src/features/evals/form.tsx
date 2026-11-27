/** evals — 평가 등록/편집 폼 (POST /evals, PATCH /evals/{id}). */
import { useMemo, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { apiPost, apiPatch, ApiError } from "../../shared/api/client";
import { useI18n } from "../../shared/i18n";
import { Button } from "../../shared/ui";
import {
  inputCls, statusOf,
  type EvalCheck, type EvalItem,
} from "./types";
import {
  EMPTY_DRAFT, draftFromCheck, serializeCheck,
  type CheckDraft,
} from "./checkModel";
import { CheckRow } from "./checks";

export function EvalForm({
  initial,
  workflowKeys,
  onDone,
  onCancel,
}: {
  initial?: EvalItem | null;
  workflowKeys: string[];
  onDone: (saved?: EvalItem) => void;
  onCancel: () => void;
}) {
  const { t } = useI18n();
  const qc = useQueryClient();
  const input0 = initial?.input || {};
  const [name, setName] = useState(initial?.name || "");
  const [workflowKey, setWorkflowKey] = useState(initial?.workflow_key || "");
  const [task, setTask] = useState(String(input0.task ?? ""));
  const [extraJson, setExtraJson] = useState(() => {
    const rest = { ...input0 };
    delete rest.task;
    return Object.keys(rest).length ? JSON.stringify(rest, null, 2) : "";
  });
  const [status, setStatus] = useState(statusOf(initial || { id: "" }));
  const [checks, setChecks] = useState<CheckDraft[]>(() =>
    (initial?.checks || []).map(draftFromCheck),
  );
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  // 미완성 체크(빈 값 등)가 있으면 저장 비활성 — serializeCheck의
  // 첫 에러 문자열을 그대로 표시용으로 재사용한다.
  const checkErr = useMemo(
    () => checks.map((d) => serializeCheck(d, t)).find((r): r is string => typeof r === "string") || "",
    [checks, t],
  );

  const submit = async () => {
    if (!name.trim()) { setErr(t("evals.needName", "이름을 입력하세요")); return; }
    if (!workflowKey.trim()) { setErr(t("evals.needWorkflow", "워크플로 키를 입력하세요")); return; }
    if (!task.trim()) { setErr(t("evals.needTask", "입력 task를 입력하세요")); return; }

    let extra: Record<string, unknown> = {};
    if (extraJson.trim()) {
      try {
        const v = JSON.parse(extraJson);
        if (v === null || typeof v !== "object" || Array.isArray(v)) {
          setErr(t("evals.badJson", "JSON 형식이 올바르지 않습니다"));
          return;
        }
        extra = v as Record<string, unknown>;
      } catch {
        setErr(t("evals.badJson", "JSON 형식이 올바르지 않습니다"));
        return;
      }
    }

    const serialized: EvalCheck[] = [];
    for (const d of checks) {
      const c = serializeCheck(d, t);
      if (typeof c === "string") { setErr(c); return; }
      serialized.push(c);
    }

    const input: Record<string, unknown> = { ...extra, task: task.trim() };
    setBusy(true);
    setErr("");
    try {
      let saved: EvalItem | undefined;
      if (initial?.id) {
        saved = await apiPatch<EvalItem>(`/evals/${encodeURIComponent(initial.id)}`, {
          name: name.trim(),
          workflow_key: workflowKey.trim(),
          input,
          checks: serialized,
          status,
        });
      } else {
        saved = await apiPost<EvalItem>("/evals", {
          name: name.trim(),
          workflow_key: workflowKey.trim(),
          input,
          checks: serialized,
        });
      }
      await qc.invalidateQueries({ queryKey: ["evals"] });
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
          <span className="mb-1 block text-xs font-medium text-muted">{t("evals.nameField", "이름")}</span>
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder={t("evals.namePh", "예: dev_review 회귀")} className={inputCls} />
        </label>
        <label className="block">
          <span className="mb-1 block text-xs font-medium text-muted">{t("evals.workflowField", "워크플로")}</span>
          <input
            value={workflowKey}
            onChange={(e) => setWorkflowKey(e.target.value)}
            placeholder={t("evals.workflowPh", "워크플로 키 (예: dev_review)")}
            className={inputCls}
            list="eval-workflow-keys"
          />
          <datalist id="eval-workflow-keys">
            {workflowKeys.map((k) => <option key={k} value={k} />)}
          </datalist>
        </label>
      </div>
      <label className="block">
        <span className="mb-1 block text-xs font-medium text-muted">{t("evals.taskField", "입력 task")}</span>
        <textarea value={task} onChange={(e) => setTask(e.target.value)} rows={3} placeholder={t("evals.taskPh", "평가에 사용할 작업 내용")} className={inputCls} />
      </label>
      <label className="block">
        <span className="mb-1 block text-xs font-medium text-muted">{t("evals.extraInput", "추가 입력 (JSON, 선택)")}</span>
        <textarea value={extraJson} onChange={(e) => setExtraJson(e.target.value)} rows={3} placeholder={t("evals.extraInputPh", '{"key": "value"}')} className={`${inputCls} font-mono text-xs`} spellCheck={false} />
      </label>

      <div>
        <div className="mb-1.5 flex items-center justify-between">
          <span className="text-xs font-medium text-muted">{t("evals.checksField", "체크")}</span>
          <Button variant="secondary" className="px-2.5 py-1 text-xs" onClick={() => setChecks((c) => [...c, { ...EMPTY_DRAFT }])}>
            + {t("evals.addCheck", "체크 추가")}
          </Button>
        </div>
        {checks.length === 0 && (
          <div className="rounded-lg border border-dashed border-border p-3 text-center text-xs text-muted">
            {t("evals.noChecks", "체크가 없습니다 — 추가하면 실행 결과를 검증합니다")}
          </div>
        )}
        <div className="space-y-2">
          {checks.map((d, i) => (
            <CheckRow
              key={i}
              draft={d}
              canRemove={checks.length > 1}
              onChange={(nd) => setChecks((c) => c.map((x, j) => (j === i ? nd : x)))}
              onRemove={() => setChecks((c) => c.filter((_, j) => j !== i))}
            />
          ))}
        </div>
      </div>

      {initial?.id && (
        <label className="block">
          <span className="mb-1 block text-xs font-medium text-muted">{t("evals.statusField", "상태")}</span>
          <select value={status} onChange={(e) => setStatus(e.target.value)} className={`${inputCls} w-auto`}>
            <option value="active">{t("evals.statusActive", "사용 중")}</option>
            <option value="archived">{t("evals.statusArchived", "보관됨")}</option>
          </select>
        </label>
      )}

      {checkErr && <div className="text-xs text-warn">{checkErr}</div>}
      {err && <div className="rounded-lg border border-err-border bg-err-soft p-2.5 text-sm text-err">{err}</div>}

      <div className="flex justify-end gap-2">
        <Button variant="secondary" className="px-3 py-1 text-xs" onClick={onCancel}>{t("common.cancel", "취소")}</Button>
        <Button className="px-3 py-1 text-xs" disabled={busy || !!checkErr} onClick={submit}>
          {busy ? t("common.saving", "저장 중…") : t("common.save", "저장")}
        </Button>
      </div>
    </div>
  );
}
