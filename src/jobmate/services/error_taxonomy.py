"""spec 007 US3 — 에러 분류 체계.

stage_runs/runs.error 문자열을 패턴 매칭으로 분류한다. 분류는
store 계층(finish_stage/update_run)이 기록 시점에 자동 적용하고,
분류 컬럼이 없는 레거시 행은 observability/errors 조회 시점에
이 함수로 재분류한다.

클래스 어휘(고정): timeout, rate_limit, auth, tool_error,
schema_parse, budget, unknown.
"""

from __future__ import annotations

ERROR_CLASSES = (
    "timeout",
    "rate_limit",
    "auth",
    "tool_error",
    "schema_parse",
    "budget",
    "unknown",
)

# (error_class, 소문자 부분문자열 패턴들) — 위에서 아래 순서로 첫 매칭.
# budget을 먼저 두는 이유: "tool_call_budget"/"cost_budget" 같은 내부
# 에러가 tool/parse 패턴에 먼저 걸리는 것을 막기 위함.
_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "budget",
        (
            "budget",
            "cost_budget",
            "token_budget",
            "tool_call_budget",
            "time_budget",
            "budget_exceeded",
            "budget_exhausted",
            "insufficient_quota",
        ),
    ),
    (
        "timeout",
        (
            "timeout",
            "timed out",
            "deadline exceeded",
            "stale_timeout",
            "concurrency_timeout",
        ),
    ),
    (
        "rate_limit",
        (
            "429",
            "rate limit",
            "rate_limit",
            "ratelimit",
            "too many requests",
            "throttl",
        ),
    ),
    (
        "auth",
        (
            "401",
            "403",
            "unauthorized",
            "unauthorised",
            "forbidden",
            "authentication",
            "invalid api key",
            "invalid token",
            "api key",
            "access denied",
            "permission denied",
        ),
    ),
    (
        "schema_parse",
        (
            "parse",
            "parsing",
            "json",
            "schema",
            "validation",
            "decode",
            "malformed",
            "invalid response",
            "unexpected token",
        ),
    ),
    (
        "tool_error",
        (
            "tool",
            "tool_call",
            "tool_use",
            "function_call",
        ),
    ),
)


def classify_error(text: str | None) -> str:
    """에러 문자열 → error_class. 매칭 실패/빈 입력은 'unknown'."""
    s = (text or "").lower()
    if not s.strip():
        return "unknown"
    for error_class, needles in _PATTERNS:
        if any(n in s for n in needles):
            return error_class
    return "unknown"
