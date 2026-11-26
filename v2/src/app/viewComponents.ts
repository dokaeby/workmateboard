import { lazy } from "react";
import type { ComponentType } from "react";
import type { ViewKey } from "../shared/api/types";

/**
 * 뷰 → lazy 컴포넌트 레지스트리. 컴포넌트가 아닌 상수라 views.tsx(컴포넌트
 * 전용 파일, Fast Refresh)와 분리한다.
 */
export const VIEW_COMPONENTS: Partial<Record<ViewKey, ComponentType>> = {
  office: lazy(() => import("../features/office")),
  monitor: lazy(() => import("../features/monitor")),
  radar: lazy(() => import("../features/radar")),
  gmail: lazy(() => import("../features/gmail")),
  calendar: lazy(() => import("../features/calendar")),
  runs: lazy(() => import("../features/runs")),
  inbox: lazy(() => import("../features/inbox")),
  projects: lazy(() => import("../features/projects")),
  sessionlog: lazy(() => import("../features/sessionlog")),
  risks: lazy(() => import("../features/risks")),
  approvals: lazy(() => import("../features/approvals")),
  perf: lazy(() => import("../features/perf")),
  insights: lazy(() => import("../features/insights")),
  automation: lazy(() => import("../features/automation")),
  system: lazy(() => import("../features/system")),
  archive: lazy(() => import("../features/archive")),
  cost: lazy(() => import("../features/cost")),
  developer: lazy(() => import("../features/developer")),
  playbooks: lazy(() => import("../features/playbooks")),
  connectors: lazy(() => import("../features/connectors")),
  triggers: lazy(() => import("../features/triggers")),
  builder: lazy(() => import("../features/builder")),
  team: lazy(() => import("../features/team")),
  agents: lazy(() => import("../features/agents")),
  goals: lazy(() => import("../features/goals")),
  audit: lazy(() => import("../features/audit")),
  settings: lazy(() => import("../features/settings")),
};
