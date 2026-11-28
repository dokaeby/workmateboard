import { useI18n, setLang, type Lang } from "../../shared/i18n";
import { apiPatch } from "../../shared/api/client";

// 클릭 순서대로 직렬화 — 빠른 연속 토글 시 마지막 선택이 최종 저장되도록 한다.
// silent401: 의사 인증(API 키/로컬 오픈) 사용자는 /me/*가 401을 반환하므로
// 전역 "세션 만료" 이벤트를 발생시키지 않는다.
let localeQueue: Promise<unknown> = Promise.resolve();
function persistLocale(l: Lang) {
  localeQueue = localeQueue
    .then(() => apiPatch("/me/locale", { locale: l }, { silent401: true }))
    .catch(() => {});
}

export function LangToggle() {
  const { lang, t } = useI18n();
  const toggle = () => {
    const next: Lang = lang === "ko" ? "en" : "ko";
    setLang(next);
    persistLocale(next);
  };
  return (
    <button
      className="rounded-md border border-border px-2 py-1 text-xs text-muted hover:bg-surface-hover"
      onClick={toggle}
      aria-label={t("common.language", "Language")}
    >
      {lang === "ko" ? "EN" : "KO"}
    </button>
  );
}
