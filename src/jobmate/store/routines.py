# mypy: disable-error-code="attr-defined"
"""Routine 스토어 메서드 믹스인 (db.py Store에서 추출).

agent-management 3차 (paperclip spec C) — routines/routine_runs 영속화.
스키마는 schema_sqlite.py/schema_pg.py에 이미 정의돼 있다.

동시성 모델은 workflow_schedules(usage.py)를 그대로 따른다:
- due_routines() 조회 → claim_routine_fire()가 next_run_at CAS로
  automation loop과 cron tick(또는 다중 인스턴스)의 중복 발사를 막는다.
- routine_runs.idempotency_key UNIQUE가 두 번째 방어선 — 동일
  routine/scheduled_for의 중복 task 생성을 차단한다.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from ..models import RoutineRunStatus, RoutineStatus, ScheduleKind
from . import orgctx
from .util import _clamp_limit, _iso, _loads, _now

# routine_runs.status 유효 어휘 — services 계층이 쓰기 전 검증하지만
# 저장 계층에서도 방어적으로 화이트리스트한다.
_RUN_STATUSES = {s.value for s in RoutineRunStatus}
# routines.status는 컬럼이 없고 enabled 플래그의 파생값이다(_routine_json).


def _org_filter() -> tuple[str, list[Any]]:
    """ctx 조직이 있으면 ` AND org_id=?` 조건 — 시스템/미인증(ctx='')은 무필터."""
    oid = orgctx.current_org_id()
    return (" AND org_id=?", [oid]) if oid else ("", [])


class RoutineStoreMixin:
    # ---- routines ----
    @staticmethod
    def _routine_json(r: Any) -> dict[str, Any]:
        d = dict(r)
        d["schedule"] = _loads(d.get("schedule") or "{}")
        d["task_template"] = _loads(d.get("task_template") or "{}")
        d["enabled"] = bool(d.get("enabled"))
        d["status"] = RoutineStatus.ACTIVE.value if d["enabled"] else RoutineStatus.PAUSED.value
        d["schedule_kind"] = str(d.get("schedule_kind") or ScheduleKind.INTERVAL.value)
        return d

    @staticmethod
    def _routine_run_json(r: Any) -> dict[str, Any]:
        return dict(r)

    def create_routine(
        self,
        *,
        name: str,
        description: str = "",
        project_id: str | None = None,
        task_template: dict[str, Any] | None = None,
        assignee_agent_id: str | None = None,
        schedule_kind: str = "interval",
        schedule: dict[str, Any] | None = None,
        timezone: str = "UTC",
        max_concurrent: int = 1,
        enabled: bool = True,
        next_run_at: str | None = None,
        created_by: str = "",
        org_id: str | None = None,
    ) -> dict[str, Any]:
        """routine 생성 — schedule/next_run_at 검증·계산은 서비스 계층 몫."""
        now = _iso(_now())
        rid = uuid.uuid4().hex[:12]
        # org 귀속: 명시 > project 부모 > ctx — project 없는 routine도
        # ctx/명시값으로 올바른 조직에 기록된다.
        oid = (org_id or "").strip()
        pid = (project_id or "").strip()[:64]
        if not oid and pid:
            prow = self._conn.execute("SELECT org_id FROM projects WHERE id=?", (pid,)).fetchone()
            if prow is not None:
                oid = str(prow["org_id"] or "")
        if not oid:
            oid = orgctx.org_for_write()
        with self.tx() as c:
            c.execute(
                "INSERT INTO routines(id,name,description,project_id,task_template,"
                "assignee_agent_id,schedule_kind,schedule,timezone,max_concurrent,"
                "enabled,next_run_at,last_run_at,created_by,created_at,updated_at,"
                "org_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    rid,
                    name.strip()[:200],
                    description.strip()[:2000],
                    (project_id or "").strip()[:64] or None,
                    json.dumps(task_template or {}, ensure_ascii=False),
                    (assignee_agent_id or "").strip()[:64] or None,
                    schedule_kind.strip()[:20],
                    json.dumps(schedule or {}, ensure_ascii=False),
                    (timezone or "UTC").strip()[:64],
                    max(1, int(max_concurrent or 1)),
                    1 if enabled else 0,
                    (next_run_at or "").strip()[:64] or None,
                    None,
                    created_by[:200],
                    now,
                    now,
                    oid,
                ),
            )
        row = self.get_routine(rid)
        return row if row is not None else {"id": rid}

    def get_routine(self, routine_id: str) -> dict[str, Any] | None:
        frag, params = _org_filter()
        row = self._conn.execute(
            "SELECT * FROM routines WHERE id=?" + frag,  # noqa: S608
            (routine_id, *params),
        ).fetchone()
        return self._routine_json(row) if row else None

    def list_routines(
        self,
        *,
        enabled: bool | None = None,
        assignee_agent_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM routines"
        frag, fparams = _org_filter()
        conds: list[str] = []
        params: list[Any] = []
        if frag:
            conds.append(frag.strip().removeprefix("AND").strip())
            params.extend(fparams)
        if enabled is not None:
            conds.append("enabled=?")
            params.append(1 if enabled else 0)
        if assignee_agent_id:
            conds.append("assignee_agent_id=?")
            params.append(assignee_agent_id.strip()[:64])
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(_clamp_limit(limit, 100, 500))
        return [self._routine_json(r) for r in self._conn.execute(sql, params).fetchall()]

    _ROUTINE_UPDATE_FIELDS = {
        "name",
        "description",
        "project_id",
        "task_template",
        "assignee_agent_id",
        "schedule_kind",
        "schedule",
        "timezone",
        "max_concurrent",
        "enabled",
        "next_run_at",
        "last_run_at",
    }

    def update_routine(self, routine_id: str, **fields: Any) -> dict[str, Any] | None:
        """부분 갱신 — _ROUTINE_UPDATE_FIELDS 화이트리스트만 허용."""
        routine = self.get_routine(routine_id)
        if routine is None:
            return None
        # None은 '비우기'가 의미 있는 nullable 컬럼만 허용 — 그 외 필드의
        # 명시 null은 무시한다(patch 의미론: null 필드는 건드리지 않음).
        nullable_clear = {"project_id", "assignee_agent_id"}
        updates: dict[str, Any] = {}
        for k, v in fields.items():
            if k not in self._ROUTINE_UPDATE_FIELDS:
                continue
            if v is None and k not in nullable_clear:
                continue
            if k in ("task_template", "schedule") and not isinstance(v, dict):
                continue
            if k == "enabled":
                updates["enabled"] = 1 if v else 0
            elif k in ("task_template", "schedule"):
                updates[k] = json.dumps(v or {}, ensure_ascii=False)
            elif k == "max_concurrent":
                updates[k] = max(1, int(v or 1))
            else:
                updates[k] = str(v).strip()[:200] if k == "name" else v
        if not updates:
            return routine
        # schedule_kind/timezone/next_run_at 등 길이 제한이 필요한 컬럼 정규화
        for k, lim in (
            ("schedule_kind", 20),
            ("timezone", 64),
            ("assignee_agent_id", 64),
            ("project_id", 64),
            ("next_run_at", 64),
            ("last_run_at", 64),
        ):
            if k in updates and updates[k] is not None:
                updates[k] = str(updates[k]).strip()[:lim] or None
        if "description" in updates:
            updates["description"] = str(updates["description"]).strip()[:2000]
        sets = ", ".join(f"{k}=?" for k in updates)
        frag, fparams = _org_filter()
        with self.tx() as c:
            c.execute(
                f"UPDATE routines SET {sets}, updated_at=? WHERE id=?" + frag,  # noqa: S608
                (*updates.values(), _iso(_now()), routine_id, *fparams),
            )
        return self.get_routine(routine_id)

    def delete_routine(self, routine_id: str) -> bool:
        """routine 삭제 — routine_runs 이력은 보존한다(감사 가능성)."""
        frag, params = _org_filter()
        with self.tx() as c:
            cur = c.execute("DELETE FROM routines WHERE id=?" + frag, (routine_id, *params))  # noqa: S608
        return cur.rowcount > 0

    def due_routines(self, *, now: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """enabled 이고 next_run_at이 지난 routine — next_run_at 오름차순, bounded."""
        rows = self._conn.execute(
            "SELECT * FROM routines WHERE enabled=1 AND next_run_at IS NOT NULL"
            " AND next_run_at <= ? ORDER BY next_run_at LIMIT ?",
            ((now or _iso(_now())), _clamp_limit(limit, 50, 200)),
        ).fetchall()
        return [self._routine_json(r) for r in rows]

    def claim_routine_fire(
        self,
        routine_id: str,
        expected_next_run_at: str,
        last_run_at: str,
        next_run_at: str | None,
    ) -> bool:
        """스케줄 발사 원자 claim — next_run_at compare-and-swap.

        due_routines()로 읽은 행의 next_run_at이 그대로일 때만 last/next를
        갱신한다. claim_schedule_fire와 동일한 패턴 — automation loop과
        cron tick(또는 다중 인스턴스)이 같은 routine을 동시에 발사하는 것을
        막는다. rowcount>0이면 claim 성공 — 호출자는 run 생성으로 진행."""
        with self.tx() as c:
            cur = c.execute(
                "UPDATE routines SET last_run_at=?, next_run_at=?, updated_at=?"
                " WHERE id=? AND enabled=1 AND next_run_at=?",
                (last_run_at, next_run_at, _iso(_now()), routine_id, expected_next_run_at),
            )
        return cur.rowcount > 0

    def claim_routine_fire_with_run(
        self,
        routine_id: str,
        expected_next_run_at: str,
        last_run_at: str,
        next_run_at: str | None,
        *,
        scheduled_for: str,
        idempotency_key: str,
        status: str = "pending",
    ) -> tuple[str, dict[str, Any] | None]:
        """claim + pending routine_run 생성을 단일 tx로 — (action, run) 반환.

        action: 'claimed'(새 run 생성) / 'claim_lost'(CAS 실패) /
        'duplicate'(동일 idempotency_key의 run이 이미 존재 — claim은
        커밋돼 스케줄은 전진하고 기존 run을 반환한다).

        두 단계를 한 tx로 묶어 claim 후 크래시로 발생분이 유실되거나
        routine_run 없이 task만 생기는 경로를 없앤다. routine_runs INSERT는
        INSERT OR IGNORE(PG는 ON CONFLICT DO NOTHING으로 번역)라 중복 키여도
        tx가 abort되지 않는다."""
        st = (status or RoutineRunStatus.PENDING.value).strip().lower()[:20]
        if st not in _RUN_STATUSES:
            st = RoutineRunStatus.PENDING.value
        now = _iso(_now())
        rrid = uuid.uuid4().hex[:12]
        key = (idempotency_key or "").strip()[:200]
        if not key:
            return "claim_lost", None
        with self.tx() as c:
            cur = c.execute(
                "UPDATE routines SET last_run_at=?, next_run_at=?, updated_at=?"
                " WHERE id=? AND enabled=1 AND next_run_at=?",
                (last_run_at, next_run_at, now, routine_id, expected_next_run_at),
            )
            if cur.rowcount == 0:
                return "claim_lost", None
            ins = c.execute(
                "INSERT OR IGNORE INTO routine_runs(id,routine_id,scheduled_for,"
                "status,idempotency_key,created_at) VALUES(?,?,?,?,?,?)",
                (rrid, routine_id, scheduled_for.strip()[:64], st, key, now),
            )
            if ins.rowcount == 0:
                # 동일 idempotency_key — 기존 run을 같은 tx에서 읽는다.
                row = c.execute(
                    "SELECT * FROM routine_runs WHERE idempotency_key=?", (key,)
                ).fetchone()
                return "duplicate", self._routine_run_json(row) if row else None
            row = c.execute("SELECT * FROM routine_runs WHERE id=?", (rrid,)).fetchone()
            return "claimed", self._routine_run_json(row) if row else {"id": rrid}

    # ---- routine_runs ----
    def create_routine_run(
        self,
        *,
        routine_id: str,
        scheduled_for: str,
        idempotency_key: str,
        status: str = "pending",
    ) -> tuple[dict[str, Any] | None, bool]:
        """routine_runs 행 생성 — (row, created) 반환.

        idempotency_key UNIQUE가 동일 routine/scheduled_for의 중복 행을
        차단한다 — UNIQUE 위반(동시 생성 레이스) 시 기존 행을 반환하고
        created=False (create_recovery_action과 동일 패턴)."""
        st = (status or RoutineRunStatus.PENDING.value).strip().lower()[:20]
        if st not in _RUN_STATUSES:
            st = RoutineRunStatus.PENDING.value
        now = _iso(_now())
        rrid = uuid.uuid4().hex[:12]
        key = (idempotency_key or "").strip()[:200]
        if not key:
            return None, False
        try:
            with self.tx() as c:
                c.execute(
                    "INSERT INTO routine_runs(id,routine_id,scheduled_for,status,"
                    "idempotency_key,created_at) VALUES(?,?,?,?,?,?)",
                    (rrid, routine_id, scheduled_for.strip()[:64], st, key, now),
                )
        except self._integrity_errors:
            existing = self.get_routine_run_by_key(key)
            if existing is not None:
                return existing, False
            raise
        return self.get_routine_run(rrid), True

    def get_routine_run(self, run_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM routine_runs WHERE id=?", (run_id,)).fetchone()
        return self._routine_run_json(row) if row else None

    def get_routine_run_by_key(self, idempotency_key: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM routine_runs WHERE idempotency_key=?",
            ((idempotency_key or "").strip()[:200],),
        ).fetchone()
        return self._routine_run_json(row) if row else None

    def list_routine_runs(self, routine_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM routine_runs WHERE routine_id=?"
            " ORDER BY created_at DESC, id DESC LIMIT ?",
            (routine_id, _clamp_limit(limit, 50, 200)),
        ).fetchall()
        return [self._routine_run_json(r) for r in rows]

    def mark_routine_run(
        self,
        run_id: str,
        *,
        status: str,
        task_id: str | None = None,
        error_code: str = "",
        finished: bool = True,
    ) -> dict[str, Any] | None:
        """routine_runs 상태 전이 — status는 RoutineRunStatus 어휘만 허용."""
        st = (status or "").strip().lower()[:20]
        if st not in _RUN_STATUSES:
            return None
        fields: dict[str, Any] = {"status": st, "error_code": (error_code or "")[:200]}
        if task_id is not None:
            fields["task_id"] = str(task_id).strip()[:64] or None
        if finished:
            fields["finished_at"] = _iso(_now())
        sets = ", ".join(f"{k}=?" for k in fields)
        with self.tx() as c:
            c.execute(
                f"UPDATE routine_runs SET {sets} WHERE id=?",  # noqa: S608
                (*fields.values(), run_id),
            )
        return self.get_routine_run(run_id)

    def fail_stale_pending_routine_runs(self, older_than: str) -> int:
        """pending 상태로 오래 남은 routine_runs를 failed로 마감한다.

        claim_routine_fire_with_run은 pending run을 먼저 만들고 task
        생성·마감은 이후에 한다 — 그 사이 프로세스 크래시나 마감 단계
        실패가 나면 run이 영구 pending으로 남아 routine 이력이 '진행 중'으로
        보인다(R2-5). watchdog(_process_due_routines)가 호출해 회수한다."""
        with self.tx() as c:
            cur = c.execute(
                "UPDATE routine_runs SET status='failed',"
                " error_code='interrupted', finished_at=?"
                " WHERE status='pending' AND created_at < ?",
                (_iso(_now()), older_than),
            )
        return int(cur.rowcount or 0)

    def routine_inflight_count(self, routine_id: str) -> int:
        """max_concurrent 판정 — 이 routine이 만든 task 중 비종결 상태 개수.

        routine_runs.task_id로 연결된 task가 done/cancelled가 아닌 것만 센다.
        routine_run이 여러 개 같은 task를 가리키는 경우는 없으므로
        task 기준 DISTINCT로 집계한다."""
        row = self._conn.execute(
            "SELECT COUNT(DISTINCT rr.task_id) AS c FROM routine_runs rr"
            " JOIN tasks t ON t.id = rr.task_id"
            " WHERE rr.routine_id=? AND rr.task_id IS NOT NULL"
            " AND t.status NOT IN ('done','cancelled')",
            (routine_id,),
        ).fetchone()
        return int(row["c"] or 0) if row else 0

    # ---- routine → task 프로젝트 태깅 ----
    def tag_task_routine_project(
        self, task_id: str, project_id: str | None, goal_id: str | None = None
    ) -> None:
        """routine이 만든 task에 project/goal 태그를 단다.

        management store의 update_task는 project_id/goal_id를 받지 않으므로
        routine 경로에서만 직접 갱신한다 — Goal→Project→Task 추적(spec 3차 A)
        을 위해 routine.project_id를 task에 전파한다."""
        with self.tx() as c:
            c.execute(
                "UPDATE tasks SET project_id=?, goal_id=?, updated_at=? WHERE id=?",
                (
                    (project_id or "").strip()[:64] or None,
                    (goal_id or "").strip()[:64] or None,
                    _iso(_now()),
                    task_id,
                ),
            )
