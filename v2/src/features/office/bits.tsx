import { Badge } from "../../shared/ui";
import type { RunListItem, OfficeStageRun, SessionView, Schedule } from "./types";

// dev_review stage_key → 표시 순서 (레거시 OFFICE_STAGES와 동일)
export const STAGE_ORDER: { key: string; label: string }[] = [
  { key: "analysis", label: "요구사항 분석" },
  { key: "implement", label: "구현" },
  { key: "auto_checks", label: "자동 검사" },
  { key: "security", label: "보안 검토" },
  { key: "code_review", label: "코드 리뷰" },
  { key: "qa", label: "최종 검수" },
  { key: "approval", label: "사람 승인" },
];
export const STAGE_LABEL: Record<string, string> = Object.fromEntries(STAGE_ORDER.map((s) => [s.key, s.label]));

// 레거시 OFFICE_ACTIVE와 동일
export const ACTIVE_STATUSES = new Set(["running", "waiting_approval", "waiting_review", "queued", "needs_human"]);

/** 레거시 _sessionArtifacts — artifacts가 JSON 문자열일 수 있음 */
export function sessionArtifacts(s: SessionView | undefined): string[] {
  let a = s?.artifacts;
  if (typeof a === "string") {
    try { a = JSON.parse(a); } catch { return []; }
  }
  return Array.isArray(a) ? a.filter((x): x is string => typeof x === "string") : [];
}

export function localDateStr(): string {
  const n = new Date();
  const p = (x: number) => String(x).padStart(2, "0");
  return `${n.getFullYear()}-${p(n.getMonth() + 1)}-${p(n.getDate())}`;
}

export function scheduleInputText(input: Schedule["input"]): string {
  if (input && typeof input === "object") {
    const task = (input as Record<string, unknown>).task;
    if (typeof task === "string" && task) return task;
    const summary = (input as Record<string, unknown>).summary;
    if (typeof summary === "string" && summary) return summary;
    try { return JSON.stringify(input).slice(0, 80); } catch { return String(input).slice(0, 80); }
  }
  if (input) return String(input).slice(0, 80);
  return "-";
}

export const RUN_STATUS_TONE: Record<string, "ok" | "warn" | "err" | "accent" | "muted"> = {
  succeeded: "ok", completed: "ok", running: "accent", queued: "muted",
  pending: "muted", waiting_approval: "warn", waiting_review: "warn",
  failed: "err", rejected: "err", skipped: "muted", cancelled: "muted",
  budget_exhausted: "err", needs_human: "warn",
};

export const SESSION_STATUS_TONE: Record<string, "ok" | "warn" | "err" | "accent" | "muted"> = {
  working: "accent", decision: "warn", rest: "muted", ended: "muted",
};

export function StatusBadge({ status }: { status?: string }) {
  return <Badge tone={RUN_STATUS_TONE[status || ""] ?? "muted"}>{status || "-"}</Badge>;
}

/** stage_key별 최신 attempt — 레거시 officeState.stages와 동일 규칙 */
export function latestStageRuns(stages: OfficeStageRun[] | undefined): Map<string, OfficeStageRun> {
  const map = new Map<string, OfficeStageRun>();
  for (const s of stages || []) {
    const key = s.stage_key || s.stage || "";
    if (!key) continue;
    const cur = map.get(key);
    if (!cur || (s.attempt || 1) > (cur.attempt || 1)) map.set(key, s);
  }
  return map;
}

/** 레거시 officePickRun — 활성 상태 우선(최신 created_at), 없으면 전체 최신 */
export function pickRun(runs: RunListItem[]): RunListItem | null {
  const dr = (runs || []).filter((r) => (r.workflow || "") === "dev_review");
  if (!dr.length) return null;
  const byCreated = (a: RunListItem, b: RunListItem) =>
    String(b.created_at || "").localeCompare(String(a.created_at || ""));
  const active = dr.filter((r) => ACTIVE_STATUSES.has(r.status)).sort(byCreated);
  if (active.length) return active[0];
  return dr.slice().sort(byCreated)[0];
}

export function stageTone(status?: string): "ok" | "warn" | "err" | "accent" | "muted" {
  return RUN_STATUS_TONE[status || ""] ?? "muted";
}
