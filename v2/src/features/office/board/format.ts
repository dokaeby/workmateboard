// ── 포맷 헬퍼 — 레거시 officeFmt* 계열과 동일 규칙 ──
import type { BoardRun, BoardStageRun } from "./types";

/** 레거시 officeFmtSecs: "45s" / "3m 20s" / "1h 5m" */
export function fmtSecs(s: number): string {
  s = Math.max(0, Math.round(s));
  if (s < 60) return s + "s";
  const m = Math.floor(s / 60);
  const r = s % 60;
  if (m < 60) return m + "m " + (r ? r + "s" : "");
  return Math.floor(m / 60) + "h " + (m % 60) + "m";
}

/** 레거시 officeFmtTs: "YYYY-MM-DD HH:MM:SS" (초까지, T→공백) */
export function fmtTsRaw(iso: string | null | undefined): string {
  return (iso || "").slice(0, 19).replace("T", " ") || "-";
}

export function parseMs(iso: string | null | undefined): number {
  if (!iso) return NaN;
  return Date.parse(iso);
}

export function fmtElapsedRun(run: BoardRun): string {
  const a = parseMs(run.created_at);
  const b = parseMs(run.updated_at);
  if (isNaN(a) || isNaN(b)) return "-";
  return fmtSecs((b - a) / 1000);
}

export function fmtDuration(sr: BoardStageRun | null | undefined, now: number): string {
  if (!sr || !sr.started_at) return "-";
  const start = parseMs(sr.started_at);
  const end = sr.finished_at ? parseMs(sr.finished_at) : now;
  if (isNaN(start) || isNaN(end)) return "-";
  return fmtSecs((end - start) / 1000);
}

/** diff 본문 200줄 제한 — 레거시 diffCap200과 동일 */
export function diffCap200(diff: string | undefined, t: (k: string, f?: string) => string): string {
  const s = String(diff ?? "");
  const lines = s.split("\n");
  return lines.length > 200
    ? lines.slice(0, 200).join("\n") + "\n" + t("office.diffTruncated", "… (이하 생략)")
    : s;
}

/** stage_key별 최신 attempt — 레거시 officeState.stages와 동일 규칙. */
export function latestStageRuns(stageRuns: BoardStageRun[] | undefined): Map<string, BoardStageRun> {
  const map = new Map<string, BoardStageRun>();
  for (const s of stageRuns || []) {
    const key = s.stage_key || s.stage || "";
    if (!key) continue;
    const cur = map.get(key);
    if (!cur || (s.attempt || 1) > (cur.attempt || 1)) map.set(key, s);
  }
  return map;
}
