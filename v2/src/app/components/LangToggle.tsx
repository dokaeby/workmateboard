import { useI18n, setLang } from "../../shared/i18n";

export function LangToggle() {
  const { lang, t } = useI18n();
  return (
    <button
      className="rounded-md border border-border px-2 py-1 text-xs text-muted hover:bg-surface-hover"
      onClick={() => setLang(lang === "ko" ? "en" : "ko")}
      aria-label={t("common.language", "Language")}
    >
      {lang === "ko" ? "EN" : "KO"}
    </button>
  );
}
