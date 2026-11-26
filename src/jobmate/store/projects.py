# mypy: disable-error-code="attr-defined"
from __future__ import annotations

import uuid
from typing import Any

from .util import _iso, _now, clean_scope_id


class ProjectStoreMixin:
    # ---- projects (팀장 프로젝트 마스터/태깅) ----
    @staticmethod
    def _project_json(r: Any) -> dict[str, Any]:
        d = dict(r)
        try:
            d["target_count"] = int(d.get("target_count") or 0)
        except (TypeError, ValueError):
            d["target_count"] = 0
        return d

    def create_project(
        self,
        name: str,
        color: str = "",
        description: str = "",
        goal: str = "",
        target_count: int = 0,
        goal_id: str | None = None,
        created_by: str = "",
    ) -> dict[str, Any] | None:
        """프로젝트 생성. name은 trim 후 lower() 기준 중복 시 None.
        goal_id는 goals.id 참조(존재 검증은 라우트 계층)."""
        name = name.strip()[:200]
        if not name:
            return None
        if self.find_project_by_name(name) is not None:
            return None
        pid = uuid.uuid4().hex[:12]
        now = _iso(_now())
        gid = (goal_id or "").strip()[:64] or None
        try:
            with self.tx() as c:
                c.execute(
                    "INSERT INTO projects(id,name,color,description,goal,target_count,"
                    "goal_id,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        pid,
                        name,
                        color.strip()[:32],
                        description.strip()[:2000],
                        goal.strip()[:500],
                        int(target_count or 0),
                        gid,
                        created_by[:200],
                        now,
                    ),
                )
        except self._integrity_errors:
            return None
        return self.get_project(pid)

    def list_projects(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM projects ORDER BY lower(name), name").fetchall()
        return [self._project_json(r) for r in rows]

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        return self._project_json(row) if row else None

    def find_project_by_name(self, name: str) -> dict[str, Any] | None:
        """프로젝트 이름 조회 — trim + case-insensitive."""
        row = self._conn.execute(
            "SELECT * FROM projects WHERE lower(name)=lower(?)",
            (name.strip()[:200],),
        ).fetchone()
        return self._project_json(row) if row else None

    def update_project(self, project_id: str, **fields: Any) -> dict[str, Any] | None:
        """name/color/description/goal/target_count/goal_id 부분 갱신.
        name 변경 시 ci 중복 검사. goal_id=None은 해제."""
        proj = self.get_project(project_id)
        if proj is None:
            return None
        updates: dict[str, Any] = {}
        if "name" in fields:
            new_name = str(fields["name"] or "").strip()[:200]
            if not new_name:
                return None
            other = self.find_project_by_name(new_name)
            if other is not None and other["id"] != project_id:
                return None
            updates["name"] = new_name
        if "color" in fields:
            updates["color"] = str(fields["color"] or "").strip()[:32]
        if "description" in fields:
            updates["description"] = str(fields["description"] or "").strip()[:2000]
        if "goal" in fields:
            updates["goal"] = str(fields["goal"] or "").strip()[:500]
        if "target_count" in fields:
            try:
                updates["target_count"] = max(0, int(fields["target_count"] or 0))
            except (TypeError, ValueError):
                return None
        if "goal_id" in fields:
            gv = fields["goal_id"]
            updates["goal_id"] = (str(gv).strip()[:64] or None) if gv else None
        if not updates:
            return proj
        sets = ", ".join(f"{k}=?" for k in updates)
        try:
            with self.tx() as c:
                c.execute(
                    f"UPDATE projects SET {sets} WHERE id=?",  # noqa: S608 - keys는 고정 허용 목록
                    (*updates.values(), project_id),
                )
        except self._integrity_errors:
            return None
        return self.get_project(project_id)

    def delete_project(self, project_id: str) -> bool:
        """프로젝트 삭제 — 세션/run의 태그 값은 그대로 둔다(고아 태그 허용)."""
        with self.tx() as c:
            cur = c.execute("DELETE FROM projects WHERE id=?", (project_id,))
        return cur.rowcount > 0

    # ---- 프로젝트 태깅 (runs.project / agent_sessions.project) ----
    def set_run_project(self, run_id: str, project: str) -> bool:
        """run의 project 태그 설정. ''이면 태그 해제. updated_at은 건드리지 않는다."""
        with self.tx() as c:
            cur = c.execute(
                "UPDATE runs SET project=? WHERE id=?",
                (clean_scope_id(project), run_id),
            )
        return cur.rowcount > 0
