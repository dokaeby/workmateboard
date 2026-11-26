# mypy: disable-error-code="attr-defined"
"""승인 요청 + 복구 작업 스토어 메서드 믹스인 (db.py Store에서 추출).

paperclip-agent-management-spec 2차 D/E — approval_requests 범용 승인함과
recovery_actions watchdog 복구 추적. 상태 전이·도메인 규칙은 서비스 계층
(services/approvals.py, services/watchdog.py)이 담당하고, 여기서는
원자적 쓰기·멱등 조회·CAS 결정만 제공한다.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from ..models import (
    ApprovalActionType,
    ApprovalRisk,
    ApprovalStatus,
    RecoveryKind,
    RecoveryOwnerType,
    RecoveryStatus,
    RunStatus,
)
from .util import _clamp_limit, _iso, _now

_APPROVAL_STATUSES = {s.value for s in ApprovalStatus}
_APPROVAL_RISKS = {s.value for s in ApprovalRisk}
_APPROVAL_ACTIONS = {s.value for s in ApprovalActionType}
_RECOVERY_KINDS = {s.value for s in RecoveryKind}
_RECOVERY_STATUSES = {s.value for s in RecoveryStatus}
_RECOVERY_OWNERS = {s.value for s in RecoveryOwnerType}

# watchdog stuck_run 감지 대상 — 사람/시스템 결정 대기 상태 + 절대 pickup되지
# 않은 queued. needs_human은 이미 사람 알림 경로가 있는 상태라 제외하고,
# _reap_stale_runs의 30분 queued/running fail job과는 별개로 더 긴 꼬리
# (waiting_*)를 담당한다.
_STUCK_RUN_STATUSES = (
    RunStatus.WAITING_APPROVAL.value,
    RunStatus.WAITING_REVIEW.value,
    RunStatus.QUEUED.value,
)
# terminal 판정 — wakeups._TERMINAL_STATUSES와 동일 집합(서비스 계층이
# 순환 import 없이 쓰도록 여기서 독립 정의).
_TERMINAL_RUN_STATUSES = (
    RunStatus.COMPLETED.value,
    RunStatus.FAILED.value,
    RunStatus.REJECTED.value,
    RunStatus.CANCELLED.value,
    RunStatus.BUDGET_EXHAUSTED.value,
    RunStatus.NEEDS_HUMAN.value,
)
# in_progress task에 '살아있는 작업'으로 인정하는 run 상태 —
# store/management.current_task_for_agent의 active-run predicate와 동일.
_ACTIVE_RUN_STATUSES = (
    RunStatus.QUEUED.value,
    RunStatus.RUNNING.value,
    RunStatus.WAITING_APPROVAL.value,
    RunStatus.WAITING_REVIEW.value,
)
# heartbeat를 보내야 하는 canonical agent 상태 — paused/error/pending_approval/
# terminated는 의도된 비작업 상태라 stale로 보지 않는다(오탐 방지).
_HEARTBEAT_AGENT_STATUSES = ("active", "idle", "running")


class GovernanceStoreMixin:
    # ---- approval_requests (범용 승인함) ----
    @staticmethod
    def _approval_effective_status(d: dict[str, Any], now_iso: str) -> str:
        """pending + expires_at 경과 → 'expired'로 읽는다(lazy expiry).

        DB 행은 건드리지 않는다 — 결정 CAS가 expires_at 조건으로 이미
        만료된 행의 결정을 차단하고, 다음 sweep이 상태를 확정한다."""
        st = str(d.get("status") or "")
        if st == ApprovalStatus.PENDING.value:
            exp = str(d.get("expires_at") or "")
            if exp and exp < now_iso:
                return ApprovalStatus.EXPIRED.value
        return st if st in _APPROVAL_STATUSES else ApprovalStatus.PENDING.value

    @staticmethod
    def _approval_json(r: Any) -> dict[str, Any]:
        d = dict(r)
        try:
            payload = json.loads(d.get("safe_payload") or "{}")
            d["safe_payload"] = payload if isinstance(payload, dict) else {}
        except (ValueError, TypeError):
            d["safe_payload"] = {}
        for k in (
            "requested_by_agent_id",
            "task_id",
            "run_id",
            "expires_at",
            "decided_by",
            "decided_at",
        ):
            if d.get(k) is None:
                d[k] = None
        at = str(d.get("action_type") or "").strip().lower()
        d["action_type"] = at if at in _APPROVAL_ACTIONS else ApprovalActionType.RECOVERY.value
        rk = str(d.get("risk_level") or "").strip().lower()
        d["risk_level"] = rk if rk in _APPROVAL_RISKS else ApprovalRisk.MEDIUM.value
        d["summary"] = str(d.get("summary") or "")[:300]
        d["decision_comment"] = str(d.get("decision_comment") or "")[:500]
        d["status"] = GovernanceStoreMixin._approval_effective_status(d, _iso(_now()) or "")
        return d

    def create_approval_request(
        self,
        *,
        action_type: str,
        risk_level: str = "medium",
        summary: str = "",
        safe_payload: dict[str, Any] | None = None,
        requested_by: str = "",
        requested_by_agent_id: str | None = None,
        task_id: str | None = None,
        run_id: str | None = None,
        resource_type: str = "",
        resource_id: str = "",
        expires_at: str | None = None,
    ) -> dict[str, Any] | None:
        """승인 요청 생성 — 항상 status='pending'으로 시작.

        검증(유효 enum·요약 길이·safe_payload secret 거부)은 서비스 계층
        책임 — 여기서는 저장 가능한 형태로 정규화만 한다."""
        now = _iso(_now())
        aid = uuid.uuid4().hex[:12]
        # safe_payload는 통째로 저장 — 8000자 초과는 잘라내지 않고 거부.
        # 잘린 JSON은 파싱 실패로 {}가 돼 부수효과 필드를 조용히 잃는다.
        # 서비스 계층이 422로 먼저 거르지만 store도 독립적으로 방어한다.
        try:
            payload_json = json.dumps(safe_payload or {}, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            payload_json = "{}"
        if len(payload_json) > 8000:
            raise ValueError("safe_payload exceeds 8000 chars")
        with self.tx() as c:
            c.execute(
                "INSERT INTO approval_requests(id,action_type,risk_level,status,"
                "requested_by_agent_id,requested_by,task_id,run_id,resource_type,"
                "resource_id,summary,safe_payload,expires_at,created_at,updated_at)"
                " VALUES(?,?,?,'pending',?,?,?,?,?,?,?,?,?,?,?)",
                (
                    aid,
                    (action_type or "").strip().lower()[:40],
                    (risk_level or "medium").strip().lower()[:20],
                    (requested_by_agent_id or "").strip()[:64] or None,
                    (requested_by or "").strip()[:200],
                    (task_id or "").strip()[:64] or None,
                    (run_id or "").strip()[:64] or None,
                    (resource_type or "").strip().lower()[:40],
                    (resource_id or "").strip()[:64],
                    (summary or "").strip()[:300],
                    payload_json,
                    (expires_at or "").strip()[:64] or None,
                    now,
                    now,
                ),
            )
        return self.get_approval_request(aid)

    def expire_stale_approval_requests(self, now_iso: str | None = None) -> int:
        """expires_at이 지난 pending 행을 expired로 확정 — lazy expiry의
        DB 반영. list/decide 경로가 호출해 읽기-시점 판정과 저장 상태를
        맞춘다."""
        cutoff = now_iso or (_iso(_now()) or "")
        with self.tx() as c:
            cur = c.execute(
                "UPDATE approval_requests SET status='expired', updated_at=?"
                " WHERE status='pending' AND expires_at IS NOT NULL AND expires_at < ?",
                (cutoff, cutoff),
            )
        return int(getattr(cur, "rowcount", 0) or 0)

    def get_approval_request(self, approval_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM approval_requests WHERE id=?", (approval_id,)
        ).fetchone()
        return self._approval_json(row) if row else None

    def list_approval_requests(
        self,
        *,
        status: str | None = None,
        action_type: str | None = None,
        run_id: str | None = None,
        task_id: str | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """승인 요청 조회 — 최신순. 읽기 전에 만료 sweep을 돌려
        expires_at 경과 pending이 'expired'로 보이게 한다."""
        self.expire_stale_approval_requests()
        sql = "SELECT * FROM approval_requests"
        conds: list[str] = []
        params: list[Any] = []
        if status:
            conds.append("status=?")
            params.append(status.strip().lower()[:20])
        if action_type:
            conds.append("action_type=?")
            params.append(action_type.strip().lower()[:40])
        if run_id:
            conds.append("run_id=?")
            params.append(run_id.strip()[:64])
        if task_id:
            conds.append("task_id=?")
            params.append(task_id.strip()[:64])
        if resource_type:
            conds.append("resource_type=?")
            params.append(resource_type.strip().lower()[:40])
        if resource_id:
            conds.append("resource_id=?")
            params.append(resource_id.strip()[:64])
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(_clamp_limit(limit, 100, 500))
        return [self._approval_json(r) for r in self._conn.execute(sql, params).fetchall()]

    def find_pending_approval(
        self, action_type: str, resource_type: str = "", resource_id: str = ""
    ) -> dict[str, Any] | None:
        """같은 action/resource의 pending 요청 — 자동 생성 경로의 중복 방지."""
        self.expire_stale_approval_requests()
        conds = ["status='pending'", "action_type=?"]
        params: list[Any] = [action_type.strip().lower()[:40]]
        if resource_type:
            conds.append("resource_type=?")
            params.append(resource_type.strip().lower()[:40])
        if resource_id:
            conds.append("resource_id=?")
            params.append(resource_id.strip()[:64])
        row = self._conn.execute(
            "SELECT * FROM approval_requests WHERE " + " AND ".join(conds) + " LIMIT 1",  # noqa: S608 - conds는 고정 절만
            params,
        ).fetchone()
        return self._approval_json(row) if row else None

    def has_decided_approval(self, action_type: str, resource_type: str, resource_id: str) -> bool:
        """동일 action/resource의 approved 결정 존재 여부 — agent_terminate
        가드처럼 '승인된 요청이 있는가'만 보는 경로용."""
        row = self._conn.execute(
            "SELECT 1 AS x FROM approval_requests WHERE status='approved'"
            " AND action_type=? AND resource_type=? AND resource_id=? LIMIT 1",
            (
                action_type.strip().lower()[:40],
                resource_type.strip().lower()[:40],
                resource_id.strip()[:64],
            ),
        ).fetchone()
        return row is not None

    def decide_approval_request(
        self,
        approval_id: str,
        decision: str,
        *,
        decided_by: str = "",
        decision_comment: str = "",
    ) -> dict[str, Any] | None:
        """pending → approved|rejected CAS — 동시 결정 중 정확히 하나만 성공.

        expires_at이 지난 행도 결정할 수 없다(만료는 비-pending 취급).
        rowcount 0이면 이미 결정됐거나 만료된 것 — 호출자가 409로 매핑."""
        dec = "approved" if decision == "approve" else "rejected"
        now = _iso(_now())
        with self.tx() as c:
            cur = c.execute(
                "UPDATE approval_requests SET status=?, decided_by=?,"
                " decision_comment=?, decided_at=?, updated_at=?"
                " WHERE id=? AND status='pending'"
                " AND (expires_at IS NULL OR expires_at >= ?)",
                (
                    dec,
                    (decided_by or "").strip()[:200],
                    (decision_comment or "").strip()[:500],
                    now,
                    now,
                    approval_id,
                    now,
                ),
            )
        if int(getattr(cur, "rowcount", 0) or 0) <= 0:
            return None
        return self.get_approval_request(approval_id)

    # ---- recovery_actions (watchdog 복구 추적) ----
    @staticmethod
    def _recovery_json(r: Any) -> dict[str, Any]:
        d = dict(r)
        try:
            d["attempt_count"] = max(0, int(d.get("attempt_count") or 0))
        except (TypeError, ValueError):
            d["attempt_count"] = 0
        try:
            d["max_attempts"] = max(1, int(d.get("max_attempts") or 3))
        except (TypeError, ValueError):
            d["max_attempts"] = 3
        kd = str(d.get("kind") or "").strip().lower()
        d["kind"] = kd if kd in _RECOVERY_KINDS else RecoveryKind.STUCK_RUN.value
        st = str(d.get("status") or "").strip().lower()
        d["status"] = st if st in _RECOVERY_STATUSES else RecoveryStatus.ACTIVE.value
        ot = str(d.get("owner_type") or "").strip().lower()
        d["owner_type"] = ot if ot in _RECOVERY_OWNERS else RecoveryOwnerType.SYSTEM.value
        for k in ("next_attempt_at", "timeout_at", "resolved_at"):
            if d.get(k) is None:
                d[k] = None
        return d

    def open_recovery_for(self, source_type: str, source_id: str) -> dict[str, Any] | None:
        """source의 active|escalated recovery — 중복 생성 전 조회."""
        row = self._conn.execute(
            "SELECT * FROM recovery_actions WHERE source_type=? AND source_id=?"
            " AND status IN ('active','escalated') ORDER BY created_at DESC LIMIT 1",
            (source_type.strip().lower()[:20], source_id.strip()[:64]),
        ).fetchone()
        return self._recovery_json(row) if row else None

    def create_recovery_action(
        self,
        *,
        source_type: str,
        source_id: str,
        kind: str,
        max_attempts: int = 3,
        owner_type: str = "system",
        timeout_at: str | None = None,
        next_attempt_at: str | None = None,
    ) -> tuple[dict[str, Any] | None, bool]:
        """복구 액션 생성 — (row, created) 반환. source당 open 1건.

        사전 조회 + 부분 UNIQUE 인덱스(idx_recovery_actions_open)의
        이중 방어 — 인덱스 위반(동시 생성 레이스) 시 기존 행을 반환하고
        created=False. 인덱스가 없는 더티 DB에서도 사전 조회가 막는다."""
        st = (source_type or "").strip().lower()[:20]
        sid = (source_id or "").strip()[:64]
        if not st or not sid:
            return None, False
        existing = self.open_recovery_for(st, sid)
        if existing is not None:
            return existing, False
        try:
            max_att = max(1, int(max_attempts))
        except (TypeError, ValueError):
            max_att = 3
        now = _iso(_now())
        rid = uuid.uuid4().hex[:12]
        try:
            with self.tx() as c:
                c.execute(
                    "INSERT INTO recovery_actions(id,source_type,source_id,kind,status,"
                    "attempt_count,max_attempts,next_attempt_at,timeout_at,owner_type,"
                    "created_at,updated_at) VALUES(?,?,?,?,'active',0,?,?,?,?,?,?)",
                    (
                        rid,
                        st,
                        sid,
                        (kind or "").strip().lower()[:30],
                        max_att,
                        (next_attempt_at or "").strip()[:64] or None,
                        (timeout_at or "").strip()[:64] or None,
                        (owner_type or "system").strip().lower()[:20],
                        now,
                        now,
                    ),
                )
        except self._integrity_errors:
            existing = self.open_recovery_for(st, sid)
            if existing is not None:
                return existing, False
            raise
        return self.get_recovery_action(rid), True

    def get_recovery_action(self, recovery_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM recovery_actions WHERE id=?", (recovery_id,)
        ).fetchone()
        return self._recovery_json(row) if row else None

    def list_recovery_actions(
        self,
        *,
        status: str | None = None,
        kind: str | None = None,
        source_type: str | None = None,
        source_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM recovery_actions"
        conds: list[str] = []
        params: list[Any] = []
        if status:
            conds.append("status=?")
            params.append(status.strip().lower()[:20])
        if kind:
            conds.append("kind=?")
            params.append(kind.strip().lower()[:30])
        if source_type:
            conds.append("source_type=?")
            params.append(source_type.strip().lower()[:20])
        if source_id:
            conds.append("source_id=?")
            params.append(source_id.strip()[:64])
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(_clamp_limit(limit, 100, 500))
        return [self._recovery_json(r) for r in self._conn.execute(sql, params).fetchall()]

    def list_open_recovery_actions(self, limit: int = 200) -> list[dict[str, Any]]:
        """active|escalated 복구 액션 — watchdog 주기별 건강 재검사용.

        active를 escalated보다 먼저 정렬 — 오랫동안 escalated로 남은
        행이 limit을 채워 최신 active 복구가 스캔에서 밀리는 기아를
        막는다(건강 재검사는 둘 다 대상이지만 재시도 가능 건이 우선)."""
        rows = self._conn.execute(
            "SELECT * FROM recovery_actions WHERE status IN ('active','escalated')"
            " ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END,"
            " created_at, id LIMIT ?",
            (_clamp_limit(limit, 200, 500),),
        ).fetchall()
        return [self._recovery_json(r) for r in rows]

    def record_recovery_attempt(
        self,
        recovery_id: str,
        *,
        outcome: str = "",
        next_attempt_at: str | None = None,
        owner_type: str | None = None,
    ) -> bool:
        """attempt_count+1 + 다음 시도 시각/outcome 갱신 — open 상태에서만."""
        sets = "attempt_count=attempt_count+1, updated_at=?"
        params: list[Any] = [_iso(_now())]
        if outcome:
            sets += ", outcome=?"
            params.append(outcome[:200])
        if next_attempt_at is not None:
            sets += ", next_attempt_at=?"
            params.append(next_attempt_at[:64] or None)
        if owner_type:
            sets += ", owner_type=?"
            params.append(owner_type[:20])
        params.extend([recovery_id])
        with self.tx() as c:
            cur = c.execute(
                f"UPDATE recovery_actions SET {sets}"  # noqa: S608 - sets는 고정 fragment
                " WHERE id=? AND status IN ('active','escalated')",
                tuple(params),
            )
        return int(getattr(cur, "rowcount", 0) or 0) > 0

    def set_recovery_status(
        self,
        recovery_id: str,
        status: str,
        *,
        outcome: str | None = None,
        resolution_note: str = "",
        owner_type: str | None = None,
    ) -> bool:
        """open → resolved|cancelled|escalated 전이 — 'active'는 시작 상태라
        목표로 허용하지 않는다. escalated도 open 상태라 이후
        resolved/cancelled로 진행 가능하고, 종료 상태는 되돌리지 않는다."""
        st = (status or "").strip().lower()[:20]
        if st not in _RECOVERY_STATUSES or st == RecoveryStatus.ACTIVE.value:
            return False
        now = _iso(_now())
        resolved_at = now if st == RecoveryStatus.RESOLVED.value else None
        with self.tx() as c:
            cur = c.execute(
                "UPDATE recovery_actions SET status=?,"
                " outcome=COALESCE(?, outcome),"
                " resolution_note=CASE WHEN ?<>'' THEN ? ELSE resolution_note END,"
                " owner_type=COALESCE(?, owner_type),"
                " resolved_at=COALESCE(?, resolved_at), updated_at=?"
                " WHERE id=? AND status IN ('active','escalated')",
                (
                    st,
                    (outcome or "")[:200] or None,
                    resolution_note,
                    resolution_note[:500],
                    (owner_type or "")[:20] or None,
                    resolved_at,
                    now,
                    recovery_id,
                ),
            )
        return int(getattr(cur, "rowcount", 0) or 0) > 0

    # ---- watchdog 감지 쿼리 ----
    def agents_with_stale_heartbeat(
        self, cutoff_iso: str, limit: int = 200
    ) -> list[dict[str, Any]]:
        """external kind + 작업 상태 agent 중 last_seen_at이 cutoff보다 오래된 행.

        workflow kind는 서버 heartbeat가 없어 항상 stale로 오인되므로 제외.
        last_seen_at NULL(한 번도 heartbeat 없음)은 미연결로 보고 제외 —
        생성 직후/미배포 agent의 오탐을 막는다.
        가장 오래된 것부터 limit까지만 — 대량 stale 시 detector 후보
        리스트가 무한정 커지는 것을 막는다."""
        placeholders = ",".join("?" for _ in _HEARTBEAT_AGENT_STATUSES)
        rows = self._conn.execute(
            "SELECT * FROM agents WHERE kind='external'"  # noqa: S608 - placeholders는 고정 '?' 나열
            f" AND status IN ({placeholders})"
            " AND last_seen_at IS NOT NULL AND last_seen_at < ?"
            " ORDER BY last_seen_at LIMIT ?",
            (
                *_HEARTBEAT_AGENT_STATUSES,
                cutoff_iso,
                _clamp_limit(limit, 200, 500),
            ),
        ).fetchall()
        return [self._agent_json(r) for r in rows]

    def stranded_in_progress_tasks(self, limit: int = 200) -> list[dict[str, Any]]:
        """in_progress인데 checkout도 활성 run(current_run_id/task_runs)도 없는 task.

        활성 run 판정은 current_task_for_agent와 동일한 상태 집합 —
        queued/running/waiting_*은 아직 살아있는 작업으로 본다."""
        ph = ",".join("?" for _ in _ACTIVE_RUN_STATUSES)
        rows = self._conn.execute(
            "SELECT t.* FROM tasks t"  # noqa: S608 - placeholders는 고정 '?' 나열
            " WHERE t.status='in_progress' AND t.checkout_agent_id IS NULL"
            " AND NOT EXISTS ("
            "  SELECT 1 FROM runs r WHERE r.id = t.current_run_id"
            f"  AND r.status IN ({ph})"
            " )"
            " AND NOT EXISTS ("
            "  SELECT 1 FROM task_runs tr JOIN runs r2 ON r2.id = tr.run_id"
            f"  WHERE tr.task_id = t.id AND r2.status IN ({ph})"
            " )"
            " ORDER BY t.updated_at LIMIT ?",
            (*_ACTIVE_RUN_STATUSES, *_ACTIVE_RUN_STATUSES, _clamp_limit(limit, 200, 500)),
        ).fetchall()
        return [self._task_json(r) for r in rows]

    def stuck_runs(self, cutoff_iso: str, limit: int = 200) -> list[Any]:
        """waiting_*/queued 상태로 cutoff보다 오래 updated_at이 없는 run.

        _reap_stale_runs가 30분에 queued/running을 failed로 마감하므로
        여기서는 더 긴 꼬리(기본 24h)만 잡는다 — 중복 fail은 없다."""
        ph = ",".join("?" for _ in _STUCK_RUN_STATUSES)
        rows = self._conn.execute(
            "SELECT * FROM runs WHERE status IN ("  # noqa: S608 - placeholders는 고정 '?' 나열
            f"{ph}) AND updated_at < ? ORDER BY updated_at LIMIT ?",
            (*_STUCK_RUN_STATUSES, cutoff_iso, _clamp_limit(limit, 200, 500)),
        ).fetchall()
        return [self._row_to_run(r) for r in rows]

    def terminal_runs_missing_cost(self, limit: int = 200) -> list[Any]:
        """terminal run 중 토큰 사용 stage_run이 있는데 그 stage_run을
        커버하는 cost event가 없는 run — 비용 미보고 감지.

        stage_run 커버리지 규칙(store/costs.py stage_runs_missing_cost_events와
        동일): cost_events.stage_run_id = sr.id 인 이벤트가 있으면 그
        stage_run은 커버됨. run_id만 있고 stage_run_id가 NULL인 레거시
        이벤트는 해당 run 전체를 커버하는 것으로 본다(기존 동작 유지).
        토큰 0 stage_run만 있는 run은 emit이 기록하지 않으므로 제외해
        영구 미커버 오탐을 막는다."""
        ph = ",".join("?" for _ in _TERMINAL_RUN_STATUSES)
        rows = self._conn.execute(
            "SELECT r.* FROM runs r"  # noqa: S608 - placeholders는 고정 '?' 나열
            f" WHERE r.status IN ({ph})"
            " AND EXISTS ("
            "  SELECT 1 FROM stage_runs sr"
            "  WHERE sr.run_id = r.id"
            "  AND sr.finished_at IS NOT NULL"
            "  AND COALESCE(sr.tokens_in,0) + COALESCE(sr.tokens_out,0) > 0"
            "  AND NOT EXISTS (SELECT 1 FROM cost_events ce"
            "   WHERE ce.stage_run_id = sr.id)"
            "  AND NOT EXISTS (SELECT 1 FROM cost_events ce2"
            "   WHERE ce2.run_id = r.id AND ce2.stage_run_id IS NULL)"
            " )"
            " ORDER BY r.updated_at LIMIT ?",
            (*_TERMINAL_RUN_STATUSES, _clamp_limit(limit, 200, 500)),
        ).fetchall()
        return [self._row_to_run(r) for r in rows]

    def run_has_uncovered_cost_stage(self, run_id: str) -> bool:
        """run에 미커버 토큰 stage_run이 남아 있으면 True — missing_cost
        건강 재검사용. terminal_runs_missing_cost와 동일 커버리지 규칙."""
        row = self._conn.execute(
            "SELECT 1 FROM stage_runs sr"
            " WHERE sr.run_id = ? AND sr.finished_at IS NOT NULL"
            " AND COALESCE(sr.tokens_in,0) + COALESCE(sr.tokens_out,0) > 0"
            " AND NOT EXISTS (SELECT 1 FROM cost_events ce"
            "  WHERE ce.stage_run_id = sr.id)"
            " AND NOT EXISTS (SELECT 1 FROM cost_events ce2"
            "  WHERE ce2.run_id = ? AND ce2.stage_run_id IS NULL)"
            " LIMIT 1",
            (run_id, run_id),
        ).fetchone()
        return row is not None

    def runs_with_failed_resume(self, since_iso: str, limit: int = 200) -> list[Any]:
        """승인 후 resume 실패(run_resume_error audit)로 다시 waiting_approval에
        멈춘 run — 이후 승인/거절 결정이 없는 것만.

        since_iso 이후의 resume_error만 본다 — 오래된 과거 실패는 이미
        사람이 처리했거나 재승인된 상태일 수 있어 노이즈를 줄인다."""
        rows = self._conn.execute(
            "SELECT * FROM runs r WHERE r.status='waiting_approval'"
            " AND EXISTS (SELECT 1 FROM audit_events a WHERE a.run_id = r.id"
            "  AND a.action='run_resume_error' AND a.created_at >= ?)"
            " AND NOT EXISTS (SELECT 1 FROM audit_events a2 WHERE a2.run_id = r.id"
            "  AND a2.action IN ('run_approve','run_reject')"
            "  AND a2.created_at > COALESCE(("
            "   SELECT MAX(a3.created_at) FROM audit_events a3"
            "   WHERE a3.run_id = r.id AND a3.action='run_resume_error'), ''))"
            " ORDER BY r.updated_at LIMIT ?",
            (since_iso, _clamp_limit(limit, 200, 500)),
        ).fetchall()
        return [self._row_to_run(r) for r in rows]
