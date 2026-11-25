/** office-life.ts 의 순수 도우미 — 이름 해시로 색·종을 고르고, 무작위 뽑기. */

import * as I18N from "./office-i18n";
import { OFFICE_COLORS } from "./office-world";

export const rnd = (a: number, b: number) => a + Math.random() * (b - a);
export const pick = <T,>(arr: T[]): T => arr[Math.floor(Math.random() * arr.length)];
export const line = (name: string) => pick(I18N.lines(name));            // 지금 언어의 대사 하나

function hash(s: string): number {                       // FNV-1a — 이름이 같으면 색·종이 늘 같다
  let h = 2166136261;
  for (const ch of s) h = Math.imul(h ^ ch.codePointAt(0)!, 16777619);
  return h >>> 0;
}

export const colorOf = (name: string): string => OFFICE_COLORS[hash(name) % OFFICE_COLORS.length];
export const speciesOf = (name: string): string => (hash("종:" + name) % 2 ? "capy" : "frog");
export const shuffle = <T,>(arr: T[]): T[] => arr.map((v) => [Math.random(), v] as [number, T]).sort((a, b) => a[0] - b[0]).map((v) => v[1]);
