/** 사무실 화면 — 레거시 office-view.js 이식. 캔버스 2D 렌더러(30fps 스로틀은 호출부가 담당).
 * 자세 이름 → 시트 칸 표(POSES), 스프라이트 그리기·이름표·말풍선·건강 배지·파티클·디버그 표시.
 * 프레임워크 비의존 — SpriteBoard.tsx 가 rAF 루프에서 life.update() + view.render() 를 부른다.
 * 이미지 파이프라인은 view-assets.ts, 순수 그리기 도우미는 view-draw.ts 로 분리(동작 동일). */

import { OFFICE_ATLAS } from "./office-atlas";
import {
  OFFICE_BLOCKS, OFFICE_CODE, OFFICE_DESKS, OFFICE_FLOOR, OFFICE_H, OFFICE_MAP_EDGES,
  OFFICE_MAP_NODES, OFFICE_MEETING, OFFICE_SPOTS, OFFICE_W, OFFICE_WALLS,
} from "./office-world";
import { SIT_DESK, type Life } from "./office-life";
import * as I18N from "./office-i18n";
import type { Mini, Person, Pt, Walker } from "./types";
import { createSpriteAssets } from "./view-assets";
import {
  hopFx, inkOn, jelly, poseOf, roundBox, shadow, sprite, standOf, STAND, turn, turnSx, walkPose,
} from "./view-draw";

const A = OFFICE_ATLAS;

const MINI = 0.5;              // 서브에이전트 = 주인 키의 50%
const BG = "#EDEFF1";
const REDUCED = !!(typeof window !== "undefined" && window.matchMedia
  && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
const FONT = '-apple-system, BlinkMacSystemFont, "Apple SD Gothic Neo", "Malgun Gothic", "Segoe UI", sans-serif';

const rnd = (a: number, b: number) => a + Math.random() * (b - a);
const pick = <T,>(arr: T[]): T => arr[Math.floor(Math.random() * arr.length)];

interface Particle {
  x: number; y: number; text: string; color: string; born: number; life: number;
}

interface ViewState {
  s: number; ox: number; oy: number; dpr: number; small: boolean;
}

interface ChairEnt {
  chair: [number, number, number, number];
  y: number;
}

export interface OfficeViewOpts {
  /** 스프라이트 이미지 베이스 — 기본 "office" (v2/public/office, 상대 경로라
   *  dev(/)와 prod(/app/) 양쪽에서 동작). 백엔드 static/legacy는 v2가 의존하지 않는다. */
  assetBase?: string;
  debug?: boolean;
  /** 고르기 해제 포함 토글 선택 콜백 — SpriteBoard 가 props.onSelect 로 연결 */
  onSelect?: (id: string | null) => void;
}

export function createOfficeView(life: Life, opts: OfficeViewOpts = {}) {
  const assetBase = opts.assetBase ?? "office";
  const debug = !!opts.debug;
  const L = life.L;
  const assets = createSpriteAssets(assetBase);

  const st = {
    sel: null as string | null,
    view: { s: 1, ox: 0, oy: 0, dpr: 1, small: false } as ViewState,
    canvasW: 0, canvasH: 0,
    parts: [] as Particle[],
  };

  function drawPerson(ctx: CanvasRenderingContext2D, p: Person) {
    const t = L.t, stand = standOf(p), ph = p.phase, pose = poseOf(p, t);
    let dy = 0, dx = 0, tilt = 0;
    let { sx, sy } = jelly(t + ph, 3.2, 0.03);
    if (p.path.length && !(p.over && t < p.over.until)) {            // 걸음 — 저벅저벅
      sx = sy = 1;
    } else if (pose === "sitBack" && p.act?.work) {                   // 빡 코딩 — 어깨 들썩 + 잔떨림
      dx = Math.sin(t * 38 + ph) * 0.6; dy = -Math.abs(Math.sin(t * 17 + ph)) * 1.6;
      tilt = Math.sin(t * 7 + ph) * 0.03; ({ sx, sy } = jelly(t + ph, 17, 0.02));
    } else if (p.act?.hop && pose === "stand") {                      // 승인 대기 — 쉬지 않고 폴짝폴짝
      ({ dy, sx, sy } = hopFx((((t + ph) * 1.15) % 0.9), 18) || { dy: 0, sx: 1, sy: 1 });
    } else if (pose === "sitLol" || pose === "phLol") {              // 휴대폰 보다 킥킥 — 작게 들썩
      dy = -Math.abs(Math.sin(t * 13 + ph)) * 1.8; tilt = Math.sin(t * 6.5 + ph) * 0.04;
    } else if (pose.startsWith("sit")) {                              // 앉아서 — 느릿한 숨 + 갸웃
      ({ sx, sy } = jelly(t + ph, 1.8, 0.025)); tilt = Math.sin(t * 0.9 + ph) * 0.035;
    } else if (pose === "phScroll" || pose === "phText") {            // 서서 휴대폰 — 고개 까딱, 엄지 톡톡
      tilt = Math.sin(t * 1.3 + ph) * 0.025; dy = -Math.abs(Math.sin(t * 5 + ph)) * 0.6;
    } else if (pose === "laugh") {                                    // 깔깔 — 빠르게 들썩
      dy = -Math.abs(Math.sin(t * 16)) * 3; tilt = Math.sin(t * 8) * 0.05;
    } else if (pose === "talk" || pose === "sofaTalk" || pose === "phone") {
      dy = -Math.abs(Math.sin(t * 7 + ph)) * 2; tilt = Math.sin(t * 3.5 + ph) * 0.05;
    } else if (pose === "cheer") {
      ({ dy, sx, sy } = hopFx((t * 1.6) % 0.62, 8) || { dy: 0, sx: 1, sy: 1 });
    } else if (pose === "wave") {
      tilt = Math.sin(t * 9) * 0.07;
    } else if (pose === "sip" || pose === "mug") {
      tilt = Math.sin(t * 1.4 + ph) * 0.04;
    } else if (pose === "stretch") {
      sy = 1 + Math.abs(Math.sin(t * 2.5)) * 0.05; sx = 1 - (sy - 1) * 0.6;
    } else {
      tilt = Math.sin(t * 1.7 + ph) * 0.035;
    }
    if (!pose.startsWith("sit")) shadow(ctx, p.x, p.y, stand * 0.21 * (1 + dy / 60));
    // 의자의 꼬마는 방석을 깔고 앉은 듯 올린다 — 안 그러면 등받이에 모자만 보인다.
    // 의자마다 올리는 양(chairs[].kid)을 그림 만들 때 재 둔다: 어느 줄에서나 머리가 비슷하게 나온다.
    const chairIdx = p.kid && !p.path.length && p.act ? p.act.chair ?? -1 : -1;
    const chair = chairIdx >= 0 ? A.chairs?.[chairIdx] : undefined;
    if (chair) dy -= chair.kid || 0;
    p.box = sprite(assets, ctx, p.species, p.color, pose, p.x + dx, p.y + dy, stand, p.face ?? 1,
                   { alpha: p.alpha, sx: sx * turnSx(p, t), sy, tilt });
    p.feet = p.y;
  }

  function drawMini(ctx: CanvasRenderingContext2D, m: Mini) {
    const p = L.people.get(m.owner);
    const species = p?.species || "frog", color = p?.color || "#888", stand = p ? standOf(p) : STAND;
    const t = L.t, ph = m.phase;
    let pose = "mStand", face = m.face ?? 1, dy = 0, dx = 0, tilt = 0, sx = 1, sy = 1;
    if (m.over && t < m.over.until) pose = m.over.pose;
    if (m.path.length) {                                              // 종종걸음 — 빠른 걸음, 발 딛을 때 통
      if (Math.abs(m.vx || 0) > 0.5) turn(m, (m.vx || 0) > 0 ? 1 : -1, t);
      face = m.face ?? 1;
      pose = walkPose(m, true);
      const w = Math.sin(m.wp || 0), c = 1 - Math.abs(w);
      tilt = w * 0.08; dy = -Math.abs(w) * 2.6;
      sy = 1 - 0.06 * c * c; sx = 1 + 0.05 * c * c;
    } else {                                                          // 줄 서서 꼼지락 — 한시도 가만히 안 있는다
      const w = Math.sin(t * 6 + ph);
      tilt = w * 0.12; dx = Math.sin(t * 2.3 + ph) * 2; dy = -Math.abs(w) * 1.6;
      ({ sx, sy } = jelly(t + ph, 6, 0.05));
      if (pose === "mJump") ({ dy, sx, sy } = hopFx(0.6 - ((m.over?.until ?? t) - t), 9) || { dy, sx, sy });
      if (m.flipT && t - m.flipT < 0.15) sx *= 0.7 + ((t - m.flipT) / 0.15) * 0.3;   // 고개 돌릴 때 '뿅'
    }
    shadow(ctx, m.x, m.y, stand * MINI * 0.2);
    m.box = sprite(assets, ctx, species, color, pose, m.x + dx, m.y + dy, stand, face,
                   { alpha: m.alpha, k: MINI, sx: sx * turnSx(m, t), sy, tilt });
  }

  // 고른 사람 발밑 고리 — 그 사람 색으로. 뒤쪽 반은 캐릭터보다 먼저(바닥에), 앞쪽 반은 캐릭터 다음에 그려
  // 고리가 발을 둘러싼 것처럼 보이게 한다(앉은 사람은 의자 등받이 위로 앞쪽 반이 보인다).
  function drawRing(ctx: CanvasRenderingContext2D, front: boolean) {
    const p = st.sel ? L.people.get(st.sel) : undefined;
    if (!p || p.alpha < 0.3) return;
    const r = standOf(p) * 0.42 * (1 + Math.sin(L.t * 4) * 0.05), ry = r * 0.34;
    ctx.save();
    ctx.globalAlpha = Math.min(1, p.alpha);
    ctx.strokeStyle = p.color;
    ctx.lineWidth = 3.5;
    ctx.beginPath();
    if (front) ctx.ellipse(p.x, p.y - 1, r, ry, 0, 0, Math.PI);
    else {
      ctx.fillStyle = p.color + "38";
      ctx.ellipse(p.x, p.y - 1, r, ry, 0, 0, Math.PI * 2);
      ctx.fill();
      ctx.beginPath();
      ctx.ellipse(p.x, p.y - 1, r, ry, 0, Math.PI, Math.PI * 2);
    }
    ctx.stroke();
    ctx.restore();
  }

  // ---------- 글씨 — 이름표(발밑)·말풍선(머리 위) ----------
  function nameTags(ctx: CanvasRenderingContext2D) {
    interface Tag { p: Person; x: number; y: number; w: number; h: number }
    const tags: Tag[] = [], fs = st.view.small ? 10 : 12, th = fs + 6;
    ctx.font = `600 ${fs}px ${FONT}`;
    for (const p of L.people.values()) {
      if (!p.box || p.alpha < 0.3) continue;
      const [x, y] = scr(p.x, p.feet ?? p.y);
      tags.push({ p, x, y: y + 3, w: ctx.measureText(p.name).width + 18, h: th });
    }
    tags.sort((a, b) => a.y - b.y);
    const placed: Tag[] = [];
    for (const g of tags) {                    // 겹치면 아래로 비켜 쌓는다
      for (let k = 0; k < 6; k++) {
        const o = placed.find((q) => Math.abs(q.x - g.x) < (q.w + g.w) / 2 + 2
          && g.y < q.y + q.h + 1 && g.y + g.h > q.y - 1);
        if (!o) break;
        g.y = o.y + o.h + 2;
      }
      placed.push(g);
      const lx = Math.round(g.x - g.w / 2), on = g.p.id === st.sel;
      ctx.fillStyle = on ? g.p.color : "rgba(255, 255, 255, .9)";
      ctx.strokeStyle = on ? "rgba(20, 30, 25, .5)" : "rgba(20, 30, 25, .14)";
      ctx.lineWidth = 1;
      roundBox(ctx, lx, g.y, g.w, g.h, 3); ctx.fill(); ctx.stroke();
      ctx.fillStyle = on ? inkOn(g.p.color) : g.p.color;
      ctx.beginPath(); ctx.arc(lx + 8, g.y + g.h / 2 + 0.5, 3, 0, Math.PI * 2); ctx.fill();
      ctx.fillStyle = on ? inkOn(g.p.color) : "#1E2622";
      ctx.textBaseline = "middle"; ctx.font = `${on ? 800 : 600} ${fs}px ${FONT}`;
      ctx.fillText(g.p.name, lx + 14, g.y + g.h / 2 + 1);
    }
  }
  function bubbles(ctx: CanvasRenderingContext2D) {
    interface B { x: number; bottom: number; w: number; h: number; text: string; kind: string; color?: string; fs: number }
    const items: B[] = [], fs = st.view.small ? 11 : 13;
    const add = (e: Walker, text: string, kind: string, color?: string) => {
      if (!e.box || e.alpha < 0.3) return;
      const [x, top] = scr(e.x, e.box.top);
      ctx.font = kind === "ask" ? `700 ${fs}px ${FONT}` : `${kind === "work" ? 600 : 500} ${fs}px ${FONT}`;
      const w = ctx.measureText(text).width + (kind === "ask" ? 8 : kind === "work" ? 22 : 16);
      items.push({ x, bottom: top - 4, w, h: kind === "ask" ? fs + 6 : fs + 11, text, kind, color, fs });
    };
    for (const p of L.people.values()) {
      // 승인 대기 또는 질문(ask)이 있으면 머리 위에 짧은 ask 표시 — 60자쯤에서 자른다
      const ask = p.status === "decision" || p.ask ? I18N.ask(p.ask, true) || I18N.t("needs") : "";
      if (ask) add(p, I18N.cut(ask, 60), "ask", p.color);
      if (p.bubble) add(p, p.bubble.text, p.bubble.kind, p.color);
    }
    for (const m of L.minis.values()) {
      if (m.bubble) add(m, m.bubble.text, m.bubble.kind, L.people.get(m.owner)?.color);
    }
    items.sort((a, b) => b.bottom - a.bottom);
    const placed: B[] = [];
    for (const b of items) {                  // 겹치면 위로 비켜 쌓는다
      for (let k = 0; k < 8; k++) {
        const o = placed.find((q) => Math.abs(q.x - b.x) < (q.w + b.w) / 2 + 3
          && b.bottom > q.bottom - q.h - 3 && b.bottom - b.h < q.bottom + 3);
        if (!o) break;
        b.bottom = o.bottom - o.h - 4;
      }
      placed.push(b);
      drawBubble(ctx, b);
    }
  }
  function drawBubble(ctx: CanvasRenderingContext2D, b: { x: number; bottom: number; w: number; h: number; text: string; kind: string; color?: string; fs: number }) {
    const lx = Math.round(b.x - b.w / 2), ty = Math.round(b.bottom - b.h);
    ctx.textBaseline = "middle";
    if (b.kind === "ask") {                    // 승인 대기 — 빨간 글씨(흰 테두리로 어디서나 읽히게)
      ctx.font = `700 ${b.fs}px ${FONT}`; ctx.textAlign = "center"; ctx.lineJoin = "round";
      ctx.lineWidth = 4; ctx.strokeStyle = "rgba(255, 255, 255, .95)";
      ctx.strokeText(b.text, b.x, ty + b.h / 2);
      ctx.fillStyle = "#D7263D"; ctx.fillText(b.text, b.x, ty + b.h / 2);
      ctx.textAlign = "start";
      return;
    }
    const think = b.kind === "think";
    ctx.fillStyle = think ? "#F2F4F6" : "#FFFFFF";
    ctx.strokeStyle = "rgba(20, 30, 25, .2)"; ctx.lineWidth = 1;
    roundBox(ctx, lx, ty, b.w, b.h, think ? 11 : 6); ctx.fill(); ctx.stroke();
    if (think) {                               // 생각 = 동글동글 꼬리
      ctx.beginPath(); ctx.arc(b.x - 2, ty + b.h + 4, 2.5, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
    } else {
      ctx.beginPath(); ctx.moveTo(b.x - 5, ty + b.h); ctx.lineTo(b.x, ty + b.h + 6); ctx.lineTo(b.x + 5, ty + b.h); ctx.fill();
    }
    if (b.kind === "work") { ctx.fillStyle = b.color || "#4ADE80"; ctx.fillRect(lx + 4, ty + 5, 3, b.h - 10); }
    ctx.fillStyle = think ? "#3F4A55" : "#1E2622";
    ctx.font = `${b.kind === "work" ? 600 : 500} ${b.fs}px ${FONT}`;
    ctx.fillText(b.text, lx + (b.kind === "work" ? 12 : 8), ty + b.h / 2 + 1);
  }

  // 세션 건강도 — stale = 머리 위 주황 "!", long_idle = 옅은 "zZ". 화면 좌표로 작게 그린다.
  function healthBadges(ctx: CanvasRenderingContext2D) {
    const fs = st.view.small ? 9 : 10;
    ctx.font = `700 ${fs}px ${FONT}`;
    ctx.textAlign = "center"; ctx.textBaseline = "middle";
    for (const p of L.people.values()) {
      const h = p.health;
      if ((h !== "stale" && h !== "long_idle") || !p.box || p.alpha < 0.3) continue;
      const [x, y] = scr(p.box.x + p.box.w - 3, p.box.top + 7);
      ctx.globalAlpha = Math.min(1, p.alpha);
      if (h === "stale") {
        ctx.fillStyle = "#F5A623"; ctx.strokeStyle = "rgba(255, 255, 255, .95)"; ctx.lineWidth = 2;
        ctx.beginPath(); ctx.arc(x, y, fs * 0.85, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
        ctx.fillStyle = "#1E2622"; ctx.fillText("!", x, y + 0.5);
      } else {
        ctx.fillStyle = "rgba(86, 97, 91, .85)"; ctx.fillText("zZ", x + 4, y - 2);
      }
    }
    ctx.globalAlpha = 1; ctx.textAlign = "start";
  }

  // ---------- 한 장 그리기 ----------
  /** 캔버스 크기 변경 대응 — CSS 픽셀 크기를 읽어 backing store·뷰 변환을 다시 잡는다 */
  function resize(cv: HTMLCanvasElement) {
    const cw = cv.clientWidth, ch = cv.clientHeight;
    if (!cw || !ch) return;
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    cv.width = Math.round(cw * dpr); cv.height = Math.round(ch * dpr);
    cv.style.width = cw + "px"; cv.style.height = ch + "px";
    const s = Math.min(cw / (OFFICE_W - 40), ch / (OFFICE_H - 30));    // 벽 바깥 여백은 조금 잘라도 된다
    st.view = { s, ox: cw / 2 - OFFICE_W / 2 * s, oy: ch / 2 - (OFFICE_H / 2 + 4) * s, dpr, small: cw < 700 };
    st.canvasW = cv.width; st.canvasH = cv.height;
  }
  const scr = (x: number, y: number): Pt => [st.view.ox + x * st.view.s, st.view.oy + y * st.view.s];

  // 회의 구역 표시 — 회의실 안 탁자 앞 빈 바닥에 옅은 점선 러그. 모임이 없어도 항상 보인다.
  function drawMeetZone(ctx: CanvasRenderingContext2D) {
    const [zx, zy] = OFFICE_MEETING.zone;
    const pulse = REDUCED ? 0.4 : 0.32 + Math.sin(L.t * 1.4) * 0.12;
    ctx.save();
    ctx.globalAlpha = pulse;
    ctx.fillStyle = "#8A9AA6";
    ctx.strokeStyle = "#8A9AA6";
    ctx.setLineDash([6, 5]);
    ctx.lineWidth = 1.5;
    ctx.beginPath(); ctx.ellipse(zx, zy, 62, 24, 0, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
    ctx.setLineDash([]);
    ctx.font = `600 11px ${FONT}`;
    ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.fillStyle = "#5A6B76";
    ctx.fillText(I18N.t("meet.zone"), zx, zy + 34);
    ctx.restore();
  }

  const CHAIRS: ChairEnt[] = (A.chairs || []).map((c) => ({ chair: c.box, y: c.seat[1] + SIT_DESK + 1 }));
  function drawChair(ctx: CanvasRenderingContext2D, c: ChairEnt) {
    const [x, y, w, h] = c.chair;
    ctx.drawImage(assets.imgs!.chairs, x, y, w, h, x, y, w, h);
  }

  function particles() {
    const t = L.t;
    for (const p of L.people.values()) {
      if (st.parts.length > 60 || t < (p.nextPart ?? 0)) continue;
      if (p.act?.work && !p.path.length) {
        p.nextPart = t + rnd(0.5, 1.1);
        const side = Math.random() < 0.5 ? -1 : 1;
        st.parts.push({ x: p.x + side * rnd(14, 26), y: p.y - standOf(p) * rnd(0.55, 0.8),
                        text: pick(OFFICE_CODE), color: p.color, born: t, life: 1.6 });
      } else if (p.act?.sleepy && !p.path.length && t - (p.arrivedAt ?? t) > 8 && !(p.over && t < p.over.until)) {
        p.nextPart = t + rnd(1.6, 2.6);
        st.parts.push({ x: p.x + 12, y: p.y - standOf(p) * 0.75, text: "z", color: "#7A8A99", born: t, life: 2.2 });
      }
    }
    st.parts = st.parts.filter((q) => t - q.born < q.life);
  }

  function debugMarks(ctx: CanvasRenderingContext2D) {
    ctx.font = "11px monospace";
    ctx.strokeStyle = "rgba(220,0,0,.8)"; ctx.lineWidth = 3;
    for (const [a, b] of OFFICE_WALLS) { ctx.beginPath(); ctx.moveTo(a[0], a[1]); ctx.lineTo(b[0], b[1]); ctx.stroke(); }
    ctx.strokeStyle = "rgba(230,120,0,.9)"; ctx.lineWidth = 2;
    for (const poly of OFFICE_BLOCKS) {
      ctx.beginPath();
      poly.forEach((p, i) => (i ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1])));
      ctx.closePath(); ctx.stroke();
    }
    ctx.strokeStyle = "rgba(0,150,0,.6)";
    ctx.beginPath();
    OFFICE_FLOOR.forEach((p, i) => (i ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1])));
    ctx.closePath(); ctx.stroke();
    ctx.lineWidth = 1;
    ctx.strokeStyle = "rgba(0,0,170,.35)";
    for (const [a, b] of OFFICE_MAP_EDGES) {
      ctx.beginPath();
      ctx.moveTo(OFFICE_MAP_NODES[a][0], OFFICE_MAP_NODES[a][1]);
      ctx.lineTo(OFFICE_MAP_NODES[b][0], OFFICE_MAP_NODES[b][1]);
      ctx.stroke();
    }
    for (const [k, [x, y]] of Object.entries(OFFICE_MAP_NODES)) {
      ctx.fillStyle = "#00A"; ctx.fillRect(x - 3, y - 3, 6, 6); ctx.fillText(k, x + 4, y - 4);
    }
    ctx.fillStyle = "#C00";
    for (const d of OFFICE_DESKS) ctx.fillRect(d.seat[0] - 4, d.seat[1] - 4, 8, 8);
    ctx.fillStyle = "#0A0";
    for (const list of Object.values(OFFICE_SPOTS)) for (const s of list) ctx.fillRect(s.at[0] - 4, s.at[1] - 4, 8, 8);
    for (const p of L.people.values()) {                  // 가는 길
      if (!p.path.length) continue;
      ctx.strokeStyle = p.color; ctx.beginPath(); ctx.moveTo(p.x, p.y);
      for (const q of p.path) ctx.lineTo(q[0], q[1]);
      ctx.stroke();
    }
  }

  function drawAll(ctx: CanvasRenderingContext2D) {
    const v = st.view;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.fillStyle = BG;
    ctx.fillRect(0, 0, st.canvasW, st.canvasH);
    if (!assets.loaded || !assets.imgs) return;
    ctx.setTransform(v.s * v.dpr, 0, 0, v.s * v.dpr, v.ox * v.dpr, v.oy * v.dpr);
    ctx.imageSmoothingQuality = "high";
    ctx.drawImage(assets.imgs.floor, 0, 0, OFFICE_W, OFFICE_H);
    drawMeetZone(ctx);
    drawRing(ctx, false);
    // 깊이 순서(발끝 y)대로 — 책상·회의실 의자 등받이도 끼워 넣는다
    const all: (Person | Mini | ChairEnt)[] = [...L.people.values(), ...L.minis.values(), ...CHAIRS]
      .sort((a, b) => a.y - b.y);
    for (const e of all) {
      if ("chair" in e) drawChair(ctx, e);
      else if ("species" in e) drawPerson(ctx, e);
      else drawMini(ctx, e as Mini);
    }
    drawRing(ctx, true);
    if (debug) debugMarks(ctx);
    ctx.setTransform(v.dpr, 0, 0, v.dpr, 0, 0);    // 글씨는 화면 좌표로 — 사무실을 줄여도 크기 그대로
    for (const q of st.parts) {
      const a = 1 - (L.t - q.born) / q.life;
      const [x, y] = scr(q.x, q.y - (L.t - q.born) * 22);
      ctx.globalAlpha = Math.max(0, a);
      ctx.font = "700 12px ui-monospace, Menlo, Consolas, monospace";
      ctx.fillStyle = q.color;
      ctx.fillText(q.text, x - 8, y);
    }
    ctx.globalAlpha = 1;
    nameTags(ctx);
    bubbles(ctx);
    healthBadges(ctx);
    for (const p of L.people.values()) {
      if ((p.extra ?? 0) > 0 && p.box) {
        const [mx, my] = scr(p.x + 30 + 6 * 21, p.y + 34);
        ctx.font = `700 12px ${FONT}`; ctx.fillStyle = "#1E2622"; ctx.fillText(`+${p.extra}`, mx, my);
      }
    }
  }

  /** 한 프레임 그리기 — 파티클 갱신 + 전체 렌더 (레거시 frame()의 particles()+draw()) */
  function render(ctx: CanvasRenderingContext2D) {
    if (!REDUCED) particles();
    drawAll(ctx);
  }

  // ---------- 고르기 — 캐릭터를 누르면 발밑에 고리. 같은 사람을 다시 누르면 해제 ----------
  /** 캔버스 CSS 픽셀 좌표 → 사무실 좌표 */
  function toOffice(sx: number, sy: number): Pt {
    return [(sx - st.view.ox) / st.view.s, (sy - st.view.oy) / st.view.s];
  }
  function hit(sx: number, sy: number): string | null {
    const [x, y] = toOffice(sx, sy);
    const all = [...L.people.values(), ...L.minis.values()]
      .filter((e) => e.box && e.alpha > 0.3)
      .sort((a, b) => b.y - a.y);
    for (const e of all) {
      const bx = e.box!.x, by = e.box!.y, w = e.box!.w, h = e.box!.h;
      if (x >= bx && x <= bx + w && y >= by && y <= by + h) return (e as Mini).owner || e.id;
    }
    return null;
  }
  function setSelected(id: string | null) { st.sel = id; }
  function select(id: string | null, keep?: boolean) {
    st.sel = id && (keep || id !== st.sel) ? id : null;
    opts.onSelect?.(st.sel);
  }

  // 시험·점검용 — 지금 사무실에 누가 어디서 무엇을 하는지
  function debugInfo() {
    return [...L.people.values()].map((p) => ({
      id: p.id, name: p.name, species: p.species, color: p.color, kid: !!p.kid, status: p.status,
      home: p.home, project: p.project,
      act: p.path.length ? "walk" : p.act?.work ? "desk" : p.act?.hop ? "ask" : (p.act?.pose || "idle"),
      pose: p.box ? poseOf(p, L.t) : null, bubble: p.bubble?.text || "", selected: p.id === st.sel,
      minis: [...L.minis.values()].filter((m) => m.owner === p.id && (m.state === "wait" || m.state === "in")).length,
      at: p.box ? scr(p.x, p.box.top + p.box.h * 0.55) : null,
    }));
  }

  return {
    loadImages: assets.loadImages, resize, render, hit, select, setSelected, debugInfo,
    get selected() { return st.sel; },
    get loaded() { return assets.loaded; },
    get failedImage() { return assets.failImg; },
    scr,
  };
}

export type OfficeView = ReturnType<typeof createOfficeView>;
