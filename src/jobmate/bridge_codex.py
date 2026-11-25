"""jobMate agent-bridge — Codex CLI 스캐너.

agent_bridge.py에서 분리된 코드 모션 리팩터링 — 동작 변경 없음.
"""

from __future__ import annotations

import logging
import re
import shutil
import sqlite3
import subprocess
from collections.abc import Iterable
from pathlib import Path

from .bridge_common import (
    _CODEX_HEAD_BYTES,
    _CODEX_TAIL_BYTES,
    ENDED_SECONDS,
    SessionState,
    _alive,
    _base_status,
    _clip,
    _is_decision_tool,
    _iso,
    _iter_jsonl,
    _mtime,
    _read_edges,
    _usage_int,
)

logger = logging.getLogger("jobmate.agent_bridge")


# ---------------------------------------------------------------------------
# Codex CLI: ~/.codex/sessions/**/*.jsonl(.zst)
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
    timed_out = False
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
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            # zstd가 행업 상태로 남지 않도록 kill — 부분 결과는 버린다.
            proc.kill()
            proc.wait()
            timed_out = True
            logger.warning("zstd decompress timed out for %s", path)
    if timed_out:
        return b"", b""
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
    key_mtimes: dict[str, float] = {}  # session_key → 파일 mtime (catalog 병합용)
    if not root.is_dir():
        return states
    paths = sorted(
        p
        for p in root.rglob("*")
        if p.is_file() and (p.name.endswith(".jsonl") or p.name.endswith(".jsonl.zst"))
    )
    for path in paths:
        mtime = _mtime(path)
        if not _alive(mtime, now):
            continue
        if path.name.endswith(".zst"):
            head, tail = _zstd_edges(path, _CODEX_HEAD_BYTES, _CODEX_TAIL_BYTES)
        else:
            head, tail = _read_edges(path, _CODEX_HEAD_BYTES, _CODEX_TAIL_BYTES)
        head_entries, tail_entries = _codex_entries(head, tail)
        # claude와 동일 — head+tail 모두 사용해 중간 구간 엔트리 누락 방지.
        entries = head_entries + tail_entries

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
            # sessions/YYYY/MM/DD 구조 — 날짜 디렉터리를 건너뛰고
            # 프로젝트명에 가까운 조상을 사용 (없으면 "")
            for anc in path.parents:
                n = anc.name
                if not n or n.startswith(".") or n == "sessions":
                    break
                if re.fullmatch(r"\d{4}|\d{2}", n):
                    continue
                project = n
                break

        # 마지막 의미 payload 기준 상태 판정
        # (name, call_id) — output은 call_id가 일치하는 call만 제거한다.
        # call_id 없는 output은 가장 최근 call과 짝지어 제거 (FIFO 가정).
        pending_calls: list[tuple[str, str]] = []
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
                pending_calls.append((str(p.get("name") or "tool"), str(p.get("call_id") or "")))
                last_signal = "call"
            elif isinstance(ptype, str) and ptype.endswith("_output"):
                cid = str(p.get("call_id") or "")
                if cid:
                    pending_calls = [c for c in pending_calls if c[1] != cid]
                elif pending_calls:
                    pending_calls.pop()
                last_signal = "output"

        pending_call = pending_calls[-1][0] if pending_calls else ""
        decision = bool(pending_call) and _is_decision_tool(pending_call) and last_signal == "call"
        if decision:
            status = "decision"
        elif saw_task_complete and last_signal == "task_complete":
            status = "rest"
        else:
            status = _base_status(mtime, now)

        tokens_in = tokens_out = None
        try:
            tokens_in, tokens_out = _codex_tokens(head_entries + tail_entries)
        except Exception as e:
            logger.debug("codex token scan failed %s: %s", path, e)

        skey = _codex_session_key(path.name)
        key_mtimes[skey] = mtime
        states.append(
            SessionState(
                tool="codex",
                session_key=skey,
                project=project,
                title=title,
                status=status,
                doing=doing or pending_call,
                ask=pending_call if decision else "",
                event_at=_iso(mtime),
                path=str(path),
                tokens_in=tokens_in,
                tokens_out=tokens_out,
            )
        )

    # 데스크톱/앱 서버 스레드 — ~/.codex/sqlite/codex.db (local_thread_catalog)
    # rollout jsonl과 thread_id가 같으면 rollout 쪽을 우선한다.
    by_key = {s.session_key: s for s in states}
    db_path = root.parent / "sqlite" / "codex.db"
    states.extend(_codex_catalog_sessions(db_path, by_key, key_mtimes, now))
    return states


def _codex_catalog_sessions(
    db_path: Path,
    by_key: dict[str, SessionState],
    key_mtimes: dict[str, float],
    now: float,
) -> list[SessionState]:
    """codex.db local_thread_catalog — Codex 데스크톱/IDE(chatgpt, vscode) 스레드.

    rollout과 thread_id가 겹치면 상태만 갱신한다 — catalog의
    source_recency_at이 rollout mtime보다 최신이면 recency를 끌어올려
    긴 도구 실행 중 rest로 떨어지는 것을 막는다."""
    states: list[SessionState] = []
    if not db_path.is_file():
        return states
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
        try:
            tables = {
                r[0]
                for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            if "local_thread_catalog" not in tables:
                return states
            rows = con.execute(
                "SELECT thread_id, display_title, cwd, source_kind,"
                " source_recency_at FROM local_thread_catalog"
                " WHERE missing_candidate = 0"
                " ORDER BY source_recency_at DESC LIMIT 50"
            ).fetchall()
        finally:
            con.close()
    except sqlite3.Error as e:
        logger.debug("codex catalog read failed %s: %s", db_path, e)
        return states

    for thread_id, title, cwd, source_kind, recency in rows:
        if not thread_id:
            continue
        try:
            recency_f = float(recency or 0)
        except (TypeError, ValueError):
            continue
        existing = by_key.get(thread_id)
        if existing is not None:
            # rollout 세션 — catalog recency가 더 최신이면 상태만 끌어올린다
            if recency_f > key_mtimes.get(thread_id, 0.0):
                key_mtimes[thread_id] = recency_f
                if existing.status != "decision":
                    existing.status = _base_status(recency_f, now)
                existing.event_at = _iso(recency_f)
            continue
        if recency_f <= 0 or (now - recency_f) > ENDED_SECONDS:
            continue
        project = Path(cwd).name if isinstance(cwd, str) and cwd else str(source_kind or "")
        states.append(
            SessionState(
                tool="codex",
                session_key=thread_id,
                project=project,
                title=_clip(str(title or ""), 60),
                status=_base_status(recency_f, now),
                doing="",
                ask="",
                event_at=_iso(recency_f),
                path=str(db_path),
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


def _codex_tokens(entries: Iterable[dict]) -> tuple[int | None, int | None]:
    """event_msg/token_count의 total_token_usage — 누적값이라 마지막 것을 취한다.

    head+tail을 시간순으로 넘기면 tail의 최신 이벤트가 마지막에 온다."""
    tokens_in = tokens_out = None
    for e in entries:
        if e.get("type") != "event_msg":
            continue
        p = _payload(e)
        if p.get("type") != "token_count":
            continue
        info = p.get("info")
        total = info.get("total_token_usage") if isinstance(info, dict) else None
        if not isinstance(total, dict):
            continue
        i = _usage_int(total.get("input_tokens"))
        o = _usage_int(total.get("output_tokens"))
        if i is not None:
            tokens_in = i
        if o is not None:
            tokens_out = o
    return tokens_in, tokens_out
