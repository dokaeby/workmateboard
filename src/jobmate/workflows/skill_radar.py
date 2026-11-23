"""skill_radar 워크플로 — GitHub skill/plugin 저장소 일일 수집·요약·발행.

skillRadar 포팅: collect → dedupe → summarize(LLM, 캐시) → rank → 사람승인 → publish.
"""

from __future__ import annotations

from ..models import StageSpec, WorkflowSpec

SPEC = WorkflowSpec(
    key="skill_radar",
    version="0.2.0",
    description="GitHub skill/plugin 저장소 일일 수집·요약·랭킹·승인·발행",
    entry="collect",
    stages=[
        StageSpec(
            key="collect",
            kind="function",
            function="radar_collect",
            description="GitHub search + README 수집",
        ),
        StageSpec(
            key="dedupe",
            kind="function",
            function="radar_dedupe",
            description="archived/fork/중복 제거",
        ),
        StageSpec(
            key="summarize",
            kind="function",
            function="radar_summarize",
            description="sanitize+분류+3개 언어 요약 (content_hash 캐시)",
        ),
        StageSpec(
            key="verify",
            kind="function",
            function="radar_verify",
            description="요약 충실도 judge 검증 (실패 시 재요약)",
        ),
        StageSpec(
            key="rank",
            kind="function",
            function="radar_rank",
            description="점수화·정렬·증가 star 계산",
        ),
        StageSpec(key="approve", kind="human", description="스냅샷 발행 승인"),
        StageSpec(
            key="publish",
            kind="function",
            function="radar_publish",
            description="플랫폼별 snapshot 저장 + 알림",
        ),
    ],
    branches={
        "approve": {"decision": {"approve": "publish", "reject": "finalize"}},
        "publish": {"status": {"published": "finalize", "rejected": "finalize"}},
    },
    max_review_rounds=0,
    max_implement_rounds=1,
    max_fix_rounds=0,
)
