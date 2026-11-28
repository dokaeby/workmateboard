/** evals — 온라인 평가 스케줄 (spec 008 US2).
 *  GET/POST/PATCH/DELETE /evals/schedules + GET /evals/schedules/{id}/history.
 *  run/stage 완료 시 sample_rate 확률로 기존 eval 파이프라인이 돌아
 *  eval_runs에 source='online'으로 기록된다. */
import { useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { apiGet, apiPost, apiPatch, apiDelete, ApiError } from "../../shared/api/client";
import { useI18n } from "../../shared/i18n";
import { Card, Empty, Spinner, ErrorBox, Badge, Button } from "../../shared/ui";
import { fmtTs } from "../../shared/lib/format";
import { inputCls, ago, type EvalsResponse } from "./types";
import { ScoreBadge } from "./runs";

interface EvalSchedule {
  id: string;
  name?: string;
  target?: string; // "run_completed" | "stage_completed"
  workflow_key?: string;
  sample_rate?: number; // 0..1
  eval_name?: string;
  enabled?: boolean;
  created_at?: string;
  updated_at?: string;
}
interface SchedulesResponse { items?: EvalSchedule[] }
interface SchedHistoryItem {
  id: string;
  eval_id?: string;
  run_id?: string | null;
  status?: string;
  score?: number | null;
  results?: unknown;
  error?: string | null;
  created_at?: string;
  scored_at?: string | null;
  source?: string;
}
interface SchedHistoryResponse { items?: SchedHistoryItem[] }

const TARGETS = ["run_completed", "stage_completed"] as const;

const HIST_STATUS_TONE: Record<string, "muted" | "ok" | "warn" | "err" | "accent"> = {
  pending: "warn",
  scored: "ok",
  error: "err",
};

function targetLabel(t: (k: string, f?: string) => string, target?: string): string {
  switch (target) {
    case "run_completed": return t("evals.on.targetRun", "run 완료 시");
    case "stage_completed": return t("evals.on.targetStage", "stage 완료 시");
    default: return target || "-";
  }
}

function histStatusLabel(t: (k: string, f?: string) => string, st?: string): string {
  switch (st) {
    case "pending": return t("evals.runStatusPending", "대기");
    case "scored": return t("evals.runStatusScored", "채점 완료");
    case "error": return t("evals.runStatusError", "오류");
    default: return st || "?";
  }
}

/** 스케줄 1건의 최근 온라인 평가 실행 — 펼침 시에만 조회. */
function ScheduleHistory({ scheduleId }: { scheduleId: string }) {
  const { t } = useI18n();
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["eval-sched-history", scheduleId],
    queryFn: () =>
      apiGet<SchedHistoryResponse>(`/evals/schedules/${encodeURIComponent(scheduleId)}/history`),
    staleTime: 15000,
  });
  const items = data?.items || [];

  if (isLoading) return <Spinner />;
  if (error) return <ErrorBox message={error instanceof Error ? error.message : String(error)} onRetry={() => refetch()} />;
  if (!items.length) return <Empty>{t("evals.on.histEmpty", "아직 온라인 평가 실행이 없습니다")}</Empty>;

  return (
    <div className="space-y-1.5">
      {items.map((r) => (
        <div key={r.id} className="flex flex-wrap items-center gap-2 rounded-md border border-border px-2.5 py-1.5 text-xs">
          <Badge tone={HIST_STATUS_TONE[r.status || ""] || "muted"}>{histStatusLabel(t, r.status)}</Badge>
          <ScoreBadge score={r.score} />
          {r.source && <Badge tone="accent">{r.source}</Badge>}
          <span className="text-muted">{fmtTs(r.created_at)}</span>
          {r.scored_at && <span className="text-muted">· {fmtTs(r.scored_at)}</span>}
          {r.run_id && (
            <a
              href={`/?view=runs&run=${encodeURIComponent(r.run_id)}`}
              className="ml-auto font-medium text-accent hover:underline"
            >
              {t("evals.viewRun", "실행 보기")}
            </a>
          )}
          {r.error && <div className="w-full text-err">{r.error}</div>}
        </div>
      ))}
    </div>
  );
}

/** 스케줄 등록/편집 폼. */
function ScheduleForm({
  initial,
  workflowKeys,
  evalNames,
  onDone,
  onCancel,
}: {
  initial?: EvalSchedule | null;
  workflowKeys: string[];
  evalNames: string[];
  onDone: () => void;
  onCancel: () => void;
}) {
  const { t } = useI18n();
  const qc = useQueryClient();
  const [name, setName] = useState(initial?.name || "");
  const [target, setTarget] = useState<string>(initial?.target || "run_completed");
  const [workflowKey, setWorkflowKey] = useState(initial?.workflow_key || "");
  const [samplePct, setSamplePct] = useState<number>(
    initial?.sample_rate != null ? Math.round(Number(initial.sample_rate) * 100) : 100,
  );
  const [evalName, setEvalName] = useState(initial?.eval_name || "");
  const [enabled, setEnabled] = useState(initial?.enabled !== false);
  // 슬라이더를 건드리지 않은 채 편집 저장하면 원래 sample_rate(소수 정밀도)를
  // 그대로 보존하기 위해 dirty 여부를 추적한다.
  const [sampleTouched, setSampleTouched] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  const submit = async () => {
    if (!name.trim()) { setErr(t("evals.needName", "이름을 입력하세요")); return; }
    if (!evalName.trim()) { setErr(t("evals.on.needEval", "평가 이름을 입력하세요")); return; }
    const rate = Math.min(100, Math.max(0, samplePct)) / 100;
    setBusy(true);
    setErr("");
    try {
      // workflow_key는 항상 전송 — 빈 문자열은 "전체 워크플로"로 해제한다
      // (백엔드 PATCH는 model_fields_set 기준이라 ""를 내면 필드가 지워진다).
      const body: Record<string, unknown> = {
        name: name.trim(),
        target,
        workflow_key: workflowKey.trim(),
        eval_name: evalName.trim(),
        enabled,
      };
      // 편집 시 슬라이더를 건드리지 않았으면 sample_rate를 보내지 않아
      // 원래 소수 정밀도 값이 보존된다. 생성 시에는 항상 전송.
      if (!initial?.id || sampleTouched) body.sample_rate = rate;
      if (initial?.id) {
        await apiPatch(`/evals/schedules/${encodeURIComponent(initial.id)}`, body);
      } else {
        await apiPost("/evals/schedules", body);
      }
      await qc.invalidateQueries({ queryKey: ["eval-schedules"] });
      onDone();
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
          <span className="mb-1 block text-xs font-medium text-muted">{t("evals.on.evalField", "평가")}</span>
          <input
            value={evalName}
            onChange={(e) => setEvalName(e.target.value)}
            placeholder={t("evals.on.evalPh", "실행할 평가 이름")}
            className={inputCls}
            list="eval-sched-eval-names"
          />
          <datalist id="eval-sched-eval-names">
            {evalNames.map((n) => <option key={n} value={n} />)}
          </datalist>
        </label>
      </div>
      <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-2">
        <label className="block">
          <span className="mb-1 block text-xs font-medium text-muted">{t("evals.on.targetField", "실행 시점")}</span>
          <select value={target} onChange={(e) => setTarget(e.target.value)} className={inputCls}>
            {TARGETS.map((tg) => <option key={tg} value={tg}>{targetLabel(t, tg)}</option>)}
          </select>
        </label>
        <label className="block">
          <span className="mb-1 block text-xs font-medium text-muted">{t("evals.workflowField", "워크플로")}</span>
          <input
            value={workflowKey}
            onChange={(e) => setWorkflowKey(e.target.value)}
            placeholder={t("evals.on.workflowPh", "비워두면 전체 워크플로 (예: dev_review)")}
            className={inputCls}
            list="eval-sched-workflow-keys"
          />
          <datalist id="eval-sched-workflow-keys">
            {workflowKeys.map((k) => <option key={k} value={k} />)}
          </datalist>
        </label>
      </div>
      <label className="block">
        <span className="mb-1 flex items-center justify-between text-xs font-medium text-muted">
          <span>{t("evals.on.sampleRate", "샘플링 비율")}</span>
          <span className="text-ink">{samplePct}%</span>
        </span>
        <input
          type="range" min={0} max={100} step={1} value={samplePct}
          onChange={(e) => { setSamplePct(Number(e.target.value)); setSampleTouched(true); }}
          className="w-full accent-accent"
        />
        <span className="text-[11px] text-muted">{t("evals.on.sampleHint", "완료 이벤트 중 이 비율만큼 온라인 평가를 실행합니다")}</span>
      </label>
      <label className="flex items-center gap-2 text-sm text-ink">
        <input type="checkbox" className="accent-accent" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
        {t("evals.on.enabledField", "활성화")}
      </label>

      {err && <div className="rounded-lg border border-err-border bg-err-soft p-2.5 text-sm text-err">{err}</div>}

      <div className="flex justify-end gap-2">
        <Button variant="secondary" className="px-3 py-1 text-xs" onClick={onCancel}>{t("common.cancel", "취소")}</Button>
        <Button className="px-3 py-1 text-xs" disabled={busy} onClick={submit}>
          {busy ? t("common.saving", "저장 중…") : t("common.save", "저장")}
        </Button>
      </div>
    </div>
  );
}

export function OnlineEvals({ workflowKeys }: { workflowKeys: string[] }) {
  const { t } = useI18n();
  const qc = useQueryClient();
  const [showForm, setShowForm] = useState(false);
  const [editing, setEditing] = useState<EvalSchedule | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["eval-schedules"],
    queryFn: () => apiGet<SchedulesResponse>("/evals/schedules"),
    refetchInterval: 30000,
  });
  // eval_name 선택지 — 등록된 평가 이름 (GET /evals)
  const evalNamesQ = useQuery({
    queryKey: ["eval-names"],
    staleTime: 60000,
    queryFn: async (): Promise<string[]> => {
      try {
        const d = await apiGet<EvalsResponse>("/evals?status=active");
        return (d.items || []).map((e) => e.name || "").filter(Boolean);
      } catch {
        return [];
      }
    },
  });
  const evalNames = useMemo(() => evalNamesQ.data || [], [evalNamesQ.data]);
  const items = data?.items || [];

  const toggleEnabled = async (s: EvalSchedule) => {
    setBusyId(s.id);
    try {
      await apiPatch(`/evals/schedules/${encodeURIComponent(s.id)}`, { enabled: !(s.enabled !== false) });
      await qc.invalidateQueries({ queryKey: ["eval-schedules"] });
    } catch (e) {
      window.alert(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusyId(null);
    }
  };

  const remove = async (s: EvalSchedule) => {
    if (!window.confirm(t("evals.on.confirmDelete", "'{name}' 온라인 평가 스케줄을 삭제할까요?").replace("{name}", s.name || s.id))) return;
    setBusyId(s.id);
    try {
      await apiDelete(`/evals/schedules/${encodeURIComponent(s.id)}`);
      await qc.invalidateQueries({ queryKey: ["eval-schedules"] });
    } catch (e) {
      window.alert(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusyId(null);
    }
  };

  return (
    <Card
      title={t("evals.on.title", "온라인 평가")}
      className="mt-3"
      actions={
        <Button
          variant="secondary"
          className="px-2.5 py-1 text-xs"
          onClick={() => { setEditing(null); setShowForm((v) => !v); }}
        >
          {showForm && !editing ? t("evals.close", "닫기") : "+ " + t("evals.on.add", "스케줄 추가")}
        </Button>
      }
    >
      <p className="mb-3 text-sm text-muted">
        {t("evals.on.desc", "run/stage 완료 이벤트를 샘플링해 기존 평가를 자동 실행합니다. 결과는 source=online으로 기록됩니다.")}
      </p>

      {(showForm || editing) && (
        <div className="mb-3 rounded-lg border border-border p-3">
          {/* key로 create/edit 전환 시 stale state를 리셋한다 */}
          <ScheduleForm
            key={editing?.id ?? "new"}
            initial={editing}
            workflowKeys={workflowKeys}
            evalNames={evalNames}
            onDone={() => { setShowForm(false); setEditing(null); }}
            onCancel={() => { setShowForm(false); setEditing(null); }}
          />
        </div>
      )}

      {isLoading && <Spinner />}
      {error && <ErrorBox message={error instanceof Error ? error.message : String(error)} onRetry={() => refetch()} />}
      {data && !items.length && (
        <Empty>{t("evals.on.empty", "등록된 온라인 평가 스케줄이 없습니다")}</Empty>
      )}

      <div className="space-y-2">
        {items.map((s) => {
          const open = expanded === s.id;
          const enabled = s.enabled !== false;
          return (
            <div key={s.id} className="rounded-lg border border-border">
              <div className="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2">
                <button
                  onClick={() => setExpanded(open ? null : s.id)}
                  className="min-w-0 flex-1 truncate text-left text-sm font-medium text-ink hover:text-accent"
                >
                  {s.name || s.id}
                  <span className="ml-2 text-xs font-normal text-muted">
                    {open ? "▾" : "▸"} {t("evals.on.history", "이력")}
                  </span>
                </button>
                <Badge tone="accent">{s.eval_name || "-"}</Badge>
                <Badge tone="muted">{targetLabel(t, s.target)}</Badge>
                {s.workflow_key && <Badge tone="muted">{s.workflow_key}</Badge>}
                <span className="text-xs text-muted">
                  {t("evals.on.sample", "샘플")} {Math.round((Number(s.sample_rate) || 0) * 100)}%
                </span>
                <label className="inline-flex items-center gap-1 text-xs text-muted">
                  <input
                    type="checkbox"
                    className="accent-accent"
                    checked={enabled}
                    disabled={busyId === s.id}
                    onChange={() => void toggleEnabled(s)}
                  />
                  {enabled ? t("evals.on.on", "켜짐") : t("evals.on.off", "꺼짐")}
                </label>
                <Button
                  variant="ghost"
                  className="px-2 py-0.5 text-xs"
                  disabled={busyId === s.id}
                  onClick={() => { setEditing(s); setShowForm(false); }}
                >
                  {t("evals.edit", "편집")}
                </Button>
                <Button
                  variant="ghost"
                  className="px-2 py-0.5 text-xs text-err"
                  disabled={busyId === s.id}
                  onClick={() => void remove(s)}
                >
                  {t("common.delete", "삭제")}
                </Button>
                <span className="text-[11px] text-muted">{ago(s.updated_at || s.created_at)}</span>
              </div>
              {open && (
                <div className="border-t border-border px-3 py-2.5">
                  <ScheduleHistory scheduleId={s.id} />
                </div>
              )}
            </div>
          );
        })}
      </div>
    </Card>
  );
}
