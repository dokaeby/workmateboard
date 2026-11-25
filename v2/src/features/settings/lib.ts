/** settings feature 공용 상수·정규화 헬퍼 — 레거시 settings.html 로직 이식. */
import type { NotifyRule, SettingsResponse, WidgetDraft, WidgetPref } from "./types";

export const NOTIFY_TOOLS = ["", "codex", "claude", "gemini", "devin"] as const;
export const NOTIFY_STATUSES = ["working", "decision", "rest"] as const;
export type NotifyStatus = (typeof NOTIFY_STATUSES)[number];

/** 대시보드 위젯 정의 — index.html의 inbox 뷰 카드와 동기화. */
export const DASH_WIDGETS: { id: string; key: string; fallback: string }[] = [
  { id: "ask", key: "settings.widget.ask", fallback: "자연어 쿼리" },
  { id: "inbox", key: "settings.widget.inbox", fallback: "알림함" },
  { id: "usage", key: "settings.widget.usage", fallback: "비용 요약" },
  { id: "reports", key: "settings.widget.reports", fallback: "리포트" },
  { id: "cards", key: "settings.widget.cards", fallback: "카드" },
];

export function widgetLabel(id: string, t: (k: string, f?: string) => string): string {
  const w = DASH_WIDGETS.find((d) => d.id === id);
  if (!w) return id;
  return t(w.key, w.fallback);
}

/** 레거시 규칙은 statuses[] 또는 단일 status 문자열 — statuses로 통일. */
export function ruleStatuses(r: NotifyRule): string[] {
  if (Array.isArray(r.statuses)) return r.statuses.filter((s) => typeof s === "string");
  if (typeof r.status === "string" && r.status) return [r.status];
  return [];
}

export interface NotifyRuleDraft {
  tool: string;
  project: string;
  statuses: string[];
}

export function normalizeNotifyRules(rules: NotifyRule[] | undefined): NotifyRuleDraft[] {
  return (rules || []).map((r) => ({
    tool: r.tool || "",
    project: r.project || "",
    statuses: ruleStatuses(r),
  }));
}

/** prefs.dashboard_widgets → 정렬된 드래프트. 미등록 id는 뒤에 유지(레거시 동일). */
export function normalizeWidgets(prefs: SettingsResponse | null | undefined): WidgetDraft[] {
  const src: WidgetPref[] = Array.isArray(prefs?.dashboard_widgets)
    ? prefs.dashboard_widgets.filter((w) => w && w.id)
    : [];
  const byId = new Map<string, WidgetPref>();
  src.forEach((w) => byId.set(w.id, w));
  const known = DASH_WIDGETS.map((d) => d.id);
  const extra = src.map((w) => w.id).filter((id) => !known.includes(id));
  const items = [...known, ...extra].map((id, i) => ({ id, i, w: byId.get(id) }));
  items.sort((a, b) => {
    const oa = a.w && typeof a.w.order === "number" ? a.w.order : 900 + a.i;
    const ob = b.w && typeof b.w.order === "number" ? b.w.order : 900 + b.i;
    return oa - ob || a.i - b.i;
  });
  return items.map((it, i) => ({
    id: it.id,
    visible: it.w ? it.w.visible !== false : true,
    order: i,
  }));
}

/** 초 → "45s" / "12m 3s" / "2h 5m" (레거시 officeFmtSecs 동일 규칙). */
export function fmtSecs(s: number | null | undefined): string {
  const n = Math.max(0, Math.round(Number(s) || 0));
  if (n < 60) return `${n}s`;
  const m = Math.floor(n / 60);
  const r = n % 60;
  if (m < 60) return `${m}m${r ? ` ${r}s` : ""}`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}

/** ISO → "YYYY-MM-DD HH:MM:SS" 앞 19자 (레거시 officeFmtTsLocal 동일). */
export function fmtTsShort(iso: string | null | undefined): string {
  return (iso || "").slice(0, 19).replace("T", " ") || "-";
}

export function normalizeRole(r: unknown): "member" | "lead" | "admin" {
  const k = String(r || "").toLowerCase();
  return k === "lead" || k === "admin" ? k : "member";
}
