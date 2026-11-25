import { describe, it, expect } from "vitest";
import { pickRun, stageTone, sessionArtifacts, scheduleInputText, STAGE_ORDER } from "./bits";
import type { RunListItem } from "./types";

describe("pickRun", () => {
  const mk = (id: string, status: string, wf = "dev_review", ts = "2024-01-01T00:00:00Z"): RunListItem =>
    ({ id, status, workflow: wf, created_at: ts });
  it("empty → null", () => {
    expect(pickRun([])).toBeNull();
    expect(pickRun([mk("a", "running", "other_wf")])).toBeNull();
  });
  it("prefers active over finished, newest first", () => {
    const runs = [
      mk("old-active", "running", "dev_review", "2024-01-01T00:00:00Z"),
      mk("new-finished", "succeeded", "dev_review", "2024-02-01T00:00:00Z"),
      mk("new-active", "waiting_approval", "dev_review", "2024-01-15T00:00:00Z"),
    ];
    expect(pickRun(runs)?.id).toBe("new-active");
  });
  it("no active → newest dev_review", () => {
    const runs = [mk("a", "succeeded", "dev_review", "2024-01-01T00:00:00Z"), mk("b", "failed", "dev_review", "2024-03-01T00:00:00Z")];
    expect(pickRun(runs)?.id).toBe("b");
  });
});

describe("stageTone", () => {
  it("maps known, defaults muted", () => {
    expect(stageTone("succeeded")).toBe("ok");
    expect(stageTone("failed")).toBe("err");
    expect(stageTone("waiting_approval")).toBe("warn");
    expect(stageTone("running")).toBe("accent");
    expect(stageTone("unknown")).toBe("muted");
    expect(stageTone(undefined)).toBe("muted");
  });
});

describe("sessionArtifacts", () => {
  it("parses string JSON and filters non-strings", () => {
    expect(sessionArtifacts({ artifacts: '["a.txt","b.png",1]' } as never)).toEqual(["a.txt", "b.png"]);
    expect(sessionArtifacts({ artifacts: ["x"] } as never)).toEqual(["x"]);
    expect(sessionArtifacts({ artifacts: "not-json" } as never)).toEqual([]);
    expect(sessionArtifacts(undefined)).toEqual([]);
  });
});

describe("scheduleInputText", () => {
  it("prefers task > summary > json", () => {
    expect(scheduleInputText({ task: "do it" } as never)).toBe("do it");
    expect(scheduleInputText({ summary: "sum" } as never)).toBe("sum");
    expect(scheduleInputText({ foo: 1 } as never)).toBe('{"foo":1}');
    expect(scheduleInputText("plain" as never)).toBe("plain");
    expect(scheduleInputText(null)).toBe("-");
    expect(scheduleInputText(undefined)).toBe("-");
  });
});

describe("STAGE_ORDER", () => {
  it("has dev_review stage keys", () => {
    expect(STAGE_ORDER.length).toBeGreaterThan(3);
    expect(STAGE_ORDER.every((s) => s.key && s.label)).toBe(true);
  });
});
