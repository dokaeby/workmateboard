"""gmail_calendar 워크플로 — 메시지 단위 run. 동일 엔진 stage/run/audit.

calendar-agent 포팅: 멀티채널(gmail/slack/telegram) 공통 경로.
extract → validate → conflict_check → register → notify.
"""

from __future__ import annotations

from ..models import StageSpec, WorkflowSpec

SPEC = WorkflowSpec(
    key="gmail_calendar",
    version="0.2.0",
    description="메일/메시지에서 일정 추출→검증→충돌확인→등록→알림",
    entry="extract",
    stages=[
        StageSpec(
            key="extract",
            kind="llm",
            agent="extractor",
            description="메시지 본문에서 일정 후보 추출",
        ),
        StageSpec(
            key="validate",
            kind="function",
            function="gmail_validate",
            description="결정적 검증(모호/반복/tz)",
        ),
        StageSpec(
            key="conflict_check",
            kind="function",
            function="calendar_conflict_check",
            description="Calendar 중복·충돌 검사",
        ),
        StageSpec(
            key="register",
            kind="function",
            function="gmail_register",
            description="멱등 Calendar 등록",
        ),
        StageSpec(
            key="notify",
            kind="function",
            function="gmail_notify",
            description="알림 큐잉·발송·예약",
        ),
    ],
    branches={
        "extract": {"done_state": {"BLOCKED": "finalize", "NEEDS_CONTEXT": "finalize"}},
        "validate": {
            "status": {
                "validated": "conflict_check",
                "needs_review": "finalize",
                "invalid": "finalize",
            }
        },
        "conflict_check": {
            "status": {
                "clear": "register",
                "self": "register",
                "duplicate": "notify",
                "conflict": "finalize",
                "invalid": "finalize",
            }
        },
        "register": {
            "status": {
                "created": "notify",
                "recovered": "notify",
                "duplicate": "notify",
                "duplicate_pending": "finalize",
                "retry_wait": "finalize",
                "no_adapter": "finalize",
            }
        },
    },
    max_review_rounds=0,
    max_implement_rounds=1,
    max_fix_rounds=0,
)
