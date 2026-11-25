/** risks 뷰 i18n 사전. */

import { registerI18n } from "../../shared/i18n";

export const STR: { ko: Record<string, string>; en: Record<string, string> } = {
  ko: {
    "risks.title": "리스크",
    "risks.desc": "반복 실패·승인 지연·비용 급증 등 라이브 탐지 — 1분마다 갱신",
    "risks.empty": "감지된 리스크 없음",
    "risks.viewRef": "보기",
    "sev.high": "높음",
    "sev.medium": "중간",
    "sev.low": "낮음",
  },
  en: {
    "risks.title": "Risks",
    "risks.desc": "Live detection: repeated failures, stale approvals, cost spikes — refreshes every minute",
    "risks.empty": "No risks detected",
    "risks.viewRef": "View",
    "sev.high": "High",
    "sev.medium": "Medium",
    "sev.low": "Low",
    // 백엔드 detect_risks의 i18n 메타 — ko는 원문 fallback 사용
    "risk.stage_failure.title": "{wf} · {stage} stage repeatedly failing",
    "risk.stage_failure.detail": "Failed in {n} runs over the last 7 days.",
    "risk.stale_approval.title": "Approval pending too long: {wf}",
    "risk.stale_approval.detail": "{task} — waiting for approval for {dur}.",
    "risk.idle_session.title": "Session {health}: {tool} {key}",
    "risk.idle_session.detail": "Status {status} — last event {dur} ago.",
    "risk.cost_spike.title": "LLM cost spike today",
    "risk.cost_spike.detail": "Today's est. cost ${today} — over 3× the 7-day daily avg of ${avg}.",
    "risk.trigger_failure.title": "Trigger error: {id}",
    "risk.trigger_failure.detail": "Error on last trigger — {err}",
    "risk.budget.title": "Monthly budget exceeded",
    "risk.budget.detail": "This month's est. cost ${mtd} > budget ${budget}.",
  },
};

registerI18n(STR);
