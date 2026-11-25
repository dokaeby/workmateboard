/** team 뷰 i18n 사전. */

import { registerI18n } from "../../shared/i18n";

export const STR: { ko: Record<string, string>; en: Record<string, string> } = {
  ko: {
    "team.title": "팀",
    "team.desc": "멤버별 실행·세션 활동 — 60초마다 자동 갱신",
    "team.empty": "팀 멤버가 없습니다",
    "team.email": "이메일",
    "team.name": "이름",
    "team.role": "역할",
    "team.runs": "실행",
    "team.sessions": "세션",
    "team.lastActive": "최근 활동",
    "team.me": "나",
    "team.changeRole": "역할 변경",
    "team.noId": "사용자 ID가 없습니다",
    "team.leadOnly": "이 화면은 리드(lead) 이상 전용입니다.",
  },
  en: {
    "team.title": "Team",
    "team.desc": "Per-member run and session activity — auto-refreshes every 60s",
    "team.empty": "No team members",
    "team.email": "Email",
    "team.name": "Name",
    "team.role": "Role",
    "team.runs": "Runs",
    "team.sessions": "Sessions",
    "team.lastActive": "Last active",
    "team.me": "me",
    "team.changeRole": "Change role",
    "team.noId": "User ID is missing",
    "team.leadOnly": "This page requires a lead role or higher.",
  },
};

registerI18n(STR);
