/** tasks — 실행 이력 — task detail 응답이 내장하는 runs/task_runs/current_run_id를 렌더.
 *  spec §3차 F는 task→run 전용 목록 엔드포인트를 정의하지 않아
 *  detail 응답 필드만 사용한다(없으면 current_run_id만 표시). */
import { useMemo } from "react";
import type { Task } from "../../../shared/api/types";
import { useI18n } from "../../../shared/i18n";
import { Badge } from "../../../shared/ui";
import { fmtTs } from "../../../shared/lib/format";
import { STATUS_TONE } from "../shared";

export function RunHistorySection({ task }: { task: Task }) {
  const { t } = useI18n();
  const embedded = useMemo(() => {
    const rows: { runId: string; status?: string; attempt?: number; at?: string }[] = [];
    const seen = new Set<string>();
    const push = (rid: unknown, status?: unknown, attempt?: unknown, at?: unknown) => {
      const id = String(rid || "");
      if (!id || seen.has(id)) return;
      seen.add(id);
      rows.push({
        runId: id,
        status: status ? String(status) : undefined,
        attempt: typeof attempt === "number" ? attempt : undefined,
        at: at ? String(at) : undefined,
      });
    };
    for (const r of task.task_runs || []) {
      push(r.run_id || r.id, r.status, r.attempt, r.created_at);
    }
    for (const r of task.runs || []) {
      push(r.run_id || r.id, r.status, r.attempt, r.created_at);
    }
    if (task.current_run_id) push(task.current_run_id);
    return rows;
  }, [task]);

  return (
    <div>
      <h3 className="mb-1 text-xs font-semibold uppercase text-muted">{t("tasks.runHistory", "실행 이력")}</h3>
      {!embedded.length && <div className="text-xs text-muted">{t("tasks.noRuns", "연결된 실행이 없습니다")}</div>}
      <ul className="space-y-1">
        {embedded.map((r) => (
          <li key={r.runId} className="flex flex-wrap items-center gap-2 rounded-md border border-border bg-surface-hover px-2.5 py-1.5 text-xs">
            <span className="font-mono text-ink">{r.runId.slice(0, 8)}</span>
            {r.status && <Badge tone={STATUS_TONE[r.status] ?? "muted"}>{r.status}</Badge>}
            {r.attempt != null && r.attempt > 1 && <span className="text-muted">#{r.attempt}</span>}
            {r.at && <span className="text-muted">{fmtTs(r.at)}</span>}
          </li>
        ))}
      </ul>
    </div>
  );
}
