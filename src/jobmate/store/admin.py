# mypy: disable-error-code="attr-defined"
"""Store mixin — 관리자/보존/내보내기/헬스 집계 메서드 (db.py에서 verbatim 이동)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .util import _clamp_limit, _iso, _loads, _now

# retention 삭제 대상 run 상태 — wakeups._TERMINAL_STATUSES와 동일 집합.
# 고정 SQL 조각(리터럴)이라 f-string 삽입해도 인젝션 위험이 없다.
_TERMINAL_RUN_STATUSES_SQL = (
    "('completed','failed','rejected','cancelled','budget_exhausted','needs_human')"
)


class AdminStoreMixin:
    def retention_sweep(self, now: datetime | None = None) -> dict[str, int]:
        """보존 기간 경과 데이터 정리 — cron tick에서 하루 1회 호출 권장.

        - audit_events: created_at < now-90d 삭제
        - llm_usage: created_at < now-180d 삭제
        - auth_sessions: expires_at < now 삭제(만료 세션)
        - runs/stage_runs: 종결 상태(completed|failed|rejected|cancelled|
          budget_exhausted|needs_human — wakeups._TERMINAL_STATUSES와 동일)이고
          created_at < now-180d 삭제
          (진행 중 queued/running/waiting_* 는 절대 삭제하지 않는다)
        - message_extractions: processed 이고 created_at < now-30d면
          source_text만 비움(행은 유지 — 재처리 방지용 키/상태 보존)
        - agent_session_events: created_at < now-30d 삭제
        - wakeup_requests: finished_at < now-90d 삭제
          (미완료 queued/deferred는 진행 중 요청일 수 있어 보존)
        - budget_incidents: open 아닌 행 created_at < now-180d 삭제
          (open 인시던트는 정책 감시 중이므로 보존)
        - eval_runs: status <> 'pending'이고 created_at < now-365d 삭제
          (pending은 lazy 채점 대기 중이므로 보존; evals 픽스처 자체는
          이력이므로 유지)
        - agent_events: created_at < now-90d 삭제 — heartbeat 등 고빈도
          이벤트 포함 전체 타입 동일 기준. tasks/task_runs는 업무 이력
          소유권이라 별도 정책이 없어 보존한다.
        반환값: 테이블별 삭제/정리 건수."""
        now = now or _now()
        d30 = _iso(now - timedelta(days=30))
        d90 = _iso(now - timedelta(days=90))
        d180 = _iso(now - timedelta(days=180))
        d365 = _iso(now - timedelta(days=365))
        now_iso = _iso(now)
        counts: dict[str, int] = {}
        with self.tx() as c:
            cur = c.execute("DELETE FROM audit_events WHERE created_at < ?", (d90,))
            counts["audit_events"] = int(getattr(cur, "rowcount", 0) or 0)
            cur = c.execute("DELETE FROM llm_usage WHERE created_at < ?", (d180,))
            counts["llm_usage"] = int(getattr(cur, "rowcount", 0) or 0)
            cur = c.execute("DELETE FROM auth_sessions WHERE expires_at < ?", (now_iso,))
            counts["auth_sessions"] = int(getattr(cur, "rowcount", 0) or 0)
            cur = c.execute("DELETE FROM agent_session_events WHERE created_at < ?", (d30,))
            counts["agent_session_events"] = int(getattr(cur, "rowcount", 0) or 0)
            cur = c.execute(
                "UPDATE message_extractions SET source_text=''"
                " WHERE processing_status='processed' AND created_at < ? AND source_text <> ''",
                (d30,),
            )
            counts["message_extractions_cleared"] = int(getattr(cur, "rowcount", 0) or 0)
            # 종결 run만 삭제 — 자식(stage_runs/approvals/run_comments)부터 정리.
            # IN 리스트 대신 동일 조건의 서브쿼리로 지워 바인딩 변수 수를
            # 일정하게 유지한다(삭제 대상 수와 무관하게 단일 문장).
            cur = c.execute(
                "DELETE FROM stage_runs WHERE run_id IN "  # noqa: S608
                f"(SELECT id FROM runs WHERE status IN {_TERMINAL_RUN_STATUSES_SQL}"
                " AND created_at < ?)",
                (d180,),
            )
            counts["stage_runs"] = int(getattr(cur, "rowcount", 0) or 0)
            cur = c.execute(
                "DELETE FROM approvals WHERE run_id IN "  # noqa: S608
                f"(SELECT id FROM runs WHERE status IN {_TERMINAL_RUN_STATUSES_SQL}"
                " AND created_at < ?)",
                (d180,),
            )
            counts["approvals"] = int(getattr(cur, "rowcount", 0) or 0)
            cur = c.execute(
                "DELETE FROM run_comments WHERE run_id IN "  # noqa: S608
                f"(SELECT id FROM runs WHERE status IN {_TERMINAL_RUN_STATUSES_SQL}"
                " AND created_at < ?)",
                (d180,),
            )
            counts["run_comments"] = int(getattr(cur, "rowcount", 0) or 0)
            cur = c.execute(
                f"DELETE FROM runs WHERE status IN {_TERMINAL_RUN_STATUSES_SQL} AND created_at < ?",  # noqa: S608
                (d180,),
            )
            counts["runs"] = int(getattr(cur, "rowcount", 0) or 0)
            cur = c.execute(
                "DELETE FROM wakeup_requests WHERE finished_at IS NOT NULL AND finished_at < ?",
                (d90,),
            )
            counts["wakeup_requests"] = int(getattr(cur, "rowcount", 0) or 0)
            cur = c.execute(
                "DELETE FROM budget_incidents WHERE status <> 'open' AND created_at < ?",
                (d180,),
            )
            counts["budget_incidents"] = int(getattr(cur, "rowcount", 0) or 0)
            # eval_runs 채점 이력 — pending(lazy 채점 대기)은 보존.
            cur = c.execute(
                "DELETE FROM eval_runs WHERE status <> 'pending' AND created_at < ?",
                (d365,),
            )
            counts["eval_runs"] = int(getattr(cur, "rowcount", 0) or 0)
            # agent_events — 90일 경과분 전부 삭제(타입 무관). 멱등 replay는
            # 최근 키만 의미가 있고, 장기 보존 감사는 audit_events(90d)가 담당.
            cur = c.execute("DELETE FROM agent_events WHERE created_at < ?", (d90,))
            counts["agent_events"] = int(getattr(cur, "rowcount", 0) or 0)
        return counts

    # ---- agent_sessions 브릿지/헬스 집계 ----
    def agent_sessions_bridge_info(self) -> dict[str, Any]:
        """last_push_at(전체 최신 updated_at) + 머신별 그룹 집계."""
        row = self._conn.execute(
            "SELECT MAX(updated_at) AS last_push_at FROM agent_sessions"
        ).fetchone()
        last_push_at = row["last_push_at"] if row else None
        rows = self._conn.execute(
            "SELECT machine, MAX(updated_at) AS last_push_at, COUNT(*) AS sessions"
            " FROM agent_sessions GROUP BY machine"
        ).fetchall()
        machines = [
            {
                "machine": r["machine"] or "",
                "last_push_at": r["last_push_at"],
                "sessions": int(r["sessions"]),
            }
            for r in rows
        ]
        return {"last_push_at": last_push_at, "machines": machines}

    # ---- users 관리 ----
    def list_users(self) -> list[dict[str, Any]]:
        """전체 사용자 — 비밀번호 해시 제외."""
        rows = self._conn.execute(
            "SELECT id,email,display_name,role,created_at FROM users ORDER BY created_at"
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["role"] = self._norm_role(d.get("role"))
            out.append(d)
        return out

    def set_user_role(self, user_id: str, role: str) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE users SET role=?, updated_at=? WHERE id=?",
                (role, _iso(_now()), user_id),
            )

    def set_user_password(self, user_id: str, password_hash: str) -> None:
        """비밀번호 해시 교체 — 호출 측에서 현재 비밀번호 검증 + 세션 revoke 책임."""
        with self.tx() as c:
            c.execute(
                "UPDATE users SET password_hash=?, updated_at=? WHERE id=?",
                (password_hash, _iso(_now()), user_id),
            )

    def promote_to_admin_if_first(self, user_id: str) -> bool:
        """사용자가 유일한 사용자일 때만 admin 승격 — 단일 UPDATE 조건으로
        동시 가입 레이스에서 둘 다 admin이 되거나 둘 다 놓치는 것을 막는다.
        반환값: 실제로 승격됐으면 True."""
        with self.tx() as c:
            cur = c.execute(
                "UPDATE users SET role='admin', updated_at=?"
                " WHERE id=? AND (SELECT COUNT(*) FROM users)=1",
                (_iso(_now()), user_id),
            )
            return int(getattr(cur, "rowcount", 0) or 0) > 0

    def delete_user_cascade(self, user_id: str) -> dict[str, int]:
        """계정 삭제(개인정보보호법 대응) — users + auth_sessions +
        email_verifications + 본인 귀속 데이터(run_comments)를 정리한다.

        audit_events는 법적 보존이 필요할 수 있으므로 삭제하지 않고
        actor만 'deleted_user'로 마스킹해 유지한다. 호출 측에서 삭제 행위를
        audit에 먼저 기록해야 한다(마스킹 후에도 action은 남는다).
        타 사용자 데이터는 절대 건드리지 않는다.
        반환값: 테이블별 삭제 건수."""
        user = self.get_user_by_id(user_id)
        email = str((user or {}).get("email") or "").lower()
        counts: dict[str, int] = {}
        with self.tx() as c:
            cur = c.execute("DELETE FROM auth_sessions WHERE user_id=?", (user_id,))
            counts["auth_sessions"] = int(getattr(cur, "rowcount", 0) or 0)
            if email:
                cur = c.execute("DELETE FROM email_verifications WHERE email=?", (email,))
                counts["email_verifications"] = int(getattr(cur, "rowcount", 0) or 0)
                c.execute(
                    "UPDATE audit_events SET actor='deleted_user' WHERE actor=? OR lower(actor)=?",
                    (user_id, email),
                )
                c.execute(
                    "DELETE FROM run_comments WHERE user_id=? OR lower(email)=?",
                    (user_id, email),
                )
            cur = c.execute("DELETE FROM users WHERE id=?", (user_id,))
            counts["users"] = int(getattr(cur, "rowcount", 0) or 0)
        return counts

    def export_user_data(self, user_id: str) -> dict[str, Any]:
        """본인 데이터 export(개인정보 열람권) — 비밀번호 해시·세션 토큰
        해시·인증 토큰 해시는 제외하고 본인 귀속 데이터만 반환한다."""
        user = self.get_user_by_id(user_id) or {}
        email = str(user.get("email") or "").lower()
        with self.tx() as c:
            sess_rows = c.execute(
                "SELECT created_at, expires_at, revoked_at FROM auth_sessions"
                " WHERE user_id=? ORDER BY created_at DESC",
                (user_id,),
            ).fetchall()
            audit_rows = c.execute(
                "SELECT run_id,actor,action,object_ref,detail,created_at"
                " FROM audit_events WHERE actor=? OR lower(actor)=?"
                " ORDER BY created_at DESC LIMIT 1000",
                (user_id, email),
            ).fetchall()
            comment_rows = c.execute(
                "SELECT run_id,body,created_at FROM run_comments"
                " WHERE user_id=? OR lower(email)=? ORDER BY created_at DESC LIMIT 1000",
                (user_id, email),
            ).fetchall()
            verify_rows = c.execute(
                "SELECT email,expires_at,used_at,created_at FROM email_verifications"
                " WHERE email=? ORDER BY created_at DESC",
                (email,),
            ).fetchall()
        safe_user = {k: v for k, v in user.items() if k != "password_hash"}
        return {
            "exported_at": _iso(_now()),
            "user": safe_user,
            "sessions": [dict(r) for r in sess_rows],
            "audit_events": [
                {
                    "run_id": r["run_id"],
                    "actor": r["actor"],
                    "action": r["action"],
                    "target": r["object_ref"],
                    "detail": _loads(r["detail"]),
                    "created_at": r["created_at"],
                }
                for r in audit_rows
            ],
            "run_comments": [dict(r) for r in comment_rows],
            "email_verifications": [dict(r) for r in verify_rows],
        }

    def list_audit(self, limit: int = 200) -> list[dict[str, Any]]:
        """전체 audit_events 최신순 — 관리자 감사 타임라인용."""
        rows = self._conn.execute(
            "SELECT id,run_id,actor,action,object_ref,detail,created_at"
            " FROM audit_events ORDER BY created_at DESC, id DESC LIMIT ?",
            (_clamp_limit(limit, 200, 1000),),
        ).fetchall()
        return [
            {
                "id": r["id"],
                "run_id": r["run_id"],
                "actor": r["actor"],
                "action": r["action"],
                "target": r["object_ref"],
                "detail": _loads(r["detail"]),
                "ts": r["created_at"],
            }
            for r in rows
        ]

    # ---- team 뷰 (다중 사용자 활동 집계) ----
    def team_activity(self) -> list[dict[str, Any]]:
        """users × runs.created_by × auth_sessions 집계.

        created_by는 세션 사용자 email(없으면 'api-key'/'local' 등 시스템 값) —
        email/id 어느 쪽으로 기록됐든 매칭한다."""
        users = self.list_users()
        run_rows = self._conn.execute(
            "SELECT created_by, COUNT(*) AS n, MAX(created_at) AS last_at"
            " FROM runs WHERE created_by != '' GROUP BY created_by"
        ).fetchall()
        sess_rows = self._conn.execute(
            "SELECT user_id, COUNT(*) AS n, MAX(created_at) AS last_at"
            " FROM auth_sessions GROUP BY user_id"
        ).fetchall()
        run_by_actor = {r["created_by"]: dict(r) for r in run_rows}
        sess_by_user = {r["user_id"]: dict(r) for r in sess_rows}
        items = []
        for u in users:
            uid, email = u["id"], u["email"]
            rr = run_by_actor.get(email) or run_by_actor.get(uid)
            sr = sess_by_user.get(uid)
            last_active = ""
            for cand in (
                (rr or {}).get("last_at"),
                (sr or {}).get("last_at"),
            ):
                if cand and cand > last_active:
                    last_active = cand
            items.append(
                {
                    "id": uid,
                    "email": email,
                    "name": u.get("display_name", ""),
                    "role": self._norm_role(u.get("role", "user")),
                    "run_count": int((rr or {}).get("n") or 0),
                    "session_count": int((sr or {}).get("n") or 0),
                    "last_active_at": last_active or None,
                }
            )
        items.sort(key=lambda x: (x["last_active_at"] or "", x["email"]), reverse=True)
        return items

    # ---- export 헬퍼 ----
    def export_runs(self, since_iso: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT id,workflow_key,workflow_version,status,trace_id,error,"
            "created_by,agent_id,goal_id,created_at,updated_at FROM runs"
            " WHERE created_at >= ? ORDER BY created_at",
            (since_iso,),
        ).fetchall()
        return [dict(r) for r in rows]

    def export_sessions(self, since_iso: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT tool,session_key,project,title,status,machine,"
            "tokens_in,tokens_out,last_event_at,created_at,updated_at"
            " FROM agent_sessions WHERE updated_at >= ? ORDER BY updated_at",
            (since_iso,),
        ).fetchall()
        return [dict(r) for r in rows]

    def export_reports(self, since_iso: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT id,kind,content,created_at FROM reports WHERE created_at >= ?"
            " ORDER BY created_at",
            (since_iso,),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["content"] = _loads(d.get("content") or "{}")
            out.append(d)
        return out

    def stage_run_rows(self, since_iso: str) -> list[dict[str, Any]]:
        """since 이후 생성된 run의 stage_runs + run 컨텍스트 JOIN.

        리스크(7d 단계 실패)·성과 비교·프로젝트 상세(30d 비용) 집계용.
        반환 dict: stage 필드 + workflow_key/project/run_status/run_created_at/
        run_updated_at."""
        rows = self._conn.execute(
            "SELECT sr.run_id, sr.stage_key, sr.attempt, sr.status, sr.model,"
            " sr.tokens_in, sr.tokens_out, sr.started_at, sr.finished_at, sr.error,"
            " r.workflow_key, r.project, r.status AS run_status,"
            " r.created_at AS run_created_at, r.updated_at AS run_updated_at"
            " FROM stage_runs sr JOIN runs r ON r.id = sr.run_id"
            " WHERE r.created_at >= ? ORDER BY sr.started_at",
            (since_iso,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- SSE 스트림용 증분 조회 ----
    def activity_since(self, since_iso: str, limit: int = 500) -> dict[str, list[dict[str, Any]]]:
        """since_iso(ISO8601, 경계 포함) 이후 갱신된 행을 테이블별로 반환.

        SSE 엔드포인트의 폴링 tailing용. 각 행의 "ts"는 워터마크 비교용
        타임스탬프(runs=updated_at, stages=COALESCE(finished_at,started_at),
        sessions=created_at). 같은 ts의 복수 행은 호출자가 id로 중복 제거한다.
        """
        out: dict[str, list[dict[str, Any]]] = {"runs": [], "stages": [], "sessions": []}
        rows = self._conn.execute(
            "SELECT id, workflow_key, status, project, created_at, updated_at"
            " FROM runs WHERE updated_at >= ? ORDER BY updated_at, id LIMIT ?",
            (since_iso, limit),
        ).fetchall()
        for r in rows:
            out["runs"].append(
                {
                    "id": r["id"],
                    "workflow": r["workflow_key"],
                    "status": r["status"],
                    "project": r["project"] or "",
                    "created_at": r["created_at"],
                    "updated_at": r["updated_at"],
                    "ts": r["updated_at"],
                }
            )
        rows = self._conn.execute(
            "SELECT id, run_id, stage_key, attempt, status, started_at, finished_at"
            " FROM stage_runs"
            " WHERE COALESCE(finished_at, started_at) >= ?"
            " ORDER BY COALESCE(finished_at, started_at), id LIMIT ?",
            (since_iso, limit),
        ).fetchall()
        for r in rows:
            ts = r["finished_at"] or r["started_at"] or ""
            out["stages"].append(
                {
                    "id": r["id"],
                    "run_id": r["run_id"],
                    "stage_key": r["stage_key"],
                    "attempt": r["attempt"],
                    "status": r["status"],
                    "started_at": r["started_at"],
                    "finished_at": r["finished_at"],
                    "ts": ts,
                }
            )
        rows = self._conn.execute(
            "SELECT id, tool, session_key, status, project, doing, ask, created_at"
            " FROM agent_session_events WHERE created_at >= ?"
            " ORDER BY created_at, id LIMIT ?",
            (since_iso, limit),
        ).fetchall()
        for r in rows:
            out["sessions"].append(
                {
                    "id": r["id"],
                    "tool": r["tool"],
                    "session_key": r["session_key"],
                    "status": r["status"],
                    "project": r["project"] or "",
                    "doing": r["doing"] or "",
                    "ask": r["ask"] or "",
                    "created_at": r["created_at"],
                    "ts": r["created_at"],
                }
            )
        return out

    # ---- db 메타 (헬스 상세) ----
    def db_info(self) -> dict[str, Any]:
        """백엔드 종류/경로/SQLite 파일 크기(PG는 size_bytes=None)."""
        if self._pg:
            return {"type": "postgresql", "path": None, "size_bytes": None}
        path = str(self._path)
        size = None
        try:
            size = self._path.stat().st_size
        except OSError:
            pass
        return {"type": "sqlite", "path": path, "size_bytes": size}
