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
# env로 추가되는 접두사에서 금지하는 셸/인터프리터 basename — 첫 토큰 기준.
# (`python -m pytest` 같은 모듈 호출은 아래 _PY_INTERPRETER 예외로 허용)
_SHELL_BINARIES = frozenset(
    {
        # 셸·인터프리터
        "sh", "bash", "zsh", "fish", "dash", "ksh", "csh", "tcsh",
        "node", "nodejs", "deno", "bun", "bunx", "perl", "ruby", "php", "lua",
        "pythonw", "pypy", "pypy3", "jython", "micropython", "osascript",
        # 셸/프로세스 재진입 래퍼
        "env", "xargs", "find", "awk", "gawk", "sed", "nohup", "setsid",
        "timeout", "watch", "parallel", "busybox", "command", "builtin",
        "exec", "eval", "source", "sudo", "doas", "su", "runuser", "pkexec",
        "chroot", "unshare", "nsenter", "capsh", "setpriv", "nice", "ionice",
        "taskset", "chrt", "stdbuf", "flock", "daemon",
        # 네트워크/원격 실행
        "nc", "ncat", "netcat", "socat", "curl", "wget", "aria2c", "http",
        "https", "ssh", "autossh", "mosh", "telnet", "ftp", "sftp", "scp",
        "rsync", "kubectl", "helm", "terraform", "ansible", "vault",
        # 패키지 러너/빌드 도구 — 내부에서 임의 코드 실행 가능
        "make", "cmake", "ninja", "meson", "tox", "nox", "hatch", "pdm",
        "poetry", "pipenv", "rye", "uv", "uvx", "pip", "pip3", "npm", "npx",
        "pnpm", "yarn", "cargo", "rustc", "go", "gcc", "g++", "clang", "cc",
        "docker", "podman", "nerdctl", "buildah",
        # 디버거·에디터(TUI 셸 이스케이프)
        "gdb", "lldb", "strace", "dtruss", "dtrace", "vim", "nvim", "vi",
        "emacs", "nano", "joe", "ed", "ex", "less", "more", "man", "tmux",
        "screen", "byobu", "zellij",
        # OS 셸
        "cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh",
    }
)  # fmt: skip
_PY_INTERPRETER = re.compile(r"^py(thon)?[0-9.]*$")
_ALLOWED_MODULES = frozenset({"pytest", "ruff", "mypy"})
# env 확장 접두사 허용 문자 — 공백/하이픈/점/슬래시/영숫자만.
# 셸 메타문자·인용부호는 접두사 자체에도 허용하지 않는다.
_PREFIX_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-/]*(?: [A-Za-z0-9_.\-/]+)*$")

# run_tests 인자 정책 — 허용 기반(allowlist).
# LLM이 인자를 제어하므로 argparse 유일접두사 축약(`--junitx=`→`--junitxml`)과
# 미등록 옵션(`--log-file`, `--html-report`, `ruff format`, `--fix` 등)을
# 모두 차단해야 한다. 따라서:
#   - pytest: 경로 인자 + 명시 허용 플래그만 통과. 나머지 `-` 토큰은 전부 거부.
#   - ruff: `check` 서브커맨드만 허용 + 파일쓰기/수정 옵션 denylist.
#   - mypy: 파일쓰기/코드로드/설정주입 옵션 denylist.
#   - env 확장 접두사(알 수 없는 명령): 기존 denylist + 경로 이탈 검사만 적용.
# deny 판정은 옵션 토큰의 `=` 앞부분(base)에 대해 deny와의 접두사 관계를
# 양방향으로 검사한다 — tok이 deny의 축약이거나 deny가 tok의 확장인 경우 차단.

# pytest 허용 장문 옵션 (`--opt` 또는 `--opt=value` 형태만 인정).
# 출력 파일/플러그인 로드/설정 주입/코드 임포트 계열은 전부 미포함 → 자동 거부.
_PYTEST_ALLOWED_LONG_OPTS = frozenset(
    {
        "--tb",
        "--maxfail",
        "--disable-warnings",
        "--durations",
        "--strict-markers",
        "--strict-config",
        "--lf",
        "--ff",
        "--collect-only",
        "--continue-on-collection-errors",
        "--color",
        "--code-highlight",
        "--capture",
        "--deselect",
        "--ignore",
        "--ignore-glob",
        "--exitfirst",
        "--setup-show",
        "--markers",
        "--fixtures",
        "--version",
        "--help",
    }
)
# pytest 단축 옵션: 값을 받는 것(-k/-m/-r/-l)과 플래그 묶음(qvsx)만 허용.
# -p(플러그인 로드)/-c(설정 파일)/-o(ini override)는 어떤 형태든 거부.
_PYTEST_VALUE_SHORTS = ("-k", "-m", "-r", "-l")
_PYTEST_FLAG_SHORTS_RE = re.compile(r"-[qvsx]+")

# ruff/mypy도 pytest와 동일하게 허용 플래그 allowlist로 관리한다.
# denylist는 새 옵션(리포트/코드로드/실행 계열)이 추가될 때마다 우회가
# 생기므로, 명시 허용 목록에 없는 `-` 토큰은 전부 거부한다.
# 파일 쓰기(--output-file/--*-report/--cache-dir/--config)·코드 로드
# (--shadow-file/--custom-typeshed/--python-executable)·패키지 설치
# (--install-types)·소스 수정(--fix/--add-noqa) 옵션은 목록에 없으므로
# `=` 결합·단축 결합 등 어떤 형태로 와도 차단된다.
_RUFF_ALLOWED_LONG_OPTS = frozenset(
    {
        "--select",
        "--ignore",
        "--extend-select",
        "--extend-ignore",
        "--per-file-ignores",
        "--output-format",
        "--statistics",
        "--show-source",
        "--diff",
        "--no-cache",
        "--isolated",
        "--respect-gitignore",
        "--force-exclude",
        "--preview",
        "--target-version",
        "--line-length",
        "--exit-zero",
        "--show-settings",
        "--exclude",
        "--extend-exclude",
    }
)
# ruff 단축 옵션 — 출력/수정 계열이 없으므로 플래그 묶음만 허용.
_RUFF_FLAG_SHORTS_RE = re.compile(r"-[vqVh]+")

_MYPY_ALLOWED_LONG_OPTS = frozenset(
    {
        # 분석 동작 플래그
        "--strict",
        "--ignore-missing-imports",
        "--check-untyped-defs",
        "--disallow-untyped-defs",
        "--disallow-incomplete-defs",
        "--disallow-untyped-calls",
        "--disallow-untyped-decorators",
        "--disallow-any-generics",
        "--disallow-any-unimported",
        "--disallow-any-expr",
        "--disallow-any-decorated",
        "--disallow-any-explicit",
        "--disallow-subclassing-any",
        "--warn-redundant-casts",
        "--warn-unused-ignores",
        "--warn-return-any",
        "--warn-unreachable",
        "--strict-equality",
        "--strict-concatenate",
        "--strict-bytes",
        "--extra-checks",
        "--show-error-codes",
        "--show-column-numbers",
        "--show-error-end",
        "--show-error-context",
        "--hide-error-context",
        "--show-absolute-path",
        "--show-traceback",
        "--no-error-summary",
        "--pretty",
        "--no-pretty",
        "--no-color-output",
        "--no-implicit-optional",
        "--explicit-package-bases",
        "--namespace-packages",
        "--fast-module-lookup",
        "--fast-deps",
        "--skip-version-check",
        "--no-incremental",
        "--no-site-packages",
        "--no-silence-site-packages",
        "--allow-redefinition",
        "--local-partial-types",
        # 값 옵션 — 분석 범위/에러 코드만 바꾸며 파일 쓰기·코드 실행 없음
        "--enable-error-code",
        "--disable-error-code",
        "--enable-incomplete-feature",
        "--follow-imports",
        "--follow-imports-for-stubs",
        "--python-version",
        "--platform",
        "--always-true",
        "--always-false",
        "--exclude",
    }
)
# mypy 단축 옵션 — -c(프로그램 텍스트)/-m/-p(모듈·패키지)는 제외.
_MYPY_FLAG_SHORTS_RE = re.compile(r"-[hV]+")

# env 확장 등 미등록 접두사용 명령에 적용하는 기존 denylist(보수적 유지).
_GENERIC_DENIED_LONG_OPTS = (
    "--junitxml",
    "--basetemp",
    "--baselinetemp",
    "--rootdir",
    "--confcutdir",
    "--noconftest",
    "--cov-report",
    "--cov-config",
    "--resultlog",
    "--output-file",
    "--config",
    "--config-file",
    "--log-file",
    "--html",
    "--self-contained-html",
    "--override-ini",
    "--doctest-modules",
    "--pastebin",
    "--cache-dir",
)
_GENERIC_DENIED_SHORT_OPTS = ("-p", "-c", "-o")


def _path_arg_safe(tok: str) -> bool:
    """위치 인자/옵션 값이 repo 밖을 가리키지 않는지 검사 (절대경로·.. 이탈 차단)."""
    if tok.startswith(("/", "~")):
        return False
    return ".." not in tok.split("/")


def _long_opt_denied(tok: str, denied: tuple[str, ...]) -> bool:
    """`=` 앞부분(base)과 deny 목록의 접두사 관계를 양방향 검사한다."""
    if not tok.startswith("--") or tok == "--":
        return False
    base = tok.split("=", 1)[0]
    return any(base == d or base.startswith(d) or d.startswith(base) for d in denied)


def _short_opt_denied(tok: str, denied: tuple[str, ...]) -> bool:
    """단축 옵션 deny — `-p`, `-pfoo`, `-cFILE` 등 붙여쓴 형태도 차단."""
    return any(tok.startswith(o) and (len(tok) == 2 or tok[2] not in "-=") for o in denied)


def _pytest_arg_ok(tok: str) -> bool:
    if not tok.startswith("-") or tok == "-":
        return _path_arg_safe(tok)
    if tok == "--":
        return True
    if tok.startswith("--"):
        # 허용 목록에 없는 장문 옵션(축약형 포함)은 전부 거부
        return tok.split("=", 1)[0] in _PYTEST_ALLOWED_LONG_OPTS
    # 단축 옵션 — -p/-c/-o 계열은 붙여쓰기 포함 전면 거부
    if _short_opt_denied(tok, ("-p", "-c", "-o")):
        return False
    for pre in _PYTEST_VALUE_SHORTS:
        if tok == pre or (tok.startswith(pre) and len(tok) > 2 and tok[2] != "-"):
            return True
    return _PYTEST_FLAG_SHORTS_RE.fullmatch(tok) is not None


def _denylist_args_ok(
    tokens: list[str], long_denied: tuple[str, ...], short_denied: tuple[str, ...]
) -> bool:
    for tok in tokens:
        if tok == "--":
            continue
        if tok.startswith("--"):
            if _long_opt_denied(tok, long_denied):
                return False
        elif tok.startswith("-") and tok != "-":
            if _short_opt_denied(tok, short_denied):
                return False
        elif not _path_arg_safe(tok):
            return False
    return True


def _allowlist_args_ok(
    tokens: list[str], allowed_long: frozenset[str], flag_shorts: re.Pattern[str]
) -> bool:
    """허용 플래그 allowlist 검사 — pytest와 동일 정책.

    `--opt=value`는 base가 허용 목록에 있을 때만 통과(축약·미등록 전부 거부).
    단축 옵션은 flag_shorts 정규식에 맞는 플래그 묶음만 허용.
    그 외 `-` 토큰과 `--` 이후를 포함한 모든 토큰은 위치 인자로 보고
    _path_arg_safe를 적용한다."""
    for tok in tokens:
        if tok == "--":
            continue
        if tok.startswith("--"):
            if tok.split("=", 1)[0] not in allowed_long:
                return False
        elif tok.startswith("-") and tok != "-":
            if not flag_shorts.fullmatch(tok):
                return False
        elif not _path_arg_safe(tok):
            return False
    return True


def _validate_tool_args(tool: str | None, tokens: list[str], prefix: str = "") -> bool:
    """도구별 인자 검증. 통과하면 True."""
    if tool == "pytest":
        return all(_pytest_arg_ok(t) for t in tokens)
    if tool == "ruff":
        # 접두사에 `check`가 이미 있으면(env 확장 "ruff check --statistics" 등)
        # 나머지 인자 전체가 옵션/경로 인자다 — `check`를 다시 요구하면 안 된다.
        if "check" in prefix.split():
            return _allowlist_args_ok(tokens, _RUFF_ALLOWED_LONG_OPTS, _RUFF_FLAG_SHORTS_RE)
        # `ruff check` 서브커맨드만 허용 — `ruff format` 등은 거부
        if not tokens or tokens[0] != "check":
            return False
        return _allowlist_args_ok(tokens[1:], _RUFF_ALLOWED_LONG_OPTS, _RUFF_FLAG_SHORTS_RE)
    if tool == "mypy":
        return _allowlist_args_ok(tokens, _MYPY_ALLOWED_LONG_OPTS, _MYPY_FLAG_SHORTS_RE)
    # 미등록 접두사 — 기존 denylist로 보수적 검증
    return _denylist_args_ok(tokens, _GENERIC_DENIED_LONG_OPTS, _GENERIC_DENIED_SHORT_OPTS)


def _tool_for_prefix(prefix: str) -> str | None:
    """접두사 안의 첫 번째 인식 도구 토큰으로 도구 종류 추론.

    마지막 토큰 기준이면 `mypy --no-incremental` 같은 env 확장 접두사가
    도구를 미등록(None)으로 오인해, LLM이 제어하는 인자가 mypy/pytest
    전용 allowlist 대신 incomplete generic denylist로 검증된다
    (`--install-types`, `--html-report`, `--pyargs` 등 우회)."""
    for tok in prefix.split():
        if tok in ("pytest", "ruff", "mypy"):
            return tok
    return None


def _prefix_allowed(p: str) -> bool:
    """env 확장 접두사 검사 — 문자 집합 + 셸/인터프리터 재진입 차단.

    `python script.py`, `bash script.sh`처럼 첫 토큰이 인터프리터/셸이면
    임의 코드 실행 경로가 열린다. `python -m pytest` 같은 허용 도구의
    모듈 호출만 예외로 둔다."""
    if not (p and len(p) <= 80 and _PREFIX_SAFE.match(p)):
        return False
    parts = p.split()
    for i, tok in enumerate(parts):
        base = Path(tok).name.lower()
        if base in _SHELL_BINARIES:
            return False
        if _PY_INTERPRETER.match(base):
            nxt = parts[i + 1 : i + 3]
            if len(nxt) == 2 and nxt[0] == "-m" and nxt[1] in _ALLOWED_MODULES:
                continue
            return False
    return True


def _allowed_test_prefixes() -> list[str]:
    """기본 접두사 + JOBMATE_TEST_COMMAND_PREFIXES env 확장분을 반환한다."""
    prefixes = list(_DEFAULT_TEST_PREFIXES)
    raw = os.environ.get("JOBMATE_TEST_COMMAND_PREFIXES", "")
    for part in raw.split(","):
        p = " ".join(part.split())
        if _prefix_allowed(p):
            prefixes.append(p)
    return prefixes


def _match_test_prefix(tokens: list[str]) -> tuple[str | None, list[str]]:
    """argv 토큰이 허용 접두사와 일치하는지 검사. (prefix, 나머지 인자) 반환."""
    for p in sorted(_allowed_test_prefixes(), key=lambda s: -len(s.split())):
        pp = p.split()
        if tokens[: len(pp)] == pp:
            return p, tokens[len(pp) :]
    return None, tokens


def _is_allowed_test_command(command: str) -> bool:
    """정규화된 명령이 허용 접두사와 일치하고 인자 정책을 통과하는지 검사한다."""
    if _SHELL_METACHARS.search(command):
        return False
    tokens = command.split()
    prefix, rest = _match_test_prefix(tokens)
    if prefix is None:
        return False
    return _validate_tool_args(_tool_for_prefix(prefix), rest, prefix)


# read_file 시크릿/자격증명 경로 차단 — repo root 하위라도 읽기를 거부한다.
# basename은 소문자 기준, 경로는 `/` 구분 상대경로 기준으로 검사한다
# (`.envrc`, `dir/.env`, `Prod.ENV` 등 우회 변형 대응).
_SENSITIVE_NAME_SUBSTR = ("credential", "secret")
_SENSITIVE_SUFFIXES = (
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".jks",
    ".keystore",
    ".kdbx",
    ".sqlite",
    ".sqlite3",
    ".db",
    ".ppk",
    ".token",
)
_SENSITIVE_PREFIXES = ("id_rsa", "id_dsa", "id_ecdsa", "id_ed25519")


def _is_sensitive_relpath(rel: Path) -> bool:
    """repo 상대경로가 시크릿/자격증명 파일이면 True."""
    parts = [part.lower() for part in rel.parts]
    if ".git" in parts:
        return True
    name = parts[-1] if parts else ""
    if not name:
        return False
    if name.startswith(".env") or name.endswith(".env"):  # .env, .env.*, .envrc, prod.env 등
        return True
    if any(name.startswith(p) for p in _SENSITIVE_PREFIXES):
        return True
    if any(s in name for s in _SENSITIVE_NAME_SUBSTR):
        return True
    if ".sqlite" in name:  # .sqlite/.sqlite3 및 -wal/-shm 부속 파일
        return True
    if name.endswith(_SENSITIVE_SUFFIXES):
        return True
    if "token" in name and name.endswith(".json"):
        return True
    return False


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
            rel = p.relative_to(root)
            if _is_sensitive_relpath(rel):
                logger.warning("read_file denied sensitive path: %s", rel)
                raise ToolDeniedError("sensitive path")
            with open(p, encoding="utf-8", errors="replace") as f:
                return f.read(ToolExecutor.MAX_RESULT_CHARS)

        def run_tests(command: str = "pytest -q") -> dict[str, Any]:
            normalized = " ".join(str(command).split())
            if not _is_allowed_test_command(normalized):
                logger.warning("run_tests denied command: %s", normalized[:80])
                raise ToolDeniedError(
                    "command not allowed: only configured test/lint/typecheck "
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
