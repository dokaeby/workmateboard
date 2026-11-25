"use strict";
// jobMate 공통 i18n — 한국어(ko)·영어(en).
// 언어 우선순위: localStorage 'jobmate_lang' > 브라우저 언어(ko 계열이면 ko, 아니면 en).
// 다른 페이지/파일은 JM_I18N.register({ko:{...}, en:{...}})로 자체 키를 추가한다.
// DOM 적용은 전용 속성만 사용한다 — Skill Radar의 data-i18n과 네임스페이스를 분리하기 위해
// data-jm-i18n(텍스트) / data-jm-i18n-placeholder / data-jm-i18n-title / data-jm-i18n-aria.
// 키가 사전에 없으면 요소를 건드리지 않아 한국어 원문이 그대로 남는다.
window.JM_I18N = (() => {
  const STORE = "jobmate_lang";
  const dicts = { ko: {}, en: {} };
  let lang = "ko";

  const norm = v => {
    const s = String(v || "").toLowerCase();
    return s.startsWith("ko") ? "ko" : s.startsWith("en") ? "en" : "";
  };

  (function detect() {
    let saved = "";
    try { saved = norm(localStorage.getItem(STORE)); } catch { /* 저장소 접근 불가 */ }
    lang = saved || (norm(navigator.language) === "ko" ? "ko" : "en");
  })();

  const has = key =>
    Object.prototype.hasOwnProperty.call(dicts[lang], key) ||
    Object.prototype.hasOwnProperty.call(dicts.ko, key);

  // t(key, ...args) — {0},{1} 위치를 args로 치환. lang → ko → key 순 fallback.
  function t(key, ...args) {
    let s = dicts[lang][key];
    if (s == null) s = dicts.ko[key];
    if (s == null) s = key;
    s = String(s);
    if (args.length) {
      s = s.replace(/\{(\d+)\}/g, (m, i) =>
        args[+i] != null ? String(args[+i]) : m);
    }
    return s;
  }

  function register(dict) {
    if (!dict) return;
    for (const l of ["ko", "en"]) {
      const src = dict[l];
      if (src && typeof src === "object") Object.assign(dicts[l], src);
    }
  }

  function apply(root) {
    const base = root && root.querySelectorAll ? root : document;
    const scope = [base, ...base.querySelectorAll("*")].filter(
      el => el.nodeType === 1);
    for (const el of scope) {
      if (el.hasAttribute("data-jm-i18n")) {
        const k = el.getAttribute("data-jm-i18n");
        if (has(k)) el.textContent = t(k);
      }
      if (el.hasAttribute("data-jm-i18n-placeholder")) {
        const k = el.getAttribute("data-jm-i18n-placeholder");
        if (has(k)) el.setAttribute("placeholder", t(k));
      }
      if (el.hasAttribute("data-jm-i18n-title")) {
        const k = el.getAttribute("data-jm-i18n-title");
        if (has(k)) el.setAttribute("title", t(k));
      }
      if (el.hasAttribute("data-jm-i18n-aria")) {
        const k = el.getAttribute("data-jm-i18n-aria");
        if (has(k)) el.setAttribute("aria-label", t(k));
      }
    }
  }

  function setLang(l) {
    l = norm(l);
    if (!l) return;
    lang = l;
    try { localStorage.setItem(STORE, l); } catch { /* 저장 못 해도 화면은 바뀐다 */ }
    document.documentElement.lang = l;
    apply(document);
    window.dispatchEvent(new CustomEvent("jm-lang-change", { detail: l }));
  }

  document.documentElement.lang = lang;
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => apply(document));
  } else {
    apply(document);
  }

  return { lang: () => lang, t, has, setLang, register, apply };
})();
