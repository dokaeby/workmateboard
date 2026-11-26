# mypy: disable-error-code="attr-defined"
"""Agent registry 스토어 메서드 믹스인 (db.py Store에서 추출).

agents 테이블 — Paperclip agents 포팅: adapter_type(provider) + reports_to
조직도 + status 라이프사이클 + kind(workflow|external 실행 모델) +
adapter_config JSON. status 어휘는 models.management의 AgentStatus 7값에
레거시 'archived'(=terminated 별칭)를 더한 집합 — canonical 판정은
서비스/모델 계층이 담당한다.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from .util import _iso, _now

_AGENT_KINDS = ("workflow", "external")


class AgentStoreMixin:
    # ---- agents (에이전트 레지스트리/조직도) ----
    @staticmethod
    def _agent_json(r: Any) -> dict[str, Any]:
        d = dict(r)
        try:
            caps = json.loads(d.get("capabilities") or "[]")
            d["capabilities"] = [str(c) for c in caps] if isinstance(caps, list) else []
        except (ValueError, TypeError):
            d["capabilities"] = []
        try:
            cfg = json.loads(d.get("adapter_config") or "{}")
            d["adapter_config"] = cfg if isinstance(cfg, dict) else {}
        except (ValueError, TypeError):
            d["adapter_config"] = {}
        try:
            d["monthly_budget_usd"] = (
                float(d["monthly_budget_usd"]) if d.get("monthly_budget_usd") is not None else None
            )
        except (TypeError, ValueError):
            d["monthly_budget_usd"] = None
        try:
            d["monthly_budget_microusd"] = int(d.get("monthly_budget_microusd") or 0)
        except (TypeError, ValueError):
            d["monthly_budget_microusd"] = 0
        # 미설정 TEXT 컬럼은 None 대신 ""로 정규화 — API 계약상 null 가능 필드는
        # role/title/description/reports_to/last_seen_at만 null을 유지한다.
        for k in ("role", "title", "description"):
            if d.get(k) is None:
                d[k] = ""
        kind = str(d.get("kind") or "").strip().lower()
        d["kind"] = kind if kind in _AGENT_KINDS else "workflow"
        return d

    def create_agent(
        self,
        name: str,
        adapter_type: str,
        *,
        role: str = "",
        title: str = "",
        description: str = "",
        capabilities: list[str] | None = None,
        kind: str = "workflow",
        adapter_config: dict[str, Any] | None = None,
        reports_to: str | None = None,
        status: str = "active",
        monthly_budget_usd: float | None = None,
        monthly_budget_microusd: int = 0,
        created_by: str = "",
    ) -> dict[str, Any] | None:
        """에이전트 생성. name은 trim 후 lower() 기준 중복 시 None."""
        name = name.strip()[:100]
        adapter_type = adapter_type.strip().lower()[:32]
        if not name or not adapter_type:
            return None
        if self.find_agent_by_name(name) is not None:
            return None
        rt = (reports_to or "").strip()[:64] or None
        # 방어 심화 — SQLite는 PRAGMA foreign_keys가 커넥션별로 꺼져 있을 수
        # 있어 reports_to FK가 강제되지 않는다. 존재하지 않는 상위는 여기서 거부.
        # (앱 계층 agents.validate_reports_to가 archived/사이클까지 검증)
        if rt is not None and self.get_agent(rt) is None:
            return None
        k = (kind or "").strip().lower()[:20]
        if k not in _AGENT_KINDS:
            k = "workflow"
        cfg = adapter_config if isinstance(adapter_config, dict) else {}
        try:
            microusd = max(0, int(monthly_budget_microusd or 0))
        except (TypeError, ValueError):
            microusd = 0
        aid = uuid.uuid4().hex[:12]
        now = _iso(_now())
        try:
            with self.tx() as c:
                c.execute(
                    "INSERT INTO agents(id,name,adapter_type,role,title,description,"
                    "capabilities,kind,adapter_config,reports_to,status,"
                    "monthly_budget_usd,monthly_budget_microusd,created_by,"
                    "created_at,updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        aid,
                        name,
                        adapter_type,
                        role.strip()[:100] or None,
                        title.strip()[:200] or None,
                        description.strip()[:2000] or None,
                        json.dumps([str(c)[:100] for c in (capabilities or [])][:50]),
                        k,
                        json.dumps(cfg, ensure_ascii=False, default=str)[:8000],
                        rt,
                        status.strip().lower()[:20] or "active",
                        monthly_budget_usd,
                        microusd,
                        created_by[:200],
                        now,
                        now,
                    ),
                )
        except self._integrity_errors:
            return None
        return self.get_agent(aid)

    def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
        return self._agent_json(row) if row else None

    def find_agent_by_name(self, name: str) -> dict[str, Any] | None:
        """에이전트 이름 조회 — trim + case-insensitive."""
        row = self._conn.execute(
            "SELECT * FROM agents WHERE lower(name)=lower(?)",
            (name.strip()[:100],),
        ).fetchone()
        return self._agent_json(row) if row else None

    def list_agents(self, status: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM agents"
        params: list[Any] = []
        if status:
            sql += " WHERE status=?"
            params.append(status.strip().lower()[:20])
        sql += " ORDER BY lower(name), name"
        return [self._agent_json(r) for r in self._conn.execute(sql, params).fetchall()]

    def update_agent(self, agent_id: str, **fields: Any) -> dict[str, Any] | None:
        """부분 갱신 — name/adapter_type/role/title/description/capabilities/
        kind/adapter_config/reports_to/status/monthly_budget_usd/
        monthly_budget_microusd/last_seen_at.
        None 반환 = 에이전트 없음 또는 name 중복(integrity)."""
        agent = self.get_agent(agent_id)
        if agent is None:
            return None
        updates: dict[str, Any] = {}
        if "name" in fields:
            new_name = str(fields["name"] or "").strip()[:100]
            if not new_name:
                return None
            other = self.find_agent_by_name(new_name)
            if other is not None and other["id"] != agent_id:
                return None
            updates["name"] = new_name
        if "adapter_type" in fields:
            at = str(fields["adapter_type"] or "").strip().lower()[:32]
            if not at:
                return None
            updates["adapter_type"] = at
        if "role" in fields:
            updates["role"] = str(fields["role"] or "").strip()[:100] or None
        if "title" in fields:
            updates["title"] = str(fields["title"] or "").strip()[:200] or None
        if "description" in fields:
            updates["description"] = str(fields["description"] or "").strip()[:2000] or None
        if "kind" in fields:
            k = str(fields["kind"] or "").strip().lower()[:20]
            updates["kind"] = k if k in _AGENT_KINDS else "workflow"
        if "adapter_config" in fields:
            cfg = fields["adapter_config"]
            updates["adapter_config"] = (
                json.dumps(cfg, ensure_ascii=False, default=str)[:8000]
                if isinstance(cfg, dict)
                else "{}"
            )
        if "capabilities" in fields:
            caps = fields["capabilities"]
            if caps is None:
                updates["capabilities"] = "[]"
            elif isinstance(caps, (list, tuple)):
                updates["capabilities"] = json.dumps(
                    [str(c)[:100] for c in caps][:50], ensure_ascii=False
                )
            else:
                return None
        if "reports_to" in fields:
            rt = fields["reports_to"]
            rt_val = (str(rt).strip()[:64] or None) if rt else None
            if rt_val is not None:
                # 방어 심화 — FK 미강제 커넥션 대비 존재/자기참조 검사.
                # 순환 참조는 앱 계층(reports_to_would_cycle)이 담당 —
                # 여기서 체인을 다시 걷지는 않는다(검사~쓰기 사이 TOCTOU는
                # 잔존 한계로 docs §19에 기록).
                if rt_val == agent_id or self.get_agent(rt_val) is None:
                    return None
            updates["reports_to"] = rt_val
        if "status" in fields:
            st = str(fields["status"] or "").strip().lower()[:20]
            if st:
                updates["status"] = st
        if "monthly_budget_usd" in fields:
            mb = fields["monthly_budget_usd"]
            updates["monthly_budget_usd"] = float(mb) if mb is not None else None
        if "monthly_budget_microusd" in fields:
            try:
                updates["monthly_budget_microusd"] = max(
                    0, int(fields["monthly_budget_microusd"] or 0)
                )
            except (TypeError, ValueError):
                updates["monthly_budget_microusd"] = 0
        if "last_seen_at" in fields:
            lsa = fields["last_seen_at"]
            updates["last_seen_at"] = str(lsa)[:64] if lsa else None
        if not updates:
            return agent
        sets = ", ".join(f"{k}=?" for k in updates)
        try:
            with self.tx() as c:
                c.execute(
                    f"UPDATE agents SET {sets}, updated_at=? WHERE id=?",  # noqa: S608 - keys는 고정 허용 목록
                    (*updates.values(), _iso(_now()), agent_id),
                )
        except self._integrity_errors:
            return None
        return self.get_agent(agent_id)

    def transition_agent_status(
        self, agent_id: str, from_status: str, to_status: str
    ) -> dict[str, Any] | None:
        """status 조건부 전이 — `WHERE lower(status)=from` CAS.

        읽기~쓰기 사이 다른 writer가 status를 바꿨으면 rowcount=0 → None.
        'terminated는 진짜 terminal'을 동시성 하에서도 보장한다 —
        from_status 이후 누군가 terminated로 바꿨다면 이 전이는 실패한다.
        from_status는 canonical 값 — 레거시 'archived' 저장행은
        canonical terminated라 이 경로에 도달하지 않는다(전이 거부됨)."""
        with self.tx() as c:
            cur = c.execute(
                "UPDATE agents SET status=?, updated_at=? WHERE id=? AND lower(status)=?",
                (
                    to_status.strip().lower()[:20],
                    _iso(_now()),
                    agent_id,
                    from_status.strip().lower()[:20],
                ),
            )
        if int(getattr(cur, "rowcount", 0) or 0) <= 0:
            return None
        return self.get_agent(agent_id)

    def archive_agent(self, agent_id: str) -> bool:
        """소프트 삭제 — status='archived'. runs가 참조 중이어도 행은 보존."""
        with self.tx() as c:
            cur = c.execute(
                "UPDATE agents SET status='archived', updated_at=? WHERE id=?",
                (_iso(_now()), agent_id),
            )
        return cur.rowcount > 0

    def reports_to_would_cycle(self, agent_id: str, reports_to: str) -> bool:
        """reports_to 체인을 따라 올라가며 agent_id에 도달하면 True(사이클).

        PATCH에서 자기 자신을 상위로 두는 경우와 a→b→a 순환을 모두 막는다.
        방문 집합으로 방어 — 데이터가 이미 오염돼 있어도 무한 루프하지 않는다."""
        target = reports_to.strip()
        if not target or target == agent_id:
            return True
        seen: set[str] = set()
        cur: str | None = target
        while cur and cur not in seen:
            if cur == agent_id:
                return True
            seen.add(cur)
            row = self._conn.execute("SELECT reports_to FROM agents WHERE id=?", (cur,)).fetchone()
            cur = str(row["reports_to"]) if row and row["reports_to"] else None
        return False

    # ---- 에이전트별 run 집계 (AgentOut.stats용) ----
    def agent_run_stats(self, since_iso: str) -> dict[str, dict[str, Any]]:
        """agent_id별 {runs_30d, last_run_at} — runs.agent_id 기준.

        last_run_at은 기간 제한 없이 전체 이력의 최신 run 시각."""
        out: dict[str, dict[str, Any]] = {}
        rows = self._conn.execute(
            "SELECT agent_id, COUNT(*) AS c FROM runs"
            " WHERE agent_id IS NOT NULL AND agent_id <> '' AND created_at >= ?"
            " GROUP BY agent_id",
            (since_iso,),
        ).fetchall()
        for r in rows:
            out.setdefault(str(r["agent_id"]), {})["runs_30d"] = int(r["c"])
        rows = self._conn.execute(
            "SELECT agent_id, MAX(created_at) AS m FROM runs"
            " WHERE agent_id IS NOT NULL AND agent_id <> ''"
            " GROUP BY agent_id"
        ).fetchall()
        for r in rows:
            out.setdefault(str(r["agent_id"]), {})["last_run_at"] = r["m"]
        return out

    def agent_spend_rows(self, since_iso: str) -> list[dict[str, Any]]:
        """since 이후 agent_id가 태깅된 stage_runs×runs 조인 원시 행.

        USD 환산은 호출자가 pricing.cost_usd로 수행(budget_spend_rows와 동일)."""
        rows = self._conn.execute(
            "SELECT r.agent_id AS agent_id, sr.model AS model,"
            " sr.tokens_in AS tokens_in, sr.tokens_out AS tokens_out"
            " FROM stage_runs sr JOIN runs r ON r.id = sr.run_id"
            " WHERE sr.started_at >= ? AND r.agent_id IS NOT NULL AND r.agent_id <> ''",
            (since_iso,),
        ).fetchall()
        return [dict(r) for r in rows]
