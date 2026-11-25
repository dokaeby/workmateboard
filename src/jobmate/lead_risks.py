"""리스크 탐지 집계 — detect_risks (GET /risks).

라이브 계산, dedupe, 심각도+최신순 최대 50건. Store 읽기 + 순수 함수.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from .lead_common import (
    _APPROVAL_STATES,
    _MAX_ITEMS,
    _age_seconds,
    _fmt_duration,
    _now,
    session_health,
)
from .pricing import cost_usd

if TYPE_CHECKING:
    from .store.db import Store


# ---------- 리스크 탐지 (GET /risks) ----------

_SEV_RANK = {"high": 0, "medium": 1, "low": 2}


def _risk(
    rtype: str,
    severity: str,
    title: str,
    detail: str,
    ref_kind: str,
    ref_id: str,
    at: Any,
    now: datetime,
    i18n: dict[str, Any] | None = None,
) -> dict[str, Any]:
    age = _age_seconds(at, now)
    out: dict[str, Any] = {
        "type": rtype,
        "severity": severity,
        "title": title,
        "detail": detail,
        "ref_kind": ref_kind,
        "ref_id": ref_id,
        "at": str(at or ""),
        "age_seconds": round(age, 1) if age is not None else None,
    }
    if i18n:
        # 프론트 i18n용 — title/detail의 언어별 템플릿 키+파라미터.
        # 한국어 title/detail은 리포트/하위호환용으로 그대로 유지한다.
        out["i18n"] = i18n
    return out


def detect_risks(store: Store) -> dict[str, Any]:
    """GET /risks — 라이브 계산, dedupe, 심각도+최신순 최대 50건."""
    now = _now()
    items: list[dict[str, Any]] = []

    # 1) stage_failures — 같은 workflow+stage가 7일 내 서로 다른 run에서 ≥2회 실패
    since_7d = (now - timedelta(days=7)).isoformat()
    stage_rows = store.stage_run_rows(since_7d)
    latest: dict[tuple[str, str, str], tuple[int, str]] = {}
    run_meta: dict[str, dict[str, Any]] = {}
    for row in stage_rows:
        wf = str(row.get("workflow_key") or "")
        rid = str(row.get("run_id") or "")
        skey = str(row.get("stage_key") or "")
        run_meta[rid] = row
        key3 = (wf, rid, skey)
        attempt = int(row.get("attempt") or 0)
        if key3 not in latest or attempt > latest[key3][0]:
            latest[key3] = (attempt, str(row.get("status") or ""))
    fail_groups: dict[tuple[str, str], dict[str, Any]] = {}
    for (wf, rid, skey), (_a, st) in latest.items():
        if st != "failed":
            continue
        g = fail_groups.setdefault((wf, skey), {"runs": [], "latest_at": ""})
        g["runs"].append(rid)
        meta = run_meta.get(rid) or {}
        fail_at = str(meta.get("run_updated_at") or meta.get("started_at") or "")
        if fail_at > g["latest_at"]:
            g["latest_at"] = fail_at
    for (wf, skey), g in fail_groups.items():
        n = len(set(g["runs"]))
        if n < 2:
            continue
        severity = "high" if n >= 3 else "medium"
        latest_rid = sorted(set(g["runs"]))[-1]
        items.append(
            _risk(
                "stage_failure",
                severity,
                f"{wf} · {skey} 단계 반복 실패",
                f"최근 7일 {n}개 run에서 '{skey}' 단계가 실패했습니다.",
                "run",
                latest_rid,
                g["latest_at"],
                now,
                i18n={
                    "title_key": "risk.stage_failure.title",
                    "title_params": {"wf": wf, "stage": skey},
                    "detail_key": "risk.stage_failure.detail",
                    "detail_params": {"n": n, "stage": skey},
                },
            )
        )

    # 2) stale_approval — waiting_* 상태가 4h 초과
    for r in store.list_runs(limit=200):
        st = str(getattr(r, "status", "") or "")
        if st not in _APPROVAL_STATES:
            continue
        age = _age_seconds(getattr(r, "updated_at", None), now)
        if age is None or age < 4 * 3600:
            continue
        severity = "high" if age > 24 * 3600 else "medium"
        task = ""
        if isinstance(getattr(r, "input", None), dict):
            task = str(r.input.get("task") or "")[:60]
        items.append(
            _risk(
                "stale_approval",
                severity,
                f"승인 대기 지연: {getattr(r, 'workflow_key', '')}",
                f"{task or r.id} — {_fmt_duration(age)}째 승인 대기 중입니다.",
                "run",
                r.id,
                getattr(r, "updated_at", None),
                now,
                i18n={
                    "title_key": "risk.stale_approval.title",
                    "title_params": {"wf": str(getattr(r, "workflow_key", "") or "")},
                    "detail_key": "risk.stale_approval.detail",
                    "detail_params": {"task": task or r.id, "dur_s": round(age)},
                },
            )
        )

    # 3) idle_session — health가 stale/long_idle
    for s in store.list_agent_sessions(limit=300, include_ended=True):
        health = session_health(s)
        if health not in ("stale", "long_idle"):
            continue
        severity = "medium" if health == "stale" else "low"
        sess_at = s.get("last_event_at") or s.get("updated_at")
        items.append(
            _risk(
                "idle_session",
                severity,
                f"세션 {health}: {s.get('tool') or ''} {s.get('session_key', '')[:12]}",
                f"상태 {s.get('status')} — 마지막 이벤트 "
                f"{_fmt_duration(_age_seconds(sess_at, now) or 0)} 전.",
                "session",
                str(s.get("session_key") or ""),
                sess_at,
                now,
                i18n={
                    "title_key": "risk.idle_session.title",
                    "title_params": {
                        "health": health,
                        "tool": str(s.get("tool") or ""),
                        "key": str(s.get("session_key") or "")[:12],
                    },
                    "detail_key": "risk.idle_session.detail",
                    "detail_params": {
                        "status": str(s.get("status") or ""),
                        "dur_s": round(_age_seconds(sess_at, now) or 0),
                    },
                },
            )
        )

    # 4) cost_spike — 오늘 추정 비용 > 3× 7일 일평균 (그리고 >$1)
    cutoff_8d = (now - timedelta(days=8)).isoformat()
    daily_cost: dict[str, float] = {}
    for row in store.llm_usage_rows(cutoff_8d):
        day = str(row.get("created_at") or "")[:10]
        if not day:
            continue
        daily_cost[day] = daily_cost.get(day, 0.0) + cost_usd(
            str(row.get("model") or ""),
            int(row.get("tokens_in") or 0),
            int(row.get("tokens_out") or 0),
        )
    today_key = now.date().isoformat()
    today_cost = daily_cost.get(today_key, 0.0)
    prev7 = [
        v
        for d, v in daily_cost.items()
        if d != today_key and d >= (now - timedelta(days=7)).date().isoformat()
    ]
    avg7 = sum(prev7) / len(prev7) if prev7 else 0.0
    if today_cost > 1.0 and today_cost > 3 * avg7:
        items.append(
            _risk(
                "cost_spike",
                "medium",
                "오늘 LLM 비용 급증",
                f"오늘 추정 비용 ${today_cost:.2f} — 7일 일평균 ${avg7:.2f}의 3배 초과.",
                "cost",
                "",
                now.isoformat(),
                now,
                i18n={
                    "title_key": "risk.cost_spike.title",
                    "detail_key": "risk.cost_spike.detail",
                    "detail_params": {
                        "today": f"{today_cost:.2f}",
                        "avg": f"{avg7:.2f}",
                    },
                },
            )
        )

    # 5) trigger_failures — last_error가 남아있는 트리거
    for t in store.list_triggers():
        err_msg = str(t.get("last_error") or "").strip()
        if not err_msg:
            continue
        trig_at = t.get("last_fired_at") or t.get("created_at")
        items.append(
            _risk(
                "trigger_failure",
                "medium",
                f"트리거 오류: {t.get('id')}",
                f"마지막 발화 시 오류 — {err_msg[:160]}",
                "trigger",
                str(t.get("id") or ""),
                trig_at,
                now,
                i18n={
                    "title_key": "risk.trigger_failure.title",
                    "title_params": {"id": str(t.get("id") or "")},
                    "detail_key": "risk.trigger_failure.detail",
                    "detail_params": {"err": err_msg[:160]},
                },
            )
        )

    # 6) budget — 월 누적 비용 > monthly_budget_usd pref
    prefs = store.get_state("prefs", {})
    budget_raw = prefs.get("monthly_budget_usd") if isinstance(prefs, dict) else None
    try:
        budget = float(budget_raw) if budget_raw is not None else None
    except (TypeError, ValueError):
        budget = None
    if budget is not None and budget > 0:
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        mtd = 0.0
        for row in store.llm_usage_rows(month_start.isoformat()):
            mtd += cost_usd(
                str(row.get("model") or ""),
                int(row.get("tokens_in") or 0),
                int(row.get("tokens_out") or 0),
            )
        if mtd > budget:
            items.append(
                _risk(
                    "budget",
                    "high",
                    "월 예산 초과",
                    f"이번 달 추정 비용 ${mtd:.2f} > 예산 ${budget:.2f}.",
                    "cost",
                    "",
                    now.isoformat(),
                    now,
                    i18n={
                        "title_key": "risk.budget.title",
                        "detail_key": "risk.budget.detail",
                        "detail_params": {
                            "mtd": f"{mtd:.2f}",
                            "budget": f"{budget:.2f}",
                        },
                    },
                )
            )

    # 정렬: 심각도 → 최신순, 최대 50, type+ref 중복 제거
    seen: set[tuple[str, str, str]] = set()
    deduped: list[dict[str, Any]] = []
    for it in sorted(
        items,
        key=lambda x: (_SEV_RANK.get(str(x["severity"]), 3), -(x["age_seconds"] or 0)),
    ):
        k = (it["type"], it["ref_kind"], it["ref_id"])
        if k in seen:
            continue
        seen.add(k)
        deduped.append(it)
        if len(deduped) >= _MAX_ITEMS:
            break
    return {"items": deduped}
