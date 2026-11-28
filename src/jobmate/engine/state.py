"""LangGraph 상태 — run 누적 컨텍스트. 모두 JSON 직렬화 가능해야 한다."""

from __future__ import annotations

import time
from typing import Annotated, Any, TypedDict


def _merge_dicts(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    """stage_outputs/stage_attempts용 merge reducer — 키 단위 병합."""
    return {**left, **right}


class HarnessState(TypedDict, total=False):
    run_id: str
    workflow_key: str
    stage_outputs: Annotated[dict[str, dict[str, Any]], _merge_dicts]
    stage_attempts: Annotated[dict[str, int], _merge_dicts]
    # 루프 카운터 (노드 반환값으로만 갱신 — edge 함수는 mutate 금지)
    review_rounds: int
    implement_rounds: int
    fix_rounds: int
    # 예산 누적
    tokens_in: int
    tokens_out: int
    tool_calls: int
    cost_usd: float
    started_at: float
    # 제어 플래그
    escalate: str  # needs_human|budget_exhausted|failed|""
    approval: dict[str, Any]
    final_status: str
    # 메시지 인테이크 컨텍스트 (calendar 계열 워크플로)
    gmail_message_id: str
    connection_id: str
    source_provider: str  # gmail|slack|telegram
    source_id: str
    thread_id: str
    source_url: str
    message_text: str
    # skill_radar 컨텍스트
    platform: str  # claude|chatgpt|gemini|all
    force: bool
    # retry-from-stage (spec 006 US4) — 이 목록의 stage_key는 이미 succeeded
    # 출력이 state.stage_outputs에 시드돼 있으므로 노드 실행을 건너뛴다.
    retry_skip_stages: list[str]


def initial_state(
    run_id: str, workflow_key: str, gmail_ctx: dict[str, Any] | None = None
) -> HarnessState:
    s: HarnessState = {
        "run_id": run_id,
        "workflow_key": workflow_key,
        "stage_outputs": {},
        "stage_attempts": {},
        "review_rounds": 0,
        "implement_rounds": 0,
        "fix_rounds": 0,
        "tokens_in": 0,
        "tokens_out": 0,
        "tool_calls": 0,
        "cost_usd": 0.0,
        "started_at": time.time(),
        "escalate": "",
        # 항상 명시 — 같은 thread 재invoke 시 이전 retry의 skip 목록이
        # 채널에 남아 succeeded 단계를 잘못 건너뛰는 것을 막는다.
        "retry_skip_stages": [],
    }
    if gmail_ctx:
        s.update(gmail_ctx)  # type: ignore[typeddict-item]
    return s
