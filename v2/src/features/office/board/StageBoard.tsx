/** Agent-Office 3D 보드 — 레거시 index.html의 officeRender* 계열을 React로 이식.
 * 노드 상태색·엣지 live/done/fail/dim·진행률·attempt ×N 배지·ORCH 노드·
 * 승인 대기 펄스·이벤트 로그를 레거시와 동일한 규칙으로 재현한다.
 * 데이터는 props로 받는다(부모가 office 데이터를 연결). */
import { useEffect, useId, useMemo, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { useI18n, registerI18n } from "../../../shared/i18n";
import { jsonSummary } from "../../../shared/lib/format";
import {
  OFFICE_STAGES, OFFICE_NODES, OFFICE_EDGES, OFFICE_ORCH_CLS,
  type OfficeNodeDef,
} from "./layout";
import { demoRunState } from "./demo";
import type {
  BoardAuditEvent, BoardNodeCode, BoardRun, BoardSelection,
  BoardStageRun, StageBoardProps,
} from "./types";
import "./office-board.css";

registerI18n({
  ko: {
    "office.chip.elapsed": "실행시간",
    "office.chip.tokens": "총 토큰",
    "office.chip.progress": "진행률",
    "office.waiting": "승인 대기",
    "office.waitingFor": "대기",
    "office.waitingIdle": "대기 중",
    "office.emptyRuns": "실행된 dev_review 워크플로가 없습니다",
    "office.selectAgent": "선택 에이전트",
    "office.eventLog": "이벤트 로그",
    "office.noEvents": "이벤트 없음",
    "office.node.ANA": "요구사항 분석",
    "office.node.DEV": "구현",
    "office.node.TEST": "자동 검사",
    "office.node.ORCH": "오케스트레이터",
    "office.node.SEC": "보안 검토",
    "office.node.CR": "코드 리뷰",
    "office.node.QA": "최종 검수",
    "office.node.HUM": "사람 승인",
    "office.kv.status": "상태",
    "office.kv.stage": "Stage",
    "office.kv.attempt": "시도",
    "office.kv.model": "모델",
    "office.kv.tokens": "토큰",
    "office.kv.duration": "소요시간",
    "office.kv.started": "시작",
    "office.kv.finished": "완료",
    "office.kv.error": "오류",
    "office.kv.failCause": "실패 원인",
    "office.kv.noError": "(오류 메시지 없음)",
    "office.kv.input": "입력",
    "office.kv.output": "출력",
    "office.kv.approvals": "승인",
    "office.kv.noApprovals": "기록 없음",
    "office.kv.workflow": "워크플로",
    "office.kv.runId": "Run ID",
    "office.kv.created": "생성",
    "office.kv.updated": "갱신",
    "office.kv.elapsed": "실행시간",
    "office.notRun": "아직 실행되지 않은 단계입니다.",
    "office.orchTitle": "ORCH · 오케스트레이터",
    "office.approve": "승인",
    "office.reject": "거절",
    "office.diffTitle": "승인 대상 변경 사항 (diff)",
    "office.diffEmpty": "변경된 파일이 없습니다",
    "office.diffTruncated": "… (이하 생략)",
    "office.demoBadge": "DEMO",
  },
  en: {
    "office.chip.elapsed": "Elapsed",
    "office.chip.tokens": "Total tokens",
    "office.chip.progress": "Progress",
    "office.waiting": "Waiting approval",
    "office.waitingFor": "waiting",
    "office.waitingIdle": "waiting",
    "office.emptyRuns": "No dev_review workflow runs",
    "office.selectAgent": "Select agent",
    "office.eventLog": "Event log",
    "office.noEvents": "No events",
    "office.node.ANA": "Requirements analysis",
    "office.node.DEV": "Implement",
    "office.node.TEST": "Auto checks",
    "office.node.ORCH": "Orchestrator",
    "office.node.SEC": "Security review",
    "office.node.CR": "Code review",
    "office.node.QA": "Final QA",
    "office.node.HUM": "Human approval",
    "office.kv.status": "Status",
    "office.kv.stage": "Stage",
    "office.kv.attempt": "Attempt",
    "office.kv.model": "Model",
    "office.kv.tokens": "Tokens",
    "office.kv.duration": "Duration",
    "office.kv.started": "Started",
    "office.kv.finished": "Finished",
    "office.kv.error": "Error",
    "office.kv.failCause": "Failure cause",
    "office.kv.noError": "(no error message)",
    "office.kv.input": "Input",
    "office.kv.output": "Output",
    "office.kv.approvals": "Approval",
    "office.kv.noApprovals": "No records",
    "office.kv.workflow": "Workflow",
    "office.kv.runId": "Run ID",
    "office.kv.created": "Created",
    "office.kv.updated": "Updated",
    "office.kv.elapsed": "Elapsed",
    "office.notRun": "This stage has not run yet.",
    "office.orchTitle": "ORCH · Orchestrator",
    "office.approve": "Approve",
    "office.reject": "Reject",
    "office.diffTitle": "Changes pending approval (diff)",
    "office.diffEmpty": "No changed files",
    "office.diffTruncated": "… (truncated)",
    "office.demoBadge": "DEMO",
  },
});

// ── 포맷 헬퍼 — 레거시 officeFmt* 계열과 동일 규칙 ──

/** 레거시 officeFmtSecs: "45s" / "3m 20s" / "1h 5m" */
function fmtSecs(s: number): string {
  s = Math.max(0, Math.round(s));
  if (s < 60) return s + "s";
  const m = Math.floor(s / 60);
  const r = s % 60;
  if (m < 60) return m + "m " + (r ? r + "s" : "");
  return Math.floor(m / 60) + "h " + (m % 60) + "m";
}

/** 레거시 officeFmtTs: "YYYY-MM-DD HH:MM:SS" (초까지, T→공백) */
function fmtTsRaw(iso: string | null | undefined): string {
  return (iso || "").slice(0, 19).replace("T", " ") || "-";
}

function parseMs(iso: string | null | undefined): number {
  if (!iso) return NaN;
  return Date.parse(iso);
}

function fmtElapsedRun(run: BoardRun): string {
  const a = parseMs(run.created_at);
  const b = parseMs(run.updated_at);
  if (isNaN(a) || isNaN(b)) return "-";
  return fmtSecs((b - a) / 1000);
}

function fmtDuration(sr: BoardStageRun | null | undefined, now: number): string {
  if (!sr || !sr.started_at) return "-";
  const start = parseMs(sr.started_at);
  const end = sr.finished_at ? parseMs(sr.finished_at) : now;
  if (isNaN(start) || isNaN(end)) return "-";
  return fmtSecs((end - start) / 1000);
}

/** stage_key별 최신 attempt — 레거시 officeState.stages와 동일 규칙. */
function latestStageRuns(stageRuns: BoardStageRun[] | undefined): Map<string, BoardStageRun> {
  const map = new Map<string, BoardStageRun>();
  for (const s of stageRuns || []) {
    const key = s.stage_key || s.stage || "";
    if (!key) continue;
    const cur = map.get(key);
    if (!cur || (s.attempt || 1) > (cur.attempt || 1)) map.set(key, s);
  }
  return map;
}

interface NodeInfo {
  cls: string;
  statusText: string;
  retry: number; // attempt > 1 일 때 표시할 배수
}

/** 레거시 officeNodeInfo — ORCH는 run.status, 나머지는 stage 상태. */
function nodeInfo(
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
function edgePath(a: { x: number; y: number }, b: { x: number; y: number }): string {
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

type EdgeState = "done" | "fail" | "live" | "dim";

/** 레거시 officeEdgeState — 화살표 끝 노드의 stage 상태로 간선 색상 결정. */
function edgeState(
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

const EDGE_CLS: Record<EdgeState, string> = {
  done: "office-edge-done",
  fail: "office-edge-fail",
  live: "office-edge-live",
  dim: "office-edge-dim",
};

// ── 노드 아이콘 — 레거시 OFFICE_ICONS와 동일한 24×24 stroke SVG ──
const ICON_PROPS = {
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 1.8,
  strokeLinecap: "round",
  strokeLinejoin: "round",
} as const;

const OFFICE_ICONS: Record<BoardNodeCode, ReactNode> = {
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

// ── SVG 레이어: 원근 그리드 / 피드백 버스 / 간선 ──

/** 원근 플로어 그리드 — 소실점(51%,5%)에서 방사선 + 아래로 벌어지는 수평선. */
function GridSvg() {
  const vp: [number, number] = [51, 5];
  let rays = "";
  for (let x = -10; x <= 110; x += 8) rays += `M${vp[0]} ${vp[1]}L${x} 100 `;
  const rows = [12, 19, 28, 40, 56, 78, 96];
  let hz = "";
  for (const y of rows) hz += `M0 ${y}H100 `;
  return (
    <svg className="office-grid" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">
      <path d={rays} stroke="rgba(70,160,230,.09)" strokeWidth=".28" />
      <path d={hz} stroke="rgba(70,160,230,.13)" strokeWidth=".32" />
      <path d="M0 100H100" stroke="rgba(120,200,255,.28)" strokeWidth=".4" />
    </svg>
  );
}

/** 빨간 피드백 버스 — 검증 단계 결과가 DEV로 되돌아가는 재작업 경로(장식). */
function FeedbackSvg() {
  const busY = 58.5;
  const xs = [16.7, 41.4, 65.3, 88.5];
  const up = [45, 48.5, 52.5, 56.5];
  const line = (x1: number, y1: number, x2: number, y2: number, mark?: string) => (
    <line key={`${x1},${y1},${x2},${y2},${mark || ""}`} x1={x1} y1={y1} x2={x2} y2={y2}
      markerEnd={mark ? `url(#office-fx-arr)` : undefined} />
  );
  const down = xs.map((x) => line(x, 63.5, x, busY));
  down.push(line(83.4, 51, 83.4, busY));
  const bus = line(Math.min(...xs), busY, Math.max(...xs), busY);
  const arrows = up.map((x) => (
    <path key={x} d={`M${x} ${busY} L${x} 51.5`} markerEnd="url(#office-fx-arr)" />
  ));
  const pulses = [30, 60, 85].map((x, i) => (
    <circle key={x} className="office-bus-pulse" cx={x} cy={busY} r=".7" fill="#ffd7dd">
      <animate attributeName="opacity" values="1;.15;1" dur={`${1.4 + i * 0.5}s`} repeatCount="indefinite" />
    </circle>
  ));
  const segs = [...down, bus, ...arrows];
  return (
    <svg className="office-fx" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">
      <defs>
        <marker id="office-fx-arr" markerUnits="userSpaceOnUse" markerWidth="1.5" markerHeight="1.5"
          refX="1.2" refY=".75" orient="auto">
          <path d="M0 0L1.5 .75L0 1.5z" fill="#ff8296" />
        </marker>
      </defs>
      <g className="office-fx-glow" stroke="#ff1d47" strokeWidth="3" strokeLinecap="round" opacity=".14" fill="none">
        {segs}
      </g>
      <g className="office-fx-body" stroke="#ff5d74" strokeWidth="1.2" strokeLinecap="round" fill="none">
        {segs}
      </g>
      <g className="office-fx-core" stroke="#ffd9de" strokeWidth=".5" strokeLinecap="round" fill="none">
        {[...down, bus]}
      </g>
      {pulses}
    </svg>
  );
}

function EdgesSvg({ stages }: { stages: Map<string, BoardStageRun> }) {
  const uid = useId().replace(/[^a-zA-Z0-9]/g, "");
  const arrId = `office-edge-arr-${uid}`;
  const byCode = Object.fromEntries(OFFICE_NODES.map((n) => [n.code, n]));
  return (
    <svg className="office-edges" viewBox="0 0 100 100" preserveAspectRatio="none">
      <defs>
        <marker id={arrId} markerUnits="userSpaceOnUse" markerWidth="2.2" markerHeight="2.2"
          refX="1.8" refY="1.1" orient="auto">
          <path d="M0 0L2.2 1.1L0 2.2z" fill="#9fd8ff" />
        </marker>
      </defs>
      {OFFICE_EDGES.map(([f, t]) => {
        const a = byCode[f];
        const b = byCode[t];
        const d = edgePath(a, b);
        const st = edgeState(t, stages);
        const cls = EDGE_CLS[st];
        return (
          <g key={`${f}-${t}`} className={`office-edge-grp ${cls}`}>
            {/* 3중 스트로크: 글로우 + 본선 + 밝은 코어 */}
            <path className="office-edge-glow" d={d} />
            <path className="office-edge-body" d={d} />
            <path className="office-edge-core" d={d} markerEnd={`url(#${arrId})`} />
            {st === "live" && <path className="office-edge-flow" d={d} />}
            {(st === "live" || st === "done") && (
              <circle
                className="office-packet" r="1.7"
                fill={st === "live" ? "#7fd4ff" : "#47acff"}
                color={st === "live" ? "#7fd4ff" : "#47acff"}
              >
                <animateMotion
                  dur={`${st === "live" ? "1.6" : "3.2"}s`}
                  begin={`${((f.charCodeAt(0) * 7) % 30) / 10}s`}
                  repeatCount="indefinite"
                  path={d}
                />
              </circle>
            )}
          </g>
        );
      })}
    </svg>
  );
}

// ── 상세 패널 ──

function Kv({ k, v, children }: { k: string; v?: ReactNode; children?: ReactNode }) {
  return (
    <div className="office-kv">
      <span>{k}</span>
      <b>{children ?? v ?? "-"}</b>
    </div>
  );
}

function StageDetail({
  node, sr, t,
}: {
  node: OfficeNodeDef;
  sr: BoardStageRun | null;
  t: (k: string, f?: string) => string;
}) {
  const head = (
    <div className="text-base font-bold text-ink" style={{ margin: "0 0 4px", overflowWrap: "anywhere" }}>
      {node.code} · {t(`office.node.${node.code}`, node.label)}
    </div>
  );
  if (!sr) {
    return (
      <div>
        {head}
        <Kv k={t("office.kv.status", "상태")} v="pending" />
        <div className="text-xs text-muted">{t("office.notRun", "아직 실행되지 않은 단계입니다.")}</div>
      </div>
    );
  }
  const tokens = (sr.tokens_in || 0) + (sr.tokens_out || 0);
  // 실패 단계: 오류를 빨간 박스로 강조 + 출력 앞부분 함께 표시
  const failBox = sr.status === "failed" ? (
    <div className="office-err-box">
      <b>{t("office.kv.failCause", "실패 원인")}</b>
      {"\n"}{sr.error || t("office.kv.noError", "(오류 메시지 없음)")}
    </div>
  ) : null;
  const outSlice = sr.status === "failed" ? 800 : 1200;
  const now = Date.now();
  return (
    <div>
      {head}
      <Kv k={t("office.kv.stage", "Stage")} v={node.key || "-"} />
      <Kv k={t("office.kv.status", "상태")} v={sr.status} />
      <Kv k={t("office.kv.attempt", "시도")} v={`attempt ${sr.attempt || 1}`} />
      <Kv k={t("office.kv.model", "모델")} v={sr.model || "-"} />
      <Kv k={t("office.kv.tokens", "토큰")}
        v={`in ${sr.tokens_in || 0} / out ${sr.tokens_out || 0} (합계 ${tokens})`} />
      <Kv k={t("office.kv.duration", "소요시간")} v={fmtDuration(sr, now)} />
      <Kv k={t("office.kv.started", "시작")} v={fmtTsRaw(sr.started_at)} />
      <Kv k={t("office.kv.finished", "완료")} v={fmtTsRaw(sr.finished_at)} />
      {failBox}
      {sr.error && sr.status !== "failed" && (
        <Kv k={t("office.kv.error", "오류")} v={sr.error} />
      )}
      <div className="office-kv" style={{ alignItems: "flex-start" }}>
        <span>{t("office.kv.input", "입력")}</span>
        <b>{jsonSummary(sr.input, 300)}</b>
      </div>
      <div className="office-kv"><span>{t("office.kv.output", "출력")}</span></div>
      <pre className="office-pre">
        {JSON.stringify(sr.output || {}, null, 2).slice(0, outSlice)}
      </pre>
    </div>
  );
}

function OrchDetail({ run, t }: { run: BoardRun; t: (k: string, f?: string) => string }) {
  const approvals = run.approvals || [];
  return (
    <div>
      <div className="text-base font-bold text-ink" style={{ margin: "0 0 4px", overflowWrap: "anywhere" }}>
        {t("office.orchTitle", "ORCH · 오케스트레이터")}
      </div>
      <Kv k={t("office.kv.status", "상태")} v={run.status} />
      <Kv k={t("office.kv.workflow", "워크플로")}
        v={`${run.workflow || "-"} v${run.version ?? "-"}`} />
      <Kv k={t("office.kv.runId", "Run ID")} v={run.id} />
      <Kv k={t("office.kv.created", "생성")} v={fmtTsRaw(run.created_at)} />
      <Kv k={t("office.kv.updated", "갱신")} v={fmtTsRaw(run.updated_at)} />
      <Kv k={t("office.kv.elapsed", "실행시간")} v={fmtElapsedRun(run)} />
      {run.error ? <Kv k={t("office.kv.error", "오류")} v={run.error} /> : null}
      <div className="office-kv" style={{ alignItems: "flex-start" }}>
        <span>{t("office.kv.input", "입력")}</span>
        <b>{jsonSummary(run.input, 400)}</b>
      </div>
      {approvals.length
        ? approvals.map((a, i) => (
          <Kv key={i} k={t("office.kv.approvals", "승인")}
            v={`${a.decision} · ${a.stage_key} · ${a.decided_by} · ${fmtTsRaw(a.decided_at)}${a.comment ? " — " + a.comment : ""}`} />
        ))
        : <Kv k={t("office.kv.approvals", "승인")} v={t("office.kv.noApprovals", "기록 없음")} />}
    </div>
  );
}

function diffCap200(diff: string | undefined): string {
  const s = String(diff ?? "");
  const lines = s.split("\n");
  return lines.length > 200
    ? lines.slice(0, 200).join("\n") + "\n… (이하 생략)"
    : s;
}

function DiffPreview({ diff, t }: { diff: StageBoardProps["diff"]; t: (k: string, f?: string) => string }) {
  const [open, setOpen] = useState<number | null>(null);
  const files = diff?.files || [];
  const summary = diff?.summary
    ? typeof diff.summary === "string"
      ? diff.summary
      : JSON.stringify(diff.summary).slice(0, 400)
    : "";
  return (
    <div style={{ marginTop: 12 }}>
      <div style={{ fontSize: 12, fontWeight: 700, color: "var(--muted)", marginBottom: 4 }}>
        {t("office.diffTitle", "승인 대상 변경 사항 (diff)")}
      </div>
      {summary && <div className="text-xs text-muted" style={{ marginBottom: 6 }}>{summary}</div>}
      {!files.length && <div className="text-xs text-muted">{t("office.diffEmpty", "변경된 파일이 없습니다")}</div>}
      {files.map((f, i) => (
        <div key={i}>
          <button
            type="button"
            className="office-diff-head"
            onClick={() => setOpen(open === i ? null : i)}
          >
            <span style={{ fontFamily: "ui-monospace, monospace", overflowWrap: "anywhere" }}>{f.path}</span>
            <span className="office-chip" style={{ padding: "1px 8px" }}>{f.status || "modified"}</span>
            <span className="text-xs text-muted" style={{ marginLeft: "auto", flex: "none" }}>
              +{f.additions || 0} / −{f.deletions || 0}
            </span>
          </button>
          {open === i && (
            <pre className="office-pre office-diff-pre">{diffCap200(f.diff)}</pre>
          )}
        </div>
      ))}
    </div>
  );
}

// ── 이벤트 로그 (run.audit 시간순, 최신 하단) ──

function EventLog({ audit, t }: { audit: BoardAuditEvent[]; t: (k: string, f?: string) => string }) {
  const ref = useRef<HTMLDivElement>(null);
  const prevLen = useRef(0);
  const grew = audit.length > prevLen.current;
  useEffect(() => {
    // 새 이벤트가 추가되면 하단으로 스크롤, 길이 감소(run 교체) 시 맨 위로
    const el = ref.current;
    if (el) el.scrollTop = grew ? el.scrollHeight : 0;
    prevLen.current = audit.length;
  }, [audit.length, grew]);

  return (
    <div
      ref={ref}
      className="max-h-72 overflow-y-auto rounded-xl border border-border bg-surface p-4 shadow-sm"
    >
      <h2 className="mb-2 text-base font-semibold text-ink">{t("office.eventLog", "이벤트 로그")}</h2>
      {!audit.length && <div className="office-empty">{t("office.noEvents", "이벤트 없음")}</div>}
      {audit.map((a, i) => {
        const det = a.detail && (typeof a.detail === "string"
          ? a.detail
          : Object.keys(a.detail).length
            ? JSON.stringify(a.detail).slice(0, 160)
            : "");
        return (
          <div className="office-log-row" key={a.id || i}>
            <span className="office-log-time">{fmtTsRaw(a.created_at)}</span>
            <span className="office-log-actor">{a.actor}</span>
            <span className="office-log-action">{a.action}</span>
            {a.object_ref ? <span className="office-log-ref">{a.object_ref}</span> : null}
            {det ? <span className="office-log-detail">{det}</span> : null}
          </div>
        );
      })}
    </div>
  );
}

// ── 메인 보드 ──

export function StageBoard({
  run: runProp,
  stageRuns: stageRunsProp,
  selected,
  onSelect,
  demo = false,
  onApprove,
  diff,
  diffLoading = false,
  className = "",
}: StageBoardProps) {
  const { t } = useI18n();
  // 1초 틱 — 실행 중 단계 소요시간/승인 대기 경과시간 갱신용
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);

  // demo 모드: run이 없고 demo=true면 가상 run을 주기적으로 합성
  const [demoState, setDemoState] = useState(() => (demo && !runProp ? demoRunState() : null));
  useEffect(() => {
    if (!demo || runProp) return;
    const id = setInterval(() => setDemoState(demoRunState()), 1000);
    return () => clearInterval(id);
  }, [demo, runProp]);

  const run = runProp ?? demoState?.run ?? null;
  const stageRuns = stageRunsProp ?? demoState?.stageRuns ?? run?.stages;

  const stages = useMemo(() => latestStageRuns(stageRuns), [stageRuns]);

  // 선택 상태 — controlled(selected prop) 또는 내부 상태(기본 ORCH)
  const [innerSel, setInnerSel] = useState<BoardSelection>({ type: "orch" });
  const sel: BoardSelection | null =
    selected !== undefined ? selected : run ? innerSel : null;
  const handleSelect = (s: BoardSelection) => {
    if (selected === undefined) setInnerSel(s);
    onSelect?.(s);
  };

  // 헤더 칩 — 레거시 officeRenderHeader와 동일 계산
  const tokens = useMemo(
    () => (stageRuns || []).reduce((acc, s) => acc + (s.tokens_in || 0) + (s.tokens_out || 0), 0),
    [stageRuns],
  );
  const done = OFFICE_STAGES.filter(
    ({ key }) => stages.get(key)?.status === "succeeded",
  ).length;
  const progress = Math.round((done / OFFICE_STAGES.length) * 100);

  // 승인 대기 펄스 — approval.created_at(없으면 requested_at/decided_at/updated_at) 기준
  const waitingSince = useMemo(() => {
    if (!run || run.status !== "waiting_approval") return null;
    const aps = run.approvals || [];
    const pend = aps.find((a) => !a.decided_at) || aps[aps.length - 1];
    const ts = pend && (pend.created_at || pend.requested_at || pend.decided_at)
      ? parseMs(pend.created_at || pend.requested_at || pend.decided_at)
      : parseMs(run.updated_at);
    return isNaN(ts) ? "idle" : ts;
  }, [run]);

  const [approveBusy, setApproveBusy] = useState(false);
  const handleApprove = async (decision: "approve" | "reject") => {
    if (!onApprove || approveBusy) return;
    setApproveBusy(true);
    try {
      await onApprove(decision);
    } finally {
      setApproveBusy(false);
    }
  };

  if (!run) {
    return (
      <div className={`office-board ${className}`}>
        <div className="office-empty">{t("office.emptyRuns", "실행된 dev_review 워크플로가 없습니다")}</div>
      </div>
    );
  }

  const isOrchSel = !sel || sel.type === "orch";
  const selNode = !isOrchSel
    ? OFFICE_NODES.find((n) => n.code === sel!.code) || null
    : null;
  const selStageRun = selNode?.key ? stages.get(selNode.key) || null : null;
  const showApprove = run.status === "waiting_approval" && !!onApprove;
  const showDiff = run.status === "waiting_approval" &&
    (isOrchSel || selNode?.code === "HUM" || selNode?.key === "approval");

  return (
    <div className={`office-board ${className}`}>
      {/* 상태 칩: 실행시간 / 총 토큰 / 진행률 */}
      <div className="office-status">
        <span className="office-chip">
          {t("office.chip.elapsed", "실행시간")} <b>{fmtElapsedRun(run)}</b>
        </span>
        <span className="office-chip">
          {t("office.chip.tokens", "총 토큰")} <b>{tokens.toLocaleString()}</b>
        </span>
        <span className="office-chip">
          {t("office.chip.progress", "진행률")} <b>{progress}%</b>
        </span>
        {demo && !runProp && (
          <span className="office-chip" style={{ borderColor: "#f59e0b", color: "#b45309" }}>
            {t("office.demoBadge", "DEMO")}
          </span>
        )}
      </div>

      {/* 승인 대기 펄스 배지 */}
      {run.status === "waiting_approval" && (
        <div className="office-waiting">
          <span className="office-waiting-dot" />
          <span>{t("office.waiting", "승인 대기")}</span>
          <b>
            {waitingSince === "idle"
              ? t("office.waitingIdle", "대기 중")
              : `${t("office.waitingFor", "대기")} ${fmtSecs((now - (waitingSince as number)) / 1000)}`}
          </b>
        </div>
      )}

      {/* 3D 보드 */}
      <div className="office-stage">
        <div className="office-perspective">
          <div className="office-plate">
            <GridSvg />
            <i className="office-block office-block-bl" />
            <i className="office-block office-block-br" />
            <FeedbackSvg />
            <EdgesSvg stages={stages} />
            {OFFICE_NODES.map((n) => {
              const info = nodeInfo(n.code, run, stages);
              const isSel = isOrchSel
                ? n.code === "ORCH"
                : n.code === sel!.code;
              const stCls = n.code === "ORCH" ? ` office-st-${info.statusText}` : "";
              // 실패 단계는 툴팁에 오류 요약
              let tip = t(`office.node.${n.code}`, n.label);
              if (n.key) {
                const fsr = stages.get(n.key);
                if (fsr && fsr.status === "failed" && fsr.error) {
                  tip += " — " + String(fsr.error).slice(0, 160);
                }
              }
              return (
                <div
                  key={n.code}
                  role="button"
                  tabIndex={0}
                  className={
                    `office-node office-node-${info.cls}` +
                    `${n.code === "ORCH" ? " office-node-orch" : ""}` +
                    `${isSel ? " office-node-selected" : ""}`
                  }
                  data-node={n.code}
                  title={tip}
                  style={{ left: `${n.x}%`, top: `${n.y}%`, "--nw": `${n.w}%` } as CSSProperties}
                  onClick={() => handleSelect(n.code === "ORCH" ? { type: "orch" } : { type: "stage", code: n.code })}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      handleSelect(n.code === "ORCH" ? { type: "orch" } : { type: "stage", code: n.code });
                    }
                  }}
                >
                  <i className="office-node-shadow" />
                  <i className="office-node-base" />
                  <div className="office-node-face">
                    <span className="office-node-ico">{OFFICE_ICONS[n.code]}</span>
                    <div className="office-node-body">
                      <div className="office-node-code">{n.code}</div>
                      <div className="office-node-label">{t(`office.node.${n.code}`, n.label)}</div>
                      <div className={`office-node-status${stCls}`}>{info.statusText}</div>
                    </div>
                  </div>
                  {info.retry > 1 && <span className="office-node-retry">×{info.retry}</span>}
                </div>
              );
            })}
          </div>
        </div>
      </div>

      {/* 상세 + 이벤트 로그 */}
      <div className="office-split">
        <div className="rounded-xl border border-border bg-surface p-4 shadow-sm">
          {!run ? (
            <div className="office-empty">{t("office.selectAgent", "선택 에이전트")}</div>
          ) : isOrchSel ? (
            <>
              <OrchDetail run={run} t={t} />
              {showDiff && (diffLoading
                ? <div className="text-xs text-muted" style={{ marginTop: 12 }}>diff 로딩…</div>
                : diff ? <DiffPreview diff={diff} t={t} /> : null)}
              {showApprove && (
                <div style={{ marginTop: 12, display: "flex", gap: 8 }}>
                  <button
                    type="button"
                    disabled={approveBusy}
                    onClick={() => handleApprove("approve")}
                    className="rounded-lg bg-accent px-3 py-1.5 text-sm font-medium text-white hover:bg-accent-hover disabled:opacity-50"
                  >
                    {t("office.approve", "승인")}
                  </button>
                  <button
                    type="button"
                    disabled={approveBusy}
                    onClick={() => handleApprove("reject")}
                    className="rounded-lg border border-border bg-surface px-3 py-1.5 text-sm font-medium text-ink-2 hover:bg-surface-hover disabled:opacity-50"
                  >
                    {t("office.reject", "거절")}
                  </button>
                </div>
              )}
            </>
          ) : selNode ? (
            <>
              <StageDetail node={selNode} sr={selStageRun} t={t} />
              {showDiff && diff && <DiffPreview diff={diff} t={t} />}
              {showApprove && selNode.code === "HUM" && (
                <div style={{ marginTop: 12, display: "flex", gap: 8 }}>
                  <button
                    type="button"
                    disabled={approveBusy}
                    onClick={() => handleApprove("approve")}
                    className="rounded-lg bg-accent px-3 py-1.5 text-sm font-medium text-white hover:bg-accent-hover disabled:opacity-50"
                  >
                    {t("office.approve", "승인")}
                  </button>
                  <button
                    type="button"
                    disabled={approveBusy}
                    onClick={() => handleApprove("reject")}
                    className="rounded-lg border border-border bg-surface px-3 py-1.5 text-sm font-medium text-ink-2 hover:bg-surface-hover disabled:opacity-50"
                  >
                    {t("office.reject", "거절")}
                  </button>
                </div>
              )}
            </>
          ) : (
            <div className="office-empty">{t("office.selectAgent", "선택 에이전트")}</div>
          )}
        </div>
        <EventLog audit={run.audit || []} t={t} />
      </div>
    </div>
  );
}
