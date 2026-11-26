/** audit 뷰 i18n 사전. */

import { registerI18n } from "../../shared/i18n";

export const STR: { ko: Record<string, string>; en: Record<string, string> } = {
  ko: {
    "audit.title": "감사 로그",
    "audit.desc": "관리자·권한 변경 등 감사 이벤트, 최신순, 최대 200건",
    "audit.empty": "감사 로그가 없습니다",
    "audit.ts": "시각",
    "audit.actor": "행위자",
    "audit.action": "행동",
    "audit.target": "대상",
    "audit.detail": "상세",
    "audit.adminOnly": "감사 로그는 관리자(admin) 전용입니다.",
  },
  en: {
    "audit.title": "Audit log",
    "audit.desc": "Admin and permission-change audit events, newest first, up to 200",
    "audit.empty": "No audit log entries",
    "audit.ts": "Time",
    "audit.actor": "Actor",
    "audit.action": "Action",
    "audit.target": "Target",
    "audit.detail": "Details",
    "audit.adminOnly": "The audit log is for admins only.",
  },
};

registerI18n(STR);
