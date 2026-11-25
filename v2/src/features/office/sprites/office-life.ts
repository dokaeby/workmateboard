/** 사무실 사람들의 하루 — 레거시 office-life.js 이식.
 * 심즈처럼 — 쉬는 사람은 할 일 목록(커피·소파·카페 탁자·통화·기지개·서버 점검·회의·산책·수다)에서
 * 무게를 두고 고르고, 일하는 사람은 자기 책상에서 타닥타닥하다 가끔 만세·머리 긁기를 한다.
 * 지나가다 마주치면 인사. 걷기·충돌은 office-path.ts, 지도는 office-world.ts, 대사는 office-i18n.ts.
 *
 * 주의: 레거시 office-view.js 의 frame()에서 OfficeLife.update(dt) 호출이 실수로 빠져 있었다
 * (커밋 4a73de3 — 주석은 "필요해 그대로 둔다"고 적혀 있음). 여기서는 update()를 정상 호출한다. */

import * as I18N from "./office-i18n";
import {
  OFFICE_CHATS, OFFICE_COLORS, OFFICE_DESKS, OFFICE_HOMES, OFFICE_MAP_NODES, OFFICE_MEETING,
  OFFICE_OVERFLOW, OFFICE_SPOTS,
} from "./office-world";
import { blocked, dist, nearest, onFloor, officeMotion, route } from "./office-path";
import type {
  ActStep, Chat, Desk, LifeEvent, LifeState, Mini, Person, PersonInput, Pt, Spot, SubInput,
} from "./types";

const SPEED = 36, MINI_SPEED = 78;        // 걷는 속도(그림 픽셀/초) — 어른은 고른 걸음, 서브에이전트는 종종걸음
export const SIT_DESK = 18, SIT_SOFA = 16; // 의자 등받이·방석 점 → 앉은 그림의 발끝까지

const rnd = (a: number, b: number) => a + Math.random() * (b - a);
const pick = <T,>(arr: T[]): T => arr[Math.floor(Math.random() * arr.length)];
const line = (name: string) => pick(I18N.lines(name));            // 지금 언어의 대사 하나

function hash(s: string): number {                       // FNV-1a — 이름이 같으면 색·종이 늘 같다
  let h = 2166136261;
  for (const ch of s) h = Math.imul(h ^ ch.codePointAt(0)!, 16777619);
  return h >>> 0;
}

export const colorOf = (name: string): string => OFFICE_COLORS[hash(name) % OFFICE_COLORS.length];
export const speciesOf = (name: string): string => (hash("종:" + name) % 2 ? "capy" : "frog");
const cut = I18N.cut;
const shuffle = <T,>(arr: T[]): T[] => arr.map((v) => [Math.random(), v] as [number, T]).sort((a, b) => a[0] - b[0]).map((v) => v[1]);

// ---------- 알림 소리 — 승인·질문이 새로 생기면 짧은 차임(WebAudio 진동자, 파일 없음) ----------
const MUTE_KEY = "jobmate.officeMute";
let muted = (() => { try { return localStorage.getItem(MUTE_KEY) === "1"; } catch { return false; } })();
let actx: AudioContext | null = null;
function audio(): AudioContext | null {
  if (!actx) {
    try {
      const AC = window.AudioContext || (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
      actx = AC ? new AC() : null;
    } catch { actx = null; }
  }
  if (actx && actx.state === "suspended") actx.resume().catch(() => {});
  return actx;
}
function chime() {
  if (muted) return;
  const ctx = audio();
  if (!ctx) return;
  const t0 = ctx.currentTime + 0.01;
  for (const [f, d, v] of [[880, 0, 0.12], [1318.5, 0.1, 0.1]] as [number, number, number][]) {
    const o = ctx.createOscillator(), g = ctx.createGain();
    o.type = "sine"; o.frequency.value = f;
    g.gain.setValueAtTime(0.0001, t0 + d);
    g.gain.exponentialRampToValueAtTime(v, t0 + d + 0.02);
    g.gain.exponentialRampToValueAtTime(0.0001, t0 + d + 0.3);
    o.connect(g); g.connect(ctx.destination);
    o.start(t0 + d); o.stop(t0 + d + 0.32);
  }
}
// 첫 클릭 뒤 AudioContext 재개 — 브라우저 자동재생 정책 대비
export function wakeAudio() { audio(); }
export const soundMuted = () => muted;
export function setSoundMuted(v: boolean) {
  muted = !!v;
  try { localStorage.setItem(MUTE_KEY, muted ? "1" : "0"); } catch { /* 저장 못 해도 지금은 적용된다 */ }
}

/** 하루 시뮬레이션 — 보드 인스턴스마다 하나씩 만든다(레거시는 전역 싱글턴) */
export function createLife() {
  const L: LifeState = {
    t: 0, people: new Map(), minis: new Map(), desks: new Map(), taken: new Map(),
    chats: new Set(), greeted: new Map(), events: [], synced: false, meetNext: new Map(),
  };
  const { step, separate } = officeMotion(L, SIT_DESK);

  // ---------- 말풍선 ----------
  function say(p: Person | Mini, text: string, dur = 2.8, kind = "talk") {
    if (text) p.bubble = { text: cut(text, 48), until: L.t + dur, kind };
  }

  // ---------- 할 일 = 단계 목록 ----------
  function run(p: Person, steps: ActStep[]) {
    p.steps = steps; p.si = -1; advance(p);
  }
  function advance(p: Person) {
    p.si = (p.si ?? -1) + 1;
    const s = p.steps?.[p.si];
    if (!s) { p.steps = null; p.act = null; return; }
    if (s.go) {
      // 의자에서 일어나면 앉으러 들어온 통로 점으로 먼저 물러난다 — 바로 다음 곳으로 가면
      // 등받이 뒤·책상 위를 가로질러 걷는다
      const from: Pt = [p.x, p.y], exit = p.exit;
      p.path = exit && dist(from, exit) < 160 && !blocked(from, exit)
        ? [exit, ...route(exit, s.go, s.via)]
        : route(from, s.go, s.via);
      p.exit = null; p.act = null; return;
    }
    p.path = [];
    p.act = s;
    const prevVia = p.steps?.[p.si - 1]?.via;
    p.exit = (s.chair ?? -1) >= 0 && prevVia ? OFFICE_MAP_NODES[prevVia] ?? null : null;
    p.until = L.t + (s.dur ?? 3);
    p.arrivedAt = L.t;
    p.mixAt = L.t + rnd(2, 5);
    if (s.face) p.face = s.face;
    if (s.chat && !p.chat) p.until = L.t + 0.5;      // 걸어오는 사이 수다가 끝났다 — 서 있지 말고 다음 일로
    if (s.say) say(p, typeof s.say === "function" ? s.say() : s.say, s.sayDur || 2.8, s.sayKind || "talk");
  }
  // 첫 화면 — 걷지 않고 그 자리에 바로 놓는다
  function snap(p: Person) {
    while (p.steps && p.path.length) {
      const to = p.path[p.path.length - 1];
      p.x = to[0]; p.y = to[1]; p.path = [];
      advance(p);
    }
  }

  // ---------- 자리 ----------
  function release(p: Person) {
    for (const [k, who] of L.taken) if (who === p.id) L.taken.delete(k);
    if (modeOf(p) === "rest") L.desks.delete(p.id);        // 쉬는 사람은 책상을 붙잡지 않는다
  }
  // 그 자리에 누가 앉아 있거나 지금 그리로 가는 중인가 — 같은 의자에 둘이 겹쳐 앉지 않게
  const goalOf = (q: Person | Mini): Pt => (q.path.length ? q.path[q.path.length - 1] : [q.x, q.y]);
  // 예약(L.taken)까지 본다 — 수다 자리와 카페 의자는 좌표가 같아서 예약을 안 보면 둘이 한 의자에 앉는다
  const spotAt = (k: string): Pt | undefined => {
    const [kind, i] = k.split(":");
    return OFFICE_SPOTS[kind]?.[+i]?.at;
  };
  const occupied = (pt: Pt, me?: Person | null): boolean =>
    [...L.people.values()].some((q) => q !== me && !q.leaving && dist(goalOf(q), pt) < 34)
    || [...L.taken].some(([k, who]) => who !== me?.id && spotAt(k) && dist(spotAt(k)!, pt) < 34)
    // 회의실 의자를 일자리로 배정받은 사람(승인 대기로 잠깐 일어난 사이에도 그 의자는 그 사람 것)
    || [...L.desks].some(([n, k]) => k < 0 && n !== me?.id && dist(sitAt(OFFICE_OVERFLOW[-1 - k].seat, SIT_DESK), pt) < 34);

  function takeSpot(p: Person, kind: string): Spot | null {
    const list = OFFICE_SPOTS[kind] || [];
    const free = list.map((_, i) => i).filter((i) => !L.taken.has(`${kind}:${i}`) && !occupied(list[i].at, p));
    if (!free.length) return null;
    const i = pick(free);
    L.taken.set(`${kind}:${i}`, p.id);
    return list[i];
  }
  // 책상 고르기 — 순서대로 채우지 않고 흩어 앉힌다. 서브에이전트가 줄 서는 쪽(오른쪽 옆자리)은 비워 둔다.
  const rightOf = (i: number) =>
    (i >= 0 && i + 1 < OFFICE_DESKS.length && OFFICE_DESKS[i + 1].row === OFFICE_DESKS[i].row ? i + 1 : -1);
  const leftOf = (i: number) =>
    (i > 0 && OFFICE_DESKS[i - 1].row === OFFICE_DESKS[i].row ? i - 1 : -1);

  function deskOk(i: number, p: Person): boolean {
    const owner = new Map([...L.desks].map(([n, k]) => [k, n]));
    if (owner.has(i) && owner.get(i) !== p.id) return false;
    const left = owner.get(leftOf(i));                       // 왼쪽 사람의 서브에이전트 줄 자리면 안 된다
    if (left && left !== p.id && (L.people.get(left)?.subCount ?? 0) > 0) return false;
    if (p.subCount > 0) {                                     // 내 서브에이전트 줄 자리(오른쪽)가 비어 있어야
      const right = owner.get(rightOf(i));
      if (right && right !== p.id) return false;
    }
    return true;
  }

  function deskOf(p: Person): Desk | null {
    const cur = L.desks.get(p.id);
    const lost = cur !== undefined && cur < 0 && occupied(sitAt(OFFICE_OVERFLOW[-1 - cur].seat, SIT_DESK), p);
    if (cur === undefined || (cur >= 0 && !deskOk(cur, p)) || lost) {
      const taken = [...L.desks].filter(([n]) => n !== p.id).map(([, k]) => k);
      const seats = taken.filter((k) => k >= 0).map((k) => OFFICE_DESKS[k].seat);
      const cands = OFFICE_DESKS.map((_, i) => i)
        .filter((i) => !taken.includes(i) && deskOk(i, p))
        .map((i) => ({
          i,
          far: Math.min(260, ...seats.map((q) => dist(q, OFFICE_DESKS[i].seat)))
               - 0.35 * dist([p.x, p.y], OFFICE_DESKS[i].seat),   // 남과 떨어지되 너무 멀리 걷지 않게
        }))
        .sort((a, b) => b.far - a.far);
      let i = cands.length ? pick(cands.slice(0, 3)).i : -1;  // 그중 좋은 자리 셋 중 하나
      if (i < 0) {                                              // 책상이 다 찼다 — 비어 있는 회의실 의자로
        const free = OFFICE_OVERFLOW.map((_, k) => k)
          .filter((k) => !taken.includes(-1 - k) && !occupied(sitAt(OFFICE_OVERFLOW[k].seat, SIT_DESK), p));
        if (!free.length) { L.desks.delete(p.id); return null; }   // 회의실도 다 찼다 — 한 의자에 둘이 앉지 않는다
        i = -1 - free[0];
      }
      L.desks.set(p.id, i);
    }
    const i = L.desks.get(p.id)!;
    return i >= 0 ? OFFICE_DESKS[i] : OFFICE_OVERFLOW[-1 - i];
  }

  // 지금 해야 할 몫 — 승인 대기 > 일(본체가 쉬어도 서브에이전트가 일하면 본체도 책상에서 일하는 중) > 쉼
  const busy = (p: Person) => p.status === "working" || (p.status !== "decision" && p.subCount > 0);
  const modeOf = (p: Person) => (p.status === "decision" ? "ask" : busy(p) ? "work" : "rest");
  const sitAt = (pt: Pt, dy: number): Pt => [pt[0], pt[1] + dy];
  const chairOf = (d: Desk) => d.chair ?? OFFICE_DESKS.indexOf(d);

  // ---------- 무엇을 할까 ----------
  const hasOwn = (o: object, k: string) => Object.prototype.hasOwnProperty.call(o, k);
  const homeOf = (p: Person) => (p.home && hasOwn(OFFICE_HOMES, p.home) ? OFFICE_HOMES[p.home] : null);

  function choose(p: Person) {
    leaveChat(p);
    release(p);
    p.meeting = false;                    // 회의 모임 중 다른 일을 고르면 모임은 끝 — 자리를 지킨 채 다시 일한다
    if (p.leaving) return;
    const home = homeOf(p);
    if (home && modeOf(p) === "work") {        // 서버실 랙 앞에 서서 일한다 — 가끔 생각·통화·만세
      const w = home.work;
      return run(p, [{ go: w.at, via: w.via },
                     { pose: "standBack", dur: Infinity, face: w.face, work: true, fixed: true,
                       mix: [["think", 0.12, 1.6], ["phone", 0.08, 2.6], ["cheer", 0.05, 1.3]] }]);
    }
    if (home && modeOf(p) === "rest" && Math.random() < home.rest) {
      const w = home.work;
      return run(p, [{ go: w.at, via: w.via },
                     { pose: pick(["standBack", "think", "phone", "phScroll"]), dur: rnd(14, 28), face: w.face, fixed: true,
                       say: Math.random() < 0.4 ? () => line("rack") : null,
                       mix: [["think", 0.15, 1.6]] }]);
    }
    if (modeOf(p) === "work") {
      const d = deskOf(p);
      if (!d) {                                  // 앉을 데가 없다 — 빈 산책 자리에서 서서 일한다(등을 보이고)
        const s = takeSpot(p, "stroll");
        return run(p, [...(s ? [{ go: s.at, via: s.via } as ActStep] : []),
                       { pose: "standBack", dur: Infinity, work: true, mix: [["think", 0.12, 1.6], ["cheer", 0.05, 1.3]] }]);
      }
      return run(p, [{ go: sitAt(d.seat, SIT_DESK), via: d.via },
                     { pose: "sitBack", dur: Infinity, work: true, chair: chairOf(d),
                       mix: [["cheer", 0.06, 1.3], ["scratch", 0.09, 1.6]] }]);
    }
    if (p.status === "decision" && home) {       // 서버실 사람은 승인 대기도 서버실에서 폴짝
      return run(p, [{ go: home.work.at, via: home.work.via },
                     { pose: "stand", dur: Infinity, hop: true, mix: [["wave", 0.25, 1.1]] }]);
    }
    if (p.status === "decision") {               // 의자에서 일어나 책상 앞 통로에서 폴짝 — 머리 글씨가 안 묻히게
      const d = deskOf(p);
      if (!d) return run(p, [{ pose: "stand", dur: Infinity, hop: true, mix: [["wave", 0.25, 1.1]] }]);
      return run(p, [{ go: [d.seat[0] + 10, d.seat[1] + SIT_DESK + 40], via: d.via },
                     { pose: "stand", dur: Infinity, hop: true, mix: [["wave", 0.25, 1.1]] }]);
    }
    rest(p);
  }

  // 쉬는 사람 규칙: 기본은 앉아 있기(소파·탕비실·회의실·빈 책상). 서서 돌아다니는 사람은 한꺼번에 3명까지.
  const SIT: [string, number][] = [["sofa", 14], ["cafe", 12], ["chatSit", 14], ["meeting", 9], ["read", 7], ["deskrest", 9]];
  const MOVE: [string, number][] = [["coffee", 12], ["chat", 8], ["phone", 5], ["stretch", 3], ["server", 3], ["stroll", 6]];
  const ROAM_MAX = 3;
  const PHONE_START = 0.4;
  const PHONE_SIT: [string, number, number][] = [["sitScroll", 0.22, 6], ["sitText", 0.1, 3.5], ["sitLol", 0.05, 1.8]];
  const PHONE_STAND: [string, number, number][] = [["phScroll", 0.3, 5], ["phText", 0.12, 3], ["phLol", 0.05, 1.6]];
  const sofaPose = () => (Math.random() < PHONE_START ? "sitScroll" : "sitFront");
  const sofaMix = (pose: string, extra: [string, number, number][]): [string, number, number][] =>
    (pose === "sitScroll"
      ? [["sitText", 0.14, 3.5], ["sitLol", 0.08, 1.8], ["sitFront", 0.1, 3], ...extra]
      : [...PHONE_SIT, ...extra]);
  const deskRestMix = (pose: string): [string, number, number][] =>
    (pose === "sitBackPhone"
      ? [["sitBack", 0.12, 3], ["scratch", 0.06, 1.4]]
      : [["sitBackPhone", 0.28, 6], ["scratch", 0.08, 1.4]]);
  const roaming = (except: Person) =>
    [...L.people.values()].filter((q) => q !== except && !q.leaving && modeOf(q) === "rest" && !q.seated).length;

  function rest(p: Person) {
    const pool = roaming(p) >= ROAM_MAX - 1 || Math.random() < 0.7 ? SIT : MOVE;
    const total = pool.reduce((n, r) => n + r[1], 0);
    for (let tries = 0; tries < 5; tries++) {
      let r = Math.random() * total, kind = pool[0][0];
      for (const [k, w] of pool) { if ((r -= w) < 0) { kind = k; break; } }
      p.seated = SIT.some(([k]) => k === kind);
      if (ACTS[kind](p)) return;
    }
    p.seated = true;                          // 앉을 데가 다 찼다 — 빈 책상, 그것도 없으면 그 자리에서 쉰다
    if (ACTS.deskrest(p)) return;
    run(p, [{ pose: "stand", dur: rnd(6, 12), mix: PHONE_STAND }]);
  }

  const ACTS: Record<string, (p: Person) => boolean> = {
    chat: (p) => roaming(p) <= 1 && startChat(p, false),     // 서서 떠드는 건 둘이서, 돌아다니는 사람이 적을 때만
    chatSit: (p) => startChat(p, true),
    deskrest(p) {                            // 빈 책상에 앉아 쉬기 — 일하는 사람 자리는 안 뺏는다
      const free = OFFICE_DESKS.length - L.desks.size;
      if (!L.desks.has(p.id) && free < 3) return false;
      const d = deskOf(p);
      if (!d) return false;
      const pose = Math.random() < PHONE_START ? "sitBackPhone" : "sitBack";
      run(p, [{ go: sitAt(d.seat, SIT_DESK), via: d.via },
              { pose, dur: rnd(18, 36), chair: chairOf(d), mix: deskRestMix(pose) }]);
      return true;
    },
    coffee(p) {
      const s = takeSpot(p, "coffee");
      if (!s) return false;
      const steps: ActStep[] = [{ go: s.at, via: s.via }, { pose: "standBack", dur: 2.4, face: s.face, say: () => line("brew") },
                     { pose: "sip", dur: 1.6, say: () => line("sip") }, { pose: "mug", dur: rnd(2, 4) },
                     { pose: "sip", dur: 1.6 }, { pose: "mug", dur: rnd(1.5, 3) }];
      const seat = Math.random() < 0.5 && takeSpot(p, "cafe");
      const sp = sofaPose();
      if (seat) steps.push({ go: sitAt(seat.at, SIT_SOFA), via: seat.via },
                           { pose: sp, dur: rnd(8, 14), face: seat.face, mix: sofaMix(sp, [["sofaTalk", 0.12, 1.6]]) });
      run(p, steps);
      return true;
    },
    sofa(p) {
      const s = takeSpot(p, "sofa");
      if (!s) return false;
      const pose = sofaPose();
      run(p, [{ go: sitAt(s.at, SIT_SOFA), via: s.via },
              { pose, dur: rnd(20, 42), face: s.face, sleepy: pose === "sitFront",
                mix: sofaMix(pose, [["sofaTalk", 0.1, 1.6]]) }]);
      return true;
    },
    cafe(p) {
      const s = takeSpot(p, "cafe");
      if (!s) return false;
      const pose = sofaPose();
      run(p, [{ go: sitAt(s.at, SIT_SOFA), via: s.via },
              { pose, dur: rnd(16, 32), face: s.face, say: () => line("think"), sayKind: "think",
                mix: sofaMix(pose, [["sofaTalk", 0.08, 1.4]]) }]);
      return true;
    },
    phone(p) {
      const s = takeSpot(p, "stroll");
      if (!s) return false;
      const face = pick([1, -1]);
      if (Math.random() < 0.4) {               // 통화
        run(p, [{ go: s.at, via: s.via }, { pose: "phone", dur: rnd(3, 4), face, say: () => line("phone") },
                { pose: "phone", dur: rnd(3, 4), say: () => line("phone") }, { pose: "laugh", dur: 1.2 }]);
      } else {                                 // 서서 휴대폰 만지작 — 스크롤하다 답장하다 킥킥
        run(p, [{ go: s.at, via: s.via },
                { pose: "phScroll", dur: rnd(4, 7), face, say: Math.random() < 0.5 ? () => line("scroll") : null, sayKind: "think" },
                { pose: "phText", dur: rnd(2.5, 4), say: Math.random() < 0.5 ? () => line("texting") : null, sayKind: "think" },
                { pose: "phLol", dur: 1.6, say: () => line("lol") }, { pose: "phScroll", dur: rnd(3, 6), mix: PHONE_STAND }]);
      }
      return true;
    },
    read(p) {
      const arm = takeSpot(p, "armchair");
      if (arm) {
        run(p, [{ go: sitAt(arm.at, SIT_SOFA), via: arm.via },
                { pose: "sitFront", dur: rnd(20, 38), face: arm.face, say: () => line("reading"), sayKind: "think", sleepy: true }]);
        return true;
      }
      return false;                           // 서재는 안락의자 둘뿐 — 서서 읽는 자리는 붐벼서 뺐다
    },
    stretch(p) {
      run(p, [{ pose: "stretch", dur: 2.2, say: () => line("stretch") }, { pose: "stand", dur: 1.2 }]);
      return true;
    },
    server(p) {
      const s = takeSpot(p, "server");
      if (!s) return false;
      run(p, [{ go: s.at, via: s.via }, { pose: "standBack", dur: 4, face: s.face, say: () => line("rackOk") },
              { pose: "think", dur: 1.6 }]);
      return true;
    },
    meeting(p) {
      const board = Math.random() < 0.4 && takeSpot(p, "board");
      if (board) {
        run(p, [{ go: board.at, via: board.via },
                { pose: "talk", dur: 5, face: -1, say: () => line("meeting") }, { pose: "think", dur: 2 }]);
        return true;
      }
      const s = takeSpot(p, "meeting");
      if (!s) return false;
      run(p, [{ go: sitAt(s.at, SIT_DESK), via: s.via },
              { pose: "sitBack", dur: rnd(18, 34), chair: s.chair, mix: [["sitBackPhone", 0.25, 6], ["scratch", 0.1, 1.4]] }]);
      return true;
    },
    stroll(p) {
      const s = takeSpot(p, "stroll");
      const keys = Object.keys(OFFICE_MAP_NODES).filter((k) => !["out", "door"].includes(k));
      const n = OFFICE_MAP_NODES[pick(keys)];
      const at: Pt = s ? s.at : [n[0] + rnd(-10, 10), n[1] + rnd(-6, 6)];
      const look = Math.random() < 0.35;      // 멈춰 서서 휴대폰 확인
      run(p, [{ go: at, via: s?.via },
              { pose: look ? "phScroll" : "stand", dur: look ? rnd(4, 7) : rnd(2, 4), face: pick([1, -1]),
                say: look && Math.random() < 0.4 ? () => line("scroll") : null, sayKind: "think" },
              { pose: "think", dur: 1.6, say: Math.random() < 0.4 ? () => line("think") : null, sayKind: "think" }]);
      return true;
    },
  };

  // ---------- 수다 — 두세 명이 둘러서서 번갈아 말한다 ----------
  function free(q: Person) {
    return !q.leaving && !q.chat && modeOf(q) === "rest";
  }
  function startChat(p: Person, sit: boolean): boolean {
    const busySpots = new Set([...L.chats].map((c) => c.spot));
    const spots = OFFICE_CHATS.filter((c) => !busySpots.has(c) && !!c.sit === sit
      && !c.pts.some((pt) => occupied(pt, p)));
    const mates = shuffle([...L.people.values()].filter((q) => q !== p && free(q)));
    if (!spots.length || !mates.length) return false;
    const spot = pick(spots);
    const n = sit ? Math.min(spot.pts.length, 1 + mates.length) : 2;       // 서서 떠드는 건 둘까지
    const members = [p, ...mates.slice(0, n - 1)];
    const chat: Chat = { spot, members, start: 0, next: 0, said: 0, last: null, born: L.t,
                   lines: 4 + Math.floor(Math.random() * 4) + n };
    L.chats.add(chat);
    members.forEach((q, k) => {
      if (q !== p) { leaveChat(q); release(q); }
      q.chat = chat;
      q.seated = !!spot.sit;
      const at = spot.sit ? sitAt(spot.pts[k], SIT_SOFA) : spot.pts[k];
      run(q, [{ go: at, via: spot.vias?.[k] || spot.via }, { pose: spot.sit ? "sitFront" : "stand", dur: Infinity, chat: true }]);
    });
    return true;
  }
  function leaveChat(p: Person) {
    const c = p.chat;
    if (!c) return;
    p.chat = null;
    c.members = c.members.filter((q) => q !== p);
    if (c.members.length < 2) endChat(c, false);
  }
  function endChat(c: Chat, bye: boolean) {
    L.chats.delete(c);
    for (const q of c.members) {
      q.chat = null;
      if (bye && Math.random() < 0.7) { q.over = { pose: "wave", until: L.t + 1 }; say(q, line("bye"), 1.4); }
      if (q.act?.chat) q.until = L.t + (bye ? 1.1 : 0);
    }
  }
  const FUNNY = /ㅋ|대박|천재|!|lol|haha|😂/i;
  function chatTick(c: Chat) {
    const here = c.members.filter((q) => !q.path.length && q.act?.chat);
    if (!c.start) {
      if (here.length === c.members.length || (L.t - c.born > 22 && here.length >= 2)) { c.start = L.t; c.next = L.t + 0.4; }
      else if (L.t - c.born > 40) return endChat(c, false);
      return;
    }
    if (L.t < c.next) return;
    if (c.said >= c.lines || here.length < 2) return endChat(c, true);
    const speaker = pick(here.filter((q) => q !== c.last)) || here[0];
    const said = line("talk");
    say(speaker, said, 2.3);
    speaker.over = { pose: c.spot.sit ? "sofaTalk" : (Math.random() < 0.75 ? "talk" : "wave"), until: L.t + 2.2 };
    const cx = here.reduce((n, q) => n + q.x, 0) / here.length;
    speaker.face = speaker.x < cx ? 1 : -1;
    const funny = FUNNY.test(said);
    for (const q of here) {
      if (q === speaker) continue;
      q.face = q.x < speaker.x ? 1 : -1;
      const r = Math.random();
      if (!c.spot.sit && (funny ? r < 0.65 : r < 0.25)) q.over = { pose: "laugh", until: L.t + rnd(1.2, 2) };
      else if (!c.spot.sit && r < 0.45) q.over = { pose: "think", until: L.t + rnd(1, 1.8) };
    }
    c.last = speaker;
    c.said++;
    c.next = L.t + rnd(2.3, 3);
  }

  // ---------- 회의 — 같은 프로젝트(project) 사람들이 잠깐 모여 이야기한다 ----------
  const MEET_GAP = 55, MEET_STAY = 15;
  function meetTick(t: number) {
    const groups = new Map<string, Person[]>();
    for (const p of L.people.values()) {
      if (p.leaving || p.meeting || p.chat || p.status !== "working" || !p.project) continue;
      if (!groups.has(p.project)) groups.set(p.project, []);
      groups.get(p.project)!.push(p);
    }
    const used = new Set<number>();                        // 이번 틱에 이미 간 자리 — 프로젝트끼리 자리를 나눠 쓴다
    for (const [proj, members] of groups) {
      if (members.length < 2) continue;
      const next = L.meetNext.get(proj);
      if (next === undefined) { L.meetNext.set(proj, t + rnd(20, 50)); continue; }   // 처음 본 프로젝트는 조금 뒤에
      if (t < next) continue;
      const idx = shuffle(OFFICE_MEETING.spots.map((_, i) => i)).filter((i) => !used.has(i));
      const ms = shuffle(members);
      const n = Math.min(ms.length, idx.length);
      if (n < 2) { L.meetNext.set(proj, t + 15); continue; }   // 자리가 모자라 둘이 못 모이면 잠시 뒤 다시
      for (let k = 0; k < n; k++) {
        const p = ms[k], spot = OFFICE_MEETING.spots[idx[k]];
        used.add(idx[k]);
        p.meeting = true;
        run(p, [{ go: spot.at, via: spot.via },
                { pose: "stand", dur: MEET_STAY, meet: true,
                  face: spot.at[0] < OFFICE_MEETING.zone[0] ? 1 : -1,
                  mix: [["talk", 0.4, 1.7], ["think", 0.15, 1.5], ["wave", 0.08, 1.1]] }]);
      }
      L.meetNext.set(proj, t + MEET_GAP + rnd(0, 25));
    }
  }

  // ---------- 서브에이전트(작은 캐릭터) ----------
  function slotOf(m: Mini): Pt {
    const p = L.people.get(m.owner);
    if (!p) return [m.x, m.y];
    const row0 = [...L.minis.values()].filter((q) => q.owner === m.owner && q.state !== "out" && q.state !== "bye");
    const k = Math.max(0, row0.indexOf(m));
    const row = Math.floor(k / 6), col = k % 6, dir = homeOf(p)?.queue || 1;
    // 회의실 의자에서 일하면 옆이 의자 줄이다 — 의자 위가 아니라 앞 통로 쪽으로 비스듬히 선다
    if (!p.path.length && (p.act?.chair ?? -1) >= OFFICE_DESKS.length)
      return [p.x + 20 + col * 26 - row * 14, p.y + 34 + col * 15 + row * 22];
    return [p.x + dir * (46 + col * 27 - row * 12), p.y + 20 + col * 4 + row * 26];
  }
  function miniLeave(m: Mini, delay: number) {
    if (m.state === "bye" || m.state === "out") return;
    m.state = "bye";
    m.byeAt = L.t + delay;
  }

  // ---------- 데이터가 오면 들이고 내보낸다 ----------
  const START_NODES = ["inner", "la1", "B1", "B2", "C1", "C2", "A2", "A3", "ra2", "ra3"];
  function sync(list: PersonInput[]) {
    const seen = new Set<string>();
    for (const d of list) {
      seen.add(d.id);
      let p = L.people.get(d.id);
      const fresh = !p;
      if (fresh) {
        p = {
          id: d.id, name: d.name, species: speciesOf(d.name), color: colorOf(d.name),
          x: 0, y: 0, path: [], steps: null, act: null, face: pick([1, -1]),
          phase: rnd(0, 6.28), alpha: 1, over: null, bubble: null,
          nextSay: L.t + rnd(3, 8), nextPart: 0,
          status: d.status || "rest", ask: d.ask || "", doing: I18N.act(d.doing),
          subCount: d.subCount || 0, subs: d.subs || [], commits: d.commits,
          path0: d.path, project: d.project || "", health: d.health || "", home: d.home || null,
        };
        L.people.set(d.id, p);
      }
      // ask 열쇠 — decision 상태거나 ask 가 있으면 "decision|ask문구". 문구가 바뀌면 새 질문으로 친다
      const was = p!.status, wasAsk = (p!.status === "decision" || p!.ask) ? "d|" + (p!.ask || "") : "",
            wasDoing = p!.doing, back = p!.leaving, wasMode = fresh ? null : modeOf(p!),
            wasDesk = L.desks.get(p!.id);
      Object.assign(p!, {
        name: d.name, status: d.status || "rest", ask: d.ask || "", kid: !!d.kid, home: d.home || null,
        doing: I18N.act(d.doing), subCount: d.subCount || 0, subs: d.subs || [], commits: d.commits,
        path0: d.path, project: d.project || "", health: d.health || "",
      });
      if (fresh) {
        if (L.synced) {                              // 나중에 켠 세션 = 문으로 출근
          p!.x = OFFICE_MAP_NODES.out[0]; p!.y = OFFICE_MAP_NODES.out[1]; p!.alpha = 0;
          event(p!, "in");
          choose(p!);
        } else {                                     // 첫 화면 — 통로 점 하나에 세워 두고 할 일 자리로 바로 놓는다
          [p!.x, p!.y] = OFFICE_MAP_NODES[pick(START_NODES)];
          choose(p!); snap(p!);
        }
      } else if (back) {
        p!.leaving = false; p!.gone = false; p!.alpha = 1;
        choose(p!);
      } else if (was !== p!.status || wasMode !== modeOf(p!)) {
        if (p!.status === "working" && was !== "working") event(p!, "work");
        else if (p!.status === "decision" && was !== "decision") event(p!, "ask", p!.ask);
        else if (modeOf(p!) === "work" && wasMode !== "work") event(p!, "subwork");
        else if (wasMode === "work" && modeOf(p!) === "rest") { event(p!, "done"); p!.over = { pose: "cheer", until: L.t + 1.4 }; }
        choose(p!);
      } else if (modeOf(p!) === "work" && wasDesk !== undefined && wasDesk >= 0 && !deskOk(wasDesk, p!)) {
        choose(p!);                                 // 서브에이전트가 생겼는데 옆자리가 막혔다 — 넓은 자리로 옮겨 앉는다
      }
      // 승인·질문이 새로 생기거나 문구가 바뀌면 알림 — 첫 불러오기(L.synced 전)에는 울리지 않는다
      const askKey = (p!.status === "decision" || p!.ask) ? "d|" + (p!.ask || "") : "";
      if (L.synced && askKey && askKey !== wasAsk) chime();
      if (modeOf(p!) === "work" && p!.doing && p!.doing !== wasDoing) {
        say(p!, p!.doing, 4, "work"); p!.nextSay = L.t + rnd(6, 10);
      }
      syncMinis(p!, d);
    }
    for (const p of [...L.people.values()]) {
      if (seen.has(p.id) || p.leaving) continue;
      p.leaving = true;
      leaveChat(p);
      release(p);
      L.desks.delete(p.id);
      event(p, "out");
      run(p, [{ go: OFFICE_MAP_NODES.out }]);
      for (const m of L.minis.values()) if (m.owner === p.id) miniLeave(m, 0);
    }
    // 회의 예약표 청소 — 다 나간 프로젝트의 다음 회의 시각은 버린다
    const projs = new Set([...L.people.values()].map((p) => p.project).filter(Boolean));
    for (const k of L.meetNext.keys()) if (!projs.has(k)) L.meetNext.delete(k);
    L.synced = true;
  }

  function syncMinis(p: Person, d: PersonInput) {
    const keyOf = (s: SubInput) => `${p.id}|${s.id}`;          // 서브에이전트 id 는 짧아서 주인 id 를 붙여 가른다
    const subs = d.subs || [];
    const ids = new Set(subs.map(keyOf));
    let k = 0;
    for (const m of [...L.minis.values()]) {
      if (m.owner !== p.id || m.state === "bye" || m.state === "out") continue;
      if (!ids.has(m.id)) { miniLeave(m, k++ * 1.1); event(p, "subOut", m.desc || m.type || ""); }
    }
    for (const s of subs) {
      const old = L.minis.get(keyOf(s));
      if (old) {
        const nd = I18N.act(s.doing);
        if (nd && nd !== old.doing) { old.doing = nd; old.nextSay = Math.min(old.nextSay ?? Infinity, L.t + 1); }
        continue;
      }
      const m: Mini = {
        id: keyOf(s), owner: p.id, type: s.type, desc: s.desc, doing: I18N.act(s.doing),
        x: 0, y: 0, path: [], face: pick([1, -1]), phase: rnd(0, 6.28), alpha: 1, over: null, bubble: null,
        state: "wait", flipAt: L.t + rnd(1, 3), hopAt: L.t + rnd(2, 5), nextSay: L.t + rnd(4, 10),
        si: 0, steps: null, act: null,
      };
      L.minis.set(m.id, m);
      if (L.synced) {                                // 새 서브에이전트 = 문으로 출근해서 줄에 선다
        m.x = OFFICE_MAP_NODES.out[0]; m.y = OFFICE_MAP_NODES.out[1]; m.alpha = 0; m.state = "in";
        m.path = route([m.x, m.y], [p.x, p.y]).slice(0, -1);
        event(p, "subIn", s.desc || s.type || "");
      } else { [m.x, m.y] = slotOf(m); }
    }
    p.extra = Math.max(0, (d.subCount || 0) - subs.length);
  }

  // 사건 — 글은 화면이 언어에 맞게 만든다(key = i18n 의 ev.* , arg = 끼워 넣을 말)
  function event(p: Person, key: string, arg = "") {
    const ev: LifeEvent = { at: Date.now(), id: p.id, name: p.name, color: p.color, key, arg };
    L.events.push(ev);
    if (L.events.length > 80) L.events.splice(0, L.events.length - 80);
  }

  // ---------- 한 프레임 ----------
  function update(dt: number) {
    L.t += dt;
    const t = L.t;
    for (const p of [...L.people.values()]) {
      if (p.alpha < 1 && !p.gone) p.alpha = Math.min(1, p.alpha + dt * 2);
      const paused = p.over && t < p.over.until && p.over.stop;
      if (p.path.length && !paused) {
        if (step(p, SPEED, dt, 0)) { if (p.leaving) p.gone = true; else advance(p); }
      } else if (!p.path.length && p.act && p.until !== undefined && t > p.until) advance(p);
      else if (!p.path.length && !p.act && !p.leaving) choose(p);
      if (p.gone) { p.alpha -= dt * 2.5; if (p.alpha <= 0) { L.people.delete(p.id); continue; } }
      if (p.over && t > p.over.until) p.over = null;
      // 자세 섞기 — 같은 자리에서도 가끔 다른 몸짓(만세·머리 긁기·손 흔들기·수다 손짓)
      if (p.act?.mix && p.mixAt !== undefined && t > p.mixAt && !p.over) {
        p.mixAt = t + rnd(3, 7);
        for (const [pose, prob, dur] of p.act.mix) {
          if (Math.random() < prob) {
            p.over = { pose, until: t + dur };
            if (pose === "cheer") say(p, line("cheer"), 1.4);
            if (pose === "sitLol" || pose === "phLol") say(p, line("lol"), 1.6);
            else if (/Scroll|BackPhone/.test(pose) && Math.random() < 0.3) say(p, line("scroll"), 2.2, "think");
            else if (/Text$/.test(pose) && Math.random() < 0.35) say(p, line("texting"), 2, "think");
            if (pose === "wave" && p.status === "decision") say(p, line("askWave"), 1.2);
            break;
          }
        }
      }
      // 말풍선 — 일하는 사람은 실제 하는 일, 회의 중이면 수다, 쉬는 사람은 혼잣말
      if (p.nextSay !== undefined && t > p.nextSay && !p.chat) {
        if (p.act?.work) { say(p, p.doing || line("work"), 3.6, "work"); p.nextSay = t + rnd(6, 11); }
        else if (p.act?.meet) { say(p, line("talk"), 2.4); p.nextSay = t + rnd(3.5, 6.5); }
        else if (!p.act?.hop) { if (Math.random() < 0.5) say(p, line("think"), 2.2, "think"); p.nextSay = t + rnd(10, 22); }
        else p.nextSay = t + 5;
      }
      if (p.bubble && t > p.bubble.until) p.bubble = null;
    }
    for (const c of [...L.chats]) chatTick(c);
    greet(t);
    meetTick(t);
    separate(dt);
    for (const m of [...L.minis.values()]) {
      const owner = L.people.get(m.owner);
      if (m.state === "in" && !m.path.length) {
        m.state = "wait";
        say(m, line("miniIn"), 1.4);
      }
      if (m.state === "bye" && m.byeAt !== undefined && t >= m.byeAt && !m.byeSaid) {
        m.byeSaid = true;
        m.byeEnd = t + 1.3;
        m.over = { pose: "mBye", until: m.byeEnd };
        say(m, line("miniBye"), 1.6);
      }
      if (m.state === "bye" && m.byeSaid && m.byeEnd !== undefined && t > m.byeEnd) {
        m.state = "out";
        m.path = route([m.x, m.y], OFFICE_MAP_NODES.out);
      }
      if (m.path.length) {
        if (step(m, MINI_SPEED, dt, 11) && m.state === "out") m.gone = true;
      } else if (m.state === "wait" && owner) {
        const [sx, sy] = slotOf(m);
        if (Math.hypot(sx - m.x, sy - m.y) > 2 && onFloor([sx, sy]))
          m.path = blocked([m.x, m.y], [sx, sy]) ? route([m.x, m.y], [sx, sy]) : [[sx, sy]];
        if (m.flipAt !== undefined && t > m.flipAt) { m.flipGap = rnd(1, 3); m.flipAt = t + m.flipGap; m.face = -(m.face ?? 1); m.flipT = t; }
        if (m.hopAt !== undefined && t > m.hopAt) {
          m.hopAt = t + rnd(2, 5);
          m.over = Math.random() < 0.35 ? { pose: "mSide", until: t + 1.2 } : { pose: "mJump", until: t + 0.6 };
        }
        if (m.nextSay !== undefined && t > m.nextSay) {
          m.nextSay = t + rnd(8, 16);
          if (Math.random() < 0.6) say(m, m.doing || line("miniIdle"), 2.4, m.doing ? "work" : "talk");
        }
      } else if (!owner && m.state === "wait") miniLeave(m, 0);
      if (m.alpha < 1 && !m.gone) m.alpha = Math.min(1, m.alpha + dt * 2);
      if (m.gone) { m.alpha -= dt * 2.5; if (m.alpha <= 0) L.minis.delete(m.id); }
      if (m.over && t > m.over.until) m.over = null;
      if (m.bubble && t > m.bubble.until) m.bubble = null;
    }
  }

  // 지나가다 마주치면 손 흔들어 인사 — 둘 다 걷는 중이고 가까울 때, 같은 짝은 40초에 한 번
  function greet(t: number) {
    const walkers = [...L.people.values()].filter((p) => p.path.length > 0 && !p.leaving && !p.over);
    for (let i = 0; i < walkers.length; i++) {
      for (let j = i + 1; j < walkers.length; j++) {
        const a = walkers[i], b = walkers[j];
        if (Math.hypot(a.x - b.x, a.y - b.y) > 42) continue;
        const key = [a.id, b.id].sort().join("|");
        if (t - (L.greeted.get(key) ?? -99) < 40) continue;
        L.greeted.set(key, t);
        if (Math.random() < 0.4) continue;
        for (const [p, q] of [[a, b], [b, a]] as [Person, Person][]) {
          p.face = p.x < q.x ? 1 : -1;
          p.over = { pose: "wave", until: t + 0.9 };
          say(p, line("greet"), 1.4);
        }
      }
    }
  }

  return {
    L, SIT_DESK, SIT_SOFA,
    sync, update, cut, nearest, route, colorOf, speciesOf, modeOf, busy,
    blocked, onFloor,
    drainEvents: () => L.events.splice(0, L.events.length),
    soundMuted, setSoundMuted, wakeAudio,
  };
}

export type Life = ReturnType<typeof createLife>;
