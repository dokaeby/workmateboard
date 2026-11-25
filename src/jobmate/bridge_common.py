"""jobMate agent-bridge 공통 — 스캐너 공유 헬퍼/상수/SessionState.

agent_bridge.py에서 분리된 코드 모션 리팩터링 — 동작 변경 없음.
"""

from __future__ import annotations

import json
import logging
import socket
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("jobmate.agent_bridge")

IDLE_SECONDS = 45.0
ENDED_SECONDS = 600.0
HEARTBEAT_SECONDS = 60.0
HTTP_TIMEOUT = 10.0
SUBAGENT_BUSY_SECONDS = 180.0

_CLAUDE_TAIL_BYTES = 512 * 1024
_CLAUDE_HEAD_BYTES = 128 * 1024
_CODEX_TAIL_BYTES = 256 * 1024
_CODEX_HEAD_BYTES = 64 * 1024

_STATUS_VALUES = ("working", "decision", "rest", "ended")


@dataclass
class SessionState:
    tool: str
    session_key: str
    project: str
    title: str
    status: str  # working | decision | rest | ended
    doing: str
    ask: str
    event_at: str  # ISO timestamp (파일 mtime 기준)
    path: str
    ended_summary: str = ""  # 종료 시 수집한 repo 요약 (ended push 전용)
    artifacts: list[str] | None = None  # 종료 시 수집한 산출물 짧은 문자열 목록
    tokens_in: int | None = None  # 누적 입력 토큰 (수집 가능 시)
    tokens_out: int | None = None  # 누적 출력 토큰


def _machine_name() -> str:
    """짧은 호스트명 (서버 측 machine 필터용). 실패 시 'unknown'."""
    try:
        return socket.gethostname().split(".")[0][:128] or "unknown"
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# 공통 헬퍼
# ---------------------------------------------------------------------------


def _clip(text: str | None, limit: int) -> str:
    if not text:
        return ""
    return " ".join(text.split())[:limit].strip()


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)) + f".{int((ts % 1) * 1000):03d}Z"


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _alive(mtime: float, now: float) -> bool:
    """ENDED_SECONDS 이내 파일만 세션으로 취급."""
    return 0.0 < mtime and (now - mtime) <= ENDED_SECONDS


def _base_status(mtime: float, now: float) -> str:
    return "working" if (now - mtime) < IDLE_SECONDS else "rest"


_DECISION_TOOLS = frozenset({"ask_user_question", "request_user_input"})


def _is_decision_tool(name: str) -> bool:
    return name.rsplit(".", 1)[-1].lower() in _DECISION_TOOLS


def _read_edges(path: Path, head_bytes: int, tail_bytes: int) -> tuple[bytes, bytes]:
    """파일 앞/뒤 일부만 읽는다. 작은 파일은 전체를 head로 반환."""
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            head = f.read(min(head_bytes, size))
            if size <= head_bytes:
                return head, b""
            if size <= head_bytes + tail_bytes:
                # 중간 크기 파일 — head 이후~EOF를 tail로 읽어 중간 구간 유실 방지
                f.seek(head_bytes)
                return head, f.read()
            f.seek(size - tail_bytes)
            tail = f.read(tail_bytes)
            return head, tail
    except OSError as e:
        logger.debug("read failed %s: %s", path, e)
        return b"", b""


def _iter_jsonl(data: bytes) -> Iterable[dict]:
    for line in data.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(obj, dict):
            yield obj


def _text_of_content(content: object) -> str:
    """message.content(str | list[block])에서 텍스트를 추출."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif block.get("type") == "output_text" and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "\n".join(parts)
    return ""


def _blocks(content: object) -> list[dict]:
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def _tool_use_names(content: object) -> list[str]:
    return [
        str(b.get("name") or "tool")
        for b in _blocks(content)
        if b.get("type") in ("tool_use", "tool_call", "function_call")
    ]


def _has_tool_result(content: object) -> bool:
    return any(b.get("type") == "tool_result" for b in _blocks(content))


def _usage_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
