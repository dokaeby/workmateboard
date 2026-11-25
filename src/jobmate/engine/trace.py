"""LangSmith 트레이싱 공통 헬퍼.

- LangGraph invoke/resume 에 run 메타데이터(thread_id 외 metadata/tags)를
  실어 LangSmith trace가 jobMate run_id·workflow와 바로 연결되게 한다.
- LangGraph 밖에서 도는 백그라운드 잡(automation tick, gmail 폴링, radar
  일일 수집 등)은 @job_traceable 로 감싸 root trace로 기록한다.
- LANGCHAIN_TRACING_V2/LANGCHAIN_API_KEY 가 없으면 langsmith 측에서
  업로드를 건너뛰므로 호출부는 항상 장식해도 안전하다.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any, TypeVar

from langsmith import traceable

F = TypeVar("F", bound=Callable[..., Any])

# LangSmith로 나가면 안 되는 값 — 키 이름 기준 마스킹.
# (토큰/시크릿류 + 인바운드 메시지 원문: 이메일 본문 등 PII)
_REDACT_KEY_RE = re.compile(
    r"token|secret|password|passwd|api[_-]?key|authorization|credential|"
    r"source_text|message_text|body|content",
    re.IGNORECASE,
)
_REDACTED = "***REDACTED***"


def graph_config(thread_id: str, *, workflow: str, run_id: str | None = None) -> dict[str, Any]:
    """LangGraph invoke/resume용 RunnableConfig — LangSmith metadata/tags 포함."""
    meta: dict[str, Any] = {"jobmate.workflow": workflow}
    tags = ["jobmate", f"workflow:{workflow}"]
    if run_id:
        meta["jobmate.run_id"] = run_id
        tags.append(f"run:{run_id}")
    return {
        "configurable": {"thread_id": thread_id},
        "metadata": meta,
        "tags": tags,
    }


def _safe_value(v: Any, depth: int = 0) -> Any:
    """langsmith 입력/출력 레코딩용 — 직렬화 불가 객체(Store, LLM, adapters)와
    민감 키의 문자열 값을 걸러낸다."""
    if v is None or isinstance(v, (int, float, bool)):
        return v
    if isinstance(v, str):
        return v if len(v) <= 500 else v[:500] + "…"
    if depth >= 2:
        return None
    if isinstance(v, dict):
        return {
            str(k): (_REDACTED if _REDACT_KEY_RE.search(str(k)) else _safe_value(x, depth + 1))
            for k, x in list(v.items())[:50]
        }
    if isinstance(v, (list, tuple)):
        return [_safe_value(x, depth + 1) for x in v[:50]]
    return None


def _process_inputs(inputs: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in inputs.items():
        sv = _safe_value(v)
        if sv is not None:
            out[k] = sv
    return out


def _process_outputs(outputs: Any) -> Any:
    return _safe_value(outputs)


def job_traceable(name: str) -> Callable[[F], F]:
    """백그라운드 잡 함수용 @traceable 래퍼 — 직렬화 불가 인자/민감 값을 자동 필터링."""
    return traceable(  # type: ignore[return-value]
        name=name,
        run_type="chain",
        process_inputs=_process_inputs,
        process_outputs=_process_outputs,
    )
