/** agents 조직도 — /api/v1/agents 계약 타입 + reports_to 트리 빌드(순수 함수). */

export type AgentStatus = "active" | "paused" | "archived" | (string & {});

export interface Agent {
  id: string;
  name?: string;
  adapter_type?: string;
  role?: string;
  description?: string;
  capabilities?: string[];
  reports_to?: string | null;
  status?: AgentStatus;
  monthly_budget_usd?: number | null;
  created_at?: string;
  updated_at?: string;
  runs_30d?: number;
  cost_30d_usd?: number;
  last_run_at?: string | null;
}

export interface AgentsResponse {
  items?: Agent[];
}

export interface AgentNode {
  agent: Agent;
  children: AgentNode[];
  depth: number;
}

const MAX_DEPTH = 32;

function normStatus(a: Agent): string {
  return String(a.status || "active").toLowerCase();
}

/**
 * reports_to로 트리를 만든다.
 * 루트 = reports_to가 없거나, 가리키는 에이전트가 없거나(삭제/미등록),
 * reports_to 대상이 archived인 경우. 순환 참조는 visited 가드로 끊고
 * 도달 불가 노드는 루트로 승격한다.
 */
export function buildForest(agents: Agent[]): AgentNode[] {
  const byId = new Map<string, Agent>();
  for (const a of agents) {
    if (a && a.id) byId.set(a.id, a);
  }

  const kids = new Map<string, Agent[]>();
  const rootIds = new Set<string>();
  for (const a of agents) {
    if (!a || !a.id) continue;
    const pid = a.reports_to || "";
    const parent = pid ? byId.get(pid) : undefined;
    const parentArchived = !!parent && normStatus(parent) === "archived";
    if (parent && parent.id !== a.id && !parentArchived) {
      const arr = kids.get(pid) || [];
      arr.push(a);
      kids.set(pid, arr);
    } else {
      rootIds.add(a.id);
    }
  }

  const out: AgentNode[] = [];
  const seen = new Set<string>();

  const visit = (a: Agent, depth: number): AgentNode => {
    seen.add(a.id);
    const children: AgentNode[] = [];
    if (depth < MAX_DEPTH) {
      for (const c of kids.get(a.id) || []) {
        if (seen.has(c.id)) continue; // 순환 방지
        children.push(visit(c, depth + 1));
      }
    }
    return { agent: a, children, depth };
  };

  // 입력 순서 유지 — API 정렬에 의존하지 않고 결정적 출력.
  for (const a of agents) {
    if (a && a.id && rootIds.has(a.id) && !seen.has(a.id)) out.push(visit(a, 0));
  }
  // 순환 등으로 어떤 부모에도 붙지 못한 노드는 루트로 승격.
  for (const a of agents) {
    if (a && a.id && !seen.has(a.id)) out.push(visit(a, 0));
  }
  return out;
}
