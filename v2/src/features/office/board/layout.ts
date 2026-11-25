/** 보드 레이아웃 상수 — 레거시 index.html OFFICE_STAGES/OFFICE_NODES/OFFICE_EDGES/
 * OFFICE_ORCH_CLS를 그대로 이식. 좌표는 보드 % 단위(노드 중심 x,y / 너비 w%). */
import type { BoardNodeCode } from "./types";

export interface OfficeStageDef {
  node: Exclude<BoardNodeCode, "ORCH">;
  key: string;
}

/** dev_review stage_key → 노드 매핑 (레거시 OFFICE_STAGES와 동일 순서). */
export const OFFICE_STAGES: OfficeStageDef[] = [
  { node: "ANA", key: "analysis" },
  { node: "DEV", key: "implement" },
  { node: "TEST", key: "auto_checks" },
  { node: "SEC", key: "security" },
  { node: "CR", key: "code_review" },
  { node: "QA", key: "qa" },
  { node: "HUM", key: "approval" },
];

/** 레거시 OFFICE_ACTIVE와 동일 — 진행 중으로 간주하는 run 상태. */
export const OFFICE_ACTIVE = new Set([
  "running", "waiting_approval", "waiting_review", "queued", "needs_human",
]);

export interface OfficeNodeDef {
  code: BoardNodeCode;
  key: string | null;
  label: string;
  x: number;
  y: number;
  w: number;
}

/** 노드 배치: 상단 ORCH → ANA·DEV·TEST, 하단 SEC→CR→QA→HUM 스네이크. */
export const OFFICE_NODES: OfficeNodeDef[] = [
  { code: "ANA", key: "analysis", label: "요구사항 분석", x: 20.6, y: 40.7, w: 25 },
  { code: "DEV", key: "implement", label: "구현", x: 52.2, y: 40.1, w: 25 },
  { code: "TEST", key: "auto_checks", label: "자동 검사", x: 83.4, y: 40.4, w: 23 },
  { code: "ORCH", key: null, label: "오케스트레이터", x: 52.2, y: 16.5, w: 42 },
  { code: "SEC", key: "security", label: "보안 검토", x: 16.7, y: 77.2, w: 20 },
  { code: "CR", key: "code_review", label: "코드 리뷰", x: 41.4, y: 77.2, w: 20 },
  { code: "QA", key: "qa", label: "최종 검수", x: 65.3, y: 77.2, w: 19 },
  { code: "HUM", key: "approval", label: "사람 승인", x: 88.5, y: 77.2, w: 18 },
];

export const OFFICE_EDGES: [BoardNodeCode, BoardNodeCode][] = [
  ["ORCH", "ANA"], ["ORCH", "DEV"], ["ORCH", "TEST"],
  ["ANA", "DEV"], ["DEV", "TEST"],
  ["SEC", "CR"], ["CR", "QA"], ["QA", "HUM"],
];

/** ORCH 노드 테두리 상태: run.status → 노드 상태 클래스 (레거시와 동일). */
export const OFFICE_ORCH_CLS: Record<string, string> = {
  completed: "succeeded", failed: "failed",
  rejected: "failed", budget_exhausted: "failed", cancelled: "skipped",
  running: "running",
};
