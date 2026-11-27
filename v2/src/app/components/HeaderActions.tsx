import { apiPost } from "../../shared/api/client";
import { useI18n } from "../../shared/i18n";
import { useAppStore } from "../../shared/store/appStore";
import { PushButton } from "./PushButton";

function LogoutButton() {
  const { t } = useI18n();
  const logout = async () => {
    try { await apiPost("/auth/logout"); } catch { /* 이미 폐기됨 */ }
    try { localStorage.removeItem("jobmate_api_key"); } catch { /* noop */ }
    window.location.href = "/";
  };
  return (
    <button
      className="flex items-center gap-1 rounded-md px-2 py-1.5 text-muted transition-colors hover:bg-surface-hover"
      onClick={logout}
      title={t("hdr.logout", "로그아웃")}
      aria-label={t("hdr.logout", "로그아웃")}
    >
      {/* 로그인(화살표+문)과 구분 — 출구 아이콘 + 텍스트 라벨 병기 */}
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></svg>
      <span className="text-xs font-medium">{t("hdr.logout", "로그아웃")}</span>
    </button>
  );
}

/** 데스크톱 상단바 액션 — 레거시 .header-actions (푸시/설정/로그아웃) 이식. */
export function HeaderActions() {
  const { t } = useI18n();
  const { setView } = useAppStore();
  return (
    <>
      <PushButton />
      <button
        className="rounded-md p-2 text-muted transition-colors hover:bg-surface-hover"
        onClick={() => setView("settings")}
        title={t("hdr.settings", "설정")}
        aria-label={t("hdr.settings", "설정")}
      >
        <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06-.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33h.01a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51h.01a1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82v.01a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
      </button>
      <LogoutButton />
    </>
  );
}
