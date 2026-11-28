"""spec 008 US2 — 온라인 평가(online evals).

run/stage가 terminal에 도달하면 활성 eval_schedules를 평가해 sample_rate
확률로 기존 eval 채점 경로(evals.maybe_score_eval_run — 결정적 체크,
lazy 채점과 동일 엔진)를 실행한다. 결과는 eval_runs에 source='online',
schedule_id와 함께 기록되고, 점수가 불합격 기준 미만이면 inbox 알림을 남긴다.

호출 경로:
- api/jobs.py::_execute_run_job / _create_and_run — run 종결 직후 즉시 평가.
- api/jobs.py::_online_eval_sweep — automation cycle 백스톱(reaper·승인
  resume 등 직접 hook을 놓친 경로 보완). (schedule_id, run_id) dedupe가
  중복 평가를 막는다.

조직 스코프: 호출자가 run의 org로 org_scope를 씌운 상태에서 호출해야 한다
(_execute_run_job은 이미 run org로 재진입, sweep은 행 org로 감싼다).
ctx='' 경로에서는 _org_filter가 무필터라 전 조직 스케줄이 섞이므로
서비스 진입 시 run.org_id와 스케줄 org_id를 명시 비교해 한 번 더 격리한다.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from .. import evals as evals_svc
from ..models import StageStatus
from ..store import orgctx
from ..store.db import Store

logger = logging.getLogger("jobmate.online_evals")

# 불합격 판정 — eval score는 통과 체크 비율(0~100). 80 미만이면 inbox 알림.
# evals 테이블에 pass threshold 컬럼이 없으므로 서비스 상수로 둔다
# (프론트 scoreTone의 ok>=80 기준과 동일).
ONLINE_EVAL_FAIL_SCORE = 80.0

_TERMINAL_STAGE_STATUSES = frozenset(
    {StageStatus.SUCCEEDED.value, StageStatus.FAILED.value, StageStatus.SKIPPED.value}
)


def _run_org_id(store: Store, run_id: str) -> str:
    """run 행의 org_id — Run 모델에 없으므로 raw 조회(orgctx 무관)."""
    try:
        row = store._conn.execute(  # noqa: SLF001 - org 필터 없는 내부 조회
            "SELECT org_id FROM runs WHERE id=?", (run_id,)
        ).fetchone()
        return str(row["org_id"] or "") if row else ""
    except Exception:
        return ""


def _org_in_scope(schedule: dict[str, Any], org_id: str) -> bool:
    """ctx='' 무필터 경로 방어 — 스케줄 org와 run org가 다르면 평가하지 않는다.

    org_id=''(조직 미귀속 run)는 evaluate_run이 상위에서 이미 걸러낸다 —
    여기서는 방어적으로도 불일치를 제외한다."""
    return bool(org_id) and str(schedule.get("org_id") or "") == org_id


def _sampled_for(schedule: dict[str, Any], run_id: str) -> bool:
    """(schedule, run) 결정적 샘플링 — sha256(schedule_id:run_id) → [0,1).

    stage_completed처럼 run당 다중 트리거 이벤트가 있어도 같은
    (schedule_id, run_id)면 항상 같은 판정이 나온다 — stage 수만큼
    확률이 부풀지 않도록 run당 1회만 평가한다."""
    try:
        rate = float(schedule.get("sample_rate") or 0.0)
    except (TypeError, ValueError):
        rate = 0.0
    if rate <= 0.0:
        return False
    if rate >= 1.0:
        return True
    sid = str(schedule.get("id") or "")
    digest = hashlib.sha256(f"{sid}:{run_id}".encode()).digest()
    # 상위 64bit → [0,1) 비편향 근사(2**64 분모).
    draw = int.from_bytes(digest[:8], "big") / float(1 << 64)
    return draw < rate


def _resolve_eval(store: Store, eval_name: str, schedule_org_id: str = "") -> dict[str, Any] | None:
    """eval_name → 활성 eval 행. org 스코프는 find_eval_by_name의 _org_filter
    + 스케줄 org로 명시 재스코프(ctx='' 경로의 타 조직 eval 오인 방지)."""
    if schedule_org_id:
        with orgctx.org_scope(schedule_org_id):
            ev = store.find_eval_by_name(str(eval_name or ""))
    else:
        ev = store.find_eval_by_name(str(eval_name or ""))
    if ev is None:
        return None
    if str(ev.get("status") or "active") != "active":
        return None
    return ev


def _maybe_notify_failure(store: Store, er: dict[str, Any], schedule: dict[str, Any]) -> None:
    """scored + score < FAIL_SCORE → inbox 알림(dedupe_key로 중복 방지)."""
    if str(er.get("status") or "") != "scored":
        return
    score = er.get("score")
    if score is None:
        return
    try:
        score_f = float(score)
    except (TypeError, ValueError):
        return
    if score_f >= ONLINE_EVAL_FAIL_SCORE:
        return
    er_id = str(er.get("id") or "")
    sched_name = str(schedule.get("name") or schedule.get("id") or "")
    try:
        store.upsert_inbox_item(
            "eval",
            f"온라인 평가 불합격: {sched_name}",
            body=(
                f"eval={er.get('eval_id')} run={er.get('run_id')} "
                f"score={score_f:.1f} (기준 {ONLINE_EVAL_FAIL_SCORE:.0f} 미만)"
            ),
            link="/?view=evals",
            dedupe_key=f"online-eval-fail:{er_id}",
        )
    except Exception as e:
        logger.warning("online eval inbox notify failed er=%s: %s", er_id, str(e)[:200])


def _fire_schedule(
    store: Store,
    schedule: dict[str, Any],
    run_id: str,
    org_id: str,
) -> dict[str, Any] | None:
    """단일 스케줄에 대한 eval_run 생성 + 즉시 채점 + 불합격 알림.

    dedupe: (schedule_id, run_id) 기존 행이 있으면 None."""
    sid = str(schedule.get("id") or "")
    if not sid or store.eval_run_exists(sid, run_id):
        return None
    ev = _resolve_eval(
        store, str(schedule.get("eval_name") or ""), str(schedule.get("org_id") or "")
    )
    if ev is None:
        logger.info(
            "online eval skipped: eval not found schedule=%s eval_name=%s",
            sid,
            str(schedule.get("eval_name") or "")[:80],
        )
        return None
    try:
        er = store.create_online_eval_run(str(ev["id"]), run_id, sid)
    except Exception as e:
        logger.warning("online eval run create failed schedule=%s: %s", sid, str(e)[:200])
        return None
    try:
        store.audit(
            run_id,
            "system",
            "online_eval_created",
            sid,
            {"eval_run_id": str(er.get("id") or ""), "eval_id": str(ev.get("id") or "")},
        )
    except Exception:
        logger.debug("online eval audit failed", exc_info=True)
    # run은 이미 terminal — maybe_score_eval_run이 즉시 결정적 채점을 수행.
    try:
        er = evals_svc.maybe_score_eval_run(store, er)
    except Exception:
        logger.warning("online eval scoring failed er=%s", er.get("id"), exc_info=True)
        return er
    _maybe_notify_failure(store, er, schedule)
    return er


def _schedules_for(
    store: Store, target: str, workflow_key: str, org_id: str
) -> list[dict[str, Any]]:
    """target 일치 + workflow_key 필터(빈 값=전체) + org 일치 + enabled."""
    out: list[dict[str, Any]] = []
    for s in store.list_eval_schedules(enabled_only=True):
        if str(s.get("target") or "") != target:
            continue
        if not _org_in_scope(s, org_id):
            continue
        swk = str(s.get("workflow_key") or "").strip()
        if swk and swk != workflow_key:
            continue
        out.append(s)
    return out


def evaluate_run(store: Store, run_id: str) -> int:
    """run 1건의 온라인 평가 평가 — 생성된 eval_run 수를 반환.

    run_completed 스케줄은 run이 terminal일 때만 발화하고,
    stage_completed 스케줄은 terminal stage_run마다 발화한다
    (dedupe (schedule_id, run_id)가 중복을 막는다 — 첫 샘플 통과
    stage_run이 eval_run을 만든다)."""
    run = store.get_run(run_id)
    if run is None:
        return 0
    status = str(getattr(run.status, "value", run.status) or "")
    org_id = _run_org_id(store, run_id)
    if not org_id:
        # 조직 미귀속 run — 어느 조직의 eval/schedule을 쓸지 결정할 수
        # 없으므로 온라인 평가를 건너뛴다(ctx='' 무필터 경로의
        # cross-org 평가 방지).
        logger.debug("online eval skipped: run %s has no org", run_id)
        return 0
    workflow_key = str(run.workflow_key or "")
    created = 0

    # ---- run_completed ----
    if status in evals_svc.TERMINAL_RUN_STATUSES:
        for sched in _schedules_for(store, "run_completed", workflow_key, org_id):
            try:
                if not _sampled_for(sched, run_id):
                    continue
                if _fire_schedule(store, sched, run_id, org_id) is not None:
                    created += 1
            except Exception:
                logger.warning(
                    "online eval schedule failed id=%s run=%s",
                    sched.get("id"),
                    run_id,
                    exc_info=True,
                )

    # ---- stage_completed — terminal stage_run 각각이 트리거 이벤트 ----
    try:
        stage_runs = store.get_stage_runs(run_id)
    except Exception:
        logger.warning("online eval stage scan failed run=%s", run_id, exc_info=True)
        stage_runs = []
    terminal_stages = [
        sr
        for sr in stage_runs
        if str(getattr(sr.status, "value", sr.status) or "") in _TERMINAL_STAGE_STATUSES
    ]
    if terminal_stages:
        for sched in _schedules_for(store, "stage_completed", workflow_key, org_id):
            sid = str(sched.get("id") or "")
            # 샘플링은 (schedule, run)당 1회 — stage 수만큼 확률이
            # 부풀지 않게 결정적 draw를 먼저 평가한다.
            try:
                if not _sampled_for(sched, run_id):
                    continue
            except Exception:
                logger.warning(
                    "online eval stage sample failed id=%s run=%s",
                    sched.get("id"),
                    run_id,
                    exc_info=True,
                )
                continue
            for sr in terminal_stages:
                # (schedule_id, run_id) dedupe — eval_run이 이미 있으면
                # 이후 stage는 평가하지 않는다.
                if store.eval_run_exists(sid, run_id):
                    break
                try:
                    if _fire_schedule(store, sched, run_id, org_id) is not None:
                        created += 1
                        break
                except Exception:
                    logger.warning(
                        "online eval stage schedule failed id=%s run=%s stage=%s",
                        sched.get("id"),
                        run_id,
                        getattr(sr, "stage_key", "?"),
                        exc_info=True,
                    )
    return created


def on_run_terminal(store: Store, run_id: str) -> int:
    """jobs.py hook 진입점 — run 종결 후 호출. run org로 스코프 재적용."""
    oid = _run_org_id(store, run_id)
    if oid and orgctx.current_org_id() != oid:
        with orgctx.org_scope(oid):
            return evaluate_run(store, run_id)
    return evaluate_run(store, run_id)


def sweep(store: Store, *, limit: int = 300) -> int:
    """automation cycle 백스톱 — watermark 이후 terminal run/stage를 스캔.

    직접 hook(_execute_run_job/_create_and_run)을 놓친 경로(reaper·
    승인 resume 등)를 보완한다. (schedule_id, run_id) dedupe로
    직접 평가와 겹쳐도 안전하다. 스케줄이 하나도 없으면 즉시 반환."""
    try:
        if not store.list_eval_schedules(enabled_only=True):
            return 0
    except Exception:
        return 0
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    wm_state = store.get_state("online_eval_sweep_ts", {})
    wm = str((wm_state or {}).get("ts") or "")
    try:
        since_dt = datetime.fromisoformat(wm) if wm else now - timedelta(minutes=15)
    except ValueError:
        since_dt = now - timedelta(minutes=15)
    since = (since_dt - timedelta(minutes=2)).isoformat()
    run_ids: list[str] = []
    try:
        rows = store._conn.execute(  # noqa: SLF001 - sweep 내부 집계 조회
            "SELECT id,status,org_id,updated_at FROM runs WHERE updated_at >= ?"
            " ORDER BY updated_at LIMIT ?",
            (since, max(1, int(limit))),
        ).fetchall()
        for r in rows:
            if str(r["status"]) in evals_svc.TERMINAL_RUN_STATUSES:
                run_ids.append(str(r["id"]))
        srows = store._conn.execute(  # noqa: SLF001
            "SELECT run_id FROM stage_runs WHERE finished_at >= ?"
            " AND status IN ('succeeded','failed','skipped')"
            " ORDER BY finished_at LIMIT ?",
            (since, max(1, int(limit))),
        ).fetchall()
        for r in srows:
            rid = str(r["run_id"] or "")
            if rid and rid not in run_ids:
                run_ids.append(rid)
    except Exception:
        logger.warning("online eval sweep scan failed", exc_info=True)
        return 0
    created = 0
    for rid in run_ids:
        try:
            created += on_run_terminal(store, rid)
        except Exception:
            logger.warning("online eval sweep run failed run=%s", rid, exc_info=True)
    try:
        store.set_state("online_eval_sweep_ts", {"ts": now.isoformat()})
    except Exception:
        logger.debug("online eval sweep watermark update failed", exc_info=True)
    return created
