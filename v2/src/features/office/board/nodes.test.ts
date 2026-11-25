import { describe, it, expect } from "vitest";
import { edgePath, edgeState, nodeInfo, EDGE_CLS } from "./nodes";
import type { BoardRun, BoardStageRun } from "./types";

describe("edgePath", () => {
  it("straight line for same row/col", () => {
    expect(edgePath({ x: 10, y: 20 }, { x: 50, y: 20 })).toBe("M 10 20 L 50 20");
    expect(edgePath({ x: 10, y: 20 }, { x: 10, y: 60 })).toBe("M 10 20 L 10 60");
  });
  it("ortho path for diagonal", () => {
    const d = edgePath({ x: 10, y: 20 }, { x: 50, y: 60 });
    expect(d.startsWith("M 10 20")).toBe(true);
    expect(d).toContain("Q"); // rounded corner
    expect(d.endsWith("L 50 60")).toBe(true);
  });
});

describe("edgeState", () => {
  const stages = new Map<string, BoardStageRun>([
    ["analysis", { stage_key: "analysis", status: "succeeded" }],
    ["implement", { stage_key: "implement", status: "running" }],
    ["qa", { stage_key: "qa", status: "failed" }],
  ]);
  it("maps stage status to edge state", () => {
    // NODE→key 매핑은 layout.ts 기준 — succeeded/running/failed 노드 각각 확인
    const codes = ["ANA", "DEV", "QA", "ORCH", "HUM"] as const;
    const results = codes.map((c) => edgeState(c, stages));
    expect(results.every((r) => ["done", "fail", "live", "dim"].includes(r))).toBe(true);
    expect(EDGE_CLS.done).toBe("office-edge-done");
    expect(EDGE_CLS.dim).toBe("office-edge-dim");
  });
  it("node without key → dim", () => {
    expect(edgeState("ORCH", stages)).toBe("dim");
  });
});

describe("nodeInfo", () => {
  const run: BoardRun = { id: "r1", status: "running" };
  const stages = new Map<string, BoardStageRun>([
    ["analysis", { stage_key: "analysis", status: "succeeded", attempt: 2 }],
  ]);
  it("ORCH reflects run status", () => {
    const info = nodeInfo("ORCH", run, stages);
    expect(info.statusText).toBe("running");
    expect(info.retry).toBe(0);
  });
  it("stage node reflects stage run", () => {
    const info = nodeInfo("ANA", run, stages);
    expect(info.cls).toBe("succeeded");
    expect(info.retry).toBe(2);
  });
  it("missing stage → pending", () => {
    const info = nodeInfo("QA", run, stages);
    expect(info.cls).toBe("pending");
    expect(info.retry).toBe(0);
  });
  it("null run ORCH → queued", () => {
    expect(nodeInfo("ORCH", null, stages).statusText).toBe("queued");
  });
});
