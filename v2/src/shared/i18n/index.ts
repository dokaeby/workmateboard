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
let lang: Lang = detect();
const listeners = new Set<() => void>();

function detect(): Lang {
  try {
    const saved = localStorage.getItem(STORE);
    if (saved === "ko" || saved === "en") return saved;
  } catch { /* noop */ }
  return (navigator.language || "ko").toLowerCase().startsWith("ko") ? "ko" : "en";
}

export function registerI18n(d: { ko?: Dict; en?: Dict }) {
  Object.assign(dicts.ko, d.ko || {});
  Object.assign(dicts.en, d.en || {});
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

/** 기능 모듈용 — 등록된 사전 + 언어를 구독하는 hook. */
export function useI18n() {
  useSyncExternalStore(subscribe, getLang);
  return {
    lang,
    t: (key: string, fallback?: string) => dicts[lang][key] ?? dicts.ko[key] ?? fallback ?? key,
  };
}
