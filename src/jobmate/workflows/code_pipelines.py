"""코드 작업 변형 워크플로 — dev_review 파이프라인의 단계 부분집합.

bugfix / refactor / docs_update / dependency_update 모두 analysis→implement→
auto_checks 이후 리뷰 단계만 다르다. implement 단계 key는 "implement"로
고정해 diff 미리보기(/runs/{id}/diff)가 모든 파이프라인에서 동작한다.
"""

from __future__ import annotations

from ..models import StageSpec, WorkflowSpec

_ANALYSIS = StageSpec(
    key="analysis", kind="llm", agent="analysis", description="요구사항·설계 분석"
)
_IMPLEMENT = StageSpec(
    key="implement", kind="llm", agent="implementer", description="변경 제안(diff) 생성"
)
_AUTO_CHECKS = StageSpec(
    key="auto_checks",
    kind="function",
    function="auto_checks",
    description="결정적 자동 검사",
)
_SECURITY = StageSpec(key="security", kind="llm", agent="security", description="보안 검토")
_CODE_REVIEW = StageSpec(key="code_review", kind="llm", agent="reviewer", description="코드 리뷰")
_APPROVAL = StageSpec(key="approval", kind="human", description="사람 승인")

_ANALYSIS_BRANCH = {"done_state": {"BLOCKED": "finalize", "NEEDS_CONTEXT": "finalize"}}
_IMPLEMENT_BRANCH = {
    "done_state": {
        "DONE": "auto_checks",
        "DONE_WITH_CONCERNS": "auto_checks",
        "BLOCKED": "finalize",
        "NEEDS_CONTEXT": "finalize",
    }
}
_AUTO_CHECKS_FAIL = "implement"
_APPROVAL_BRANCH = {"decision": {"approve": "finalize", "reject": "finalize"}}
_SECURITY_BRANCH = {
    "decision": {
        "approve": "code_review",
        "request_changes": "implement",
        "needs_human": "finalize",
    }
}
_CODE_REVIEW_BRANCH = {
    "decision": {"approve": "approval", "request_changes": "implement", "needs_human": "finalize"}
}

BUGFIX_SPEC = WorkflowSpec(
    key="bugfix",
    version="0.1.0",
    description="버그 수정 — 분석→구현→자동검사→보안→승인 (리뷰/QA 생략)",
    entry="analysis",
    stages=[_ANALYSIS, _IMPLEMENT, _AUTO_CHECKS, _SECURITY, _APPROVAL],
    branches={
        "analysis": _ANALYSIS_BRANCH,
        "implement": _IMPLEMENT_BRANCH,
        "auto_checks": {"passed": {"true": "security", "false": _AUTO_CHECKS_FAIL}},
        "security": {
            "decision": {
                "approve": "approval",
                "request_changes": "implement",
                "needs_human": "finalize",
            }
        },
        "approval": _APPROVAL_BRANCH,
    },
    max_review_rounds=2,
    max_implement_rounds=3,
    max_fix_rounds=5,
)

REFACTOR_SPEC = WorkflowSpec(
    key="refactor",
    version="0.1.0",
    description="리팩터링 — 분석→구현→자동검사→코드리뷰→승인 (보안/QA 생략)",
    entry="analysis",
    stages=[_ANALYSIS, _IMPLEMENT, _AUTO_CHECKS, _CODE_REVIEW, _APPROVAL],
    branches={
        "analysis": _ANALYSIS_BRANCH,
        "implement": _IMPLEMENT_BRANCH,
        "auto_checks": {"passed": {"true": "code_review", "false": _AUTO_CHECKS_FAIL}},
        "code_review": _CODE_REVIEW_BRANCH,
        "approval": _APPROVAL_BRANCH,
    },
    max_review_rounds=2,
    max_implement_rounds=3,
    max_fix_rounds=5,
)

DOCS_UPDATE_SPEC = WorkflowSpec(
    key="docs_update",
    version="0.1.0",
    description="문서 갱신 — 분석→구현→자동검사→승인 (리뷰 단계 없음)",
    entry="analysis",
    stages=[_ANALYSIS, _IMPLEMENT, _AUTO_CHECKS, _APPROVAL],
    branches={
        "analysis": _ANALYSIS_BRANCH,
        "implement": _IMPLEMENT_BRANCH,
        "auto_checks": {"passed": {"true": "approval", "false": _AUTO_CHECKS_FAIL}},
        "approval": _APPROVAL_BRANCH,
    },
    max_review_rounds=0,
    max_implement_rounds=3,
    max_fix_rounds=5,
)

DEPENDENCY_UPDATE_SPEC = WorkflowSpec(
    key="dependency_update",
    version="0.1.0",
    description="의존성 업데이트 — 분석→구현→자동검사→보안→코드리뷰→승인 (QA 생략)",
    entry="analysis",
    stages=[_ANALYSIS, _IMPLEMENT, _AUTO_CHECKS, _SECURITY, _CODE_REVIEW, _APPROVAL],
    branches={
        "analysis": _ANALYSIS_BRANCH,
        "implement": _IMPLEMENT_BRANCH,
        "auto_checks": {"passed": {"true": "security", "false": _AUTO_CHECKS_FAIL}},
        "security": _SECURITY_BRANCH,
        "code_review": _CODE_REVIEW_BRANCH,
        "approval": _APPROVAL_BRANCH,
    },
    max_review_rounds=2,
    max_implement_rounds=3,
    max_fix_rounds=5,
)
