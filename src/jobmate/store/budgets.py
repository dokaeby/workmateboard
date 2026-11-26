# mypy: disable-error-code="attr-defined"
"""Budget policy/incident 스토어 메서드 믹스인 (db.py Store에서 추출)."""

from __future__ import annotations

import uuid
from typing import Any

from .util import _iso, _now


class BudgetStoreMixin:
    # ---- budget_policies (스코프별 월간 예산 — microusd 권위, *_usd 호환) ----
    _UNPRICED_POLICIES = ("block", "allow")
    _WINDOW_KINDS = ("calendar_month_utc", "lifetime")

    @staticmethod
    def _policy_json(r: Any) -> dict[str, Any]:
        d = dict(r)
        for k in ("amount_usd",):
            try:
                d[k] = float(d.get(k) or 0)
            except (TypeError, ValueError):
                d[k] = 0.0
        for k in ("warn_percent",):
            try:
                d[k] = int(d.get(k) or 0)
            except (TypeError, ValueError):
                d[k] = 0
        # microusd 컬럼 — 마이그레이션 전 레거시 행은 NULL/0일 수 있어
        # amount_usd에서 파생해 채운다(표시용; 권위 판정은 서비스 계층).
        try:
            d["amount_microusd"] = int(d.get("amount_microusd") or 0)
        except (TypeError, ValueError):
            d["amount_microusd"] = 0
        if not d["amount_microusd"] and d["amount_usd"] > 0:
            d["amount_microusd"] = int(round(d["amount_usd"] * 1_000_000))
        try:
            d["reservation_microusd"] = max(0, int(d.get("reservation_microusd") or 0))
        except (TypeError, ValueError):
            d["reservation_microusd"] = 0
        up = str(d.get("unpriced_usage_policy") or "allow").strip().lower()
        d["unpriced_usage_policy"] = up if up in BudgetStoreMixin._UNPRICED_POLICIES else "allow"
        wk = str(d.get("window_kind") or "calendar_month_utc").strip().lower()
        d["window_kind"] = wk if wk in BudgetStoreMixin._WINDOW_KINDS else "calendar_month_utc"
        for k in ("hard_stop_enabled", "is_active"):
            d[k] = bool(d.get(k))
        return d

    def upsert_budget_policy(
        self,
        scope_type: str,
        scope_id: str = "",
        *,
        amount_usd: float,
        warn_percent: int = 80,
        hard_stop_enabled: bool = True,
        is_active: bool = True,
        amount_microusd: int | None = None,
        reservation_microusd: int = 0,
        unpriced_usage_policy: str = "allow",
        window_kind: str = "calendar_month_utc",
    ) -> dict[str, Any] | None:
        """(scope_type, scope_id) UNIQUE 기준 upsert. scope_id ''는 global.

        amount_microusd=None이면 amount_usd에서 파생(레거시 호출자 호환) —
        amount_usd 열도 함께 기록해 기존 읽기 경로를 유지한다(dual-field).
        get→insert 사이 동시 upsert 레이스는 UNIQUE 위반으로 실패할 수 있어
        IntegrityError 시 1회 재조회 후 재시도한다."""
        scope_type = scope_type.strip().lower()
        scope_id = (scope_id or "").strip()[:200]
        if scope_type == "global":
            scope_id = ""
        if amount_microusd is None:
            try:
                amount_microusd = int(round(float(amount_usd) * 1_000_000))
            except (TypeError, ValueError):
                amount_microusd = 0
        amount_microusd = max(0, int(amount_microusd))
        try:
            reservation_microusd = max(0, int(reservation_microusd))
        except (TypeError, ValueError):
            reservation_microusd = 0
        upp = (unpriced_usage_policy or "allow").strip().lower()
        if upp not in self._UNPRICED_POLICIES:
            upp = "allow"
        wk = (window_kind or "calendar_month_utc").strip().lower()
        if wk not in self._WINDOW_KINDS:
            wk = "calendar_month_utc"
        for _attempt in range(2):
            existing = self.get_budget_policy(scope_type, scope_id)
            now = _iso(_now())
            try:
                with self.tx() as c:
                    if existing is None:
                        c.execute(
                            "INSERT INTO budget_policies(id,scope_type,scope_id,window_kind,"
                            "amount_usd,amount_microusd,reservation_microusd,"
                            "unpriced_usage_policy,warn_percent,hard_stop_enabled,"
                            "is_active,created_at,updated_at)"
                            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (
                                uuid.uuid4().hex[:12],
                                scope_type,
                                scope_id,
                                wk,
                                float(amount_usd),
                                amount_microusd,
                                reservation_microusd,
                                upp,
                                int(warn_percent),
                                1 if hard_stop_enabled else 0,
                                1 if is_active else 0,
                                now,
                                now,
                            ),
                        )
                    else:
                        c.execute(
                            "UPDATE budget_policies SET amount_usd=?, amount_microusd=?,"
                            " reservation_microusd=?, unpriced_usage_policy=?,"
                            " window_kind=?, warn_percent=?, hard_stop_enabled=?,"
                            " is_active=?, updated_at=? WHERE id=?",
                            (
                                float(amount_usd),
                                amount_microusd,
                                reservation_microusd,
                                upp,
                                wk,
                                int(warn_percent),
                                1 if hard_stop_enabled else 0,
                                1 if is_active else 0,
                                now,
                                existing["id"],
                            ),
                        )
                return self.get_budget_policy(scope_type, scope_id)
            except self._integrity_errors:
                continue  # 동시 INSERT 레이스 — 재조회 후 UPDATE 경로로 재시도
        return self.get_budget_policy(scope_type, scope_id)

    def get_budget_policy(self, scope_type: str, scope_id: str = "") -> dict[str, Any] | None:
        scope_id = "" if scope_type == "global" else (scope_id or "").strip()[:200]
        row = self._conn.execute(
            "SELECT * FROM budget_policies WHERE scope_type=? AND scope_id=?",
            (scope_type, scope_id),
        ).fetchone()
        return self._policy_json(row) if row else None

    def list_budget_policies(self, active_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM budget_policies"
        if active_only:
            sql += " WHERE is_active=1"
        sql += " ORDER BY scope_type, scope_id"
        return [self._policy_json(r) for r in self._conn.execute(sql).fetchall()]

    def deactivate_budget_policy(self, scope_type: str, scope_id: str = "") -> bool:
        """DELETE 의미 — 행 삭제 대신 is_active=0 (인시던트/이력 보존)."""
        scope_id = "" if scope_type == "global" else (scope_id or "").strip()[:200]
        with self.tx() as c:
            cur = c.execute(
                "UPDATE budget_policies SET is_active=0, updated_at=?"
                " WHERE scope_type=? AND scope_id=?",
                (_iso(_now()), scope_type, scope_id),
            )
        return cur.rowcount > 0

    # ---- budget_incidents (임계 초과 사건) ----
    @staticmethod
    def _incident_json(r: Any) -> dict[str, Any]:
        d = dict(r)
        for k in ("amount_limit", "amount_observed"):
            try:
                d[k] = float(d.get(k) or 0)
            except (TypeError, ValueError):
                d[k] = 0.0
        # microusd 필드 — observed_spend_microusd는 NULL 허용(0 대체 금지)
        osm = d.get("observed_spend_microusd")
        if osm is not None:
            try:
                d["observed_spend_microusd"] = int(osm)
            except (TypeError, ValueError):
                d["observed_spend_microusd"] = None
        else:
            d["observed_spend_microusd"] = None
        try:
            d["unpriced_event_count"] = max(0, int(d.get("unpriced_event_count") or 0))
        except (TypeError, ValueError):
            d["unpriced_event_count"] = 0
        d["reason_code"] = str(d.get("reason_code") or "")[:100]
        return d

    def has_open_budget_incident(
        self, policy_id: str, window_start: str, threshold_type: str
    ) -> bool:
        row = self._conn.execute(
            "SELECT 1 AS x FROM budget_incidents WHERE policy_id=? AND window_start=?"
            " AND threshold_type=? AND status='open' LIMIT 1",
            (policy_id, window_start, threshold_type),
        ).fetchone()
        return row is not None

    def create_budget_incident(
        self,
        policy_id: str,
        scope_type: str,
        scope_id: str,
        window_start: str,
        threshold_type: str,
        amount_limit: float,
        amount_observed: float,
        note: str = "",
        *,
        observed_spend_microusd: int | None = None,
        unpriced_event_count: int = 0,
        reason_code: str = "",
    ) -> str | None:
        """open 인시던트가 같은 (policy, window, threshold)에 있으면 None(멱등).

        has_open 검사~INSERT 사이 레이스는 부분 UNIQUE 인덱스
        (idx_budget_incidents_open)가 막고, 위반 시 None을 반환한다.
        observed_spend_microusd는 microusd 권위 지출 — None이면
        amount_observed(레거시 float)만 기록된다."""
        if self.has_open_budget_incident(policy_id, window_start, threshold_type):
            return None
        if observed_spend_microusd is not None and (
            not isinstance(observed_spend_microusd, int)
            or isinstance(observed_spend_microusd, bool)
        ):
            observed_spend_microusd = None
        try:
            unpriced_n = max(0, int(unpriced_event_count))
        except (TypeError, ValueError):
            unpriced_n = 0
        iid = uuid.uuid4().hex[:12]
        try:
            with self.tx() as c:
                c.execute(
                    "INSERT INTO budget_incidents(id,policy_id,scope_type,scope_id,window_start,"
                    "threshold_type,amount_limit,amount_observed,status,note,created_at,"
                    "observed_spend_microusd,unpriced_event_count,reason_code)"
                    " VALUES(?,?,?,?,?,?,?,?,'open',?,?,?,?,?)",
                    (
                        iid,
                        policy_id,
                        scope_type,
                        scope_id,
                        window_start,
                        threshold_type,
                        float(amount_limit),
                        float(amount_observed),
                        note[:500],
                        _iso(_now()),
                        observed_spend_microusd,
                        unpriced_n,
                        (reason_code or "")[:100],
                    ),
                )
        except self._integrity_errors:
            return None  # 동시 생성 레이스 — 이미 open 인시던트 존재
        return iid

    def get_budget_incident(self, incident_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM budget_incidents WHERE id=?", (incident_id,)
        ).fetchone()
        return self._incident_json(row) if row else None

    def list_budget_incidents(
        self, status: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM budget_incidents"
        params: list[Any] = []
        if status:
            sql += " WHERE status=?"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        return [self._incident_json(r) for r in self._conn.execute(sql, params).fetchall()]

    def open_budget_incidents_for_policy(self, policy_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM budget_incidents WHERE policy_id=? AND status='open'"
            " ORDER BY created_at",
            (policy_id,),
        ).fetchall()
        return [self._incident_json(r) for r in rows]

    def resolve_budget_incident(
        self,
        incident_id: str,
        status: str = "resolved",
        note: str = "",
        resolved_at: str | None = None,
    ) -> bool:
        """status: resolved|dismissed. 이미 종료된 인시던트는 False."""
        with self.tx() as c:
            cur = c.execute(
                "UPDATE budget_incidents SET status=?, note=CASE WHEN ?<>'' THEN ? ELSE note END,"
                " resolved_at=? WHERE id=? AND status='open'",
                (status, note, note, resolved_at or _iso(_now()), incident_id),
            )
        return cur.rowcount > 0

    def resolve_open_budget_incidents_for_policy(
        self, policy_id: str, status: str = "resolved", note: str = ""
    ) -> int:
        """정책의 open 인시던트를 전부 종료 — raise_budget_and_resume 경로용."""
        with self.tx() as c:
            cur = c.execute(
                "UPDATE budget_incidents SET status=?, resolved_at=?,"
                " note=CASE WHEN ?<>'' THEN ? ELSE note END"
                " WHERE policy_id=? AND status='open'",
                (status, _iso(_now()), note, note, policy_id),
            )
        return cur.rowcount

    # ---- 스코프별 월간 지출 원시 행 (비용 환산은 jobmate.budget_scope) ----
    def budget_spend_rows(
        self, since_iso: str, scope_type: str = "global", scope_id: str = ""
    ) -> list[dict[str, Any]]:
        """since 이후 stage_runs×runs 조인 행 — model/tokens만 반환.

        USD 환산은 호출자가 pricing.cost_usd로 수행(단가표 단일 관리).

        알 수 없는 scope_type은 필터 없이 전체 지출을 반환하던 버그가 있어
        빈 결과(지출 0)로 처리한다."""
        if scope_type not in ("global", "workflow", "project", "agent"):
            return []
        sql = (
            "SELECT sr.model, sr.tokens_in, sr.tokens_out FROM stage_runs sr"
            " JOIN runs r ON r.id = sr.run_id WHERE sr.started_at >= ?"
        )
        params: list[Any] = [since_iso]
        if scope_type == "workflow" and scope_id:
            sql += " AND r.workflow_key = ?"
            params.append(scope_id)
        elif scope_type == "project" and scope_id:
            sql += " AND r.project = ?"
            params.append(scope_id)
        elif scope_type == "agent" and scope_id:
            # agent 스코프 — runs.agent_id 태그 기준 귀속
            sql += " AND r.agent_id = ?"
            params.append(scope_id)
        rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
