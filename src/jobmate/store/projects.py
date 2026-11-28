# mypy: disable-error-code="attr-defined"
from __future__ import annotations

import uuid
from typing import Any

from . import orgctx
from .util import _iso, _now, clean_scope_id

# spec 3차 A — projects.status 어휘(planned|active|paused|completed|cancelled).
PROJECT_STATUSES = ("planned", "active", "paused", "completed", "cancelled")


def _org_bind() -> list[str]:
    """org_id 필터 바인딩 — `AND (org_id=? OR ?='')` 조건에 넘길 [oid, oid].

    ctx=''이면 OR 우항이 항상 참이라 전 조직 조회(시스템/스케줄러),
    ctx가 있으면 해당 조직 행만 조회·갱신한다(spec 003 US3/FR-013)."""
    oid = orgctx.current_org_id()
    return [oid, oid]


class ProjectStoreMixin:
    # ---- projects (팀장 프로젝트 마스터/태깅) ----
    @staticmethod
    def _project_json(r: Any) -> dict[str, Any]:
        d = dict(r)
        try:
            d["target_count"] = int(d.get("target_count") or 0)
        except (TypeError, ValueError):
            d["target_count"] = 0
        if not d.get("status"):
            d["status"] = "active"
        return d

    def create_project(
        self,
        name: str,
        color: str = "",
        description: str = "",
        goal: str = "",
        target_count: int = 0,
        goal_id: str | None = None,
        status: str = "active",
        owner_agent_id: str | None = None,
        repository_url: str | None = None,
        repository_ref: str | None = None,
        workspace_path: str | None = None,
        created_by: str = "",
        org_id: str | None = None,
    ) -> dict[str, Any] | None:
        """프로젝트 생성. name은 trim 후 lower() 기준 중복 시 None.
        goal_id는 goals.id 참조(존재/상태 검증은 라우트 계층)."""
        name = name.strip()[:200]
        if not name:
            return None
        if self.find_project_by_name(name) is not None:
            return None
        pid = uuid.uuid4().hex[:12]
        now = _iso(_now())
        gid = (goal_id or "").strip()[:64] or None
        # org_id 해석(FR-013) — owner_agent_id 부모의 org를 우선 전파하고,
        # 없으면 명시 인자 → 요청 컨텍스트 순.
        oid = ""
        oaid = (owner_agent_id or "").strip()[:64]
        if oaid:
            arow = self._conn.execute("SELECT org_id FROM agents WHERE id=?", (oaid,)).fetchone()
            if arow is not None:
                oid = str(arow["org_id"] or "")
        oid = oid or orgctx.org_for_write(org_id)
        try:
            with self.tx() as c:
                c.execute(
                    "INSERT INTO projects(id,name,color,description,goal,target_count,"
                    "goal_id,status,owner_agent_id,repository_url,repository_ref,"
                    "workspace_path,created_by,org_id,created_at,updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        pid,
                        name,
                        color.strip()[:32],
                        description.strip()[:2000],
                        goal.strip()[:500],
                        int(target_count or 0),
                        gid,
                        (status or "").strip().lower()[:20] or "active",
                        oaid or None,
                        (repository_url or "").strip()[:500] or None,
                        (repository_ref or "").strip()[:200] or None,
                        (workspace_path or "").strip()[:1000] or None,
                        created_by[:200],
                        oid,
                        now,
                        now,
                    ),
                )
        except self._integrity_errors:
            return None
        return self.get_project(pid)

    def list_projects(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM projects WHERE (org_id=? OR ?='') ORDER BY lower(name), name",
            _org_bind(),
        ).fetchall()
        return [self._project_json(r) for r in rows]

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM projects WHERE id=? AND (org_id=? OR ?='')",
            (project_id, *_org_bind()),
        ).fetchone()
        return self._project_json(row) if row else None

    def find_project_by_name(self, name: str) -> dict[str, Any] | None:
        """프로젝트 이름 조회 — trim + case-insensitive."""
        row = self._conn.execute(
            "SELECT * FROM projects WHERE lower(name)=lower(?) AND (org_id=? OR ?='')",
            (name.strip()[:200], *_org_bind()),
        ).fetchone()
        return self._project_json(row) if row else None

    def update_project(self, project_id: str, **fields: Any) -> dict[str, Any] | None:
        """name/color/description/goal/target_count/goal_id/status/
        owner_agent_id/repository_url/repository_ref/workspace_path 부분 갱신.
        name 변경 시 ci 중복 검사. goal_id=None은 해제.
        갱신이 있으면 updated_at을 항상 갱신한다(spec 3차 A)."""
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
        if "status" in fields:
            st = str(fields["status"] or "").strip().lower()[:20]
            if st:
                updates["status"] = st
        if "owner_agent_id" in fields:
            ov = fields["owner_agent_id"]
            updates["owner_agent_id"] = (str(ov).strip()[:64] or None) if ov else None
        if "repository_url" in fields:
            rv = fields["repository_url"]
            updates["repository_url"] = (str(rv).strip()[:500] or None) if rv else None
        if "repository_ref" in fields:
            rf = fields["repository_ref"]
            updates["repository_ref"] = (str(rf).strip()[:200] or None) if rf else None
        if "workspace_path" in fields:
            wp = fields["workspace_path"]
            updates["workspace_path"] = (str(wp).strip()[:1000] or None) if wp else None
        if not updates:
            return proj
        sets = ", ".join(f"{k}=?" for k in updates)
        try:
            with self.tx() as c:
                c.execute(
                    f"UPDATE projects SET {sets}, updated_at=? WHERE id=?"  # noqa: S608 - keys는 고정 허용 목록
                    " AND (org_id=? OR ?='')",
                    (*updates.values(), _iso(_now()), project_id, *_org_bind()),
                )
        except self._integrity_errors:
            return None
        return self.get_project(project_id)

    def cancel_project(self, project_id: str) -> bool:
        """소프트 취소 — status='cancelled' (spec 3차 A).

        tasks.project_id/세션·run의 project 태그 이력은 보존한다 —
        하드 DELETE는 태그 이력과의 연결을 끊으므로 사용하지 않는다."""
        with self.tx() as c:
            cur = c.execute(
                "UPDATE projects SET status='cancelled', updated_at=? WHERE id=?"
                " AND (org_id=? OR ?='')",
                (_iso(_now()), project_id, *_org_bind()),
            )
        return cur.rowcount > 0

    # ---- 프로젝트 태깅 (runs.project / agent_sessions.project) ----
    def set_run_project(self, run_id: str, project: str) -> bool:
        """run의 project 태그 설정. ''이면 태그 해제. updated_at은 건드리지 않는다."""
        with self.tx() as c:
            cur = c.execute(
                "UPDATE runs SET project=? WHERE id=? AND (org_id=? OR ?='')",
                (clean_scope_id(project), run_id, *_org_bind()),
            )
        return cur.rowcount > 0
