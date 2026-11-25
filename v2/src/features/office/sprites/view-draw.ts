/** 순수 캔버스 그리기 도우미 — office-view.ts 에서 분리(코드 이동, 동작 동일).
 * 자세 표(POSES)·걷기 자세 계산·스프라이트 그리기·꼬물거림/폴짝 효과·방향 전환·
 * 둥근 상자·글자색 판정만 담는다. 상태(Life)는 인자로 받고, 스프라이트 칸은
 * view-assets 의 SpriteAssets.cell 을 통해 얻는다(역참조 없음 — 순환 import 없음). */

import { OFFICE_ATLAS } from "./office-atlas";
import type { Box, Person, Walker } from "./types";
import { TINT, type SpriteAssets } from "./view-assets";

const A = OFFICE_ATLAS;

// 자세 이름 → [시트 꼬리, 칸, 원래 보는 쪽(-1 왼쪽·1 오른쪽·0 정면)]
const POSES: Record<string, [string, number, number]> = {
  stand: ["", 0, -1], sitBack: ["", 3, 0], sitFront: ["", 4, 0], mug: ["", 5, 0],
  talk: ["2", 1, 1], laugh: ["2", 2, 0], sip: ["2", 3, 0], wave: ["2", 4, 0], think: ["2", 5, 0],
  phone: ["2", 6, 0], stretch: ["2", 7, 0],
  // 걷기 = walk 시트. 어른 앞걸음 0~7·뒷걸음 8~15(아래 stepF*·stepB*), 서브에이전트 앞(mWalk*)·뒤(mBack*) 16~19
  mWalk: ["walk", 16, -1], mWalk2: ["walk", 17, -1], mBack: ["walk", 18, 1], mBack2: ["walk", 19, 1],
  standBack: ["3", 3, 1],         // 등 보이고 서 있기(서버실 랙·커피 머신 앞)
  cheer: ["3", 5, 0], scratch: ["3", 6, 0], sofaTalk: ["3", 7, 0],
  mStand: ["mini", 0, 0], mBye: ["mini", 3, 0], mJump: ["mini", 4, 0], mSide: ["mini", 5, 1], mBow: ["mini", 6, 0],
  // 휴대폰 만지작 — 서서 스크롤·문자·킥킥, 앉아서(소파 정면) 스크롤·문자·킥킥, 책상 등지고
  phScroll: ["phone", 1, 0], phText: ["phone", 2, 0], phLol: ["phone", 3, 0],
  sitScroll: ["phone", 4, 0], sitText: ["phone", 5, 0], sitBackPhone: ["phone", 6, 0], sitLol: ["phone", 7, 0],
};
// 어른 걷기 = 한 걸음 8장(앞·뒤). 칸 수가 바뀌면 WALK_N 과 시트 칸 번호만 고친다
const WALK_N = 8;
for (let n = 0; n < WALK_N; n++) {
  POSES["stepF" + n] = ["walk", n, -1];
  POSES["stepB" + n] = ["walk", WALK_N + n, 1];
}
// 서브에이전트 종종걸음 한 바퀴(두 걸음 = 걸음 위상 2π)에 돌리는 칸
const MINI_WALK: Record<string, [string, string]> = { front: ["mWalk", "mWalk2"], back: ["mBack", "mBack2"] };

export const STAND = 96;            // 보통 캐릭터 키(사무실 그림 픽셀)
export const KID = 0.7;             // 커밋이 적은 저장소 = 꼬마(70%)
export const standOf = (p: Walker) => STAND * (p.kid ? KID : 1);

export interface SpriteOpts {
  alpha?: number;
  k?: number;
  sx?: number;
  sy?: number;
  tilt?: number;
}

// ---------- 걷기 자세·방향 전환 ----------
export function walkPose(e: Walker, mini: boolean): string {
  const up = (e.vy || 0) < -Math.abs(e.vx || 0) * (mini ? 0.3 : 0.25);
  if (mini) {
    const set = up ? MINI_WALK.back : MINI_WALK.front;
    return set[Math.floor(((e.wp || 0) / (2 * Math.PI / set.length))) % set.length];
  }
  const sp = (e as Person).species || "frog";
  const n = Math.floor((e.dist || 0) / ((A.strides[sp] ?? A.strides.frog) * standOf(e) / A.stand)) % WALK_N;
  return (up ? "stepB" : "stepF") + n;
}

// ---------- 자세 하나를 발끝(x,y) 기준으로 — 돌려주는 값 = 눌림 판정·글씨 자리용 상자(그림 좌표) ----------
export function sprite(assets: SpriteAssets, ctx: CanvasRenderingContext2D, species: string, color: string, pose: string,
                       x: number, y: number, stand: number, face: number, o: SpriteOpts = {}): Box {
  const [tail, i, native] = POSES[pose] || POSES.stand;
  const sheet = species + tail;
  const c = assets.cell(sheet, i, color);
  const k = (stand * (o.k || 1)) / (A.stand * TINT);
  const dw = c.width * k, dh = c.height * k;
  const mirror = native ? face !== native : (!pose.startsWith("sitBack") && face > 0);
  ctx.save();
  ctx.globalAlpha = o.alpha ?? 1;
  ctx.translate(x, y);
  if (o.tilt) ctx.rotate(o.tilt);
  ctx.scale((mirror ? -1 : 1) * (o.sx || 1), o.sy || 1);
  ctx.drawImage(c, -dw / 2, -dh + 2 * TINT * k, dw, dh);
  ctx.restore();
  const h = stand * (o.k || 1) * (A.heights[sheet]?.[i] ?? 1) * (o.sy || 1);
  return { x: x - h * 0.34, y: y - h, w: h * 0.68, h, top: y - h };
}

export function shadow(ctx: CanvasRenderingContext2D, x: number, y: number, w: number) {
  ctx.fillStyle = "rgba(20, 30, 25, .15)";
  ctx.beginPath();
  ctx.ellipse(x, y - 1, w, w * 0.32, 0, 0, Math.PI * 2);
  ctx.fill();
}

// ---------- 꼬물거림 — 같은 자세라도 늘 조금씩 움직인다 ----------
export const jelly = (t: number, speed: number, amt: number) => {
  const w = Math.sin(t * speed);
  return { sx: 1 - w * amt * 0.7, sy: 1 + w * amt };
};
export function hopFx(since: number, h: number): { dy: number; sx: number; sy: number } | null {
  // 폴짝 한 번: 웅크림 → 늘어나며 뜀 → 찌그러지며 착지
  if (since < 0 || since > 0.62) return null;
  if (since < 0.08) return { dy: 0, sx: 1.1, sy: 0.88 };
  if (since < 0.5) { const u = (since - 0.08) / 0.42; return { dy: -Math.sin(u * Math.PI) * h, sx: 0.94, sy: 1.08 }; }
  return { dy: 0, sx: 1.12, sy: 0.86 };
}

// 방향을 바꾸면 '뿅' — 가로가 잠깐 좁아졌다 돌아온다(휙 뒤집히는 대신)
export function turn(e: Walker, face: number, t: number) { if (e.face !== face) { e.face = face; e.turnT = t; } }
export const turnSx = (e: Walker, t: number) =>
  (e.turnT && t - e.turnT < 0.14 ? 0.62 + ((t - e.turnT) / 0.14) * 0.38 : 1);

export function poseOf(p: Person, t: number): string {
  if (p.over && t < p.over.until) return p.over.pose;
  if (p.path.length) {
    if (Math.abs(p.vx || 0) > 0.5) turn(p, (p.vx || 0) > 0 ? 1 : -1, t);
    return walkPose(p, false);
  }
  return p.act?.pose || "stand";
}

// ---------- 글씨 — 이름표(발밑)·말풍선(머리 위) ----------
export function roundBox(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: number) {
  ctx.beginPath(); ctx.roundRect(x + 0.5, y + 0.5, w, h, r);
}
// 색 위에 올릴 글자색 — 밝은 색이면 진한 글자, 어두우면 흰 글자
export const inkOn = (hex: string) => {
  const [r, g, b] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255);
  return 0.2126 * r + 0.7152 * g + 0.0722 * b > 0.55 ? "#1E2622" : "#FFFFFF";
};
