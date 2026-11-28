import type { ViewKey } from "../shared/api/types";

export interface NavItem {
  view: ViewKey;
  label: string;
  icon: string;
  minRole?: "lead" | "admin";
}

/** 사이드바/모바일바 공용 내비게이션 — index.html nav-item과 동일 구성.
 *  spec 010 US3 — Agent Office 그룹 순(관제→관리→이력→도구→보안→비용→
 *  성능→보고→프로젝트→설정)으로 재배치. 라벨/권한/prefs 게이트는 유지. */
export const NAV_ITEMS: NavItem[] = [
  // 관제 — 실시간 관측/트레이스/알림 규칙
  { view: "office", label: "관측 센터", icon: "📡" },
  { view: "monitor", label: "Agent Insights", icon: "📡" },
  { view: "traces", label: "트레이스", icon: "≋", minRole: "lead" },
  { view: "alerts", label: "알림 규칙", icon: "🚨", minRole: "lead" },
  { view: "inbox", label: "알림", icon: "🔔" },
  // 관리 — 승인/에이전트/목표/작업/팀
  { view: "approvals", label: "승인", icon: "✅", minRole: "lead" },
  { view: "agents", label: "에이전트", icon: "🕸", minRole: "lead" },
  { view: "goals", label: "목표", icon: "🎯" },
  { view: "tasks", label: "작업", icon: "🗂" },
  { view: "team", label: "팀", icon: "👥", minRole: "lead" },
  // 이력 — 실행/세션/아카이브
  { view: "runs", label: "실행 이력", icon: "▶" },
  { view: "sessionlog", label: "세션 기록", icon: "🕘", minRole: "lead" },
  { view: "archive", label: "아카이브", icon: "🗄", minRole: "lead" },
  // 도구 — radar/gmail/calendar/플레이북/커넥터/트리거/빌더/루틴/평가/자동화/개발자
  { view: "radar", label: "Skill Radar", icon: "◈" },
  { view: "gmail", label: "Gmail Checker", icon: "✉" },
  { view: "calendar", label: "Calendar", icon: "▣" },
  { view: "playbooks", label: "플레이북", icon: "📒", minRole: "lead" },
  { view: "connectors", label: "커넥터", icon: "🔌", minRole: "lead" },
  { view: "triggers", label: "트리거", icon: "⚡", minRole: "lead" },
  { view: "builder", label: "워크플로 빌더", icon: "🧩", minRole: "lead" },
  { view: "routines", label: "루틴", icon: "🔁", minRole: "lead" },
  { view: "evals", label: "평가", icon: "🧪", minRole: "lead" },
  { view: "automation", label: "자동화", icon: "⚙", minRole: "admin" },
  { view: "developer", label: "개발자", icon: "🛠", minRole: "admin" },
  // 보안 — 리스크/감사 로그
  { view: "risks", label: "리스크", icon: "⚠", minRole: "lead" },
  { view: "audit", label: "감사 로그", icon: "🔍", minRole: "admin" },
  // 비용
  { view: "cost", label: "비용", icon: "💰", minRole: "lead" },
  // 성능/보고
  { view: "perf", label: "성과", icon: "📈", minRole: "lead" },
  { view: "insights", label: "인사이트", icon: "📊", minRole: "lead" },
  // 프로젝트
  { view: "projects", label: "프로젝트", icon: "📁", minRole: "lead" },
  // 설정
  { view: "system", label: "시스템", icon: "🖥", minRole: "admin" },
  { view: "settings", label: "설정", icon: "⚙" },
];

export function visibleNavItems(role: string | null, prefs: { sidebar_menu?: Record<string, boolean> } | null) {
  return NAV_ITEMS.filter((it) => {
    const rank = { member: 0, lead: 1, admin: 2 } as Record<string, number>;
    const userRank = rank[(role ?? "member")] ?? 0;
    if (it.minRole && userRank < rank[it.minRole]) return false;
    if (it.view === "office" || it.view === "traces" || it.view === "alerts" || it.view === "monitor" || it.view === "radar" || it.view === "settings") return true;
    return prefs?.sidebar_menu?.[it.view] === true;
  });
}
