/** tasks — 산출물 섹션 — GET/POST /management/tasks/{id}/artifacts.
 *  URI는 <a> 링크로만 렌더한다(서버가 https/승인된 repo-relative만 허용).
 *  체크섬은 앞 12자만 표시. */
import { useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { apiGet, apiPost, ApiError } from "../../../shared/api/client";
import type { TaskArtifact, TaskArtifactsResponse } from "../../../shared/api/types";
import { useI18n } from "../../../shared/i18n";
import { Badge, Button } from "../../../shared/ui";
import { fmtTs, safeUrl } from "../../../shared/lib/format";
import {
  ARTIFACT_KINDS, inputCls, smallBtn,
  kindLabel, fmtBytes,
} from "../shared";

export function ArtifactsSection({ taskId }: { taskId: string }) {
  const { t } = useI18n();
  const qc = useQueryClient();
  const [showForm, setShowForm] = useState(false);
  const [kind, setKind] = useState("other");
  const [name, setName] = useState("");
  const [uri, setUri] = useState("");
  const [mediaType, setMediaType] = useState("");
  const [checksum, setChecksum] = useState("");
  const [sizeBytes, setSizeBytes] = useState("");
  const [runId, setRunId] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  const { data, isLoading, error } = useQuery({
    queryKey: ["task-artifacts", taskId],
    queryFn: () => apiGet<TaskArtifactsResponse>(`/management/tasks/${encodeURIComponent(taskId)}/artifacts`),
  });
  const items = useMemo(() => (Array.isArray(data?.items) ? data.items : []), [data]);

  const submit = async () => {
    if (!name.trim() || !uri.trim()) {
      setErr(t("tasks.artNeedFields", "이름과 URI를 입력하세요"));
      return;
    }
    const body: Record<string, unknown> = {
      kind,
      name: name.trim(),
      uri: uri.trim(),
    };
    if (mediaType.trim()) body.media_type = mediaType.trim();
    if (checksum.trim()) body.checksum_sha256 = checksum.trim();
    if (sizeBytes.trim() && Number(sizeBytes) >= 0) body.size_bytes = Number(sizeBytes);
    if (runId.trim()) body.run_id = runId.trim();

    setBusy(true);
    setErr("");
    try {
      await apiPost(`/management/tasks/${encodeURIComponent(taskId)}/artifacts`, body);
      setShowForm(false);
      setName(""); setUri(""); setMediaType(""); setChecksum(""); setSizeBytes(""); setRunId("");
      await qc.invalidateQueries({ queryKey: ["task-artifacts", taskId] });
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div>
      <div className="mb-1 flex items-center justify-between">
        <h3 className="text-xs font-semibold uppercase text-muted">{t("tasks.artifacts", "산출물")}</h3>
        <Button variant="secondary" className="px-2 py-0.5 text-[11px]" onClick={() => setShowForm((v) => !v)}>
          {showForm ? t("common.close", "닫기") : "+ " + t("tasks.addArtifact", "산출물 추가")}
        </Button>
      </div>
      {isLoading && <div className="text-xs text-muted">{t("common.loading", "로딩…")}</div>}
      {error && <div className="text-xs text-muted">{error instanceof Error ? error.message : String(error)}</div>}
      {data && !items.length && <div className="text-xs text-muted">{t("tasks.noArtifacts", "등록된 산출물이 없습니다")}</div>}
      <ul className="space-y-1.5">
        {items.map((a: TaskArtifact) => (
          <li key={a.id} className="rounded-md border border-border bg-surface-hover px-2.5 py-2">
            <div className="flex flex-wrap items-center gap-1.5">
              <Badge tone="accent">{kindLabel(t, String(a.kind || "other"))}</Badge>
              <span className="min-w-0 flex-1 truncate text-sm font-medium text-ink">{a.name || a.id}</span>
              <span className="text-[11px] text-muted">{fmtTs(a.created_at)}</span>
            </div>
            <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-xs">
              {a.uri && (
                <a
                  href={safeUrl(a.uri)}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="max-w-full truncate text-accent hover:underline"
                >
                  {a.uri}
                </a>
              )}
              {a.checksum_sha256 && (
                <span className="font-mono text-muted" title={a.checksum_sha256}>
                  sha256:{String(a.checksum_sha256).slice(0, 12)}…
                </span>
              )}
              {a.size_bytes != null && <span className="text-muted">{fmtBytes(a.size_bytes)}</span>}
              {a.media_type && <span className="text-muted">{a.media_type}</span>}
              {a.run_id && <span className="text-muted">run:{String(a.run_id).slice(0, 8)}</span>}
            </div>
          </li>
        ))}
      </ul>
      {showForm && (
        <div className="mt-2 space-y-2 rounded-lg border border-border bg-surface-hover p-3">
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
            <label className="block">
              <span className="mb-1 block text-xs font-medium text-muted">{t("tasks.artKind", "종류")}</span>
              <select value={kind} onChange={(e) => setKind(e.target.value)} className={inputCls}>
                {ARTIFACT_KINDS.map((k) => (
                  <option key={k} value={k}>{kindLabel(t, k)}</option>
                ))}
              </select>
            </label>
            <label className="block">
              <span className="mb-1 block text-xs font-medium text-muted">{t("tasks.artName", "이름")}</span>
              <input value={name} onChange={(e) => setName(e.target.value)} className={inputCls} />
            </label>
            <label className="block sm:col-span-2">
              <span className="mb-1 block text-xs font-medium text-muted">{t("tasks.artUri", "URI (https 또는 repo-relative 경로)")}</span>
              <input value={uri} onChange={(e) => setUri(e.target.value)} placeholder="https://…" className={inputCls} />
            </label>
            <label className="block">
              <span className="mb-1 block text-xs font-medium text-muted">{t("tasks.artMediaType", "미디어 타입 (선택)")}</span>
              <input value={mediaType} onChange={(e) => setMediaType(e.target.value)} placeholder="text/markdown" className={inputCls} />
            </label>
            <label className="block">
              <span className="mb-1 block text-xs font-medium text-muted">{t("tasks.artChecksum", "SHA-256 (선택)")}</span>
              <input value={checksum} onChange={(e) => setChecksum(e.target.value)} className={`${inputCls} font-mono text-xs`} />
            </label>
            <label className="block">
              <span className="mb-1 block text-xs font-medium text-muted">{t("tasks.artSize", "크기 bytes (선택)")}</span>
              <input value={sizeBytes} onChange={(e) => setSizeBytes(e.target.value)} type="number" min="0" className={inputCls} />
            </label>
            <label className="block">
              <span className="mb-1 block text-xs font-medium text-muted">{t("tasks.artRunId", "연결 run ID (선택)")}</span>
              <input value={runId} onChange={(e) => setRunId(e.target.value)} className={`${inputCls} font-mono text-xs`} />
            </label>
          </div>
          {err && <p className="text-xs text-err">{err}</p>}
          <div className="flex justify-end gap-2">
            <Button variant="secondary" className={smallBtn} onClick={() => setShowForm(false)} disabled={busy}>
              {t("common.cancel", "취소")}
            </Button>
            <Button className={smallBtn} disabled={busy} onClick={submit}>
              {busy ? t("common.saving", "저장 중…") : t("common.save", "저장")}
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
