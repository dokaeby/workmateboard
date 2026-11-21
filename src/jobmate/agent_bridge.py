"""jobMate agent-bridge — 로컬 코딩 에이전트 세션 수집기.

claude / codex / gemini / devin CLI의 세션 파일을 스캔해 상태를 계산하고
jobMate API(`POST /api/v1/agent-sessions`)로 push한다.

외부 의존성: stdlib + httpx + zstd CLI 서브프로세스만 사용한다.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import sqlite3
import subprocess
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("jobmate.agent_bridge")

IDLE_SECONDS = 45.0
ENDED_SECONDS = 600.0
HEARTBEAT_SECONDS = 60.0
HTTP_TIMEOUT = 10.0

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


def _read_edges(path: Path, head_bytes: int, tail_bytes: int) -> tuple[bytes, bytes]:
    """파일 앞/뒤 일부만 읽는다. 작은 파일은 전체를 head로 반환."""
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            head = f.read(min(head_bytes, size))
            if size <= head_bytes + tail_bytes:
                return head, b""
            f.seek(max(0, size - tail_bytes))
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


# ---------------------------------------------------------------------------
# Claude Code: ~/.claude/projects/<slug>/*.jsonl
# ---------------------------------------------------------------------------


def _claude_entries(head: bytes, tail: bytes) -> tuple[list[dict], list[dict]]:
    """(head_entries, tail_entries) — title 용 head, 상태 용 tail."""
    head_entries = list(_iter_jsonl(head))
    if tail:
        # tail 첫 라인은 잘린 라인일 수 있으므로 첫 줄을 버린다.
        tail = tail.split(b"\n", 1)[1] if b"\n" in tail else b""
        return head_entries, list(_iter_jsonl(tail))
    return head_entries, []


def _claude_title(entries: Iterable[dict]) -> str:
    for e in entries:
        if e.get("type") != "user" or e.get("isMeta"):
            continue
        text = _text_of_content((e.get("message") or {}).get("content"))
        text = text.strip()
        # <command-...>, <local-command-...> 같은 메타 프롬프트 제외
        if text and not text.startswith("<"):
            return _clip(text, 60)
    return ""


def _claude_slug_project(slug: str) -> str:
    """'-Users-sec-Documents-foo' → 'foo' (basename)."""
    decoded = slug.lstrip("-").replace("-", "/")
    return decoded.rsplit("/", 1)[-1] if decoded else slug


def scan_claude(root: Path, now: float) -> list[SessionState]:
    """root = ~/.claude/projects 디렉터리."""
    states: list[SessionState] = []
    if not root.is_dir():
        return states
    for path in sorted(root.glob("*/*.jsonl")):
        if path.name == "memory.jsonl" or not path.is_file():
            continue
        mtime = _mtime(path)
        if not _alive(mtime, now):
            continue
        head, tail = _read_edges(path, _CLAUDE_HEAD_BYTES, _CLAUDE_TAIL_BYTES)
        head_entries, tail_entries = _claude_entries(head, tail)
        entries = tail_entries or head_entries

        title = _claude_title(head_entries) or _claude_title(entries)
        project = ""
        cwd = ""
        for e in reversed(entries):
            c = e.get("cwd")
            if isinstance(c, str) and c:
                cwd = c
                break
        if cwd:
            project = Path(cwd).name
        if not project:
            project = _claude_slug_project(path.parent.name)

        # 마지막 assistant 이후 미해결 tool_use → decision
        pending: list[str] = []
        doing = ""
        for e in entries:
            if e.get("isSidechain"):
                continue
            etype = e.get("type")
            content = (e.get("message") or {}).get("content")
            if etype == "assistant":
                names = _tool_use_names(content)
                if names:
                    pending.extend(names)
                text = _text_of_content(content)
                if _clip(text, 1):
                    doing = _clip(text, 80)
            elif etype == "user" and _has_tool_result(content):
                # tool_result 도착 → 대기 해소 (id 매칭 없이 순서 기반)
                pending.clear()
        decision = bool(pending)

        status = "decision" if decision else _base_status(mtime, now)
        states.append(
            SessionState(
                tool="claude",
                session_key=path.stem,
                project=project,
                title=title,
                status=status,
                doing=doing or (pending[-1] if pending else ""),
                ask=pending[-1] if pending else "",
                event_at=_iso(mtime),
                path=str(path),
            )
        )
    return states


# ---------------------------------------------------------------------------
# Codex CLI: ~/.codex/sessions/**/*.jsonl.zst
# ---------------------------------------------------------------------------

_CODEX_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _codex_session_key(name: str) -> str:
    m = _CODEX_UUID_RE.search(name)
    return m.group(0) if m else name


def _zstd_edges(path: Path, head_bytes: int, tail_bytes: int) -> tuple[bytes, bytes]:
    """`zstd -dc` 출력을 앞/뒤 일부만 메모리에 유지하며 스트리밍."""
    try:
        zstd = shutil.which("zstd")
        if not zstd:
            logger.debug("zstd not found; skip %s", path)
            return b"", b""
        proc = subprocess.Popen(  # noqa: S603 - 고정 argv, 로컬 파일 경로만 전달
            [zstd, "-dc", str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError as e:
        logger.debug("zstd spawn failed for %s: %s", path, e)
        return b"", b""
    assert proc.stdout is not None
    head = bytearray()
    tail = bytearray()
    head_done = False
    chunk_size = 1024 * 1024
    try:
        while True:
            chunk = proc.stdout.read(chunk_size)
            if not chunk:
                break
            if not head_done:
                need = head_bytes - len(head)
                if len(chunk) <= need:
                    head.extend(chunk)
                    continue
                head.extend(chunk[:need])
                chunk = chunk[need:]
                head_done = True
            tail.extend(chunk)
            if len(tail) > tail_bytes:
                del tail[: len(tail) - tail_bytes]
    finally:
        proc.stdout.close()
        proc.wait()
    return bytes(head), bytes(tail)


def _codex_entries(head: bytes, tail: bytes) -> tuple[list[dict], list[dict]]:
    head_entries = list(_iter_jsonl(head))
    if tail:
        tail = tail.split(b"\n", 1)[1] if b"\n" in tail else b""
        return head_entries, list(_iter_jsonl(tail))
    return head_entries, []


def _payload(entry: dict) -> dict:
    p = entry.get("payload")
    return p if isinstance(p, dict) else {}


def _codex_text(payload: dict) -> str:
    content = payload.get("content")
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "\n".join(parts)
    if isinstance(content, str):
        return content
    return ""


def scan_codex(root: Path, now: float) -> list[SessionState]:
    """root = ~/.codex/sessions 디렉터리."""
    states: list[SessionState] = []
    if not root.is_dir():
        return states
    for path in sorted(root.rglob("*.jsonl.zst")):
        if not path.is_file():
            continue
        mtime = _mtime(path)
        if not _alive(mtime, now):
            continue
        head, tail = _zstd_edges(path, _CODEX_HEAD_BYTES, _CODEX_TAIL_BYTES)
        head_entries, tail_entries = _codex_entries(head, tail)
        entries = tail_entries or head_entries

        project = ""
        title = ""
        for e in head_entries:
            p = _payload(e)
            if e.get("type") == "session_meta":
                cwd = p.get("cwd")
                if isinstance(cwd, str) and cwd:
                    project = Path(cwd).name
            if not title:
                title = _codex_title_entry(e)
        if not project:
            # 상위 디렉터리명(YYYY/MM/DD)보다 프로젝트명에 가까운 조상 사용
            project = path.parent.parent.parent.name if len(path.parents) >= 4 else path.parent.name

        # 마지막 의미 payload 기준 상태 판정
        pending_calls: list[str] = []
        doing = ""
        saw_task_complete = False
        last_signal = ""  # assistant | call | output | task_complete
        for e in entries:
            etype = e.get("type")
            p = _payload(e)
            ptype = p.get("type")
            if etype == "event_msg" and ptype == "task_complete":
                saw_task_complete = True
                last_signal = "task_complete"
                continue
            if etype != "response_item":
                continue
            if ptype == "message":
                role = p.get("role")
                text = _clip(_codex_text(p), 80)
                if role == "assistant" and text:
                    doing = text
                    last_signal = "assistant"
                elif role == "user" and not title:
                    title = _clip(text, 60)
            elif ptype in ("function_call", "custom_tool_call", "local_shell_call"):
                pending_calls.append(str(p.get("name") or "tool"))
                last_signal = "call"
            elif isinstance(ptype, str) and ptype.endswith("_output"):
                pending_calls.clear()
                last_signal = "output"

        decision = bool(pending_calls) and last_signal == "call"
        if decision:
            status = "decision"
        elif saw_task_complete and last_signal == "task_complete":
            status = "rest"
        else:
            status = _base_status(mtime, now)

        states.append(
            SessionState(
                tool="codex",
                session_key=_codex_session_key(path.name),
                project=project,
                title=title,
                status=status,
                doing=doing or (pending_calls[-1] if pending_calls else ""),
                ask=pending_calls[-1] if pending_calls else "",
                event_at=_iso(mtime),
                path=str(path),
            )
        )
    return states


def _codex_title_entry(entry: dict) -> str:
    if entry.get("type") != "response_item":
        return ""
    p = _payload(entry)
    if p.get("type") == "message" and p.get("role") == "user":
        return _clip(_codex_text(p), 60)
    return ""


# ---------------------------------------------------------------------------
# Gemini (antigravity-cli): ~/.gemini/antigravity-cli/conversations/*.db
# ---------------------------------------------------------------------------


def _blob_snippet(blob: object, limit: int = 80) -> str:
    """protobuf blob에서 읽을 수 있는 문자열 조각을 추출 (best-effort)."""
    if not isinstance(blob, (bytes, bytearray)) or not blob:
        return ""
    text = bytes(blob).decode("utf-8", errors="ignore")
    # 길이 있는 printable run 중 가장 긴 것을 사용
    runs = re.findall(r"[\w가-힣][\w가-힣 .,:;_\-/()\[\]{}<>#%'\"+=!?]{7,}", text)
    if not runs:
        return ""
    return _clip(max(runs, key=len), limit)


def scan_gemini(root: Path, now: float) -> list[SessionState]:
    """root = ~/.gemini/antigravity-cli/conversations 디렉터리.

    주의: steps 테이블은 protobuf blob 기반이라 스키마가 불안정하다.
    최신 step의 payload에서 텍스트 조각을 best-effort로 추출하고,
    실패하면 mtime 기반 working/rest + doing='gemini session'으로 degrade한다.
    """
    states: list[SessionState] = []
    if not root.is_dir():
        return states
    for path in sorted(root.glob("*.db")):
        if not path.is_file():
            continue
        mtime = _mtime(path)
        if not _alive(mtime, now):
            continue
        doing = "gemini session"
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
            try:
                tables = {
                    r[0]
                    for r in con.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                if "steps" in tables:
                    row = con.execute(
                        "SELECT step_payload FROM steps ORDER BY idx DESC LIMIT 1"
                    ).fetchone()
                    if row:
                        doing = _blob_snippet(row[0]) or doing
            finally:
                con.close()
        except sqlite3.Error as e:
            logger.debug("gemini db read failed %s: %s", path, e)

        states.append(
            SessionState(
                tool="gemini",
                session_key=path.stem,
                project="",
                title="",
                status=_base_status(mtime, now),
                doing=doing,
                ask="",
                event_at=_iso(mtime),
                path=str(path),
            )
        )
    return states


# ---------------------------------------------------------------------------
# Devin CLI: ~/.local/share/devin/cli (transcripts/*.json + session_locks/)
# ---------------------------------------------------------------------------


def scan_devin(root: Path, now: float) -> list[SessionState]:
    """root = ~/.local/share/devin/cli 디렉터리.

    transcripts/*.json (ATIF) + session_locks/<stem>.lock 존재 여부로
    활성 세션을 판별한다. lock이 없는 transcript는 ended이므로 반환하지 않는다
    (Bridge가 이전 스캔 대비 사라진 세션을 ended로 push한다).
    """
    states: list[SessionState] = []
    tdir = root / "transcripts"
    ldir = root / "session_locks"
    if not tdir.is_dir():
        return states
    for path in sorted(tdir.glob("*.json")):
        if not path.is_file():
            continue
        mtime = _mtime(path)
        if not _alive(mtime, now):
            continue
        if ldir.is_dir() and not (ldir / f"{path.stem}.lock").exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError) as e:
            logger.debug("devin transcript parse failed %s: %s", path, e)
            continue
        steps = data.get("steps") if isinstance(data, dict) else None
        if not isinstance(steps, list):
            continue

        title = ""
        doing = ""
        pending_tool = ""
        for s in steps:
            if not isinstance(s, dict):
                continue
            src = s.get("source")
            msg = s.get("message") if isinstance(s.get("message"), str) else ""
            if src == "user" and not title:
                title = _clip(msg, 60)
            if src == "agent":
                calls = s.get("tool_calls")
                observation = s.get("observation")
                has_result = bool(observation)
                if isinstance(calls, list) and calls and not has_result:
                    fn = calls[0].get("function_name") if isinstance(calls[0], dict) else None
                    pending_tool = str(fn or "tool")
                else:
                    pending_tool = ""
                doing = _clip(msg, 80) or doing

        decision = bool(pending_tool)
        states.append(
            SessionState(
                tool="devin",
                session_key=path.stem,
                project="",
                title=title,
                status="decision" if decision else _base_status(mtime, now),
                doing=doing or pending_tool,
                ask=pending_tool,
                event_at=_iso(mtime),
                path=str(path),
            )
        )
    return states


# ---------------------------------------------------------------------------
# Bridge
# ---------------------------------------------------------------------------


class Bridge:
    """4개 스캐너 결과를 diff/push한다."""

    def __init__(
        self,
        url: str,
        api_key: str | None = None,
        *,
        scanners: dict[str, Callable[[Path, float], list[SessionState]]] | None = None,
        roots: dict[str, Path] | None = None,
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
            body = {
                "tool": state.tool,
                "session_key": state.session_key,
                "project": state.project,
                "title": state.title,
                "status": state.status,
                "doing": state.doing,
                "ask": state.ask,
                "event_at": state.event_at,
            }
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
            )
            if self._post(ended, post_client):
                self._ended_sent.add(key)
                counts["ended"] += 1
            self._last.pop(key, None)
            self._last_push.pop(key, None)

        for key, state in current.items():
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
