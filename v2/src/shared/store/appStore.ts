/**
 * 앱 전역 클라이언트 상태 — Zustand 단방향 스토어.
 * 서버 상태(API 데이터)는 각 feature의 React Query 훅이 담당하고,
 * 여기는 UI 상태(현재 뷰/역할/사이드바/모바일바)만 둔다.
 */
import { create } from "zustand";
import type { Prefs, Role, ViewKey } from "../api/types";

export const ROLE_RANK: Record<Role, number> = { member: 0, lead: 1, admin: 2 };

interface AppState {
  view: ViewKey;
  role: Role | null;          // null = /auth/me 로드 전
  prefs: Prefs | null;
  sidebarOpen: boolean;       // 모바일 드로어
  setView: (v: ViewKey) => void;
  setRole: (r: Role | null) => void;
  setPrefs: (p: Prefs | null) => void;
  toggleSidebar: () => void;
  setSidebarOpen: (b: boolean) => void;
}

export const useAppStore = create<AppState>((set) => ({
  view: "office",
  role: null,
  prefs: null,
  sidebarOpen: false,
  setView: (view) => set({ view }),
  setRole: (role) => set({ role }),
  setPrefs: (prefs) => set({ prefs }),
  toggleSidebar: () => set((s) => ({ sidebarOpen: !s.sidebarOpen })),
  setSidebarOpen: (sidebarOpen) => set({ sidebarOpen }),
}));

export function roleAllows(min: "member" | "lead" | "admin", role: Role | null): boolean {
  const r = ROLE_RANK[role ?? "member"];
  return r >= ROLE_RANK[min];
}

/** 사이드바 메뉴 표시 규칙 — index.html과 동일: office/radar 항상, 나머지는 prefs.sidebar_menu. */
export const SIDEBAR_ALWAYS = new Set<ViewKey>(["office", "radar"]);
export function sidebarMenuVisible(view: ViewKey, prefs: Prefs | null): boolean {
  if (SIDEBAR_ALWAYS.has(view)) return true;
  return prefs?.sidebar_menu?.[view] === true;
}
