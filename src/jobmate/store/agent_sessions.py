# mypy: disable-error-code="attr-defined"
"""Agent session store methods extracted verbatim from db.py."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from .util import _clamp_limit, _dt, _iso, _now


class AgentSessionStoreMixin:
    def upsert_agent_session(
        self,
        tool: str,
        session_key: str,
        status: str,
        *,
        project: str | None = None,
        title: str | None = None,
        doing: str | None = None,
        ask: str | None = None,
        event_at: str | None = None,
        ended_summary: str | None = None,
        artifacts: list[str] | None = None,
        machine: str | None = None,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
    ) -> None:
        """(tool, session_key) 기준 upsert.

        선택 필드가 None이면 기존 값을 유지한다(COALESCE) — 브릿지가 부분
        상태만 보내도 이전 값이 지워지지 않게 하기 위함.
        tokens_in/out은 브릿지가 누적 카운터를 보내므로 MAX로 갱신한다.
        status/doing/ask/project/title이 실제로 바뀐 경우에만
        agent_session_events에 전이 이벤트를 남긴다 (60초 내 동일 이벤트는 생략)."""
        now_dt = _now()
        now = _iso(now_dt)
        artifacts_json = (
            json.dumps(artifacts, ensure_ascii=False) if artifacts is not None else None
        )
        with self.tx() as c:
            prev = c.execute(
                "SELECT status,project,title,doing,ask FROM agent_sessions"
                " WHERE tool=? AND session_key=?",
                (tool, session_key),
            ).fetchone()
            # COALESCE 의미와 동일하게 effective 값을 계산해 변경 여부 판정
            eff = {
                "status": status,
                "project": project if project is not None else (prev["project"] if prev else None),
                "title": title if title is not None else (prev["title"] if prev else None),
                "doing": doing if doing is not None else (prev["doing"] if prev else None),
                "ask": ask if ask is not None else (prev["ask"] if prev else None),
            }
            changed = prev is None or any(
                eff[k] != prev[k] for k in ("status", "project", "title", "doing", "ask")
            )
            c.execute(
                "INSERT INTO agent_sessions(id,tool,session_key,project,title,status,"
                "doing,ask,ended_summary,artifacts,machine,tokens_in,tokens_out,"
                "last_event_at,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT (tool,session_key) DO UPDATE SET"
                " project=COALESCE(excluded.project, agent_sessions.project),"
                " title=COALESCE(excluded.title, agent_sessions.title),"
                " status=excluded.status,"
                " doing=COALESCE(excluded.doing, agent_sessions.doing),"
                " ask=COALESCE(excluded.ask, agent_sessions.ask),"
                " ended_summary=COALESCE(excluded.ended_summary,"
                " agent_sessions.ended_summary),"
                " artifacts=COALESCE(excluded.artifacts, agent_sessions.artifacts),"
                " machine=COALESCE(excluded.machine, agent_sessions.machine),"
                " tokens_in=CASE WHEN excluded.tokens_in IS NOT NULL"
                " AND (agent_sessions.tokens_in IS NULL"
                " OR excluded.tokens_in>agent_sessions.tokens_in)"
                " THEN excluded.tokens_in ELSE agent_sessions.tokens_in END,"
                " tokens_out=CASE WHEN excluded.tokens_out IS NOT NULL"
                " AND (agent_sessions.tokens_out IS NULL"
                " OR excluded.tokens_out>agent_sessions.tokens_out)"
                " THEN excluded.tokens_out ELSE agent_sessions.tokens_out END,"
                " last_event_at=COALESCE(excluded.last_event_at,"
                " agent_sessions.last_event_at),"
                " updated_at=excluded.updated_at",
                (
                    uuid.uuid4().hex[:12],
                    tool,
                    session_key,
                    project,
                    title,
                    status,
                    doing,
                    ask,
                    ended_summary,
                    artifacts_json,
                    machine,
                    tokens_in,
                    tokens_out,
                    event_at,
                    now,
                    now,
                ),
            )
            if changed and not self._recent_duplicate_event(c, tool, session_key, eff, now_dt):
                c.execute(
                    "INSERT INTO agent_session_events(id,tool,session_key,status,"
                    "project,doing,ask,event_at,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        uuid.uuid4().hex[:12],
                        tool,
                        session_key,
                        eff["status"],
                        eff["project"],
                        eff["doing"],
                        eff["ask"],
                        event_at or now,
                        now,
                    ),
                )

    def _recent_duplicate_event(
        self,
        c: Any,
        tool: str,
        session_key: str,
        eff: dict[str, Any],
        now_dt: datetime,
    ) -> bool:
        """직전 이벤트와 status/doing/ask/project가 같고 60초 이내면 중복으로 간주."""
        row = c.execute(
            "SELECT status,doing,ask,project,created_at FROM agent_session_events"
            " WHERE tool=? AND session_key=?"
            " ORDER BY created_at DESC, id DESC LIMIT 1",
            (tool, session_key),
        ).fetchone()
        if row is None:
            return False
        if not (
            row["status"] == eff["status"]
            and (row["doing"] or None) == (eff["doing"] or None)
            and (row["ask"] or None) == (eff["ask"] or None)
            and (row["project"] or None) == (eff["project"] or None)
        ):
            return False
        prev_at = _dt(row["created_at"])
        if prev_at is None:
            return False
        if prev_at.tzinfo is None:
            prev_at = prev_at.replace(tzinfo=UTC)
        return (now_dt - prev_at).total_seconds() < self._SESSION_EVENT_DEDUP_SECONDS

    def get_agent_session(self, tool: str, session_key: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM agent_sessions WHERE tool=? AND session_key=?",
            (tool, session_key),
        ).fetchone()
        return dict(row) if row else None

    def list_agent_sessions(
        self,
        limit: int = 50,
        include_ended: bool = False,
        machine: str | None = None,
    ) -> list[dict[str, Any]]:
        """최근 이벤트 순 세션 목록. include_ended=False면 status='ended' 제외.
        machine 지정 시 해당 머신의 세션만 (정확 일치)."""
        sql = (
            "SELECT id,tool,session_key,project,title,status,doing,ask,"
            "ended_summary,artifacts,machine,tokens_in,tokens_out,"
            "last_event_at,created_at,updated_at"
            " FROM agent_sessions"
        )
        clauses: list[str] = []
        params: list[Any] = []
        if not include_ended:
            clauses.append("status != 'ended'")
        if machine is not None:
            clauses.append("machine=?")
            params.append(machine)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY last_event_at DESC NULLS LAST, updated_at DESC LIMIT ?"
        params.append(_clamp_limit(limit, 50, 500))
        rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def agent_session_history(
        self, tool: str, session_key: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        """세션의 상태 전이 이벤트를 시간순(오래된→최신)으로 반환."""
        rows = self._conn.execute(
            "SELECT id,tool,session_key,status,project,doing,ask,event_at,created_at"
            " FROM agent_session_events WHERE tool=? AND session_key=?"
            " ORDER BY created_at DESC, id DESC LIMIT ?",
            (tool, session_key, _clamp_limit(limit, 100, 500)),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def agent_session_stats(self, since: str | None = None) -> dict[str, Any]:
        """tool별 세션 통계.

        - sessions: agent_sessions 행 수 (전체 기간)
        - working_seconds: 이벤트 간격 추정 (working 상태 이벤트 → 다음 이벤트,
          간격은 최대 1시간으로 캡, 마지막 working 이벤트는 현재까지)
        - decisions/ended: 해당 status 이벤트 수
        - last_active: last_event_at/updated_at 중 최신 (ISO)
        since(ISO 날짜/시각) 지정 시 working/decision/ended 집계만 그 이후로 제한."""
        per: dict[str, dict[str, Any]] = {}

        def bucket(tool: str) -> dict[str, Any]:
            return per.setdefault(
                tool,
                {
                    "sessions": 0,
                    "working_seconds": 0.0,
                    "decisions": 0,
                    "ended": 0,
                    "last_active": "",
                },
            )

        sess_rows = self._conn.execute(
            "SELECT tool,status,last_event_at,updated_at FROM agent_sessions"
        ).fetchall()
        for r in sess_rows:
            b = bucket(r["tool"])
            b["sessions"] += 1
            ts = r["last_event_at"] or r["updated_at"] or ""
            if ts > b["last_active"]:
                b["last_active"] = ts

        ev_sql = (
            "SELECT tool,session_key,status,doing,ask,event_at,created_at FROM agent_session_events"
        )
        params: list[Any] = []
        if since:
            ev_sql += " WHERE created_at >= ?"
            params.append(since)
        ev_sql += " ORDER BY tool, session_key, created_at, id"
        rows = [dict(r) for r in self._conn.execute(ev_sql, params).fetchall()]

        now_dt = _now()
        prev_key: tuple[str, str] | None = None
        prev_t: datetime | None = None
        prev_status = ""
        last_working: dict[tuple[str, str], datetime] = {}
        for r in rows:
            tool = r["tool"]
            key = (tool, r["session_key"])
            b = bucket(tool)
            st = r["status"] or ""
            if st == "decision":
                b["decisions"] += 1
            elif st == "ended":
                b["ended"] += 1
            t = _dt(r["event_at"]) or _dt(r["created_at"])
            if t is not None and t.tzinfo is None:
                t = t.replace(tzinfo=UTC)
            if prev_key == key and prev_status == "working" and t and prev_t:
                gap = (t - prev_t).total_seconds()
                if gap > 0:
                    b["working_seconds"] += min(gap, self._WORKING_GAP_CAP_SECONDS)
            prev_key, prev_t, prev_status = key, t, st
            if t is not None:
                if st == "working":
                    last_working[key] = t
                else:
                    last_working.pop(key, None)
        # 마지막 이벤트가 working인 세션은 현재 시각까지의 간격을 추정에 포함
        for (tool, _sk), t in last_working.items():
            gap = (now_dt - t).total_seconds()
            if gap > 0:
                bucket(tool)["working_seconds"] += min(gap, self._WORKING_GAP_CAP_SECONDS)
        out: dict[str, Any] = {}
        for tool, b in per.items():
            out[tool] = {
                "sessions": b["sessions"],
                "working_seconds": int(b["working_seconds"]),
                "decisions": b["decisions"],
                "ended": b["ended"],
                "last_active": b["last_active"],
            }
        return out

    def session_work_seconds(self, since_iso: str) -> dict[str, Any]:
        """since 이후 윈도우의 tool/project별 세션 통계 (인사이트 뷰용).

        agent_session_events를 (tool, session_key)별로 시간순 정렬해
        agent_session_stats와 동일한 간격 추정으로 working_seconds를 계산한다
        (working 이벤트 → 다음 이벤트, 최대 _WORKING_GAP_CAP_SECONDS 캡,
        마지막 working 이벤트는 현재 시각까지). 윈도우 이전 이벤트는 로드하지
        않으므로 경계에서 시작된 working 구간은 누락될 수 있다(단순화).

        반환:
          tools:    {tool: {sessions, working_seconds, decisions}}
          projects: {project: {sessions, working_seconds}}
          total_sessions: 윈도우 내 이벤트가 있는 고유 (tool, session_key) 수
        project는 이벤트 값 우선, 없으면 agent_sessions의 현재 project,
        둘 다 없으면 "(no project)". 세션 수는 tool/project별 고유
        session_key 수 — 프로젝트를 오간 세션은 각 버킷에 모두 집계된다."""
        rows = [
            dict(r)
            for r in self._conn.execute(
                "SELECT tool,session_key,project,status,event_at,created_at"
                " FROM agent_session_events WHERE created_at >= ?"
                " ORDER BY tool, session_key, created_at, id",
                (since_iso,),
            ).fetchall()
        ]
        sess_project = {
            (r["tool"], r["session_key"]): r["project"]
            for r in self._conn.execute(
                "SELECT tool,session_key,project FROM agent_sessions"
            ).fetchall()
        }

        def project_of(tool: str, session_key: str, event_project: Any) -> str:
            return str(event_project or sess_project.get((tool, session_key)) or "(no project)")

        tools: dict[str, dict[str, Any]] = {}
        projects: dict[str, dict[str, Any]] = {}
        tool_sessions: dict[str, set[str]] = {}
        proj_sessions: dict[str, set[str]] = {}
        all_sessions: set[tuple[str, str]] = set()

        def t_bucket(tool: str) -> dict[str, Any]:
            return tools.setdefault(tool, {"sessions": 0, "working_seconds": 0.0, "decisions": 0})

        def p_bucket(project: str) -> dict[str, Any]:
            return projects.setdefault(project, {"sessions": 0, "working_seconds": 0.0})

        def add_work(tool: str, project: str, seconds: float) -> None:
            t_bucket(tool)["working_seconds"] += seconds
            p_bucket(project)["working_seconds"] += seconds

        now_dt = _now()
        prev_key: tuple[str, str] | None = None
        prev_t: datetime | None = None
        prev_status = ""
        prev_project = ""
        last_working: dict[tuple[str, str], tuple[datetime, str, str]] = {}
        for r in rows:
            tool = str(r["tool"])
            skey = str(r["session_key"])
            key = (tool, skey)
            project = project_of(tool, skey, r["project"])
            st = str(r["status"] or "")
            all_sessions.add(key)
            tool_sessions.setdefault(tool, set()).add(skey)
            proj_sessions.setdefault(project, set()).add(skey)
            if st == "decision":
                t_bucket(tool)["decisions"] += 1
            t = _dt(r["event_at"]) or _dt(r["created_at"])
            if t is not None and t.tzinfo is None:
                t = t.replace(tzinfo=UTC)
            if prev_key == key and prev_status == "working" and t and prev_t:
                gap = (t - prev_t).total_seconds()
                if gap > 0:
                    add_work(tool, prev_project, min(gap, self._WORKING_GAP_CAP_SECONDS))
            prev_key, prev_t, prev_status, prev_project = key, t, st, project
            if t is not None:
                if st == "working":
                    last_working[key] = (t, tool, project)
                else:
                    last_working.pop(key, None)
        # 마지막 이벤트가 working인 세션은 현재 시각까지의 간격을 추정에 포함
        for _key, (t, tool, project) in last_working.items():
            gap = (now_dt - t).total_seconds()
            if gap > 0:
                add_work(tool, project, min(gap, self._WORKING_GAP_CAP_SECONDS))

        for tool, b in tools.items():
            b["sessions"] = len(tool_sessions.get(tool, ()))
            b["working_seconds"] = int(b["working_seconds"])
        for project, b in projects.items():
            b["sessions"] = len(proj_sessions.get(project, ()))
            b["working_seconds"] = int(b["working_seconds"])
        return {
            "tools": tools,
            "projects": projects,
            "total_sessions": len(all_sessions),
        }

    def agent_session_hourly(self, day: str) -> list[dict[str, Any]]:
        """로컬 날짜(YYYY-MM-DD)의 시간대별 working/decision 초 — 24개 버킷.

        agent_session_events를 (tool, session_key)별로 시간순 정렬해,
        working/decision 이벤트의 event_at부터 다음 이벤트의 created_at까지의
        구간을 해당 로컬 날짜로 클립해 시간 버킷에 합산한다. 다음 이벤트가
        없으면 그날의 끝(미래면 현재 시각)까지로 본다."""
        try:
            d = datetime.fromisoformat(day)
        except ValueError as exc:
            raise ValueError(f"bad day: {day!r}") from exc
        # naive → 로컬 자정 aware
        day_start = datetime(d.year, d.month, d.day).astimezone()
        day_end = day_start + timedelta(days=1)
        end_cap = min(day_end, _now())
        rows = self._conn.execute(
            "SELECT tool,session_key,status,event_at,created_at"
            " FROM agent_session_events"
            " ORDER BY tool, session_key, created_at, id"
        ).fetchall()
        per: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for r in rows:
            row = dict(r)
            per.setdefault((row["tool"], row["session_key"]), []).append(row)
        seconds = [0.0] * 24
        for evs in per.values():
            for i, ev in enumerate(evs):
                if (ev.get("status") or "") not in ("working", "decision"):
                    continue
                start = _dt(ev.get("event_at")) or _dt(ev.get("created_at"))
                if start is None:
                    continue
                if start.tzinfo is None:
                    start = start.replace(tzinfo=UTC)
                nxt = evs[i + 1] if i + 1 < len(evs) else None
                end = _dt(nxt.get("created_at")) if nxt else None
                if end is not None and end.tzinfo is None:
                    end = end.replace(tzinfo=UTC)
                if end is None or end > end_cap:
                    end = end_cap
                # 해당 로컬 날짜로 클립
                s = max(start, day_start)
                e = min(end, day_end)
                if e <= s:
                    continue
                cur = s.astimezone()
                end_local = e.astimezone()
                while cur < end_local:
                    nxt_hour = cur.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
                    seg_end = min(nxt_hour, end_local)
                    seconds[cur.hour] += (seg_end - cur).total_seconds()
                    cur = seg_end
        return [{"hour": h, "working_seconds": int(round(seconds[h]))} for h in range(24)]

    # ---- llm_usage (LLM 토큰 사용량) ----
    def record_session_activity(
        self,
        tool: str,
        session_key: str,
        status: str,
        doing: str | None = None,
        project: str | None = None,
    ) -> None:
        """push된 세션 상태를 활동 로그에 적재. status/doing이 바뀌었거나
        마지막 기록 후 60초가 지나야만 insert(스로틀). 가끔 7일 이전 행을 정리."""
        import secrets

        now_dt = _now()
        now = _iso(now_dt)
        with self.tx() as c:
            row = c.execute(
                "SELECT status,doing,recorded_at FROM session_activity"
                " WHERE tool=? AND session_key=?"
                " ORDER BY recorded_at DESC, id DESC LIMIT 1",
                (tool, session_key),
            ).fetchone()
            if row is not None:
                prev_at = _dt(row["recorded_at"])
                same = (row["status"] or "") == (status or "") and (row["doing"] or None) == (
                    doing or None
                )
                if same and prev_at is not None:
                    if prev_at.tzinfo is None:
                        prev_at = prev_at.replace(tzinfo=UTC)
                    if (now_dt - prev_at).total_seconds() < self._ACTIVITY_THROTTLE_SECONDS:
                        return
            c.execute(
                "INSERT INTO session_activity(tool,session_key,status,doing,project,"
                "recorded_at) VALUES(?,?,?,?,?,?)",
                (tool, session_key, status, doing, project, now),
            )
            # 저비용 보존 정책 — insert마다 ~2% 확률로 7일 이전 행 삭제
            if secrets.randbelow(50) == 0:
                cutoff = _iso(now_dt - timedelta(days=self._ACTIVITY_RETENTION_DAYS))
                c.execute("DELETE FROM session_activity WHERE recorded_at < ?", (cutoff,))

    def session_activity(
        self, tool: str, session_key: str, limit: int = 200
    ) -> list[dict[str, Any]]:
        """세션 활동을 시간순(오래된→최신)으로 반환."""
        rows = self._conn.execute(
            "SELECT recorded_at,status,doing,project FROM session_activity"
            " WHERE tool=? AND session_key=?"
            " ORDER BY recorded_at DESC, id DESC LIMIT ?",
            (tool, session_key, _clamp_limit(limit, 200, 500)),
        ).fetchall()
        return [
            {
                "at": r["recorded_at"],
                "status": r["status"],
                "doing": r["doing"],
                "project": r["project"],
            }
            for r in reversed(rows)
        ]

    # ---- reports (daily/weekly 리포트) ----
    def find_agent_session_by_key(self, session_key: str) -> dict[str, Any] | None:
        """session_key만으로 세션 조회 — 복수 tool이 같은 key를 쓰면 최신 updated_at 우선."""
        row = self._conn.execute(
            "SELECT * FROM agent_sessions WHERE session_key=?"
            " ORDER BY updated_at DESC, last_event_at DESC LIMIT 1",
            (session_key,),
        ).fetchone()
        return dict(row) if row else None

    def set_agent_session_project(
        self, session_key: str, project: str, tool: str | None = None
    ) -> dict[str, Any] | None:
        """세션 project 태그 설정 (PATCH /agent-sessions/{key}).

        tool이 주어지면 (tool, session_key) 정확 매칭, 아니면 최신 세션을 고른다.
        변경 시 agent_session_events에 project 전이 이벤트 1건을 남긴다.
        반환: 갱신된 세션 행 또는 None(세션 없음)."""
        sess = (
            self.get_agent_session(tool, session_key)
            if tool
            else self.find_agent_session_by_key(session_key)
        )
        if sess is None:
            return None
        new_project = project.strip()[:500] if project else ""
        old_project = sess.get("project") or ""
        now = _iso(_now())
        with self.tx() as c:
            c.execute(
                "UPDATE agent_sessions SET project=?, updated_at=? WHERE tool=? AND session_key=?",
                (new_project or None, now, sess["tool"], sess["session_key"]),
            )
            if new_project != old_project:
                c.execute(
                    "INSERT INTO agent_session_events(id,tool,session_key,status,"
                    "project,doing,ask,event_at,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        uuid.uuid4().hex[:12],
                        sess["tool"],
                        sess["session_key"],
                        sess.get("status") or "",
                        new_project or None,
                        sess.get("doing"),
                        sess.get("ask"),
                        now,
                        now,
                    ),
                )
        return self.get_agent_session(sess["tool"], sess["session_key"])
