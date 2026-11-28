/** jobMate 브리지 — 레거시 jobmate-bridge.js + office-view.js 의 provider/toLife/guessProject 이식.
 * GET /agent-sessions 응답을 사무실 사람(PersonInput) 목록으로 변환한다.
 * office-view.js 의 load() 가 3초마다 부르던 provider 로직 — v2 에서는 React Query 가
 * /agent-sessions?limit=50 을 폴링하고 이 모듈이 people 계약으로 변환한다.
 * doing/ask 는 평문 문자열 — office-i18n 의 act()/ask() 가 문자열을 그대로 통과시킨다. */

import type { AgentSession } from "../../../shared/api/types";
import type { PersonInput, SubInput } from "./types";
import { t as spriteT } from "./office-i18n";

/** 레거시 SESSION_TOOL_LABEL — index.html 인라인 상수와 동일 */
export const SESSION_TOOL_LABEL: Record<string, string> = {
  claude: "Claude", codex: "Codex", gemini: "Gemini", devin: "Devin",
};

/** /agent-sessions 응답에서 세션 배열 꺼내기 — 레거시 _sessionListFrom 과 동일 */
export function sessionListFrom(d: unknown): AgentSession[] {
  if (Array.isArray(d)) return d as AgentSession[];
  const o = d as Record<string, unknown> | null;
  const list = (o?.sessions || o?.agent_sessions) as unknown;
  return Array.isArray(list) ? (list as AgentSession[]) : [];
}

/** 화면의 상태 = 승인 대기 > 일(본체가 쉬어도 서브에이전트가 일하면 일하는 중) > 쉼.
 * office-panel.js modeOf / office-life.js modeOf 와 같은 잣대(입력 데이터 기준). */
export function modeOf(d: PersonInput): "decision" | "working" | "rest" {
  return d.status === "decision" ? "decision"
    : d.status === "working" || (d.subCount || 0) > 0 ? "working" : "rest";
}

/** project 보강 — provider 가 project 를 안 주면 이름·id 에서 추정한다(회의 모임 grouping 용).
 * 세션 provider 이름 형식은 "도구 · 프로젝트". 데모 캐스트는 이름으로 몇 팀으로 나눠 준다. */
export function guessProject(d: PersonInput): string {
  if (d.project) return d.project;
  const m = String(d.name || "").match(/·\s*(.+)$/);
  if (m) return m[1].trim();
  if (String(d.id || "").startsWith("/demo/")) {
    let h = 0;
    for (const c of String(d.name)) h = (h * 31 + c.charCodeAt(0)) >>> 0;
    return "demo-" + (h % 3);
  }
  return d.path || "";
}

/** 실제 에이전트 세션 → people — 레거시 jmLiveSessionPeople + office-view.js norm/guessProject 이식.
 * health=stale(5분+ 이벤트 없음)은 브릿지 다운 가능성 — working을 rest로 강등.
 * decision은 사용자 응답 대기 상태라 유지한다.
 * includeEnded=true(종료 탭)이면 ended 세션도 rest로 매핑 — 레거시 jmSessionProvider의
 * ended 분기(doing="종료: 요약")와 동일. */
export function sessionsToPeople(list: AgentSession[], opts?: { includeEnded?: boolean }): PersonInput[] {
  const includeEnded = !!opts?.includeEnded;
  const people = (Array.isArray(list) ? list : [])
    .filter((s) => s && (includeEnded || s.status !== "ended"))
    .map((s) => {
      const ext = s as AgentSession & {
        health?: string; machine?: string; tokens_in?: number; tokens_out?: number;
        est_cost_usd?: number; subs?: SubInput[]; kid?: boolean; home?: string | null;
        commits?: number | null; log?: PersonInput["log"]; ended_summary?: string;
        artifacts?: string | unknown[];
      };
      const ended = s.status === "ended";
      const id = `${s.tool}:${s.session_key}`;
      const name = `${(s.tool ? SESSION_TOOL_LABEL[s.tool] || s.tool : "agent")}` +
        ` · ${s.title || s.project || String(s.session_key || "").split(":").pop()!.slice(0, 12)}`;
      let doing = s.doing || "";
      let ask = s.ask || "";
      if (ended) {
        let summ = ext.ended_summary || "";
        if (!summ && ext.artifacts) {
          let a: unknown = ext.artifacts;
          if (typeof a === "string") { try { a = JSON.parse(a); } catch { a = []; } }
          if (Array.isArray(a)) summ = a.filter((x): x is string => typeof x === "string").join(", ");
        }
        if (!summ) summ = s.title || "";
        doing = summ ? spriteT("ended.prefix", { a: summ }) : spriteT("ended.done");
        ask = "";
      }
      const p: PersonInput = {
        id,
        name,
        status: ended ? "rest"
              : s.status === "working" ? (ext.health === "stale" ? "rest" : "working")
              : s.status === "decision" ? "decision" : "rest",
        ask,
        doing,
        subs: Array.isArray(ext.subs) ? ext.subs : [],
        subCount: ext.subs?.length || 0,
        kid: !!ext.kid, home: ext.home ?? null, commits: ext.commits ?? null,
        project: s.project || "", log: ext.log || [],
        // 확장 필드 — office-panel 이 모르는 필드는 무시됨(계약상 안전)
        machine: ext.machine || "",
        health: ext.health || "active",
        tokens_in: ext.tokens_in ?? null,
        tokens_out: ext.tokens_out ?? null,
        est_cost_usd: ext.est_cost_usd ?? null,
        session_key: s.session_key,
      };
      p.project = p.project || guessProject(p);
      return p;
    });
  // 동명 세션 구분 — 같은 이름(프로젝트/제목)이면 짧은 세션 키를 붙인다 (레거시 jmSessionProvider)
  const nameCount: Record<string, number> = {};
  for (const p of people) nameCount[p.name] = (nameCount[p.name] || 0) + 1;
  for (const p of people) {
    if (nameCount[p.name] > 1 && p.session_key) {
      p.name += ` · ${String(p.session_key).split(":").pop()!.slice(0, 12)}`;
    }
  }
  return people;
}
