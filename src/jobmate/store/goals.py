# mypy: disable-error-code="attr-defined"
"""Goal 계층 스토어 메서드 믹스인 (db.py Store에서 추출).

Paperclip goals 포팅 — parent_id 자기참조 트리, status(active|done|archived).
FK는 두지 않고 존재/자기참조는 store가, archived/사이클은 서비스 계층
(jobmate.goals)이 검증한다(agents.reports_to와 동일 분담).
"""

from __future__ import annotations

import uuid
from typing import Any

from .util import _iso, _now


class GoalStoreMixin:
    # ---- goals (목표 계층) ----
    @staticmethod
    def _goal_json(r: Any) -> dict[str, Any]:
        d = dict(r)
        if d.get("description") is None:
            d["description"] = ""
        if not d.get("status"):
            d["status"] = "active"
        return d

    def create_goal(
        self,
        title: str,
        description: str = "",
        parent_id: str | None = None,
        status: str = "active",
        created_by: str = "",
    ) -> dict[str, Any] | None:
        """목표 생성. title trim 후 비어있으면 None.

        parent 존재/자기참조는 여기서도 방어 — archived/사이클은 앱 계층
        (goals.validate_parent)이 담당한다."""
        title = title.strip()[:200]
        if not title:
            return None
        pid = (parent_id or "").strip()[:64] or None
        if pid is not None and self.get_goal(pid) is None:
            return None
        gid = uuid.uuid4().hex[:12]
        now = _iso(_now())
        with self.tx() as c:
            c.execute(
                "INSERT INTO goals(id,title,description,parent_id,status,"
                "created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    gid,
                    title,
                    description.strip()[:2000],
                    pid,
                    status.strip().lower()[:20] or "active",
                    created_by[:200],
                    now,
                    now,
                ),
            )
        return self.get_goal(gid)

    def get_goal(self, goal_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM goals WHERE id=?", (goal_id,)).fetchone()
        return self._goal_json(row) if row else None

    def list_goals(self, status: str | None = None) -> list[dict[str, Any]]:
        """목표 목록 — children_count(직계 자식 수) 포함.

        archived 목표의 자식도 parent_id를 유지한다(계약: 프론트가 트리
        렌더링 시 처리). status 필터는 정확 일치."""
        sql = "SELECT * FROM goals"
        params: list[Any] = []
        if status:
            sql += " WHERE status=?"
            params.append(status.strip().lower()[:20])
        sql += " ORDER BY created_at, id"
        rows = [self._goal_json(r) for r in self._conn.execute(sql, params).fetchall()]
        counts = self._goal_children_counts()
        for g in rows:
            g["children_count"] = counts.get(str(g.get("id") or ""), 0)
        return rows

    def _goal_children_counts(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT parent_id, COUNT(*) AS c FROM goals"
            " WHERE parent_id IS NOT NULL AND parent_id <> '' GROUP BY parent_id"
        ).fetchall()
        return {str(r["parent_id"]): int(r["c"]) for r in rows}

    def count_goal_children(self, goal_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS c FROM goals WHERE parent_id=?", (goal_id,)
        ).fetchone()
        return int(row["c"] or 0) if row is not None else 0

    def update_goal(self, goal_id: str, **fields: Any) -> dict[str, Any] | None:
        """title/description/parent_id/status 부분 갱신.

        parent_id: None/빈 문자열은 루트로 해제. 존재하지 않는 parent는 None."""
        goal = self.get_goal(goal_id)
        if goal is None:
            return None
        updates: dict[str, Any] = {}
        if "title" in fields:
            title = str(fields["title"] or "").strip()[:200]
            if not title:
                return None
            updates["title"] = title
        if "description" in fields:
            updates["description"] = str(fields["description"] or "").strip()[:2000]
        if "parent_id" in fields:
            pv = fields["parent_id"]
            pid = (str(pv).strip()[:64] or None) if pv else None
            if pid is not None:
                # 방어 심화 — FK 미강제 커넥션 대비 존재/자기참조 검사.
                # archived/사이클은 앱 계층(goals.validate_parent) 담당.
                if pid == goal_id or self.get_goal(pid) is None:
                    return None
            updates["parent_id"] = pid
        if "status" in fields:
            st = str(fields["status"] or "").strip().lower()[:20]
            if st:
                updates["status"] = st
        if not updates:
            return goal
        sets = ", ".join(f"{k}=?" for k in updates)
        with self.tx() as c:
            c.execute(
                f"UPDATE goals SET {sets}, updated_at=? WHERE id=?",  # noqa: S608 - keys는 고정 허용 목록
                (*updates.values(), _iso(_now()), goal_id),
            )
        return self.get_goal(goal_id)

    def archive_goal(self, goal_id: str) -> bool:
        """소프트 삭제 — status='archived'. runs/projects 참조 이력은 보존.

        자식 목표는 parent_id를 유지한다 — 프론트가 트리 렌더링 시
        archived 부모 아래 자식을 어떻게 표시할지 결정한다(계약상 허용)."""
        with self.tx() as c:
            cur = c.execute(
                "UPDATE goals SET status='archived', updated_at=? WHERE id=?",
                (_iso(_now()), goal_id),
            )
        return cur.rowcount > 0

    def parent_would_cycle(self, goal_id: str, parent_id: str) -> bool:
        """parent_id 체인을 따라 내려가며 goal_id에 도달하면 True(사이클).

        PATCH에서 자기 자신을 부모로 두는 경우와 a→b→a 순환을 모두 막는다.
        방문 집합으로 방어 — 데이터가 이미 오염돼 있어도 무한 루프하지 않는다.
        (agents.reports_to_would_cycle와 동일 알고리즘, 방향만 반대)"""
        target = parent_id.strip()
        if not target or target == goal_id:
            return True
        seen: set[str] = set()
        cur: str | None = target
        while cur and cur not in seen:
            if cur == goal_id:
                return True
            seen.add(cur)
            row = self._conn.execute("SELECT parent_id FROM goals WHERE id=?", (cur,)).fetchone()
            cur = str(row["parent_id"]) if row and row["parent_id"] else None
        return False
