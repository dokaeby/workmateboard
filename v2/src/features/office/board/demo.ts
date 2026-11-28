/** 데모 run — 레거시 index.html officeDemoRunState() 이식.
 * dev_review run이 없어도 보드가 계속 돌아가게 가짜 실행을 합성한다.
 * 8초마다 단계가 하나씩 succeeded로 진행되고, approval까지 끝나면
 * completed로 잠시 보이다가 처음부터 반복한다.
 * 실제 run이 있으면 부모가 이 모듈을 호출하지 않는다. */
import { OFFICE_STAGES } from "./layout";
import type { BoardAuditEvent, BoardRun, BoardStageRun } from "./types";
import { tr } from "../../../shared/i18n";
import "./i18n";

export const DEMO_STAGE_SEC = 8;
export const DEMO_DONE_SEC = 10;

let demoRunStart = 0;

/** 데모 사이클을 처음부터 다시 시작(테스트/수동 리셋용). */
export function resetDemoRun() {
  demoRunStart = 0;
}

export interface DemoRunState {
  run: BoardRun;
  stageRuns: BoardStageRun[];
}

export function demoRunState(): DemoRunState {
  if (!demoRunStart) demoRunStart = Date.now();
  const keys = OFFICE_STAGES.map((s) => s.key);
  const cycle = keys.length * DEMO_STAGE_SEC + DEMO_DONE_SEC;
  const elapsed = (Date.now() - demoRunStart) / 1000;
  if (elapsed >= cycle) {
    demoRunStart = Date.now();
    return demoRunState();
  }
  const idx = Math.floor(elapsed / DEMO_STAGE_SEC);
  const done = elapsed >= keys.length * DEMO_STAGE_SEC;
  const iso = (ms: number) => new Date(ms).toISOString();
  const stageRuns: BoardStageRun[] = keys.map((key, i) => {
    const status = done || i < idx ? "succeeded" : i === idx ? "running" : "pending";
    const started = demoRunStart + i * DEMO_STAGE_SEC * 1000;
    return {
      stage_key: key,
      status,
      attempt: 1,
      model: "demo",
      tokens_in: status === "pending" ? 0 : 800 + i * 120,
      tokens_out: status === "pending" ? 0 : 300 + i * 60,
      started_at: status === "pending" ? null : iso(started),
      finished_at: status === "succeeded" ? iso(started + DEMO_STAGE_SEC * 1000) : null,
      input: { demo: true, stage: key },
      output: status === "pending" ? {} : { demo: tr("office.demo.stageDone", "{key} 완료 (가상)").replace("{key}", key) },
      error: "",
    };
  });
  const audit: BoardAuditEvent[] = stageRuns
    .filter((s) => s.status !== "pending")
    .map((s, i) => ({
      id: `demo-audit-${i}`,
      actor: "demo",
      action: "stage_" + s.status,
      object_ref: s.stage_key || "",
      detail: { demo: true },
      created_at: s.finished_at || s.started_at || undefined,
    }));
  audit.push({
    id: "demo-audit-start",
    actor: "demo",
    action: "run_started",
    object_ref: "",
    detail: { demo: true },
    created_at: iso(demoRunStart),
  });
  audit.sort((a, b) => String(a.created_at || "").localeCompare(String(b.created_at || "")));
  return {
    run: {
      id: "demo-run",
      workflow: "dev_review",
      version: 1,
      status: done ? "completed" : "running",
      created_at: iso(demoRunStart),
      updated_at: iso(Date.now()),
      input: { task: tr("office.demo.task", "(demo) dev_review 파이프라인 시연") },
      approvals: [],
      audit,
      error: "",
    },
    stageRuns,
  };
}
