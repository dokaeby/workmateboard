# mypy: disable-error-code="attr-defined"
"""Run/StageRun/Approval/AuditEvent 스토어 메서드 믹스인 (db.py Store에서 추출)."""

from __future__ import annotations

import json
import uuid
from typing import Any, Literal

from ..models import Approval, AuditEvent, Run, RunStatus, StageRun, StageStatus
from .util import _clamp_limit, _dt, _iso, _loads, _now, clean_scope_id


class RunStoreMixin:
    def create_run(
        self,
        workflow_key: str,
        version: str,
        input_data: dict[str, Any],
        trace_id: str,
        metadata: dict[str, Any] | None = None,
        created_by: str = "",
        agent_id: str = "",
    ) -> Run:
        # input.project 자유 태그를 컬럼으로도 유지 (PATCH /runs/{id}와 동일 경로).
        # budget policy scope_id와 동일 정규화(clean_scope_id)를 적용해야
        # project 스코프 예산 귀속이 정책 생성 시 입력과 일치한다.
        project = ""
        pv = input_data.get("project")
        if isinstance(pv, str):
            project = clean_scope_id(pv)
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
        )
        with self.tx() as c:
            c.execute(
                "INSERT INTO runs(id,workflow_key,workflow_version,input,status,trace_id,"
                "error,metadata,created_by,project,agent_id,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                    _iso(run.created_at),
                    _iso(run.updated_at),
                ),
            )
        self.audit(
            run.id, "system", "run_created", run.id, {"workflow": workflow_key, "version": version}
        )
        return run

    def get_run(self, run_id: str) -> Run | None:
        row = self._conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return self._row_to_run(row) if row else None

    def list_runs(self, limit: int = 50, workflow: str | None = None) -> list[Run]:
        limit = _clamp_limit(limit, 50, 500)
        if workflow is None:
            rows = self._conn.execute(
                "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM runs WHERE workflow_key=? ORDER BY created_at DESC LIMIT ?",
                (workflow, limit),
            ).fetchall()
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
                " updated_at=? WHERE id=?",
                (
                    status.value if status is not None else None,
                    error,
                    json.dumps(metadata) if metadata is not None else None,
                    _iso(_now()),
                    run_id,
                ),
            )

    def claim_run_status(self, run_id: str, from_status: str, to_status: str) -> bool:
        """run 상태 조건부 전이 — WHERE status=? 원자 claim.

        동시 승인/재개 요청이 둘 다 waiting_approval을 읽고 resume하는 것을
        막는다. rowcount>0이면 이 호출이 claim에 성공한 것."""
        with self.tx() as c:
            cur = c.execute(
                "UPDATE runs SET status=?, updated_at=? WHERE id=? AND status=?",
                (to_status, _iso(_now()), run_id, from_status),
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
        with self.tx() as c:
            rows = c.execute(
                "SELECT id FROM runs WHERE status IN ('queued','running') AND updated_at < ?",
                (stale_before_iso,),
            ).fetchall()
            for r in rows:
                rid = str(r["id"])
                cur = c.execute(
                    "UPDATE runs SET status='failed', error=?, updated_at=?"
                    " WHERE id=? AND status IN ('queued','running')",
                    (error, now, rid),
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
        with self.tx() as c:
            c.execute(
                "INSERT INTO audit_events(id,run_id,actor,action,object_ref,detail,created_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (
                    uuid.uuid4().hex[:12],
                    run_id,
                    actor,
                    action,
                    object_ref,
                    json.dumps(detail or {}),
                    _iso(_now()),
                ),
            )

    def get_audit(self, run_id: str) -> list[AuditEvent]:
        rows = self._conn.execute(
            "SELECT * FROM audit_events WHERE run_id=? ORDER BY created_at", (run_id,)
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
