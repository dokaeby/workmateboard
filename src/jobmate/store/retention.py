# mypy: disable-error-code="attr-defined"
"""Store mixin — 데이터 보존 정책 엔진 + purge 실행 이력 (spec 005 US1).

- 유형별 TTL/삭제 방식의 기본값은 `_DEFAULT_POLICIES`(코드/env 기본값 역할)에
  있고, `retention_policies` 테이블의 행이 TTL·mode를 override한다(FR-002).
- 법정 보관 하한은 `LEGAL_MIN_TTL_DAYS` — 미만 값은 API 계층에서 422로
  거부되고, DB 직접 수정 우회는 문서화된 운영 책임으로 남긴다(FR-003).
- purge는 유형별 500행 배치 tx로 실행해 멱등·재개 가능하다(FR-004).
  실행 메타(건수만, 내용물 금지)는 `purge_runs`에, 요약은
  `retention_purge` audit 이벤트로 남는다(FR-005/006).
- `message 본문 원문`(FR-001)은 별도 messages 테이블이 없으므로
  `message_extractions.source_text`의 clear_field 정책이 담당한다.
- `inbox_items`/`notifications`/`email_summaries`는 FR-001 최소 집합에
  포함되지만 기존 sweep에 없던 유형 — 보수적 기본값 90일을 둔다.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

from .admin import _TERMINAL_RUN_STATUSES_SQL
from .util import _iso, _now

logger = logging.getLogger(__name__)

# FR-003 — 법정 보관 하한(일). 접근·인증 로그 유형의 TTL은 이 값 미만 불가.
# 목록 변경 자체도 audit에 남겨야 한다(현재는 코드 상수 — 변경 시 커밋이 이력).
LEGAL_MIN_TTL_DAYS: dict[str, int] = {"audit_events": 365}

RETENTION_MODES = ("hard_delete", "clear_field", "anonymize")

PURGE_BATCH_SIZE = 500
# 단일 유형/실행의 배치 상한 — 초과분은 다음 sweep/API 실행이 이어서 처리한다
# (멱등이라 중복 삭제·중복 집계 없음).
_MAX_BATCHES_PER_UNIT = 2000


class RetentionPolicyError(Exception):
    """정책 검증 실패 — code는 API 에러 코드로 그대로 매핑된다."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class _Step:
    """purge 단위 — 보통 테이블 1개. run_scoped=True면 부모 run이
    종결+TTL 경과일 때만 자식 행을 지운다(runs 트리용)."""

    table: str
    pk: tuple[str, ...] = ("id",)
    run_scoped: bool = False
    extra_where: str = ""


@dataclass(frozen=True)
class _Policy:
    key: str
    ttl_days: int  # 0 = cutoff가 now(만료 기준 컬럼용)
    mode: str = "hard_delete"
    table: str = ""
    pk: tuple[str, ...] = ("id",)
    cutoff_col: str = "created_at"
    clear_field: str = ""
    redact_value: str = "[anonymized]"
    # clear_field/anonymize 시 `field <> ''` 가드 — 이미 비워진 행을
    # 매 배치 다시 세지 않게 해 dry-run/재실행 건수가 정확하다.
    nonempty_field: str = ""
    extra_where: str = ""
    run_scoped: bool = False
    # 복합 정책(runs+자식) — 설정 시 table/cond 필드 대신 steps를 순회한다.
    steps: tuple[_Step, ...] = ()


# runs 트리 — 자식(stage_runs/approvals/run_comments)을 먼저 지우고 runs를
# 지운다(PG FK 순서). 비종결 상태는 _TERMINAL_RUN_STATUSES_SQL로 보호(FR-009).
_RUN_TREE_STEPS = (
    _Step("stage_runs", run_scoped=True),
    _Step("approvals", run_scoped=True),
    _Step("run_comments", run_scoped=True),
    _Step(
        "runs",
        extra_where=f"status IN {_TERMINAL_RUN_STATUSES_SQL}",
    ),
)

_DEFAULT_POLICIES: tuple[_Policy, ...] = (
    # audit_events — FR-003 법정 하한 365일. 기존 90일 기본값은 하한 위반이라
    # 365로 상향했다(스펙 명시).
    _Policy("audit_events", ttl_days=365),
    _Policy("llm_usage", ttl_days=180),
    # auth_sessions — TTL이 아니라 expires_at 기준 만료 삭제. ttl_days=0이면
    # cutoff=now → `expires_at < now`(기존 동작 동일). override N일은
    # `expires_at < now-N`으로 해석된다.
    _Policy("auth_sessions", ttl_days=0, cutoff_col="expires_at"),
    _Policy("runs", ttl_days=180, steps=_RUN_TREE_STEPS),
    # message_extractions — 행은 유지하고 source_text만 비움(FR-010).
    # "message 본문 원문" 보존 유형에 해당.
    _Policy(
        "message_extractions",
        ttl_days=30,
        mode="clear_field",
        table="message_extractions",
        pk=("source_provider", "source_id", "org_id"),
        clear_field="source_text",
        nonempty_field="source_text",
        extra_where="processing_status='processed'",
    ),
    _Policy("agent_session_events", ttl_days=30),
    # finished_at NULL(미완료 queued/deferred)은 IS NOT NULL 가드로 보존(FR-009)
    _Policy(
        "wakeup_requests",
        ttl_days=90,
        table="wakeup_requests",
        cutoff_col="finished_at",
        extra_where="finished_at IS NOT NULL",
    ),
    # open 인시던트는 정책 감시 중 — 보존(FR-009)
    _Policy(
        "budget_incidents",
        ttl_days=180,
        table="budget_incidents",
        extra_where="status <> 'open'",
    ),
    # pending(lazy 채점 대기)은 보존(FR-009)
    _Policy(
        "eval_runs",
        ttl_days=365,
        table="eval_runs",
        extra_where="status <> 'pending'",
    ),
    _Policy("agent_events", ttl_days=90),
    # 종결 delivery만 — pending/failed/delivering은 재시도 대상이라 보존
    _Policy(
        "webhook_deliveries",
        ttl_days=90,
        table="webhook_deliveries",
        extra_where="status IN ('success','dead','cancelled')",
    ),
    # 만료 후 30일 경과분 — 상태 이력은 audit_events가 보존
    _Policy("org_invitations", ttl_days=30, cutoff_col="expires_at"),
    # FR-001 최소 집합 중 기존 sweep에 없던 유형 — 보수적 기본값 90일.
    _Policy("inbox_items", ttl_days=90),
    _Policy("notifications", ttl_days=90),
    # created_at이 없고 checked_at이 유일한 시각 컬럼 — 복합 PK 사용.
    _Policy(
        "email_summaries",
        ttl_days=90,
        table="email_summaries",
        pk=("source_provider", "source_id", "org_id"),
        cutoff_col="checked_at",
    ),
)

# API override 대상이 아닌 고정 정리 단계 — (table, pk, cond).
# cond의 ?는 now_iso 하나에 바인딩된다. password_reset_tokens /
# email_verifications / mfa_tickets의 "만료 OR 사용완료" 규칙은 TTL 개념이
# 아니라 인증 토큰 수명주기라 정책 테이블에서 제외한다(기존 동작 유지).
_FIXED_STEPS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("password_reset_tokens", ("id",), "expires_at < ? OR used_at IS NOT NULL"),
    ("email_verifications", ("id",), "expires_at < ? OR used_at IS NOT NULL"),
    ("mfa_tickets", ("id",), "expires_at < ? OR used_at IS NOT NULL"),
)


def _policy_map() -> dict[str, _Policy]:
    return {p.key: p for p in _DEFAULT_POLICIES}


def _supported_modes(pol: _Policy) -> tuple[str, ...]:
    """정책이 지원하는 삭제 방식 — hard_delete는 항상 가능하고,
    clear_field/anonymize는 비우기/치환 대상 컬럼(clear_field)이 선언된
    단일 테이블 정책만 지원한다. 복합 runs 정책(steps)이나 대상 컬럼이
    없는 정책에 clear/anonymize를 허용하면 UPDATE 대상이 없어 매 sweep마다
    실패하거나 무한 재집계된다."""
    if pol.clear_field:
        return RETENTION_MODES
    return ("hard_delete",)


class RetentionStoreMixin:
    # ---- retention_policies override 조회/변경 ----

    def _retention_policy_overrides(self) -> dict[str, dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT data_type, ttl_days, mode, updated_by, updated_at FROM retention_policies"
        ).fetchall()
        return {str(r["data_type"]): dict(r) for r in rows}

    def _resolved_policies(self) -> list[_Policy]:
        """기본값 + DB override 병합 — ttl_days/mode만 override 가능."""
        overrides = self._retention_policy_overrides()
        out: list[_Policy] = []
        for p in _DEFAULT_POLICIES:
            ov = overrides.get(p.key)
            if not ov:
                out.append(p)
                continue
            mode = str(ov.get("mode") or p.mode)
            if mode not in RETENTION_MODES:
                mode = p.mode
            try:
                ttl = int(ov["ttl_days"])
            except (TypeError, ValueError, KeyError):
                # 깨진 override 행 하나가 sweep 전체를 죽이지 않게 기본값으로
                # 폴백하고 계속 진행한다(다음 행도 처리).
                logger.warning(
                    "retention_policies corrupt ttl_days for %s — using default",
                    p.key,
                )
                out.append(p)
                continue
            out.append(replace(p, ttl_days=ttl, mode=mode))
        return out

    def list_retention_policies(self) -> list[dict[str, Any]]:
        """GET /api/v1/admin/retention 응답 — 유효값 + 출처 + 법정 하한."""
        overrides = self._retention_policy_overrides()
        out: list[dict[str, Any]] = []
        for p in _DEFAULT_POLICIES:
            ov = overrides.get(p.key)
            mode = str((ov or {}).get("mode") or p.mode)
            if mode not in RETENTION_MODES:
                mode = p.mode
            try:
                ttl = int(ov["ttl_days"]) if ov else p.ttl_days
            except (TypeError, ValueError, KeyError):
                ttl = p.ttl_days
            out.append(
                {
                    "data_type": p.key,
                    "ttl_days": ttl,
                    "mode": mode,
                    "supported_modes": list(_supported_modes(p)),
                    "default_ttl_days": p.ttl_days,
                    "default_mode": p.mode,
                    "source": "override" if ov else "default",
                    "legal_min_days": LEGAL_MIN_TTL_DAYS.get(p.key),
                    "updated_by": str((ov or {}).get("updated_by") or ""),
                    "updated_at": (ov or {}).get("updated_at"),
                }
            )
        return out

    _UNSET: Any = object()

    def set_retention_policy(
        self,
        data_type: str,
        *,
        ttl_days: Any = _UNSET,
        mode: str | None = None,
        actor: str = "",
    ) -> dict[str, Any]:
        """DB override upsert/delete + `retention_policy_update` 감사.

        - ttl_days 생략 + mode 생략 → RetentionPolicyError("empty_update")
        - ttl_days=None(명시) → override 삭제, 기본값 복귀
        - ttl_days 정수 → upsert. mode=None이면 기존 override mode
          (없으면 기본 mode) 유지. mode만 변경도 가능(유효 ttl 유지)."""
        base = _policy_map().get(data_type)
        if base is None:
            raise RetentionPolicyError(
                "unknown_data_type", f"unknown retention data_type: {data_type}"
            )
        if ttl_days is self._UNSET and mode is None:
            raise RetentionPolicyError("empty_update", "ttl_days or mode is required")
        if mode is not None and mode not in RETENTION_MODES:
            raise RetentionPolicyError("invalid_mode", f"mode must be one of {RETENTION_MODES}")
        overrides = self._retention_policy_overrides()
        old_ov = overrides.get(data_type)
        old_eff: dict[str, Any] = {
            "ttl_days": int(old_ov["ttl_days"]) if old_ov else base.ttl_days,
            "mode": (
                str(old_ov["mode"])
                if old_ov and str(old_ov["mode"]) in RETENTION_MODES
                else base.mode
            ),
            "source": "override" if old_ov else "default",
        }
        if ttl_days is None:
            # 명시적 null → override 삭제(기본값 복귀). mode 동반은 무의미해 거부.
            if mode is not None:
                raise RetentionPolicyError(
                    "invalid_mode", "mode cannot be combined with ttl_days=null"
                )
            with self.tx() as c:
                c.execute("DELETE FROM retention_policies WHERE data_type=?", (data_type,))
            new_eff = {"ttl_days": base.ttl_days, "mode": base.mode, "source": "default"}
        else:
            if ttl_days is self._UNSET:
                # mode만 변경 — 현재 유효 ttl 유지
                ttl = int(old_eff["ttl_days"])
            else:
                try:
                    ttl = int(ttl_days)
                except (TypeError, ValueError):
                    raise RetentionPolicyError(
                        "invalid_ttl", "ttl_days must be an integer"
                    ) from None
                if ttl < 1:
                    raise RetentionPolicyError("invalid_ttl", "ttl_days must be >= 1")
            min_days = LEGAL_MIN_TTL_DAYS.get(data_type)
            if min_days is not None and ttl < min_days:
                raise RetentionPolicyError(
                    "retention_below_legal_minimum",
                    f"{data_type} ttl_days must be >= {min_days} (legal minimum)",
                )
            new_mode = mode if mode is not None else old_eff["mode"]
            supported = _supported_modes(base)
            if new_mode not in supported:
                raise RetentionPolicyError(
                    "invalid_mode",
                    f"mode '{new_mode}' is not supported for {data_type}"
                    f" (supported: {', '.join(supported)})",
                )
            new_eff = {"ttl_days": ttl, "mode": new_mode, "source": "override"}
            with self.tx() as c:
                c.execute(
                    "INSERT INTO retention_policies"
                    "(data_type,ttl_days,mode,updated_by,updated_at)"
                    " VALUES(?,?,?,?,?)"
                    " ON CONFLICT(data_type) DO UPDATE SET"
                    " ttl_days=excluded.ttl_days, mode=excluded.mode,"
                    " updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                    (data_type, ttl, new_mode, actor, _iso(_now())),
                )
        try:
            self.audit(
                "",
                actor or "admin",
                "retention_policy_update",
                data_type,
                {"old": old_eff, "new": new_eff},
            )
        except Exception:
            logger.debug("retention_policy_update audit failed", exc_info=True)
        return {"data_type": data_type, "policy": new_eff}

    # ---- purge 실행 ----

    def run_purge(
        self,
        *,
        dry_run: bool = False,
        actor: str = "retention_sweep",
        now: datetime | None = None,
        only: set[str] | None = None,
    ) -> dict[str, Any]:
        """보존 정책 purge 1회 실행 — purge_runs + retention_purge 감사 기록.

        dry_run=True면 어떤 행도 삭제/갱신하지 않고 예상 건수만 반환하며
        purge_runs에는 dry_run=1로 기록된다(FR-007)."""
        now = now or _now()
        started = _now()
        purge_id = uuid.uuid4().hex[:12]
        # 실행 '시도'를 먼저 기록 — purge 도중 크래시해도 running 행이 남는다
        # (spec 구현 힌트: sweep 트랜잭션 밖 별도 tx).
        with self.tx() as c:
            c.execute(
                "INSERT INTO purge_runs(id,dry_run,status,counts_json,started_at)"
                " VALUES(?,?,'running','{}',?)",
                (purge_id, 1 if dry_run else 0, _iso(started)),
            )
        counts: dict[str, int] = {}
        errors: list[str] = []
        policies = [p for p in self._resolved_policies() if only is None or p.key in only]
        for pol in policies:
            try:
                counts.update(self._purge_policy(pol, now=now, dry_run=dry_run))
            except Exception as e:
                logger.warning("retention purge type=%s failed: %s", pol.key, str(e)[:300])
                errors.append(pol.key)
        try:
            counts.update(self._purge_fixed_steps(now=now, dry_run=dry_run))
        except Exception as e:
            logger.warning("retention fixed steps failed: %s", str(e)[:300])
            errors.append("fixed_steps")
        if not errors:
            status = "completed"
        elif len(errors) >= len(policies) + 1:
            status = "failed"
        else:
            status = "partial"
        finished = _now()
        with self.tx() as c:
            c.execute(
                "UPDATE purge_runs SET status=?, counts_json=?, finished_at=? WHERE id=?",
                (status, json.dumps(counts), _iso(finished), purge_id),
            )
        try:
            # FR-006 — 건수와 실행 ID만. 삭제된 행 내용은 절대 기록하지 않는다.
            self.audit(
                "",
                actor or "retention_sweep",
                "retention_purge",
                purge_id,
                {
                    "purge_run_id": purge_id,
                    "status": status,
                    "dry_run": bool(dry_run),
                    "total": sum(counts.values()),
                    "counts": counts,
                },
            )
        except Exception:
            logger.debug("retention_purge audit failed", exc_info=True)
        return {
            "purge_run_id": purge_id,
            "status": status,
            "dry_run": bool(dry_run),
            "counts": counts,
        }

    # ---- 내부: 유형별/고정 단계 purge ----

    def _purge_policy(self, pol: _Policy, *, now: datetime, dry_run: bool) -> dict[str, int]:
        cutoff = _iso(now - timedelta(days=pol.ttl_days))
        counts: dict[str, int] = {}
        if pol.steps:
            for st in pol.steps:
                cond = self._unit_cond(
                    extra_where=st.extra_where,
                    run_scoped=st.run_scoped,
                    mode="hard_delete",
                    nonempty_field="",
                )
                n = self._purge_unit(
                    table=st.table,
                    pk=st.pk,
                    cond=cond,
                    params=(cutoff,),
                    mode="hard_delete",
                    clear_field="",
                    value="",
                    dry_run=dry_run,
                )
                counts[st.table] = n
            return counts
        # anonymize는 이미 redact_value로 치환된 행도 `field <> ''`를
        # 통과하므로 매 배치 재집계된다 — redact 값과 다른 행만 대상.
        guard_field = pol.nonempty_field
        if pol.mode == "anonymize":
            # 쓰기 대상 컬럼 기준으로 가드 — nonempty_field와 달라도
            # 이미 마스킹된 행은 제외된다.
            guard_field = pol.clear_field or pol.nonempty_field
        elif pol.mode == "clear_field" and not guard_field:
            guard_field = pol.clear_field
        cond = self._unit_cond(
            cutoff_col=pol.cutoff_col,
            extra_where=pol.extra_where,
            run_scoped=pol.run_scoped,
            mode=pol.mode,
            nonempty_field=guard_field,
            redact_value=pol.redact_value,
        )
        value = "" if pol.mode == "clear_field" else pol.redact_value
        n = self._purge_unit(
            # 단일 테이블 정책은 table 미지정 시 key가 테이블명과 동일
            table=pol.table or pol.key,
            pk=pol.pk,
            cond=cond,
            params=(cutoff,),
            mode=pol.mode,
            clear_field=pol.clear_field,
            value=value,
            dry_run=dry_run,
        )
        counts[self._count_key(pol.key, pol.mode)] = n
        return counts

    @staticmethod
    def _count_key(key: str, mode: str) -> str:
        """건수 dict 키 — clear/anonymize는 행이 남으므로 접미사를 붙여
        hard delete 건수와 구분한다(기존 `message_extractions_cleared` 키 유지)."""
        if mode == "clear_field":
            return f"{key}_cleared"
        if mode == "anonymize":
            return f"{key}_anonymized"
        return key

    @staticmethod
    def _unit_cond(
        *,
        cutoff_col: str = "created_at",
        extra_where: str,
        run_scoped: bool,
        mode: str,
        nonempty_field: str,
        redact_value: str = "",
    ) -> str:
        """삭제/집계 공통 WHERE 절 — 모든 ?는 cutoff_iso 1개에 바인딩된다
        (extra_where/nonempty_field는 파라미터 없는 리터럴 조각만 허용).
        redact_value는 코드 상수 정책 정의값이라 리터럴 삽입이 안전하다."""
        if run_scoped:
            # _TERMINAL_RUN_STATUSES_SQL은 고정 리터럴 조각 — 인젝션 위험 없음
            cond = (
                "run_id IN (SELECT id FROM runs WHERE status IN "  # noqa: S608
                f"{_TERMINAL_RUN_STATUSES_SQL} AND created_at < ?)"
            )
        else:
            cond = f"{cutoff_col} < ?"
        if extra_where:
            cond = f"{cond} AND {extra_where}"
        if mode != "hard_delete" and nonempty_field:
            cond = f"{cond} AND {nonempty_field} <> ''"
            if mode == "anonymize" and redact_value:
                # 이미 마스킹된 행을 다시 세지 않게 redact 값도 제외 —
                # 없으면 _MAX_BATCHES까지 무한 재집계된다.
                esc = redact_value.replace("'", "''")
                cond = f"{cond} AND {nonempty_field} <> '{esc}'"
        return cond

    def _purge_unit(
        self,
        *,
        table: str,
        pk: tuple[str, ...],
        cond: str,
        params: tuple[Any, ...],
        mode: str,
        clear_field: str,
        value: str,
        dry_run: bool,
    ) -> int:
        """단일 테이블 purge — pk IN (SELECT ... LIMIT 500) 배치.

        매 배치 별도 tx로 커밋돼 크래시 후 재실행 시 이미 지워진 행은
        다시 세지 않는다(멱등). 복합 PK는 row-value IN을 사용한다
        (SQLite 3.15+/PG 공통 지원)."""
        # table/pk/cond/clear_field는 코드 상수 정책 정의에서만 온다 —
        # 사용자 입력이 섞이지 않아 S608은 의도된 동적 SQL이다.
        lhs = pk[0] if len(pk) == 1 else "(" + ", ".join(pk) + ")"
        sel = ", ".join(pk)
        sub = f"SELECT {sel} FROM {table} WHERE {cond} LIMIT ?"  # noqa: S608
        if dry_run:
            with self.tx() as c:
                row = c.execute(
                    f"SELECT COUNT(*) AS n FROM {table} WHERE {cond}",  # noqa: S608
                    params,
                ).fetchone()
            return int(row["n"])
        total = 0
        for _ in range(_MAX_BATCHES_PER_UNIT):
            with self.tx() as c:
                if mode == "hard_delete":
                    cur = c.execute(
                        f"DELETE FROM {table} WHERE {lhs} IN ({sub})",  # noqa: S608
                        (*params, PURGE_BATCH_SIZE),
                    )
                else:
                    # clear_field/anonymize — 행은 유지하고 지정 필드만 비움/치환
                    cur = c.execute(
                        f"UPDATE {table} SET {clear_field}=?"  # noqa: S608
                        f" WHERE {lhs} IN ({sub})",
                        (value, *params, PURGE_BATCH_SIZE),
                    )
                n = int(getattr(cur, "rowcount", 0) or 0)
            total += n
            if n < PURGE_BATCH_SIZE:
                break
        else:
            logger.warning(
                "retention purge table=%s hit batch cap %d — remainder next run",
                table,
                _MAX_BATCHES_PER_UNIT,
            )
        return total

    def _purge_fixed_steps(self, *, now: datetime, dry_run: bool) -> dict[str, int]:
        """만료 토큰/티켓 등 정책 테이블 대상이 아닌 고정 정리 — 기존
        retention_sweep 동작을 그대로 유지한다(override 불가)."""
        now_iso = _iso(now) or ""
        counts: dict[str, int] = {}
        for table, pk, cond in _FIXED_STEPS:
            counts[table] = self._purge_unit(
                table=table,
                pk=pk,
                cond=cond,
                params=(now_iso,),
                mode="hard_delete",
                clear_field="",
                value="",
                dry_run=dry_run,
            )
        return counts
