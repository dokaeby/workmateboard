"""jobMate agent-bridge — Claude Code 스캐너.

agent_bridge.py에서 분리된 코드 모션 리팩터링 — 동작 변경 없음.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from pathlib import Path

from .bridge_common import (
    _CLAUDE_HEAD_BYTES,
    _CLAUDE_TAIL_BYTES,
    SessionState,
    _alive,
    _base_status,
    _clip,
    _has_tool_result,
    _iso,
    _iter_jsonl,
    _mtime,
    _read_edges,
    _text_of_content,
    _tool_use_names,
    _usage_int,
)

logger = logging.getLogger("jobmate.agent_bridge")


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
    """'-Users-sec-Documents-foo' → 'foo' (basename).

    한계: Claude Code는 프로젝트 디렉터리명을 만들어 slug로 쓸 때
    경로 구분자뿐 아니라 일부 특수문자도 '-'로 치환한다. 원래 이름에
    '-'나 치환 대상 문자가 있으면 구분이 모호해지므로 이 값은
    best-effort다 — 정확한 프로젝트명은 transcript의 cwd 메타데이터가
    우선 사용되어야 한다(scan_claude 참조)."""
    decoded = slug.lstrip("-").replace("-", "/")
    return decoded.rsplit("/", 1)[-1] if decoded else slug


def _claude_tokens(entries: Iterable[dict]) -> tuple[int | None, int | None]:
    """assistant 메시지의 usage 합산 (head+tail 스캔 범위 내 best-effort)."""
    tin = tout = 0
    seen = False
    for e in entries:
        if e.get("type") != "assistant":
            continue
        msg = e.get("message")
        usage = msg.get("usage") if isinstance(msg, dict) else None
        if not isinstance(usage, dict):
            continue
        i = _usage_int(usage.get("input_tokens"))
        o = _usage_int(usage.get("output_tokens"))
        if i is None and o is None:
            continue
        tin += i or 0
        tout += o or 0
        seen = True
    return (tin, tout) if seen else (None, None)


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
        # head+tail을 모두 사용 — 중간 크기 파일에서 tail만 쓰면
        # head 구간의 초기 엔트리(session_meta, 첫 user 메시지 등)가 누락된다.
        # 큰 파일은 tail 첫 라인이 잘려 있을 수 있으나 _claude_entries가 처리.
        entries = head_entries + tail_entries

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

        tokens_in = tokens_out = None
        try:
            tokens_in, tokens_out = _claude_tokens(head_entries + tail_entries)
        except Exception as e:
            logger.debug("claude token scan failed %s: %s", path, e)

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
                tokens_in=tokens_in,
                tokens_out=tokens_out,
            )
        )

    # ~/.claude/sessions/*.json — 실행 중 세션의 sessionId+mtime.
    # 긴 도구 실행으로 transcript jsonl이 멈춰도 세션 파일이 최신이면
    # working을 유지한다 (devin sessions.db / codex catalog와 동일 패턴).
    by_key = {s.session_key: s for s in states}
    sdir = root.parent / "sessions"
    mtimes = {s.session_key: _mtime(Path(s.path)) for s in states}
    if sdir.is_dir():
        for sf in sorted(sdir.glob("*.json")):
            sm = _mtime(sf)
            if not _alive(sm, now):
                continue
            try:
                meta = json.loads(sf.read_text(encoding="utf-8", errors="replace"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(meta, dict):
                continue
            sid = str(meta.get("sessionId") or meta.get("session_id") or "")
            existing = by_key.get(sid)
            if existing is None or sm <= mtimes.get(sid, 0.0):
                continue
            mtimes[sid] = sm
            if existing.status != "decision":
                existing.status = _base_status(sm, now)
            existing.event_at = _iso(sm)
    return states
