# mypy: disable-error-code="attr-defined"
"""Store mixin — LLM 사용량·워크플로 스케줄·run/LLM 집계 메서드."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from typing import Any

from . import orgctx
from .util import _dt, _iso, _loads, _now


def _org_filter(alias: str = "") -> tuple[str, list[Any]]:
    """ctx 조직이 있으면 ` AND [alias.]org_id=?` 조건 — ctx=''는 무필터."""
    oid = orgctx.current_org_id()
    if not oid:
        return "", []
    col = f"{alias}.org_id" if alias else "org_id"
    return f" AND {col}=?", [oid]


class UsageStoreMixin:
    # ---- llm_usage (LLM 토큰 사용량) ----
    def record_llm_usage(
        self, model: str, tokens_in: int, tokens_out: int, org_id: str | None = None
    ) -> None:
        """LLM 호출 1건의 토큰 사용량 기록. 프롬프트/응답 본문은 저장하지 않는다."""
        with self.tx() as c:
            c.execute(
                "INSERT INTO llm_usage(id,model,tokens_in,tokens_out,created_at,org_id)"
                " VALUES(?,?,?,?,?,?)",
                (
                    uuid.uuid4().hex[:12],
                    model,
                    int(tokens_in or 0),
                    int(tokens_out or 0),
                    _iso(_now()),
                    orgctx.org_for_write(org_id),
                ),
            )

    def llm_usage_summary(self, days: int = 7) -> dict[str, Any]:
        """최근 days일 사용량 — 모델별 합계(models) + 일별 시계열(daily)."""
        days = max(1, min(int(days), 365))
        cutoff = _iso(_now() - timedelta(days=days))
        frag, params = _org_filter()
        rows = self._conn.execute(
            "SELECT model,tokens_in,tokens_out,created_at FROM llm_usage"  # noqa: S608
            " WHERE created_at >= ?" + frag,
            (cutoff, *params),
        ).fetchall()
        models: dict[str, dict[str, int]] = {}
        daily: dict[str, dict[str, int]] = {}
        for r in rows:
            m = models.setdefault(r["model"], {"calls": 0, "tokens_in": 0, "tokens_out": 0})
            m["calls"] += 1
            m["tokens_in"] += int(r["tokens_in"] or 0)
            m["tokens_out"] += int(r["tokens_out"] or 0)
            day = (r["created_at"] or "")[:10]
            d = daily.setdefault(day, {"tokens_in": 0, "tokens_out": 0})
            d["tokens_in"] += int(r["tokens_in"] or 0)
            d["tokens_out"] += int(r["tokens_out"] or 0)
        return {
            "models": models,
            "daily": [{"date": k, **daily[k]} for k in sorted(daily)],
        }

    # ---- workflow_schedules (주기적 workflow run) ----
    @staticmethod
    def _schedule_json(r: Any) -> dict[str, Any]:
        d = dict(r)
        d["input"] = _loads(d.get("input") or "{}")
        d["enabled"] = bool(d.get("enabled"))
        return d

    def create_schedule(
        self,
        workflow: str,
        input_data: dict[str, Any],
        interval_minutes: int,
        org_id: str | None = None,
    ) -> dict[str, Any]:
        now = _now()
        nxt = now + timedelta(minutes=interval_minutes)
        sid = uuid.uuid4().hex[:12]
        with self.tx() as c:
            c.execute(
                "INSERT INTO workflow_schedules(id,workflow,input,interval_minutes,"
                "enabled,last_run_at,next_run_at,created_at,org_id)"
                " VALUES(?,?,?,?,1,NULL,?,?,?)",
                (
                    sid,
                    workflow,
                    json.dumps(input_data or {}),
                    int(interval_minutes),
                    _iso(nxt),
                    _iso(now),
                    orgctx.org_for_write(org_id),
                ),
            )
        sched = self.get_schedule(sid)
        return sched if sched is not None else {"id": sid}

    def list_schedules(self) -> list[dict[str, Any]]:
        frag, params = _org_filter()
        rows = self._conn.execute(
            "SELECT * FROM workflow_schedules"  # noqa: S608
            + frag.replace(" AND ", " WHERE ", 1)
            + " ORDER BY created_at DESC",
            tuple(params),
        ).fetchall()
        return [self._schedule_json(r) for r in rows]

    def get_schedule(self, schedule_id: str) -> dict[str, Any] | None:
        frag, params = _org_filter()
        row = self._conn.execute(
            "SELECT * FROM workflow_schedules WHERE id=?" + frag,  # noqa: S608
            (schedule_id, *params),
        ).fetchone()
        return self._schedule_json(row) if row else None

    def set_schedule_enabled(self, schedule_id: str, enabled: bool) -> dict[str, Any] | None:
        """enabled 토글. 활성화 시 next_run_at을 now+interval로 재계산."""
        sched = self.get_schedule(schedule_id)
        if sched is None:
            return None
        next_run = sched.get("next_run_at")
        if enabled:
            nxt = _now() + timedelta(minutes=int(sched["interval_minutes"]))
            next_run = _iso(nxt)
        frag, fparams = _org_filter()
        with self.tx() as c:
            c.execute(
                "UPDATE workflow_schedules SET enabled=?, next_run_at=? WHERE id=?" + frag,  # noqa: S608
                (1 if enabled else 0, next_run, schedule_id, *fparams),
            )
        return self.get_schedule(schedule_id)

    def delete_schedule(self, schedule_id: str) -> bool:
        frag, params = _org_filter()
        with self.tx() as c:
            cur = c.execute(
                "DELETE FROM workflow_schedules WHERE id=?" + frag,  # noqa: S608
                (schedule_id, *params),
            )
        return cur.rowcount > 0

    def due_schedules(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM workflow_schedules WHERE enabled=1"
            " AND next_run_at IS NOT NULL AND next_run_at <= ?"
            " ORDER BY next_run_at",
            (_iso(_now()),),
        ).fetchall()
        return [self._schedule_json(r) for r in rows]

    def claim_schedule_fire(
        self,
        schedule_id: str,
        expected_next_run_at: str,
        last_run_at: str,
        next_run_at: str,
    ) -> bool:
        """스케줄 발사 원자 claim — next_run_at compare-and-swap.

        due_schedules()로 읽은 행의 next_run_at이 그대로일 때만 last/next를
        갱신한다. automation loop과 cron tick(또는 다중 인스턴스)이 동시에
        같은 스케줄을 발사해 run을 중복 생성하는 것을 막는다.
        rowcount>0이면 claim 성공 — 호출자는 run 생성으로 진행한다."""
        with self.tx() as c:
            cur = c.execute(
                "UPDATE workflow_schedules SET last_run_at=?, next_run_at=?"
                " WHERE id=? AND enabled=1 AND next_run_at=?",
                (last_run_at, next_run_at, schedule_id, expected_next_run_at),
            )
        return cur.rowcount > 0

    def mark_schedule_fired(self, schedule_id: str, last_run_at: str, next_run_at: str) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE workflow_schedules SET last_run_at=?, next_run_at=? WHERE id=?",
                (last_run_at, next_run_at, schedule_id),
            )

    # ---- run stats (GET /runs/stats) ----
    _STATS_FAILED = {"failed", "rejected", "budget_exhausted", "needs_human", "cancelled"}
    _STATS_TERMINAL = _STATS_FAILED | {"completed"}

    def run_stats(self, workflow_key: str | None, days: int) -> dict[str, Any]:
        """최근 days일 run/stage_runs 집계. created_at 기준 윈도우."""
        days = max(1, min(int(days), 365))
        cutoff = _iso(_now() - timedelta(days=days))
        frag, fparams = _org_filter()
        frag_r, fparams_r = _org_filter("r")
        # workflow_key=None이면 전체 — "? IS NULL OR" 패턴으로 분기 없이 처리
        run_params: list[Any] = [cutoff, workflow_key, workflow_key, *fparams]
        stage_params: list[Any] = [cutoff, workflow_key, workflow_key, *fparams_r]
        run_rows = self._conn.execute(
            "SELECT id,status,created_at,updated_at FROM runs"  # noqa: S608
            " WHERE created_at >= ? AND (? IS NULL OR workflow_key=?)" + frag,
            run_params,
        ).fetchall()
        stage_rows = self._conn.execute(
            "SELECT sr.run_id, sr.stage_key, sr.attempt, sr.status, sr.tokens_in,"  # noqa: S608
            " sr.tokens_out, sr.started_at FROM stage_runs sr"
            " JOIN runs r ON r.id = sr.run_id"
            " WHERE r.created_at >= ? AND (? IS NULL OR r.workflow_key=?)" + frag_r,
            stage_params,
        ).fetchall()

        counts = {"succeeded": 0, "failed": 0, "running": 0, "waiting_approval": 0}
        durations: list[float] = []
        daily: dict[str, dict[str, int]] = {}
        for row in run_rows:
            st = row["status"]
            if st == "completed":
                counts["succeeded"] += 1
            elif st in self._STATS_FAILED:
                counts["failed"] += 1
            elif st in ("queued", "running"):
                counts["running"] += 1
            elif st == "waiting_approval":
                counts["waiting_approval"] += 1
            created = _dt(row["created_at"])
            updated = _dt(row["updated_at"])
            if st in self._STATS_TERMINAL and created and updated:
                durations.append((updated - created).total_seconds())
            day = (row["created_at"] or "")[:10]
            if day:
                daily.setdefault(day, {"runs": 0, "tokens": 0})["runs"] += 1

        tokens_in = tokens_out = 0
        latest_attempt: dict[tuple[str, str], tuple[int, str]] = {}
        for row in stage_rows:
            tin = int(row["tokens_in"] or 0)
            tout = int(row["tokens_out"] or 0)
            tokens_in += tin
            tokens_out += tout
            skey = (row["run_id"], row["stage_key"])
            attempt = int(row["attempt"] or 0)
            if skey not in latest_attempt or attempt > latest_attempt[skey][0]:
                latest_attempt[skey] = (attempt, row["status"])
            day = (row["started_at"] or "")[:10]
            if day:
                daily.setdefault(day, {"runs": 0, "tokens": 0})["tokens"] += tin + tout

        stage_failures: dict[str, int] = {}
        for (_run_id, stage_key), (_attempt, st) in latest_attempt.items():
            if st == "failed":
                stage_failures[stage_key] = stage_failures.get(stage_key, 0) + 1

        today = _now().date()
        start = today - timedelta(days=days - 1)
        out_daily: list[dict[str, Any]] = []
        d = start
        while d <= today:
            key = d.isoformat()
            bucket = daily.get(key, {"runs": 0, "tokens": 0})
            out_daily.append({"date": key, "runs": bucket["runs"], "tokens": bucket["tokens"]})
            d += timedelta(days=1)

        return {
            "workflow": workflow_key,
            "days": days,
            "runs": len(run_rows),
            **counts,
            "avg_duration_s": (round(sum(durations) / len(durations), 1) if durations else None),
            "total_tokens_in": tokens_in,
            "total_tokens_out": tokens_out,
            "stage_failures": stage_failures,
            "daily": out_daily,
        }

    def llm_stats(self, days: int, monthly_budget: int | None = None) -> dict[str, Any]:
        """Agent Insights 대시보드 집계 — run 지연시간 백분위, 토큰, 모델/프롬프트
        버전, stage 호출 수, 오류율, 승인 백로그, 일별 시계열, 최근 실패 run,
        stage 병목·모델 라우팅·재시도 낭비·워크플로 단가·월 예산 사용량.
        (블로그 권고: p95 latency, avg tokens, tool call count, retrieval hit rate
        대체 지표로 stage call·실패율, eval pass rate 대체로 승인 대기 수)"""
        days = max(1, min(int(days), 365))
        cutoff = _iso(_now() - timedelta(days=days))
        frag, fparams = _org_filter()
        frag_r, fparams_r = _org_filter("r")
        run_rows = self._conn.execute(
            "SELECT id,workflow_key,status,created_at,updated_at,error,project,trace_id"  # noqa: S608
            " FROM runs WHERE created_at >= ?" + frag,
            (cutoff, *fparams),
        ).fetchall()
        stage_rows = self._conn.execute(
            "SELECT sr.run_id, sr.stage_key, sr.attempt, sr.status, sr.model,"  # noqa: S608
            " sr.prompt_version, sr.tokens_in, sr.tokens_out, sr.error,"
            " sr.started_at, sr.finished_at"
            " FROM stage_runs sr JOIN runs r ON r.id = sr.run_id"
            " WHERE r.created_at >= ?" + frag_r,
            (cutoff, *fparams_r),
        ).fetchall()

        by_status: dict[str, int] = {}
        durations: list[float] = []
        daily: dict[str, dict[str, int]] = {}
        recent_failures: list[dict[str, Any]] = []
        waiting: list[datetime] = []
        run_workflow: dict[str, str] = {}
        wf_stats: dict[str, dict[str, Any]] = {}
        now_dt = _now()
        for row in run_rows:
            st = row["status"]
            by_status[st] = by_status.get(st, 0) + 1
            wf_key = row["workflow_key"] or "(unknown)"
            run_workflow[row["id"]] = wf_key
            wfs = wf_stats.setdefault(
                wf_key, {"workflow": wf_key, "runs": 0, "failed": 0, "durations": [], "tokens": 0}
            )
            wfs["runs"] += 1
            created = _dt(row["created_at"])
            updated = _dt(row["updated_at"])
            if st in self._STATS_TERMINAL and created and updated:
                dur = max(0.0, (updated - created).total_seconds())
                durations.append(dur)
                wfs["durations"].append(dur)
            if st in self._STATS_FAILED:
                wfs["failed"] += 1
            day = (row["created_at"] or "")[:10]
            if day:
                daily.setdefault(day, {"runs": 0, "tokens": 0, "errors": 0})
                daily[day]["runs"] += 1
                if st in self._STATS_FAILED:
                    daily[day]["errors"] += 1
            if st == "waiting_approval" and updated:
                waiting.append(updated)
            if st in self._STATS_FAILED:
                recent_failures.append(
                    {
                        "id": row["id"],
                        "workflow": row["workflow_key"],
                        "status": st,
                        "project": row["project"],
                        "error": (row["error"] or "")[:300],
                        "updated_at": row["updated_at"],
                        "trace_id": row["trace_id"],
                    }
                )
        recent_failures.sort(key=lambda x: str(x["updated_at"]), reverse=True)

        def pct(vals: list[float], p: float) -> float | None:
            if not vals:
                return None
            s = sorted(vals)
            k = max(0, min(len(s) - 1, round(p / 100 * (len(s) - 1))))
            return round(s[k], 1)

        tokens_in = tokens_out = 0
        stage_calls = stage_failures = 0
        models: dict[str, dict[str, Any]] = {}
        prompt_versions: set[str] = set()
        stage_durations: list[float] = []
        token_runs: set[str] = set()
        month_key = now_dt.strftime("%Y-%m")
        retry_calls = 0
        retry_tokens = 0
        failed_stage_tokens = 0
        attempts_hist: dict[str, int] = {}
        stage_agg: dict[str, dict[str, Any]] = {}
        smodel_agg: dict[tuple[str, str], dict[str, Any]] = {}
        pv_agg: dict[str, dict[str, Any]] = {}
        for row in stage_rows:
            tin = int(row["tokens_in"] or 0)
            tout = int(row["tokens_out"] or 0)
            tok = tin + tout
            tokens_in += tin
            tokens_out += tout
            if tin or tout:
                token_runs.add(row["run_id"])
            stage_calls += 1
            attempt = int(row["attempt"] or 1)
            attempts_hist[str(attempt)] = attempts_hist.get(str(attempt), 0) + 1
            failed = row["status"] == "failed"
            if failed:
                stage_failures += 1
                failed_stage_tokens += tok
            if attempt > 1:
                retry_calls += 1
                retry_tokens += tok
            wf = run_workflow.get(row["run_id"], "(unknown)")
            wfs = wf_stats.setdefault(
                wf, {"workflow": wf, "runs": 0, "failed": 0, "durations": [], "tokens": 0}
            )
            wfs["tokens"] += tok
            model = row["model"] or "(unknown)"
            m = models.setdefault(model, {"model": model, "calls": 0, "tokens": 0})
            m["calls"] += 1
            m["tokens"] += tok
            skey = row["stage_key"] or "(unknown)"
            sa = stage_agg.setdefault(
                skey,
                {
                    "stage_key": skey,
                    "calls": 0,
                    "failures": 0,
                    "tokens": 0,
                    "durations": [],
                    "models": {},
                },
            )
            sa["calls"] += 1
            sa["tokens"] += tok
            if failed:
                sa["failures"] += 1
            sm = sa["models"].setdefault(
                model, {"model": model, "calls": 0, "tokens": 0, "failures": 0}
            )
            sm["calls"] += 1
            sm["tokens"] += tok
            if failed:
                sm["failures"] += 1
            smk = (skey, model)
            sma = smodel_agg.setdefault(
                smk, {"stage_key": skey, "model": model, "calls": 0, "tokens": 0, "failures": 0}
            )
            sma["calls"] += 1
            sma["tokens"] += tok
            if failed:
                sma["failures"] += 1
            pv = row["prompt_version"] or ""
            if pv:
                prompt_versions.add(pv)
                pa = pv_agg.setdefault(pv, {"version": pv, "calls": 0, "tokens": 0, "failures": 0})
                pa["calls"] += 1
                pa["tokens"] += tok
                if failed:
                    pa["failures"] += 1
            st, ft = _dt(row["started_at"]), _dt(row["finished_at"])
            if st and ft:
                dur = max(0.0, (ft - st).total_seconds())
                stage_durations.append(dur)
                sa["durations"].append(dur)
            day = (row["started_at"] or "")[:10]
            if day and day in daily:
                daily[day]["tokens"] += tok

        terminal = sum(by_status.get(s, 0) for s in self._STATS_TERMINAL)
        failed = sum(by_status.get(s, 0) for s in self._STATS_FAILED)
        total_tokens = tokens_in + tokens_out
        today = _now().date()
        start = today - timedelta(days=days - 1)
        out_daily: list[dict[str, Any]] = []
        d = start
        while d <= today:
            key = d.isoformat()
            b = daily.get(key, {"runs": 0, "tokens": 0, "errors": 0})
            out_daily.append(
                {"date": key, "runs": b["runs"], "tokens": b["tokens"], "errors": b["errors"]}
            )
            d += timedelta(days=1)

        model_list = sorted(models.values(), key=lambda x: -x["tokens"])[:10]
        token_sum = sum(m["tokens"] for m in model_list)
        for m in model_list:
            m["share_pct"] = round(m["tokens"] / token_sum * 100, 1) if token_sum else 0

        # 월 예산 — 윈도우와 무관하게 당월 전체 stage 토큰 합산
        month_start = now_dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        frag_r, fparams_r = _org_filter("r")
        mrow = self._conn.execute(
            "SELECT COALESCE(SUM(sr.tokens_in + sr.tokens_out),0) AS t"  # noqa: S608
            " FROM stage_runs sr JOIN runs r ON r.id = sr.run_id"
            " WHERE sr.started_at >= ?" + frag_r,
            (_iso(month_start), *fparams_r),
        ).fetchone()
        month_tokens = int(mrow["t"] or 0) if mrow else 0
        budget = None
        if monthly_budget:
            used_pct = round(month_tokens / monthly_budget * 100, 1)
            budget = {
                "month": month_key,
                "monthly_tokens": month_tokens,
                "budget_max": monthly_budget,
                "used_pct": used_pct,
                "alert": used_pct >= 80,
            }

        by_stage = []
        for sa in stage_agg.values():
            durs = sa.pop("durations")
            models_by_stage = sorted(sa["models"].values(), key=lambda x: -x["tokens"])[:5]
            by_stage.append(
                {
                    "stage_key": sa["stage_key"],
                    "calls": sa["calls"],
                    "failures": sa["failures"],
                    "failure_rate_pct": round(sa["failures"] / sa["calls"] * 100, 1)
                    if sa["calls"]
                    else None,
                    "tokens": sa["tokens"],
                    "latency_s": {
                        "p50": pct(durs, 50),
                        "p95": pct(durs, 95),
                        "avg": round(sum(durs) / len(durs), 1) if durs else None,
                    },
                    "models": models_by_stage,
                }
            )
        by_stage.sort(key=lambda x: -(x["latency_s"]["p95"] or 0))

        by_workflow = []
        for w in wf_stats.values():
            durs = w["durations"]
            by_workflow.append(
                {
                    "workflow": w["workflow"],
                    "runs": w["runs"],
                    "failed": w["failed"],
                    "avg_duration_s": round(sum(durs) / len(durs), 1) if durs else None,
                    "total_tokens": w["tokens"],
                    "avg_tokens_per_run": round(w["tokens"] / w["runs"], 1) if w["runs"] else None,
                }
            )
        by_workflow.sort(key=lambda x: -x["total_tokens"])

        waiting_total_s = (
            round(sum((now_dt - w).total_seconds() for w in waiting), 0) if waiting else None
        )

        return {
            "days": days,
            "runs_total": len(run_rows),
            "by_status": by_status,
            "terminal_runs": terminal,
            "failed_runs": failed,
            "error_rate_pct": round(failed / terminal * 100, 1) if terminal else None,
            "latency_s": {
                "p50": pct(durations, 50),
                "p95": pct(durations, 95),
                "p99": pct(durations, 99),
                "avg": round(sum(durations) / len(durations), 1) if durations else None,
                "max": round(max(durations), 1) if durations else None,
                "samples": len(durations),
            },
            "stage_latency_s": {
                "p50": pct(stage_durations, 50),
                "p95": pct(stage_durations, 95),
                "avg": round(sum(stage_durations) / len(stage_durations), 1)
                if stage_durations
                else None,
            },
            "tokens": {
                "total_in": tokens_in,
                "total_out": tokens_out,
                "total": total_tokens,
                "avg_per_run": round(total_tokens / len(token_runs), 1) if token_runs else None,
                "runs_with_tokens": len(token_runs),
            },
            "models": model_list,
            "prompt_versions": sorted(prompt_versions),
            "prompt_version_stats": sorted(pv_agg.values(), key=lambda x: -x["tokens"]),
            "stage_model_matrix": [
                {
                    **v,
                    "failure_rate_pct": round(v["failures"] / v["calls"] * 100, 1)
                    if v["calls"]
                    else None,
                }
                for v in sorted(smodel_agg.values(), key=lambda x: -x["tokens"])
            ][:50],
            "by_stage": by_stage,
            "by_workflow": by_workflow,
            "stage_calls": stage_calls,
            "stage_failures": stage_failures,
            "stage_failure_rate_pct": round(stage_failures / stage_calls * 100, 1)
            if stage_calls
            else None,
            "retry": {
                "calls": retry_calls,
                "tokens": retry_tokens,
                "token_pct": round(retry_tokens / total_tokens * 100, 1) if total_tokens else None,
                "failed_stage_tokens": failed_stage_tokens,
                "attempts_hist": dict(sorted(attempts_hist.items(), key=lambda x: int(x[0]))),
            },
            "tokens_ratio": {
                "input_pct": round(tokens_in / total_tokens * 100, 1) if total_tokens else None,
                "output_pct": round(tokens_out / total_tokens * 100, 1) if total_tokens else None,
            },
            "budget": budget,
            "approval_backlog": {
                "waiting": by_status.get("waiting_approval", 0),
                "oldest_wait_s": round((now_dt - min(waiting)).total_seconds(), 0)
                if waiting
                else None,
                "total_wait_s": waiting_total_s,
            },
            "daily": out_daily,
            "recent_failures": recent_failures[:10],
        }

    # ---- llm_usage 상세 (비용 대시보드용 행 단위 조회) ----
    def llm_usage_rows(self, since_iso: str) -> list[dict[str, Any]]:
        frag, params = _org_filter()
        rows = self._conn.execute(
            "SELECT model,tokens_in,tokens_out,created_at FROM llm_usage"  # noqa: S608
            " WHERE created_at >= ?" + frag + " ORDER BY created_at",
            (since_iso, *params),
        ).fetchall()
        return [dict(r) for r in rows]
