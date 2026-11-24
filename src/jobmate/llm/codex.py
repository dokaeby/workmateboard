"""Codex(ChatGPT 구독) Responses API 클라이언트.

~/.codex/auth.json의 OAuth 토큰으로 chatgpt.com/backend-api/codex/responses를
호출한다. 이 엔드포인트는 stream=true가 필수이며 SSE로 응답한다.
토큰이 만료되면 auth.openai.com/oauth/token으로 갱신을 시도하고,
실패하면 `codex login` 안내 오류를 발생시킨다.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from ..models import ModelConfig
from .client import LLMError, extract_json

logger = logging.getLogger("jobmate.llm.codex")

T = TypeVar("T", bound=BaseModel)

OAUTH_ENDPOINT = "https://auth.openai.com/oauth/token"
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"


class CodexAuth:
    """~/.codex/auth.json 로드 + 필요 시 refresh_token 갱신."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path).expanduser()
        self._data: dict[str, Any] | None = None

    def _load(self) -> dict[str, Any]:
        if self._data is None:
            try:
                self._data = json.loads(self._path.read_text())
            except (OSError, json.JSONDecodeError) as e:
                raise LLMError(
                    f"codex auth not readable at {self._path}: {e}. Run `codex login` first."
                ) from e
        return self._data

    @staticmethod
    def _jwt_exp(token: str) -> float:
        try:
            payload = token.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            return float(json.loads(base64.urlsafe_b64decode(payload))["exp"])
        except (IndexError, KeyError, ValueError) as e:
            raise LLMError("codex access_token is not a valid JWT") from e

    def _refresh(self, data: dict[str, Any]) -> None:
        tokens = data.get("tokens") or {}
        refresh_token = tokens.get("refresh_token")
        if not refresh_token:
            raise LLMError("codex refresh_token missing; run `codex login`")
        resp = httpx.post(
            OAUTH_ENDPOINT,
            json={
                "client_id": CODEX_CLIENT_ID,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            timeout=30,
        )
        if resp.status_code != 200:
            logger.warning("codex token refresh failed: status=%s", resp.status_code)
            raise LLMError(f"codex token refresh failed ({resp.status_code}); run `codex login`")
        new = resp.json()
        tokens["access_token"] = new["access_token"]
        if new.get("id_token"):
            tokens["id_token"] = new["id_token"]
        if new.get("refresh_token"):
            tokens["refresh_token"] = new["refresh_token"]
        data["last_refresh"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self._path.write_text(json.dumps(data))
        self._path.chmod(0o600)
        logger.info("codex access token refreshed")

    def headers(self) -> dict[str, str]:
        data = self._load()
        tokens = data.get("tokens") or {}
        access = tokens.get("access_token", "")
        account_id = tokens.get("account_id", "")
        if not access or self._jwt_exp(access) < time.time() + 60:
            self._refresh(data)
            tokens = data["tokens"]
            access = tokens["access_token"]
        return {
            "Authorization": f"Bearer {access}",
            "chatgpt-account-id": account_id,
            "OpenAI-Beta": "responses=experimental",
            "originator": "codex_cli_rs",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }


def iter_sse_data(lines: Any) -> Any:
    """SSE 스트림에서 data: 라인의 JSON 페이로드를 순서대로 yield한다."""
    for line in lines:
        if not line.startswith("data:"):
            continue
        try:
            yield json.loads(line[5:].strip())
        except json.JSONDecodeError:
            continue


class CodexResponsesLLM:
    """Codex 구독 Responses API. LLMClient 프로토콜 구현."""

    def __init__(
        self, auth_path: Path | None = None, max_retries: int = 2, backoff: float = 1.0
    ) -> None:
        self._auth = CodexAuth(
            auth_path or Path(os.environ.get("JOBMATE_CODEX_AUTH_PATH", "~/.codex/auth.json"))
        )
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
        payload: dict[str, Any] = {
            "model": model.model,
            "instructions": system,
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": user}],
                }
            ],
            "store": False,
            "stream": True,
            "max_output_tokens": model.max_tokens,
        }
        if tools:
            # Responses API는 chat/completions와 달리 function 스키마가 최상위 평탄화된다.
            payload["tools"] = [
                {
                    "type": "function",
                    "name": t.get("function", {}).get("name", ""),
                    "description": t.get("function", {}).get("description", ""),
                    "parameters": t.get("function", {}).get("parameters", {}),
                }
                for t in tools
            ]
            if tool_choice:
                payload["tool_choice"] = tool_choice
        url = f"{model.base_url.rstrip('/')}/responses"
        last_err: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                text, usage, calls = self._stream(url, payload, model)
                if calls:
                    return {
                        "tool_calls": [
                            {
                                "id": c.get("id", ""),
                                "name": c.get("name", ""),
                                "arguments": c.get("arguments", {}),
                            }
                            for c in calls
                        ],
                        "content": text or None,
                    }, usage
                if schema is not None:
                    return schema.model_validate(extract_json(text)), usage
                try:
                    return extract_json(text), usage
                except ValueError:
                    return text, usage
            except (httpx.HTTPError, KeyError, IndexError, ValueError, ValidationError) as e:
                last_err = e
                if attempt < self._max_retries:
                    logger.warning(
                        "codex llm %s attempt %d/%d failed: %s — retrying",
                        model.model,
                        attempt + 1,
                        self._max_retries + 1,
                        str(e)[:200],
                    )
                    time.sleep(self._backoff * (2**attempt))
                    continue
                logger.error(
                    "codex llm %s failed after %d attempts: %s",
                    model.model,
                    attempt + 1,
                    str(e)[:200],
                )
                raise LLMError(f"codex llm call failed after {attempt + 1} attempts: {e}") from e
        raise LLMError(f"codex llm call failed: {last_err}")

    def _stream(
        self, url: str, payload: dict[str, Any], model: ModelConfig
    ) -> tuple[str, dict[str, int], list[dict[str, Any]]]:
        """SSE 스트림 수집. 반환: (text, usage, function_calls)."""
        parts: list[str] = []
        calls: list[dict[str, Any]] = []
        usage = {"tokens_in": 0, "tokens_out": 0}
        with httpx.Client(timeout=model.timeout_seconds) as client:
            with client.stream("POST", url, headers=self._auth.headers(), json=payload) as resp:
                if resp.status_code in (429, 500, 502, 503, 504):
                    logger.warning(
                        "codex llm %s http %s — retryable",
                        model.model,
                        resp.status_code,
                    )
                    raise httpx.HTTPStatusError(
                        f"retryable {resp.status_code}", request=resp.request, response=resp
                    )
                resp.raise_for_status()
                for ev in iter_sse_data(resp.iter_lines()):
                    etype = ev.get("type", "")
                    if etype == "response.output_text.delta":
                        parts.append(ev.get("delta", ""))
                    elif etype == "response.output_item.done":
                        item = ev.get("item") or {}
                        if item.get("type") == "function_call":
                            calls.append(
                                {
                                    "id": item.get("call_id") or item.get("id") or "",
                                    "name": item.get("name", ""),
                                    "arguments": item.get("arguments", {}),
                                }
                            )
                    elif etype == "response.completed":
                        u = (ev.get("response") or {}).get("usage") or {}
                        usage = {
                            "tokens_in": u.get("input_tokens", 0),
                            "tokens_out": u.get("output_tokens", 0),
                        }
                    elif etype in ("response.failed", "error"):
                        raise LLMError(f"codex stream error: {json.dumps(ev)[:300]}")
        return "".join(parts), usage, calls


class CompositeLLM:
    """ModelConfig.provider에 따라 openai_compat/codex 클라이언트로 디스패치."""

    def __init__(self, compat: Any | None = None, codex: Any | None = None) -> None:
        from .client import OpenAICompatLLM

        self._compat = compat or OpenAICompatLLM()
        self._codex = codex or CodexResponsesLLM()
        self._impl = self._maybe_traced()

    @staticmethod
    def _tracing_enabled() -> bool:
        return os.environ.get("LANGCHAIN_TRACING_V2") == "true" and bool(
            os.environ.get("LANGCHAIN_API_KEY")
        )

    def _maybe_traced(self) -> Any:
        if not self._tracing_enabled():
            return self._complete
        try:
            from langsmith import traceable
        except ImportError:
            return self._complete
        return traceable(name="jobmate.llm.complete", run_type="llm")(self._complete)

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
        return self._impl(
            model=model,
            system=system,
            user=user,
            schema=schema,
            tools=tools,
            tool_choice=tool_choice,
        )

    def _complete(
        self,
        *,
        model: ModelConfig,
        system: str,
        user: str,
        schema: type[T] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
    ) -> tuple[Any, dict[str, int]]:
        client = self._codex if model.provider == "codex" else self._compat
        return client.complete(
            model=model,
            system=system,
            user=user,
            schema=schema,
            tools=tools,
            tool_choice=tool_choice,
        )
