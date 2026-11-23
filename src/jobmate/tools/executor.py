"""도구 실행기 — 에이전트별 allowlist를 서버에서 강제한다."""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger("jobmate.tools")


class ToolDeniedError(Exception):
    pass


class ToolExecutor:
    """allowlist에 있는 도구만 실행. 결과 크기 제한."""

    MAX_RESULT_CHARS = 20_000

    def __init__(
        self, tools: dict[str, Callable[..., Any]] | None = None, repo_root: str | None = None
    ) -> None:
        self._tools = tools or self._default_tools(repo_root)

    @staticmethod
    def _default_tools(repo_root: str | None) -> dict[str, Callable[..., Any]]:
        import os

        root = Path(repo_root or os.getcwd()).resolve()

        def read_file(path: str) -> str:
            p = (root / path).resolve()
            if not p.is_relative_to(root):
                raise ToolDeniedError("path outside repo")
            with open(p, encoding="utf-8", errors="replace") as f:
                return f.read(ToolExecutor.MAX_RESULT_CHARS)

        def run_tests(command: str = "pytest -q") -> dict[str, Any]:
            proc = subprocess.run(  # noqa: S603
                command.split(),
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
