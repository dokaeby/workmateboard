"""dev_review 워크플로 — 분석→구현→자동검사→보안검토→코드리뷰→QA→사람승인."""

from __future__ import annotations

from ..models import StageSpec, WorkflowSpec

SPEC = WorkflowSpec(
    key="dev_review",
    version="0.1.0",
    description="요구사항 분석→구현→자동검사→보안→코드리뷰→QA→사람 승인",
    entry="analysis",
    stages=[
        StageSpec(key="analysis", kind="llm", agent="analysis", description="요구사항·설계 분석"),
        StageSpec(
            key="implement", kind="llm", agent="implementer", description="변경 제안(diff) 생성"
        ),
        StageSpec(
            key="auto_checks",
            kind="function",
            function="auto_checks",
            description="결정적 자동 검사",
        ),
        StageSpec(key="security", kind="llm", agent="security", description="보안 검토"),
        StageSpec(key="code_review", kind="llm", agent="reviewer", description="코드 리뷰"),
        StageSpec(key="qa", kind="llm", agent="qa", description="최종 검수"),
        StageSpec(key="approval", kind="human", description="사람 승인"),
    ],
    branches={
        "analysis": {"done_state": {"BLOCKED": "finalize", "NEEDS_CONTEXT": "finalize"}},
        "implement": {
            "done_state": {
                "DONE": "auto_checks",
                "DONE_WITH_CONCERNS": "auto_checks",
                "BLOCKED": "finalize",
                "NEEDS_CONTEXT": "finalize",
            }
        },
        "auto_checks": {"passed": {"true": "security", "false": "implement"}},
        "security": {
            "decision": {
                "approve": "code_review",
                "request_changes": "implement",
                "needs_human": "finalize",
            }
        },
        "code_review": {
            "decision": {"approve": "qa", "request_changes": "implement", "needs_human": "finalize"}
        },
        "qa": {"decision": {"pass": "approval", "fail": "implement", "needs_human": "finalize"}},
        "approval": {"decision": {"approve": "finalize", "reject": "finalize"}},
    },
    max_review_rounds=2,
    max_implement_rounds=3,
    max_fix_rounds=5,
)
