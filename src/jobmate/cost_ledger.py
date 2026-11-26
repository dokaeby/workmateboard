"""비용 원장 emission + 1회성 백필 (paperclip-agent-management-spec 2차 A/C).

- emit_stage_cost_event: stage_run 1건당 cost_events 1행 — idempotency_key
  `cost:{stage_run_id}`로 재시도/재처리 중복을 막는다. model이 있으면 rate
  card로 estimated, 없으면(외부 function 단계 등) unpriced로 기록한다 —
  가격을 모르는 사용량을 0으로 기록하지 않는다.
- backfill_cost_events: cost_events 도입 이전의 stage_runs를 `backfill:`
  키로 소급 기록 — maybe_backfill_cost_events가 automation_state 가드로
  프로세스당 1회만 실행한다.

주의: MeteredLLM(llm_usage)은 호출 단위 계측이고 이 원장은 stage_run 단위
청구 귀속이므로 두 경로는 별개다 — 여기서 llm_usage를 다시 기록하지 않는다.
"""

from __future__ import annotations

import logging
from typing import Any

from .pricing import PRICING_VERSION, cost_microusd
from .store.db import Store
from .store.util import _iso, _now

logger = logging.getLogger("jobmate.costs")

# automation_state 가드 키 — 백필 완료 시각/결과를 저장한다.
_BACKFILL_STATE_KEY = "cost_ledger_backfill_v1"
# 1회 호출당 처리 상한 — 대량 DB에서도 기동/루프를 막지 않는다.
_BACKFILL_BATCH = 500
_BACKFILL_MAX_BATCHES = 20


def _resolve_run_context(store: Store, run_id: str) -> tuple[str, str]:
    """run → (agent_id, task_id). agent_id는 runs.agent_id 태그,
    task_id는 task_runs 첫 링크 — 없으면 '' (spec agent_id 필수 완화)."""
    agent_id = ""
    task_id = ""
    try:
        run = store.get_run(run_id)
        if run is not None:
            agent_id = str(getattr(run, "agent_id", "") or "")
    except Exception as e:
        logger.debug("cost context run lookup failed run=%s: %s", run_id, str(e)[:120])
    try:
        links = store.task_runs_for_run(run_id)
        if links:
            task_id = str(links[0].get("task_id") or "")
    except Exception as e:
        logger.debug("cost context task lookup failed run=%s: %s", run_id, str(e)[:120])
    return agent_id, task_id


def emit_stage_cost_event(
    store: Store,
    *,
    run_id: str,
    stage_run_id: str,
    model: str = "",
    provider: str = "",
    billing_type: str = "metered_api",
    tokens_in: int = 0,
    cached_input_tokens: int = 0,
    tokens_out: int = 0,
    occurred_at: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any] | None:
    """stage_run 사용량 → cost_events 1행. 토큰 0이면 기록하지 않고 None.

    - model 있음 → cost_status='estimated', cost_microusd=rate card 정수,
      pricing_source='rate_card', pricing_version=PRICING_VERSION.
    - model 없음 → 'unpriced'(cost_microusd=NULL) — function 단계처럼
      어떤 모델이 썼는지 알 수 없는 사용량을 억지 단가로 매기지 않는다.
    - 반환: 생성/재생된 행. 예외는 호출부 책임(실행 경로는 삼켜야 함)."""
    tin = max(0, int(tokens_in or 0))
    tcached = max(0, int(cached_input_tokens or 0))
    tout = max(0, int(tokens_out or 0))
    if tin + tcached + tout <= 0:
        return None
    agent_id, task_id = _resolve_run_context(store, run_id)
    model_s = (model or "").strip()[:200]
    if model_s:
        cost_status = "estimated"
        cost_mu: int | None = cost_microusd(model_s, tin, tout, tcached)
        p_source = "rate_card"
        p_version = PRICING_VERSION
        bt = billing_type or "metered_api"
    else:
        cost_status = "unpriced"
        cost_mu = None
        p_source = ""
        p_version = ""
        bt = "unknown"
    row, created = store.record_cost_event(
        idempotency_key=idempotency_key or f"cost:{stage_run_id}",
        agent_id=agent_id,
        task_id=task_id or None,
        run_id=run_id or None,
        stage_run_id=stage_run_id,
        provider=(provider or "").strip()[:64],
        model=model_s,
        billing_type=bt,
        cost_status=cost_status,
        input_tokens=tin,
        cached_input_tokens=tcached,
        output_tokens=tout,
        cost_microusd=cost_mu,
        pricing_source=p_source,
        pricing_version=p_version,
        occurred_at=occurred_at,
    )
    logger.info(
        "cost event %s id=%s run=%s stage_run=%s status=%s model=%s provider=%s",
        "accepted" if created else "replayed",
        (row or {}).get("id", "?"),
        run_id,
        stage_run_id,
        cost_status,
        model_s or "-",
        (provider or "-")[:40],
    )
    return row


def backfill_cost_events(store: Store, *, batch: int = _BACKFILL_BATCH) -> dict[str, int]:
    """cost_events 미커버 stage_runs를 `backfill:stage:{id}` 키로 소급 기록.

    멱등 — stage_run_id가 연결된 원장 행이 있으면 대상에서 제외되므로
    반복 호출/재시작해도 중복 행이 생기지 않는다. 반환: {scanned, created}."""
    rows = store.stage_runs_missing_cost_events(limit=batch)
    created = 0
    for r in rows:
        sid = str(r.get("id") or "")
        if not sid:
            continue
        model = str(r.get("model") or "")
        # stage_run.model은 llm 단계의 agent.model.model — function 단계는
        # ''라 unpriced로 기록된다(억지 단가 금지).
        bkey = f"backfill:stage:{sid}"
        # 이미 백필 키가 있으면 emit은 replay만 반환한다 — created는
        # 실제 INSERT된 행만 세야 하므로 시도 횟수가 아니라 생성 여부로 센다.
        if store.get_cost_event_by_key(bkey) is not None:
            continue
        row = emit_stage_cost_event(
            store,
            run_id=str(r.get("run_id") or ""),
            stage_run_id=sid,
            model=model,
            provider="",
            tokens_in=int(r.get("tokens_in") or 0),
            tokens_out=int(r.get("tokens_out") or 0),
            occurred_at=str(r.get("finished_at") or r.get("started_at") or "") or None,
            idempotency_key=bkey,
        )
        if row is not None:
            created += 1
    return {"scanned": len(rows), "created": created}


def maybe_backfill_cost_events(store: Store) -> None:
    """프로세스 수명 동안 1회만 백필 — automation_state 가드.

    automation loop/cron tick에서 매 호출해도 state 확인 1쿼리로 조기
    반환한다. 백필이 배치 상한에 걸려 미완료면 다음 호출이 이어서 처리하고,
    전부 끝났을 때만 done을 기록한다."""
    try:
        state = store.get_state(_BACKFILL_STATE_KEY, {})
        if state.get("done"):
            return
        total_created = 0
        total_scanned = 0
        for _ in range(_BACKFILL_MAX_BATCHES):
            res = backfill_cost_events(store)
            total_scanned += res["scanned"]
            total_created += res["created"]
            if res["scanned"] < _BACKFILL_BATCH:
                break
        # 남은 대상이 없으면 완료 — scanned==0이면서 created==0이면 이미 정리됨
        remaining = store.stage_runs_missing_cost_events(limit=1)
        if not remaining:
            store.set_state(
                _BACKFILL_STATE_KEY,
                {
                    "done": _iso(_now()),
                    "scanned": total_scanned,
                    "created": total_created,
                },
            )
            logger.info(
                "cost ledger backfill done scanned=%d created=%d",
                total_scanned,
                total_created,
            )
        else:
            logger.info(
                "cost ledger backfill partial scanned=%d created=%d remaining>0",
                total_scanned,
                total_created,
            )
    except Exception as e:
        # 백필 실패가 run/루프를 막으면 안 된다 — 다음 주기에 재시도.
        logger.warning("cost ledger backfill failed: %s", str(e)[:200])
