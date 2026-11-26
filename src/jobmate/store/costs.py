# mypy: disable-error-code="attr-defined"
"""비용 원장 스토어 메서드 믹스인 (db.py Store에서 추출).

paperclip-agent-management-spec 2차 A/C — cost_events 테이블.
금액은 정수 microusd만 다룬다(float 금액 계산 금지 — 환산/표시는
호출 계층 책임). cost_status='unpriced' ⇔ cost_microusd IS NULL 불변식을
record_cost_event가 강제한다 — 가격을 모르는 사용량을 0으로 기록하지 않는다.
"""

from __future__ import annotations

import uuid
from typing import Any

from ..models import BillingType, CostStatus
from .util import _clamp_limit, _iso, _now

_COST_STATUSES = {s.value for s in CostStatus}
_BILLING_TYPES = {s.value for s in BillingType}

# 예산 스코프 어휘 — budget_scope.SCOPE_TYPES와 동일 집합을 유지한다
# (store 계층이 서비스 계층을 import하지 않도록 여기서 독립 정의).
_COST_SCOPE_TYPES = ("global", "workflow", "project", "agent", "task")


def _is_strict_int(v: Any) -> bool:
    """bool/float/str을 배제한 진짜 int만 True — 금액·토큰 입력 검증용."""
    return isinstance(v, int) and not isinstance(v, bool)


class CostStoreMixin:
    # ---- cost_events (비용 원장) ----
    @staticmethod
    def _cost_event_json(r: Any) -> dict[str, Any]:
        d = dict(r)
        for k in (
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "unpriced_event_count",
        ):
            if k in d:
                try:
                    d[k] = int(d.get(k) or 0)
                except (TypeError, ValueError):
                    d[k] = 0
        # cost_microusd는 NULL 허용 — 0으로 대체하지 않는다
        cm = d.get("cost_microusd")
        if cm is not None:
            try:
                d["cost_microusd"] = int(cm)
            except (TypeError, ValueError):
                d["cost_microusd"] = None
        for k in ("task_id", "run_id", "stage_run_id"):
            if d.get(k) is None:
                d[k] = None
        st = str(d.get("cost_status") or "").strip().lower()
        d["cost_status"] = st if st in _COST_STATUSES else CostStatus.UNPRICED.value
        bt = str(d.get("billing_type") or "").strip().lower()
        d["billing_type"] = bt if bt in _BILLING_TYPES else BillingType.UNKNOWN.value
        return d

    @staticmethod
    def _validate_token_count(name: str, v: Any) -> int:
        """토큰 수 검증 — 음수/bool/float/str 거부, 비음수 int만 허용."""
        if not _is_strict_int(v):
            raise ValueError(f"{name} must be an integer")
        n = int(v)
        if n < 0:
            raise ValueError(f"{name} must be >= 0")
        return n

    def record_cost_event(
        self,
        *,
        idempotency_key: str,
        agent_id: str = "",
        task_id: str | None = None,
        run_id: str | None = None,
        stage_run_id: str | None = None,
        provider: str = "",
        model: str = "",
        billing_type: str = "unknown",
        cost_status: str = "unpriced",
        input_tokens: int = 0,
        cached_input_tokens: int = 0,
        output_tokens: int = 0,
        cost_microusd: int | None = None,
        pricing_source: str = "",
        pricing_version: str = "",
        occurred_at: str | None = None,
    ) -> tuple[dict[str, Any] | None, bool]:
        """비용 이벤트 기록 — (event, created) 반환. append_agent_event와 동일 패턴.

        idempotency_key UNIQUE: 동일 키 재전송은 기존 행을 반환하고
        created=False — stage_run 재처리/외부 runner 재전송의 중복 청구를 막는다.

        검증(spec 2차 A):
        - 토큰 수·cost_microusd는 bool/float/str/음수 거부 — 정수 microusd만.
        - cost_status='unpriced' ⇔ cost_microusd IS NULL — 미가격 사용량을
          0으로 대체 기록하지 않는다. priced 상태에 cost_microusd가 없으면
          unpriced로 기록해야 한다.
        - agent_id는 spec상 필수지만 미태깅 run이 많아 ''를 허용한다
          (스펙 대비 의도적 완화 — docs §24).
        """
        key = (idempotency_key or "").strip()[:200]
        if not key:
            key = f"auto:{uuid.uuid4().hex}"
        tin = self._validate_token_count("input_tokens", input_tokens)
        tcached = self._validate_token_count("cached_input_tokens", cached_input_tokens)
        tout = self._validate_token_count("output_tokens", output_tokens)
        status = (cost_status or "").strip().lower()[:20]
        if status not in _COST_STATUSES:
            raise ValueError(f"invalid cost_status: {cost_status}")
        bt = (billing_type or "").strip().lower()[:40]
        if bt not in _BILLING_TYPES:
            raise ValueError(f"invalid billing_type: {billing_type}")
        if cost_microusd is not None:
            if not _is_strict_int(cost_microusd):
                raise ValueError("cost_microusd must be an integer or null")
            cm_i = int(cost_microusd)
            if cm_i < 0:
                raise ValueError("cost_microusd must be >= 0")
            cm: int | None = cm_i
        else:
            cm = None
        # NULL iff unpriced 불변식 — 양방향 강제
        if status == CostStatus.UNPRICED.value and cm is not None:
            raise ValueError("unpriced cost event must have cost_microusd=NULL")
        if status != CostStatus.UNPRICED.value and cm is None:
            raise ValueError("priced cost event requires integer cost_microusd")
        now = _iso(_now())
        cid = uuid.uuid4().hex[:12]
        try:
            with self.tx() as c:
                c.execute(
                    "INSERT INTO cost_events(id,idempotency_key,agent_id,task_id,"
                    "run_id,stage_run_id,provider,model,billing_type,cost_status,"
                    "input_tokens,cached_input_tokens,output_tokens,cost_microusd,"
                    "pricing_source,pricing_version,occurred_at,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        cid,
                        key,
                        (agent_id or "").strip()[:64],
                        (task_id or "").strip()[:64] or None,
                        (run_id or "").strip()[:64] or None,
                        (stage_run_id or "").strip()[:64] or None,
                        (provider or "").strip()[:64],
                        (model or "").strip()[:200],
                        bt,
                        status,
                        tin,
                        tcached,
                        tout,
                        cm,
                        (pricing_source or "").strip()[:100],
                        (pricing_version or "").strip()[:40],
                        occurred_at or now,
                        now,
                    ),
                )
        except self._integrity_errors:
            existing = self.get_cost_event_by_key(key)
            if existing is not None:
                return existing, False
            raise
        return self.get_cost_event_by_key(key), True

    def get_cost_event_by_key(self, idempotency_key: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM cost_events WHERE idempotency_key=?",
            ((idempotency_key or "").strip()[:200],),
        ).fetchone()
        return self._cost_event_json(row) if row else None

    def get_cost_event(self, event_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM cost_events WHERE id=?", (event_id,)).fetchone()
        return self._cost_event_json(row) if row else None

    def list_cost_events(
        self,
        *,
        agent_id: str | None = None,
        task_id: str | None = None,
        run_id: str | None = None,
        stage_run_id: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        cost_status: str | None = None,
        billing_type: str | None = None,
        occurred_from: str | None = None,
        occurred_to: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """비용 이벤트 조회 — occurred_at DESC, id DESC 고정."""
        sql = "SELECT * FROM cost_events"
        conds: list[str] = []
        params: list[Any] = []
        if agent_id:
            conds.append("agent_id=?")
            params.append(agent_id.strip()[:64])
        if task_id:
            conds.append("task_id=?")
            params.append(task_id.strip()[:64])
        if run_id:
            conds.append("run_id=?")
            params.append(run_id.strip()[:64])
        if stage_run_id:
            conds.append("stage_run_id=?")
            params.append(stage_run_id.strip()[:64])
        if provider:
            conds.append("provider=?")
            params.append(provider.strip()[:64])
        if model:
            conds.append("model=?")
            params.append(model.strip()[:200])
        if cost_status:
            conds.append("cost_status=?")
            params.append(cost_status.strip().lower()[:20])
        if billing_type:
            conds.append("billing_type=?")
            params.append(billing_type.strip().lower()[:40])
        if occurred_from:
            conds.append("occurred_at >= ?")
            params.append(occurred_from[:64])
        if occurred_to:
            conds.append("occurred_at < ?")
            params.append(occurred_to[:64])
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY occurred_at DESC, id DESC LIMIT ?"
        params.append(_clamp_limit(limit, 100, 500))
        return [self._cost_event_json(r) for r in self._conn.execute(sql, params).fetchall()]

    # ---- 스코프별 원장 지출 (budget_scope microusd 경로의 원시 합계) ----
    def _cost_scope_joins(self, scope_type: str, scope_id: str) -> tuple[str, list[Any]]:
        """cost_events 스코프 필터 — runs LEFT JOIN으로 workflow/project 귀속.

        agent 스코프는 ce.agent_id를 우선하고 비어 있으면 runs.agent_id로
        폴백한다(외부 runner 이벤트가 run만 태깅한 경우 귀속 유지)."""
        join = " LEFT JOIN runs r ON r.id = ce.run_id"
        cond = ""
        params: list[Any] = []
        sid = (scope_id or "").strip()[:200]
        if scope_type == "agent" and sid:
            cond = " AND COALESCE(NULLIF(ce.agent_id,''), r.agent_id) = ?"
            params.append(sid)
        elif scope_type == "workflow" and sid:
            cond = " AND r.workflow_key = ?"
            params.append(sid)
        elif scope_type == "project" and sid:
            cond = " AND r.project = ?"
            params.append(sid)
        elif scope_type == "task" and sid:
            cond = " AND ce.task_id = ?"
            params.append(sid)
        elif scope_type != "global":
            # 알 수 없는 scope_type은 전체 합계를 반환하지 않고 0행 —
            # 잘못된 스코프가 전체 예산을 소비한 것처럼 보이는 것을 막는다.
            cond = " AND 1=0"
        return join + cond, params

    def cost_events_spend_microusd(
        self,
        since_iso: str = "",
        scope_type: str = "global",
        scope_id: str = "",
        until_iso: str | None = None,
    ) -> int:
        """원장 이벤트의 microusd 합계 — cost_microusd IS NULL(unpriced) 제외.

        unpriced 사용량은 지출에 0으로 더하지 않고 별도 카운트
        (unpriced_cost_event_count)로 다룬다 — '모름'과 '0원'을 구분."""
        if scope_type not in _COST_SCOPE_TYPES:
            return 0
        scope_sql, scope_params = self._cost_scope_joins(scope_type, scope_id)
        sql = (
            "SELECT COALESCE(SUM(ce.cost_microusd),0) AS s FROM cost_events ce"  # noqa: S608 - scope_sql은 고정 fragment + 바운드 파라미터
            + scope_sql
            + " WHERE ce.cost_microusd IS NOT NULL"
        )
        # scope_sql의 ? 는 WHERE 절보다 앞에 위치하므로 scope_params를 먼저 둔다.
        params: list[Any] = list(scope_params)
        if since_iso:
            sql += " AND ce.occurred_at >= ?"
            params.append(since_iso)
        if until_iso:
            sql += " AND ce.occurred_at < ?"
            params.append(until_iso)
        row = self._conn.execute(sql, params).fetchone()
        try:
            return int(row["s"] or 0) if row is not None else 0
        except (TypeError, ValueError):
            return 0

    def unpriced_cost_event_count(
        self,
        since_iso: str = "",
        scope_type: str = "global",
        scope_id: str = "",
        until_iso: str | None = None,
    ) -> int:
        """기간 내 unpriced(cost_microusd IS NULL) 이벤트 수 — 스코프 귀속."""
        if scope_type not in _COST_SCOPE_TYPES:
            return 0
        scope_sql, scope_params = self._cost_scope_joins(scope_type, scope_id)
        sql = (
            "SELECT COUNT(*) AS n FROM cost_events ce"  # noqa: S608 - scope_sql은 고정 fragment + 바운드 파라미터
            + scope_sql
            + " WHERE 1=1"
        )
        # scope_sql의 ? 는 WHERE 절보다 앞에 위치하므로 scope_params를 먼저 둔다.
        params: list[Any] = list(scope_params)
        if since_iso:
            sql += " AND ce.occurred_at >= ?"
            params.append(since_iso)
        if until_iso:
            sql += " AND ce.occurred_at < ?"
            params.append(until_iso)
        # NULL iff unpriced 불변식 — cost_microusd IS NULL이 곧 unpriced
        sql += " AND ce.cost_microusd IS NULL"
        row = self._conn.execute(sql, params).fetchone()
        try:
            return int(row["n"] or 0) if row is not None else 0
        except (TypeError, ValueError):
            return 0

    def cost_summary(
        self,
        scope_type: str = "global",
        scope_id: str = "",
        occurred_from: str | None = None,
        occurred_to: str | None = None,
    ) -> dict[str, Any]:
        """GET /management/costs/summary용 — 스코프·기간 합계.

        total_microusd는 priced 이벤트만 합산하고 unpriced는 별도 카운트 —
        '미가격'을 0원으로 뭉개지 않는다. *_usd 필드는 표시 호환용 파생값."""
        if scope_type not in _COST_SCOPE_TYPES:
            scope_type = "global"
            scope_id = ""
        scope_sql, scope_params = self._cost_scope_joins(scope_type, scope_id)
        sql = (
            "SELECT ce.cost_status AS cost_status, COUNT(*) AS n,"  # noqa: S608 - scope_sql은 고정 fragment + 바운드 파라미터
            " COALESCE(SUM(ce.cost_microusd),0) AS s"
            " FROM cost_events ce" + scope_sql + " WHERE 1=1"
        )
        # scope_sql의 ? 는 WHERE 절보다 앞에 위치하므로 scope_params를 먼저 둔다.
        params: list[Any] = list(scope_params)
        if occurred_from:
            sql += " AND ce.occurred_at >= ?"
            params.append(occurred_from[:64])
        if occurred_to:
            sql += " AND ce.occurred_at < ?"
            params.append(occurred_to[:64])
        sql += " GROUP BY ce.cost_status"
        by_status: dict[str, dict[str, int]] = {}
        total_mu = 0
        total_n = 0
        for r in self._conn.execute(sql, params).fetchall():
            st = str(r["cost_status"] or "unpriced")
            n = int(r["n"] or 0)
            s = int(r["s"] or 0)
            by_status[st] = {"count": n, "microusd": s}
            total_n += n
            total_mu += s
        return {
            "scope_type": scope_type,
            "scope_id": scope_id if scope_type != "global" else "",
            "from": occurred_from or "",
            "to": occurred_to or "",
            "event_count": total_n,
            "total_microusd": total_mu,
            "total_usd": total_mu / 1_000_000,
            "unpriced_count": by_status.get("unpriced", {}).get("count", 0),
            "by_status": by_status,
        }

    def uncovered_stage_spend_rows(
        self, since_iso: str, scope_type: str = "global", scope_id: str = ""
    ) -> list[dict[str, Any]]:
        """cost_events 미커버 stage_runs의 (model,tokens) 원시 행.

        runner가 방출한 cost 이벤트(stage_run_id 연결)와 백필 행이 있는
        stage_run은 제외해 이중 계상을 막는다. 남은 행은 cost 원장이 아직
        없는 레거시/외부 경로의 사용량 — budget_scope가 rate card로
        microusd 환산해 원장 지출에 합산한다."""
        if scope_type not in _COST_SCOPE_TYPES:
            return []
        sql = (
            "SELECT sr.model, sr.tokens_in, sr.tokens_out FROM stage_runs sr"
            " JOIN runs r ON r.id = sr.run_id"
            " WHERE sr.started_at >= ?"
            " AND NOT EXISTS (SELECT 1 FROM cost_events ce"
            "  WHERE ce.stage_run_id = sr.id)"
        )
        params: list[Any] = [since_iso]
        sid = (scope_id or "").strip()[:200]
        if scope_type == "workflow" and sid:
            sql += " AND r.workflow_key = ?"
            params.append(sid)
        elif scope_type == "project" and sid:
            sql += " AND r.project = ?"
            params.append(sid)
        elif scope_type == "agent" and sid:
            sql += " AND r.agent_id = ?"
            params.append(sid)
        elif scope_type == "task" and sid:
            sql += (
                " AND EXISTS (SELECT 1 FROM task_runs tr"
                "  WHERE tr.run_id = r.id AND tr.task_id = ?)"
            )
            params.append(sid)
        rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def stage_runs_missing_cost_events(self, limit: int = 500) -> list[dict[str, Any]]:
        """종결됐지만 cost_events 행이 없는 stage_runs — 백필 대상.

        stage_run_id가 연결된 cost_events가 하나라도 있으면(live 'cost:' 키든
        'backfill:' 키든) 제외 — 재실행/재기동 시 중복 백필을 막는다."""
        rows = self._conn.execute(
            "SELECT sr.id, sr.run_id, sr.stage_key, sr.model, sr.tokens_in,"
            " sr.tokens_out, sr.started_at, sr.finished_at,"
            " r.agent_id AS run_agent_id"
            " FROM stage_runs sr JOIN runs r ON r.id = sr.run_id"
            " WHERE sr.finished_at IS NOT NULL"
            # 토큰 0인 stage_run은 emit이 기록하지 않으므로 영구 미커버로
            # 남아 백필 done 가드가 영원히 안 서는 것을 막는다.
            " AND COALESCE(sr.tokens_in,0) + COALESCE(sr.tokens_out,0) > 0"
            " AND NOT EXISTS (SELECT 1 FROM cost_events ce"
            "  WHERE ce.stage_run_id = sr.id)"
            " ORDER BY sr.started_at LIMIT ?",
            (_clamp_limit(limit, 500, 2000),),
        ).fetchall()
        return [dict(r) for r in rows]
