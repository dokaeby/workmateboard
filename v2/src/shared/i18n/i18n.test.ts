/**
 * i18n 커버리지 테스트 — spec 006 FR-026.
 * 1) 모든 feature/app i18n.ts의 STR: ko 키는 en에도 존재해야 한다
 *    (en-only 키는 백엔드 생성 한국어 제목의 EN 렌더링용 의도적 예외 — inbox.t.* 등).
 * 2) 소스의 리터럴 t("key")/tx(t,"key") 호출은 등록된 사전에 키가 있어야 한다.
 *    (동적 키 `t(`nav.${v}`)`·맵 경유 키는 스캔 대상이 아니다.)
 */
import { describe, expect, it } from "vitest";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { fileURLToPath } from "node:url";
import { registerI18n, i18nDicts, tr, setLang } from "./index";
import { STR as APP_STR } from "../../app/i18n";
import "../ui"; // shared/ui 프리미티브의 core 사전 등록

// feature/*\/i18n.ts + app/i18n.ts + office\/board\/i18n.ts — STR을 export하는 모듈.
// app/i18n.ts는 App.tsx에서 core로 등록하므로 여기서 동일하게 등록한다.
const modules = import.meta.glob<{ STR?: { ko: Record<string, string>; en: Record<string, string> } }>(
  "../../**/i18n.ts",
  { eager: true },
);
for (const m of Object.values(modules)) {
  if (m.STR) registerI18n(m.STR);
}
registerI18n(APP_STR, { core: true });

const SRC = fileURLToPath(new URL("../..", import.meta.url)); // src/

function* walk(dir: string): Generator<string> {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) yield* walk(p);
    else if (/\.(ts|tsx)$/.test(name)) yield p;
  }
}

describe("i18n 사전 무결성", () => {
  it("모든 i18n 모듈의 ko 키는 en 사전에도 존재한다", () => {
    const missing: string[] = [];
    for (const [path, m] of Object.entries(modules)) {
      const str = m.STR;
      if (!str) continue;
      for (const k of Object.keys(str.ko)) {
        if (!(k in str.en)) missing.push(`${path}: ${k}`);
      }
    }
    expect(missing).toEqual([]);
  });

  it("소스의 리터럴 t()/tx() 키는 등록된 사전에 존재한다", () => {
    const { ko, en } = i18nDicts();
    const known = new Set([...Object.keys(ko), ...Object.keys(en)]);
    // t("key" / tx(t, "key" — 앞이 식별자/멤버접근이 아닌 호출만 (I18N.t, spriteT 등 별도 사전 제외)
    const callRe = /(?<![.\w$])(?:t|tx)\(\s*(?:t\s*,\s*)?["']([^"']+)["']/g;
    const missing: string[] = [];
    for (const file of walk(SRC)) {
      const rel = relative(SRC, file);
      if (rel.endsWith("i18n.test.ts")) continue;
      const src = readFileSync(file, "utf8");
      // 스프라이트 사무실은 office-i18n 별도 사전 — registerI18n 키가 아니므로 제외
      if (rel.startsWith("features/office/sprites/")) continue;
      for (const m of src.matchAll(callRe)) {
        if (!known.has(m[1])) missing.push(`${rel}: ${m[1]}`);
      }
    }
    expect([...new Set(missing)]).toEqual([]);
  });

  it("tr()은 누락 키에 ko→fallback 순으로 폴백한다", () => {
    setLang("en");
    expect(tr("__definitely_missing_key__", "FB")).toBe("FB");
    expect(tr("__definitely_missing_key__")).toBe("__definitely_missing_key__");
    // 등록된 키는 언어별 dict 값을 반환
    expect(tr("nav.runs")).not.toBe("nav.runs");
    setLang("ko");
  });
});
