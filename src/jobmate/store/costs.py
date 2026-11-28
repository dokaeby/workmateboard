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
from . import orgctx
from .util import _clamp_limit, _iso, _now

_COST_STATUSES = {s.value for s in CostStatus}
_BILLING_TYPES = {s.value for s in BillingType}


def _org_filter(alias: str = "") -> tuple[str, list[Any]]:
    """ctx 조직이 있으면 ` AND [alias.]org_id=?` 조건 — ctx=''는 무필터."""
    oid = orgctx.current_org_id()
    if not oid:
        return "", []
    col = f"{alias}.org_id" if alias else "org_id"
    return f" AND {col}=?", [oid]


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
        org_id: str | None = None,
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
        # spec 003 US3 — UNIQUE(org_id,idempotency_key) 복합키가 조직을
        # 네임스페이스하므로 원시 키를 그대로 저장한다.
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
        # org 귀속: 명시 > 부모(run→task→agent) > ctx. run 실행/백필 등
        # ctx='' 경로에서도 비용 이벤트가 올바른 조직 원장에 기록되게 한다.
        oid = (org_id or "").strip()
        if not oid:
            oid = self._cost_parent_org(run_id, task_id, agent_id)
        if not oid:
            oid = orgctx.org_for_write()
        try:
            with self.tx() as c:
                c.execute(
                    "INSERT INTO cost_events(id,idempotency_key,agent_id,task_id,"
                    "run_id,stage_run_id,provider,model,billing_type,cost_status,"
                    "input_tokens,cached_input_tokens,output_tokens,cost_microusd,"
                    "pricing_source,pricing_version,occurred_at,created_at,org_id)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                        oid,
                    ),
                )
        except self._integrity_errors:
            existing = self.get_cost_event_by_key(key, org_id=oid)
            if existing is not None:
                return existing, False
            raise
        return self.get_cost_event_by_key(key, org_id=oid), True

    def _cost_parent_org(self, run_id: str | None, task_id: str | None, agent_id: str) -> str:
        """비용 이벤트의 부모 행(org-scoped)에서 org_id 상속 — run 우선."""
        for table, pid in (("runs", run_id), ("tasks", task_id), ("agents", agent_id)):
            pid_s = (pid or "").strip()[:64]
            if not pid_s:
                continue
            row = self._conn.execute(
                f"SELECT org_id FROM {table} WHERE id=?",  # noqa: S608 - table은 고정 리터럴
                (pid_s,),
            ).fetchone()
            if row is not None:
                return str(row["org_id"] or "")
        return ""

    def get_cost_event_by_key(
        self, idempotency_key: str, *, org_id: str | None = None
    ) -> dict[str, Any] | None:
        key = (idempotency_key or "").strip()[:200]
        if org_id is not None:
            # record_cost_event replay — (org_id,key) 복합 UNIQUE에 대응.
            row = self._conn.execute(
                "SELECT * FROM cost_events WHERE idempotency_key=? AND org_id=?",
                (key, org_id),
            ).fetchone()
        else:
            frag, params = _org_filter()
            row = self._conn.execute(
                "SELECT * FROM cost_events WHERE idempotency_key=?" + frag,  # noqa: S608
                (key, *params),
            ).fetchone()
        return self._cost_event_json(row) if row else None

    def get_cost_event(self, event_id: str) -> dict[str, Any] | None:
        frag, params = _org_filter()
        row = self._conn.execute(
            "SELECT * FROM cost_events WHERE id=?" + frag,  # noqa: S608
            (event_id, *params),
        ).fetchone()
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
        frag, fparams = _org_filter()
        if frag:
            conds.append(frag.strip().removeprefix("AND").strip())
            params.extend(fparams)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY occurred_at DESC, id DESC LIMIT ?"
        params.append(_clamp_limit(limit, 100, 500))
        return [self._cost_event_json(r) for r in self._conn.execute(sql, params).fetchall()]

    # ---- 스코프별 원장 지출 (budget_scope microusd 경로의 원시 합계) ----
    def _cost_scope_joins(self, scope_type: str, scope_id: str) -> tuple[str, str, list[Any]]:
        """cost_events 스코프 필터 — (join_sql, where_cond, params) 반환.

        스코프 조건은 반드시 WHERE 절에 둬야 한다 — JOIN ... ON에 넣으면
        LEFT JOIN 특성상 매칭 실패 행이 r.* = NULL로 살아남아 모든 비-global
        스코프가 전체 합계를 반환하는 버그가 된다(H-원장 스코프 누수).

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
            # 알 수 없는 scope_type/빈 scope_id는 전체 합계를 반환하지 않고
            # 0행 — 잘못된 스코프가 전체 예산을 소비한 것처럼 보이는 것을 막는다.
            cond = " AND 1=0"
        return join, cond, params

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
        join_sql, cond_sql, scope_params = self._cost_scope_joins(scope_type, scope_id)
        org_sql, org_params = _org_filter("ce")
        sql = (
            "SELECT COALESCE(SUM(ce.cost_microusd),0) AS s FROM cost_events ce"  # noqa: S608 - join/cond는 고정 fragment + 바운드 파라미터
            + join_sql
            + " WHERE ce.cost_microusd IS NOT NULL"
            + cond_sql
            + org_sql
        )
        # cond_sql/org_sql의 ? 는 WHERE 절 선두에 위치하므로 scope→org 순으로 둔다.
        params: list[Any] = [*scope_params, *org_params]
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
        join_sql, cond_sql, scope_params = self._cost_scope_joins(scope_type, scope_id)
        org_sql, org_params = _org_filter("ce")
        sql = (
            "SELECT COUNT(*) AS n FROM cost_events ce"  # noqa: S608 - join/cond는 고정 fragment + 바운드 파라미터
            + join_sql
            + " WHERE 1=1"
            + cond_sql
            + org_sql
        )
        # cond_sql/org_sql의 ? 는 WHERE 절 선두에 위치하므로 scope→org 순으로 둔다.
        params: list[Any] = [*scope_params, *org_params]
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
        '미가격'을 0원으로 뭉개지 않는다. *_usd 필드는 표시 호환용 파생값.
        알 수 없는 scope_type은 sibling 함수들과 동일하게 0행 결과를 반환한다
        (_cost_scope_joins가 'AND 1=0'을 붙인다) — global로 강제 변환해
        전체 합계를 보여주지 않는다."""
        join_sql, cond_sql, scope_params = self._cost_scope_joins(scope_type, scope_id)
        org_sql, org_params = _org_filter("ce")
        sql = (
            "SELECT ce.cost_status AS cost_status, COUNT(*) AS n,"  # noqa: S608 - join/cond는 고정 fragment + 바운드 파라미터
            " COALESCE(SUM(ce.cost_microusd),0) AS s"
            " FROM cost_events ce" + join_sql + " WHERE 1=1" + cond_sql + org_sql
        )
        # cond_sql/org_sql의 ? 는 WHERE 절 선두에 위치하므로 scope→org 순으로 둔다.
        params: list[Any] = [*scope_params, *org_params]
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
        # by_agent — 비용 뷰(v2)가 기대하는 에이전트별 집계. 귀속 표현식은
        # _cost_scope_joins의 agent 스코프와 동일하게 맞춘다(ce.agent_id 우선,
        # 비어 있으면 runs.agent_id 폴백). unpriced 이벤트만 있는 에이전트도
        # unpriced_count와 함께 포함한다 — '미가격'을 누락시키지 않는다.
        agent_expr = "COALESCE(NULLIF(ce.agent_id,''), r.agent_id, '')"
        agent_sql = (
            f"SELECT {agent_expr} AS agent_id,"  # noqa: S608 - agent_expr는 고정 fragment
            " COALESCE(SUM(ce.cost_microusd),0) AS s,"
            " SUM(CASE WHEN ce.cost_microusd IS NULL THEN 1 ELSE 0 END) AS unpriced"
            " FROM cost_events ce" + join_sql + " WHERE 1=1" + cond_sql + org_sql
        )
        # WHERE 절은 by_status 쿼리와 동일 — params는 이미 scope→org→
        # occurred_from/to 순으로 바인드돼 있으므로 값을 다시 append하지 않는다.
        if occurred_from:
            agent_sql += " AND ce.occurred_at >= ?"
        if occurred_to:
            agent_sql += " AND ce.occurred_at < ?"
        agent_params: list[Any] = list(params)
        agent_sql += f" GROUP BY {agent_expr} ORDER BY s DESC, agent_id LIMIT 50"  # noqa: S608
        by_agent: list[dict[str, Any]] = []
        for r in self._conn.execute(agent_sql, agent_params).fetchall():
            by_agent.append(
                {
                    "agent_id": str(r["agent_id"] or ""),
                    "total_microusd": int(r["s"] or 0),
                    "unpriced_count": int(r["unpriced"] or 0),
                }
            )
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
            "by_agent": by_agent,
        }

    def uncovered_stage_spend_rows(
        self, since_iso: str, scope_type: str = "global", scope_id: str = ""
    ) -> list[dict[str, Any]]:
        """cost_events 미커버 stage_runs의 (model,tokens) 원시 행.

        runner가 방출한 cost 이벤트(stage_run_id 연결)와 백필 행이 있는
        stage_run은 제외해 이중 계상을 막는다. 남은 행은 cost 원장이 아직
        없는 레거시/외부 경로의 사용량 — budget_scope가 rate card로
        microusd 환산해 원장 지출에 합산한다.

        알려진 브리지 갭(수용): run_id는 있지만 stage_run_id가 없는 외부
        cost_events는 해당 stage_run의 토큰 기반 미커버 행과 별개로 합산돼
        이중 계상될 수 있다 — 외부 runner가 stage_run_id를 태깅하도록
        유도하는 것이 완전한 해법이다.

        월 경계 차이(수용): 원장 이벤트는 occurred_at으로, 이 함수의 미커버
        행은 sr.started_at으로 기간을 자른다 — 경계 시각의 stage_run은
        원장/미커버 집계가 어긋날 수 있으나 드물고 허용 범위로 본다."""
        if scope_type not in _COST_SCOPE_TYPES:
            return []
        org_sql, org_params = _org_filter("r")
        sql = (
            "SELECT sr.model, sr.tokens_in, sr.tokens_out FROM stage_runs sr"  # noqa: S608
            " JOIN runs r ON r.id = sr.run_id"
            " WHERE sr.started_at >= ?" + org_sql + " AND NOT EXISTS (SELECT 1 FROM cost_events ce"
            "  WHERE ce.stage_run_id = sr.id)"
        )
        params: list[Any] = [since_iso, *org_params]
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
