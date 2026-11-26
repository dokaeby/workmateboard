import { describe, it, expect } from "vitest";
import { buildForest, type Agent } from "./tree";

const a = (id: string, reports_to: string | null = null, status = "active"): Agent => ({
  id,
  name: id,
  reports_to,
  status,
});

function ids(nodes: ReturnType<typeof buildForest>): string[] {
  return nodes.map((n) => n.agent.id);
}

describe("buildForest", () => {
  it("builds a simple hierarchy", () => {
    const forest = buildForest([
      a("ceo"),
      a("cto", "ceo"),
      a("eng", "cto"),
      a("design", "ceo"),
    ]);
    expect(ids(forest)).toEqual(["ceo"]);
    const ceo = forest[0];
    expect(ceo.children.map((c) => c.agent.id)).toEqual(["cto", "design"]);
    expect(ceo.children[0].children.map((c) => c.agent.id)).toEqual(["eng"]);
    expect(ceo.children[0].children[0].depth).toBe(2);
  });

  it("multiple roots keep input order", () => {
    const forest = buildForest([a("b"), a("a"), a("c", "missing")]);
    expect(ids(forest)).toEqual(["b", "a", "c"]);
  });

  it("reports_to pointing to a missing agent becomes a root", () => {
    const forest = buildForest([a("x", "ghost")]);
    expect(ids(forest)).toEqual(["x"]);
    expect(forest[0].children).toEqual([]);
  });

  it("reports_to an archived agent becomes a root", () => {
    const forest = buildForest([
      a("boss", null, "archived"),
      a("minion", "boss"),
    ]);
    // boss는 archived라도 노드로 남고, minion은 루트로 승격
    expect(ids(forest)).toContain("minion");
    const boss = forest.find((n) => n.agent.id === "boss");
    expect(boss?.children).toEqual([]);
  });

  it("self-reference is treated as root", () => {
    const forest = buildForest([a("me", "me")]);
    expect(ids(forest)).toEqual(["me"]);
  });

  it("breaks cycles without hanging", () => {
    const forest = buildForest([a("x", "y"), a("y", "x")]);
    const flat = forest.flatMap(function walk(n): string[] {
      return [n.agent.id, ...n.children.flatMap(walk)];
    });
    expect(flat.sort()).toEqual(["x", "y"]);
  });

  it("handles empty input", () => {
    expect(buildForest([])).toEqual([]);
  });
});
