import { describe, it, expect } from "vitest";
import { fmtSecs, fmtTsRaw, parseMs, fmtElapsedRun, fmtDuration, latestStageRuns } from "./format";
import type { BoardRun, BoardStageRun } from "./types";

describe("fmtSecs", () => {
  it("formats s/m/h", () => {
    expect(fmtSecs(45)).toBe("45s");
    expect(fmtSecs(200)).toBe("3m 20s");
    expect(fmtSecs(3900)).toBe("1h 5m");
    expect(fmtSecs(-3)).toBe("0s");
  });
});

describe("fmtTsRaw / parseMs", () => {
  it("ts raw", () => {
    expect(fmtTsRaw("2024-05-01T12:34:56.789Z")).toBe("2024-05-01 12:34:56");
    expect(fmtTsRaw(null)).toBe("-");
    expect(fmtTsRaw("")).toBe("-");
  });
  it("parseMs", () => {
    expect(parseMs(null)).toBeNaN();
    expect(parseMs("2024-01-01T00:00:00Z")).toBe(Date.parse("2024-01-01T00:00:00Z"));
  });
});

describe("fmtElapsedRun / fmtDuration", () => {
  const run: BoardRun = { id: "r1", status: "running", created_at: "2024-01-01T00:00:00Z", updated_at: "2024-01-01T00:01:30Z" };
  it("elapsed run", () => {
    expect(fmtElapsedRun(run)).toBe("1m 30s");
    expect(fmtElapsedRun({ id: "x", status: "queued" })).toBe("-");
  });
  it("stage duration", () => {
    const sr: BoardStageRun = { stage_key: "dev", status: "running", started_at: "2024-01-01T00:00:00Z" };
    const now = Date.parse("2024-01-01T00:00:45Z");
    expect(fmtDuration(sr, now)).toBe("45s");
    expect(fmtDuration(null, now)).toBe("-");
    expect(fmtDuration({ ...sr, finished_at: "2024-01-01T00:02:00Z" }, now)).toBe("2m ");
  });
});

describe("latestStageRuns", () => {
  it("keeps highest attempt per stage_key", () => {
    const runs: BoardStageRun[] = [
      { stage_key: "dev", attempt: 1, status: "failed" },
      { stage_key: "dev", attempt: 2, status: "succeeded" },
      { stage_key: "test", status: "running" },
      { stage: "qa", status: "queued" }, // stage_key 없음 → stage로 키
      { status: "orphan" },               // 키 없음 → 무시
    ];
    const m = latestStageRuns(runs);
    expect(m.get("dev")?.attempt).toBe(2);
    expect(m.get("dev")?.status).toBe("succeeded");
    expect(m.get("test")?.status).toBe("running");
    expect(m.get("qa")?.status).toBe("queued");
    expect(m.size).toBe(3);
  });
  it("empty/undefined", () => {
    expect(latestStageRuns(undefined).size).toBe(0);
    expect(latestStageRuns([]).size).toBe(0);
  });
});
