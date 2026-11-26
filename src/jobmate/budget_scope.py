"""스코프드 예산 — global/project/workflow 월간 USD 정책 평가·인시던트.

Paperclip budget_policies의 단순 포팅. 지출은 stage_runs×runs 조인의
토큰×모델 단가(pricing.cost_usd)로 환산하며, 스코프 귀속은
workflow→runs.workflow_key, project→runs.project, global→전체다.
월간 지출은 60초 인메모리 캐시를 둔다(app.py _mtd_llm_cost_usd와 동일 패턴).
"""

from __future__ import annotations

import logging
import math
import time
from datetime import UTC, datetime
from typing import Any

from .pricing import cost_usd
from .store.db import Store
from .store.util import clean_scope_id

logger = logging.getLogger("jobmate.budget")

SCOPE_TYPES = ("global", "workflow", "project", "agent")
WINDOW_KIND = "calendar_month_utc"

# 부분 갱신에서 "필드 미전송"과 "명시 null"을 구분하는 sentinel.
# upsert_policy의 각 파라미터 기본값 — 미전송이면 기존 행 값을 유지한다.
UNSET: Any = object()


_SPEND_CACHE_TTL_S = 60.0
# (scope_type, scope_id, month) → (monotonic_ts, usd)
_spend_cache: dict[tuple[str, str, str], tuple[float, float]] = {}


def month_window(now: datetime | None = None) -> tuple[str, str]:
    """(month 'YYYY-MM', window_start ISO) — calendar_month_utc 기준."""
    now = now or datetime.now(UTC)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return now.strftime("%Y-%m"), start.isoformat()


def scope_spend_usd(
    store: Store, scope_type: str, scope_id: str = "", month: str | None = None
) -> float:
    """스코프의 당월 LLM 비용(USD 추정). 60초 캐시."""
    month_key, start_iso = month_window()
    if month:
        month_key = month
        try:
            y, m = month_key.split("-", 1)
            start_iso = datetime(int(y), int(m), 1, tzinfo=UTC).isoformat()
        except (ValueError, TypeError):
            month_key, start_iso = month_window()
    cache_key = (scope_type, scope_id, month_key)
    hit = _spend_cache.get(cache_key)
    now_ts = time.monotonic()
    if hit is not None and now_ts - hit[0] < _SPEND_CACHE_TTL_S:
        return hit[1]
    usd = 0.0
    for r in store.budget_spend_rows(start_iso, scope_type, scope_id):
        usd += cost_usd(
            str(r.get("model") or ""),
            int(r.get("tokens_in") or 0),
            int(r.get("tokens_out") or 0),
        )
    _spend_cache[cache_key] = (now_ts, usd)
    if len(_spend_cache) > 500:
        _spend_cache.clear()
    return usd


def _scope_label(policy: dict[str, Any]) -> str:
    st = str(policy.get("scope_type") or "")
    sid = clean_scope_id(str(policy.get("scope_id") or ""))
    if st == "global":
        return "전체(global)"
    return f"{st}:{sid}"


def _relevant_policies(
    store: Store, workflow_key: str, project: str, agent_id: str = ""
) -> list[dict[str, Any]]:
    """global + 매칭 workflow + 매칭 project + 매칭 agent의 active 정책."""
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
    return out


def evaluate_admission(
    store: Store, workflow_key: str, project: str = "", agent_id: str = ""
) -> list[dict[str, Any]]:
    """run 생성 전 예산 심사 — 차단 정책 목록 반환(빈 리스트면 허용).

    각 관련 정책의 당월 지출을 계산해 warn/hard 임계 초과 인시던트를
    (멱등으로) 생성하고 인박스에 알린다. hard_stop_enabled이고
    month_spend >= amount_usd이면 blocking.

    agent_id를 알면 agent 스코프 정책(scope_id=agent_id)과
    agents.monthly_budget_usd(암묵 상한 — 정책 행 없이 직접 차단)도
    함께 심사한다."""
    # 호출자가 넘기는 project 태그도 policy scope_id와 동일 정규화 —
    # runs.project 컬럼 생성 시 정규화와 매칭 규칙을 맞춘다.
    project = clean_scope_id(project)
    agent_id = clean_scope_id(agent_id)
    month, window_start = month_window()
    blocks: list[dict[str, Any]] = []
    for p in _relevant_policies(store, workflow_key, project, agent_id):
        amount = float(p.get("amount_usd") or 0)
        if amount <= 0:
            continue
        st = str(p.get("scope_type") or "global")
        sid = clean_scope_id(str(p.get("scope_id") or ""))
        spend = scope_spend_usd(store, st, sid, month)
        used_pct = round(spend / amount * 100, 1) if amount else 0.0
        warn_limit = amount * int(p.get("warn_percent") or 80) / 100
        hard = bool(p.get("hard_stop_enabled")) and spend >= amount
        soft = spend >= warn_limit
        if soft or hard:
            threshold = "hard" if hard else "soft"
            iid = store.create_budget_incident(
                str(p["id"]),
                st,
                sid,
                window_start,
                threshold,
                amount,
                spend,
                note=f"{_scope_label(p)} {month} spend={spend:.4f} limit={amount:.4f}",
            )
            if iid is not None:
                try:
                    store.upsert_inbox_item(
                        kind="budget",
                        title=f"예산 임계 초과: {_scope_label(p)}",
                        body=(
                            f"{month} 추정 지출 ${spend:.4f} / "
                            f"한도 ${amount:.4f} ({used_pct}%) — {threshold}"
                        ),
                        link="/?view=cost",
                        dedupe_key=f"budget:{p['id']}:{month}:{threshold}",
                        reset_read=True,
                    )
                except Exception as e:
                    logger.warning("budget inbox item failed: %s", str(e)[:200])
                logger.warning(
                    "budget %s threshold=%s scope=%s spend=%.4f limit=%.4f",
                    threshold,
                    st,
                    sid,
                    spend,
                    amount,
                )
        if hard:
            blocks.append(
                {
                    "policy_id": p["id"],
                    "scope_type": st,
                    "scope_id": sid,
                    "scope": _scope_label(p),
                    "amount_usd": amount,
                    "month_spend_usd": round(spend, 6),
                    "used_pct": used_pct,
                }
            )
    # agents.monthly_budget_usd — budget_policies 행 없이 에이전트 레지스트리에
    # 직접 설정하는 월간 상한. calendar_month_utc 윈도우·agent 스코프 지출과
    # 동일 기준으로 hard block만 적용한다(인시던트/인박스는 정책 전용 —
    # 정책 행이 없으므로 생성하지 않는다).
    if agent_id:
        agent = store.get_agent(agent_id)
        mb = (agent or {}).get("monthly_budget_usd")
        try:
            mb_limit = float(mb) if mb is not None else 0.0
        except (TypeError, ValueError):
            mb_limit = 0.0
        if mb_limit > 0:
            spend = scope_spend_usd(store, "agent", agent_id, month)
            if spend >= mb_limit:
                logger.warning(
                    "agent monthly_budget exceeded agent=%s spend=%.4f limit=%.4f",
                    agent_id,
                    spend,
                    mb_limit,
                )
                blocks.append(
                    {
                        "policy_id": "",
                        "scope_type": "agent",
                        "scope_id": agent_id,
                        "scope": f"agent:{(agent or {}).get('name') or agent_id}",
                        "amount_usd": mb_limit,
                        "month_spend_usd": round(spend, 6),
                        "used_pct": round(spend / mb_limit * 100, 1),
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
    warn_percent: Any = UNSET,
    hard_stop_enabled: Any = UNSET,
    is_active: Any = UNSET,
) -> dict[str, Any] | None:
    """정책 부분 upsert — UNSET(미전송) 필드는 기존 행 값을 유지한다.

    amount_usd 의미론:
    - UNSET: 기존 amount 유지.
    - 숫자: amount 갱신. is_active 미전송이면 amount>0으로 활성 판정.
    - None(명시 null): amount는 유지한 채 is_active=False로 비활성화
      (재활성 시 한도가 복원되도록 amount를 0으로 지우지 않는다).
    is_active 명시 전송 시 위 파생 규칙보다 우선한다.

    warn_percent/hard_stop_enabled/is_active의 명시 null은 "미전송"과
    동일하게 취급한다 — null을 bool()/int()로 변환하면 무음 비활성화나
    TypeError 크래시가 나므로, 이 필드들에서는 null=기존 값 유지가
    가장 안전한 의미론이다(amount_usd만 null이 '비활성화' 의미를 가진다)."""
    scope_type = scope_type.strip().lower()
    if scope_type not in SCOPE_TYPES:
        return None
    scope_id = "" if scope_type == "global" else clean_scope_id(scope_id)
    existing = store.get_budget_policy(scope_type, scope_id)

    # 명시 null → UNSET 정규화 (위 docstring 참조)
    if warn_percent is None:
        warn_percent = UNSET
    if hard_stop_enabled is None:
        hard_stop_enabled = UNSET
    if is_active is None:
        is_active = UNSET

    amount_sent = amount_usd is not UNSET
    if amount_sent and amount_usd is not None:
        amount = float(amount_usd)
        # inf/nan은 pydantic을 우회한 내부 호출에서도 차단 — nan은 모든
        # 비교가 False라 무음 통과하고, inf는 사실상 무제한 예산이 된다.
        if not math.isfinite(amount):
            raise ValueError("amount_usd must be a finite number")
    else:
        amount = float((existing or {}).get("amount_usd") or 0)

    if is_active is not UNSET:
        active = bool(is_active)
    elif amount_sent:
        # 명시 null/0 → 비활성( amount는 위에서 유지 ), 양수 → 활성
        active = amount_usd is not None and amount > 0
    elif existing is None:
        active = amount > 0
    else:
        active = bool(existing.get("is_active"))

    return store.upsert_budget_policy(
        scope_type,
        scope_id,
        amount_usd=amount,
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
    """GET /budgets/overview용 — 정책 + 당월 지출/사용률/인시던트 수."""
    month, _ws = month_window()
    out = []
    for p in store.list_budget_policies():
        st = str(p.get("scope_type") or "global")
        sid = str(p.get("scope_id") or "")
        amount = float(p.get("amount_usd") or 0)
        spend = scope_spend_usd(store, st, sid, month)
        used_pct = round(spend / amount * 100, 1) if amount > 0 else 0.0
        if not p.get("is_active"):
            status = "disabled"
        elif amount <= 0:
            status = "disabled"
        elif spend >= amount:
            status = "hard_stop"
        elif spend >= amount * int(p.get("warn_percent") or 80) / 100:
            status = "warning"
        else:
            status = "ok"
        open_incidents = len(store.open_budget_incidents_for_policy(str(p["id"])))
        out.append(
            {
                "id": p["id"],
                "scope_type": st,
                "scope_id": sid,
                "amount_usd": amount,
                "warn_percent": int(p.get("warn_percent") or 80),
                "hard_stop_enabled": bool(p.get("hard_stop_enabled")),
                "is_active": bool(p.get("is_active")),
                "month_spend_usd": round(spend, 6),
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
    note: str = "",
    actor: str = "user",
) -> dict[str, Any] | None:
    """인시던트 해결.

    - keep_paused: 해당 인시던트만 resolved.
    - raise_budget_and_resume: amount_usd > amount_observed 필수,
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
    observed = float(inc.get("amount_observed") or 0)
    new_amount = float(amount_usd) if amount_usd is not None else 0.0
    if not math.isfinite(new_amount):
        raise ValueError("amount_usd must be a finite number")
    if new_amount <= observed:
        raise ValueError("amount_usd must be greater than observed spend")
    policy = store.get_budget_policy(
        str(inc.get("scope_type") or "global"), str(inc.get("scope_id") or "")
    )
    if policy is None:
        return None
    store.upsert_budget_policy(
        str(inc.get("scope_type") or "global"),
        str(inc.get("scope_id") or ""),
        amount_usd=new_amount,
        warn_percent=int(policy.get("warn_percent") or 80),
        hard_stop_enabled=bool(policy.get("hard_stop_enabled", True)),
        is_active=True,
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
            "amount_usd": new_amount,
            "observed": observed,
            "incident_id": incident_id,
        },
    )
    logger.info(
        "budget policy %s raised to %.4f (observed %.4f) by %s",
        inc.get("policy_id"),
        new_amount,
        observed,
        actor,
    )
    return store.get_budget_incident(incident_id)
