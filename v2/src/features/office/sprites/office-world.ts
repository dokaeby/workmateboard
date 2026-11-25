/** 사무실 지도 — 레거시 office-world.js 이식.
 * 좌표는 전부 사무실 그림(floor.webp, 1536×1024) 픽셀 기준.
 * 구역: 서재·라운지(유리벽)·탕비실·유리 회의실·서버실·개발석 11자리·안내데스크·출입문.
 * 좌표는 그림 위에 점을 찍어 확대해 보며 하나씩 맞춘 값 — 그림을 바꾸면 여기를 다시 잰다. */

import type { ChatSpot, Desk, HomeSpot, MeetingSpot, Pt, Spot } from "./types";

export const OFFICE_W = 1536;
export const OFFICE_H = 1024;

// 통로 — 캐릭터는 이 점들을 이어 걷는다. 유리벽은 문 자리(lgdoor·mdoor·srvdoor)로만 지난다.
export const OFFICE_MAP_NODES: Record<string, Pt> = {
  out: [380, 830], door: [450, 792], inner: [522, 748],
  la1: [535, 650], la2: [548, 545], la3: [522, 440],
  ta1: [585, 352], ta2: [725, 352], ta3: [872, 366], ta4: [1010, 384], ta5: [1150, 418],
  lgdoor: [541, 378], lounge: [495, 382],                     // 라운지 유리문(탕비실 쪽 유리벽 끝)
  lgS: [420, 398], lgW: [292, 392], rug: [448, 330],          // 라운지 커피테이블을 돌아가는 점
  pan: [758, 300], pan2: [850, 318],                           // 탕비실 — 탁자 사이 빈 바닥
  mfront: [968, 336], mdoor: [976, 299], mtg: [1015, 292], mtgF: [1108, 346], mtgF2: [1212, 362],
  mtgR: [1288, 302],                                           // 회의실 — 탁자 앞쪽으로 돌아 오른쪽 끝까지
  ra1: [1215, 470], ra2: [1190, 570], ra3: [1160, 680], ra4: [1080, 800],
  srvdoor: [1277, 562], srv: [1318, 548],                      // 서버실 유리문(왼쪽 짧은 면)
  A0: [566, 474],                                              // 왼쪽 통로 → A 통로 입구(4번 책상 위를 가로지르지 않게)
  A1: [690, 490], A2: [825, 508], A3: [960, 528], A4: [1100, 545],
  B1: [612, 600], B2: [760, 622], B3: [905, 645], B4: [1060, 668],
  C1: [742, 780], C2: [845, 805], C3: [990, 830],
};

export const OFFICE_MAP_EDGES: [string, string][] = [
  ["out", "door"], ["door", "inner"], ["inner", "la1"], ["la1", "la2"], ["la2", "la3"], ["la3", "ta1"],
  ["ta1", "ta2"], ["ta2", "ta3"], ["ta3", "ta4"], ["ta4", "ta5"], ["ta5", "ra1"], ["ra1", "ra2"],
  ["ra2", "ra3"], ["ra3", "ra4"], ["ta1", "lgdoor"], ["lgdoor", "lounge"], ["lounge", "lgS"],
  ["lgS", "lgW"], ["lounge", "rug"],
  ["ta2", "pan"], ["pan", "pan2"], ["ta3", "pan2"], ["ta3", "mfront"], ["ta4", "mfront"],
  ["mfront", "mdoor"], ["mdoor", "mtg"], ["mtg", "mtgF"], ["mtgF", "mtgF2"], ["mtgF2", "mtgR"], ["ra2", "srvdoor"], ["srvdoor", "srv"],
  ["la3", "A0"], ["A0", "A1"], ["A1", "A2"], ["A2", "A3"], ["A3", "A4"], ["A4", "ra1"], ["A4", "ra2"],
  ["la1", "B1"], ["B1", "B2"], ["B2", "B3"], ["B3", "B4"], ["B4", "ra2"], ["B4", "ra3"],
  ["inner", "C1"], ["C1", "C2"], ["C2", "C3"], ["C3", "ra4"],
];

// 벽 — 바닥에 닿는 선(유리벽·책장). 걷기도, 서로 밀어내기도 이 선을 못 넘는다.
// 문은 선을 끊어 둔 자리다: 라운지 527~553, 회의실 947~1005 사선, 서버실 1252~1302 짧은 면.
// 서재는 사방이 막혀 출입구가 안 보여 들어가지 않는다(안락의자는 장식).
export const OFFICE_WALLS: [Pt, Pt][] = [
  [[290, 463], [553, 390]],      // 라운지 앞 유리벽
  [[413, 262], [527, 365]],      // 라운지↔탕비실 유리벽
  [[287, 575], [363, 637]],      // 서재 유리벽
  [[290, 463], [287, 575]],      // 서재 위 책장 끝
  [[925, 120], [947, 282]],      // 탕비실↔회의실 유리벽
  [[1005, 315], [1250, 460]],    // 회의실 앞 유리벽
  [[1250, 460], [1400, 385]],    // 회의실↔서버실
  [[1250, 460], [1252, 548]],    // 서버실 왼쪽 모서리
  [[1302, 572], [1398, 654]],    // 서버실 앞 유리벽
];

// 가구 — 사람이 뚫고 지나가면 안 되는 덩어리(탕비실 둥근 탁자 둘·라운지 커피테이블·회의 탁자·안내데스크).
function officeRing(cx: number, cy: number, rx: number, ry: number): Pt[] {
  return Array.from({ length: 10 }, (_, i) => [cx + rx * Math.cos((i * Math.PI) / 5), cy + ry * Math.sin((i * Math.PI) / 5)] as Pt);
}

export const OFFICE_BLOCKS: Pt[][] = [
  officeRing(683, 256, 50, 32),                              // 탕비실 탁자 A(+뒤쪽 의자)
  officeRing(847, 220, 50, 32),                              // 탕비실 탁자 B
  officeRing(368, 352, 58, 24),                              // 라운지 커피테이블
  [[1046, 248], [1120, 221], [1252, 300], [1192, 329]],      // 회의 탁자
  [[548, 770], [726, 795], [726, 905], [548, 880]],          // 안내데스크
];

// 바닥 — 이 안에서만 움직인다(밀려도 바닥 밖으로 안 나간다)
export const OFFICE_FLOOR: Pt[] = [
  [200, 300], [413, 250], [515, 258], [800, 232], [925, 250], [1000, 190], [1300, 290], [1400, 385],
  [1500, 585], [845, 966], [447, 850], [355, 845], [335, 785], [365, 640], [287, 575], [290, 463], [180, 430],
];

// 일하는 자리 — 모니터 앞 의자. seat = 앉은 그림의 발끝보다 18 위, 발끝 = 등받이 아래 끝 - 2.
// via = 의자 앞 통로 점, row = 같은 줄(서브에이전트는 오른쪽 옆자리 쪽으로 줄 선다 — 그 자리는 비워 둔다).
export const OFFICE_DESKS: Desk[] = [
  { seat: [657, 429], via: "A1", row: 1 }, { seat: [787, 449], via: "A2", row: 1 },
  { seat: [919, 468], via: "A3", row: 1 }, { seat: [1052, 489], via: "A4", row: 1 },
  { seat: [591, 546], via: "B1", row: 2 }, { seat: [733, 570], via: "B2", row: 2 },
  { seat: [880, 596], via: "B3", row: 2 }, { seat: [1027, 621], via: "B4", row: 2 },
  { seat: [638, 708], via: "C1", row: 3 }, { seat: [788, 739], via: "C2", row: 3 },
  { seat: [940, 768], via: "C3", row: 3 },
];

// 자리가 모자라면 — 회의실 앞줄 의자에 앉아(등을 보이고) 일한다. chair = office-atlas chairs 번호
export const OFFICE_OVERFLOW: Desk[] = [
  { seat: [1062, 274], via: "mtg", chair: 11 }, { seat: [1154, 326], via: "mtgF", chair: 13 },
  { seat: [1109, 300], via: "mtgF", chair: 12 },               // 가운데는 마지막 — 양옆과 등이 겹친다
];

// 쉬는 자리. face = 보는 쪽(-1 왼쪽, 1 오른쪽).
export const OFFICE_SPOTS: Record<string, Spot[]> = {
  sofa: [
    { at: [290, 320], via: "lgW", face: 1 }, { at: [370, 302], via: "rug", face: -1 },
    { at: [232, 386], via: "lgW", face: 1 },                  // 옆 소파는 한 자리만 — 둘이면 겹쳐 보인다
  ],
  coffee: [{ at: [612, 258], via: "ta1", face: -1 }, { at: [565, 274], via: "ta1", face: -1 }],
  cafe: [                                   // 탕비실 둥근 탁자 앞쪽 의자
    { at: [640, 294], via: "ta1", face: 1 }, { at: [700, 304], via: "ta2", face: -1 },
    { at: [800, 254], via: "pan", face: 1 }, { at: [893, 267], via: "pan2", face: -1 },
  ],
  meeting: [{ at: [1062, 274], via: "mtg", face: 1, chair: 11 }, { at: [1154, 326], via: "mtgF", face: 1, chair: 13 }],
                                                              // 이웃 의자는 하나 건너 — 등이 겹치지 않게
  board: [{ at: [1213, 262], via: "mtgR", face: 1 }],   // 탁자 오른쪽 끝을 돌아 화이트보드 앞
  server: [{ at: [1335, 566], via: "srv", face: 1 }],       // 서버실 안, 랙 앞
  stroll: [
    { at: [560, 690], via: "la1" }, { at: [1100, 760], via: "ra4" }, { at: [1205, 650], via: "ra2" },
    { at: [830, 880], via: "C2" }, { at: [1150, 446], via: "ta5" },
  ],
};

// 특별 자리 — Person.home 값으로 고른다. "server" = 서버실 랙 앞에 서서 일하고(등을 보이고),
// 쉴 때도 rest 비율만큼 그 방에 머문다. queue = 서브에이전트가 줄 서는 방향.
export const OFFICE_HOMES: Record<string, HomeSpot> = {
  server: { work: { at: [1335, 566], via: "srv", face: 1 }, rest: 0.75, queue: -1 },
};

// 회의 모임 — 같은 프로젝트(project) 사람들이 잠깐 모여 서는 자리.
export const OFFICE_MEETING: { zone: Pt; spots: MeetingSpot[] } = {
  zone: [1150, 358],
  spots: [
    { at: [1015, 300], via: "mtg" },      // 탁자 왼쪽 끝
    { at: [1060, 330], via: "mtgF" },
    { at: [1108, 352], via: "mtgF" },     // 탁자 앞줄
    { at: [1180, 340], via: "mtgF2" },
    { at: [1212, 366], via: "mtgF2" },
    { at: [1288, 310], via: "mtgR" },     // 오른쪽 끝
    { at: [1230, 350], via: "mtgF2" },
    { at: [1040, 290], via: "mtg" },
  ],
};

// 수다 자리 — 두세 명이 둘러선다. sit = 앉아서 떠드는 자리(탕비실 탁자).
export const OFFICE_CHATS: ChatSpot[] = [
  { pts: [[1158, 454], [1220, 472]], via: "ta5" },
  { pts: [[1110, 700], [1176, 690]], via: "ra3" },
  { pts: [[1236, 600], [1300, 614]], via: "ra2" },
  { pts: [[796, 884], [862, 894]], via: "C2" },
  { pts: [[1075, 332], [1160, 356], [1248, 342]], via: "mtg" },
  { pts: [[640, 294], [700, 304]], via: "ta2", sit: true },
  { pts: [[800, 254], [893, 267]], via: "pan2", sit: true },
  { pts: [[290, 320], [370, 302]], vias: ["lgW", "rug"], sit: true },   // 커피테이블 양쪽으로 돌아 앉는다
];

// 코딩할 때 머리 위로 떠오르는 기호(언어와 무관)
export const OFFICE_CODE = ["{ }", "</>", "( );", "=>", "01", "git", "fn", "#"];

// 프로젝트별 모자·가디건 색 — 이름으로 고정(켜고 꺼도 같은 색). 서로 잘 갈리는 16색.
export const OFFICE_COLORS = [
  "#E74C3C", "#E67E22", "#F1C40F", "#27AE60", "#16A085", "#2E86DE", "#3742FA", "#8E44AD",
  "#E84393", "#8D6E63", "#2C3E50", "#00B8D4", "#7CB342", "#FF7043", "#5F27CD", "#C0392B",
];
