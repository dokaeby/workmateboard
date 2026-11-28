# mypy: disable-error-code="attr-defined"
"""Run/StageRun/Approval/AuditEvent 스토어 메서드 믹스인 (db.py Store에서 추출)."""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Literal

from ..models import Approval, AuditEvent, Run, RunStatus, StageRun, StageStatus
from . import orgctx
from .orgs import OrgInactiveError
from .util import _clamp_limit, _dt, _iso, _loads, _now, clean_scope_id

logger = logging.getLogger("jobmate.store")


def _org_bind() -> list[str]:
    """org_id 필터 바인딩 파라미터 — 요청 조직 컨텍스트(없으면 '').

    쿼리에는 `AND (org_id=? OR ?='')` 조건을 리터럴로 박고 이 값을 넘긴다 —
    ctx=''이면 OR 우항이 항상 참이라 전 조직 조회(시스템/스케줄러 의도),
    ctx가 있으면 해당 조직 행만 조회·갱신한다(spec 003 US3/FR-013).
    동적 SQL 문자열 연결을 쓰지 않아 S608 정적 검사도 통과한다."""
    oid = orgctx.current_org_id()
    return [oid, oid]


def _org_id_of(row: Any) -> str:
    return str(row["org_id"] or "") if row is not None else ""


class RunStoreMixin:
    def resolve_run_goal_id(self, input_data: dict[str, Any], goal_id: str = "") -> str:
        """run의 goal_id 해석 — create_run/admit_run 공용 단일 경로.

        순서: 명시 goal_id 파라미터 → input_data.goal_id → task effective
        goal(input.meta.task_id / input_data.task_id → task.goal_id →
        project.goal_id, spec 3차 A) → 프로젝트(input.project 태그 →
        projects.name ci 매칭)의 goal_id → ''.

        task 컨텍스트를 자유 텍스트 project 태그보다 먼저 해석한다 —
        management wake가 만드는 run은 '이 task를 위해 실행'이므로 task의
        effective goal이 프로젝트 이름 폴백보다 권위 있다. wakeups.admit_run이
        scope_key/payload 계산 전에 이 함수를 호출해 코얼레싱
        scope·wakeup payload·생성 run의 goal_id가 항상 일치하게 한다
        (H1 — project 폴백 goal이 scope 계산에서 빠지던 버그 수정)."""
        gid = (goal_id or "").strip()[:64]
        if not gid:
            gv = input_data.get("goal_id")
            if isinstance(gv, str):
                gid = gv.strip()[:64]
        if not gid:
            # task effective goal — wake 경로는 input.meta.task_id에
            # canonical task id를 싣는다(agent_management.wake_agent).
            tid = ""
            meta = input_data.get("meta")
            if isinstance(meta, dict):
                tid = str(meta.get("task_id") or "").strip()
            if not tid:
                tid = str(input_data.get("task_id") or "").strip()
            if tid:
                gid = self.task_effective_goal_id(self.get_task(tid))
        if not gid:
            pv = input_data.get("project")
            project = clean_scope_id(pv) if isinstance(pv, str) else ""
            if project:
                proj = self.find_project_by_name(project)
                if proj is not None:
                    gid = str(proj.get("goal_id") or "")[:64]
        return gid

    def _goal_usable(self, gid: str) -> bool:
        """goal_id가 존재하고 cancelled가 아니면 True — run 태깅 가능 여부.

        spec 3차 A: 유효성은 cancelled만 거부한다 — paused/achieved 목표도
        기존 run 이력 태깅과 동일하게 허용한다(achieved는 달성 기록,
        paused는 일시 중지일 뿐 참조 무결성을 깨지 않는다)."""
        if not gid:
            return False
        g = self.get_goal(gid)
        return g is not None and str(g.get("status") or "") != "cancelled"

    def create_run(
        self,
        workflow_key: str,
        version: str,
        input_data: dict[str, Any],
        trace_id: str,
        metadata: dict[str, Any] | None = None,
        created_by: str = "",
        agent_id: str = "",
        goal_id: str = "",
        org_id: str | None = None,
    ) -> Run:
        # input.project 자유 태그를 컬럼으로도 유지 (PATCH /runs/{id}와 동일 경로).
        # budget policy scope_id와 동일 정규화(clean_scope_id)를 적용해야
        # project 스코프 예산 귀속이 정책 생성 시 입력과 일치한다.
        project = ""
        pv = input_data.get("project")
        if isinstance(pv, str):
            project = clean_scope_id(pv)
        # goal_id 해석은 resolve_run_goal_id 단일 경로 — admit_run의
        # scope_key/payload 계산과 반드시 같은 결과가 나와야 한다.
        gid = self.resolve_run_goal_id(input_data, goal_id)
        # 존재하지 않거나 cancelled인 goal은 태깅하지 않는다 — 명시값도
        # 여기서는 strip한다(API 계층이 명시 goal의 422를 이미 담당하고,
        # task/project 폴백/스케줄 JSON 등 비-API 경로의 dangling 참조를
        # 막는 defense-in-depth). 폴백 goal이 cancelled면 조용히 태그 해제.
        if gid and not self._goal_usable(gid):
            logger.warning(
                "run goal_id %s unusable (missing/cancelled) — stripped workflow=%s",
                gid,
                workflow_key,
            )
            gid = ""
        # input의 goal_id/goal 키는 컬럼/컨텍스트로 흡수하고 저장 input에서는
        # 제거한다 — 코얼레싱 canonical input이 (요청 input, runs.goal_id)
        # 양쪽에서 같은 형태가 되도록. "goal"은 예약 키라 호출자가 임의 값을
        # 넣어도 프롬프트/컨텍스트로 새어 나가지 않게 항상 제거한다.
        input_data = {k: v for k, v in input_data.items() if k not in ("goal_id", "goal")}
        # goal 컨텍스트를 input에 주입 — 워크플로 stage가 run.input["goal"]
        # 의 {id,title}을 읽어 목표 맥락을 프롬프트에 반영할 수 있다.
        # ("goal" 키는 코얼레싱 scope 계산에서 제외 — wakeups.scope_key_for)
        goal_row = self.get_goal(gid) if gid else None
        if goal_row is not None:
            input_data["goal"] = {"id": gid, "title": str(goal_row.get("title") or "")}
        # org_id 해석(FR-013) — 부모(agents/goals)의 org를 우선 전파한다.
        # 스케줄러·웹훅 경로는 요청 ctx가 ''라 명시/ctx만으로는 org를 알 수
        # 없고, 부모 org 전파가 유일한 정확한 경로다. 부모가 없으면
        # 명시 인자 → 요청 컨텍스트 순(orgctx.org_for_write).
        oid = ""
        aid = (agent_id or "").strip()[:64]
        if aid:
            oid = _org_id_of(
                self._conn.execute("SELECT org_id FROM agents WHERE id=?", (aid,)).fetchone()
            )
        if not oid and goal_row is not None:
            oid = str(goal_row.get("org_id") or "")
        oid = oid or orgctx.org_for_write(org_id)
        # FR-031 — suspended/deleted 조직의 신규 run 생성을 중앙 차단.
        # API·스케줄러·wakeup·evals·CLI 등 모든 진입점이 create_run을
        # 지나므로 호출측 precheck(jobs/triggers/org_access_block)와
        # 별개로 여기서 한 번 강제한다. ''(레거시 무스코프)는 대상 아님.
        if oid and self.org_inactive(oid):
            raise OrgInactiveError(f"organization not active: {oid}")
        run = Run(
            id=uuid.uuid4().hex[:12],
            workflow_key=workflow_key,
            workflow_version=version,
            input=input_data,
            status=RunStatus.QUEUED,
            trace_id=trace_id,
            created_at=_now(),
            updated_at=_now(),
            metadata=metadata or {},
            created_by=created_by,
            project=project,
            agent_id=(agent_id or "").strip()[:64],
            goal_id=gid,
        )
        with self.tx() as c:
            c.execute(
                "INSERT INTO runs(id,workflow_key,workflow_version,input,status,trace_id,"
                "error,metadata,created_by,project,agent_id,goal_id,org_id,"
                "created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run.id,
                    run.workflow_key,
                    run.workflow_version,
                    json.dumps(run.input),
                    run.status.value,
                    run.trace_id,
                    run.error,
                    json.dumps(run.metadata),
                    run.created_by,
                    project,
                    run.agent_id or None,
                    run.goal_id or None,
                    oid,
                    _iso(run.created_at),
                    _iso(run.updated_at),
                ),
            )
        self.audit(
            run.id, "system", "run_created", run.id, {"workflow": workflow_key, "version": version}
        )
        return run

    def get_run(self, run_id: str) -> Run | None:
        row = self._conn.execute(
            "SELECT * FROM runs WHERE id=? AND (org_id=? OR ?='')",
            (run_id, *_org_bind()),
        ).fetchone()
        return self._row_to_run(row) if row else None

    def list_runs(self, limit: int = 50, workflow: str | None = None) -> list[Run]:
        limit = _clamp_limit(limit, 50, 500)
        conds: list[str] = ["(org_id=? OR ?='')"]
        params: list[Any] = [*_org_bind()]
        if workflow is not None:
            conds.append("workflow_key=?")
            params.append(workflow)
        sql = "SELECT * FROM runs"
        sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_run(r) for r in rows]

    def update_run(
        self,
        run_id: str,
        *,
        status: RunStatus | None = None,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """run 갱신 — 단일 UPDATE로 lost update 방지.

        이전 구현은 get_run으로 읽은 뒤 전체 컬럼을 다시 써서, 읽기~쓰기 사이에
        다른 writer(stage 완료 등)가 바꾼 status/metadata를 덮어쓸 수 있었다.
        COALESCE(?, col) 패턴으로 None 인자만 기존 값을 유지하고, status/error/
        metadata 각각 독립적으로 현재 행 기준 갱신한다."""
        with self.tx() as c:
            c.execute(
                "UPDATE runs SET status=COALESCE(?, status),"
                " error=COALESCE(?, error), metadata=COALESCE(?, metadata),"
                " updated_at=? WHERE id=? AND (org_id=? OR ?='')",
                (
                    status.value if status is not None else None,
                    error,
                    json.dumps(metadata) if metadata is not None else None,
                    _iso(_now()),
                    run_id,
                    *_org_bind(),
                ),
            )

    def claim_run_status(self, run_id: str, from_status: str, to_status: str) -> bool:
        """run 상태 조건부 전이 — WHERE status=? 원자 claim.

        동시 승인/재개 요청이 둘 다 waiting_approval을 읽고 resume하는 것을
        막는다. rowcount>0이면 이 호출이 claim에 성공한 것."""
        with self.tx() as c:
            cur = c.execute(
                "UPDATE runs SET status=?, updated_at=? WHERE id=? AND status=?"
                " AND (org_id=? OR ?='')",
                (to_status, _iso(_now()), run_id, from_status, *_org_bind()),
            )
        return cur.rowcount > 0

    def fail_stale_runs(
        self,
        stale_before_iso: str,
        error: str = "stale_timeout",
        reaped_out: list[str] | None = None,
    ) -> int:
        """updated_at이 stale_before_iso보다 오래된 queued/running run을 failed로 마감.

        재시작/강제종료로 running에 고착된 run 복구용. waiting_approval 등
        사람 승인 대기 상태는 정상 대기이므로 절대 건드리지 않는다.
        각 run의 아직 running인 stage_runs도 failed로 정리하고 audit을 남긴다.
        reaped_out이 주어지면 마감된 run id를 append한다 — 호출자가
        wakeup finalize(on_run_finished) 등 후속 처리를 하기 위함.
        반환값: 마감된 run 수."""
        now = _iso(_now())
        reaped: list[str] = []
        org_params = _org_bind()
        with self.tx() as c:
            rows = c.execute(
                "SELECT id FROM runs WHERE status IN ('queued','running') AND updated_at < ?"
                " AND (org_id=? OR ?='')",
                (stale_before_iso, *org_params),
            ).fetchall()
            for r in rows:
                rid = str(r["id"])
                cur = c.execute(
                    "UPDATE runs SET status='failed', error=?, updated_at=?"
                    " WHERE id=? AND status IN ('queued','running')"
                    " AND (org_id=? OR ?='')",
                    (error, now, rid, *org_params),
                )
                if int(getattr(cur, "rowcount", 0) or 0) <= 0:
                    continue  # 조회~갱신 사이 상태 전이 — 다른 경로가 처리 중
                reaped.append(rid)
                c.execute(
                    "UPDATE stage_runs SET status='failed', error=?, finished_at=?"
                    " WHERE run_id=? AND status='running'",
                    (error, now, rid),
                )
        for rid in reaped:
            self.audit(rid, "system", "run_stale_reaped", rid, {"error": error})
        if reaped_out is not None:
            reaped_out.extend(reaped)
        return len(reaped)

    # ---- goal 태깅 (runs.goal_id) ----
    def set_run_goal(self, run_id: str, goal_id: str) -> bool:
        """run의 goal 태그 설정/해제. ''이면 해제(NULL). updated_at은 건드리지 않는다."""
        gid = (goal_id or "").strip()[:64] or None
        with self.tx() as c:
            cur = c.execute(
                "UPDATE runs SET goal_id=? WHERE id=? AND (org_id=? OR ?='')",
                (gid, run_id, *_org_bind()),
            )
        return cur.rowcount > 0

    def _row_to_run(self, row: Any) -> Run:
        return Run(
            id=row["id"],
            workflow_key=row["workflow_key"],
            workflow_version=row["workflow_version"],
            input=_loads(row["input"]),
            status=RunStatus(row["status"]),
            trace_id=row["trace_id"],
            error=row["error"],
            metadata=_loads(row["metadata"]),
            created_by=row["created_by"] if "created_by" in row.keys() else "",
            project=row["project"] if "project" in row.keys() else "",
            agent_id=row["agent_id"] if "agent_id" in row.keys() and row["agent_id"] else "",
            goal_id=row["goal_id"] if "goal_id" in row.keys() and row["goal_id"] else "",
            created_at=_dt(row["created_at"]) or _now(),
            updated_at=_dt(row["updated_at"]) or _now(),
        )

    # ---- stage_runs ----
    def start_stage(
        self,
        run_id: str,
        stage_key: str,
        attempt: int,
        input_data: dict[str, Any],
        model: str = "",
        prompt_version: str = "",
    ) -> StageRun:
        sr = StageRun(
            id=uuid.uuid4().hex[:12],
            run_id=run_id,
            stage_key=stage_key,
            attempt=attempt,
            status=StageStatus.RUNNING,
            input=input_data,
            model=model,
            prompt_version=prompt_version,
            started_at=_now(),
        )
        with self.tx() as c:
            c.execute(
                "INSERT INTO stage_runs(id,run_id,stage_key,attempt,status,input,model,"
                "prompt_version,started_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    sr.id,
                    sr.run_id,
                    sr.stage_key,
                    sr.attempt,
                    sr.status.value,
                    json.dumps(input_data),
                    model,
                    prompt_version,
                    _iso(sr.started_at),
                ),
            )
        self.audit(
            run_id,
            "system",
            "stage_start",
            f"{stage_key}#{attempt}",
            {"stage": stage_key, "attempt": attempt},
        )
        return sr

    def finish_stage(
        self,
        stage_run_id: str,
        *,
        status: StageStatus | str,
        output: dict[str, Any] | None = None,
        error: str = "",
        tokens_in: int = 0,
        tokens_out: int = 0,
    ) -> None:
        status_enum = StageStatus(status)
        with self.tx() as c:
            c.execute(
                "UPDATE stage_runs SET status=?, output=?, error=?, tokens_in=?,"
                "tokens_out=?, finished_at=? WHERE id=?",
                (
                    status_enum.value,
                    json.dumps(output or {}),
                    error,
                    tokens_in,
                    tokens_out,
                    _iso(_now()),
                    stage_run_id,
                ),
            )
        if status_enum == StageStatus.SUCCEEDED:
            row = self._conn.execute(
                "SELECT run_id, stage_key, attempt FROM stage_runs WHERE id=?", (stage_run_id,)
            ).fetchone()
            if row:
                self.audit(
                    row["run_id"],
                    "system",
                    "stage_done",
                    f"{row['stage_key']}#{row['attempt']}",
                    {"status": status_enum.value},
                )

    def get_stage_runs(self, run_id: str) -> list[StageRun]:
        rows = self._conn.execute(
            "SELECT * FROM stage_runs WHERE run_id=? ORDER BY started_at, attempt", (run_id,)
        ).fetchall()
        return [self._row_to_stage(r) for r in rows]

    def get_stage_run(self, stage_run_id: str) -> StageRun | None:
        """stage_run 단건 조회 — cost-events 외부 보고의 참조 무결성 검증용."""
        row = self._conn.execute("SELECT * FROM stage_runs WHERE id=?", (stage_run_id,)).fetchone()
        return self._row_to_stage(row) if row else None

    def latest_stage(self, run_id: str, stage_key: str) -> StageRun | None:
        rows = self.get_stage_runs(run_id)
        matched = [r for r in rows if r.stage_key == stage_key]
        return matched[-1] if matched else None

    def _row_to_stage(self, row: Any) -> StageRun:
        return StageRun(
            id=row["id"],
            run_id=row["run_id"],
            stage_key=row["stage_key"],
            attempt=row["attempt"],
            status=StageStatus(row["status"]),
            input=_loads(row["input"]),
            output=_loads(row["output"]),
            error=row["error"],
            model=row["model"],
            prompt_version=row["prompt_version"],
            tokens_in=row["tokens_in"],
            tokens_out=row["tokens_out"],
            started_at=_dt(row["started_at"]),
            finished_at=_dt(row["finished_at"]),
        )

    # ---- approvals ----
    def add_approval(
        self,
        run_id: str,
        stage_key: str,
        decision: Literal["approve", "reject"],
        comment: str = "",
        decided_by: str = "local",
    ) -> Approval:
        now = _now()
        ap = Approval(
            id=uuid.uuid4().hex[:12],
            run_id=run_id,
            stage_key=stage_key,
            decision=decision,
            comment=comment,
            decided_by=decided_by,
            decided_at=now,
            created_at=now,
        )
        with self.tx() as c:
            c.execute(
                "INSERT INTO approvals(id,run_id,stage_key,decision,comment,"
                "decided_by,decided_at,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    ap.id,
                    ap.run_id,
                    ap.stage_key,
                    ap.decision,
                    ap.comment,
                    ap.decided_by,
                    _iso(ap.decided_at),
                    _iso(ap.created_at),
                ),
            )
        self.audit(
            run_id, decided_by, f"approval_{decision}", stage_key, {"comment": comment[:200]}
        )
        return ap

    def get_approvals(self, run_id: str) -> list[Approval]:
        rows = self._conn.execute(
            "SELECT * FROM approvals WHERE run_id=? ORDER BY decided_at", (run_id,)
        ).fetchall()
        return [
            Approval(
                id=r["id"],
                run_id=r["run_id"],
                stage_key=r["stage_key"],
                decision=r["decision"],
                comment=r["comment"],
                decided_by=r["decided_by"],
                decided_at=_dt(r["decided_at"]) or _now(),
                created_at=_dt(r["created_at"]) or _dt(r["decided_at"]),
            )
            for r in rows
        ]

    # ---- audit ----
    def audit(
        self,
        run_id: str,
        actor: str,
        action: str,
        object_ref: str = "",
        detail: dict[str, Any] | None = None,
    ) -> None:
        """본문/토큰이 detail에 들어가지 않도록 호출자가 책임진다."""
        # org_id는 부모 run의 org를 전파 — 스케줄러·내부 경로(ctx='')에서도
        # run이 속한 조직의 감사 로그로 기록된다(FR-013). run을 못 찾으면
        # 명시/요청 컨텍스트로 폴백.
        oid = _org_id_of(
            self._conn.execute("SELECT org_id FROM runs WHERE id=?", (run_id,)).fetchone()
        )
        oid = oid or orgctx.org_for_write()
        with self.tx() as c:
            c.execute(
                "INSERT INTO audit_events(id,run_id,actor,action,object_ref,detail,org_id,"
                "created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    uuid.uuid4().hex[:12],
                    run_id,
                    actor,
                    action,
                    object_ref,
                    json.dumps(detail or {}),
                    oid,
                    _iso(_now()),
                ),
            )

    def get_audit(self, run_id: str) -> list[AuditEvent]:
        rows = self._conn.execute(
            "SELECT * FROM audit_events WHERE run_id=? AND (org_id=? OR ?='') ORDER BY created_at",
            (run_id, *_org_bind()),
        ).fetchall()
        return [
            AuditEvent(
                id=r["id"],
                run_id=r["run_id"],
                actor=r["actor"],
                action=r["action"],
                object_ref=r["object_ref"],
                detail=_loads(r["detail"]),
                created_at=_dt(r["created_at"]) or _now(),
            )
            for r in rows
        ]
