/** gmail 뷰 i18n 사전. */

import { registerI18n } from "../../shared/i18n";

export const STR: { ko: Record<string, string>; en: Record<string, string> } = {
  ko: {
    "gmail.title": "Gmail Checker",
    "gmail.desc": "수신함 메시지 요약 및 일정 추출 상태",
    "gmail.summaryBadge": "요약",
    "gmail.noSubject": "(제목 없음)",
    "gmail.retry": "재시도",
    "gmail.extracted": "추출",
    "gmail.pollStatus": "Gmail → Calendar 폴링",
    "gmail.on": "켜짐",
    "gmail.off": "꺼짐",
    "gmail.lastPoll": "마지막 폴링",
    "gmail.digestTitle": "오늘의 요약",
    "gmail.pollNow": "지금 폴링",
    "gmail.polling": "폴링 중…",
    "gmail.autoOn": "자동 폴링 켜기",
    "gmail.autoOff": "자동 폴링 끄기",
    "gmail.empty": "메시지 없음",
  },
  en: {
    "gmail.title": "Gmail Checker",
    "gmail.desc": "Inbox message summaries and calendar extraction status",
    "gmail.summaryBadge": "Summary",
    "gmail.noSubject": "(no subject)",
    "gmail.retry": "Retry",
    "gmail.extracted": "Extracted",
    "gmail.pollStatus": "Gmail → Calendar polling",
    "gmail.on": "On",
    "gmail.off": "Off",
    "gmail.lastPoll": "Last poll",
    "gmail.digestTitle": "Today's digest",
    "gmail.pollNow": "Poll now",
    "gmail.polling": "Polling…",
    "gmail.autoOn": "Turn on auto-polling",
    "gmail.autoOff": "Turn off auto-polling",
    "gmail.empty": "No messages",
  },
};

registerI18n(STR);
