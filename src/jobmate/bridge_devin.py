"""jobMate agent-bridge — Devin CLI 스캐너.

agent_bridge.py에서 분리된 코드 모션 리팩터링 — 동작 변경 없음.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from pathlib import Path
from typing import Any

from .bridge_common import (
    ENDED_SECONDS,
    SUBAGENT_BUSY_SECONDS,
    SessionState,
    _alive,
    _base_status,
    _clip,
    _is_decision_tool,
    _iso,
    _mtime,
)

logger = logging.getLogger("jobmate.agent_bridge")


def _devin_subagent_titles(
    con: sqlite3.Connection, session_ids: list[str]
) -> dict[str, dict[str, str]]:
    """sessions.db에서 session_id -> {agent_id: title} 맵을 만든다.

    subagent_heads에는 제목이 없으므로, message_nodes의 spawn 결과
    ("subagent started with agent_id=…", tool_call_id 포함)를 tool_call_state의
    rawInput.title과 조인해 복원한다. 테이블/컬럼이 없는 구버전 DB는 빈 맵."""
    out: dict[str, dict[str, str]] = {}
    if not session_ids:
        return out
    # 세션별 개별 쿼리(N+1) 대신 IN 절 단일 쿼리 + LIMIT.
    # sid 수가 비정상적으로 크면 쿼리가 비대해지므로 상한을 둔다.
    sids = list(dict.fromkeys(session_ids))[:500]
    try:
        ph = ",".join("?" * len(sids))
        rows = con.execute(
            "SELECT session_id, chat_message FROM message_nodes"  # noqa: S608 - placeholder만 생성, 값은 바인딩
            f" WHERE session_id IN ({ph})"
            " AND chat_message LIKE '%subagent started with agent_id=%'"
            " LIMIT 2000",
            tuple(sids),
        ).fetchall()
    except sqlite3.Error:
        return out
    pending: list[tuple[str, str, str]] = []  # (session_id, agent_id, tool_call_id)
    for sid, msg in rows:
        if not isinstance(msg, str):
            continue
        m = re.search(r"subagent started with agent_id=([0-9a-f]{8})", msg)
        t = re.search(r'"tool_call_id"\s*:\s*"(call_[^"]+)"', msg)
        if m and t:
            pending.append((sid, m.group(1), t.group(1)))
    if not pending:
        return out
    # tool_call_state 조회도 (session_id, tool_call_id) 쌍 단위 배치 —
    # pending 건별 개별 쿼리(N+1) 대신 IN 절 2개로 한 번에 가져온다.
    pair_sids = sorted({str(s) for s, _, _ in pending})
    call_ids = sorted({c for _, _, c in pending})
    lookup: dict[tuple[str, str], Any] = {}
    try:
        ph_s = ",".join("?" * len(pair_sids))
        ph_c = ",".join("?" * len(call_ids))
        tc_rows = con.execute(
            "SELECT session_id, tool_call_id, tool_call_json FROM tool_call_state"  # noqa: S608 - placeholder만 생성, 값은 바인딩
            f" WHERE session_id IN ({ph_s}) AND tool_call_id IN ({ph_c})"
            " LIMIT 2000",
            tuple(pair_sids) + tuple(call_ids),
        ).fetchall()
        for s, c, j in tc_rows:
            lookup[(str(s), str(c))] = j
    except sqlite3.Error:
        pass
    for sid, agent_id, call_id in pending:
        title = ""
        raw = lookup.get((str(sid), call_id))
        if raw:
            try:
                tj = json.loads(raw)
                title = str((tj.get("rawInput") or {}).get("title") or "")
            except (json.JSONDecodeError, AttributeError, TypeError):
                title = ""
        out.setdefault(str(sid), {})[agent_id] = _clip(title, 60)
    return out


# ---------------------------------------------------------------------------
# Devin CLI: ~/.local/share/devin/cli (transcripts/*.json + session_locks/)
# ---------------------------------------------------------------------------


# ATIF transcript는 단일 JSON — head/tail 부분 파싱이 불가하므로
# 비정상적으로 큰 파일은 로드 자체를 포기한다(스캔 지연·메모리 보호).
_DEVIN_MAX_TRANSCRIPT_BYTES = 32 * 1024 * 1024
_devin_lock_warned = False


def scan_devin(root: Path, now: float) -> list[SessionState]:
    """root = ~/.local/share/devin/cli 디렉터리.

    transcripts/*.json (ATIF) + session_locks/<stem>.lock 존재 여부로
    활성 세션을 판별한다. lock이 없는 transcript는 ended이므로 반환하지 않는다
    (Bridge가 이전 스캔 대비 사라진 세션을 ended로 push한다).

    session_locks/ 디렉터리 자체가 없으면 lock 기능을 지원하지 않는
    버전으로 간주한다 — 이 경우 transcript를 전부 활성으로 처리하면
    종료된 세션이 계속 살아있는 것처럼 보이므로, transcript는 수집하지
    않는다(ended로 수렴). sessions.db 기반 세션은 영향 없음.
    """
    global _devin_lock_warned
    states: list[SessionState] = []
    tdir = root / "transcripts"
    ldir = root / "session_locks"
    mtimes: dict[str, float] = {}  # session_key → 가장 최신 활동 시각
    if not tdir.is_dir():
        return states
    lock_supported = ldir.is_dir()
    if not lock_supported and tdir.is_dir() and not _devin_lock_warned:
        _devin_lock_warned = True
        logger.info(
            "devin session_locks dir missing at %s — transcripts treated as ended",
            ldir,
        )
    for path in sorted(tdir.glob("*.json")):
        if not path.is_file():
            continue
        mtime = _mtime(path)
        if not _alive(mtime, now):
            continue
        if not lock_supported or not (ldir / f"{path.stem}.lock").exists():
            continue
        try:
            if path.stat().st_size > _DEVIN_MAX_TRANSCRIPT_BYTES:
                logger.warning(
                    "devin transcript too large (%d bytes > %d), skipping %s",
                    path.stat().st_size,
                    _DEVIN_MAX_TRANSCRIPT_BYTES,
                    path,
                )
                continue
        except OSError:
            continue
        mtimes[path.stem] = mtime
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
                # 빈 dict/list는 결과 없음과 동일하게 취급
                has_result = observation is not None and observation not in ("", {}, [])
                if isinstance(calls, list) and calls and not has_result:
                    # step에 tool call이 여러 개면 decision 도구가 먼저 와도 잡는다
                    fns = [
                        str(c.get("function_name") or "tool") for c in calls if isinstance(c, dict)
                    ]
                    pending_tool = next(
                        (f for f in fns if _is_decision_tool(f)), fns[0] if fns else "tool"
                    )
                else:
                    pending_tool = ""
                doing = _clip(msg, 80) or doing

        decision = _is_decision_tool(pending_tool)
        states.append(
            SessionState(
                tool="devin",
                session_key=path.stem,
                project="",
                title=title,
                status="decision" if decision else _base_status(mtime, now),
                doing=doing or pending_tool,
                ask=pending_tool if decision else "",
                event_at=_iso(mtime),
                path=str(path),
            )
        )

    # sessions.db — Devin 데스크톱/클라우드 세션 목록. transcript가 없어도
    # 최근 활동이 있는 세션을 잡고, transcript가 있으면 project/title을 채운다.
    by_key = {s.session_key: s for s in states}
    db_path = root / "sessions.db"
    sub_heads: list[tuple[Any, ...]] = []
    sub_titles: dict[str, dict[str, str]] = {}
    if db_path.is_file():
        try:
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
            try:
                tables = {
                    r[0]
                    for r in con.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                if "sessions" in tables:
                    rows = con.execute(
                        "SELECT id, title, working_directory, last_activity_at"
                        " FROM sessions WHERE hidden = 0"
                        " ORDER BY last_activity_at DESC LIMIT 50"
                    ).fetchall()
                else:
                    rows = []
                if "subagent_heads" in tables:
                    sub_heads = con.execute(
                        "SELECT session_id, agent_id, updated_at FROM subagent_heads"
                    ).fetchall()
                    head_sids = sorted({str(h[0]) for h in sub_heads if h[0]})
                    if head_sids:
                        sub_titles = _devin_subagent_titles(con, head_sids)
            finally:
                con.close()
        except sqlite3.Error as e:
            logger.debug("devin sessions.db read failed %s: %s", db_path, e)
            rows = []
        for sid, title, wd, activity in rows:
            if not sid:
                continue
            try:
                act = float(activity or 0)
            except (TypeError, ValueError):
                continue
            if act <= 0 or (now - act) > ENDED_SECONDS:
                continue
            project = Path(wd).name if isinstance(wd, str) and wd else ""
            existing = by_key.get(sid)
            if existing is not None:
                if not existing.project:
                    existing.project = project
                if not existing.title and title:
                    existing.title = _clip(str(title), 60)
                # sessions.db last_activity_at 이 transcript mtime 보다 최신이면
                # recency를 끌어올린다 — 긴 도구 실행 중 transcript가 멈춰도
                # working이 유지되도록 (decision 상태는 더 강한 신호라 유지)
                if act > mtimes.get(sid, 0.0):
                    mtimes[sid] = act
                    if existing.status != "decision":
                        existing.status = _base_status(act, now)
                    existing.event_at = _iso(act)
                continue
            st = SessionState(
                tool="devin",
                session_key=sid,
                project=project,
                title=_clip(str(title or ""), 60),
                status=_base_status(act, now),
                doing="",
                ask="",
                event_at=_iso(act),
                path=str(db_path),
            )
            states.append(st)
            by_key[sid] = st

    # subagent_heads — 백그라운드 서브에이전트를 가상 세션으로 노출하고,
    # 활동 중인 서브에이전트가 있으면 부모 세션을 working으로 승격한다.
    # session_key는 "<session>:sub:<agent_id>" — path param 안전 문자만 사용.
    active_by_parent: dict[str, int] = {}
    emitted: set[str] = set()
    for h in sub_heads:
        sid, agent_id, upd = h[0], h[1], h[2]
        if not sid or not agent_id:
            continue
        try:
            upd_f = float(upd or 0)
        except (TypeError, ValueError):
            continue
        if upd_f <= 0 or (now - upd_f) > ENDED_SECONDS:
            continue
        parent = by_key.get(str(sid))
        if parent is None:
            continue
        busy = (now - upd_f) <= SUBAGENT_BUSY_SECONDS
        if busy:
            active_by_parent[str(sid)] = active_by_parent.get(str(sid), 0) + 1
        vkey = f"{sid}:sub:{agent_id}"
        if vkey in emitted:
            continue
        emitted.add(vkey)
        states.append(
            SessionState(
                tool="devin",
                session_key=vkey,
                project=parent.project,
                title=(sub_titles.get(str(sid)) or {}).get(str(agent_id), "")
                or f"subagent {agent_id}",
                status="working" if busy else "rest",
                doing="",
                ask="",
                event_at=_iso(upd_f),
                path=str(db_path),
            )
        )
    for sid, n in active_by_parent.items():
        parent = by_key.get(sid)
        if parent is not None and parent.status == "rest":
            parent.status = "working"
            parent.doing = _clip(f"{parent.doing} · sub×{n}" if parent.doing else f"sub×{n}", 80)
    return states
