"""jobMate agent-bridge — 로컬 코딩 에이전트 세션 수집기.

claude / codex / gemini / devin CLI의 세션 파일을 스캔해 상태를 계산하고
jobMate API(`POST /api/v1/agent-sessions`)로 push한다.

외부 의존성: stdlib + httpx + zstd CLI 서브프로세스만 사용한다.

스캐너/공통 헬퍼는 bridge_common / bridge_claude / bridge_codex /
bridge_gemini / bridge_devin 모듈로 분리됐다 — 이 모듈은 repo 헬퍼와
Bridge만 유지하며, 기존 공개 API를 위해 이동된 이름을 모두 재수출한다.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

# 스캐너/공통 헬퍼 재수출 — 기존 `from jobmate import agent_bridge as ab`
# 사용처가 ab.scan_claude / ab.SessionState / ab._read_edges 등을 그대로 쓸 수 있도록.
# 주의: `_devin_lock_warned`·`_DEVIN_MAX_TRANSCRIPT_BYTES`는 값 복사다 —
# monkeypatch는 반드시 `jobmate.bridge_devin` 모듈을 대상으로 해야 한다
# (`jobmate.agent_bridge._DEVIN_MAX_TRANSCRIPT_BYTES` 패치는 원본에 영향 없음).
from .bridge_claude import (
    _claude_entries,
    _claude_slug_project,
    _claude_title,
    _claude_tokens,
    scan_claude,
)
from .bridge_codex import (
    _CODEX_UUID_RE,
    _codex_catalog_sessions,
    _codex_entries,
    _codex_session_key,
    _codex_text,
    _codex_title_entry,
    _codex_tokens,
    _payload,
    _zstd_edges,
    scan_codex,
)
from .bridge_common import (
    _CLAUDE_HEAD_BYTES,
    _CLAUDE_TAIL_BYTES,
    _CODEX_HEAD_BYTES,
    _CODEX_TAIL_BYTES,
    _DECISION_TOOLS,
    _STATUS_VALUES,
    ENDED_SECONDS,
    HEARTBEAT_SECONDS,
    HTTP_TIMEOUT,
    IDLE_SECONDS,
    SUBAGENT_BUSY_SECONDS,
    SessionState,
    _alive,
    _base_status,
    _blocks,
    _clip,
    _has_tool_result,
    _is_decision_tool,
    _iso,
    _iter_jsonl,
    _machine_name,
    _mtime,
    _read_edges,
    _text_of_content,
    _tool_use_names,
    _usage_int,
)
from .bridge_devin import (
    _DEVIN_MAX_TRANSCRIPT_BYTES,
    _devin_lock_warned,
    _devin_subagent_titles,
    scan_devin,
)
from .bridge_gemini import _blob_snippet, scan_gemini

logger = logging.getLogger("jobmate.agent_bridge")

# 이동된 이름들의 재수출 목록 — ruff F401(미사용 import) 면제용.
__all__ = [
    "Bridge",
    "ENDED_SECONDS",
    "HEARTBEAT_SECONDS",
    "HTTP_TIMEOUT",
    "IDLE_SECONDS",
    "SUBAGENT_BUSY_SECONDS",
    "SessionState",
    "_CACHE_MAX",
    "_CLAUDE_HEAD_BYTES",
    "_CLAUDE_TAIL_BYTES",
    "_CODEX_HEAD_BYTES",
    "_CODEX_TAIL_BYTES",
    "_CODEX_UUID_RE",
    "_DECISION_TOOLS",
    "_DEVIN_MAX_TRANSCRIPT_BYTES",
    "_GIT_TIMEOUT",
    "_STATUS_VALUES",
    "_alive",
    "_base_status",
    "_blob_snippet",
    "_blocks",
    "_claude_entries",
    "_claude_slug_project",
    "_claude_title",
    "_claude_tokens",
    "_clip",
    "_codex_catalog_sessions",
    "_codex_entries",
    "_codex_session_key",
    "_codex_text",
    "_codex_title_entry",
    "_codex_tokens",
    "_default_repo_roots",
    "_devin_lock_warned",
    "_devin_subagent_titles",
    "_git",
    "_git_end_summary",
    "_has_tool_result",
    "_is_decision_tool",
    "_iso",
    "_iter_jsonl",
    "_machine_name",
    "_mtime",
    "_payload",
    "_read_edges",
    "_text_of_content",
    "_tool_use_names",
    "_usage_int",
    "_zstd_edges",
    "scan_claude",
    "scan_codex",
    "scan_devin",
    "scan_gemini",
]


# ---------------------------------------------------------------------------
# Bridge
# ---------------------------------------------------------------------------

_GIT_TIMEOUT = 5.0

# Bridge 추적 캐시 상한 — 장기 실행 시 _ended_sent/_ended_cache/_repo_cache가
# 세션 수만큼 무한 성장하는 것을 막는다.
_CACHE_MAX = 5000


def _default_repo_roots() -> list[Path]:
    """project명(디렉터리 basename) → git repo 탐색 루트.

    JOBMATE_REPO_ROOTS(경로 구분자=os.pathsep)로 override 가능."""
    env = os.environ.get("JOBMATE_REPO_ROOTS", "")
    if env:
        return [Path(p).expanduser() for p in env.split(os.pathsep) if p.strip()]
    home = Path.home()
    return [home / d for d in ("Documents", "projects", "code", "src", "work", "dev")]


def _git(repo: Path, *args: str) -> str:
    """저비용 git 조회 — 실패/타임아웃 시 빈 문자열."""
    git = shutil.which("git")
    if not git:
        return ""
    try:
        proc = subprocess.run(  # noqa: S603 - 고정 argv, 로컬 repo 경로만 전달
            [git, "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as e:
        logger.debug("git %s failed in %s: %s", args[0] if args else "?", repo, e)
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _git_end_summary(repo: Path) -> tuple[str, list[str]]:
    """종료 세션용 경량 repo 요약 — (ended_summary, artifacts).

    마지막 커밋/미커밋 수/직전 커밋 diff stat만 수집한다(각 git 호출 5s 타임아웃)."""
    artifacts: list[str] = []
    head = _git(repo, "log", "-1", "--format=%h %s")
    if head:
        artifacts.append(_clip(f"commit {head}", 300))
    status = _git(repo, "status", "--porcelain")
    dirty = sum(1 for ln in status.splitlines() if ln.strip()) if status else 0
    if dirty:
        artifacts.append(f"uncommitted {dirty}")
    diff = _git(repo, "diff", "--stat", "HEAD~1..HEAD")
    diff_files = 0
    diff_lines = ""
    for ln in diff.splitlines():
        if "|" in ln:
            diff_files += 1
        elif ln.strip() and diff_lines == "":
            diff_lines = ln.strip()
    if diff_files:
        artifacts.append(f"diff~1 {diff_files} files")
    parts = []
    if head:
        parts.append(f"HEAD {head}")
    if dirty:
        parts.append(f"uncommitted {dirty}")
    if diff_files:
        parts.append(f"diff~1 {diff_files} files")
    if diff_lines:
        parts.append(diff_lines)
    return (_clip(" · ".join(parts), 500), artifacts[:10])


class Bridge:
    """4개 스캐너 결과를 diff/push한다."""

    def __init__(
        self,
        url: str,
        api_key: str | None = None,
        *,
        scanners: dict[str, Callable[[Path, float], list[SessionState]]] | None = None,
        roots: dict[str, Path] | None = None,
        repo_roots: list[Path] | None = None,
        client: Any | None = None,
        heartbeat_seconds: float = HEARTBEAT_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.url = url.rstrip("/")
        self.api_key = api_key
        self._scanners = scanners or {
            "claude": scan_claude,
            "codex": scan_codex,
            "gemini": scan_gemini,
            "devin": scan_devin,
        }
        self._roots = roots or {
            "claude": Path.home() / ".claude" / "projects",
            "codex": Path.home() / ".codex" / "sessions",
            "gemini": Path.home() / ".gemini" / "antigravity-cli" / "conversations",
            "devin": Path.home() / ".local" / "share" / "devin" / "cli",
        }
        self._client = client
        self._heartbeat = heartbeat_seconds
        self._clock = clock
        self._last: dict[tuple[str, str], SessionState] = {}
        self._last_push: dict[tuple[str, str], float] = {}
        self._ended_sent: set[tuple[str, str]] = set()
        self._repo_roots = repo_roots if repo_roots is not None else _default_repo_roots()
        self._repo_cache: dict[str, Path | None] = {}
        self._ended_cache: dict[tuple[str, str], tuple[str, list[str]]] = {}
        self._machine = _machine_name()

    @staticmethod
    def _cache_put(cache: dict[Any, Any], key: Any, value: Any, limit: int = _CACHE_MAX) -> None:
        """캐시 크기 상한 — 초과 시 전체 비우고 재삽입 (오래된 항목부터 제거와
        동등한 효과; 키 집합이 작아 clear 비용이 무시할 수준)."""
        if len(cache) >= limit:
            cache.clear()
        cache[key] = value

    def _repo_for(self, project: str) -> Path | None:
        """project명(basename) → git repo 경로. 결과는 프로젝트별 캐시."""
        if not project:
            return None
        if project in self._repo_cache:
            return self._repo_cache[project]
        found: Path | None = None
        for root in self._repo_roots:
            if not root.is_dir():
                continue
            # 깊이 3까지 탐색 — ~/Documents/python-projects/foo 같은 중첩 대응
            for cand in (
                root / project,
                *root.glob(f"*/{project}"),
                *root.glob(f"*/*/{project}"),
            ):
                if cand.is_dir() and (cand / ".git").exists():
                    found = cand
                    break
            if found is not None:
                break
        self._cache_put(self._repo_cache, project, found)
        return found

    def _ended_artifacts(self, state: SessionState) -> tuple[str, list[str]]:
        """ended 전이 시 1회만 repo 요약 수집 (session_key별 캐시)."""
        key = (state.tool, state.session_key)
        if key in self._ended_cache:
            return self._ended_cache[key]
        result: tuple[str, list[str]] = ("", [])
        repo = self._repo_for(state.project)
        if repo is not None:
            try:
                result = _git_end_summary(repo)
            except Exception as e:
                logger.debug("ended summary failed %s/%s: %s", state.tool, key[1], e)
        self._cache_put(self._ended_cache, key, result)
        return result

    def scan_all(self) -> dict[tuple[str, str], SessionState]:
        now = self._clock()
        found: dict[tuple[str, str], SessionState] = {}
        for tool, scan in self._scanners.items():
            root = self._roots.get(tool, Path("/nonexistent"))
            try:
                for st in scan(root, now):
                    found[(st.tool, st.session_key)] = st
            except Exception as e:  # 스캐너 하나가 실패해도 나머지는 계속
                logger.warning("scan %s failed: %s", tool, e)
        return found

    def _post(self, state: SessionState, client: Any | None = None) -> bool:
        import httpx

        client = client if client is not None else self._client
        close = False
        if client is None:
            client = httpx.Client(timeout=HTTP_TIMEOUT)
            close = True
        try:
            headers = {}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            body: dict[str, Any] = {
                "tool": state.tool,
                "session_key": state.session_key,
                "project": state.project,
                "title": state.title,
                "status": state.status,
                "doing": state.doing,
                "ask": state.ask,
                "event_at": state.event_at,
                "machine": self._machine,
            }
            if state.tokens_in is not None:
                body["tokens_in"] = state.tokens_in
            if state.tokens_out is not None:
                body["tokens_out"] = state.tokens_out
            if state.ended_summary:
                body["ended_summary"] = state.ended_summary
            if state.artifacts:
                body["artifacts"] = state.artifacts
            resp = client.post(f"{self.url}/api/v1/agent-sessions", json=body, headers=headers)
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.warning("push failed %s/%s: %s", state.tool, state.session_key, e)
            return False
        finally:
            if close:
                client.close()

    def push(self, client: Any | None = None) -> dict[str, int]:
        """현재 스캔과 이전 스캔을 비교해 변경분만 POST한다.

        반환: {"pushed": n, "ended": n, "skipped": n}
        """
        now = self._clock()
        current = self.scan_all()
        counts = {"pushed": 0, "ended": 0, "skipped": 0}
        post_client = client if client is not None else self._client

        # 사라진 세션 → ended 1회 push 후 추적 제거
        for key in set(self._last) | set(self._last_push):
            if key in current or key in self._ended_sent:
                continue
            prev = self._last.get(key)
            if prev is None:
                continue
            ended = SessionState(
                tool=prev.tool,
                session_key=prev.session_key,
                project=prev.project,
                title=prev.title,
                status="ended",
                doing="",
                ask="",
                event_at=prev.event_at,
                path=prev.path,
                tokens_in=prev.tokens_in,
                tokens_out=prev.tokens_out,
            )
            # 종료 전이 시점에만 경량 repo 산출물 수집 (프로젝트→repo 매핑 실패 시 스킵)
            ended.ended_summary, ended.artifacts = self._ended_artifacts(prev)
            if self._post(ended, post_client):
                self._ended_sent.add(key)
                if len(self._ended_sent) > _CACHE_MAX:
                    self._ended_sent.clear()
                counts["ended"] += 1
                # push 성공 시에만 추적 제거 — 실패하면 _last/_last_push를
                # 유지해 다음 스캔에서 ended를 재시도한다(영구 유실 방지).
                # 재전송은 서버 upsert라 중복이어도 무해하다.
                self._last.pop(key, None)
                self._last_push.pop(key, None)

        for key, state in current.items():
            # 재등장한 세션은 ended 추적을 해제 — 다시 사라질 때 ended를 재전송해야 한다
            self._ended_sent.discard(key)
            prev = self._last.get(key)
            last_t = self._last_push.get(key, 0.0)
            changed = (
                prev is None
                or prev.status != state.status
                or prev.doing != state.doing
                or prev.ask != state.ask
                or prev.project != state.project
                or prev.title != state.title
            )
            heartbeat_due = (now - last_t) >= self._heartbeat
            if not (changed or heartbeat_due):
                counts["skipped"] += 1
                continue
            if state.status == "ended":
                # 스캐너가 ended를 직접 반환하는 경우는 없음 — 방어적 처리
                self._ended_sent.add(key)
            if self._post(state, post_client):
                self._last[key] = state
                self._last_push[key] = now
                counts["pushed"] += 1
        return counts
