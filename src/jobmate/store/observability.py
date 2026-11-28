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
