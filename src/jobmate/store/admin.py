# mypy: disable-error-code="attr-defined"
"""Store mixin — 관리자/보존/내보내기/헬스 집계 메서드 (db.py에서 verbatim 이동)."""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any

from . import orgctx
from .util import _clamp_limit, _iso, _loads, _now

logger = logging.getLogger(__name__)


def _org_filter() -> tuple[str, list[Any]]:
    """ctx 조직이 있으면 ` AND org_id=?` 조건 — 시스템/미인증(ctx='')은 무필터."""
    oid = orgctx.current_org_id()
    return (" AND org_id=?", [oid]) if oid else ("", [])


def _audit_like_escape(s: str) -> str:
    """LIKE 패턴 이스케이프 — action prefix 매칭에서 %/_/\\를 리터럴로."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def encode_audit_cursor(created_at: str, row_id: str) -> str:
    """keyset cursor 인코딩 — (created_at,id) 복합 키의 불투명 문자열."""
    return base64.urlsafe_b64encode(f"{created_at}|{row_id}".encode()).decode()


def decode_audit_cursor(cursor: str) -> tuple[str, str] | None:
    """encode_audit_cursor 역변환 — 형식이 아니면 None."""
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        ts, sep, rid = raw.partition("|")
        if not sep or not ts or not rid:
            return None
        return ts, rid
    except Exception:
        return None


# retention 삭제 대상 run 상태 — wakeups._TERMINAL_STATUSES와 동일 집합.
# 고정 SQL 조각(리터럴)이라 f-string 삽입해도 인젝션 위험이 없다.
_TERMINAL_RUN_STATUSES_SQL = (
    "('completed','failed','rejected','cancelled','budget_exhausted','needs_human')"
)

# FR-014 anonymize 대상 — (table, column). 값이 user_id 또는 email과
# 일치하는 행의 행위자 식별자를 'deleted_user'로 치환한다(행 자체는 보존).
# approvals.decided_by/approval_requests.requested_by·decided_by는
# 'local'/에이전트 id 등 비사용자 값도 저장되지만 user_id/email 매칭만
# 갱신하므로 오탐이 없다.
_ANONYMIZE_ACTOR_COLUMNS: tuple[tuple[str, str], ...] = (
    ("runs", "created_by"),
    ("agent_events", "actor"),
    ("agents", "created_by"),
    ("goals", "created_by"),
    ("evals", "created_by"),
    ("tasks", "created_by"),
    ("playbooks", "created_by"),
    ("workflow_revisions", "created_by"),
    ("projects", "created_by"),
    ("task_artifacts", "created_by"),
    ("routines", "created_by"),
    ("organizations", "created_by"),
    ("webhook_subscriptions", "created_by"),
    ("approvals", "decided_by"),
    ("approval_requests", "requested_by"),
    ("approval_requests", "decided_by"),
)
# author_id는 user_id(uuid)만 저장한다 — email 매칭 대상이 아니다.
_ANONYMIZE_USER_ID_ONLY_COLUMNS: tuple[tuple[str, str], ...] = (("task_comments", "author_id"),)


class AdminStoreMixin:
    def retention_sweep(self, now: datetime | None = None) -> dict[str, int]:
        """보존 기간 경과 데이터 정리 — cron tick에서 하루 1회 호출 권장.

        spec 005 US1 이후 실제 purge는 store/retention.py의 정책 엔진
        (`run_purge`)이 수행한다 — 유형별 TTL은 retention_policies DB
        override > 코드 기본값 순으로 결정되고, 실행은 purge_runs에,
        요약은 retention_purge 감사 이벤트로 기록된다. 이 메서드는
        기존 시그니처를 유지하는 얇은 위임이다.

        정책 유형(기본 TTL): audit_events 365d(법정 하한), llm_usage 180d,
        auth_sessions 만료 기준, runs+자식(종결 상태만) 180d,
        message_extractions source_text 비우기 30d, agent_session_events 30d,
        wakeup_requests 90d(완료분만), budget_incidents 180d(open 제외),
        eval_runs 365d(pending 제외), agent_events 90d,
        webhook_deliveries 90d(종결만), org_invitations 30d,
        inbox_items/notifications/email_summaries 90d.
        여기에 정책 외 고정 정리(만료 password_reset_tokens/
        email_verifications/mfa_tickets)와 삭제 조직 purge가 추가된다.
        반환값: 테이블별 삭제/정리 건수."""
        result = self.run_purge(dry_run=False, actor="retention_sweep", now=now or _now())
        counts = dict(result.get("counts") or {})
        counts.update(self.purge_deleted_organizations(now=now))
        return counts

    def purge_deleted_organizations(
        self, retention_days: int = 30, now: datetime | None = None
    ) -> dict[str, int]:
        """soft-delete 후 retention_days 경과한 조직의 org 스코프 행 삭제(FR-028).

        audit_events는 법적 보존을 위해 유지 — organizations/멤버십/초대/
        user_permissions 포함 나머지 org 스코프 테이블을 org_id로 삭제한다.
        조직당 단일 tx — 한 조직의 실패가 다른 조직 purge를 막지 않는다."""
        from .migrations import _ORG_SCOPED_TABLES

        cutoff = _iso((now or _now()) - timedelta(days=retention_days))
        rows = self._conn.execute(
            "SELECT id FROM organizations WHERE status='deleted' AND updated_at < ?",
            (cutoff,),
        ).fetchall()
        purged_orgs = 0
        total_rows = 0
        tables = [t for t in _ORG_SCOPED_TABLES if t != "audit_events"]
        tables += ["organization_members", "org_invitations"]
        for r in rows:
            oid = str(r["id"])
            try:
                with self.tx() as c:
                    # org_id 컬럼이 없는 자식 테이블을 부모보다 먼저 정리한다.
                    # PG에서는 stage_runs/approvals가 runs.id를 REFERENCES하므로
                    # 이 순서가 아니면 FK 위반으로 purge 전체가 실패한다.
                    # 나머지 테이블도 고아 행을 남기지 않도록 runs/tasks/
                    # routines/triggers/agent_sessions 기준으로 삭제한다.
                    for sql, params in (
                        (
                            "DELETE FROM stage_runs WHERE run_id IN"
                            " (SELECT id FROM runs WHERE org_id=?)",
                            (oid,),
                        ),
                        (
                            "DELETE FROM approvals WHERE run_id IN"
                            " (SELECT id FROM runs WHERE org_id=?)",
                            (oid,),
                        ),
                        (
                            "DELETE FROM run_comments WHERE run_id IN"
                            " (SELECT id FROM runs WHERE org_id=?)",
                            (oid,),
                        ),
                        (
                            "DELETE FROM task_runs WHERE run_id IN"
                            " (SELECT id FROM runs WHERE org_id=?)"
                            " OR task_id IN (SELECT id FROM tasks WHERE org_id=?)",
                            (oid, oid),
                        ),
                        (
                            "DELETE FROM routine_runs WHERE routine_id IN"
                            " (SELECT id FROM routines WHERE org_id=?)"
                            " OR task_id IN (SELECT id FROM tasks WHERE org_id=?)",
                            (oid, oid),
                        ),
                        (
                            "DELETE FROM trigger_fires WHERE trigger_id IN"
                            " (SELECT id FROM triggers WHERE org_id=?)",
                            (oid,),
                        ),
                        (
                            # run_id 직결분. deferred(run_id='')는 아래에서
                            # payload JSON을 파싱해 org_id를 판별한다 —
                            # LIKE 부분문자열 매칭은 입력 내 우연 일치로
                            # 타 조직 행을 지우거나 구형 행을 놓친다.
                            "DELETE FROM wakeup_requests WHERE run_id IN"
                            " (SELECT id FROM runs WHERE org_id=?)",
                            (oid,),
                        ),
                        (
                            # agent_session_events는 (tool,session_key)로만
                            # agent_sessions에 연결된다 — org_id는 세션 행 소유.
                            # NOT EXISTS 가드: 같은 키를 공유하는 타 조직
                            # 세션이 있으면 이벤트는 어느 조직 소유인지
                            # 구분할 수 없으므로 보존한다(과삭제 방지).
                            "DELETE FROM agent_session_events WHERE EXISTS ("
                            "SELECT 1 FROM agent_sessions s"
                            " WHERE s.tool=agent_session_events.tool"
                            " AND s.session_key=agent_session_events.session_key"
                            " AND s.org_id=?)"
                            " AND NOT EXISTS ("
                            "SELECT 1 FROM agent_sessions s2"
                            " WHERE s2.tool=agent_session_events.tool"
                            " AND s2.session_key=agent_session_events.session_key"
                            " AND s2.org_id<>?)",
                            (oid, oid),
                        ),
                    ):
                        cur = c.execute(sql, params)
                        total_rows += int(getattr(cur, "rowcount", 0) or 0)
                    # deferred wakeup(run_id='')의 payload org_id 파싱 판별.
                    wrows = c.execute(
                        "SELECT id, payload FROM wakeup_requests WHERE run_id=''"
                    ).fetchall()
                    for wr in wrows:
                        try:
                            wp = json.loads(str(wr["payload"] or "{}"))
                        except Exception:
                            logger.debug("org purge: unparseable wakeup payload id=%s", wr["id"])
                            continue
                        if not isinstance(wp, dict) or str(wp.get("org_id") or "") != oid:
                            continue
                        cur = c.execute("DELETE FROM wakeup_requests WHERE id=?", (str(wr["id"]),))
                        total_rows += int(getattr(cur, "rowcount", 0) or 0)
                    # 타 조직 agent가 이 조직 agent를 reports_to로 참조 중이면
                    # 끊는다 — agents.reports_to는 PG FK라 부모 삭제 전 NULL화 필요.
                    c.execute(
                        "UPDATE agents SET reports_to=NULL WHERE reports_to IS NOT NULL"
                        " AND reports_to IN (SELECT id FROM agents WHERE org_id=?)",
                        (oid,),
                    )
                    for t in tables:
                        cur = c.execute(
                            f"DELETE FROM {t} WHERE org_id=?",  # noqa: S608
                            (oid,),
                        )
                        total_rows += int(getattr(cur, "rowcount", 0) or 0)
                    cur = c.execute("DELETE FROM organizations WHERE id=?", (oid,))
                    total_rows += int(getattr(cur, "rowcount", 0) or 0)
                purged_orgs += 1
            except Exception as e:
                logger.warning("org purge failed id=%s: %s", oid, str(e)[:200])
        if purged_orgs:
            logger.info("purged %d deleted organizations (%d rows)", purged_orgs, total_rows)
        return {"purged_organizations": purged_orgs, "purged_org_rows": total_rows}

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
        """계정 삭제(개인정보보호법 대응) — spec 005 FR-014 처리 매트릭스.

        - hard delete: users, auth_sessions, email_verifications,
          password_reset_tokens, run_comments, mfa_*, api_keys,
          user_permissions, organization_members, org_invitations,
          consent_records, 본인 생성 share_links/dashboard_cards
        - anonymize: runs/agents/goals/evals/tasks/playbooks/
          workflow_revisions/projects/task_artifacts/routines/organizations/
          webhook_subscriptions.created_by, approvals.decided_by,
          approval_requests.requested_by·decided_by, agent_events.actor,
          task_comments.author_id → 'deleted_user' (업무 이력 행은 보존,
          행위자 식별자만 제거), audit_events.actor는 'deleted_user'로
          마스킹(법적 보존 — 기간은 retention 정책)
        - soft-delete: 본인 귀속 개인 워크스페이스(멤버 0명)는
          status='deleted' — 자식 행 정리는 purge_deleted_organizations가
          보존 기간 경과 후 수행
        - retain: audit_events(마스킹 후 보존), llm_usage(actor 컬럼 없음 —
          비용 집계만), notifications/inbox_items/message_extractions 등
          시스템·조직 소유 데이터
        - push_subscriptions/inbox_items는 소유자(user_id) 컬럼이 없어 본인
          귀속을 식별할 수 없다 — 스키마 보강 전까지 보존(알려진 한계).
        삭제 성공 시 같은 tx에 deletion_receipts 행을 기록한다(FR-015/016 —
        user_id 해시+유형별 건수만, PII 없음). 호출 측은 반환 counts와 별도로
        latest_deletion_receipt_for_hash(user_id_hash)로 영수증 id를 회수한다.
        타 사용자 데이터는 절대 건드리지 않는다.
        반환값: 테이블별 삭제/익명화 건수."""
        user = self.get_user_by_id(user_id)
        email = str((user or {}).get("email") or "").lower()
        counts: dict[str, int] = {}
        with self.tx() as c:
            cur = c.execute("DELETE FROM auth_sessions WHERE user_id=?", (user_id,))
            counts["auth_sessions"] = int(getattr(cur, "rowcount", 0) or 0)
            if email:
                cur = c.execute(
                    "DELETE FROM password_reset_tokens WHERE user_id=? OR lower(email)=?",
                    (user_id, email),
                )
            else:
                cur = c.execute("DELETE FROM password_reset_tokens WHERE user_id=?", (user_id,))
            counts["password_reset_tokens"] = int(getattr(cur, "rowcount", 0) or 0)
            for table in ("mfa_tickets", "mfa_backup_codes", "mfa_secrets"):
                cur = c.execute(f"DELETE FROM {table} WHERE user_id=?", (user_id,))  # noqa: S608
                counts[table] = int(getattr(cur, "rowcount", 0) or 0)
            cur = c.execute("DELETE FROM api_keys WHERE owner_user_id=?", (user_id,))
            counts["api_keys"] = int(getattr(cur, "rowcount", 0) or 0)
            # user_permissions — FR-032 계정 삭제 정리. user_id 키 행은
            # 전 조직에서 제거한다(계정이 사라지면 grant도 무의미).
            cur = c.execute("DELETE FROM user_permissions WHERE user_id=?", (user_id,))
            counts["user_permissions"] = int(getattr(cur, "rowcount", 0) or 0)
            # 조직 멤버십·초대 정리(spec 003) — 초대는 invited_by(user_id 또는
            # 이메일)와 수신 이메일 양쪽으로 매칭해 잔존 PII를 제거한다.
            # 개인 워크스페이스 후보는 멤버십 삭제 전에 수집한다 — 멤버 행이
            # 지워진 뒤에는 조직이 이 사용자에게 귀속됐는지 알 수 없다.
            # created_by(가입 시 uid 저장) 또는 멤버십 소속으로 판별한다.
            own_org_conds = ["o.created_by=?"]
            own_org_params: list[Any] = [user_id]
            if email:
                own_org_conds.append("o.created_by=?")
                own_org_params.append(email)
            personal_org_ids = [
                str(r["id"])
                for r in c.execute(
                    "SELECT o.id FROM organizations o"  # noqa: S608
                    " WHERE o.kind='personal' AND o.status<>'deleted' AND ("
                    + " OR ".join(own_org_conds)
                    + " OR o.id IN (SELECT org_id FROM organization_members"
                    " WHERE user_id=?))",
                    (*own_org_params, user_id),
                ).fetchall()
            ]
            cur = c.execute("DELETE FROM organization_members WHERE user_id=?", (user_id,))
            counts["organization_members"] = int(getattr(cur, "rowcount", 0) or 0)
            if email:
                cur = c.execute(
                    "DELETE FROM org_invitations WHERE invited_by=? OR invited_by=?"
                    " OR lower(email)=?",
                    (user_id, email, email),
                )
            else:
                cur = c.execute("DELETE FROM org_invitations WHERE invited_by=?", (user_id,))
            counts["org_invitations"] = int(getattr(cur, "rowcount", 0) or 0)
            # 본인 귀속 개인 워크스페이스 정리 — 멤버가 0명이 된 조직만
            # soft-delete(status='deleted')한다. 실제 자식 행 정리는
            # purge_deleted_organizations가 보존 기간 경과 후 수행하므로
            # org 스코프 데이터가 고아로 남지 않는다(FR-028). 무관한
            # memberless 개인 조직(타인 생성·이미 고아)은 건드리지 않는다.
            if personal_org_ids:
                placeholders = ",".join("?" for _ in personal_org_ids)
                cur = c.execute(
                    "UPDATE organizations SET status='deleted', updated_at=?"  # noqa: S608
                    " WHERE kind='personal' AND status<>'deleted'"
                    f" AND id IN ({placeholders})"
                    " AND id NOT IN (SELECT org_id FROM organization_members)",
                    (_iso(_now()), *personal_org_ids),
                )
                counts["organizations"] = int(getattr(cur, "rowcount", 0) or 0)
            else:
                counts["organizations"] = 0
            if email:
                cur = c.execute("DELETE FROM email_verifications WHERE email=?", (email,))
                counts["email_verifications"] = int(getattr(cur, "rowcount", 0) or 0)
            # audit_events.actor·run_comments는 user_id 매칭도 포함한다 —
            # email이 비어 있는 계정도 반드시 정리해야 하므로 if email:
            # 가드 밖에서 처리한다.
            if email:
                c.execute(
                    "UPDATE audit_events SET actor='deleted_user' WHERE actor=? OR lower(actor)=?",
                    (user_id, email),
                )
                cur = c.execute(
                    "DELETE FROM run_comments WHERE user_id=? OR lower(email)=?",
                    (user_id, email),
                )
            else:
                c.execute(
                    "UPDATE audit_events SET actor='deleted_user' WHERE actor=?",
                    (user_id,),
                )
                cur = c.execute("DELETE FROM run_comments WHERE user_id=?", (user_id,))
            counts["run_comments"] = int(getattr(cur, "rowcount", 0) or 0)
            # spec 005 FR-014 — hard delete: 본인 동의 이력(계정 파기 시 함께
            # 삭제 — spec Assumptions: 동의 이력도 파기 대상).
            cur = c.execute("DELETE FROM consent_records WHERE user_id=?", (user_id,))
            counts["consent_records"] = int(getattr(cur, "rowcount", 0) or 0)
            # hard delete: 본인 생성 share_links/dashboard_cards — created_by는
            # _actor_label 경유로 세션 사용자 email(또는 user_id)를 저장한다.
            for table in ("share_links", "dashboard_cards"):
                if email:
                    cur = c.execute(
                        f"DELETE FROM {table} WHERE created_by=? OR created_by=?",  # noqa: S608
                        (user_id, email),
                    )
                else:
                    cur = c.execute(
                        f"DELETE FROM {table} WHERE created_by=?",  # noqa: S608
                        (user_id,),
                    )
                counts[table] = int(getattr(cur, "rowcount", 0) or 0)
            # anonymize — FR-014 처리 매트릭스: 행위자 식별자 컬럼을
            # 'deleted_user'로 치환한다. 행 자체는 업무/관찰 이력으로 보존.
            # 컬럼 값은 user_id 또는 email 어느 쪽으로 기록됐든 매칭한다.
            for table, col in _ANONYMIZE_ACTOR_COLUMNS:
                if email:
                    cur = c.execute(
                        f"UPDATE {table} SET {col}='deleted_user'"  # noqa: S608
                        f" WHERE {col}=? OR {col}=?",
                        (user_id, email),
                    )
                else:
                    cur = c.execute(
                        f"UPDATE {table} SET {col}='deleted_user' WHERE {col}=?",  # noqa: S608
                        (user_id,),
                    )
                counts[f"{table}_{col}_anonymized"] = int(getattr(cur, "rowcount", 0) or 0)
            # author_id는 uuid(user_id)만 저장 — email 매칭 없이 user_id만.
            for table, col in _ANONYMIZE_USER_ID_ONLY_COLUMNS:
                cur = c.execute(
                    f"UPDATE {table} SET {col}='deleted_user' WHERE {col}=?",  # noqa: S608
                    (user_id,),
                )
                counts[f"{table}_{col}_anonymized"] = int(getattr(cur, "rowcount", 0) or 0)
            cur = c.execute("DELETE FROM users WHERE id=?", (user_id,))
            counts["users"] = int(getattr(cur, "rowcount", 0) or 0)
            # 삭제 영수증 — 사용자 행이 실제로 지워진 경우에만 같은 tx에 기록
            # (미존재 user_id로 빈 영수증이 남는 것을 막는다). PII 없이 해시+건수.
            if counts.get("users"):
                self._deletion_receipt_insert(
                    c, self.user_id_hash(user_id), counts, status="completed"
                )
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
            # email이 비어 있으면 '' 매칭으로 actor=''·email='' 행을
            # 끌어오지 않도록 user_id 매칭만 사용한다.
            actor_conds = ["actor=?"]
            actor_params: list[Any] = [user_id]
            if email:
                actor_conds.append("lower(actor)=?")
                actor_params.append(email)
            actor_where = " OR ".join(actor_conds)
            audit_rows = c.execute(
                "SELECT run_id,actor,action,object_ref,detail,created_at"  # noqa: S608
                f" FROM audit_events WHERE {actor_where}"
                " ORDER BY created_at DESC LIMIT 1000",
                tuple(actor_params),
            ).fetchall()
            comment_rows = c.execute(
                "SELECT run_id,body,created_at FROM run_comments"  # noqa: S608
                " WHERE user_id=?"
                + (" OR lower(email)=?" if email else "")
                + " ORDER BY created_at DESC LIMIT 1000",
                (user_id, *([email] if email else [])),
            ).fetchall()
            verify_rows = c.execute(
                "SELECT email,expires_at,used_at,created_at FROM email_verifications"
                " WHERE email=? ORDER BY created_at DESC",
                (email,),
            ).fetchall()
            reset_rows = c.execute(
                "SELECT email,expires_at,used_at,created_at FROM password_reset_tokens"
                " WHERE email=? ORDER BY created_at DESC",
                (email,),
            ).fetchall()
            key_rows = c.execute(
                "SELECT id,name,key_prefix,scopes_json,expires_at,revoked_at,"
                "last_used_at,created_at FROM api_keys WHERE owner_user_id=?"
                " ORDER BY created_at DESC",
                (user_id,),
            ).fetchall()
            member_rows = c.execute(
                "SELECT m.org_id,m.role,m.status,m.joined_at,o.name AS org_name,"
                "o.slug AS org_slug,o.kind AS org_kind"
                " FROM organization_members m JOIN organizations o ON o.id=m.org_id"
                " WHERE m.user_id=? ORDER BY m.joined_at",
                (user_id,),
            ).fetchall()
            invite_rows = c.execute(
                "SELECT org_id,email,role,status,expires_at,used_at,created_at"
                " FROM org_invitations WHERE invited_by=? OR invited_by=?"
                " OR lower(email)=? ORDER BY created_at DESC",
                (user_id, email, email),
            ).fetchall()
            # spec 005 FR-017 — 동의 이력(본인 열람권 대상)
            consent_rows = c.execute(
                "SELECT terms_version,privacy_version,agreed_at,ip,user_agent,created_at"
                " FROM consent_records WHERE user_id=? ORDER BY created_at DESC LIMIT 1000",
                (user_id,),
            ).fetchall()
            # 본인 created_by인 runs — id/워크플로/상태/시각만(input·metadata
            # 등 타인·시스템 데이터가 섞일 수 있는 본문은 제외). email이
            # 비어 있으면 '' 매칭으로 타인 행을 끌어오지 않도록 user_id만.
            run_cond = "created_by=?"
            run_params: list[Any] = [user_id]
            if email:
                run_cond += " OR created_by=?"
                run_params.append(email)
            run_rows = c.execute(
                "SELECT id,workflow_key,status,created_at,updated_at FROM runs"  # noqa: S608
                f" WHERE {run_cond} ORDER BY created_at DESC LIMIT 1000",
                tuple(run_params),
            ).fetchall()
            # 본인 생성 share_links 메타 — payload(공유 스냅샷 본문)는 제외.
            # token은 살아있는 bearer 자격증명이라 export에 포함하지 않는다.
            share_cond = "created_by=?"
            share_params: list[Any] = [user_id]
            if email:
                share_cond += " OR created_by=?"
                share_params.append(email)
            share_rows = c.execute(
                "SELECT kind,ref_id,created_at,expires_at FROM share_links"  # noqa: S608
                f" WHERE {share_cond} ORDER BY created_at DESC LIMIT 1000",
                tuple(share_params),
            ).fetchall()
            # push_subscriptions/inbox_items는 소유자 컬럼이 없어 본인 귀속을
            # 식별할 수 없다 — 스키마 보강 전까지 export에서도 제외(알려진 한계).
            # push endpoint 원문·keys는 어떤 경우에도 export하지 않는다.
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
            "password_reset_tokens": [dict(r) for r in reset_rows],
            # key_hash는 절대 포함하지 않는다 — 메타데이터만
            "api_keys": [dict(r) for r in key_rows],
            # token_hash는 절대 포함하지 않는다 — 초대 메타데이터만
            "org_memberships": [dict(r) for r in member_rows],
            "org_invitations": [dict(r) for r in invite_rows],
            # spec 005 FR-017 — 동의 이력·본인 생성 runs/공유 링크 메타
            "consent_records": [dict(r) for r in consent_rows],
            "runs": [dict(r) for r in run_rows],
            "share_links": [dict(r) for r in share_rows],
        }

    def list_audit(
        self, limit: int = 200, *, include_platform: bool = False
    ) -> list[dict[str, Any]]:
        """전체 audit_events 최신순 — 관리자 감사 타임라인용.
        사용자-facing 조회라 ctx 조직으로 스코프한다(시스템 경로는 무필터).
        include_platform=True면 조직 필터를 적용하지 않는다 — 플랫폼
        admin이 org_id='' 포함 전체 행을 조회할 때 사용한다."""
        frag, params = ("", []) if include_platform else _org_filter()
        rows = self._conn.execute(
            "SELECT id,run_id,actor,action,object_ref,detail,created_at"  # noqa: S608
            " FROM audit_events"
            + frag.replace(" AND ", " WHERE ", 1)
            + " ORDER BY created_at DESC, id DESC LIMIT ?",
            (*params, _clamp_limit(limit, 200, 1000)),
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

    # ---- audit keyset 페이지네이션/스트리밍 (spec 005 US3 / FR-019~020) ----
    def iter_audit(
        self,
        *,
        frm: str = "",
        to: str = "",
        actor: str = "",
        action: str = "",
        run_id: str = "",
        target: str = "",
        cursor: str = "",
        batch: int = 500,
        include_platform: bool = False,
    ) -> Iterator[dict[str, Any]]:
        """audit_events 필터 조회 제너레이터 — (created_at,id) DESC keyset.

        GET /api/v1/audit의 필터 조회와 /api/v1/audit/export의 스트리밍이
        공용으로 사용한다. cursor는 encode_audit_cursor가 만든 불투명
        문자열로, OFFSET 스캔 없이 대용량 테이블에서도 batch 단위로만
        fetch한다(상주 메모리 상한). 잘못된 cursor는 ValueError —
        호출자(라우터)가 422로 변환한다. 행 형태는 list_audit와 동일.
        include_platform=True면 조직 필터를 적용하지 않는다(플랫폼 admin)."""
        conds: list[str] = []
        params: list[Any] = []
        frag, fparams = ("", []) if include_platform else _org_filter()
        if frag:
            conds.append(frag[len(" AND ") :])
            params.extend(fparams)
        if frm:
            conds.append("created_at >= ?")
            params.append(frm)
        if to:
            conds.append("created_at <= ?")
            params.append(to)
        if actor:
            conds.append("actor = ?")
            params.append(actor)
        if action:
            # action은 prefix 매칭 허용(FR-019) — LIKE 와일드카드는 이스케이프.
            conds.append("action LIKE ? ESCAPE '\\'")
            params.append(_audit_like_escape(action) + "%")
        if run_id:
            conds.append("run_id = ?")
            params.append(run_id)
        if target:
            conds.append("object_ref = ?")
            params.append(target)
        last: tuple[str, str] | None = None
        if cursor:
            decoded = decode_audit_cursor(cursor)
            if decoded is None:
                raise ValueError("invalid audit cursor")
            last = decoded
        size = _clamp_limit(batch, 500, 2000)

        def _gen() -> Iterator[dict[str, Any]]:
            cur_last = last
            while True:
                cconds = list(conds)
                cparams = list(params)
                if cur_last is not None:
                    cconds.append("(created_at < ? OR (created_at = ? AND id < ?))")
                    cparams.extend([cur_last[0], cur_last[0], cur_last[1]])
                where = " AND ".join(cconds)
                rows = self._conn.execute(
                    "SELECT id,run_id,actor,action,object_ref,detail,created_at"  # noqa: S608
                    " FROM audit_events"
                    + (" WHERE " + where if where else "")
                    + " ORDER BY created_at DESC, id DESC LIMIT ?",
                    (*cparams, size),
                ).fetchall()
                if not rows:
                    return
                for r in rows:
                    yield {
                        "id": r["id"],
                        "run_id": r["run_id"],
                        "actor": r["actor"],
                        "action": r["action"],
                        "target": r["object_ref"],
                        "detail": _loads(r["detail"]),
                        "ts": r["created_at"],
                    }
                if len(rows) < size:
                    return
                cur_last = (rows[-1]["created_at"], rows[-1]["id"])

        return _gen()

    # ---- team 뷰 (다중 사용자 활동 집계) ----
    def team_activity(self) -> list[dict[str, Any]]:
        """users × runs.created_by × auth_sessions 집계.

        created_by는 세션 사용자 email(없으면 'api-key'/'local' 등 시스템 값) —
        email/id 어느 쪽으로 기록됐든 매칭한다."""
        users = self.list_users()
        frag, params = _org_filter()
        run_rows = self._conn.execute(
            "SELECT created_by, COUNT(*) AS n, MAX(created_at) AS last_at"  # noqa: S608
            " FROM runs WHERE created_by != ''" + frag + " GROUP BY created_by",  # noqa: S608
            params,
        ).fetchall()
        sess_rows = self._conn.execute(
            "SELECT user_id, COUNT(*) AS n, MAX(created_at) AS last_at"
            " FROM auth_sessions GROUP BY user_id"
        ).fetchall()
        # spec 003 US3 — ctx 조직이 있으면 멤버십 사용자만 노출한다.
        # 전역 사용자 디렉터리(email/세션 수)가 타 조직에 새는 것을 막는다.
        member_ids: set[str] | None = None
        oid = orgctx.current_org_id()
        if oid:
            member_ids = {
                str(r["user_id"])
                for r in self._conn.execute(
                    "SELECT user_id FROM organization_members WHERE org_id=?", (oid,)
                ).fetchall()
            }
        run_by_actor = {r["created_by"]: dict(r) for r in run_rows}
        sess_by_user = {r["user_id"]: dict(r) for r in sess_rows}
        items = []
        for u in users:
            uid, email = u["id"], u["email"]
            if member_ids is not None and uid not in member_ids:
                continue
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
        frag, params = _org_filter()
        rows = self._conn.execute(
            "SELECT id,workflow_key,workflow_version,status,trace_id,error,"  # noqa: S608
            "created_by,agent_id,goal_id,created_at,updated_at FROM runs"
            " WHERE created_at >= ?" + frag + " ORDER BY created_at",  # noqa: S608
            (since_iso, *params),
        ).fetchall()
        return [dict(r) for r in rows]

    def export_sessions(self, since_iso: str) -> list[dict[str, Any]]:
        frag, params = _org_filter()
        rows = self._conn.execute(
            "SELECT tool,session_key,project,title,status,machine,"  # noqa: S608
            "tokens_in,tokens_out,last_event_at,created_at,updated_at"
            " FROM agent_sessions WHERE updated_at >= ?" + frag + " ORDER BY updated_at",  # noqa: S608
            (since_iso, *params),
        ).fetchall()
        return [dict(r) for r in rows]

    def export_reports(self, since_iso: str) -> list[dict[str, Any]]:
        frag, params = _org_filter()
        rows = self._conn.execute(
            "SELECT id,kind,content,created_at FROM reports WHERE created_at >= ?"  # noqa: S608
            + frag
            + " ORDER BY created_at",
            (since_iso, *params),
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
        frag, params = _org_filter()
        rows = self._conn.execute(
            "SELECT sr.run_id, sr.stage_key, sr.attempt, sr.status, sr.model,"  # noqa: S608
            " sr.tokens_in, sr.tokens_out, sr.started_at, sr.finished_at, sr.error,"
            " r.workflow_key, r.project, r.status AS run_status,"
            " r.created_at AS run_created_at, r.updated_at AS run_updated_at"
            " FROM stage_runs sr JOIN runs r ON r.id = sr.run_id"
            " WHERE r.created_at >= ?"  # noqa: S608
            + frag.replace("org_id=?", "r.org_id=?")
            + " ORDER BY sr.started_at",
            (since_iso, *params),
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
        frag, params = _org_filter()
        rows = self._conn.execute(
            "SELECT id, workflow_key, status, project, created_at, updated_at"  # noqa: S608
            " FROM runs WHERE updated_at >= ?" + frag + " ORDER BY updated_at, id LIMIT ?",  # noqa: S608
            (since_iso, *params, limit),
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
            "SELECT sr.id, sr.run_id, sr.stage_key, sr.attempt, sr.status,"  # noqa: S608
            " sr.started_at, sr.finished_at"
            " FROM stage_runs sr JOIN runs r ON r.id = sr.run_id"
            " WHERE COALESCE(sr.finished_at, sr.started_at) >= ?"  # noqa: S608
            + frag.replace("org_id=?", "r.org_id=?")
            + " ORDER BY COALESCE(sr.finished_at, sr.started_at), sr.id LIMIT ?",
            (since_iso, *params, limit),
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
        # agent_session_events는 org_id가 없어 부모 agent_sessions로 조인해
        # 조직을 판별한다 — ''(플랫폼/env 키 푸시) 세션은 공유, 고아 이벤트는
        # org 특정 불가라 ctx 조직 요청에서는 제외한다.
        _ev_oid = orgctx.current_org_id()
        sfrag = " AND (s.org_id=? OR s.org_id='')" if _ev_oid else ""
        sparams: tuple[Any, ...] = (since_iso, _ev_oid, limit) if _ev_oid else (since_iso, limit)
        rows = self._conn.execute(
            "SELECT e.id, e.tool, e.session_key, e.status, e.project, e.doing,"  # noqa: S608
            " e.ask, e.created_at"
            " FROM agent_session_events e"
            " LEFT JOIN agent_sessions s"
            " ON s.tool = e.tool AND s.session_key = e.session_key"
            " WHERE e.created_at >= ?" + sfrag + " ORDER BY e.created_at, e.id LIMIT ?",  # noqa: S608
            sparams,
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
