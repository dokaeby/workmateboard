# mypy: disable-error-code="attr-defined"
"""Agent management 스토어 메서드 믹스인 (db.py Store에서 추출).

paperclip-agent-management-spec 1차 — tasks / agent_events / task_runs.
task는 업무의 영속 소유권, run은 실행 시도 — task_runs 복합 PK가 둘을
연결한다. 상태 전이 검증은 서비스 계층(models.management)이 담당하고,
여기서는 원자적 쓰기·멱등 조회만 제공한다.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from ..models import TaskPriority, TaskStatus
from .util import _clamp_limit, _iso, _now

_TASK_ID_RE = re.compile(r"^WMB-(\d+)$")
_TASK_STATUSES = {s.value for s in TaskStatus}
_TASK_PRIORITIES = {s.value for s in TaskPriority}


class ManagementStoreMixin:
    # ---- tasks ----
    _TASK_JSON_NULLABLE = (
        "assignee_agent_id",
        "reviewer_agent_id",
        "parent_id",
        "project_id",
        "goal_id",
        "current_run_id",
        "checkout_agent_id",
        "checkout_at",
    )

    @staticmethod
    def _task_json(r: Any) -> dict[str, Any]:
        d = dict(r)
        for k in ManagementStoreMixin._TASK_JSON_NULLABLE:
            if d.get(k) is None:
                d[k] = None
        st = str(d.get("status") or "").strip().lower()
        d["status"] = st if st in _TASK_STATUSES else TaskStatus.BACKLOG.value
        pr = str(d.get("priority") or "").strip().lower()
        d["priority"] = pr if pr in _TASK_PRIORITIES else TaskPriority.MEDIUM.value
        d["title"] = str(d.get("title") or "")[:200]
        d["description"] = str(d.get("description") or "")[:8000]
        return d

    @staticmethod
    def _next_task_identifier(c: Any) -> str:
        """'WMB-<n>' 채번 — tx() 안에서 호출해 직렬화된 스캔+삽입을 보장한다.

        store._lock이 쓰기 트랜잭션 전체를 직렬화하므로 같은 Store 인스턴스
        안에서는 MAX+1 계산이 안전하다(인스턴스 간 경합은 UNIQUE 제약이
        최후 방어선 — create_task가 integrity 오류 시 1회 재시도)."""
        rows = c.execute("SELECT identifier FROM tasks WHERE identifier LIKE 'WMB-%'").fetchall()
        mx = 0
        for r in rows:
            m = _TASK_ID_RE.match(str(r["identifier"] or ""))
            if m:
                mx = max(mx, int(m.group(1)))
        return f"WMB-{mx + 1}"

    def create_task(
        self,
        title: str,
        *,
        description: str = "",
        status: str = "backlog",
        priority: str = "medium",
        assignee_agent_id: str | None = None,
        reviewer_agent_id: str | None = None,
        parent_id: str | None = None,
        project_id: str | None = None,
        goal_id: str | None = None,
        created_by: str = "",
    ) -> dict[str, Any] | None:
        """task 생성 — title 필수(≤200자). 실패 시 None.

        project_id/goal_id 참조 존재 검증은 서비스 계층
        (agent_management.create_task)이 담당한다."""
        title = title.strip()[:200]
        if not title:
            return None
        st = (status or "").strip().lower()[:20] or TaskStatus.BACKLOG.value
        pr = (priority or "").strip().lower()[:20] or TaskPriority.MEDIUM.value
        now = _iso(_now())
        tid = uuid.uuid4().hex[:12]
        for _attempt in range(2):
            try:
                with self.tx() as c:
                    ident = self._next_task_identifier(c)
                    c.execute(
                        "INSERT INTO tasks(id,identifier,title,description,status,"
                        "priority,assignee_agent_id,reviewer_agent_id,parent_id,"
                        "project_id,goal_id,"
                        "created_by,created_at,updated_at)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            tid,
                            ident,
                            title,
                            description.strip()[:8000],
                            st,
                            pr,
                            (assignee_agent_id or "").strip()[:64] or None,
                            (reviewer_agent_id or "").strip()[:64] or None,
                            (parent_id or "").strip()[:64] or None,
                            (project_id or "").strip()[:64] or None,
                            (goal_id or "").strip()[:64] or None,
                            created_by[:200],
                            now,
                            now,
                        ),
                    )
                return self.get_task(tid)
            except self._integrity_errors:
                # identifier 채번 경합 — 새 id/identifier로 1회 재시도
                tid = uuid.uuid4().hex[:12]
                continue
        return None

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        """id 또는 identifier('WMB-1')로 task 조회."""
        row = self._conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None and _TASK_ID_RE.match(str(task_id or "")):
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE identifier=?", (task_id.strip(),)
            ).fetchone()
        return self._task_json(row) if row else None

    def list_tasks(
        self,
        *,
        status: str | None = None,
        assignee_agent_id: str | None = None,
        project_id: str | None = None,
        goal_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM tasks"
        conds: list[str] = []
        params: list[Any] = []
        if status:
            conds.append("status=?")
            params.append(status.strip().lower()[:20])
        if assignee_agent_id:
            conds.append("assignee_agent_id=?")
            params.append(assignee_agent_id.strip()[:64])
        if project_id:
            conds.append("project_id=?")
            params.append(project_id.strip()[:64])
        if goal_id:
            conds.append("goal_id=?")
            params.append(goal_id.strip()[:64])
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(_clamp_limit(limit, 50, 200))
        return [self._task_json(r) for r in self._conn.execute(sql, params).fetchall()]

    def update_task(self, task_id: str, **fields: Any) -> dict[str, Any] | None:
        """부분 갱신 — title/description/status/priority/assignee_agent_id/
        reviewer_agent_id/parent_id/project_id/goal_id/current_run_id.
        None = task 없음. project_id/goal_id의 참조 존재 검증은 서비스
        계층(agent_management.patch_task)이 담당한다."""
        task = self.get_task(task_id)
        if task is None:
            return None
        updates: dict[str, Any] = {}
        if "title" in fields:
            t = str(fields["title"] or "").strip()[:200]
            if not t:
                return None
            updates["title"] = t
        if "description" in fields:
            updates["description"] = str(fields["description"] or "").strip()[:8000]
        if "status" in fields:
            st = str(fields["status"] or "").strip().lower()[:20]
            updates["status"] = st or TaskStatus.BACKLOG.value
        if "priority" in fields:
            pr = str(fields["priority"] or "").strip().lower()[:20]
            updates["priority"] = pr or TaskPriority.MEDIUM.value
        if "assignee_agent_id" in fields:
            v = fields["assignee_agent_id"]
            updates["assignee_agent_id"] = str(v).strip()[:64] or None if v else None
        if "reviewer_agent_id" in fields:
            v = fields["reviewer_agent_id"]
            updates["reviewer_agent_id"] = str(v).strip()[:64] or None if v else None
        if "parent_id" in fields:
            v = fields["parent_id"]
            pid = str(v).strip()[:64] or None if v else None
            # 자기 참조 방지 — 순환은 서비스 계층이 체인을 검증한다.
            if pid is not None and pid == task["id"]:
                return None
            updates["parent_id"] = pid
        if "project_id" in fields:
            v = fields["project_id"]
            updates["project_id"] = str(v).strip()[:64] or None if v else None
        if "goal_id" in fields:
            v = fields["goal_id"]
            updates["goal_id"] = str(v).strip()[:64] or None if v else None
        if "current_run_id" in fields:
            v = fields["current_run_id"]
            updates["current_run_id"] = str(v).strip()[:64] or None if v else None
        if not updates:
            return task
        sets = ", ".join(f"{k}=?" for k in updates)
        with self.tx() as c:
            c.execute(
                f"UPDATE tasks SET {sets}, updated_at=? WHERE id=?",  # noqa: S608
                (*updates.values(), _iso(_now()), task["id"]),
            )
        return self.get_task(task["id"])

    def checkout_task(self, task_id: str, agent_id: str) -> bool:
        """atomic checkout — checkout_agent_id가 비어 있거나 같은 agent일 때만 성공.

        동시 checkout 경합은 UPDATE의 WHERE 절이 직렬화한다 — 같은 Store의
        tx() 락 + 행 단위 조건 갱신으로 정확히 하나만 rowcount>0을 얻는다."""
        now = _iso(_now())
        with self.tx() as c:
            cur = c.execute(
                "UPDATE tasks SET checkout_agent_id=?, checkout_at=?, updated_at=?"
                " WHERE id=? AND status NOT IN ('done','cancelled')"
                " AND (checkout_agent_id IS NULL OR checkout_agent_id=?)",
                (agent_id, now, now, task_id, agent_id),
            )
        return int(getattr(cur, "rowcount", 0) or 0) > 0

    def release_task_checkout(self, task_id: str) -> bool:
        """checkout 해제 — run 종결/수동 해제 시 호출."""
        with self.tx() as c:
            cur = c.execute(
                "UPDATE tasks SET checkout_agent_id=NULL, checkout_at=NULL,"
                " updated_at=? WHERE id=?",
                (_iso(_now()), task_id),
            )
        return int(getattr(cur, "rowcount", 0) or 0) > 0

    def current_task_for_agent(self, agent_id: str) -> dict[str, Any] | None:
        """agent의 현재 task — checkout 중이거나, 배정받았고 활성 run이
        연결된(task_runs/current_run_id) 미종결 task. Office '현재 task'
        표시용.

        workflow wake는 checkout을 거치지 않고 task_runs/current_run_id만
        설정하므로 assignee+활성 run 경로도 함께 조회한다(H3). current_run_id가
        가리키는 run이 이미 종결이면(투영 누락 잔여) 현재 작업으로 보지 않는다."""
        aid = agent_id.strip()[:64]
        row = self._conn.execute(
            "SELECT t.* FROM tasks t"
            " WHERE t.status NOT IN ('done','cancelled')"
            " AND ("
            "  t.checkout_agent_id = ?"
            "  OR (t.assignee_agent_id = ? AND EXISTS ("
            "   SELECT 1 FROM runs r WHERE r.id = t.current_run_id"
            "   AND r.status IN ('queued','running','waiting_approval','waiting_review')"
            "  ))"
            " )"
            " ORDER BY t.updated_at DESC LIMIT 1",
            (aid, aid),
        ).fetchone()
        return self._task_json(row) if row else None

    def task_effective_goal_id(self, task: dict[str, Any] | None) -> str:
        """task의 effective goal — spec 3차 A: task.goal_id → project.goal_id
        → '' 순서로 해석한다. run 태깅 폴백(store/runs.py)과 조회 API가
        공유하는 단일 경로."""
        if not task:
            return ""
        gid = str(task.get("goal_id") or "").strip()
        if gid:
            return gid[:64]
        pid = str(task.get("project_id") or "").strip()
        if pid:
            proj = self.get_project(pid)
            if proj is not None:
                return str(proj.get("goal_id") or "").strip()[:64]
        return ""

    # ---- task_runs (task↔run 링크) ----
    def link_task_run(self, task_id: str, run_id: str) -> dict[str, Any]:
        """task↔run 연결 — (task_id,run_id) UNIQUE로 멱등.

        attempt는 해당 task의 기존 링크 수 + 1. 이미 연결돼 있으면 기존 행 반환."""
        now = _iso(_now())
        with self.tx() as c:
            cnt = c.execute(
                "SELECT COUNT(*) AS n FROM task_runs WHERE task_id=?", (task_id,)
            ).fetchone()
            attempt = int(cnt["n"] if cnt is not None else 0) + 1
            try:
                c.execute(
                    "INSERT INTO task_runs(task_id,run_id,attempt,created_at) VALUES(?,?,?,?)",
                    (task_id, run_id, attempt, now),
                )
            except self._integrity_errors:
                pass  # 이미 링크됨 — 아래 조회가 기존 행을 반환
        row = self._conn.execute(
            "SELECT * FROM task_runs WHERE task_id=? AND run_id=?", (task_id, run_id)
        ).fetchone()
        return dict(row) if row is not None else {"task_id": task_id, "run_id": run_id}

    def task_runs_for_task(self, task_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM task_runs WHERE task_id=? ORDER BY created_at, attempt",
            (task_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def task_runs_for_run(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM task_runs WHERE run_id=? ORDER BY created_at",
            (run_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def tasks_for_run(self, run_id: str) -> list[dict[str, Any]]:
        """run에 연결된 task 전체 — run→task 상태 투영용."""
        rows = self._conn.execute(
            "SELECT t.* FROM tasks t JOIN task_runs tr ON tr.task_id = t.id"
            " WHERE tr.run_id=? ORDER BY tr.created_at",
            (run_id,),
        ).fetchall()
        return [self._task_json(r) for r in rows]

    # ---- agent_events (통합 이벤트 로그, idempotency_key 멱등) ----
    @staticmethod
    def _event_json(r: Any) -> dict[str, Any]:
        d = dict(r)
        try:
            detail = json.loads(d.get("detail") or "{}")
            d["detail"] = detail if isinstance(detail, dict) else {}
        except (ValueError, TypeError):
            d["detail"] = {}
        return d

    def append_agent_event(
        self,
        *,
        idempotency_key: str,
        event_type: str,
        agent_id: str | None = None,
        task_id: str | None = None,
        run_id: str | None = None,
        actor: str = "",
        summary: str = "",
        detail: dict[str, Any] | None = None,
        occurred_at: str | None = None,
    ) -> tuple[dict[str, Any] | None, bool]:
        """이벤트 추가 — (event, created) 반환.

        idempotency_key UNIQUE: 동일 키 재전송은 기존 행을 반환하고
        created=False — 호출자는 재생 응답을 기존 결과에서 복원한다."""
        key = (idempotency_key or "").strip()[:200]
        if not key:
            key = f"auto:{uuid.uuid4().hex}"
        now = _iso(_now())
        eid = uuid.uuid4().hex[:12]
        # detail은 TEXT 컬럼 — 직렬화 결과를 그대로 자르면 JSON이 깨져
        # _event_json이 detail={}로 되돌린다(정보 유실). 8000자를 넘으면
        # 원문 JSON의 미리보기 문자열을 담은 유효 JSON 래퍼로 대체한다 —
        # 손실은 있지만 파싱은 항상 성공한다.
        detail_json = json.dumps(detail or {}, ensure_ascii=False, default=str)
        if len(detail_json) > 8000:
            detail_json = json.dumps(
                {
                    "_truncated": True,
                    "chars": len(detail_json),
                    "preview": detail_json[:7900],
                },
                ensure_ascii=False,
            )
        try:
            with self.tx() as c:
                c.execute(
                    "INSERT INTO agent_events(id,idempotency_key,event_type,"
                    "agent_id,task_id,run_id,actor,summary,detail,"
                    "occurred_at,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        eid,
                        key,
                        event_type.strip().lower()[:40],
                        (agent_id or "").strip()[:64] or None,
                        (task_id or "").strip()[:64] or None,
                        (run_id or "").strip()[:64] or None,
                        actor[:200],
                        summary.strip()[:200],
                        detail_json,
                        occurred_at or now,
                        now,
                    ),
                )
        except self._integrity_errors:
            existing = self.get_agent_event_by_key(key)
            if existing is not None:
                return existing, False
            raise
        return self.get_agent_event_by_key(key), True

    def get_agent_event_by_key(self, idempotency_key: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM agent_events WHERE idempotency_key=?",
            ((idempotency_key or "").strip()[:200],),
        ).fetchone()
        return self._event_json(row) if row else None

    def get_agent_event(self, event_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM agent_events WHERE id=?", (event_id,)).fetchone()
        return self._event_json(row) if row else None

    def list_agent_events(
        self,
        *,
        agent_id: str | None = None,
        task_id: str | None = None,
        run_id: str | None = None,
        after: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """이벤트 조회 — 최신순(created_at DESC, id DESC) 고정.

        after: 커서 — 기존 이벤트 id이면 그 행보다 최신 이벤트만
        (created_at DESC, id DESC 순서 기준, 동일 타임스탬프는 id로 구분),
        ISO 타임스탬프면 그 시각 이후(>)만 반환. 해석 불가 값은 무시한다."""
        sql = "SELECT * FROM agent_events"
        conds: list[str] = []
        params: list[Any] = []
        if agent_id:
            conds.append("agent_id=?")
            params.append(agent_id.strip()[:64])
        if task_id:
            conds.append("task_id=?")
            params.append(task_id.strip()[:64])
        if run_id:
            conds.append("run_id=?")
            params.append(run_id.strip()[:64])
        cursor = self._resolve_event_cursor(after)
        if cursor is not None:
            ts, cid = cursor
            if cid:
                # 'after'는 커서보다 최신 이벤트 — 정렬이
                # (created_at DESC, id DESC)이므로 같은 created_at에서는
                # id가 큰 행이 먼저 온다. id 타이브레이커로 동일
                # 타임스탬프 이벤트의 누락을 막는다.
                conds.append("(created_at > ? OR (created_at = ? AND id > ?))")
                params.extend([ts, ts, cid])
            elif ts:
                conds.append("created_at > ?")
                params.append(ts)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(_clamp_limit(limit, 50, 200))
        return [self._event_json(r) for r in self._conn.execute(sql, params).fetchall()]

    def _resolve_event_cursor(self, after: str | None) -> tuple[str, str] | None:
        """after 커서 → (created_at, id) 튜플. id 또는 ISO timestamp.

        id 커서는 타이브레이커용 id를 함께 반환하고, 순수 timestamp는
        ('', id='') 형태로 반환하지 않고 (ts, '')로 구분한다 — 호출부는
        id가 비어 있으면 timestamp 비교만 수행한다."""
        if not after:
            return None
        raw = after.strip()[:64]
        if not raw:
            return None
        ev = self.get_agent_event(raw)
        if ev is not None:
            ts = str(ev.get("created_at") or "")
            return (ts, str(ev.get("id") or "")) if ts else None
        # ISO timestamp로 해석 시도
        try:
            from datetime import datetime

            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return dt.isoformat(), ""
        except ValueError:
            return None
