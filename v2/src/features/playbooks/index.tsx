/** 플레이북 — 워크플로 실행 템플릿 목록/생성/실행/삭제 (GET·POST·DELETE /playbooks). */
import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { apiGet, apiPost, apiDelete, ApiError } from "../../shared/api/client";
import type { Playbook, PlaybooksResponse, WorkflowInfo, CustomWorkflow } from "../../shared/api/types";
import { useI18n, registerI18n } from "../../shared/i18n";
import { useAppStore } from "../../shared/store/appStore";
import { PageHead, Card, Empty, Spinner, ErrorBox, Button, Badge } from "../../shared/ui";
import { fmtTs } from "../../shared/lib/format";
import { STR } from "./i18n";

registerI18n(STR);


/** 내장 + 커스텀 워크플로 키 목록 (레거시 _allWorkflowKeys와 동일). */
function useWorkflowKeys() {
  return useQuery({
    queryKey: ["workflow-keys"],
    staleTime: 300000,
    queryFn: async (): Promise<string[]> => {
      const keys = new Set<string>();
      try {
        const wfs = await apiGet<WorkflowInfo[]>("/workflows");
        for (const w of wfs || []) if (w.key) keys.add(w.key);
      } catch { /* /workflows 실패 시 커스텀만 */ }
      try {
        const d = await apiGet<{ items?: CustomWorkflow[] }>("/workflows/custom");
        for (const c of d.items || []) {
          const spec = c.spec as { key?: string } | undefined;
          const n = c.name || spec?.key || "";
          if (n) keys.add(n);
        }
      } catch { /* 엔드포인트 미구현 시 무시 */ }
      return [...keys];
    },
  });
}

function NewPlaybookForm({ onDone }: { onDone: () => void }) {
  const { t } = useI18n();
  const qc = useQueryClient();
  const { data: wfs } = useWorkflowKeys();
  const [name, setName] = useState("");
  const [workflow, setWorkflow] = useState("");
  const [task, setTask] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ text: string; err?: boolean }>({ text: "" });

  const wf = workflow || wfs?.[0] || "";
  const submit = async () => {
    if (!name.trim() || !wf || !task.trim()) {
      setMsg({ text: t("pb.needAll", "이름·워크플로·작업 내용을 모두 입력하세요"), err: true });
      return;
    }
    setBusy(true);
    setMsg({ text: t("common.creating", "생성 중…") });
    try {
      await apiPost("/playbooks", { name: name.trim(), workflow: wf, input: { task: task.trim() } });
      setName(""); setTask("");
      setMsg({ text: t("common.created", "생성했습니다") });
      qc.invalidateQueries({ queryKey: ["playbooks"] });
      onDone();
    } catch (e) {
      setMsg({ text: e instanceof ApiError ? e.message : String(e), err: true });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mb-3 space-y-2 rounded-lg border border-border bg-surface-hover p-3">
      <div className="flex flex-wrap gap-2">
        <input
          value={name} onChange={(e) => setName(e.target.value)}
          placeholder={t("pb.namePh", "이름 (예: 주간 리뷰)")} required
          className="min-w-44 flex-1 rounded-md border border-border bg-surface px-2 py-1.5 text-sm"
        />
        <select
          value={wf} onChange={(e) => setWorkflow(e.target.value)}
          className="rounded-md border border-border bg-surface px-2 py-1.5 text-sm"
        >
          {(wfs || []).map((k) => <option key={k} value={k}>{k}</option>)}
          {!wfs?.length && <option value="">{t("pb.noWorkflow", "워크플로 없음")}</option>}
        </select>
      </div>
      <textarea
        value={task} onChange={(e) => setTask(e.target.value)} rows={3}
        placeholder={t("pb.taskPh", "작업 내용 (task) — 플레이북 실행 시 이 입력이 사용됩니다")}
        className="w-full rounded-md border border-border bg-surface px-2 py-1.5 text-sm"
      />
      <div className="flex items-center justify-end gap-2">
        {msg.text && <span className={`text-xs ${msg.err ? "text-err" : "text-ok"}`}>{msg.text}</span>}
        <Button variant="secondary" className="px-3 py-1 text-xs" onClick={onDone}>{t("common.cancel", "취소")}</Button>
        <Button className="px-3 py-1 text-xs" disabled={busy} onClick={submit}>{t("common.create", "생성")}</Button>
      </div>
    </div>
  );
}

export default function PlaybooksPage() {
  const { t } = useI18n();
  const qc = useQueryClient();
  const setView = useAppStore((s) => s.setView);
  const [showNew, setShowNew] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["playbooks"],
    queryFn: () => apiGet<PlaybooksResponse>("/playbooks"),
  });
  const items = data?.items || [];

  const run = async (p: Playbook) => {
    setBusyId(p.id);
    try {
      const r = await apiPost<{ run_id?: string; status?: string }>(`/playbooks/${encodeURIComponent(p.id)}/run`);
      qc.invalidateQueries({ queryKey: ["runs"] });
      setView("runs");
      if (r?.run_id) alert(t("pb.runStarted", "실행을 시작했습니다: {id}").replace("{id}", r.run_id));
    } catch (e) {
      alert(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusyId(null);
    }
  };

  const del = async (p: Playbook) => {
    if (!confirm(t("pb.confirmDelete", "'{name}' 플레이북을 삭제할까요?").replace("{name}", p.name || p.id))) return;
    try {
      await apiDelete(`/playbooks/${encodeURIComponent(p.id)}`);
      qc.invalidateQueries({ queryKey: ["playbooks"] });
    } catch (e) {
      alert(e instanceof ApiError ? e.message : String(e));
    }
  };

  return (
    <div className="mx-auto max-w-3xl p-4">
      <PageHead title={t("pb.title", "플레이북")} desc={t("pb.desc", "자주 쓰는 워크플로 실행 템플릿")}>
        <Button className="text-xs" onClick={() => setShowNew((v) => !v)}>
          {showNew ? t("common.close", "닫기") : "+ " + t("pb.new", "새 플레이북")}
        </Button>
        <Button variant="secondary" onClick={() => refetch()} className="text-xs">{t("common.refresh", "새로고침")}</Button>
      </PageHead>
      {showNew && <NewPlaybookForm onDone={() => setShowNew(false)} />}
      <Card>
        {isLoading && <Spinner />}
        {error && <ErrorBox message={error instanceof Error ? error.message : String(error)} onRetry={() => refetch()} />}
        {data && !items.length && <Empty>{t("pb.empty", "플레이북이 없습니다")}</Empty>}
        {items.map((p) => (
          <div key={p.id} className="flex flex-wrap items-center gap-3 border-b border-border py-3 last:border-0">
            <div className="min-w-0 flex-1">
              <div className="truncate text-sm font-medium text-ink">{p.name || t("pb.unnamed", "(이름 없음)")}</div>
              <div className="text-xs text-muted">
                {p.created_by ? <span className="mr-1 rounded bg-surface-hover px-1">{p.created_by}</span> : null}
                {fmtTs(p.created_at)}
                {p.input?.task ? <span className="ml-2 truncate">— {String(p.input.task).slice(0, 80)}</span> : null}
              </div>
            </div>
            <Badge tone="accent">{p.workflow || "-"}</Badge>
            <div className="flex gap-1.5">
              <Button className="px-2.5 py-1 text-xs" disabled={busyId === p.id} onClick={() => run(p)}>
                {busyId === p.id ? t("pb.running", "실행 중…") : t("pb.run", "실행")}
              </Button>
              <Button variant="secondary" className="px-2.5 py-1 text-xs" onClick={() => del(p)}>
                {t("common.delete", "삭제")}
              </Button>
            </div>
          </div>
        ))}
      </Card>
    </div>
  );
}
