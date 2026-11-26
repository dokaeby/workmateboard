"""스코프드 예산 — global/workflow/project/agent/task 정책 평가·인시던트.

Paperclip budget_policies/budget_incidents 포팅 + 2차 microusd 원장 연동.

지출 권위는 cost_events.cost_microusd 정수 합계다(paperclip spec Global
Constraint — 금액 계산에 float 금지). cost_events가 아직 없는 stage_runs는
uncovered_stage_spend_rows로 찾아 rate card(pricing.cost_microusd,
순수 정수)로 환산해 합산한다 — 원장 이벤트와 stage_run_id로 연결돼 있어
이중 계상되지 않는다. *_usd 필드는 모두 microusd/1e6 파생 표시값이다.

월간 지출은 60초 인메모리 캐시를 둔다(app.py _mtd_llm_cost_usd와 동일 패턴).
"""

from __future__ import annotations

import logging
import math
import time
from datetime import UTC, datetime
from typing import Any

from .pricing import MICROUSD_PER_USD, cost_microusd
from .store.db import Store
from .store.util import clean_scope_id

logger = logging.getLogger("jobmate.budget")

SCOPE_TYPES = ("global", "workflow", "project", "agent", "task")
WINDOW_KIND = "calendar_month_utc"
_WINDOW_KINDS = ("calendar_month_utc", "lifetime")
_UNPRICED_POLICIES = ("block", "allow")

# 부분 갱신에서 "필드 미전송"과 "명시 null"을 구분하는 sentinel.
# upsert_policy의 각 파라미터 기본값 — 미전송이면 기존 행 값을 유지한다.
UNSET: Any = object()


_SPEND_CACHE_TTL_S = 60.0
# (scope_type, scope_id, month, window_kind) → (monotonic_ts, microusd:int)
_spend_cache: dict[tuple[str, str, str, str], tuple[float, int]] = {}


def month_window(now: datetime | None = None) -> tuple[str, str]:
    """(month 'YYYY-MM', window_start ISO) — calendar_month_utc 기준."""
    now = now or datetime.now(UTC)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return now.strftime("%Y-%m"), start.isoformat()


def _month_start_iso(month: str) -> str:
    """'YYYY-MM' → 해당 월 1일 00:00 UTC ISO. 파싱 실패 시 이번 달 시작."""
    try:
        y, m = month.split("-", 1)
        return datetime(int(y), int(m), 1, tzinfo=UTC).isoformat()
    except (ValueError, TypeError):
        return month_window()[1]


def _usd_to_microusd(v: Any) -> int:
    """레거시 *_usd 값을 microusd 정수로 변환 — 마이그레이션/표시 경계 전용.

    런타임 금액 계산은 cost_microusd 정수 경로를 쓰고, 이 함수는 기존
    float 컬럼(amount_usd, monthly_budget_usd)을 읽을 때만 호출한다."""
    try:
        return max(0, int(round(float(v) * MICROUSD_PER_USD)))
    except (TypeError, ValueError):
        return 0


def _strict_nonneg_int(v: Any, name: str) -> int:
    """microusd 입력 검증 — bool/float/str/음수 거부."""
    if not isinstance(v, int) or isinstance(v, bool):
        raise ValueError(f"{name} must be an integer")
    if v < 0:
        raise ValueError(f"{name} must be >= 0")
    return v


def scope_spend_microusd(
    store: Store,
    scope_type: str,
    scope_id: str = "",
    month: str | None = None,
    window_kind: str = WINDOW_KIND,
) -> int:
    """스코프의 기간 지출 microusd 정수 — 원장 + 미커버 stage_rows 합계.

    - cost_events.cost_microusd 합계(NULL=unpriced 제외 — '모름'은 지출이
      아니며 unpriced_usage_policy가 별도로 다룬다).
    - cost_events가 없는 stage_runs는 rate card로 환산해 합산(이중 계상
      방지: stage_run_id로 연결된 원장 이벤트가 있는 stage_run은 제외).
    - window_kind='lifetime'이면 기간 하한 없이 전체 합계.
    """
    month_key, start_iso = month_window()
    if month:
        month_key = month
        start_iso = _month_start_iso(month_key)
    wk = (window_kind or WINDOW_KIND).strip().lower()
    if wk not in _WINDOW_KINDS:
        wk = WINDOW_KIND
    since = "" if wk == "lifetime" else start_iso
    cache_key = (scope_type, scope_id, month_key, wk)
    hit = _spend_cache.get(cache_key)
    now_ts = time.monotonic()
    if hit is not None and now_ts - hit[0] < _SPEND_CACHE_TTL_S:
        return hit[1]
    mu = store.cost_events_spend_microusd(since, scope_type, scope_id)
    for r in store.uncovered_stage_spend_rows(since, scope_type, scope_id):
        # cost_microusd(model, tokens_in, tokens_out) — stage_runs는 캐시
        # 토큰을 별도 기록하지 않으므로 3인자 호출.
        mu += cost_microusd(
            str(r.get("model") or ""),
            int(r.get("tokens_in") or 0),
            int(r.get("tokens_out") or 0),
        )
    _spend_cache[cache_key] = (now_ts, mu)
    if len(_spend_cache) > 500:
        _spend_cache.clear()
    return mu


def scope_spend_usd(
    store: Store, scope_type: str, scope_id: str = "", month: str | None = None
) -> float:
    """스코프의 당월 비용 USD — microusd 권위값의 표시용 파생(레거시 호환)."""
    return scope_spend_microusd(store, scope_type, scope_id, month) / MICROUSD_PER_USD


def _scope_label(policy: dict[str, Any]) -> str:
    st = str(policy.get("scope_type") or "")
    sid = clean_scope_id(str(policy.get("scope_id") or ""))
    if st == "global":
        return "전체(global)"
    return f"{st}:{sid}"


def _relevant_policies(
    store: Store,
    workflow_key: str,
    project: str,
    agent_id: str = "",
    task_id: str = "",
) -> list[dict[str, Any]]:
    """global + 매칭 workflow/project/agent/task의 active 정책."""
    out = []
    for p in store.list_budget_policies(active_only=True):
        st = str(p.get("scope_type") or "")
        sid = str(p.get("scope_id") or "")
        if st == "global":
            out.append(p)
        elif st == "workflow" and sid == workflow_key:
            out.append(p)
        elif st == "project" and project and sid == project:
            out.append(p)
        elif st == "agent" and agent_id and sid == agent_id:
            out.append(p)
        elif st == "task" and task_id and sid == task_id:
            out.append(p)
    return out


def _policy_amount_microusd(policy: dict[str, Any]) -> int:
    """정책 한도 microusd — amount_microusd>0이면 권위값, 아니면 amount_usd 파생."""
    mu = policy.get("amount_microusd")
    try:
        mu_i = int(mu) if mu is not None else 0
    except (TypeError, ValueError):
        mu_i = 0
    if mu_i > 0:
        return mu_i
    return _usd_to_microusd(policy.get("amount_usd"))


def _window_since_iso(window_kind: str, month_start_iso: str) -> str:
    """정책 window_kind에 맞는 지출 조회 하한 — lifetime은 전체."""
    return "" if window_kind == "lifetime" else month_start_iso


def evaluate_admission(
    store: Store,
    workflow_key: str,
    project: str = "",
    agent_id: str = "",
    task_id: str = "",
) -> list[dict[str, Any]]:
    """run 생성 전 예산 심사 — 차단 정책 목록 반환(빈 리스트면 허용).

    모든 비교는 microusd 정수로 수행한다:
    - 한도: amount_microusd>0이면 그 값, 아니면 amount_usd*1e6 파생.
    - 지출: 원장 microusd 합계 + 미커버 stage_rows rate-card 환산.
    - reservation_microusd는 새 run 시작 가능 여부에만 반영된다
      (spend + reservation >= amount이면 차단) — 실제 지출로 기록되지 않는다.
    - unpriced_usage_policy='block'이고 윈도우 내 unpriced 이벤트가 있으면
      지출 한도와 무관하게 hard_stop을 생성한다(hard_stop_enabled와 독립 —
      'block' 자체가 명시적 중지 정책).
    - warn_percent/hard 임계 인시던트는 기존과 동일하게 멱등 생성된다.

    agent_id를 알면 agent 스코프 정책과 agents.monthly_budget_microusd
    (monthly_budget_usd보다 우선 — spec 2차)도 함께 심사한다."""
    # 호출자가 넘기는 project 태그도 policy scope_id와 동일 정규화 —
    # runs.project 컬럼 생성 시 정규화와 매칭 규칙을 맞춘다.
    project = clean_scope_id(project)
    agent_id = clean_scope_id(agent_id)
    task_id = clean_scope_id(task_id)
    month, window_start = month_window()
    blocks: list[dict[str, Any]] = []
    for p in _relevant_policies(store, workflow_key, project, agent_id, task_id):
        amount_mu = _policy_amount_microusd(p)
        if amount_mu <= 0:
            continue
        st = str(p.get("scope_type") or "global")
        sid = clean_scope_id(str(p.get("scope_id") or ""))
        wk = str(p.get("window_kind") or WINDOW_KIND)
        spend_mu = scope_spend_microusd(store, st, sid, month, wk)
        reservation_mu = int(p.get("reservation_microusd") or 0)
        amount_usd = amount_mu / MICROUSD_PER_USD
        spend_usd = spend_mu / MICROUSD_PER_USD
        used_pct = round(spend_mu / amount_mu * 100, 1) if amount_mu else 0.0
        warn_limit_mu = amount_mu * int(p.get("warn_percent") or 80) // 100
        # hard stop: 관측 지출 + 예약분이 한도에 도달하거나, block 정책에서
        # unpriced 사용량이 윈도우 내 존재.
        hard_limit = spend_mu + max(0, reservation_mu) >= amount_mu
        unpriced_n = 0
        hard_unpriced = False
        if str(p.get("unpriced_usage_policy") or "allow") == "block":
            unpriced_n = store.unpriced_cost_event_count(
                _window_since_iso(wk, window_start), st, sid
            )
            hard_unpriced = unpriced_n > 0
        hard = bool(p.get("hard_stop_enabled")) and hard_limit
        if hard_unpriced:
            hard = True
        reason_code = ""
        if hard_unpriced:
            reason_code = "unpriced_usage"
        elif hard_limit and hard:
            reason_code = "limit_exceeded"
        soft = spend_mu >= warn_limit_mu
        if soft or hard:
            threshold = "hard" if hard else "soft"
            iid = store.create_budget_incident(
                str(p["id"]),
                st,
                sid,
                window_start,
                threshold,
                amount_usd,
                spend_usd,
                note=(
                    f"{_scope_label(p)} {month} spend=${spend_usd:.4f} "
                    f"limit=${amount_usd:.4f} spend_microusd={spend_mu} "
                    f"reservation_microusd={reservation_mu}"
                ),
                observed_spend_microusd=spend_mu,
                unpriced_event_count=unpriced_n,
                reason_code=reason_code,
            )
            if iid is not None:
                try:
                    store.upsert_inbox_item(
                        kind="budget",
                        title=f"예산 임계 초과: {_scope_label(p)}",
                        body=(
                            f"{month} 추정 지출 ${spend_usd:.4f} / "
                            f"한도 ${amount_usd:.4f} ({used_pct}%) — {threshold}"
                        ),
                        link="/?view=cost",
                        dedupe_key=f"budget:{p['id']}:{month}:{threshold}",
                        reset_read=True,
                    )
                except Exception as e:
                    logger.warning("budget inbox item failed: %s", str(e)[:200])
                logger.warning(
                    "budget %s threshold=%s scope=%s spend_microusd=%d "
                    "reservation_microusd=%d limit_microusd=%d unpriced=%d",
                    threshold,
                    st,
                    sid,
                    spend_mu,
                    reservation_mu,
                    amount_mu,
                    unpriced_n,
                )
        if hard:
            blocks.append(
                {
                    "policy_id": p["id"],
                    "scope_type": st,
                    "scope_id": sid,
                    "scope": _scope_label(p),
                    "amount_usd": amount_usd,
                    "month_spend_usd": round(spend_usd, 6),
                    "amount_microusd": amount_mu,
                    "month_spend_microusd": spend_mu,
                    "reservation_microusd": max(0, reservation_mu),
                    "unpriced_events": unpriced_n,
                    "reason_code": reason_code,
                    "used_pct": used_pct,
                }
            )
    # agents.monthly_budget_microusd — 정책 행 없이 에이전트 레지스트리에
    # 직접 설정하는 월간 상한. microusd>0이면 usd보다 우선한다(spec 2차).
    # calendar_month_utc 윈도우·agent 스코프 지출과 동일 기준으로
    # hard block만 적용한다(인시던트/인박스는 정책 전용).
    if agent_id:
        agent = store.get_agent(agent_id)
        mb_mu = int((agent or {}).get("monthly_budget_microusd") or 0)
        limit_mu = mb_mu if mb_mu > 0 else _usd_to_microusd((agent or {}).get("monthly_budget_usd"))
        if limit_mu > 0:
            spend_mu = scope_spend_microusd(store, "agent", agent_id, month)
            if spend_mu >= limit_mu:
                spend_usd = spend_mu / MICROUSD_PER_USD
                logger.warning(
                    "agent monthly_budget exceeded agent=%s spend_microusd=%d limit_microusd=%d",
                    agent_id,
                    spend_mu,
                    limit_mu,
                )
                blocks.append(
                    {
                        "policy_id": "",
                        "scope_type": "agent",
                        "scope_id": agent_id,
                        "scope": f"agent:{(agent or {}).get('name') or agent_id}",
                        "amount_usd": limit_mu / MICROUSD_PER_USD,
                        "month_spend_usd": round(spend_usd, 6),
                        "amount_microusd": limit_mu,
                        "month_spend_microusd": spend_mu,
                        "reservation_microusd": 0,
                        "unpriced_events": 0,
                        "reason_code": "limit_exceeded",
                        "used_pct": round(spend_mu / limit_mu * 100, 1),
                    }
                )
    return blocks


# ---------------- CRUD helpers ----------------


def upsert_policy(
    store: Store,
    scope_type: str,
    scope_id: str = "",
    *,
    amount_usd: Any = UNSET,
    amount_microusd: Any = UNSET,
    warn_percent: Any = UNSET,
    hard_stop_enabled: Any = UNSET,
    is_active: Any = UNSET,
    reservation_microusd: Any = UNSET,
    unpriced_usage_policy: Any = UNSET,
    window_kind: Any = UNSET,
) -> dict[str, Any] | None:
    """정책 부분 upsert — UNSET(미전송) 필드는 기존 행 값을 유지한다.

    amount 의미론(dual-field — amount_microusd가 권위):
    - amount_microusd 전송: 그 값이 한도. amount_usd는 표시용 파생으로 기록.
    - amount_usd만 전송: amount_microusd는 usd*1e6 파생.
    - 둘 다 미전송: 기존 한도 유지. amount_usd 명시 null(단 microusd 미전송)은
      기존 의미대로 비활성화 — 한도는 보존된다.
    - 둘 다 전송: amount_microusd 우선.

    reservation_microusd/unpriced_usage_policy/window_kind도 UNSET이면 기존
    값을 유지하고, 명시 null은 미전송과 동일하게 취급한다(무음 비활성화 방지).
    """
    scope_type = scope_type.strip().lower()
    if scope_type not in SCOPE_TYPES:
        return None
    scope_id = "" if scope_type == "global" else clean_scope_id(scope_id)
    existing = store.get_budget_policy(scope_type, scope_id)

    # 명시 null → UNSET 정규화
    if warn_percent is None:
        warn_percent = UNSET
    if hard_stop_enabled is None:
        hard_stop_enabled = UNSET
    if is_active is None:
        is_active = UNSET
    if amount_microusd is None:
        amount_microusd = UNSET
    if reservation_microusd is None:
        reservation_microusd = UNSET
    if unpriced_usage_policy is None:
        unpriced_usage_policy = UNSET
    if window_kind is None:
        window_kind = UNSET

    # --- 한도 해석 (microusd 권위) ---
    # usd_field_sent는 "필드 전송 여부" — 명시 null(amount_usd=None)도
    # 전송으로 취급해 비활성화 의미를 유지한다(UNSET과 구분).
    mu_sent = amount_microusd is not UNSET
    usd_field_sent = amount_usd is not UNSET
    usd_sent = usd_field_sent and amount_usd is not None
    if mu_sent:
        amount_mu = _strict_nonneg_int(amount_microusd, "amount_microusd")
        amount = amount_mu / MICROUSD_PER_USD
    elif usd_sent:
        amount = float(amount_usd)
        # inf/nan은 pydantic을 우회한 내부 호출에서도 차단 — nan은 모든
        # 비교가 False라 무음 통과하고, inf는 사실상 무제한 예산이 된다.
        if not math.isfinite(amount):
            raise ValueError("amount_usd must be a finite number")
        amount_mu = _usd_to_microusd(amount)
    else:
        # amount_usd 미전송 또는 명시 null(단 microusd 미전송) —
        # 기존 한도(amount_usd/amount_microusd)를 그대로 보존한다.
        amount = float((existing or {}).get("amount_usd") or 0)
        amount_mu = int((existing or {}).get("amount_microusd") or 0) or _usd_to_microusd(amount)

    if is_active is not UNSET:
        active = bool(is_active)
    elif usd_field_sent and not mu_sent and amount_usd is None:
        # 명시 null amount_usd( microusd 미전송) → 비활성 — 한도는 보존.
        active = False
    elif mu_sent or usd_sent:
        # 양수 한도 전송 → 활성.
        active = amount_mu > 0
    elif existing is None:
        active = amount_mu > 0
    else:
        active = bool(existing.get("is_active"))

    # --- 나머지 신규 필드 해석 ---
    if reservation_microusd is not UNSET:
        reservation_mu = _strict_nonneg_int(reservation_microusd, "reservation_microusd")
    else:
        reservation_mu = int((existing or {}).get("reservation_microusd") or 0)
    if unpriced_usage_policy is not UNSET:
        upp = str(unpriced_usage_policy).strip().lower()
        if upp not in _UNPRICED_POLICIES:
            raise ValueError("unpriced_usage_policy must be 'block' or 'allow'")
    else:
        upp = str((existing or {}).get("unpriced_usage_policy") or "allow")
    if window_kind is not UNSET:
        wk = str(window_kind).strip().lower()
        if wk not in _WINDOW_KINDS:
            raise ValueError("window_kind must be 'calendar_month_utc' or 'lifetime'")
    else:
        wk = str((existing or {}).get("window_kind") or WINDOW_KIND)

    return store.upsert_budget_policy(
        scope_type,
        scope_id,
        amount_usd=amount,
        amount_microusd=amount_mu,
        reservation_microusd=reservation_mu,
        unpriced_usage_policy=upp,
        window_kind=wk,
        warn_percent=(
            int(warn_percent)
            if warn_percent is not UNSET
            else int((existing or {}).get("warn_percent") or 80)
        ),
        hard_stop_enabled=(
            bool(hard_stop_enabled)
            if hard_stop_enabled is not UNSET
            else bool((existing or {}).get("hard_stop_enabled", True))
        ),
        is_active=bool(active),
    )


def delete_policy(store: Store, scope_type: str, scope_id: str = "") -> bool:
    """DELETE = 비활성화(이력 보존)."""
    return store.deactivate_budget_policy(scope_type, scope_id)


def list_policies_with_spend(store: Store) -> list[dict[str, Any]]:
    """GET /budgets/overview용 — 정책 + 당월 지출/사용률/인시던트 수 (microusd 권위)."""
    month, window_start = month_window()
    out = []
    for p in store.list_budget_policies():
        st = str(p.get("scope_type") or "global")
        sid = str(p.get("scope_id") or "")
        amount_mu = _policy_amount_microusd(p)
        reservation_mu = int(p.get("reservation_microusd") or 0)
        wk = str(p.get("window_kind") or WINDOW_KIND)
        since = _window_since_iso(wk, window_start)
        spend_mu = scope_spend_microusd(store, st, sid, month, wk)
        unpriced_n = store.unpriced_cost_event_count(since, st, sid)
        amount_usd = amount_mu / MICROUSD_PER_USD
        spend_usd = spend_mu / MICROUSD_PER_USD
        used_pct = round(spend_mu / amount_mu * 100, 1) if amount_mu > 0 else 0.0
        if not p.get("is_active"):
            status = "disabled"
        elif amount_mu <= 0:
            status = "disabled"
        elif spend_mu + max(0, reservation_mu) >= amount_mu:
            status = "hard_stop"
        elif spend_mu >= amount_mu * int(p.get("warn_percent") or 80) / 100:
            status = "warning"
        else:
            status = "ok"
        open_incidents = len(store.open_budget_incidents_for_policy(str(p["id"])))
        out.append(
            {
                "id": p["id"],
                "scope_type": st,
                "scope_id": sid,
                "amount_usd": amount_usd,
                "amount_microusd": amount_mu,
                "reservation_microusd": max(0, reservation_mu),
                "unpriced_usage_policy": str(p.get("unpriced_usage_policy") or "allow"),
                "window_kind": wk,
                "warn_percent": int(p.get("warn_percent") or 80),
                "hard_stop_enabled": bool(p.get("hard_stop_enabled")),
                "is_active": bool(p.get("is_active")),
                "month_spend_usd": round(spend_usd, 6),
                "month_spend_microusd": spend_mu,
                "unpriced_events": unpriced_n,
                "used_pct": used_pct,
                "status": status,
                "open_incidents": open_incidents,
            }
        )
    return out


def resolve_incident(
    store: Store,
    incident_id: str,
    action: str,
    *,
    amount_usd: float | None = None,
    amount_microusd: int | None = None,
    note: str = "",
    actor: str = "user",
) -> dict[str, Any] | None:
    """인시던트 해결.

    - keep_paused: 해당 인시던트만 resolved.
    - raise_budget_and_resume: 새 한도가 관측 지출보다 커야 함
      (microusd 우선 — observed_spend_microusd, 없으면 amount_observed 파생),
      정책 amount 갱신 + 정책의 open 인시던트 전부 resolved + 감사 로그.
    - dismiss: 해당 인시던트만 dismissed.
    """
    inc = store.get_budget_incident(incident_id)
    if inc is None or inc.get("status") != "open":
        return None
    action = (action or "").strip().lower()
    if action == "keep_paused":
        store.resolve_budget_incident(incident_id, "resolved", note=note)
        return store.get_budget_incident(incident_id)
    if action == "dismiss":
        store.resolve_budget_incident(incident_id, "dismissed", note=note)
        return store.get_budget_incident(incident_id)
    if action != "raise_budget_and_resume":
        return None
    observed_mu = inc.get("observed_spend_microusd")
    observed_mu_i = (
        int(observed_mu)
        if isinstance(observed_mu, int) and not isinstance(observed_mu, bool)
        else _usd_to_microusd(inc.get("amount_observed"))
    )
    if amount_microusd is not None:
        new_mu = _strict_nonneg_int(amount_microusd, "amount_microusd")
    elif amount_usd is not None:
        new_amount_f = float(amount_usd)
        if not math.isfinite(new_amount_f):
            raise ValueError("amount_usd must be a finite number")
        new_mu = _usd_to_microusd(new_amount_f)
    else:
        new_mu = 0
    if new_mu <= observed_mu_i:
        raise ValueError("new budget must be greater than observed spend")
    policy = store.get_budget_policy(
        str(inc.get("scope_type") or "global"), str(inc.get("scope_id") or "")
    )
    if policy is None:
        return None
    store.upsert_budget_policy(
        str(inc.get("scope_type") or "global"),
        str(inc.get("scope_id") or ""),
        amount_usd=new_mu / MICROUSD_PER_USD,
        amount_microusd=new_mu,
        warn_percent=int(policy.get("warn_percent") or 80),
        hard_stop_enabled=bool(policy.get("hard_stop_enabled", True)),
        is_active=True,
        reservation_microusd=int(policy.get("reservation_microusd") or 0),
        unpriced_usage_policy=str(policy.get("unpriced_usage_policy") or "allow"),
        window_kind=str(policy.get("window_kind") or WINDOW_KIND),
    )
    store.resolve_open_budget_incidents_for_policy(
        str(inc.get("policy_id") or ""), "resolved", note=note or "budget raised"
    )
    store.audit(
        "",
        actor,
        "budget_policy_raise",
        str(inc.get("policy_id") or ""),
        {
            "scope_type": inc.get("scope_type"),
            "scope_id": inc.get("scope_id"),
            "amount_usd": new_mu / MICROUSD_PER_USD,
            "amount_microusd": new_mu,
            "observed": observed_mu_i / MICROUSD_PER_USD,
            "observed_microusd": observed_mu_i,
            "incident_id": incident_id,
        },
    )
    logger.info(
        "budget policy %s raised to %d microusd (observed %d) by %s",
        inc.get("policy_id"),
        new_mu,
        observed_mu_i,
        actor,
    )
    return store.get_budget_incident(incident_id)
