/** developer 뷰 i18n 사전 — /developer/status 연동/인증 구성 상태 문자열. */

import { registerI18n } from "../../shared/i18n";

export const STR: { ko: Record<string, string>; en: Record<string, string> } = {
  ko: {
    "dev.title": "개발자",
    "dev.desc": "연동·인증 구성 상태, 값이 아닌 설정 여부만 표시",
    "dev.integrations": "연동",
    "dev.none": "연동 정보가 없습니다",
    "dev.name": "이름",
    "dev.status": "상태",
    "dev.note": "메모",
    "dev.configured": "설정됨",
    "dev.notConfigured": "미설정",
    "dev.auth": "인증",
    "dev.google": "Google 로그인",
    "dev.password": "비밀번호 로그인",
    "dev.apiKey": "API 키",
    "dev.adminEmails": "관리자 이메일 설정",
    "dev.on": "사용",
    "dev.off": "미사용",
    "dev.generatedAt": "생성 시각",
    "dev.adminOnly": "이 화면은 관리자(admin) 전용입니다.",
  },
  en: {
    "dev.title": "Developer",
    "dev.desc": "Integration and auth configuration status. Shows which items are set, never the values",
    "dev.integrations": "Integrations",
    "dev.none": "No integration info",
    "dev.name": "Name",
    "dev.status": "Status",
    "dev.note": "Note",
    "dev.configured": "Configured",
    "dev.notConfigured": "Not configured",
    "dev.auth": "Auth",
    "dev.google": "Google sign-in",
    "dev.password": "Password sign-in",
    "dev.apiKey": "API key",
    "dev.adminEmails": "Admin emails configured",
    "dev.on": "On",
    "dev.off": "Off",
    "dev.generatedAt": "Generated at",
    "dev.adminOnly": "This view is for admins only.",
  },
};

registerI18n(STR);
