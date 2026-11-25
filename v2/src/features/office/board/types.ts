/** Agent Office 3D 보드 — 데이터 타입.
 * 레거시 index.html의 officeState/run/stage_run/audit 구조를 그대로 표현.
 * shared/api/types.ts의 Run/StageRun과 구조적으로 호환되도록 모든 필드를
 * optional로 두고, 보드 전용 확장 필드(tokens/model/stages/audit)를 추가한다. */

export type BoardNodeCode = "ANA" | "DEV" | "TEST" | "ORCH" | "SEC" | "CR" | "QA" | "HUM";

/** 상세 패널 선택 상태 — 레거시 officeState.selected와 동일. */
export type BoardSelection = { type: "orch" } | { type: "stage"; code: string };

export interface BoardStageRun {
  stage_key?: string;
  stage?: string;
  status?: string;
  attempt?: number;
  model?: string;
  tokens_in?: number;
  tokens_out?: number;
  started_at?: string | null;
  finished_at?: string | null;
  input?: unknown;
  output?: unknown;
  error?: string;
}

export interface BoardApproval {
  stage_key?: string;
  decision?: string;
  decided_by?: string;
  decided_at?: string;
  requested_at?: string;
  created_at?: string;
  comment?: string;
}

export interface BoardAuditEvent {
  id?: string;
  actor?: string;
  action?: string;
  object_ref?: string;
  detail?: Record<string, unknown> | string | null;
  created_at?: string;
}

export interface BoardRun {
  id: string;
  workflow?: string;
  version?: string | number;
  status: string;
  created_at?: string;
  updated_at?: string;
  input?: unknown;
  error?: string;
  approvals?: BoardApproval[];
  audit?: BoardAuditEvent[];
  /** 부모가 run 상세를 그대로 넘길 수 있도록 stages도 허용. */
  stages?: BoardStageRun[];
}

export interface BoardDiffFile {
  path?: string;
  status?: string;
  additions?: number;
  deletions?: number;
  diff?: string;
}

export interface BoardDiff {
  summary?: unknown;
  files?: BoardDiffFile[];
}

export interface StageBoardProps {
  /** 표시할 run. null이면 빈 상태(또는 demo=true면 가상 run). */
  run?: BoardRun | null;
  /** stage_key별 stage run 목록(재시도 포함 전체). 생략 시 run.stages 사용. */
  stageRuns?: BoardStageRun[];
  /** 선택된 노드 — controlled. 생략 시 내부 상태(기본 ORCH) 사용. */
  selected?: BoardSelection | null;
  onSelect?: (sel: BoardSelection) => void;
  /** true + run 없음 → officeDemoRunState() 가상 run을 1초 주기로 합성. */
  demo?: boolean;
  /** 승인 대기 시 승인/거절 버튼 — 부모가 POST /runs/{id}/approve를 연결. */
  onApprove?: (decision: "approve" | "reject") => void | Promise<void>;
  /** waiting_approval 시 승인 대상 diff 미리보기 데이터(부모가 fetch). */
  diff?: BoardDiff | null;
  diffLoading?: boolean;
  className?: string;
}
