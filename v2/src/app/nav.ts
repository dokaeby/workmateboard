import type { ViewKey } from "../shared/api/types";

export interface NavItem {
  view: ViewKey;
  label: string;
  icon: string;
  minRole?: "lead" | "admin";
}

/** 사이드바/모바일바 공용 내비게이션 — index.html nav-item과 동일 구성. */
export const NAV_ITEMS: NavItem[] = [
  { view: "office", label: "Agent Office", icon: "⬡" },
  { view: "monitor", label: "Agent Insights", icon: "📡" },
  { view: "radar", label: "Skill Radar", icon: "◈" },
  { view: "gmail", label: "Gmail Checker", icon: "✉" },
  { view: "calendar", label: "Calendar", icon: "▣" },
  { view: "runs", label: "실행 이력", icon: "▶" },
  { view: "inbox", label: "알림", icon: "🔔" },
  { view: "projects", label: "프로젝트", icon: "📁", minRole: "lead" },
  { view: "sessionlog", label: "세션 기록", icon: "🕘", minRole: "lead" },
  { view: "risks", label: "리스크", icon: "⚠", minRole: "lead" },
  { view: "approvals", label: "승인", icon: "✅", minRole: "lead" },
  { view: "perf", label: "성과", icon: "📈", minRole: "lead" },
  { view: "insights", label: "인사이트", icon: "📊", minRole: "lead" },
  { view: "automation", label: "자동화", icon: "⚙", minRole: "admin" },
  { view: "system", label: "시스템", icon: "🖥", minRole: "admin" },
  { view: "archive", label: "아카이브", icon: "🗄", minRole: "lead" },
  { view: "cost", label: "비용", icon: "💰", minRole: "lead" },
  { view: "developer", label: "개발자", icon: "🛠", minRole: "admin" },
  { view: "playbooks", label: "플레이북", icon: "📒", minRole: "lead" },
  { view: "connectors", label: "커넥터", icon: "🔌", minRole: "lead" },
  { view: "triggers", label: "트리거", icon: "⚡", minRole: "lead" },
  { view: "builder", label: "워크플로 빌더", icon: "🧩", minRole: "lead" },
  { view: "team", label: "팀", icon: "👥", minRole: "lead" },
  { view: "agents", label: "에이전트", icon: "🕸", minRole: "lead" },
  { view: "goals", label: "목표", icon: "🎯" },
  { view: "tasks", label: "작업", icon: "🗂" },
  { view: "routines", label: "루틴", icon: "🔁", minRole: "lead" },
  { view: "evals", label: "평가", icon: "🧪", minRole: "lead" },
  { view: "audit", label: "감사 로그", icon: "🔍", minRole: "admin" },
  { view: "settings", label: "설정", icon: "⚙" },
];

export function visibleNavItems(role: string | null, prefs: { sidebar_menu?: Record<string, boolean> } | null) {
  return NAV_ITEMS.filter((it) => {
    const rank = { member: 0, lead: 1, admin: 2 } as Record<string, number>;
    const userRank = rank[(role ?? "member")] ?? 0;
    if (it.minRole && userRank < rank[it.minRole]) return false;
    if (it.view === "office" || it.view === "monitor" || it.view === "radar" || it.view === "settings") return true;
    return prefs?.sidebar_menu?.[it.view] === true;
  });
}
