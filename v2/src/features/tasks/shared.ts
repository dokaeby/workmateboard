/** tasks — 공용 상수/라벨 헬퍼/스타일 (폼·섹션·목록이 공유). */
export const TASK_STATUSES = [
  "backlog", "todo", "in_progress", "in_review",
  "waiting_approval", "blocked", "done", "cancelled",
];
export const TASK_PRIORITIES = ["low", "medium", "high", "urgent"];
export const ARTIFACT_KINDS = [
  "plan", "diff", "test_report", "security_report",
  "review", "screenshot", "deployment", "other",
];

export const STATUS_TONE: Record<string, "ok" | "warn" | "err" | "muted" | "accent"> = {
  backlog: "muted",
  todo: "muted",
  in_progress: "accent",
  in_review: "warn",
  waiting_approval: "warn",
  blocked: "err",
  done: "ok",
  cancelled: "muted",
};
export const PRIORITY_TONE: Record<string, "ok" | "warn" | "err" | "muted" | "accent"> = {
  low: "muted",
  medium: "muted",
  high: "warn",
  urgent: "err",
};

export const inputCls =
  "w-full rounded-lg border border-border bg-surface px-2.5 py-1.5 text-sm outline-none focus:border-accent";
export const smallBtn = "px-3 py-1 text-xs";

export interface MgmtAgentOption { id: string; name?: string; role?: string }

export function statusLabel(t: (k: string, f?: string) => string, st: string): string {
  return t(`tasks.st.${st}`, st);
}
export function priorityLabel(t: (k: string, f?: string) => string, p: string): string {
  return t(`tasks.pri.${p}`, p);
}
export function kindLabel(t: (k: string, f?: string) => string, k: string): string {
  return t(`tasks.kind.${k}`, k);
}

export function fmtBytes(n: number | null | undefined): string {
  const v = Number(n);
  if (!isFinite(v) || v < 0) return "-";
  if (v < 1024) return `${v} B`;
  if (v < 1024 * 1024) return `${(v / 1024).toFixed(1)} KB`;
  return `${(v / (1024 * 1024)).toFixed(1)} MB`;
}
