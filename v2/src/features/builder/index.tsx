/** 워크플로 빌더 — 커스텀 워크플로 스펙(JSON) 작성·저장·실행·삭제 (/workflows/custom, /runs). */
import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { apiGet, apiPost, apiDelete, ApiError } from "../../shared/api/client";
import type { CustomWorkflow } from "../../shared/api/types";
import { useI18n, type TFn } from "../../shared/i18n";
import "./i18n";
import { useAppStore } from "../../shared/store/appStore";
import { PageHead, Card, Empty, Spinner, ErrorBox, Button } from "../../shared/ui";


interface CustomListResponse { items?: CustomWorkflow[] }


/** 레거시 WF_TEMPLATE과 동일한 예시 스펙. */
function wfTemplate(t: TFn) {
  return {
    key: "my_flow",
    version: "0.1.0",
    description: t("wf.tpl.desc", "커스텀 워크플로 예시"),
    entry: "analyze",
    stages: [
      { key: "analyze", kind: "llm", agent: "analysis", description: t("wf.tpl.analyze", "입력 분석") },
      { key: "review", kind: "llm", agent: "qa", description: t("wf.tpl.review", "결과 검수") },
      { key: "done", kind: "human", description: t("wf.tpl.done", "사람 확인") },
    ],
    branches: {},
  };
}

const SPEC_PLACEHOLDER = '{"key":"...","version":"0.1.0","entry":"...","stages":[...]}';

function parseSpec(raw: string, t: TFn): Record<string, unknown> {
  if (!raw.trim()) throw new Error(t("wf.needSpecJson", "스펙 JSON을 입력하세요"));
  const spec: unknown = JSON.parse(raw);
  if (!spec || typeof spec !== "object" || Array.isArray(spec)) {
    throw new Error(t("wf.specNotObject", "스펙은 JSON 객체여야 합니다"));
  }
  return spec as Record<string, unknown>;
}

export default function BuilderPage() {
  const { t } = useI18n();
  const qc = useQueryClient();
  const setView = useAppStore((s) => s.setView);
  const [name, setName] = useState("");
  const [specText, setSpecText] = useState("");
  const [task, setTask] = useState("");
  const [status, setStatus] = useState<{ text: string; err?: boolean }>({ text: "" });
  const [busy, setBusy] = useState<"" | "save" | "run">("");

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["custom-workflows"],
    queryFn: () => apiGet<CustomListResponse>("/workflows/custom"),
  });
  const items = data?.items || [];

  const load = (w: CustomWorkflow) => {
    const spec = typeof w.spec === "string" ? w.spec : JSON.stringify(w.spec, null, 2);
    setSpecText(spec);
    setName(w.name || "");
    setStatus({ text: t("wf.loaded", "불러왔습니다: {name}").replace("{name}", w.name) });
  };

  const del = async (n: string) => {
    if (!confirm(t("wf.confirmDelete", "'{name}' 워크플로를 삭제할까요?").replace("{name}", n))) return;
    try {
      await apiDelete(`/workflows/custom/${encodeURIComponent(n)}`);
      qc.invalidateQueries({ queryKey: ["custom-workflows"] });
      qc.invalidateQueries({ queryKey: ["workflow-keys"] });
      if (name === n) { setName(""); setSpecText(""); }
    } catch (e) {
      alert(e instanceof ApiError ? e.message : String(e));
    }
  };

  /** 저장/실행 공용 — 스펙 파싱 후 spec.key를 name과 일치시킨다(백엔드 요구사항). */
  const prepare = (): { name: string; spec: Record<string, unknown> } | null => {
    try {
      const spec = parseSpec(specText, t);
      const n = name.trim() || String(spec.key || "");
      if (!n) {
        setStatus({ text: t("wf.needName", "워크플로 이름(key)을 입력하세요"), err: true });
        return null;
      }
      spec.key = n;
      if (!spec.version) spec.version = "0.1.0";
      return { name: n, spec };
    } catch (e) {
      setStatus({ text: t("wf.jsonErr", "JSON 오류: {msg}").replace("{msg}", e instanceof Error ? e.message : String(e)), err: true });
      return null;
    }
  };

  const save = async () => {
    const p = prepare();
    if (!p) return;
    setBusy("save");
    setStatus({ text: t("common.saving", "저장 중…") });
    try {
      await apiPost("/workflows/custom", { name: p.name, spec: p.spec });
      setName(p.name);
      setStatus({ text: t("wf.saved", "저장했습니다: {name}").replace("{name}", p.name) });
      qc.invalidateQueries({ queryKey: ["custom-workflows"] });
      qc.invalidateQueries({ queryKey: ["workflow-keys"] });
    } catch (e) {
      setStatus({ text: e instanceof ApiError ? e.message : String(e), err: true });
    } finally {
      setBusy("");
    }
  };

  const run = async () => {
    const p = prepare();
    if (!p) return;
    const runTask = task.trim() || "custom workflow run";
    setBusy("run");
    setStatus({ text: t("common.running", "실행 중…") });
    try {
      const r = await apiPost<{ id?: string; run_id?: string }>("/runs", {
        workflow: p.name,
        task: runTask,
        metadata: { input: { task: runTask }, source: "builder" },
      });
      const rid = r?.id || r?.run_id || "";
      setStatus({ text: t("wf.runStarted", "실행 시작: {id}").replace("{id}", rid || p.name) });
      qc.invalidateQueries({ queryKey: ["runs"] });
      setView("runs");
    } catch (e) {
      setStatus({ text: e instanceof ApiError ? e.message : String(e), err: true });
    } finally {
      setBusy("");
    }
  };

  return (
    <div className="mx-auto max-w-4xl p-4">
      <PageHead title={t("wf.title", "워크플로 빌더")} desc={t("wf.desc", "커스텀 워크플로 스펙(JSON)을 작성·저장·실행")} />
      <div className="grid gap-3 lg:grid-cols-[320px_minmax(0,1fr)]">
        <Card title={t("wf.custom", "커스텀 워크플로")}>
          {isLoading && <Spinner />}
          {error && <ErrorBox message={error instanceof Error ? error.message : String(error)} onRetry={() => refetch()} />}
          {data && !items.length && <Empty>{t("wf.empty", "커스텀 워크플로가 없습니다")}</Empty>}
          {items.map((w) => (
            <div key={w.name} className="flex items-center gap-2 border-b border-border py-2 last:border-0">
              <span className="min-w-0 flex-1 truncate font-mono text-xs text-ink">{w.name}</span>
              <Button variant="ghost" className="px-2 py-0.5 text-xs" onClick={() => load(w)}>{t("wf.load", "불러오기")}</Button>
              <Button variant="ghost" className="px-2 py-0.5 text-xs text-err" onClick={() => del(w.name)}>{t("common.delete", "삭제")}</Button>
            </div>
          ))}
        </Card>
        <div className="min-w-0 space-y-3">
          <Card>
            <div className="mb-2 flex flex-wrap gap-2">
              <input
                value={name} onChange={(e) => setName(e.target.value)}
                placeholder={t("wf.namePh", "워크플로 이름 (key)")}
                className="min-w-44 flex-1 rounded-md border border-border bg-surface px-2 py-1.5 text-sm"
              />
              <Button variant="secondary" className="px-3 py-1.5 text-xs" onClick={() => {
                const tpl = wfTemplate(t);
                setSpecText(JSON.stringify(tpl, null, 2));
                if (!name.trim()) setName(tpl.key);
              }}>
                {t("wf.template", "템플릿 삽입")}
              </Button>
            </div>
            <textarea
              value={specText} onChange={(e) => setSpecText(e.target.value)} rows={16} spellCheck={false}
              placeholder={SPEC_PLACEHOLDER}
              className="w-full rounded-md border border-border bg-surface px-2 py-1.5 font-mono text-xs"
            />
            <div className="mt-2 flex flex-wrap gap-2">
              <input
                value={task} onChange={(e) => setTask(e.target.value)}
                placeholder={t("wf.taskPh", "실행 시 task 입력 (선택)")}
                className="min-w-44 flex-1 rounded-md border border-border bg-surface px-2 py-1.5 text-sm"
              />
              <Button className="px-3 py-1.5 text-xs" disabled={busy === "save"} onClick={save}>
                {busy === "save" ? t("common.saving", "저장 중…") : t("common.save", "저장")}
              </Button>
              <Button variant="secondary" className="px-3 py-1.5 text-xs" disabled={busy === "run"} onClick={run}>
                {busy === "run" ? t("common.running", "실행 중…") : t("common.run", "실행")}
              </Button>
            </div>
            {status.text && <div className={`mt-2 text-xs ${status.err ? "text-err" : "text-ok"}`}>{status.text}</div>}
          </Card>
          <Card title={t("wf.specTitle", "스펙 형식")}>
            <pre className="overflow-auto rounded-md bg-surface-hover p-2 text-[11px] leading-relaxed text-muted">{t("wf.specFormat", `{
  "key": "my_flow",            // 워크플로 이름 — 저장 name과 반드시 일치
  "version": "0.1.0",
  "description": "...",
  "entry": "analyze",          // 시작 stage 키 (또는 "finalize")
  "stages": [
    {"key": "analyze", "kind": "llm",      "agent": "analysis", "description": "..."},
    {"key": "check",   "kind": "function", "function": "run_tests"},
    {"key": "done",    "kind": "human",    "description": "사람 확인"}
  ],
  "branches": {}               // stage_key -> {output_field: {value: next_stage}}
}`)}</pre>
            <p className="mt-2 text-xs text-muted">
              {t("wf.specHint", "kind는 llm/function/human 중 하나. llm은 agent, function은 function 필수. spec.key는 워크플로 이름과 같아야 합니다.")}
            </p>
          </Card>
        </div>
      </div>
    </div>
  );
}
