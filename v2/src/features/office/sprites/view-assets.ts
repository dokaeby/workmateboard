/** 스프라이트 시트·이미지 파이프라인 — office-view.ts 에서 분리(코드 이동, 동작 동일).
 * 시트 webp 로드(loadImages), 자세 칸 색칠 캐시(cell), 캔버스 생성(canvasOf)을 소유한다.
 * office-view 의 렌더 상태(sel/view/parts)와는 무관하게 이미지 자산만 다룬다. */

import { OFFICE_ATLAS } from "./office-atlas";

const A = OFFICE_ATLAS;

const SHEETS = ["frog", "frog2", "frog3", "frogmini", "frogwalk", "frogphone",
                "capy", "capy2", "capy3", "capymini", "capywalk", "capyphone"];
export const TINT = 0.6;         // 색칠한 칸 축척 — 쓰는 자세만 그때그때 칠해 메모리를 아낀다

export interface SpriteAssets {
  imgs: Record<string, HTMLImageElement> | null;
  loaded: boolean;
  failImg: boolean;
  cells: Map<string, HTMLCanvasElement>;
  loadImages(): Promise<void>;
  canvasOf(w: number, h: number): HTMLCanvasElement;
  cell(sheet: string, i: number, color: string): HTMLCanvasElement;
}

export function createSpriteAssets(assetBase: string): SpriteAssets {
  const assets: SpriteAssets = {
    imgs: null,
    loaded: false,
    failImg: false,
    cells: new Map<string, HTMLCanvasElement>(),
    loadImages,
    canvasOf,
    cell,
  };

  // ---------- 그림 ----------
  function loadImages(): Promise<void> {
    if (assets.imgs) return Promise.resolve();
    assets.imgs = {};
    const names = ["floor", "chairs", ...SHEETS.flatMap((s) => [s, s + "-key"])];
    let left = names.length;
    return new Promise((resolve) => {
      for (const n of names) {
        const img = new Image();
        img.onload = () => { if (--left === 0) { assets.loaded = true; resolve(); } };
        img.onerror = () => { assets.failImg = true; if (--left === 0) resolve(); };
        img.src = `${assetBase}/${n}.webp`;
        assets.imgs![n] = img;
      }
    });
  }
  function canvasOf(w: number, h: number) {
    const c = document.createElement("canvas"); c.width = w; c.height = h; return c;
  }
  // 자세 한 칸을 프로젝트 색으로 — 마스크에 색을 채워 본판에 곱한다(명암·니트 결은 살고 색만 바뀐다)
  function cell(sheet: string, i: number, color: string): HTMLCanvasElement {
    const key = `${sheet}|${i}|${color}`;
    const hit = assets.cells.get(key);
    if (hit) return hit;
    const w = Math.round(A.cellW * TINT), h = Math.round(A.cellH * TINT);
    const m = canvasOf(w, h), mc = m.getContext("2d")!;
    mc.drawImage(assets.imgs![sheet + "-key"], i * A.cellW, 0, A.cellW, A.cellH, 0, 0, w, h);
    mc.globalCompositeOperation = "source-in";
    mc.fillStyle = color;
    mc.fillRect(0, 0, w, h);
    const c = canvasOf(w, h);
    const cc = c.getContext("2d")!;
    cc.drawImage(assets.imgs![sheet], i * A.cellW, 0, A.cellW, A.cellH, 0, 0, w, h);
    cc.globalCompositeOperation = "multiply";
    cc.drawImage(m, 0, 0);
    assets.cells.set(key, c);
    return c;
  }

  return assets;
}
