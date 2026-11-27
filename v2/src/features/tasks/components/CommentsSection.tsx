/** tasks — 댓글 섹션 — GET/POST /management/tasks/{id}/comments.
 *  body는 plain text로 렌더(whitespace-pre-wrap) — HTML 원문 실행 금지(spec B). */
import { useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { apiGet, apiPost, ApiError } from "../../../shared/api/client";
import type { TaskComment, TaskCommentsResponse } from "../../../shared/api/types";
import { useI18n } from "../../../shared/i18n";
import { Badge, Button } from "../../../shared/ui";
import { fmtTs } from "../../../shared/lib/format";
import { inputCls, smallBtn } from "../shared";

export function CommentsSection({ taskId }: { taskId: string }) {
  const { t } = useI18n();
  const qc = useQueryClient();
  const [body, setBody] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["task-comments", taskId],
    queryFn: () => apiGet<TaskCommentsResponse>(`/management/tasks/${encodeURIComponent(taskId)}/comments`),
  });
  const items = useMemo(() => (Array.isArray(data?.items) ? data.items : []), [data]);

  const submit = async () => {
    if (!body.trim()) return;
    setBusy(true);
    setErr("");
    try {
      await apiPost(`/management/tasks/${encodeURIComponent(taskId)}/comments`, { body: body.trim() });
      setBody("");
      await qc.invalidateQueries({ queryKey: ["task-comments", taskId] });
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div>
      <h3 className="mb-1 text-xs font-semibold uppercase text-muted">{t("tasks.comments", "댓글")}</h3>
      {isLoading && <div className="text-xs text-muted">{t("common.loading", "로딩…")}</div>}
      {error && <div className="text-xs text-muted">{error instanceof Error ? error.message : String(error)}</div>}
      {data && !items.length && <div className="text-xs text-muted">{t("tasks.noComments", "댓글이 없습니다")}</div>}
      <ul className="space-y-2">
        {items.map((c: TaskComment) => (
          <li key={c.id} className="rounded-md border border-border bg-surface-hover px-2.5 py-2">
            <div className="mb-0.5 flex flex-wrap items-center gap-1.5 text-[11px] text-muted">
              <Badge tone={c.author_type === "agent" ? "accent" : c.author_type === "system" ? "muted" : "ok"}>
                {c.author_type || "human"}
              </Badge>
              <span className="font-medium text-ink-2">{c.author_id || "-"}</span>
              <span>{fmtTs(c.created_at)}</span>
              {c.edited_at && <span>({t("tasks.edited", "수정됨")})</span>}
            </div>
            <p className="whitespace-pre-wrap text-sm text-ink-2">{c.body}</p>
          </li>
        ))}
      </ul>
      <div className="mt-2 space-y-1.5">
        <textarea
          value={body}
          onChange={(e) => setBody(e.target.value)}
          rows={2}
          placeholder={t("tasks.commentPh", "댓글 작성…")}
          className={inputCls}
        />
        {err && <p className="text-xs text-err">{err}</p>}
        <div className="flex justify-end">
          <Button className={smallBtn} disabled={busy || !body.trim()} onClick={submit}>
            {busy ? t("common.saving", "저장 중…") : t("tasks.addComment", "댓글 추가")}
          </Button>
        </div>
      </div>
      <button type="button" className="mt-1 text-[11px] text-muted hover:text-ink" onClick={() => refetch()}>
        {t("common.refresh", "새로고침")}
      </button>
    </div>
  );
}
