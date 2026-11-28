/**
 * i18n — 기능 모듈이 register()로 ko/en 사전을 등록하고 useT()로 조회.
 * 언어: localStorage 'jobmate_lang' > navigator.language > 'ko'.
 * 키가 없으면 한국어 원문(컴포넌트에 작성된 기본 텍스트)을 그대로 사용하기 위해
 * t()는 키를 그대로 반환하지 않고 호출부가 fallback을 넘기는 방식.
 */
import { useSyncExternalStore } from "react";

export type Lang = "ko" | "en";
type Dict = Record<string, string>;

const STORE = "jobmate_lang";
const dicts: Record<Lang, Dict> = { ko: {}, en: {} };
/** core 등록(app 셸·shared/ui)이 소유한 키 — feature dict는 덮어쓸 수 없다.
 *  lazy chunk 로드 순서에 따라 전역 키가 바뀌는 것을 막는다. */
const reserved = new Set<string>();
const warned = new Set<string>();
let lang: Lang = detect();
const listeners = new Set<() => void>();

function detect(): Lang {
  try {
    const saved = localStorage.getItem(STORE);
    if (saved === "ko" || saved === "en") return saved;
  } catch { /* noop */ }
  return (navigator.language || "ko").toLowerCase().startsWith("ko") ? "ko" : "en";
}

export function registerI18n(d: { ko?: Dict; en?: Dict }, opts?: { core?: boolean }) {
  for (const l of ["ko", "en"] as const) {
    for (const [k, v] of Object.entries(d[l] || {})) {
      if (!opts?.core && reserved.has(k)) {
        if (!warned.has(k)) {
          warned.add(k);
          console.warn(`[i18n] "${k}"는 core 사전 소유 — feature dict 등록을 무시합니다`);
        }
        continue;
      }
      const prev = dicts[l][k];
      if (prev !== undefined && prev !== v && !warned.has(`${l}:${k}`)) {
        warned.add(`${l}:${k}`);
        console.warn(`[i18n] "${k}" 덮어씀 (${l}): "${prev}" → "${v}"`);
      }
      dicts[l][k] = v;
      if (opts?.core) reserved.add(k);
    }
  }
  emit();
}

export function getLang(): Lang { return lang; }

export function setLang(l: Lang) {
  lang = l;
  try { localStorage.setItem(STORE, l); } catch { /* noop */ }
  emit();
}

function emit() { listeners.forEach((f) => f()); }
function subscribe(f: () => void) { listeners.add(f); return () => listeners.delete(f); }

export type TFn = (key: string, fallback?: string) => string;

/** 등록된 사전 스냅샷 — 테스트/툴링용 (컴포넌트는 useI18n/tr 사용). */
export function i18nDicts(): { ko: Dict; en: Dict } {
  return { ko: { ...dicts.ko }, en: { ...dicts.en } };
}

const warnedMissing = new Set<string>();

/** 비-React 모듈(데이터 합성·에러 메시지 등)용 즉시 번역 — 현재 언어 dict 우선,
 *  없으면 ko → fallback → key. dev 모드에서 양쪽 dict 모두 없는 키는 1회 경고. */
export function tr(key: string, fallback?: string): string {
  const v = dicts[lang][key] ?? dicts.ko[key];
  if (v === undefined) {
    if (
      key &&
      import.meta.env.DEV &&
      import.meta.env.MODE !== "test" &&
      !warnedMissing.has(key)
    ) {
      warnedMissing.add(key);
      console.warn(`[i18n] 누락 키: "${key}" — fallback/키를 그대로 표시합니다`);
    }
    return fallback ?? key;
  }
  return v;
}

/** 기능 모듈용 — 등록된 사전 + 언어를 구독하는 hook. */
export function useI18n() {
  useSyncExternalStore(subscribe, getLang);
  return {
    lang,
    t: (key: string, fallback?: string) => tr(key, fallback),
  };
}

/** t() + {param} 치환 — 백엔드 i18n 메타(title_key/params) 렌더링용. */
export function tx(
  t: TFn,
  key: string | undefined,
  params: Record<string, unknown> | undefined,
  fallback?: string,
): string {
  if (!key) return fallback ?? "";
  let s = t(key, fallback ?? key);
  for (const [k, v] of Object.entries(params || {})) {
    s = s.replaceAll(`{${k}}`, String(v ?? ""));
  }
  return s;
}
