"""LLM 사용량 계측 — LLMClient를 감싸 성공한 호출의 토큰 사용량을 store에 기록.

계측은 보조 기능이므로 store가 없거나 기록이 실패해도 LLM 호출 자체는
절대 깨지지 않도록 예외를 삼키고 경고만 남긴다.
"""

from __future__ import annotations

import logging
from typing import Any, TypeVar

from ..models import ModelConfig

logger = logging.getLogger("jobmate.llm.metering")

T = TypeVar("T")


def _model_label(model: ModelConfig) -> str:
    """기록용 모델 라벨 — codex 등 비기본 provider는 'provider/model' 형태."""
    name = model.model
    provider = getattr(model, "provider", "") or ""
    return f"{provider}/{name}" if provider and provider != "openai_compat" else name


class MeteredLLM:
    """LLMClient 래퍼 — complete() 성공 시 usage를 store.record_llm_usage에 기록."""

    def __init__(self, inner: Any, store: Any | None) -> None:
        self._inner = inner
        self._store = store

    def complete(
        self,
        *,
        model: ModelConfig,
        system: str,
        user: str,
        schema: type[T] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
    ) -> tuple[Any, dict[str, int]]:
        out, usage = self._inner.complete(
            model=model,
            system=system,
            user=user,
            schema=schema,
            tools=tools,
            tool_choice=tool_choice,
        )
        self._record(model, usage)
        return out, usage

    def _record(self, model: ModelConfig, usage: dict[str, int] | None) -> None:
        store = self._store
        if store is None or not usage:
            return
        try:
            store.record_llm_usage(
                _model_label(model),
                int(usage.get("tokens_in") or 0),
                int(usage.get("tokens_out") or 0),
            )
        except Exception as e:
            # 계측 실패가 워크플로를 중단시키면 안 된다
            logger.warning("llm usage record failed model=%s: %s", model.model, str(e)[:200])
