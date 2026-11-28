# mypy: disable-error-code="attr-defined"
"""Goal 계층 스토어 메서드 믹스인 (db.py Store에서 추출).

Paperclip goals 포팅 — parent_id 자기참조 트리,
status(active|paused|achieved|cancelled — spec 3차 A).
FK는 두지 않고 존재/자기참조는 store가, cancelled/사이클은 서비스 계층
(jobmate.goals)이 검증한다(agents.reports_to와 동일 분담).
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from . import orgctx
from .util import _iso, _now


def _org_bind() -> list[str]:
    """org_id 필터 바인딩 — `AND (org_id=? OR ?='')` 조건에 넘길 [oid, oid].

    ctx=''이면 OR 우항이 항상 참이라 전 조직 조회(시스템/스케줄러),
    ctx가 있으면 해당 조직 행만 조회·갱신한다(spec 003 US3/FR-013)."""
    oid = orgctx.current_org_id()
    return [oid, oid]


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

    @staticmethod
    def _metrics_json(value: Any) -> str:
        """success_metrics → TEXT 컬럼용 JSON. dict 외 입력은 '{}'로 정규화
        (서비스 계층이 dict/크기를 이미 검증한다 — 여기서는 방어적 직렬화)."""
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return "{}"
            try:
                json.loads(value)
                return value[:4096]
            except (ValueError, TypeError):
                return "{}"
        if isinstance(value, dict):
            try:
                return json.dumps(value, ensure_ascii=False, default=str)[:4096]
            except (TypeError, ValueError):
                return "{}"
        return "{}"

    def create_goal(
        self,
        title: str,
        description: str = "",
        parent_id: str | None = None,
        status: str = "active",
        owner_agent_id: str | None = None,
        success_metrics: Any = None,
        target_at: str | None = None,
        created_by: str = "",
        org_id: str | None = None,
    ) -> dict[str, Any] | None:
        """목표 생성. title trim 후 비어있으면 None.

        parent 존재/자기참조는 여기서도 방어 — cancelled/사이클은 앱 계층
        (goals.validate_parent)이 담당한다."""
        title = title.strip()[:200]
        if not title:
            return None
        pid = (parent_id or "").strip()[:64] or None
        if pid is not None and self.get_goal(pid) is None:
            return None
        gid = uuid.uuid4().hex[:12]
        now = _iso(_now())
        # org_id 해석(FR-013) — parent goal의 org를 우선 전파하고,
        # 없으면 명시 인자 → 요청 컨텍스트 순.
        oid = ""
        if pid is not None:
            prow = self._conn.execute("SELECT org_id FROM goals WHERE id=?", (pid,)).fetchone()
            if prow is not None:
                oid = str(prow["org_id"] or "")
        oid = oid or orgctx.org_for_write(org_id)
        with self.tx() as c:
            c.execute(
                "INSERT INTO goals(id,title,description,parent_id,status,"
                "owner_agent_id,success_metrics,target_at,"
                "created_by,org_id,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    gid,
                    title,
                    description.strip()[:2000],
                    pid,
                    status.strip().lower()[:20] or "active",
                    (owner_agent_id or "").strip()[:64] or None,
                    self._metrics_json(success_metrics if success_metrics is not None else {}),
                    (target_at or "").strip()[:64] or None,
                    created_by[:200],
                    oid,
                    now,
                    now,
                ),
            )
        return self.get_goal(gid)

    def get_goal(self, goal_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM goals WHERE id=? AND (org_id=? OR ?='')",
            (goal_id, *_org_bind()),
        ).fetchone()
        return self._goal_json(row) if row else None

    def list_goals(self, status: str | None = None) -> list[dict[str, Any]]:
        """목표 목록 — children_count(직계 자식 수) 포함.

        cancelled 목표의 자식도 parent_id를 유지한다(계약: 프론트가 트리
        렌더링 시 처리). status 필터는 정확 일치."""
        conds: list[str] = ["(org_id=? OR ?='')"]
        params: list[Any] = [*_org_bind()]
        if status:
            conds.append("status=?")
            params.append(status.strip().lower()[:20])
        sql = "SELECT * FROM goals"
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY created_at, id"
        rows = [self._goal_json(r) for r in self._conn.execute(sql, params).fetchall()]
        counts = self._goal_children_counts()
        for g in rows:
            g["children_count"] = counts.get(str(g.get("id") or ""), 0)
        return rows

    def _goal_children_counts(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT parent_id, COUNT(*) AS c FROM goals"
            " WHERE parent_id IS NOT NULL AND parent_id <> ''"
            " AND (org_id=? OR ?='') GROUP BY parent_id",
            _org_bind(),
        ).fetchall()
        return {str(r["parent_id"]): int(r["c"]) for r in rows}

    def count_goal_children(self, goal_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS c FROM goals WHERE parent_id=? AND (org_id=? OR ?='')",
            (goal_id, *_org_bind()),
        ).fetchone()
        return int(row["c"] or 0) if row is not None else 0

    def update_goal(self, goal_id: str, **fields: Any) -> dict[str, Any] | None:
        """title/description/parent_id/status/owner_agent_id/success_metrics/
        target_at 부분 갱신.

        parent_id: None/빈 문자열은 루트로 해제. 존재하지 않는 parent는 None.
        success_metrics는 dict/JSON 문자열 모두 받아 TEXT로 정규화한다."""
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
                # cancelled/사이클은 앱 계층(goals.validate_parent) 담당.
                if pid == goal_id or self.get_goal(pid) is None:
                    return None
            updates["parent_id"] = pid
        if "status" in fields:
            st = str(fields["status"] or "").strip().lower()[:20]
            if st:
                updates["status"] = st
        if "owner_agent_id" in fields:
            ov = fields["owner_agent_id"]
            updates["owner_agent_id"] = (str(ov).strip()[:64] or None) if ov else None
        if "success_metrics" in fields:
            updates["success_metrics"] = self._metrics_json(fields["success_metrics"])
        if "target_at" in fields:
            tv = fields["target_at"]
            updates["target_at"] = (str(tv).strip()[:64] or None) if tv else None
        if not updates:
            return goal
        sets = ", ".join(f"{k}=?" for k in updates)
        with self.tx() as c:
            c.execute(
                f"UPDATE goals SET {sets}, updated_at=? WHERE id=?"  # noqa: S608 - keys는 고정 허용 목록
                " AND (org_id=? OR ?='')",
                (*updates.values(), _iso(_now()), goal_id, *_org_bind()),
            )
        return self.get_goal(goal_id)

    def cancel_goal(self, goal_id: str) -> bool:
        """소프트 삭제 — status='cancelled' (spec 3차 A, 구 archive 의미).

        runs/projects 참조 이력은 보존한다. 자식 목표는 parent_id를 유지한다 —
        프론트가 트리 렌더링 시 cancelled 부모 아래 자식을 어떻게 표시할지
        결정한다(계약상 허용)."""
        with self.tx() as c:
            cur = c.execute(
                "UPDATE goals SET status='cancelled', updated_at=? WHERE id=?"
                " AND (org_id=? OR ?='')",
                (_iso(_now()), goal_id, *_org_bind()),
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
