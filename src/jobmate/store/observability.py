# mypy: disable-error-code="attr-defined"
"""spec 007 — 관측성 플랫폼 스토어 메서드 믹스인.

run 타임라인(stage+cost 조인), 비용/사용량/에러/승인 집계,
anomaly_events 기록·조회. org 스코프는 요청 컨텍스트(orgctx)의
_org_filter 패턴을 따른다 — stage_runs는 org_id 컬럼이 없어
반드시 runs와 조인해 조직을 판정한다.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from . import orgctx
from .util import _clamp_limit, _dt, _iso, _loads, _now

logger = logging.getLogger("jobmate.store.observability")

# observability_errors 소스별 조회 상한 — 윈도우 내 에러 행을 전부 Python으로
# 끌어오면 대량 장애 시 응답이 무한정 커진다. 소스(stage_runs/runs) 각각
# 최신 _ERROR_QUERY_LIMIT건만 집계하므로 total은 실제 발생 수의 하한이다
# (UI는 백분율/클래스 분포 용도 — 문서화된 근사치).
_ERROR_QUERY_LIMIT = 1000


def _org_filter(alias: str = "") -> tuple[str, list[Any]]:
    """ctx 조직이 있으면 ` AND [alias.]org_id=?` 조건 — ctx=''는 무필터."""
    oid = orgctx.current_org_id()
    if not oid:
        return "", []
    col = f"{alias}.org_id" if alias else "org_id"
    return f" AND {col}=?", [oid]


def _aware(dt: datetime | None) -> datetime | None:
    """_dt 결과를 aware datetime으로 정규화 — naive ISO(구 스키마/수기 행)와
    aware now의 뺄셈에서 TypeError가 나지 않게 UTC로 간주한다."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def _summary(text: Any, limit: int = 500) -> str:
    """stage input/output JSON → 사람이 읽는 ~500자 요약 문자열."""
    if text is None:
        return ""
    if isinstance(text, str):
        s = text
    else:
        try:
            s = json.dumps(text, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            s = str(text)
    s = s.strip()
    return s[:limit]


class ObservabilityStoreMixin:
    # ---- anomaly_events (spec 007 US7) ----
    def record_anomaly_event(
        self,
        *,
        kind: str,
        metric: str,
        value: float,
        baseline: float,
        message: str,
        fingerprint: str,
        org_id: str | None = None,
    ) -> bool:
        """이상 이벤트 기록 — (org_id,fingerprint) UNIQUE로 24h+ 중복 억제.

        반환 True=신규 삽입, False=동일 fingerprint 이미 존재."""
        oid = orgctx.org_for_write(org_id)
        now = _iso(_now())
        try:
            with self.tx() as c:
                c.execute(
                    "INSERT INTO anomaly_events(id,org_id,kind,metric,value,"
                    "baseline,message,fingerprint,detected_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        uuid.uuid4().hex[:12],
                        oid,
                        kind[:40],
                        metric[:80],
                        float(value),
                        float(baseline),
                        message[:500],
                        fingerprint[:200],
                        now,
                    ),
                )
            return True
        except self._integrity_errors:
            return False
        except Exception as e:
            logger.warning("anomaly event insert failed kind=%s: %s", kind, e)
            return False

    def list_anomaly_events(self, days: int = 30, limit: int = 100) -> list[dict[str, Any]]:
        days = max(1, min(int(days), 365))
        cutoff = _iso(_now() - timedelta(days=days))
        frag, params = _org_filter()
        rows = self._conn.execute(
            "SELECT id,org_id,kind,metric,value,baseline,message,fingerprint,detected_at"  # noqa: S608
            " FROM anomaly_events WHERE detected_at >= ?"
            + frag
            + " ORDER BY detected_at DESC LIMIT ?",
            (cutoff, *params, _clamp_limit(limit, 100, 500)),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- US1: run 타임라인 ----
    def run_timeline(self, run_id: str) -> list[dict[str, Any]]:
        """run의 stage_runs + stage 단위 cost_events 합산.

        cost_events는 stage_run_id로 조인 — 이벤트가 없는 stage는
        cost_usd=None. error_class는 저장 컬럼 우선, 없으면 분류기 적용."""
        from ..services.error_taxonomy import classify_error

        frag, params = _org_filter("r")
        rows = self._conn.execute(
            "SELECT sr.id, sr.run_id, sr.stage_key, sr.attempt, sr.status,"  # noqa: S608
            " sr.model, sr.prompt_version, sr.tokens_in, sr.tokens_out,"
            " sr.started_at, sr.finished_at, sr.error, sr.error_class,"
            " sr.input, sr.output,"
            " (SELECT COALESCE(SUM(ce.cost_microusd),0) FROM cost_events ce"
            "  WHERE ce.stage_run_id = sr.id) AS cost_mu,"
            " (SELECT COUNT(*) FROM cost_events ce"
            "  WHERE ce.stage_run_id = sr.id) AS cost_n"
            " FROM stage_runs sr JOIN runs r ON r.id = sr.run_id"
            " WHERE sr.run_id = ?" + frag + " ORDER BY sr.started_at, sr.attempt",
            (run_id, *params),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            started = _aware(_dt(r["started_at"]))
            finished = _aware(_dt(r["finished_at"]))
            duration_ms = None
            if started and finished:
                duration_ms = int(max(0.0, (finished - started).total_seconds() * 1000))
            error = str(r["error"] or "")
            eclass = str(r["error_class"] or "") or (classify_error(error) if error else "")
            cost_n = int(r["cost_n"] or 0)
            cost_mu = r["cost_mu"]
            out.append(
                {
                    "stage_run_id": r["id"],
                    "stage_key": r["stage_key"],
                    "attempt": int(r["attempt"] or 0),
                    "status": r["status"],
                    "model": r["model"] or "",
                    "prompt_version": r["prompt_version"] or "",
                    "tokens_in": int(r["tokens_in"] or 0),
                    "tokens_out": int(r["tokens_out"] or 0),
                    "cost_usd": (
                        (int(cost_mu) / 1_000_000) if cost_n and cost_mu is not None else None
                    ),
                    "started_at": r["started_at"],
                    "finished_at": r["finished_at"],
                    "duration_ms": duration_ms,
                    "error": error,
                    "error_class": eclass,
                    "input_summary": _summary(_loads(r["input"])),
                    "output_summary": _summary(_loads(r["output"])),
                }
            )
        return out

    # ---- US2: 비용 그룹 집계 ----
    _COST_GROUPS = ("model", "project", "workflow", "agent", "day")

    def observability_costs(self, days: int, group_by: str) -> dict[str, Any]:
        """cost_events 기간 집계 — group_by별 key/cost/tokens/calls/runs."""
        days = max(1, min(int(days), 365))
        group_by = group_by if group_by in self._COST_GROUPS else "model"
        cutoff = _iso(_now() - timedelta(days=days))
        frag, params = _org_filter("ce")
        if group_by == "day":
            key_expr = "substr(ce.occurred_at, 1, 10)"
        elif group_by == "model":
            key_expr = "COALESCE(NULLIF(ce.model,''), '(unknown)')"
        elif group_by == "project":
            key_expr = "COALESCE(NULLIF(r.project,''), '(none)')"
        elif group_by == "workflow":
            key_expr = "COALESCE(NULLIF(r.workflow_key,''), '(unknown)')"
        else:  # agent — cost_events.agent_id 우선, runs.agent_id 폴백
            key_expr = "COALESCE(NULLIF(ce.agent_id,''), NULLIF(r.agent_id,''), 'unknown')"
        # project/workflow/agent는 runs 조인이 필요, model/day는 불필요하지만
        # LEFT JOIN이라 비용이 없는 편이 정확하다 — 통일해 항상 조인한다.
        run_expr = "COALESCE(sr.run_id, ce.run_id)"
        # key_expr/run_expr/frag는 모두 내부 고정 리터럴 + 바운드 파라미터
        sql = (
            f"SELECT {key_expr} AS k,"  # noqa: S608
            " COALESCE(SUM(ce.cost_microusd),0) AS mu,"
            " COALESCE(SUM(ce.input_tokens),0) AS ti,"
            " COALESCE(SUM(ce.output_tokens),0) AS to_,"
            " COUNT(*) AS calls,"
            f" COUNT(DISTINCT {run_expr}) AS runs"
            " FROM cost_events ce"
            " LEFT JOIN stage_runs sr ON sr.id = ce.stage_run_id"
            f" LEFT JOIN runs r ON r.id = {run_expr}"
            f" WHERE ce.occurred_at >= ?{frag}"
            f" GROUP BY {key_expr}"
            " ORDER BY mu DESC, k LIMIT 200"
        )
        rows = self._conn.execute(sql, (cutoff, *params)).fetchall()
        return {
            "group_by": group_by,
            "days": days,
            "items": [
                {
                    "key": str(r["k"] or ""),
                    "cost_usd": int(r["mu"] or 0) / 1_000_000,
                    "tokens_in": int(r["ti"] or 0),
                    "tokens_out": int(r["to_"] or 0),
                    "calls": int(r["calls"] or 0),
                    "runs": int(r["runs"] or 0),
                }
                for r in rows
            ],
        }

    # ---- US2: 사용량 집계 ----
    def observability_usage(self, days: int) -> dict[str, Any]:
        """llm_usage(호출 단위) + cost_events(비용) + agent_sessions(세션)."""
        days = max(1, min(int(days), 365))
        cutoff = _iso(_now() - timedelta(days=days))

        # 모델별 — llm_usage 호출 단위 집계. cost_events는 stage_run 단위
        # 원장이라 같은 사용량을 이중 계상할 수 있어 모델/토큰은 llm_usage만 쓴다
        # (cost_ledger.py의 "두 경로는 별개" 주석 참조). 비용은 daily에서만
        # cost_events 원장을 사용한다.
        frag_u, params_u = _org_filter()
        urows = self._conn.execute(
            "SELECT model, COUNT(*) AS calls,"  # noqa: S608
            " COALESCE(SUM(tokens_in),0) AS ti, COALESCE(SUM(tokens_out),0) AS to_"
            " FROM llm_usage WHERE created_at >= ?"
            + frag_u
            + " GROUP BY model ORDER BY ti + to_ DESC LIMIT 100",
            (cutoff, *params_u),
        ).fetchall()
        models = {
            str(r["model"] or "(unknown)"): {
                "calls": int(r["calls"] or 0),
                "tokens_in": int(r["ti"] or 0),
                "tokens_out": int(r["to_"] or 0),
            }
            for r in urows
        }

        # 일별 — llm_usage 토큰 + cost_events 비용을 날짜 키로 병합.
        daily: dict[str, dict[str, Any]] = {}
        drows = self._conn.execute(
            "SELECT substr(created_at,1,10) AS d,"  # noqa: S608
            " COALESCE(SUM(tokens_in),0) AS ti, COALESCE(SUM(tokens_out),0) AS to_"
            " FROM llm_usage WHERE created_at >= ?" + frag_u + " GROUP BY d",
            (cutoff, *params_u),
        ).fetchall()
        for r in drows:
            daily.setdefault(
                str(r["d"]),
                {"date": str(r["d"]), "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0},
            )
            daily[str(r["d"])]["tokens_in"] += int(r["ti"] or 0)
            daily[str(r["d"])]["tokens_out"] += int(r["to_"] or 0)
        frag_c, params_c = _org_filter("ce")
        crows = self._conn.execute(
            "SELECT substr(ce.occurred_at,1,10) AS d,"  # noqa: S608
            " COALESCE(SUM(ce.cost_microusd),0) AS mu"
            " FROM cost_events ce WHERE ce.occurred_at >= ?" + frag_c + " GROUP BY d",
            (cutoff, *params_c),
        ).fetchall()
        for r in crows:
            daily.setdefault(
                str(r["d"]),
                {"date": str(r["d"]), "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0},
            )
            daily[str(r["d"])]["cost_usd"] += int(r["mu"] or 0) / 1_000_000

        # 세션 — agent_sessions를 tool별로. 기간은 last_event_at/updated_at
        # 최신값 기준(세션 자체는 기간 개념이 없어 최근 활동 세션만 집계).
        frag_s, params_s = _org_filter()
        srows = self._conn.execute(
            "SELECT tool, COUNT(*) AS sessions,"  # noqa: S608
            " COALESCE(SUM(COALESCE(tokens_in,0)),0) AS ti,"
            " COALESCE(SUM(COALESCE(tokens_out,0)),0) AS to_"
            " FROM agent_sessions"
            " WHERE COALESCE(last_event_at, updated_at, created_at) >= ?"
            + frag_s
            + " GROUP BY tool ORDER BY sessions DESC",
            (cutoff, *params_s),
        ).fetchall()
        by_tool = {
            str(r["tool"] or ""): {
                "sessions": int(r["sessions"] or 0),
                "tokens_in": int(r["ti"] or 0),
                "tokens_out": int(r["to_"] or 0),
            }
            for r in srows
        }
        sessions = {
            "total": sum(v["sessions"] for v in by_tool.values()),
            "by_tool": by_tool,
        }

        return {
            "days": days,
            "models": models,
            "daily": [daily[k] for k in sorted(daily)],
            "sessions": sessions,
        }

    # ---- US3: 에러 분류 집계 ----
    def observability_errors(self, days: int) -> dict[str, Any]:
        """stage_runs.error + runs.error를 error_class별로 집계.

        저장된 error_class 컬럼을 우선 쓰고, 비어 있는 레거시 행은
        services.error_taxonomy.classify_error로 조회 시점 분류한다."""
        from ..services.error_taxonomy import classify_error

        days = max(1, min(int(days), 365))
        cutoff = _iso(_now() - timedelta(days=days))
        items: list[dict[str, Any]] = []

        frag_r, params_r = _org_filter("r")
        for r in self._conn.execute(
            "SELECT sr.run_id, sr.stage_key, sr.error, sr.error_class,"  # noqa: S608
            " sr.finished_at AS at FROM stage_runs sr"
            " JOIN runs r ON r.id = sr.run_id"
            " WHERE sr.error <> '' AND sr.finished_at >= ?"
            + frag_r
            + " ORDER BY sr.finished_at DESC LIMIT ?",
            (cutoff, *params_r, _ERROR_QUERY_LIMIT),
        ).fetchall():
            err = str(r["error"] or "")
            eclass = str(r["error_class"] or "") or classify_error(err)
            items.append(
                {
                    "run_id": str(r["run_id"] or ""),
                    "stage_key": str(r["stage_key"] or ""),
                    "error": err[:300],
                    "error_class": eclass,
                    "at": str(r["at"] or ""),
                }
            )

        frag_run, params_run = _org_filter()
        for r in self._conn.execute(
            "SELECT id AS run_id, error, error_class, updated_at AS at"  # noqa: S608
            " FROM runs WHERE error <> '' AND updated_at >= ?"
            + frag_run
            + " ORDER BY updated_at DESC LIMIT ?",
            (cutoff, *params_run, _ERROR_QUERY_LIMIT),
        ).fetchall():
            err = str(r["error"] or "")
            eclass = str(r["error_class"] or "") or classify_error(err)
            items.append(
                {
                    "run_id": str(r["run_id"] or ""),
                    "stage_key": "",
                    "error": err[:300],
                    "error_class": eclass,
                    "at": str(r["at"] or ""),
                }
            )

        items.sort(key=lambda x: x["at"], reverse=True)
        total = len(items)
        by_class: dict[str, dict[str, Any]] = {}
        for it in items:
            b = by_class.setdefault(
                it["error_class"],
                {"error_class": it["error_class"], "count": 0, "last_seen": "", "samples": []},
            )
            b["count"] += 1
            if it["at"] > b["last_seen"]:
                b["last_seen"] = it["at"]
            if len(b["samples"]) < 5:
                b["samples"].append(
                    {
                        "run_id": it["run_id"],
                        "stage_key": it["stage_key"],
                        "error": it["error"],
                        "error_class": it["error_class"],
                        "at": it["at"],
                    }
                )
        out = sorted(by_class.values(), key=lambda x: -x["count"])
        for b in out:
            b["pct"] = round(b["count"] / total * 100, 1) if total else 0.0
        return {"days": days, "total": total, "by_class": out}

    # ---- US5: 승인 인지 ----
    def observability_approvals(self) -> dict[str, Any]:
        """승인 대기 큐 + 대기 시간 통계 + 최근 24h 결정 통계.

        waiting_since 근사: budget 승인 대기는 metadata.budget_approval.
        requested_at, 스테이지 승인은 runs.updated_at(=waiting 전이 시각)
        — 마지막 활동 시각이라 실제 대기 시작보다 늦을 수 있다."""
        now = _now()
        frag, params = _org_filter()
        rows = self._conn.execute(
            "SELECT id, workflow_key, project, agent_id, created_by,"  # noqa: S608
            " status, metadata, updated_at, created_at FROM runs"
            " WHERE status = 'waiting_approval'" + frag,
            tuple(params),
        ).fetchall()
        waiting: list[dict[str, Any]] = []
        waited_total = 0.0
        oldest = 0.0
        run_ids = [str(r["id"]) for r in rows]
        # 대기 중 run의 현재 running stage_key 일괄 조회.
        stage_by_run: dict[str, str] = {}
        if run_ids:
            marks = ",".join("?" for _ in run_ids)
            srows = self._conn.execute(
                "SELECT run_id, stage_key FROM stage_runs sr"  # noqa: S608
                " JOIN runs r ON r.id = sr.run_id"
                " WHERE sr.status = 'running' AND sr.run_id IN (" + marks + ")"
                " ORDER BY sr.started_at",
                tuple(run_ids),
            ).fetchall()
            for sr in srows:
                stage_by_run.setdefault(str(sr["run_id"]), str(sr["stage_key"] or ""))
        for r in rows:
            meta = _loads(r["metadata"])
            marker = meta.get("budget_approval") if isinstance(meta, dict) else None
            since = ""
            if isinstance(marker, dict) and marker.get("requested_at"):
                since = str(marker["requested_at"])
            if not since:
                since = str(r["updated_at"] or r["created_at"] or "")
            dt = _aware(_dt(since))
            waited = max(0.0, (now - dt).total_seconds()) if dt else 0.0
            waited_total += waited
            oldest = max(oldest, waited)
            waiting.append(
                {
                    "run_id": str(r["id"]),
                    "workflow": str(r["workflow_key"] or ""),
                    "project": str(r["project"] or ""),
                    "stage_key": stage_by_run.get(str(r["id"]), ""),
                    "waiting_since": since,
                    "waited_s": int(waited),
                    "requested_by": str(r["created_by"] or ""),
                }
            )
        waiting.sort(key=lambda x: -x["waited_s"])

        # 최근 24h 결정 — approvals는 org_id가 없어 runs 조인으로 스코프.
        cutoff = _iso(now - timedelta(hours=24))
        drows = self._conn.execute(
            "SELECT a.run_id, a.stage_key, a.decided_at, r.org_id"  # noqa: S608
            " FROM approvals a JOIN runs r ON r.id = a.run_id"
            " WHERE a.decided_at >= ?" + frag.replace("org_id", "r.org_id"),
            (cutoff, *params),
        ).fetchall()
        decided_24h = len(drows)
        # 평균 결정 소요 — run별 waiting_approval audit 시각 → approval 결정 시각.
        # audit이 없는 레거시 run은 run.created_at을 대기 시작으로 근사한다.
        decision_durations: list[float] = []
        decided_run_ids = sorted({str(r["run_id"]) for r in drows})
        wait_start: dict[str, datetime] = {}
        if decided_run_ids:
            marks = ",".join("?" for _ in decided_run_ids)
            arows = self._conn.execute(
                "SELECT run_id, object_ref, created_at FROM audit_events"  # noqa: S608
                " WHERE action = 'waiting_approval' AND run_id IN (" + marks + ")"
                " ORDER BY created_at",
                tuple(decided_run_ids),
            ).fetchall()
            for a in arows:
                key = f"{a['run_id']}|{a['object_ref']}"
                wait_start.setdefault(key, _aware(_dt(a["created_at"])) or now)
            crows = self._conn.execute(
                "SELECT id, created_at FROM runs WHERE id IN (" + marks + ")",  # noqa: S608
                tuple(decided_run_ids),
            ).fetchall()
            for c in crows:
                wait_start.setdefault(f"{c['id']}|", _aware(_dt(c["created_at"])) or now)
        for r in drows:
            decided_dt = _aware(_dt(r["decided_at"]))
            if decided_dt is None:
                continue
            key_stage = f"{r['run_id']}|{r['stage_key']}"
            key_run = f"{r['run_id']}|"
            start_dt = wait_start.get(key_stage) or wait_start.get(key_run)
            if start_dt is None:
                continue
            decision_durations.append(max(0.0, (decided_dt - start_dt).total_seconds()))
        avg_decision = (
            round(sum(decision_durations) / len(decision_durations), 1)
            if decision_durations
            else None
        )

        return {
            "waiting": waiting,
            "stats": {
                "waiting": len(waiting),
                "oldest_wait_s": int(oldest),
                "total_wait_s": int(waited_total),
                "decided_24h": decided_24h,
                "avg_decision_s": avg_decision,
            },
        }

    # ---- US7: 이상 탐지 스윕용 원시 집계 ----
    def daily_cost_microusd(self, since_iso: str, until_iso: str = "") -> int:
        """org ctx 기준 occurred_at ∈ [since, until) cost 합계 (priced만)."""
        frag, params = _org_filter("ce")
        sql = (
            "SELECT COALESCE(SUM(ce.cost_microusd),0) AS s FROM cost_events ce"  # noqa: S608
            " WHERE ce.cost_microusd IS NOT NULL AND ce.occurred_at >= ?" + frag
        )
        qparams: list[Any] = [since_iso, *params]
        if until_iso:
            sql += " AND ce.occurred_at < ?"
            qparams.append(until_iso)
        row = self._conn.execute(sql, qparams).fetchone()
        try:
            return int(row["s"] or 0) if row is not None else 0
        except (TypeError, ValueError):
            return 0

    def failed_stage_count(self, since_iso: str, until_iso: str = "") -> int:
        """org ctx 기준 기간 내 failed stage_runs 수."""
        frag, params = _org_filter("r")
        sql = (
            "SELECT COUNT(*) AS n FROM stage_runs sr"  # noqa: S608
            " JOIN runs r ON r.id = sr.run_id"
            " WHERE sr.status = 'failed' AND sr.finished_at >= ?" + frag
        )
        qparams: list[Any] = [since_iso, *params]
        if until_iso:
            sql += " AND sr.finished_at < ?"
            qparams.append(until_iso)
        row = self._conn.execute(sql, qparams).fetchone()
        try:
            return int(row["n"] or 0) if row is not None else 0
        except (TypeError, ValueError):
            return 0

    # ================= spec 011 US1 — 관제센터 집계 =================
    # notifications/inbox·runs/stage_runs·agent_sessions/llm_usage 기반.
    # 없는 컬럼(severity/progress/heartbeat 전용 필드 등)은 추정하지 않고
    # null 또는 필드 생략으로 처리한다.

    _FUNNEL_STAGE_LIMIT = 50000  # 윈도우 내 stage_run 스캔 상한(초과 시 하한 근사)
    _AGENTS_LIVE_LIMIT = 100
    _SWARM_MAX_RUNS = 25  # days 모드에서 그래프에 포함할 최근 run 수
    _SWARM_STAGE_ROW_LIMIT = 2000  # _swarm_stage_rows 총 스캔 상한(메모리 바운드)
    _SWARM_NODE_LIMIT = 100

    # agent_sessions.status(외부 브릿지 어휘) → 관제센터 §5.4 상태 집합.
    # 스키마에 planning/thinking/executing_tool을 생성하는 경로는 없지만
    # 브릿지가 그대로 보내면 패스스루로 보존한다. 미지 값은 idle로 수렴.
    _SESSION_STATUS_MAP = {
        "working": "running",
        "run": "running",
        "running": "running",
        "decision": "waiting",
        "waiting": "waiting",
        "waiting_approval": "waiting",
        "rest": "idle",
        "idle": "idle",
        "ended": "completed",
        "completed": "completed",
        "done": "completed",
        "error": "failed",
        "failed": "failed",
        "paused": "paused",
        "blocked": "blocked",
        "planning": "planning",
        "thinking": "thinking",
        "executing_tool": "executing_tool",
    }

    # stage_run.status 중 "실제 실행에 진입"한 상태 — pending/skipped는 제외.
    _EXECUTING_STAGE_STATUSES = ("running", "succeeded", "failed")

    def observability_notifications(self, days: int) -> dict[str, Any]:
        """알림함 집계 — inbox_items kind='notification' 기준.

        open=미읽음(read=0), resolved=읽음(read=1). inbox_items에는
        severity 컬럼이 없어 by_severity는 반환하지 않는다(스펙 허용).
        notifications 테이블 행은 GET /inbox의 sync_notifications_to_inbox
        미러링을 통해 kind='notification'으로 반영되므로 여기서 별도
        집계하면 이중 계산된다 — 알림함 화면과 동일한 소스를 쓴다."""
        days = max(1, min(int(days), 365))
        # notifications → inbox_items 미러링은 GET /inbox에서만 동기화되므로
        # 여기서도 보장한다(멱등 — dedupe_key=notif:{id}).
        self.sync_notifications_to_inbox()
        now = _now()
        cutoff = _iso(now - timedelta(days=days))
        frag, params = _org_filter()
        conds = ["kind='notification'", "created_at >= ?"]
        if frag:
            conds.append(frag.strip().removeprefix("AND").strip())
        rows = self._conn.execute(
            "SELECT substr(created_at,1,10) AS d, COUNT(*) AS n,"  # noqa: S608
            " COALESCE(SUM(CASE WHEN read=0 THEN 1 ELSE 0 END),0) AS open_n,"
            " COALESCE(SUM(CASE WHEN read=1 THEN 1 ELSE 0 END),0) AS resolved_n"
            " FROM inbox_items WHERE " + " AND ".join(conds) + " GROUP BY d",
            (cutoff, *params),
        ).fetchall()
        by_day = {str(r["d"]): r for r in rows}
        total = open_n = resolved_n = 0
        series: list[dict[str, Any]] = []
        # 최근 days일(UTC 일 단위) 버킷을 0으로 채운 뒤 실측값을 덮어쓴다.
        start_day = (now - timedelta(days=days - 1)).date()
        for i in range(days):
            d = (start_day + timedelta(days=i)).isoformat()
            r = by_day.get(d)
            n = int(r["n"] or 0) if r else 0
            total += n
            if r:
                open_n += int(r["open_n"] or 0)
                resolved_n += int(r["resolved_n"] or 0)
            series.append({"t": f"{d}T00:00:00Z", "count": n})
        frag_r, params_r = _org_filter()
        conds_r = ["kind='notification'"]
        if frag_r:
            conds_r.append(frag_r.strip().removeprefix("AND").strip())
        recent_rows = self._conn.execute(
            "SELECT id,title,created_at,read FROM inbox_items"  # noqa: S608
            " WHERE " + " AND ".join(conds_r) + " ORDER BY created_at DESC LIMIT 20",
            tuple(params_r),
        ).fetchall()
        recent = [
            {
                "id": str(r["id"]),
                "title": str(r["title"] or ""),
                "created_at": str(r["created_at"] or ""),
                "status": "open" if not int(r["read"] or 0) else "resolved",
            }
            for r in recent_rows
        ]
        return {
            "days": days,
            "total": total,
            "open": open_n,
            "resolved": resolved_n,
            "series": series,
            "recent": recent,
        }

    def observability_funnel(self, days: int, workflow: str = "") -> dict[str, Any]:
        """실행 퍼널 — runs/stage_runs에서 단계별 도달 run 수를 유도.

        - received: 윈도우 내 생성된 run 수
        - planned: entry가 아닌 stage_run이 1개 이상 있는 run 수
          (워크플로 spec의 entry 단계를 제외 — spec 미해석 시 모든
          stage_key를 비엔트리로 간주하는 보수적 폴백)
        - assigned: 비엔트리 stage_run이 실제 실행 상태
          (running/succeeded/failed — pending/skipped 제외)인 run 수
        - executed: status='completed'로 성공 종료한 run 수

        workflow 지정 시 해당 workflow_key로 한정한다."""
        days = max(1, min(int(days), 365))
        cutoff = _iso(_now() - timedelta(days=days))
        wf = (workflow or "").strip()[:100]
        frag, params = _org_filter("r")
        wf_frag = " AND r.workflow_key=?" if wf else ""
        wf_params: list[Any] = [wf] if wf else []

        def _run_count(extra: str = "") -> int:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM runs r"  # noqa: S608
                " WHERE r.created_at >= ?" + frag + wf_frag + extra,
                (cutoff, *params, *wf_params),
            ).fetchone()
            try:
                return int(row["n"] or 0) if row is not None else 0
            except (TypeError, ValueError):
                return 0

        received = _run_count()
        executed = _run_count(" AND r.status='completed'")

        rows = self._conn.execute(
            "SELECT sr.run_id, sr.stage_key, sr.status, r.workflow_key"  # noqa: S608
            " FROM stage_runs sr JOIN runs r ON r.id = sr.run_id"
            " WHERE r.created_at >= ?" + frag + wf_frag + " LIMIT ?",
            (cutoff, *params, *wf_params, self._FUNNEL_STAGE_LIMIT),
        ).fetchall()

        # workflow_key별 entry stage_key — spec 003 커스텀 spec도 조직
        # 컨텍스트로 해석된다. 미해석("" )은 비엔트리 판정에서 제외하지 않는다.
        from ..workflows.resolver import resolve_workflow_spec

        entry_by_wf: dict[str, str] = {}

        def _entry(wf_key: str) -> str:
            if wf_key not in entry_by_wf:
                try:
                    spec = resolve_workflow_spec(self, wf_key)
                    entry_by_wf[wf_key] = spec.entry if spec is not None else ""
                except Exception:
                    entry_by_wf[wf_key] = ""
            return entry_by_wf[wf_key]

        planned: set[str] = set()
        assigned: set[str] = set()
        for r in rows:
            rid = str(r["run_id"] or "")
            entry = _entry(str(r["workflow_key"] or ""))
            if entry and str(r["stage_key"] or "") == entry:
                continue  # entry stage — 계획 수립 이전 단계
            planned.add(rid)
            if str(r["status"] or "") in self._EXECUTING_STAGE_STATUSES:
                assigned.add(rid)

        return {
            "days": days,
            "workflow": wf,
            "stages": [
                {"key": "received", "label": "요청 수신", "count": received},
                {"key": "planned", "label": "계획 수립", "count": len(planned)},
                {"key": "assigned", "label": "작업 분배", "count": len(assigned)},
                {"key": "executed", "label": "실행", "count": executed},
            ],
        }

    @staticmethod
    def _live_agent_status(raw: Any) -> str:
        """agent_sessions.status → §5.4 상태 집합. 미지 값은 idle."""
        key = str(raw or "").strip().lower()
        return ObservabilityStoreMixin._SESSION_STATUS_MAP.get(key, "idle")

    def observability_agents_live(self) -> dict[str, Any]:
        """라이브 에이전트/세션 카드 — agent_sessions 최신 활동 순.

        agent_sessions의 org 필터 규약을 따른다('' 공유 행은 전 조직에
        노출 — 브릿지가 env 키로 푸시하는 플랫폼 세션). 모델/진행률/
        tool_calls/sub_agents/비용은 spec 012 US1에서 추가된
        agent_sessions 컬럼(model/progress_pct/cost_usd/tool_calls/
        sub_agents)을 그대로 노출한다 — 브릿지가 보내지 않은 세션은
        null 유지(추정 금지).
        last_heartbeat_at은 전용 컬럼이 없어 last_event_at(브릿지가
        보낸 마지막 이벤트 시각)을 사용한다."""
        oid = orgctx.current_org_id()
        frag = " WHERE (org_id=? OR org_id='')" if oid else ""
        params: list[Any] = [oid] if oid else []
        rows = self._conn.execute(
            "SELECT id,tool,session_key,project,title,status,doing,ask,"  # noqa: S608
            # spec 012 US1 — model/progress/cost/tool_calls/sub_agents는
            # agent_sessions 컬럼에서 읽는다. 미전송 세션은 null 유지
            # (추정 금지 — spec 수용 기준).
            " tokens_in,tokens_out,model,progress_pct,cost_usd,tool_calls,"
            " sub_agents,last_event_at,created_at,updated_at"
            " FROM agent_sessions"
            + frag
            + " ORDER BY COALESCE(last_event_at, updated_at, created_at) DESC"
            " LIMIT ?",
            (*params, self._AGENTS_LIVE_LIMIT),
        ).fetchall()
        now = _now()
        items: list[dict[str, Any]] = []
        for r in rows:
            created = _aware(_dt(r["created_at"]))
            elapsed = max(0, int((now - created).total_seconds())) if created else None
            ti, to_ = r["tokens_in"], r["tokens_out"]
            tokens = (int(ti or 0) + int(to_ or 0)) if (ti is not None or to_ is not None) else None
            heartbeat = str(r["last_event_at"] or r["updated_at"] or r["created_at"] or "")
            items.append(
                {
                    "id": f"{r['tool']}:{r['session_key']}",
                    "session_key": str(r["session_key"] or ""),
                    "name": str(r["title"] or r["session_key"] or r["tool"] or ""),
                    "role": str(r["tool"] or ""),
                    "model": str(r["model"]) if r["model"] is not None else None,
                    "status": self._live_agent_status(r["status"]),
                    "current_task": str(r["doing"] or ""),
                    "progress_pct": (
                        float(r["progress_pct"]) if r["progress_pct"] is not None else None
                    ),
                    "started_at": str(r["created_at"] or ""),
                    "elapsed_s": elapsed,
                    "tokens": tokens,
                    "cost_usd": float(r["cost_usd"]) if r["cost_usd"] is not None else None,
                    "tool_calls": (int(r["tool_calls"]) if r["tool_calls"] is not None else None),
                    "sub_agents": (int(r["sub_agents"]) if r["sub_agents"] is not None else None),
                    "last_heartbeat_at": heartbeat,
                    "project": str(r["project"] or ""),
                }
            )
        return {"count": len(items), "items": items}

    # ---- spec 011 US1: swarm 노드/엣지 ----
    def _swarm_run_row(self, run_id: str) -> dict[str, Any] | None:
        frag, params = _org_filter("r")
        row = self._conn.execute(
            "SELECT r.id, r.workflow_key, r.agent_id, r.status, r.project,"  # noqa: S608
            " r.created_at, r.updated_at FROM runs r WHERE r.id=?" + frag,
            (run_id, *params),
        ).fetchone()
        return dict(row) if row else None

    def _swarm_stage_rows(self, run_ids: list[str]) -> dict[str, list[Any]]:
        """run_id → stage_runs 행(실행 순 정렬). stage_runs는 org_id가
        없어 runs 조인으로 조직을 판정한다."""
        if not run_ids:
            return {}
        frag, params = _org_filter("r")
        marks = ",".join("?" for _ in run_ids)
        rows = self._conn.execute(
            "SELECT sr.id, sr.run_id, sr.stage_key, sr.attempt, sr.status,"  # noqa: S608
            " sr.model, sr.tokens_in, sr.tokens_out, sr.started_at,"
            " sr.finished_at, sr.error, r.workflow_key"
            " FROM stage_runs sr JOIN runs r ON r.id = sr.run_id"
            " WHERE sr.run_id IN ("
            + marks
            + ")"
            + frag
            + " ORDER BY sr.run_id, sr.started_at, sr.attempt LIMIT ?",
            (*run_ids, *params, self._SWARM_STAGE_ROW_LIMIT),
        ).fetchall()
        out: dict[str, list[Any]] = {}
        for r in rows:
            out.setdefault(str(r["run_id"]), []).append(r)
        return out

    def _swarm_agent_names(self, workflow_key: str) -> dict[str, str]:
        """workflow spec의 stage_key → agent(AgentSpec 키) 매핑.
        spec 미해석 시 빈 맵 — node.agent는 빈 문자열."""
        try:
            from ..workflows.resolver import resolve_workflow_spec

            spec = resolve_workflow_spec(self, workflow_key)
        except Exception:
            return {}
        if spec is None:
            return {}
        return {s.key: (s.agent or "") for s in spec.stages if s.agent}

    @staticmethod
    def _swarm_latency_ms(started: Any, finished: Any) -> int | None:
        s, f = _aware(_dt(started)), _aware(_dt(finished))
        if s is None or f is None:
            return None
        return int(max(0.0, (f - s).total_seconds() * 1000))

    def _swarm_run_node(self, run: dict[str, Any]) -> dict[str, Any]:
        agent_label = ""
        aid = str(run.get("agent_id") or "")
        if aid:
            try:
                agent = self.get_agent(aid)
                agent_label = str((agent or {}).get("name") or aid)
            except Exception:
                agent_label = aid
        created = _aware(_dt(run.get("created_at")))
        updated = _aware(_dt(run.get("updated_at")))
        duration = (
            int((updated - created).total_seconds() * 1000)
            if created and updated and updated >= created
            else None
        )
        status = str(run.get("status") or "")
        return {
            "id": f"run:{run['id']}",
            "run_id": str(run["id"]),
            "label": str(run.get("workflow_key") or run["id"]),
            "kind": "run",
            "workflow": str(run.get("workflow_key") or ""),
            "agent": agent_label,
            "status": status,
            "progress_pct": 100 if status == "completed" else None,
            "tokens": None,
            "latency_ms": duration,
            "error_count": 0,
        }

    def _swarm_stage_node(self, r: Any, agent_by_stage: dict[str, str]) -> dict[str, Any]:
        stage_key = str(r["stage_key"] or "")
        status = str(r["status"] or "")
        ti, to_ = int(r["tokens_in"] or 0), int(r["tokens_out"] or 0)
        return {
            "id": f"stage:{r['id']}",
            "run_id": str(r["run_id"]),
            "stage_key": stage_key,
            "attempt": int(r["attempt"] or 0),
            "label": stage_key,
            "kind": "stage",
            "agent": agent_by_stage.get(stage_key) or "",
            "status": status,
            # 진행률은 저장되지 않는다 — 성공 종료만 100으로 표시하고
            # 그 외는 null(실행 중 진행률을 추정하지 않는다).
            "progress_pct": 100 if status == "succeeded" else None,
            "tokens": ti + to_,
            "latency_ms": self._swarm_latency_ms(r["started_at"], r["finished_at"]),
            "error_count": 1 if str(r["error"] or "") else 0,
        }

    def _swarm_run_graph(
        self,
        run: dict[str, Any],
        stage_rows: list[Any],
        agent_by_stage: dict[str, str],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """run 1건의 노드/엣드 — run 노드를 부모로, stage_run을 실행
        순(started_at, attempt)으로 직렬 연결하고 각 stage_key의 첫
        실행에 run→stage 'spawn' 엣지를 둔다."""
        nodes = [self._swarm_run_node(run)]
        node_ids: list[str] = []
        for r in stage_rows:
            node = self._swarm_stage_node(r, agent_by_stage)
            nodes.append(node)
            node_ids.append(node["id"])
        edges: list[dict[str, Any]] = []
        run_node_id = nodes[0]["id"]
        seen_keys: set[str] = set()
        prev_id = ""
        for r, nid in zip(stage_rows, node_ids, strict=True):
            key = str(r["stage_key"] or "")
            if key not in seen_keys:
                seen_keys.add(key)
                edges.append({"source": run_node_id, "target": nid, "kind": "spawn"})
            if prev_id:
                edges.append({"source": prev_id, "target": nid, "kind": "sequence"})
            prev_id = nid
        return nodes, edges

    def observability_swarm(self, run_id: str = "", days: int = 7) -> dict[str, Any]:
        """노드/엣지 그래프 — run_id 지정 시 해당 run, 아니면 최근 활성 run.

        days 모드는 updated_at 기준 최근 _SWARM_MAX_RUNS개 run을 대상으로
        노드가 _SWARM_NODE_LIMIT을 넘으면 run 단위로 제외하고 나머지를
        '…' 클러스터 노드 1개로 요약한다(엣지는 포함된 노드 사이에만)."""
        run_id = (run_id or "").strip()
        if run_id:
            run = self._swarm_run_row(run_id)
            if run is None:
                return {"nodes": [], "edges": []}
            stage_map = self._swarm_stage_rows([run_id])
            stages = stage_map.get(run_id, [])
            # run 모드도 노드 상한 적용 — 초과분은 클러스터 노드로 요약
            truncated = 0
            if len(stages) + 1 > self._SWARM_NODE_LIMIT:
                truncated = len(stages) - (self._SWARM_NODE_LIMIT - 2)
                stages = stages[: self._SWARM_NODE_LIMIT - 2]
            agent_by_stage = self._swarm_agent_names(str(run.get("workflow_key") or ""))
            rn, re_ = self._swarm_run_graph(run, stages, agent_by_stage)
            if truncated:
                rn.append(
                    {
                        "id": "cluster:truncated",
                        "label": "…",
                        "kind": "cluster",
                        "count": truncated,
                        "status": "",
                    }
                )
            return {"nodes": rn, "edges": re_}

        days = max(1, min(int(days), 365))
        cutoff = _iso(_now() - timedelta(days=days))
        frag, params = _org_filter("r")
        runs = [
            dict(r)
            for r in self._conn.execute(
                "SELECT r.id, r.workflow_key, r.agent_id, r.status, r.project,"  # noqa: S608
                " r.created_at, r.updated_at FROM runs r"
                " WHERE r.updated_at >= ?" + frag + " ORDER BY r.updated_at DESC LIMIT ?",
                (cutoff, *params, self._SWARM_MAX_RUNS),
            ).fetchall()
        ]
        run_ids = [str(r["id"]) for r in runs]
        stage_map = self._swarm_stage_rows(run_ids)
        # workflow_key별 agent 매핑 캐시
        agent_cache: dict[str, dict[str, str]] = {}

        def _agents(wf_key: str) -> dict[str, str]:
            if wf_key not in agent_cache:
                agent_cache[wf_key] = self._swarm_agent_names(wf_key)
            return agent_cache[wf_key]

        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, Any]] = []
        overflow = 0
        for run in runs:
            rid = str(run["id"])
            stages = stage_map.get(rid, [])
            cost = 1 + len(stages)
            if len(nodes) + cost > self._SWARM_NODE_LIMIT:
                overflow += 1
                continue
            wf_key = str(run.get("workflow_key") or "")
            rn, re_ = self._swarm_run_graph(run, stages, _agents(wf_key))
            # 이미 방출된 노드 id와의 엣지 소스/타겟은 run 내부에 한정되므로 그대로 병합
            nodes.extend(rn)
            edges.extend(re_)
        if overflow:
            nodes.append(
                {
                    "id": "cluster:overflow",
                    "label": "…",
                    "kind": "cluster",
                    "count": overflow,
                    "status": "",
                }
            )
        return {"nodes": nodes, "edges": edges}

    # ---- spec 012 US2: Tool Call 통계 ----
    _TOOL_STATS_SCAN_LIMIT = 20000  # 윈도우 내 스팬 스캔 상한(초과 시 truncated)
    _TOOL_STATS_TOP_N = 50

    # 오류 판정 규칙은 span_error.span_attrs_is_error 단일 소스 —
    # 인제스트 시 external_spans.error_flag에 저장되고 여기서는 그 값을 읽는다.
    # 마이그레이션 이전 행은 error_flag=0(미판정)이라 오류로 세지 않는다.
    @classmethod
    def _span_attr_is_error(cls, attrs: dict[str, Any]) -> bool:
        """attributes dict → 오류 여부. 판정 규칙은 span_error 모듈 단일 소스."""
        from .span_error import span_attrs_is_error

        return span_attrs_is_error(attrs)

    def observability_tool_stats(self, days: int) -> dict[str, Any]:
        """external_spans 기반 Tool Call 통계 (spec 012 US2).

        name+kind별 count/error_count/avg_duration_ms, kind별 count,
        전체 error_rate. kind가 비어 있으면 '' 그룹으로 둔다(추정 금지).
        스캔 상한(_TOOL_STATS_SCAN_LIMIT) 초과 시 truncated=True —
        집계는 스캔된 행 기준 하한 근사다."""
        days = max(1, min(int(days), 365))
        cutoff = _iso(_now() - timedelta(days=days))
        frag, params = _org_filter()
        # attributes는 행당 수백KB까지 커질 수 있어 SELECT하지 않는다 —
        # 오류 여부는 인제스트 시 계산된 error_flag를 사용(기존 행은 0).
        # LIMIT+1로 정확한 truncated 판정.
        rows = self._conn.execute(
            "SELECT name,kind,started_at,ended_at,error_flag"  # noqa: S608
            " FROM external_spans WHERE started_at >= ?"
            + frag
            + " ORDER BY started_at DESC LIMIT ?",
            (cutoff, *params, self._TOOL_STATS_SCAN_LIMIT + 1),
        ).fetchall()
        truncated = len(rows) > self._TOOL_STATS_SCAN_LIMIT
        rows = rows[: self._TOOL_STATS_SCAN_LIMIT]

        by_name: dict[tuple[str, str], dict[str, Any]] = {}
        by_kind: dict[str, int] = {}
        total_calls = 0
        total_errors = 0
        for r in rows:
            name = str(r["name"] or "")
            kind = str(r["kind"] or "")
            is_err = bool(int(r["error_flag"] or 0))
            dur_ms: float | None = None
            s, e = _aware(_dt(r["started_at"])), _aware(_dt(r["ended_at"]))
            if s is not None and e is not None:
                dur_ms = max(0.0, (e - s).total_seconds() * 1000)

            total_calls += 1
            total_errors += 1 if is_err else 0
            by_kind[kind] = by_kind.get(kind, 0) + 1
            b = by_name.setdefault(
                (name, kind), {"count": 0, "error_count": 0, "dur_sum": 0.0, "dur_n": 0}
            )
            b["count"] += 1
            b["error_count"] += 1 if is_err else 0
            if dur_ms is not None:
                b["dur_sum"] += dur_ms
                b["dur_n"] += 1

        def _avg(b: dict[str, Any]) -> float | None:
            return round(b["dur_sum"] / b["dur_n"], 1) if b["dur_n"] else None

        ranked = sorted(by_name.items(), key=lambda kv: (-kv[1]["count"], kv[0][0], kv[0][1]))
        items: list[dict[str, Any]] = []
        rest_count = rest_err = 0
        rest_dur = 0.0
        rest_dur_n = 0
        for (name, kind), b in ranked[: self._TOOL_STATS_TOP_N]:
            items.append(
                {
                    "name": name,
                    "kind": kind,
                    "count": b["count"],
                    "error_count": b["error_count"],
                    "avg_duration_ms": _avg(b),
                }
            )
        for _key, b in ranked[self._TOOL_STATS_TOP_N :]:
            rest_count += b["count"]
            rest_err += b["error_count"]
            rest_dur += b["dur_sum"]
            rest_dur_n += b["dur_n"]
        if rest_count:
            items.append(
                {
                    "name": "기타",
                    "kind": "",
                    "count": rest_count,
                    "error_count": rest_err,
                    "avg_duration_ms": round(rest_dur / rest_dur_n, 1) if rest_dur_n else None,
                }
            )
        return {
            "days": days,
            "total_calls": total_calls,
            "by_name": items,
            "by_kind": [
                {"kind": k, "count": by_kind[k]}
                for k in sorted(by_kind, key=lambda k: (-by_kind[k], k))
            ],
            "error_rate": round(total_errors / total_calls, 4) if total_calls else 0.0,
            "truncated": truncated,
        }
