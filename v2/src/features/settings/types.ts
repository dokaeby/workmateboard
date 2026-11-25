/** settings feature 타입 — GET/POST /settings, auth/*, users, health/detail 응답. */
import type { Role, ViewKey } from "../../shared/api/types";

export interface NotifyRule {
  tool?: string | null;
  project?: string | null;
  statuses?: string[];
  /** 레거시 단일 status 필드 — statuses로 정규화한다. */
  status?: string;
}

export interface WidgetPref {
  id: string;
  visible?: boolean;
  order?: number;
}

export interface WidgetDraft {
  id: string;
  visible: boolean;
  order: number;
}

/** GET /settings 응답 — 서버가 반환하는 6개 키. */
export interface SettingsResponse {
  gmail_check_minutes?: number;
  radar_interval?: string;
  notify_rules?: NotifyRule[];
  monthly_budget_usd?: number | null;
  dashboard_widgets?: WidgetPref[];
  sidebar_menu?: Record<string, boolean>;
}

/** POST /settings 성공 응답 — 서버가 정규화한 전체 prefs를 돌려준다. */
export interface SettingsSaveResponse {
  ok?: boolean;
  prefs?: Record<string, unknown>;
}

export interface UserRow {
  id: string;
  email?: string;
  display_name?: string;
  name?: string;
  role?: Role | string;
  created_at?: string;
}

export interface UsersResponse {
  users?: UserRow[];
}

export interface AuthSession {
  id: string;
  created_at?: string;
  expires_at?: string;
  current?: boolean;
}

export interface SessionsResponse {
  sessions?: AuthSession[];
}

export interface MeUser {
  id?: string;
  email?: string;
  display_name?: string;
  name?: string;
  role?: Role | string;
}

export interface SidebarMenuItem {
  view: ViewKey;
  label: string;
  minRole?: "lead" | "admin";
}

// 공유 API 타입 재수출 — settings 컴포넌트는 "../types"에서 import
export type { HealthDetail, HealthMachine } from "../../shared/api/types";
