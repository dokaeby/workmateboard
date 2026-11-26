/** goals 트리 — /api/v1/goals 계약 타입 + parent_id 트리 빌드(순수 함수).
 *  타입은 shared/api/types의 Goal(3차 status 어휘)을 재수출한다. */

import type { Goal, GoalStatus } from "../../shared/api/types";

export type { Goal, GoalStatus };
export type { GoalsResponse } from "../../shared/api/types";

export interface GoalNode {
  goal: Goal;
  children: GoalNode[];
  depth: number;
}

const MAX_DEPTH = 32;

function normStatus(g: Goal): string {
  return String(g.status || "active").toLowerCase();
}

/**
 * parent_id로 트리를 만든다.
 * 루트 = parent_id가 없거나, 가리키는 목표가 없거나(삭제/미등록),
 * 부모가 cancelled인 경우. 순환 참조는 visited 가드로 끊고
 * 도달 불가 노드는 루트로 승격한다. agents/tree.ts의 buildForest와 동일 규칙.
 */
export function buildForest(goals: Goal[]): GoalNode[] {
  const byId = new Map<string, Goal>();
  for (const g of goals) {
    if (g && g.id) byId.set(g.id, g);
  }

  const kids = new Map<string, Goal[]>();
  const rootIds = new Set<string>();
  for (const g of goals) {
    if (!g || !g.id) continue;
    const pid = g.parent_id || "";
    const parent = pid ? byId.get(pid) : undefined;
    const parentCancelled = !!parent && normStatus(parent) === "cancelled";
    if (parent && parent.id !== g.id && !parentCancelled) {
      const arr = kids.get(pid) || [];
      arr.push(g);
      kids.set(pid, arr);
    } else {
      rootIds.add(g.id);
    }
  }

  const out: GoalNode[] = [];
  const seen = new Set<string>();

  const visit = (g: Goal, depth: number): GoalNode => {
    seen.add(g.id);
    const children: GoalNode[] = [];
    if (depth < MAX_DEPTH) {
      for (const c of kids.get(g.id) || []) {
        if (seen.has(c.id)) continue; // 순환 방지
        children.push(visit(c, depth + 1));
      }
    }
    return { goal: g, children, depth };
  };

  // 입력 순서 유지 — API 정렬에 의존하지 않고 결정적 출력.
  for (const g of goals) {
    if (g && g.id && rootIds.has(g.id) && !seen.has(g.id)) out.push(visit(g, 0));
  }
  // 순환 등으로 어떤 부모에도 붙지 못한 노드는 루트로 승격.
  for (const g of goals) {
    if (g && g.id && !seen.has(g.id)) out.push(visit(g, 0));
  }
  return out;
}
