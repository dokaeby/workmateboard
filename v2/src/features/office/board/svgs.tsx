/** Agent Office 보드 SVG 레이어 — 원근 그리드 / 피드백 버스 / 간선.
 * StageBoard.tsx에서 verbatim 이동. 간선 경로·상태 로직은 ./nodes, 좌표 상수는 ./layout. */
import { useId } from "react";
import { OFFICE_NODES, OFFICE_EDGES } from "./layout";
import { edgePath, edgeState, EDGE_CLS } from "./nodes";
import type { BoardStageRun } from "./types";

/** 원근 플로어 그리드 — 소실점(51%,5%)에서 방사선 + 아래로 벌어지는 수평선. */
export function GridSvg() {
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
export function FeedbackSvg() {
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

export function EdgesSvg({ stages }: { stages: Map<string, BoardStageRun> }) {
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
