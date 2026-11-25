/** 트리거 — 세션 상태 조건 기반 자동 실행 규칙 목록/생성/토글/삭제 (/triggers*). */
import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { apiGet, apiPost, apiDelete, ApiError } from "../../shared/api/client";
import type { Playbook, WorkflowInfo } from "../../shared/api/types";
import { useI18n, type TFn } from "../../shared/i18n";
import { PageHead, Card, Empty, Spinner, ErrorBox, Button, Badge } from "../../shared/ui";
import { fmtTs } from "../../shared/lib/format";
import "./i18n";


interface Trigger {
  id: string;
  enabled?: boolean;
  match?: { tool?: string; project?: string; state?: string };
  action?: { type?: string; workflow?: string; playbook_id?: string; report_kind?: string };
  last_fired_at?: string | null;
  last_error?: string;
  created_at?: string;
}
interface TriggersResponse { items?: Trigger[] }

const TOOLS = ["", "devin", "codex", "claude", "gemini"];
const STATES = ["", "working", "decision", "rest"];
const REPORT_KINDS = ["daily", "weekly", "exec_daily", "exec_weekly"];

const selectCls = "rounded-md border border-border bg-surface px-2 py-1.5 text-sm";
const inputCls = "rounded-md border border-border bg-surface px-2 py-1.5 text-sm";

function useWorkflowKeys() {
  return useQuery({
    queryKey: ["workflow-keys"],
    staleTime: 300000,
    queryFn: async (): Promise<string[]> => {
      const keys = new Set<string>();
      try {
        const wfs = await apiGet<WorkflowInfo[]>("/workflows");
        for (const w of wfs || []) if (w.key) keys.add(w.key);
      } catch { /* 실패 시 기본값만 */ }
      try {
        const d = await apiGet<{ items?: { name: string; spec?: { key?: string } }[] }>("/workflows/custom");
        for (const c of d.items || []) {
          const n = c.name || c.spec?.key || "";
          if (n) keys.add(n);
        }
      } catch { /* 무시 */ }
      if (!keys.size) keys.add("dev_review");
      return [...keys];
    },
  });
}


function matchText(t: TFn, m?: Trigger["match"]): string {
  const p: string[] = [];
  if (m?.tool) p.push(`${t("trg.mTool", "도구")}=${m.tool}`);
  if (m?.project) p.push(`${t("trg.mProject", "프로젝트")}=${m.project}`);
  if (m?.state) p.push(`${t("trg.mState", "상태")}=${m.state}`);
  return p.join(" · ") || t("trg.mAll", "전체 세션");
}
function actionText(t: TFn, a?: Trigger["action"]): string {
  if (!a) return "-";
  if (a.type === "run_workflow") return `${t("trg.aRunWf", "워크플로 실행")}: ${a.workflow || "-"}`;
  if (a.type === "run_playbook") return `${t("trg.aPlaybook", "플레이북 실행")}: #${a.playbook_id || "-"}`;
  if (a.type === "report") return `${t("trg.aReport", "리포트 생성")}: ${a.report_kind || "daily"}`;
  return a.type || "-";
}

function NewTriggerForm({ onDone }: { onDone: () => void }) {
  const { t } = useI18n();
  const qc = useQueryClient();
  const { data: wfs } = useWorkflowKeys();
  const { data: pbs } = useQuery({
    queryKey: ["playbooks"],
    queryFn: () => apiGet<{ items?: Playbook[] }>("/playbooks"),
  });
  const [tool, setTool] = useState("");
  const [project, setProject] = useState("");
  const [state, setState] = useState("");
  const [actionType, setActionType] = useState("run_workflow");
  const [wf, setWf] = useState("");
  const [pb, setPb] = useState("");
  const [reportKind, setReportKind] = useState("daily");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ text: string; err?: boolean }>({ text: "" });

  const wfKeys = wfs || [];
  const playbooks = pbs?.items || [];
  const effectiveWf = wf || wfKeys[0] || "";
  const effectivePb = pb || playbooks[0]?.id || "";

  const submit = async () => {
    const match: Record<string, string> = {};
    if (tool) match.tool = tool;
    if (project.trim()) match.project = project.trim();
    if (state) match.state = state;
    const action: Record<string, string> = { type: actionType };
    if (actionType === "run_workflow") {
      if (!effectiveWf) { setMsg({ text: t("trg.pickWf", "워크플로를 선택하세요"), err: true }); return; }
      action.workflow = effectiveWf;
    } else if (actionType === "run_playbook") {
      if (!effectivePb) { setMsg({ text: t("trg.pickPb", "플레이북을 선택하세요"), err: true }); return; }
      action.playbook_id = effectivePb;
    } else {
      action.report_kind = reportKind;
    }
    setBusy(true);
    setMsg({ text: t("common.adding", "추가 중…") });
    try {
      await apiPost("/triggers", { match, action });
      setProject("");
      setMsg({ text: t("common.added", "추가했습니다") });
      qc.invalidateQueries({ queryKey: ["triggers"] });
      onDone();
    } catch (e) {
      setMsg({ text: e instanceof ApiError ? e.message : String(e), err: true });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mb-3 space-y-2 rounded-lg border border-border bg-surface-hover p-3">
      <div>
        <div className="mb-1 text-xs font-bold text-muted">{t("trg.match", "조건 (match)")}</div>
        <div className="flex flex-wrap gap-2">
          <select value={tool} onChange={(e) => setTool(e.target.value)} className={selectCls} aria-label={t("trg.toolAria", "도구")}>
            {TOOLS.map((v) => <option key={v} value={v}>{v ? v : t("trg.toolAll", "도구: 전체")}</option>)}
          </select>
          <input value={project} onChange={(e) => setProject(e.target.value)} placeholder={t("trg.projectPh", "프로젝트 (선택)")} className={inputCls} />
          <select value={state} onChange={(e) => setState(e.target.value)} className={selectCls} aria-label={t("trg.stateAria", "상태")}>
            {STATES.map((v) => <option key={v} value={v}>{v ? v : t("trg.stateAll", "상태: 전체")}</option>)}
          </select>
        </div>
      </div>
      <div>
        <div className="mb-1 text-xs font-bold text-muted">{t("trg.action", "동작 (action)")}</div>
        <div className="flex flex-wrap gap-2">
          <select value={actionType} onChange={(e) => setActionType(e.target.value)} className={selectCls} aria-label={t("trg.actionAria", "동작 종류")}>
            <option value="run_workflow">{t("trg.actRun", "워크플로 실행")}</option>
            <option value="run_playbook">{t("trg.actPlaybook", "플레이북 실행")}</option>
            <option value="report">{t("trg.actReport", "리포트 생성")}</option>
          </select>
          {actionType === "run_workflow" && (
            <select value={effectiveWf} onChange={(e) => setWf(e.target.value)} className={selectCls} aria-label={t("trg.wfAria", "워크플로")}>
              {wfKeys.map((k) => <option key={k} value={k}>{k}</option>)}
            </select>
          )}
          {actionType === "run_playbook" && (
            <select value={effectivePb} onChange={(e) => setPb(e.target.value)} className={selectCls} aria-label={t("trg.pbTitle", "플레이북")}>
              {playbooks.map((p) => <option key={p.id} value={p.id}>{p.name || p.id}</option>)}
              {!playbooks.length && <option value="">{t("trg.noPlaybook", "플레이북 없음")}</option>}
            </select>
          )}
          {actionType === "report" && (
            <select value={reportKind} onChange={(e) => setReportKind(e.target.value)} className={selectCls} aria-label={t("trg.reportAria", "리포트 종류")}>
              {REPORT_KINDS.map((k) => <option key={k} value={k}>{k}</option>)}
            </select>
          )}
        </div>
      </div>
      <div className="flex items-center justify-end gap-2">
        {msg.text && <span className={`text-xs ${msg.err ? "text-err" : "text-ok"}`}>{msg.text}</span>}
        <Button variant="secondary" className="px-3 py-1 text-xs" onClick={onDone}>{t("common.cancel", "취소")}</Button>
        <Button className="px-3 py-1 text-xs" disabled={busy} onClick={submit}>{t("common.add", "추가")}</Button>
      </div>
    </div>
  );
}

function TriggerRow({ trg }: { trg: Trigger }) {
  const { t } = useI18n();
  const qc = useQueryClient();
  const [busy, setBusy] = useState(false);
  const enabled = trg.enabled !== false;

  const toggle = async () => {
    setBusy(true);
    try {
      await apiPost(`/triggers/${encodeURIComponent(trg.id)}/toggle`, { enabled: !enabled });
      qc.invalidateQueries({ queryKey: ["triggers"] });
    } catch (e) {
      alert(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const del = async () => {
    if (!confirm(t("trg.confirmDelete", "이 트리거를 삭제할까요?"))) return;
    try {
      await apiDelete(`/triggers/${encodeURIComponent(trg.id)}`);
      qc.invalidateQueries({ queryKey: ["triggers"] });
    } catch (e) {
      alert(e instanceof ApiError ? e.message : String(e));
    }
  };

  return (
    <div className="flex flex-wrap items-center gap-3 border-b border-border py-3 last:border-0">
      <div className="min-w-0 flex-1">
        <div className="text-sm text-ink">
          <b>IF</b> {matchText(t, trg.match)}
          <span className="mx-1 text-muted">→</span>
          <b>THEN</b> {actionText(t, trg.action)}
        </div>
        <div className="mt-0.5 text-xs text-muted">
          {t("trg.lastFired", "마지막 발동")}: {fmtTs(trg.last_fired_at)}
          {" · "}{t("trg.created", "생성")}: {fmtTs(trg.created_at)}
        </div>
        {trg.last_error && <div className="mt-0.5 text-xs text-err">{t("trg.lastError", "오류")}: {trg.last_error}</div>}
      </div>
      <Badge tone={enabled ? "ok" : "err"}>{enabled ? t("trg.enabled", "사용") : t("trg.disabled", "중지")}</Badge>
      <div className="flex gap-1.5">
        <Button variant="secondary" className="px-2.5 py-1 text-xs" disabled={busy} onClick={toggle}>
          {enabled ? t("trg.disable", "중지") : t("trg.enable", "사용")}
        </Button>
        <Button variant="secondary" className="px-2.5 py-1 text-xs" onClick={del}>
          {t("common.delete", "삭제")}
        </Button>
      </div>
    </div>
  );
}

export default function TriggersPage() {
  const { t } = useI18n();
  const [showNew, setShowNew] = useState(false);
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["triggers"],
    queryFn: () => apiGet<TriggersResponse>("/triggers"),
    refetchInterval: 30000,
  });
  const items = data?.items || [];

  return (
    <div className="mx-auto max-w-3xl p-4">
      <PageHead title={t("trg.title", "트리거")} desc={t("trg.desc", "세션 상태가 조건에 맞으면 워크플로/플레이북/리포트를 자동 실행")}>
        <Button className="text-xs" onClick={() => setShowNew((v) => !v)}>
          {showNew ? t("common.close", "닫기") : "+ " + t("trg.add", "트리거 추가")}
        </Button>
        <Button variant="secondary" onClick={() => refetch()} className="text-xs">{t("common.refresh", "새로고침")}</Button>
      </PageHead>
      {showNew && <NewTriggerForm onDone={() => setShowNew(false)} />}
      <Card>
        {isLoading && <Spinner />}
        {error && <ErrorBox message={error instanceof Error ? error.message : String(error)} onRetry={() => refetch()} />}
        {data && !items.length && <Empty>{t("trg.empty", "등록된 트리거가 없습니다")}</Empty>}
        {items.map((trg) => <TriggerRow key={trg.id} trg={trg} />)}
      </Card>
    </div>
  );
}
