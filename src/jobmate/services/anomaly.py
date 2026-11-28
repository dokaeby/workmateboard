"""spec 007 US7 — 이상 탐지-lite.

automation cycle(run_automation_cycle)에서 주기적으로 호출된다.
오늘의 비용(cost_events 합계)·실패 stage 수를 직전 7일 일평균과 비교해
값이 baseline*2 초과 + 절대 하한(비용 $0.5 / 에러 3건) 이상이면
anomaly_events에 기록하고 인박스 알림을 남긴다.

중복 방지: fingerprint = "{kind}:{metric}:{YYYY-MM-DD}" — 같은 날 같은
지표는 UNIQUE(org_id,fingerprint)로 1건만 기록되고, 다음 날 재발은
새 이벤트로 다시 알린다(24h 내 동일 알림 방지 요건 충족).

모든 경로는 best-effort — 탐지 실패가 automation cycle을 깨지 않는다.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from ..store import orgctx
from ..store.db import Store
from ..store.util import _iso, _now

logger = logging.getLogger("jobmate.anomaly")

SWEEP_STATE_KEY = "anomaly_sweep_v1"
# 시간 게이트 — automation cycle은 수십 초 간격이라 매 사이클 전수 스캔은
# 과하다. 1시간에 1회만 평가한다(크래시 후 재기동도 이 간격을 공유).
MIN_INTERVAL_S = 3600.0

# 임계값 — spec: value > baseline*2 AND value-baseline > floor
COST_SPIKE_MULT = 2.0
COST_SPIKE_FLOOR_USD = 0.5
ERROR_SPIKE_MULT = 2.0
ERROR_SPIKE_FLOOR = 3.0
BASELINE_DAYS = 7


def _parse_ts(value: Any) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value or ""))
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None


def run_anomaly_sweep(store: Store, *, force: bool = False) -> dict[str, Any]:
    """시간 게이트가 적용된 스윕 엔트리 — jobs.run_automation_cycle이 호출.

    force=True는 테스트/수동 트리거용 — 게이트를 무시하고 즉시 평가한다."""
    now = _now()
    if not force:
        state = store.get_state(SWEEP_STATE_KEY, {})
        last = _parse_ts((state or {}).get("ts"))
        if last is not None and (now - last).total_seconds() < MIN_INTERVAL_S:
            return {"skipped": True, "reason": "interval"}
    result = sweep_anomalies(store)
    try:
        watermark = {"ts": _iso(now)}
        watermark.update({k: v for k, v in result.items() if k != "items"})
        store.set_state(SWEEP_STATE_KEY, watermark)
    except Exception:
        logger.debug("anomaly sweep watermark update failed", exc_info=True)
    return result


def sweep_anomalies(store: Store) -> dict[str, Any]:
    """활성 조직별 비용/에러 스파이크 평가 — 반환 {detected, items}."""
    detected: list[dict[str, Any]] = []
    try:
        orgs = store.list_organizations()
    except Exception as e:
        logger.warning("anomaly sweep org list failed: %s", e)
        return {"detected": 0, "items": []}
    for org in orgs:
        if str(org.get("status") or "active") != "active":
            continue
        oid = str(org.get("id") or "")
        if not oid or oid == "__no_org__":
            continue
        try:
            with orgctx.org_scope(oid):
                detected.extend(_check_org(store, oid))
        except Exception as e:
            logger.warning("anomaly sweep org=%s failed: %s", oid, e)
    logger.info("anomaly sweep done detected=%d", len(detected))
    return {"detected": len(detected), "items": detected}


def _check_org(store: Store, oid: str) -> list[dict[str, Any]]:
    """한 조직의 오늘 vs 7일 평균 비교 — 발화 시 anomaly_events + inbox."""
    now = _now()
    today = now.date()
    midnight = datetime(today.year, today.month, today.day, tzinfo=UTC)
    today_iso = midnight.isoformat()
    week_iso = (midnight - timedelta(days=BASELINE_DAYS)).isoformat()
    day_key = today.isoformat()
    fired: list[dict[str, Any]] = []

    # ---- 비용 스파이크 ----
    try:
        today_mu = store.daily_cost_microusd(today_iso)
        prev_mu = store.daily_cost_microusd(week_iso, today_iso)
        value_usd = today_mu / 1_000_000
        baseline_usd = (prev_mu / BASELINE_DAYS) / 1_000_000
        if value_usd > baseline_usd * COST_SPIKE_MULT and (
            value_usd - baseline_usd > COST_SPIKE_FLOOR_USD
        ):
            fingerprint = f"cost_spike:daily_cost_usd:{day_key}"
            message = (
                f"일일 LLM 비용 ${value_usd:.2f} — 직전 {BASELINE_DAYS}일 평균 "
                f"${baseline_usd:.2f}의 {value_usd / baseline_usd:.1f}배"
                if baseline_usd > 0
                else f"일일 LLM 비용 ${value_usd:.2f} — 기준선 없음, 급증 감지"
            )
            if store.record_anomaly_event(
                kind="cost_spike",
                metric="daily_cost_usd",
                value=round(value_usd, 6),
                baseline=round(baseline_usd, 6),
                message=message,
                fingerprint=fingerprint,
                org_id=oid,
            ):
                fired.append({"kind": "cost_spike", "metric": "daily_cost_usd", "value": value_usd})
                _notify(store, oid, fingerprint, "비용 이상 감지", message)
                logger.warning(
                    "anomaly cost_spike org=%s value=%.4f baseline=%.4f",
                    oid,
                    value_usd,
                    baseline_usd,
                )
    except Exception as e:
        logger.warning("anomaly cost check failed org=%s: %s", oid, e)

    # ---- 에러 스파이크 ----
    try:
        today_err = store.failed_stage_count(today_iso)
        prev_err = store.failed_stage_count(week_iso, today_iso)
        baseline_err = prev_err / BASELINE_DAYS
        if today_err > baseline_err * ERROR_SPIKE_MULT and (
            today_err - baseline_err > ERROR_SPIKE_FLOOR
        ):
            fingerprint = f"error_spike:daily_failed_stages:{day_key}"
            message = (
                f"오늘 실패 stage {today_err}건 — 직전 {BASELINE_DAYS}일 일평균 "
                f"{baseline_err:.1f}건 대비 급증"
            )
            if store.record_anomaly_event(
                kind="error_spike",
                metric="daily_failed_stages",
                value=float(today_err),
                baseline=round(baseline_err, 3),
                message=message,
                fingerprint=fingerprint,
                org_id=oid,
            ):
                fired.append(
                    {"kind": "error_spike", "metric": "daily_failed_stages", "value": today_err}
                )
                _notify(store, oid, fingerprint, "에러 급증 감지", message)
                logger.warning(
                    "anomaly error_spike org=%s value=%d baseline=%.2f",
                    oid,
                    today_err,
                    baseline_err,
                )
    except Exception as e:
        logger.warning("anomaly error check failed org=%s: %s", oid, e)

    return fired


def _notify(store: Store, oid: str, fingerprint: str, title: str, body: str) -> None:
    """인박스 알림 — dedupe_key로 같은 fingerprint의 반복 알림을 막는다."""
    try:
        store.upsert_inbox_item(
            "anomaly",
            title,
            body=body[:300],
            link="/?view=monitor",
            dedupe_key=f"anomaly:{fingerprint}",
            org_id=oid,
        )
    except Exception as e:
        logger.debug("anomaly inbox item failed org=%s: %s", oid, str(e)[:200])
