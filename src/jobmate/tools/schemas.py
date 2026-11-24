"""LLM 네이티브 function calling용 도구 스키마 레지스트리.

`register_tool_schema` / `openai_tools` / `has_schema`는 외부 에이전트(MCP 등)와의
크로스 에이전트 계약 — 시그니처를 변경하지 않는다.
"""

from __future__ import annotations

from typing import Any

# name -> {"name", "description", "parameters"(JSON Schema)}
_registry: dict[str, dict[str, Any]] = {}


def register_tool_schema(name: str, description: str, parameters: dict[str, Any]) -> None:
    """도구의 function-calling 스키마를 등록한다. 같은 이름은 덮어쓴다."""
    _registry[name] = {
        "name": name,
        "description": description,
        "parameters": parameters or {"type": "object", "properties": {}},
    }


def has_schema(name: str) -> bool:
    return name in _registry


def openai_tools() -> list[dict[str, Any]]:
    """OpenAI chat/completions `tools` 파라미터 형식으로 전체 스키마를 반환."""
    return [
        {
            "type": "function",
            "function": {
                "name": entry["name"],
                "description": entry["description"],
                "parameters": entry["parameters"],
            },
        }
        for entry in _registry.values()
    ]


def schema_for(name: str) -> dict[str, Any] | None:
    entry = _registry.get(name)
    if entry is None:
        return None
    return {
        "type": "function",
        "function": {
            "name": entry["name"],
            "description": entry["description"],
            "parameters": entry["parameters"],
        },
    }


# ---- 내장 도구 스키마 (ToolExecutor._default_tools와 대응) ----
register_tool_schema(
    "read_file",
    "Read a UTF-8 text file inside the repository (repo-relative path).",
    {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "repo-relative file path"}},
        "required": ["path"],
    },
)
register_tool_schema(
    "run_tests",
    "Run a shell test command inside the repository and return exit code + output.",
    {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "test command (default: pytest -q)",
            }
        },
    },
)
register_tool_schema(
    "read_diff",
    "Return a diff text unchanged for review (no filesystem changes).",
    {
        "type": "object",
        "properties": {"diff_text": {"type": "string", "description": "unified diff text"}},
        "required": ["diff_text"],
    },
)
