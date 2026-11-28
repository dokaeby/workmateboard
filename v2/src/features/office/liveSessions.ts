/** 라이브 세션 보드 — 레거시 index.html officeLiveSessionsState() 이식.
 * 데모 OFF + dev_review run 없음 + 활성 세션 있음 → 실제 agent-sessions를
 * dev_review 파이프라인 모양의 가상 run으로 합성한다.
 * 활동 세션이 있으면 단계가 DEMO_STAGE_SEC 주기로 순환하고, 전부 rest면
 * 완료 상태로 고정된다. 순수 함수 — 시계는 호출부의 리렌더 주기가 담당한다. */
import type { AgentSession } from "../../shared/api/types";
import { OFFICE_STAGES } from "./board/layout";
import { DEMO_DONE_SEC, DEMO_STAGE_SEC } from "./board/demo";
import type { BoardAuditEvent, BoardRun, BoardStageRun } from "./board/types";
import { SESSION_TOOL_LABEL } from "./sprites/bridge";
import { tr } from "../../shared/i18n";
import "./i18n";

/** /agent-sessions 응답 — 레거시 live 보드가 읽는 필드만 (health 포함). */
export interface LiveSession extends AgentSession {
  health?: string;
}

export interface LiveSessionsState {
  run: BoardRun;
  stageRuns: BoardStageRun[];
}

// 레거시 _liveRunStart — 단계 순환 시작 시각(모듈 수명 동안 유지)
let liveRunStart = 0;

export function liveSessionsState(list: LiveSession[]): LiveSessionsState | null {
  const sessions = (Array.isArray(list) ? list : []).filter((s) => s && s.status !== "ended");
  if (!sessions.length) return null;
  // stale working(브릿지 다운 의심)은 활동으로 치지 않는다 — decision은 유지
  const active = sessions.filter(
    (s) => s.status === "decision" || (s.status === "working" && s.health !== "stale"),
  );
  const now = Date.now();
  const iso = (ms: number) => new Date(ms).toISOString();
  const label = (s: LiveSession) =>
    `${SESSION_TOOL_LABEL[s.tool || ""] || s.tool} · ${s.title || s.project || s.session_key}`;
  const title = active.length
    ? active.map(label).slice(0, 3).join(", ") +
      (active.length > 3 ? tr("office.live.more", " 외 {n}").replace("{n}", String(active.length - 3)) : "")
    : sessions.map(label).slice(0, 3).join(", ");
  const keys = OFFICE_STAGES.map((s) => s.key);
  let idx = keys.length; // 기본: 전부 succeeded (rest만 있을 때)
  if (active.length) {
    if (!liveRunStart) liveRunStart = now;
    const cycle = keys.length * DEMO_STAGE_SEC + DEMO_DONE_SEC;
    const elapsed = (now - liveRunStart) / 1000;
    if (elapsed >= cycle) {
      // 레거시는 여기서 재귀 호출 — 리셋 직후 elapsed≈0 이므로 idx=0과 동일
      liveRunStart = now;
      idx = 0;
    } else {
      idx = Math.min(keys.length, Math.floor(elapsed / DEMO_STAGE_SEC));
    }
  }
  const stageRuns: BoardStageRun[] = keys.map((key, i) => {
    const status = i < idx || !active.length ? "succeeded" : i === idx ? "running" : "pending";
    return {
      stage_key: key,
      status,
      attempt: 1,
      model: "live",
      tokens_in: status === "pending" ? 0 : 400 + i * 80,
      tokens_out: status === "pending" ? 0 : 150 + i * 40,
      started_at: status === "pending" ? null : iso(now - (keys.length - i) * 30000),
      finished_at: status === "succeeded" ? iso(now - (keys.length - i - 1) * 30000) : null,
      input: { live: true, stage: key },
      output: status === "pending" ? {} : { live: `${key} — ${tr("office.live.observed", "세션 {n}개 관측").replace("{n}", String(sessions.length))}` },
      error: "",
    };
  });
  const audit: BoardAuditEvent[] = [
    {
      id: "live-audit",
      actor: "agent-bridge",
      action: "sessions_observed",
      object_ref: "",
      detail: { live: true, active: active.length, total: sessions.length },
      created_at: iso(now),
    },
  ];
  const done = idx >= keys.length;
  return {
    run: {
      id: "live-sessions",
      workflow: "dev_review",
      version: 1,
      status: active.length && !done ? "running" : "completed",
      created_at: iso(now - 3600000),
      updated_at: iso(now),
      input: {
        task: tr("office.live.task", "(live) 에이전트 세션 {n}개 — {who}")
          .replace("{n}", String(sessions.length))
          .replace("{who}", title),
      },
      approvals: [],
      audit,
      error: "",
    },
    stageRuns,
  };
}
