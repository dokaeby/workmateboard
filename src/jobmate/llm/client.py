"""LLM 클라이언트 — OpenAI 호환 chat/completions + fake 주입."""

from __future__ import annotations

import inspect
import json
import logging
import os
import re
import time
from typing import Any, Protocol, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from ..models import ModelConfig

logger = logging.getLogger("jobmate.llm")

T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    pass


class LLMClient(Protocol):
    """모든 LLM 호출의 경계. fake 구현이 동일 계약을 따른다.

    `tools`/`tool_choice`는 선택적 function-calling 인자다.

    반환 계약 (항상 `(output, usage)` 튜플):
    - `tools` 없이 호출하거나 모델이 일반 응답을 반환하면 기존과 동일 —
      `schema`가 주어지면 검증된 모델/dict, 아니면 JSON 또는 원문 str.
    - `tools`를 전달했고 모델이 tool call을 반환하면 `output`은
      `{"tool_calls": [{"id": str, "name": str, "arguments": dict | str}],
      "content": str | None}` 형태의 dict다. `arguments`는 JSON 문자열이거나
      이미 파싱된 dict일 수 있다. 이 경우 `schema` 검증은 적용되지 않는다
      (tool call 턴은 중간 단계이므로). 호출자는 tool_calls를 실행한 뒤
      결과를 user 프롬프트에 붙여 `complete`를 다시 호출해 최종 응답을 받는다.
    """

    def complete(
        self,
        *,
        model: ModelConfig,
        system: str,
        user: str,
        schema: type[T] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
    ) -> tuple[T | dict[str, Any] | str, dict[str, int]]:
        """반환: (파싱된 출력, usage{tokens_in,tokens_out})."""
        ...


def extract_json(text: str) -> Any:
    """모델 출력에서 JSON 객체/배열을 추출한다. 실패 시 ValueError."""
    # 코드펜스 우선
    m = re.search(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", text, re.S)
    candidates = [m.group(1)] if m else []
    # 첫 { 또는 [ ~ 마지막 } 또는 ]
    start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=-1)
    if start >= 0:
        end_candidates = [i for i in (text.rfind("}"), text.rfind("]")) if i >= 0]
        end = max(end_candidates) if end_candidates else -1
        if end > start:
            candidates.append(text[start : end + 1])
    for cand in candidates:
        try:
            return json.loads(cand)
        except json.JSONDecodeError:
            continue
    raise ValueError("no JSON found in model output")


def _normalize_tool_calls(raw: Any) -> list[dict[str, Any]]:
    """OpenAI message.tool_calls → 표준 {"id","name","arguments"} 형태."""
    calls: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return calls
    for tc in raw:
        if not isinstance(tc, dict):
            continue
        fn_raw = tc.get("function")
        fn: dict[str, Any] = fn_raw if isinstance(fn_raw, dict) else {}
        name = fn.get("name") or tc.get("name")
        if not name:
            continue
        args = fn.get("arguments", tc.get("arguments", {}))
        calls.append(
            {
                "id": str(tc.get("id") or tc.get("call_id") or ""),
                "name": str(name),
                "arguments": args if args is not None else {},
            }
        )
    return calls


class OpenAICompatLLM:
    """OpenAI 호환 chat/completions 클라이언트. httpx + 재시도."""

    def __init__(self, max_retries: int = 2, backoff: float = 1.0) -> None:
        self._max_retries = max_retries
        self._backoff = backoff

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
        api_key = os.environ.get(model.api_key_env, "")
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        payload: dict[str, Any] = {
            "model": model.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": model.temperature,
            "max_tokens": model.max_tokens,
        }
        if schema is not None:
            payload["response_format"] = {"type": "json_object"}
        if tools:
            payload["tools"] = tools
            if tool_choice:
                payload["tool_choice"] = tool_choice
        url = f"{model.base_url.rstrip('/')}/chat/completions"
        last_err: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                with httpx.Client(timeout=model.timeout_seconds) as client:
                    resp = client.post(url, headers=headers, json=payload)
                if resp.status_code in (429, 500, 502, 503, 504) and attempt < self._max_retries:
                    logger.warning(
                        "llm %s attempt %d/%d http %s — retrying",
                        model.model,
                        attempt + 1,
                        self._max_retries + 1,
                        resp.status_code,
                    )
                    time.sleep(self._backoff * (2**attempt))
                    continue
                if resp.status_code >= 400:
                    raise LLMError(f"llm http {resp.status_code}: {resp.text[:300]}")
                resp.raise_for_status()
                data = resp.json()
                message = data["choices"][0]["message"]
                text = message.get("content") or ""
                usage = {
                    "tokens_in": data.get("usage", {}).get("prompt_tokens", 0),
                    "tokens_out": data.get("usage", {}).get("completion_tokens", 0),
                }
                tool_calls = _normalize_tool_calls(message.get("tool_calls"))
                if tool_calls:
                    return {"tool_calls": tool_calls, "content": text or None}, usage
                if schema is not None:
                    parsed = schema.model_validate(extract_json(text))
                    return parsed, usage
                try:
                    return extract_json(text), usage
                except ValueError:
                    return text, usage
            except (httpx.HTTPError, KeyError, IndexError, ValueError, ValidationError) as e:
                last_err = e
                if attempt < self._max_retries:
                    logger.warning(
                        "llm %s attempt %d/%d failed: %s — retrying",
                        model.model,
                        attempt + 1,
                        self._max_retries + 1,
                        str(e)[:200],
                    )
                    time.sleep(self._backoff * (2**attempt))
                    continue
                logger.error(
                    "llm %s failed after %d attempts: %s",
                    model.model,
                    attempt + 1,
                    str(e)[:200],
                )
                raise LLMError(f"llm call failed after {attempt + 1} attempts: {e}") from e
        raise LLMError(f"llm call failed: {last_err}")


class FakeLLM:
    """결정적 테스트용 fake. script: (system,user)->응답 callable 또는 큐.

    handler가 tools/tool_choice 키워드를 받으면 전달한다(미지원 시 생략).
    tool call 턴을 스크립트하려면 handler가
    `{"tool_calls": [{"id","name","arguments"}], "content": ...}` dict를 반환하면
    schema 검증 없이 그대로 전달된다.
    """

    def __init__(self, handler: Any = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._handler = handler or (lambda **kw: {"result": "ok"})
        self._accepts = _accepted_kwargs(self._handler)

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
        call_rec: dict[str, Any] = {
            "model": model.model,
            "system": system[:200],
            "user": user[:2000],
        }
        if tools is not None:
            call_rec["tools"] = [t.get("function", {}).get("name", "?") for t in tools]
        if tool_choice is not None:
            call_rec["tool_choice"] = tool_choice
        self.calls.append(call_rec)
        kwargs: dict[str, Any] = {
            "system": system,
            "user": user,
            "model": model,
            "schema": schema,
        }
        if "tools" in self._accepts:
            kwargs["tools"] = tools
        if "tool_choice" in self._accepts:
            kwargs["tool_choice"] = tool_choice
        out = self._handler(**kwargs)
        result: Any
        if isinstance(out, dict) and "tool_calls" in out:
            # tool call 턴 — schema 검증 없이 그대로 반환 (계약상 dict)
            result = out
        elif tools is not None and isinstance(out, str):
            try:
                parsed = extract_json(out)
            except ValueError:
                parsed = None
            if isinstance(parsed, dict) and "tool_calls" in parsed:
                result = parsed
            elif isinstance(out, BaseModel):
                result = out
            elif schema is not None and isinstance(out, dict):
                result = schema.model_validate(out)
            elif schema is not None and isinstance(out, str):
                result = schema.model_validate(extract_json(out))
            else:
                result = out
        elif isinstance(out, BaseModel):
            result = out
        elif schema is not None and isinstance(out, dict):
            result = schema.model_validate(out)
        elif schema is not None and isinstance(out, str):
            result = schema.model_validate(extract_json(out))
        else:
            result = out
        return result, {"tokens_in": 10, "tokens_out": 10}


def _accepted_kwargs(fn: Any) -> set[str]:
    """handler가 받는 키워드 인자 이름 집합 (**kwargs 포함 시 전체 허용)."""
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return set()
    names = {p.name for p in params}
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params):
        return {"system", "user", "model", "schema", "tools", "tool_choice"}
    return names
