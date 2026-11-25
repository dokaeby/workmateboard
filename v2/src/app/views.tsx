import { lazy, Suspense } from "react";
import type { ComponentType } from "react";
import type { ViewKey } from "../shared/api/types";

/**
 * 뷰 레지스트리 — 각 feature의 페이지 컴포넌트를 React.lazy로 지연 로딩.
 * 초기 번들에는 셸만 포함되고 뷰 청크는 최초 진입 시 로드된다.
 * 새 기능 추가: features/<name>/index.tsx에 default export 컴포넌트를 두고
 * 여기 VIEW_COMPONENTS에 등록한다.
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
  audit: lazy(() => import("../features/audit")),
  settings: lazy(() => import("../features/settings")),
};

export function ViewLoader({ view }: { view: ViewKey }) {
  const C = VIEW_COMPONENTS[view];
  if (!C) return null;
  return (
    <Suspense fallback={<div className="p-6 text-sm text-muted">로딩…</div>}>
      <C />
    </Suspense>
  );
}
