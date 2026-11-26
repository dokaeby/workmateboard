/** evals — /evals 평가 케이스(test-run harness) 목록 + 등록/편집 + 실행/이력.
 *  API 계약(백엔드 병렬 구현, 방어적 옵셔널 타이핑):
 *    GET    /evals?status=            → { items: EvalItem[] }
 *    POST   /evals                    → EvalItem
 *    GET    /evals/{id}               → EvalItem
 *    PATCH  /evals/{id}               → EvalItem (partial)
 *    DELETE /evals/{id}               → 보관 처리
 *    POST   /evals/{id}/run           → { eval_run_id, run_id, status }
 *    GET    /evals/{id}/runs          → { items: EvalRun[] } (최신순)
 */
import { useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { apiGet, apiPost, apiPatch, apiDelete, ApiError } from "../../shared/api/client";
import { useI18n, type TFn } from "../../shared/i18n";
import { PageHead, Card, Empty, Spinner, ErrorBox, Badge, Button } from "../../shared/ui";
import { fmtTs, fmtAge, jsonSummary } from "../../shared/lib/format";
import type { WorkflowInfo, CustomWorkflow } from "../../shared/api/types";
import "./i18n";

/* ── 계약 타입 (모든 필드 옵셔널 — 백엔드 응답 편차 방어) ── */

type CheckType =
  | "stage_completed"
  | "output_contains"
  | "output_not_contains"
  | "output_regex"
  | "json_path"
  | "max_cost_usd";

interface EvalCheck {
  type?: string;
  stage?: string;
  value?: unknown;
  pattern?: string;
  path?: string;
  equals?: unknown;
  contains?: unknown;
}

interface EvalItem {
  id: string;
  name?: string;
  workflow_key?: string;
  input?: Record<string, unknown>;
  checks?: EvalCheck[];
  status?: string; // "active" | "archived"
  created_at?: string;
  updated_at?: string;
  last_score?: number | null;
  last_run_at?: string | null;
}

interface EvalRunResult {
  // 채점 결과 1건 — check_type은 표시용 타입 문자열, check는 원본 체크
  // 객체(파라미터 보존, 툴팁에 JSON.stringify로 표시). 레거시 응답 대비
  // check가 문자열/객체일 수 있어 옵셔널로 둔다.
  check_type?: string;
  check?: EvalCheck | Record<string, unknown> | string | null;
  passed?: boolean;
  detail?: string;
}

interface EvalRun {
  id: string;
  run_id?: string | null;
  status?: string; // "pending" | "scored" | "error"
  score?: number | null;
  results?: EvalRunResult[];
  error?: string | null;
  created_at?: string;
  scored_at?: string | null;
}

interface EvalsResponse { items?: EvalItem[] }
interface EvalRunsResponse { items?: EvalRun[] }
interface EvalRunResponse { eval_run_id?: string; run_id?: string; status?: string }

const CHECK_TYPES: CheckType[] = [
  "stage_completed",
  "output_contains",
  "output_not_contains",
  "output_regex",
  "json_path",
  "max_cost_usd",
];

const inputCls =
  "w-full rounded-lg border border-border bg-surface px-2.5 py-1.5 text-sm outline-none focus:border-accent";

function statusOf(e: EvalItem): string {
  return String(e.status || "active").toLowerCase();
}

function scoreTone(score: number | null | undefined): "ok" | "warn" | "err" | "muted" {
  if (score == null || !isFinite(Number(score))) return "muted";
  const n = Number(score);
  if (n >= 80) return "ok";
  if (n >= 50) return "warn";
  return "err";
}

function ScoreBadge({ score }: { score?: number | null }) {
  const { t } = useI18n();
  if (score == null || !isFinite(Number(score))) {
    return <Badge tone="muted">{t("evals.noScore", "점수 없음")}</Badge>;
  }
  return <Badge tone={scoreTone(score)}>{Math.round(Number(score))}</Badge>;
}

function ago(iso: string | null | undefined): string {
  if (!iso) return "-";
  const ms = Date.now() - new Date(iso).getTime();
  if (!isFinite(ms) || ms < 0) return "-";
  return fmtAge(ms / 1000);
}

function checkLabel(t: TFn, type?: string): string {
  switch (type) {
    case "stage_completed": return t("evals.chk.stage_completed", "스테이지 완료");
    case "output_contains": return t("evals.chk.output_contains", "출력 포함");
    case "output_not_contains": return t("evals.chk.output_not_contains", "출력 미포함");
    case "output_regex": return t("evals.chk.output_regex", "출력 정규식");
    case "json_path": return t("evals.chk.json_path", "JSON 경로");
    case "max_cost_usd": return t("evals.chk.max_cost_usd", "최대 비용");
    default: return type || "?";
  }
}

/** 워크플로 키 후보 — 내장 /workflows + 커스텀 /workflows/custom 병합 (triggers와 동일 패턴). */
function useWorkflowKeys() {
  return useQuery({
    queryKey: ["eval-workflow-keys"],
    staleTime: 300000,
    queryFn: async (): Promise<string[]> => {
      const keys = new Set<string>();
      try {
        const wfs = await apiGet<WorkflowInfo[]>("/workflows");
        for (const w of wfs || []) if (w.key) keys.add(w.key);
      } catch { /* 실패 시 커스텀만 */ }
      try {
        const d = await apiGet<{ items?: CustomWorkflow[] }>("/workflows/custom");
        for (const c of d.items || []) {
          const n = c.name || "";
          if (n) keys.add(n);
        }
      } catch { /* 무시 */ }
      if (!keys.size) keys.add("dev_review");
      return [...keys].sort();
    },
  });
}

/* ── 체크 빌더 ── */

interface CheckDraft {
  type: CheckType;
  stage: string;
  value: string;
  pattern: string;
  path: string;
  jsonMode: "equals" | "contains";
  jsonValue: string;
}

const EMPTY_DRAFT: CheckDraft = {
  type: "output_contains",
  stage: "",
  value: "",
  pattern: "",
  path: "",
  jsonMode: "equals",
  jsonValue: "",
};

function draftFromCheck(c: EvalCheck): CheckDraft {
  const t = String(c.type || "output_contains") as CheckType;
  return {
    type: CHECK_TYPES.includes(t) ? t : "output_contains",
    stage: String(c.stage || ""),
    value: c.value != null ? String(c.value) : "",
    pattern: String(c.pattern || ""),
    path: String(c.path || ""),
    jsonMode: c.contains !== undefined && c.equals === undefined ? "contains" : "equals",
    jsonValue:
      c.equals !== undefined
        ? JSON.stringify(c.equals)
        : c.contains !== undefined
          ? String(c.contains)
          : "",
  };
}

/** draft → 계약 Check. 실패 시 에러 메시지 문자열 반환. */
function serializeCheck(d: CheckDraft, t: TFn): EvalCheck | string {
  switch (d.type) {
    case "stage_completed":
      if (!d.stage.trim()) return t("evals.needStage", "스테이지를 입력하세요");
      return { type: d.type, stage: d.stage.trim() };
    case "output_contains":
    case "output_not_contains":
      if (!d.value.trim()) return t("evals.needValue", "값을 입력하세요");
      return { type: d.type, value: d.value };
    case "output_regex":
      if (!d.pattern.trim()) return t("evals.needPattern", "정규식을 입력하세요");
      return { type: d.type, pattern: d.pattern };
    case "max_cost_usd": {
      const n = Number(d.value);
      if (!isFinite(n) || n < 0) return t("evals.badCost", "최대 비용은 0 이상의 숫자여야 합니다");
      return { type: d.type, value: n };
    }
    case "json_path": {
      if (!d.path.trim()) return t("evals.needPath", "JSON 경로를 입력하세요");
      const out: EvalCheck = { type: d.type, path: d.path.trim() };
      if (d.jsonMode === "contains") {
        if (!d.jsonValue.trim()) return t("evals.needValue", "값을 입력하세요");
        out.contains = d.jsonValue;
      } else {
        try {
          out.equals = JSON.parse(d.jsonValue);
        } catch {
          return t("evals.badJsonPath", "JSON 경로 값이 올바르지 않습니다");
        }
      }
      return out;
    }
  }
}

function CheckRow({
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

/* ── 등록/편집 폼 ── */

function EvalForm({
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

/* ── 실행 이력 ── */

const RUN_STATUS_TONE: Record<string, "muted" | "ok" | "warn" | "err" | "accent"> = {
  pending: "warn",
  scored: "ok",
  error: "err",
};

function runStatusLabel(t: TFn, st?: string): string {
  switch (st) {
    case "pending": return t("evals.runStatusPending", "대기");
    case "scored": return t("evals.runStatusScored", "채점 완료");
    case "error": return t("evals.runStatusError", "오류");
    default: return st || "?";
  }
}

function EvalRuns({ evalId }: { evalId: string }) {
  const { t } = useI18n();
  const qc = useQueryClient();
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["eval-runs", evalId],
    queryFn: () => apiGet<EvalRunsResponse>(`/evals/${encodeURIComponent(evalId)}/runs`),
    // 평가 실행은 비동기 — pending이 있으면 10초, 아니면 30초 폴링.
    refetchInterval: (q) =>
      (q.state.data?.items || []).some((r) => r.status === "pending") ? 10000 : 30000,
  });
  const items = data?.items || [];

  return (
    <div>
      <div className="mb-1.5 flex items-center justify-between">
        <span className="text-xs font-medium text-muted">{t("evals.history", "실행 이력")}</span>
        <Button variant="ghost" className="px-2 py-0.5 text-xs" onClick={() => { void refetch(); void qc.invalidateQueries({ queryKey: ["eval-runs", evalId] }); }}>
          {t("common.refresh", "새로고침")}
        </Button>
      </div>
      {isLoading && <Spinner />}
      {error && <ErrorBox message={error instanceof Error ? error.message : String(error)} onRetry={() => refetch()} />}
      {data && !items.length && <Empty>{t("evals.historyEmpty", "아직 실행 이력이 없습니다")}</Empty>}
      <div className="space-y-2">
        {items.map((r) => (
          <div key={r.id} className="rounded-lg border border-border p-2.5">
            <div className="flex flex-wrap items-center gap-2">
              <Badge tone={RUN_STATUS_TONE[r.status || ""] || "muted"}>{runStatusLabel(t, r.status)}</Badge>
              <ScoreBadge score={r.score} />
              <span className="text-xs text-muted">{fmtTs(r.created_at)}</span>
              {r.scored_at && <span className="text-xs text-muted">· {fmtTs(r.scored_at)}</span>}
              <span className="min-w-0 flex-1" />
              {r.run_id && (
                <a
                  href={`/?view=runs&run=${encodeURIComponent(r.run_id)}`}
                  className="text-xs font-medium text-accent hover:underline"
                >
                  {t("evals.viewRun", "실행 보기")}
                </a>
              )}
            </div>
            {r.error && <div className="mt-1.5 text-xs text-err">{r.error}</div>}
            {!!r.results?.length && (
              <div className="mt-1.5 flex flex-wrap gap-1">
                {r.results.map((res, i) => {
                  // check_type 우선, 없으면 원본 check 객체/문자열에서 타입 추출
                  // (레거시 응답 방어).
                  const rawType =
                    res.check_type ??
                    (res.check && typeof res.check === "object"
                      ? String((res.check as EvalCheck).type ?? "")
                      : typeof res.check === "string"
                        ? res.check
                        : "");
                  const label = checkLabel(t, rawType || undefined);
                  const checkJson =
                    res.check != null && typeof res.check === "object"
                      ? JSON.stringify(res.check)
                      : "";
                  const verdict = res.detail || (res.passed ? t("evals.checkPassed", "통과") : t("evals.checkFailed", "실패"));
                  return (
                    <span
                      key={i}
                      title={`${label}: ${verdict}${checkJson ? `\n${checkJson}` : ""}`}
                      className={`inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-xs font-medium ${
                        res.passed ? "bg-ok-soft text-ok" : "bg-err-soft text-err"
                      }`}
                    >
                      {res.passed ? "✓" : "✗"} {label}
                    </span>
                  );
                })}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

/* ── 상세 (편집 + 실행 + 이력) ── */

function EvalDetail({ item, workflowKeys }: { item: EvalItem; workflowKeys: string[] }) {
  const { t } = useI18n();
  const qc = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [running, setRunning] = useState(false);
  const [msg, setMsg] = useState("");

  // 목록 아이템은 요약 필드만 올 수 있으므로 상세는 단건 조회로 보강한다.
  const detail = useQuery({
    queryKey: ["eval", item.id],
    queryFn: () => apiGet<EvalItem>(`/evals/${encodeURIComponent(item.id)}`),
  });
  const full = detail.data || item;

  const runNow = async () => {
    setRunning(true);
    setMsg("");
    try {
      await apiPost<EvalRunResponse>(`/evals/${encodeURIComponent(item.id)}/run`);
      setMsg(t("evals.runStarted", "평가 실행을 시작했습니다"));
      await qc.invalidateQueries({ queryKey: ["evals"] });
      await qc.invalidateQueries({ queryKey: ["eval-runs", item.id] });
    } catch (e) {
      setMsg(e instanceof ApiError ? e.message : String(e));
    } finally {
      setRunning(false);
    }
  };

  if (editing) {
    return (
      <Card>
        <EvalForm
          initial={full}
          workflowKeys={workflowKeys}
          onDone={() => { setEditing(false); void detail.refetch(); }}
          onCancel={() => setEditing(false)}
        />
      </Card>
    );
  }

  return (
    <Card
      title={
        <span className="flex flex-wrap items-center gap-2">
          {full.name || item.id}
          <Badge tone="accent">{full.workflow_key || "-"}</Badge>
          {statusOf(full) === "archived" && <Badge tone="muted">{t("evals.statusArchived", "보관됨")}</Badge>}
        </span>
      }
      actions={
        <div className="flex flex-wrap gap-1.5">
          <Button variant="secondary" className="px-2.5 py-1 text-xs" onClick={() => setEditing(true)}>
            {t("evals.edit", "편집")}
          </Button>
          <Button className="px-2.5 py-1 text-xs" disabled={running || statusOf(full) === "archived"} onClick={runNow}>
            {running ? t("evals.running", "실행 요청 중…") : `▶ ${t("evals.runNow", "지금 실행")}`}
          </Button>
        </div>
      }
    >
      <div className="mb-3 grid grid-cols-1 gap-1.5 text-xs text-muted sm:grid-cols-2">
        <div>{t("evals.inputSummary", "입력")}: {jsonSummary(full.input, 160)}</div>
        <div>
          {t("evals.createdAt", "등록일")} {fmtTs(full.created_at)} · {t("evals.updatedAt", "수정일")} {fmtTs(full.updated_at)}
        </div>
      </div>
      {!!full.checks?.length && (
        <div className="mb-3 flex flex-wrap gap-1">
          {full.checks.map((c, i) => (
            <Badge key={i} tone="muted">{checkLabel(t, c.type)}</Badge>
          ))}
        </div>
      )}
      {msg && <div className="mb-2 rounded-lg border border-accent/30 bg-accent-soft p-2 text-xs text-accent">{msg}</div>}
      <EvalRuns evalId={item.id} />
    </Card>
  );
}

/* ── 페이지 ── */

export default function EvalsPage() {
  const { t } = useI18n();
  const qc = useQueryClient();
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState("active"); // active | archived | all
  const [expanded, setExpanded] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);

  const workflowKeys = useWorkflowKeys();

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["evals", statusFilter],
    queryFn: () =>
      apiGet<EvalsResponse>(
        statusFilter === "all" ? "/evals" : `/evals?status=${encodeURIComponent(statusFilter)}`,
      ),
    refetchInterval: 30000,
  });

  const items = useMemo(() => {
    const q = search.trim().toLowerCase();
    const list = data?.items || [];
    if (!q) return list;
    return list.filter(
      (e) =>
        String(e.name || "").toLowerCase().includes(q) ||
        String(e.workflow_key || "").toLowerCase().includes(q),
    );
  }, [data, search]);

  const archive = async (e: EvalItem) => {
    const archived = statusOf(e) === "archived";
    if (!archived && !window.confirm(t("evals.confirmArchive", "'{name}' 평가를 보관 처리할까요? 목록에서 숨겨집니다.").replace("{name}", e.name || e.id))) {
      return;
    }
    setBusyId(e.id);
    try {
      if (archived) {
        // 보관 해제 — PATCH status=active (DELETE는 보관 전용)
        await apiPatch(`/evals/${encodeURIComponent(e.id)}`, { status: "active" });
      } else {
        await apiDelete(`/evals/${encodeURIComponent(e.id)}`);
      }
      await qc.invalidateQueries({ queryKey: ["evals"] });
    } catch (err) {
      window.alert(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusyId(null);
    }
  };

  return (
    <div className="mx-auto max-w-4xl p-4">
      <PageHead title={t("evals.title", "평가")} desc={t("evals.desc", "워크플로 평가 케이스 — 실행 후 체크 결과와 점수로 품질을 측정합니다")}>
        <Button className="text-xs" onClick={() => setShowForm((v) => !v)}>
          {showForm ? t("evals.close", "닫기") : "+ " + t("evals.add", "평가 추가")}
        </Button>
      </PageHead>

      {showForm && (
        <Card className="mb-3">
          <EvalForm
            workflowKeys={workflowKeys.data || []}
            onDone={() => setShowForm(false)}
            onCancel={() => setShowForm(false)}
          />
        </Card>
      )}

      <div className="mb-3 flex flex-wrap items-center gap-2">
        <input
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder={t("evals.searchPh", "이름·워크플로 검색")}
          className={`${inputCls} max-w-xs flex-1`}
        />
        <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)} className={`${inputCls} w-auto`}>
          <option value="active">{t("evals.statusActive", "사용 중")}</option>
          <option value="archived">{t("evals.statusArchived", "보관됨")}</option>
          <option value="all">{t("evals.statusAll", "전체 상태")}</option>
        </select>
      </div>

      {isLoading && <Spinner />}
      {error && <ErrorBox message={error instanceof Error ? error.message : String(error)} onRetry={() => refetch()} />}
      {data && !items.length && (
        <Empty>
          <div>{search || statusFilter !== "active" ? t("evals.noMatch", "조건에 맞는 평가가 없습니다") : t("evals.empty", "등록된 평가가 없습니다")}</div>
          {!search && statusFilter === "active" && <div className="mt-1">{t("evals.emptyHint", "첫 평가 케이스를 추가해 워크플로 품질을 측정하세요")}</div>}
        </Empty>
      )}

      <div className="space-y-2">
        {items.map((e) => {
          const archived = statusOf(e) === "archived";
          const open = expanded === e.id;
          return (
            <Card key={e.id} className="!p-3">
              <button
                onClick={() => setExpanded(open ? null : e.id)}
                className="flex w-full flex-wrap items-center gap-x-3 gap-y-1 text-left"
              >
                <span className="min-w-0 flex-1 truncate text-sm font-medium text-ink">{e.name || e.id}</span>
                <Badge tone="accent">{e.workflow_key || "-"}</Badge>
                <Badge tone="muted">
                  {(e.checks?.length ?? 0) === 1
                    ? t("evals.checksOne", "체크 1개")
                    : t("evals.checks", "{n}개 체크").replace("{n}", String(e.checks?.length ?? 0))}
                </Badge>
                <span className="flex items-center gap-1 text-xs text-muted">
                  {t("evals.lastScore", "최근 점수")} <ScoreBadge score={e.last_score} />
                </span>
                <span className="text-xs text-muted">
                  {t("evals.lastRun", "최근 실행")} {e.last_run_at ? ago(e.last_run_at) : t("evals.neverRun", "실행 기록 없음")}
                </span>
                {archived && <Badge tone="muted">{t("evals.statusArchived", "보관됨")}</Badge>}
              </button>
              <div className="mt-2 flex flex-wrap items-center gap-1.5">
                <Button variant="ghost" className="px-2 py-1 text-xs" onClick={() => setExpanded(open ? null : e.id)}>
                  {open ? t("evals.close", "닫기") : t("evals.detail", "상세")}
                </Button>
                <Button
                  variant="ghost"
                  className="px-2 py-1 text-xs text-err"
                  disabled={busyId === e.id}
                  onClick={() => void archive(e)}
                >
                  {archived ? t("evals.unarchive", "복원") : t("evals.archive", "보관")}
                </Button>
              </div>
              {open && (
                <div className="mt-3 border-t border-border pt-3">
                  <EvalDetail item={e} workflowKeys={workflowKeys.data || []} />
                </div>
              )}
            </Card>
          );
        })}
      </div>
    </div>
  );
}
