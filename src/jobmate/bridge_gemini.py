"""jobMate agent-bridge — Gemini (antigravity-cli) 스캐너.

agent_bridge.py에서 분리된 코드 모션 리팩터링 — 동작 변경 없음.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path

from .bridge_common import SessionState, _alive, _base_status, _clip, _iso, _mtime

logger = logging.getLogger("jobmate.agent_bridge")


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
    mtimes: dict[str, float] = {}  # session_key → db 파일 mtime (lock 병합용)
    if not root.is_dir():
        return states
    for path in sorted(root.glob("*.db")):
        if not path.is_file():
            continue
        mtime = _mtime(path)
        if not _alive(mtime, now):
            continue
        mtimes[path.stem] = mtime
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

    # presence/*.lock — 대화 db 파일이 오래돼도 lock이 최근이면 활성 세션.
    # lock stem = conversations/*.db stem 이므로 같은 session_key로 병합한다.
    by_key = {s.session_key: s for s in states}
    pdir = root.parent / "presence"
    if pdir.is_dir():
        for lock in sorted(pdir.glob("*.lock")):
            lm = _mtime(lock)
            if not _alive(lm, now):
                continue
            existing = by_key.get(lock.stem)
            if existing is not None:
                # lock이 db mtime보다 최신일 때만 끌어올린다 — 오래된 lock이
                # working을 rest로 다운그레이드하는 것을 방지 (다른 스캐너와 동일 규칙)
                if lm <= mtimes.get(lock.stem, 0.0):
                    continue
                mtimes[lock.stem] = lm
                existing.status = _base_status(lm, now)
                existing.event_at = _iso(lm)
            else:
                st = SessionState(
                    tool="gemini",
                    session_key=lock.stem,
                    project="",
                    title="",
                    status=_base_status(lm, now),
                    doing="gemini session",
                    ask="",
                    event_at=_iso(lm),
                    path=str(lock),
                )
                states.append(st)
                by_key[lock.stem] = st
    return states
