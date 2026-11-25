/** 노드/엣지 상태 모델 + SVG 아이콘 — StageBoard에서 이동한 verbatim 추출. */
import type { ReactNode } from "react";
import { OFFICE_NODES, OFFICE_ORCH_CLS } from "./layout";
import type { BoardNodeCode, BoardRun, BoardStageRun } from "./types";

export interface NodeInfo {
  cls: string;
  statusText: string;
  retry: number; // attempt > 1 일 때 표시할 배수
}

/** 레거시 officeNodeInfo — ORCH는 run.status, 나머지는 stage 상태. */
export function nodeInfo(
  code: BoardNodeCode,
  run: BoardRun | null,
  stages: Map<string, BoardStageRun>,
): NodeInfo {
  if (code === "ORCH") {
    const st = run?.status || "queued";
    return { cls: OFFICE_ORCH_CLS[st] || "pending", statusText: st, retry: 0 };
  }
  const n = OFFICE_NODES.find((x) => x.code === code);
  const sr = n?.key ? stages.get(n.key) || null : null;
  return {
    cls: sr?.status || "pending",
    statusText: sr?.status || "pending",
    retry: sr ? (sr.attempt || 1) : 0,
  };
}

/** 레거시 officeEdgePath — 노드 중심 좌표 사이 직교(rounded) 경로. */
export function edgePath(a: { x: number; y: number }, b: { x: number; y: number }): string {
  const R = 4; // 코너 반경(%)
  const sameRow = Math.abs(b.y - a.y) < 2.5;
  const sameCol = Math.abs(b.x - a.x) < 2.5;
  if (sameRow || sameCol) return `M ${a.x} ${a.y} L ${b.x} ${b.y}`;
  const ortho = (pts: [number, number][]): string => {
    let d = `M ${pts[0][0]} ${pts[0][1]}`;
    for (let i = 1; i < pts.length - 1; i++) {
      const [px, py] = pts[i - 1];
      const [cx, cy] = pts[i];
      const [nx, ny] = pts[i + 1];
      const l1 = Math.hypot(cx - px, cy - py);
      const l2 = Math.hypot(nx - cx, ny - cy);
      const r = Math.min(R, l1 / 2, l2 / 2);
      const sx = (cx - px) / l1;
      const sy = (cy - py) / l1;
      const ex = (nx - cx) / l2;
      const ey = (ny - cy) / l2;
      d += ` L ${cx - sx * r} ${cy - sy * r}` +
        ` Q ${cx} ${cy} ${cx + ex * r} ${cy + ey * r}`;
    }
    const last = pts[pts.length - 1];
    return d + ` L ${last[0]} ${last[1]}`;
  };
  if (b.y > a.y + 6) {
    // 아래로: 수직 → 수평 → 수평(my) → 수직
    const my = a.y + (b.y - a.y) * 0.52;
    return ortho([[a.x, a.y], [a.x, my], [b.x, my], [b.x, b.y]]);
  }
  // 위/같은층: 수평 → 수직 엘보
  return ortho([[a.x, a.y], [b.x, a.y], [b.x, b.y]]);
}

export type EdgeState = "done" | "fail" | "live" | "dim";

/** 레거시 officeEdgeState — 화살표 끝 노드의 stage 상태로 간선 색상 결정. */
export function edgeState(
  toCode: BoardNodeCode,
  stages: Map<string, BoardStageRun>,
): EdgeState {
  const n = OFFICE_NODES.find((x) => x.code === toCode);
  if (!n || !n.key) return "dim";
  const st = stages.get(n.key)?.status || "pending";
  if (st === "succeeded") return "done";
  if (st === "failed") return "fail";
  if (st === "running") return "live";
  return "dim";
}

export const EDGE_CLS: Record<EdgeState, string> = {
  done: "office-edge-done",
  fail: "office-edge-fail",
  live: "office-edge-live",
  dim: "office-edge-dim",
};

// ── 노드 아이콘 — 레거시 OFFICE_ICONS와 동일한 24×24 stroke SVG ──
export const ICON_PROPS = {
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 1.8,
  strokeLinecap: "round",
  strokeLinejoin: "round",
} as const;

export const OFFICE_ICONS: Record<BoardNodeCode, ReactNode> = {
  ANA: (
    <svg {...ICON_PROPS}>
      <path d="M7 3h7l4 4v14H7z" /><path d="M14 3v4h4" />
      <circle cx="11" cy="13.5" r="2.4" /><path d="M11 11.5v-1M9 13.5h-1.6M12.6 13.5H14.2" />
    </svg>
  ),
  DEV: (
    <svg {...ICON_PROPS}>
      <path d="M8.5 7 4 12l4.5 5M15.5 7 20 12l-4.5 5" /><path d="M13.5 5l-3 14" />
    </svg>
  ),
  TEST: (
    <svg {...ICON_PROPS}>
      <path d="M9.5 3h5M10.5 3v5.5L5.5 17a2.4 2.4 0 0 0 2.1 3.5h8.8a2.4 2.4 0 0 0 2.1-3.5l-5-8.5V3" />
      <path d="M8 14.5h8" />
    </svg>
  ),
  ORCH: (
    <svg {...ICON_PROPS}>
      <circle cx="12" cy="12" r="3.4" />
      <path d="M12 2.5v3M12 18.5v3M2.5 12h3M18.5 12h3M5 5l2.1 2.1M16.9 16.9 19 19M19 5l-2.1 2.1M7.1 16.9 5 19" />
    </svg>
  ),
  SEC: (
    <svg {...ICON_PROPS}>
      <path d="M12 3l7.5 2.8v5.4c0 4.6-3.2 7.6-7.5 8.8-4.3-1.2-7.5-4.2-7.5-8.8V5.8z" />
      <path d="M9 11.8l2.2 2.2 4-4.2" />
    </svg>
  ),
  CR: (
    <svg {...ICON_PROPS}>
      <circle cx="6.5" cy="6" r="2.6" /><circle cx="6.5" cy="18" r="2.6" />
      <circle cx="17.5" cy="8" r="2.6" /><path d="M6.5 8.6v6.8M17.5 10.6c0 4.4-4.4 5.6-8.6 5.9" />
    </svg>
  ),
  QA: (
    <svg {...ICON_PROPS}>
      <circle cx="12" cy="12" r="8.5" /><path d="M8.2 12.2l2.6 2.6 5-5.4" />
    </svg>
  ),
  HUM: (
    <svg {...ICON_PROPS}>
      <circle cx="12" cy="8" r="4" /><path d="M4.5 20.5c.8-3.8 4-5.8 7.5-5.8s6.7 2 7.5 5.8" />
    </svg>
  ),
};
