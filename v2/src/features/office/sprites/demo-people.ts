/** 데모 캐스트 — 레거시 index.html DEMO_CAST/DEMO_ACTS/_demoStep/jmDemoPeople 이식.
 * officeDemo 토글이 켜져 있으면 실제 세션과 독립적으로 가상 캐릭터 ~7명이
 * 10~20초 주기로 working/decision/rest를 전이하고 활동 로그가 자란다.
 * 씨앗 고정 난수라 같은 시각에 열면 같은 장면이 나온다. */
import type { LogEntry, PersonInput, SubInput } from "./types";
import { guessProject } from "./bridge";

// 가상 캐스트 — 이름은 프로젝트명, commits<50 이면 kid(어린이 스프라이트)
const DEMO_CAST: { name: string; commits: number | null; home?: string; sessions?: number }[] = [
  { name: "my-app", commits: 812, sessions: 2 },
  { name: "api-server", commits: 340, home: "server" },
  { name: "docs-site", commits: 23 },
  { name: "ml-pipeline", commits: 156 },
  { name: "mobile-client", commits: 1290 },
  { name: "infra-scripts", commits: 41 },
  { name: "playground", commits: null },
];
const DEMO_ACTS = (
  [
    ["read", "server.mjs"], ["read", "README.md"], ["edit", "App.tsx"], ["edit", "routes.ts"],
    ["write", "notes.md"], ["bash", "Run unit tests"], ["bash", "Install dependencies"],
    ["grep", "TODO"], ["glob", "src/**/*.ts"], ["fetch", "developer.mozilla.org"],
    ["search", "node fs watch"], ["agent", "find auth code"], ["todo", ""], ["skill", "pdf"],
    ["mcp", "query"], ["tool", "NotebookRead"], ["text", "Tests pass now. Next I will clean up the router."],
  ] as [string, string][]
).map(([k, a]) => ({ k, a }));
const DEMO_ASKS = [
  "Claude needs your permission to use Bash",
  "Bash: npm publish --dry-run",
  "Claude needs your permission to use Write",
  "Claude needs your input",
];
const DEMO_SUB_TYPES = ["Explore", "general-purpose", "Plan"];
const DEMO_SUB_DESCS = ["find auth code", "read the test suite", "check API usage", "map the folder layout"];
const DEMO_LOG_N = 8;

// 씨앗 고정 난수 — 같은 시각에 열면 같은 장면
function demoRng(seed: number) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
const demoRand = demoRng(7);
const demoPick = <T,>(arr: T[]): T => arr[Math.floor(demoRand() * arr.length)];
const demoAct = () => demoPick(DEMO_ACTS);

interface DemoAct { k: string; a: string }

interface DemoPerson {
  name: string;
  commits: number | null;
  home?: string;
  status: string;
  ask: string;
  next: number;
  nextLog: number;
  doing: DemoAct | null;
  log: (LogEntry & DemoAct)[];
  subs: SubInput[];
}

function demoSubs(): SubInput[] {
  const n = 1 + Math.floor(demoRand() * 3);
  return Array.from({ length: n }, () => ({
    id: Math.floor(demoRand() * 0xffffff).toString(16).padStart(6, "0"),
    type: demoPick(DEMO_SUB_TYPES),
    desc: demoPick(DEMO_SUB_DESCS),
    doing: demoAct(),
  }));
}

const demoPeopleState: DemoPerson[] = DEMO_CAST.map((c, i) => ({
  name: c.name,
  commits: c.commits,
  home: c.home,
  status: i === 1 ? "decision" : i === 3 || i === 6 ? "rest" : "working",
  ask: i === 1 ? DEMO_ASKS[0] : "",
  next: Date.now() + 10000 + demoRand() * 10000,
  nextLog: Date.now() + 1000 + demoRand() * 3000,
  doing: demoAct(),
  log: [],
  subs: [],
}));
for (const p of demoPeopleState) {
  for (let j = 0; j < 4; j++) {
    p.log.push({ at: new Date(Date.now() - (4 - j) * 7000).toISOString(), ...demoAct() });
  }
  if (p.status === "working" && demoRand() < 0.6) p.subs = demoSubs();
}

function demoStep(p: DemoPerson, now: number) {
  while (now >= p.next) {
    const r = demoRand();
    p.status = r < 0.55 ? "working" : r < 0.75 ? "decision" : "rest";
    p.ask = p.status === "decision" ? demoPick(DEMO_ASKS) : "";
    p.subs = p.status === "working" && demoRand() < 0.5 ? demoSubs() : [];
    p.next += 10000 + demoRand() * 10000;
  }
  while (now >= p.nextLog) {
    if (p.status === "working") {
      p.log.push({ at: new Date(p.nextLog).toISOString(), ...demoAct() });
      if (p.log.length > DEMO_LOG_N) p.log.shift();
      for (const s of p.subs) if (demoRand() < 0.5) s.doing = demoAct();
    }
    p.nextLog += 2000 + demoRand() * 3000;
  }
}

/** 레거시 jmDemoPeople() — 호출마다 내부 상태를 시간 기준으로 전이해 people 계약을 돌려준다.
 * _tick 은 useMemo 재계산 트리거 전용(내부 시각은 항상 Date.now()). */
export function demoPeople(_tick = 0): PersonInput[] {
  const now = Date.now();
  return demoPeopleState.map((p) => {
    demoStep(p, now);
    const last = p.log[p.log.length - 1];
    const person: PersonInput = {
      id: `/demo/${p.name}`,
      name: p.name,
      status: p.status,
      ask: p.ask,
      doing: last ? { k: last.k, a: last.a } : null,
      subs: p.subs,
      subCount: p.subs.length,
      kid: p.commits !== null && p.commits < 50,
      home: p.home || null,
      commits: p.commits,
      log: p.log.map((e) => ({ ...e })),
    };
    person.project = guessProject(person);
    return person;
  });
}
