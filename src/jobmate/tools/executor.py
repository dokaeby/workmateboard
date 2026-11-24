"""도구 실행기 — 에이전트별 allowlist를 서버에서 강제한다."""

from __future__ import annotations

import logging
import os
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger("jobmate.tools")

# run_tests는 LLM이 인자를 제어한다 — 임의 명령 실행을 막기 위해
# 테스트/린트/타입검사 명령만 허용한다(셸 메타문자 없이 split() 실행되지만
# `bash -c` 같은 인터프리터 재진입과 `&&`/`|` 연결을 차단해야 한다).
# 추가 명령이 필요하면 JOBMATE_TEST_COMMAND_PREFIXES(쉼표 구분)로
# 접두사를 등록한다 — 셸/인터프리터 바이너리는 어떤 경우에도 허용되지 않는다.
_DEFAULT_TEST_PREFIXES = (
    "pytest",
    "ruff",
    "mypy",
    "python -m pytest",
    "python -m ruff",
    "python -m mypy",
)
_SHELL_METACHARS = re.compile(r"[&|;<>`$()\n]")
# env 확장 접두사 허용 문자 — 공백/하이픈/점/슬래시/영숫자만.
# 셸 메타문자·인용부호는 접두사 자체에도 허용하지 않는다.
_PREFIX_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-/]*(?: [A-Za-z0-9_.\-/]+)*$")


def _allowed_test_prefixes() -> list[str]:
    """기본 접두사 + JOBMATE_TEST_COMMAND_PREFIXES env 확장분을 반환한다."""
    prefixes = list(_DEFAULT_TEST_PREFIXES)
    raw = os.environ.get("JOBMATE_TEST_COMMAND_PREFIXES", "")
    for part in raw.split(","):
        p = " ".join(part.split())
        if p and len(p) <= 80 and _PREFIX_SAFE.match(p):
            prefixes.append(p)
    return prefixes


def _is_allowed_test_command(command: str) -> bool:
    """정규화된 명령이 허용 접두사와 일치하고 메타문자가 없는지 검사한다."""
    if _SHELL_METACHARS.search(command):
        return False
    return any(command == p or command.startswith(p + " ") for p in _allowed_test_prefixes())


class ToolDeniedError(Exception):
    pass


class ToolExecutor:
    """allowlist에 있는 도구만 실행. 결과 크기 제한."""

    MAX_RESULT_CHARS = 20_000

    def __init__(
        self,
        tools: dict[str, Callable[..., Any]] | None = None,
        repo_root: str | None = None,
        extra_tools: dict[str, Callable[..., Any]] | None = None,
    ) -> None:
        # 기본/명시 도구가 extra_tools보다 우선 — 외부 주입이 내장 도구를 덮어쓰지 못한다.
        base = tools or self._default_tools(repo_root)
        merged = dict(extra_tools or {})
        merged.update(base)
        self._tools = merged

    def openai_schemas_for(self, allowed: list[str]) -> list[dict[str, Any]]:
        """에이전트 allowlist와 교집합인 등록 스키마만 OpenAI tools 형식으로 반환.

        스키마가 등록되지 않은 도구는 LLM에 노출되지 않는다.
        """
        from .schemas import openai_tools

        wanted = set(allowed)
        return [spec for spec in openai_tools() if spec.get("function", {}).get("name") in wanted]

    @staticmethod
    def _default_tools(repo_root: str | None) -> dict[str, Callable[..., Any]]:
        root = Path(repo_root or os.getcwd()).resolve()

        def read_file(path: str) -> str:
            p = (root / path).resolve()
            if not p.is_relative_to(root):
                raise ToolDeniedError("path outside repo")
            with open(p, encoding="utf-8", errors="replace") as f:
                return f.read(ToolExecutor.MAX_RESULT_CHARS)

        def run_tests(command: str = "pytest -q") -> dict[str, Any]:
            normalized = " ".join(str(command).split())
            if not _is_allowed_test_command(normalized):
                logger.warning("run_tests denied command: %s", normalized[:80])
                raise ToolDeniedError(
                    "command not allowed — only configured test/lint/typecheck "
                    "command prefixes are permitted"
                )
            proc = subprocess.run(  # noqa: S603
                normalized.split(),
                cwd=root,
                capture_output=True,
                text=True,
                timeout=300,
            )
            out = (proc.stdout + proc.stderr)[: ToolExecutor.MAX_RESULT_CHARS]
            return {"exit_code": proc.returncode, "output": out}

        def read_diff(diff_text: str) -> dict[str, Any]:
            # diff 텍스트를 그대로 검토용으로 반환 (파일시스템 변경 없음)
            return {
                "diff": diff_text[: ToolExecutor.MAX_RESULT_CHARS],
                "lines": diff_text.count("\n"),
            }

        return {"read_file": read_file, "run_tests": run_tests, "read_diff": read_diff}

    def call(self, tool_name: str, allowed: list[str], **kwargs: Any) -> Any:
        if tool_name not in allowed:
            logger.warning("tool denied (not in allowlist): %s", tool_name)
            raise ToolDeniedError(f"tool '{tool_name}' not in allowlist")
        fn = self._tools.get(tool_name)
        if fn is None:
            logger.warning("tool denied (unknown): %s", tool_name)
            raise ToolDeniedError(f"unknown tool '{tool_name}'")
        return fn(**kwargs)
