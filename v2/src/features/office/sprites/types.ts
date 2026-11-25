/** 사무실 스프라이트 공용 타입 — 레거시 office-*.js 전역 상태를 TS로 옮긴 것.
 * 좌표는 전부 사무실 그림(floor.webp, 1536×1024) 픽셀 기준. */

export type Pt = [number, number];

/** 서버/브리지가 주는 사람 한 명의 입력(계약 = 레거시 Person, CONTRACT 4절) */
export interface SubInput {
  id: string | number;
  type?: string;
  desc?: string;
  doing?: unknown; // 문자열 또는 {k,a} — officeI18n.act()가 해석
}

export interface LogEntry {
  at?: string;
  k?: string;
  a?: string;
  [k: string]: unknown;
}

export interface PersonInput {
  id: string;
  name: string;
  status?: string; // working | decision | rest
  ask?: string;
  kid?: boolean;
  home?: string | null;
  commits?: number | null;
  path?: string;
  project?: string;
  health?: string;
  doing?: unknown;
  subCount?: number;
  subs?: SubInput[];
  log?: LogEntry[];
  /** 패널 확장 필드(모르면 무시) */
  machine?: string;
  tokens_in?: number | null;
  tokens_out?: number | null;
  est_cost_usd?: number | null;
  sessions?: number;
  pending?: number;
  source?: string;
  [k: string]: unknown;
}

/** 할 일 단계 — {go:[x,y], via} 걷기 | {pose, dur, ...} 자세 취하기 */
export interface ActStep {
  go?: Pt;
  via?: string;
  pose?: string;
  dur?: number;
  face?: number; // -1 왼쪽, 1 오른쪽
  say?: string | (() => string) | null;
  sayDur?: number;
  sayKind?: string;
  mix?: [string, number, number][]; // [pose, 확률, 지속초]
  hop?: boolean;
  work?: boolean;
  chat?: boolean;
  sleepy?: boolean;
  chair?: number;
  fixed?: boolean;
  meet?: boolean;
}

export interface Bubble {
  text: string;
  until: number;
  kind: string; // talk | think | work | ask
}

export interface Over {
  pose: string;
  until: number;
  stop?: boolean;
}

/** sprite()가 돌려주는 상자 — 눌림 판정·글씨 자리용(그림 좌표) */
export interface Box {
  x: number;
  y: number;
  w: number;
  h: number;
  top: number;
}

/** 걷기 막힘-풀기 진행 상태(office-path.js unstick) */
export interface Prog {
  to: Pt;
  best: number;
  t: number;
  side: number;
  stuck: number;
}

/** 걷는 주체 공통 필드 — 사람(Person)과 서브에이전트(Mini)가 공유 */
export interface Walker {
  id: string;
  x: number;
  y: number;
  path: Pt[];
  vx?: number;
  vy?: number;
  wp?: number; // 서브에이전트 걸음 위상
  dist?: number; // 걸은 거리(어른 걷기 그림 장 넘김용)
  prog?: Prog;
  alpha: number;
  gone?: boolean;
  act?: ActStep | null;
  steps?: ActStep[] | null;
  si?: number;
  kid?: boolean;
  face?: number;
  turnT?: number;
  over?: Over | null;
  bubble?: Bubble | null;
  box?: Box;
  feet?: number;
  owner?: string;   // Mini 전용 — 주인 Person.id
  state?: string;   // Mini 전용 — wait | in | bye | out
}

export interface Person extends Walker {
  name: string;
  species: string;
  color: string;
  phase: number;
  status: string;
  ask: string;
  doing: string;
  subCount: number;
  subs: SubInput[];
  commits?: number | null;
  path0?: string;
  project: string;
  health: string;
  home?: string | null;
  extra?: number;
  meeting?: boolean;
  chat?: Chat | null;
  seated?: boolean;
  leaving?: boolean;
  exit?: Pt | null;
  until?: number;
  arrivedAt?: number;
  mixAt?: number;
  nextSay?: number;
  nextPart?: number;
}

export interface Mini extends Walker {
  owner: string;
  phase: number;
  type?: string;
  desc?: string;
  doing: string;
  state: string; // wait | in | bye | out
  flipAt?: number;
  flipGap?: number;
  flipT?: number;
  hopAt?: number;
  nextSay?: number;
  byeAt?: number;
  byeSaid?: boolean;
  byeEnd?: number;
}

export interface ChatSpot {
  pts: Pt[];
  via?: string;
  vias?: string[];
  sit?: boolean;
}

export interface Chat {
  spot: ChatSpot;
  members: Person[];
  start: number;
  next: number;
  said: number;
  last: Person | null;
  born: number;
  lines: number;
}

/** 화면이 만든 사건 — 글은 그릴 때 지금 언어로 만든다(key = i18n 의 ev.*) */
export interface LifeEvent {
  at: number; // ms epoch
  id: string;
  name: string;
  color: string;
  key: string;
  arg: string;
}

export interface LifeState {
  t: number;
  people: Map<string, Person>;
  minis: Map<string, Mini>;
  desks: Map<string, number>;
  taken: Map<string, string>;
  chats: Set<Chat>;
  greeted: Map<string, number>;
  events: LifeEvent[];
  synced: boolean;
  meetNext: Map<string, number>;
}

export interface Spot {
  at: Pt;
  via?: string;
  vias?: string[];
  face?: number;
  sit?: boolean;
  chair?: number;
}

export interface Desk {
  seat: Pt;
  via: string;
  row?: number;
  chair?: number;
}

export interface HomeSpot {
  work: { at: Pt; via: string; face: number };
  rest: number;
  queue: number;
}

export interface MeetingSpot {
  at: Pt;
  via: string;
}
