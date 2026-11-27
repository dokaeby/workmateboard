/** agents — 공용 상수/라벨 헬퍼/스타일 (폼·조직도·드로어·목록이 공유). */
import { fmtAge } from "../../shared/lib/format";

export const ADAPTER_TYPES = ["claude", "codex", "gemini", "devin", "custom"];
// 표시/필터/폼 select 공용 전체 상태 어휘 — management 1차의 canonical
// 7값 + 레거시 별칭 archived. PATCH /agents는 서버에서 전이 allowlist를
// 거치므로 canonical 상태도 그대로 보낼 수 있다(H2).
export const DISPLAY_STATUSES = [
  "active",
  "idle",
  "running",
  "pending_approval",
  "paused",
  "error",
  "terminated",
  "archived",
];

export const STATUS_TONE: Record<string, "ok" | "warn" | "err" | "muted" | "accent"> = {
  active: "ok",
  idle: "muted",
  running: "ok",
  pending_approval: "warn",
  paused: "warn",
  error: "err",
  terminated: "muted",
  archived: "muted",
};

export const STATUS_LABEL_KEY: Record<string, string> = {
  active: "agents.statusActive",
  idle: "agents.statusIdle",
  running: "agents.statusRunning",
  pending_approval: "agents.statusPendingApproval",
  paused: "agents.statusPaused",
  error: "agents.statusError",
  terminated: "agents.statusTerminated",
  archived: "agents.statusArchived",
};

export function statusLabel(t: (k: string, f?: string) => string, st: string): string {
  return t(STATUS_LABEL_KEY[st] || "", st);
}

export function statusOf(a: { status?: string | null }): string {
  return String(a.status || "active").toLowerCase();
}

/** last_run_at → 상대 시간 (없으면 "-"). */
export function lastRunAgo(iso: string | null | undefined): string {
  if (!iso) return "-";
  const ms = Date.now() - new Date(iso).getTime();
  if (!isFinite(ms) || ms < 0) return "-";
  return fmtAge(ms / 1000);
}

export const inputCls =
  "w-full rounded-lg border border-border bg-surface px-2.5 py-1.5 text-sm outline-none focus:border-accent";
