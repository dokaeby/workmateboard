/** system 뷰 i18n 사전. */

import { registerI18n } from "../../shared/i18n";

export const STR: { ko: Record<string, string>; en: Record<string, string> } = {
  ko: {
    "sys.title": "시스템",
    "sys.desc": "API·DB·브리지·cron 상태 — 30초마다 자동 갱신",
    "sys.overview": "개요",
    "sys.uptime": "API 가동시간",
    "sys.version": "버전",
    "sys.cronTick": "크론 마지막 틱",
    "sys.reaper": "리퍼",
    "sys.machines": "브리지 머신",
    "sys.lastPush": "브리지 마지막 push",
    "sys.lastPushShort": "마지막 push",
    "sys.stale": "10분+ 지연",
    "sys.ok": "정상",
    "sys.sessions": "세션 수",
    "sys.adminOnly": "이 화면은 관리자(admin) 전용입니다.",
    "sys.unit.s": "초",
    "sys.unit.m": "분",
    "sys.unit.h": "시간",
    "sys.unit.d": "일",
  },
  en: {
    "sys.title": "System",
    "sys.desc": "API, DB, bridge, and cron status — auto-refreshes every 30s",
    "sys.overview": "Overview",
    "sys.uptime": "API uptime",
    "sys.version": "Version",
    "sys.cronTick": "Cron last tick",
    "sys.reaper": "Reaper",
    "sys.machines": "Bridge machines",
    "sys.lastPush": "Bridge last push",
    "sys.lastPushShort": "Last push",
    "sys.stale": "stale 10m+",
    "sys.ok": "OK",
    "sys.sessions": "Sessions",
    "sys.adminOnly": "This page is for admins only.",
    "sys.unit.s": "s",
    "sys.unit.m": "m",
    "sys.unit.h": "h",
    "sys.unit.d": "d",
  },
};

registerI18n(STR);
