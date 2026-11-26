/** 워크플로 빌더 — 커스텀 워크플로 스펙(JSON) 작성·저장·실행·삭제 + 버전 기록.
 *  API 계약: GET/POST /workflows/custom, PATCH/DELETE /workflows/custom/{name},
 *  GET /workflows/custom/{name}/revisions[/{rev}], POST .../revisions/{rev}/restore.
 *  저장은 base_revision 낙관적 잠금 — 409 revision_conflict 시 충돌 UI를 띄운다. */
import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { apiGet, apiPost, apiDelete, ApiError } from "../../shared/api/client";
import type { CustomWorkflow } from "../../shared/api/types";
import { useI18n, type TFn } from "../../shared/i18n";
import "./i18n";
import { useAppStore } from "../../shared/store/appStore";
import { PageHead, Card, Empty, Spinner, ErrorBox, Button } from "../../shared/ui";
import { fmtTs, fmtAge } from "../../shared/lib/format";


interface CustomListResponse { items?: CustomWorkflow[] }

/** /workflows/custom/{name}/revisions 계약 — 백엔드가 리비전 관리를 제공하기 전까지
 *  모든 필드는 optional로 다룬다. */
interface WorkflowRevisionMeta {
  revision?: number;
  note?: string;
  created_by?: string;
  created_at?: string;
}
interface WorkflowRevisionsResponse { items?: WorkflowRevisionMeta[] }
interface WorkflowRevisionDetail extends WorkflowRevisionMeta {
  name?: string;
  spec?: unknown;
}

function enc(s: string): string {
  return encodeURIComponent(s);
}

function revsPath(name: string): string {
  return `/workflows/custom/${enc(name)}/revisions`;
}

function specToText(spec: unknown): string {
  return typeof spec === "string" ? spec : JSON.stringify(spec, null, 2);
}

/** ISO → 상대 시간 (created_at 없으면 "-"). */
function ageOf(iso: string | null | undefined): string {
  if (!iso) return "-";
  const ms = Date.now() - new Date(iso).getTime();
  if (!isFinite(ms) || ms < 0) return "-";
  return fmtAge(ms / 1000);
}

/** 409 revision_conflict 판별 — 표준 err() 엔벨로프(error.code)를 우선 파싱하고,
 *  레거시 플랫 형태({error:'revision_conflict'})는 메시지 정규식으로 폴백한다. */
function isRevisionConflict(e: unknown): boolean {
  if (!(e instanceof ApiError) || e.status !== 409) return false;
  return e.code === "revision_conflict" || /revision_conflict/.test(e.message || "");
}

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

/** 저장 충돌(409) 안내 — 최신 불러오기 / 강제 저장 선택. */
function ConflictNotice({
  current,
  busy,
  onReload,
  onForce,
}: {
  current?: number;
  busy: boolean;
  onReload: () => void;
  onForce: () => void;
}) {
  const { t } = useI18n();
  const msg = current != null
    ? t("wf.conflictRev", "다른 곳에서 먼저 수정됨 (현재 리비전 {rev})").replace("{rev}", String(current))
    : t("wf.conflict", "다른 곳에서 먼저 수정됨");
  return (
    <div className="mt-2 rounded-lg border border-warn-border bg-warn-soft p-2.5 text-xs text-warn">
      <div>{msg}</div>
      <div className="mt-1.5 flex flex-wrap gap-2">
        <Button variant="secondary" className="px-2.5 py-1 text-xs" disabled={busy} onClick={onReload}>
          {t("wf.conflictReload", "최신 버전 불러오기")}
        </Button>
        <Button variant="secondary" className="px-2.5 py-1 text-xs" disabled={busy} onClick={onForce}>
          {t("wf.conflictForce", "강제 저장")}
        </Button>
      </div>
    </div>
  );
}

export default function BuilderPage() {
  const { t } = useI18n();
  const qc = useQueryClient();
  const setView = useAppStore((s) => s.setView);
  const [name, setName] = useState("");
  const [specText, setSpecText] = useState("");
  const [task, setTask] = useState("");
  const [note, setNote] = useState("");
  const [status, setStatus] = useState<{ text: string; err?: boolean }>({ text: "" });
  const [busy, setBusy] = useState<"" | "save" | "run">("");
  // 에디터가 로드/저장한 기준 리비전 — 저장 시 base_revision으로 보낸다.
  const [baseRev, setBaseRev] = useState<number | null>(null);
  const [conflict, setConflict] = useState<{ current?: number } | null>(null);
  // 기존 워크플로인데 baseRev를 못 구한 상태 — CAS 없이 저장하면 경고 후 확인받는다.
  const [casWarn, setCasWarn] = useState(false);
  const [viewRev, setViewRev] = useState<number | null>(null);
  const [revBusy, setRevBusy] = useState(false);

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["custom-workflows"],
    queryFn: () => apiGet<CustomListResponse>("/workflows/custom"),
  });
  const items = data?.items || [];

  // 목록에 존재하는 커스텀 워크플로일 때만 버전 기록을 조회한다.
  const revName = items.some((w) => w.name === name) ? name : "";
  const { data: revsData, refetch: refetchRevs } = useQuery({
    queryKey: ["wf-revisions", revName],
    queryFn: () => apiGet<WorkflowRevisionsResponse>(revsPath(revName)),
    enabled: !!revName,
  });
  const revisions = revsData?.items || [];

  const { data: revDetail, isLoading: revDetailLoading } = useQuery({
    queryKey: ["wf-revision", revName, viewRev],
    queryFn: () => apiGet<WorkflowRevisionDetail>(`${revsPath(revName)}/${viewRev}`),
    enabled: !!revName && viewRev != null,
  });

  /** 리비전 목록의 최신 번호 — 없으면 null. */
  const latestRevOf = async (n: string): Promise<number | null> => {
    try {
      const r = await apiGet<WorkflowRevisionsResponse>(revsPath(n));
      const rev = r?.items?.[0]?.revision;
      return typeof rev === "number" ? rev : null;
    } catch {
      return null;
    }
  };

  const load = async (w: CustomWorkflow) => {
    const spec = specToText(w.spec);
    setSpecText(spec);
    setName(w.name || "");
    setNote("");
    setConflict(null);
    setCasWarn(false);
    setViewRev(null);
    setStatus({ text: t("wf.loaded", "불러왔습니다: {name}").replace("{name}", w.name) });
    // 기준 리비전 = 최신 리비전. 리비전 API 미구현 환경에서는 목록의 latest_revision을 시도.
    const rev = await latestRevOf(w.name || "");
    if (rev != null) {
      setBaseRev(rev);
    } else {
      const lr = (w as CustomWorkflow & { latest_revision?: number }).latest_revision;
      setBaseRev(typeof lr === "number" ? lr : null);
    }
  };

  const del = async (n: string) => {
    if (!confirm(t("wf.confirmDelete", "'{name}' 워크플로를 삭제할까요?").replace("{name}", n))) return;
    try {
      await apiDelete(`/workflows/custom/${enc(n)}`);
      qc.invalidateQueries({ queryKey: ["custom-workflows"] });
      qc.invalidateQueries({ queryKey: ["workflow-keys"] });
      if (name === n) { setName(""); setSpecText(""); setBaseRev(null); setConflict(null); setCasWarn(false); }
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

  const save = async (force = false) => {
    const p = prepare();
    if (!p) return;
    // 기존 워크플로인데 기준 리비전을 못 구했으면(baseRev=null) CAS 없이
    // 저장되는 것을 막는다 — 조용히 base_revision을 생략하지 않고 확인받는다.
    const exists = items.some((w) => w.name === p.name);
    if (!force && exists && baseRev == null) {
      setCasWarn(true);
      setStatus({
        text: t("wf.casUnknown", "기준 리비전을 확인할 수 없습니다 — 다른 곳에서 먼저 수정됐을 수 있습니다"),
        err: true,
      });
      return;
    }
    setCasWarn(false);
    setBusy("save");
    setStatus({ text: t("common.saving", "저장 중…") });
    const body: Record<string, unknown> = { name: p.name, spec: p.spec };
    if (!force && baseRev != null) body.base_revision = baseRev;
    if (note.trim()) body.note = note.trim();
    try {
      const saved = await apiPost<{ revision?: number }>("/workflows/custom", body);
      setName(p.name);
      setNote("");
      setConflict(null);
      setCasWarn(false);
      // 저장 후 기준 리비전을 최신으로 갱신 — 응답 revision 또는 목록 재조회.
      const rev = await latestRevOf(p.name);
      const newBase = rev ?? (typeof saved?.revision === "number" ? saved.revision : baseRev);
      setBaseRev(newBase);
      setStatus({ text: t("wf.saved", "저장했습니다: {name}").replace("{name}", p.name) });
      qc.invalidateQueries({ queryKey: ["custom-workflows"] });
      qc.invalidateQueries({ queryKey: ["workflow-keys"] });
      qc.invalidateQueries({ queryKey: ["wf-revisions"] });
    } catch (e) {
      if (isRevisionConflict(e)) {
        // 충돌 — 현재 리비전을 조회해 안내에 표시한다.
        const cur = await latestRevOf(p.name);
        setConflict({ current: cur ?? undefined });
        setStatus({
          text: cur != null
            ? t("wf.conflictRev", "다른 곳에서 먼저 수정됨 (현재 리비전 {rev})").replace("{rev}", String(cur))
            : t("wf.conflict", "다른 곳에서 먼저 수정됨"),
          err: true,
        });
      } else {
        setStatus({ text: e instanceof ApiError ? e.message : String(e), err: true });
      }
    } finally {
      setBusy("");
    }
  };

  /** 충돌 시 최신 리비전의 스펙을 에디터에 불러오고 base를 갱신한다. */
  const reloadLatest = async () => {
    // name이 비어 있으면(에디터에 spec.key만 있는 상태) 스펙에서 key를 복원한다.
    let rn = name.trim();
    if (!rn) {
      try {
        const s: unknown = JSON.parse(specText);
        if (s && typeof s === "object" && typeof (s as { key?: unknown }).key === "string") {
          rn = String((s as { key: string }).key).trim();
        }
      } catch {
        /* spec 파싱 실패 — 아래에서 early return */
      }
    }
    if (!rn) return;
    setRevBusy(true);
    try {
      const r = await apiGet<WorkflowRevisionsResponse>(revsPath(rn));
      const latest = r?.items?.[0];
      const rev = latest?.revision;
      if (typeof rev === "number") {
        const d = await apiGet<WorkflowRevisionDetail>(`${revsPath(rn)}/${rev}`);
        setSpecText(specToText(d?.spec));
        setBaseRev(rev);
      } else {
        setBaseRev(null);
      }
      setConflict(null);
      setStatus({ text: t("wf.loaded", "불러왔습니다: {name}").replace("{name}", rn) });
      refetchRevs();
    } catch (e) {
      setStatus({ text: e instanceof ApiError ? e.message : String(e), err: true });
    } finally {
      setRevBusy(false);
    }
  };

  /** 특정 리비전을 새 리비전으로 복원 — 에디터 스펙도 복원본으로 교체한다. */
  const restore = async (rev: number) => {
    if (!revName) return;
    const msg = t("wf.confirmRestore", "리비전 {rev} 내용으로 복원할까요? 새 리비전으로 저장됩니다.")
      .replace("{rev}", String(rev));
    if (!confirm(msg)) return;
    setRevBusy(true);
    try {
      const r = await apiPost<{ name?: string; revision?: number }>(
        `${revsPath(revName)}/${rev}/restore`,
      );
      const newRev = typeof r?.revision === "number" ? r.revision : null;
      // 새 리비전(최신)의 스펙을 읽어 에디터에 반영한다.
      if (newRev != null) {
        const d = await apiGet<WorkflowRevisionDetail>(`${revsPath(revName)}/${newRev}`);
        setSpecText(specToText(d?.spec));
        setBaseRev(newRev);
      }
      setViewRev(null);
      setStatus({
        text: t("wf.restored", "리비전 {rev}로 복원했습니다").replace("{rev}", String(newRev ?? rev)),
      });
      qc.invalidateQueries({ queryKey: ["custom-workflows"] });
      qc.invalidateQueries({ queryKey: ["workflow-keys"] });
      qc.invalidateQueries({ queryKey: ["wf-revisions"] });
      refetchRevs();
    } catch (e) {
      setStatus({ text: e instanceof ApiError ? e.message : String(e), err: true });
    } finally {
      setRevBusy(false);
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
                value={name} onChange={(e) => { setName(e.target.value); setConflict(null); setCasWarn(false); }}
                placeholder={t("wf.namePh", "워크플로 이름 (key)")}
                className="min-w-44 flex-1 rounded-md border border-border bg-surface px-2 py-1.5 text-sm"
              />
              <Button variant="secondary" className="px-3 py-1.5 text-xs" onClick={() => {
                const tpl = wfTemplate(t);
                setSpecText(JSON.stringify(tpl, null, 2));
                if (!name.trim()) setName(tpl.key);
                setBaseRev(null);
                setConflict(null);
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
              <input
                value={note} onChange={(e) => setNote(e.target.value)}
                placeholder={t("wf.notePh", "변경 메모 (선택)")}
                className="w-40 rounded-md border border-border bg-surface px-2 py-1.5 text-sm"
              />
              <Button className="px-3 py-1.5 text-xs" disabled={busy === "save"} onClick={() => save(false)}>
                {busy === "save" ? t("common.saving", "저장 중…") : t("common.save", "저장")}
              </Button>
              <Button variant="secondary" className="px-3 py-1.5 text-xs" disabled={busy === "run"} onClick={run}>
                {busy === "run" ? t("common.running", "실행 중…") : t("common.run", "실행")}
              </Button>
            </div>
            {baseRev != null && (
              <div className="mt-2 text-[11px] text-muted">
                {t("wf.baseRev", "기준 리비전 {rev}").replace("{rev}", String(baseRev))}
              </div>
            )}
            {conflict && (
              <ConflictNotice
                current={conflict.current}
                busy={busy === "save" || revBusy}
                onReload={reloadLatest}
                onForce={() => save(true)}
              />
            )}
            {casWarn && !conflict && (
              <div className="mt-2 rounded-lg border border-warn-border bg-warn-soft p-2.5 text-xs text-warn">
                <div>{t("wf.casUnknown", "기준 리비전을 확인할 수 없습니다 — 다른 곳에서 먼저 수정됐을 수 있습니다")}</div>
                <div className="mt-1.5 flex flex-wrap gap-2">
                  <Button variant="secondary" className="px-2.5 py-1 text-xs" disabled={busy === "save"} onClick={() => save(true)}>
                    {t("wf.conflictForce", "강제 저장")}
                  </Button>
                  <Button variant="ghost" className="px-2.5 py-1 text-xs" onClick={() => setCasWarn(false)}>
                    {t("wf.casDismiss", "취소")}
                  </Button>
                </div>
              </div>
            )}
            {status.text && <div className={`mt-2 text-xs ${status.err ? "text-err" : "text-ok"}`}>{status.text}</div>}
          </Card>

          {revName && (
            <Card title={t("wf.revisions", "버전 기록")}>
              {revisions.length === 0 && (
                <Empty>{t("wf.revsEmpty", "저장된 버전이 없습니다")}</Empty>
              )}
              {revisions.map((r, i) => (
                <button
                  key={r.revision ?? r.created_at ?? i}
                  type="button"
                  onClick={() => { if (typeof r.revision === "number") setViewRev(r.revision); }}
                  className="flex w-full items-center gap-2 border-b border-border py-2 text-left last:border-0 hover:bg-surface-hover"
                >
                  <span className="shrink-0 font-mono text-xs font-semibold text-accent">
                    r{r.revision ?? "?"}
                  </span>
                  <span className="min-w-0 flex-1 truncate text-xs text-ink">
                    {r.note || "-"}
                  </span>
                  <span className="hidden shrink-0 text-[11px] text-muted sm:inline">
                    {r.created_by || "-"}
                  </span>
                  <span className="shrink-0 text-[11px] text-muted">
                    {ageOf(r.created_at)}
                  </span>
                </button>
              ))}
            </Card>
          )}

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

      {viewRev != null && revName && (
        <div className="fixed inset-0 z-50">
          <div className="absolute inset-0 bg-black/30" onClick={() => setViewRev(null)} />
          <aside
            role="dialog"
            aria-modal="true"
            aria-label={t("wf.revDetail", "버전 {rev} 상세").replace("{rev}", String(viewRev))}
            className="absolute inset-y-0 right-0 flex w-full max-w-lg flex-col overflow-y-auto border-l border-border bg-surface shadow-xl"
          >
            <div className="flex items-center gap-2 border-b border-border px-4 py-3">
              <h2 className="min-w-0 flex-1 truncate text-base font-bold text-ink">
                {t("wf.revDetail", "버전 {rev} 상세").replace("{rev}", String(viewRev))}
              </h2>
              <button
                type="button"
                onClick={() => setViewRev(null)}
                aria-label={t("common.close", "닫기")}
                className="rounded-md px-2 py-1 text-muted hover:bg-surface-hover"
              >
                ✕
              </button>
            </div>
            <div className="flex-1 space-y-3 p-4">
              {revDetailLoading && <Spinner />}
              {revDetail && (
                <>
                  <dl className="grid grid-cols-1 gap-x-4 gap-y-2 text-sm sm:grid-cols-2">
                    <div>
                      <dt className="text-xs text-muted">{t("wf.revNote", "메모")}</dt>
                      <dd className="text-ink">{revDetail.note || "-"}</dd>
                    </div>
                    <div>
                      <dt className="text-xs text-muted">{t("wf.revBy", "작성자")}</dt>
                      <dd className="text-ink">{revDetail.created_by || "-"}</dd>
                    </div>
                    <div>
                      <dt className="text-xs text-muted">{t("wf.revAt", "생성 시각")}</dt>
                      <dd className="text-ink">{fmtTs(revDetail.created_at)}</dd>
                    </div>
                  </dl>
                  <pre className="max-h-[50vh] overflow-auto rounded-md bg-surface-hover p-2 text-[11px] leading-relaxed text-muted">
                    {specToText(revDetail.spec)}
                  </pre>
                  <div className="flex justify-end">
                    <Button
                      variant="secondary"
                      className="px-3 py-1.5 text-xs"
                      disabled={revBusy}
                      onClick={() => restore(viewRev)}
                    >
                      {t("wf.restore", "이 버전으로 복원")}
                    </Button>
                  </div>
                </>
              )}
            </div>
          </aside>
        </div>
      )}
    </div>
  );
}
