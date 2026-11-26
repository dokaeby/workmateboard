"""비용 원장(cost_events) + microusd 예산 단위 테스트 (paperclip spec 2차).

- record_cost_event: 검증(정수 microusd만)·idempotency·unpriced 불변식
- emit_stage_cost_event / backfill_cost_events: stage_run당 1행, 멱등
- scope_spend_microusd/evaluate_admission: 원장+미커버 합산, reservation,
  unpriced block, agent monthly_budget_microusd 우선순위
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from jobmate import budget_scope, cost_ledger
from jobmate.pricing import PRICING_VERSION, cost_microusd
from jobmate.store.db import Store


def _store() -> Store:
    return Store(str(Path(tempfile.mkdtemp()) / "t.db"))


@pytest.fixture(autouse=True)
def _clear_spend_cache():
    budget_scope._spend_cache.clear()
    yield
    budget_scope._spend_cache.clear()


def _finished_stage(store: Store, run_id: str, tokens_in: int = 0, tokens_out: int = 0) -> str:
    sr = store.start_stage(run_id, "llm", 1, {})
    store.finish_stage(
        sr.id, status="succeeded", output={}, tokens_in=tokens_in, tokens_out=tokens_out
    )
    return sr.id


# ---------------- pricing ----------------


def test_cost_microusd_is_pure_int():
    # $3/$15 per 1M → 1M in + 1M out = $18 = 18,000,000 microusd
    mu = cost_microusd("claude", 1_000_000, 1_000_000)
    assert mu == 18_000_000
    assert isinstance(mu, int)
    # 소수 토큰 비율도 정수 floor — float 계산 없음
    # (1 in * 3M + 1 out * 15M) // 1M = 18 microusd
    assert cost_microusd("default", 1, 1) == 18
    assert cost_microusd("unknown-model", 1_000_000, 0) == 3_000_000


def test_pricing_usd_wrapper_matches():
    from jobmate.pricing import cost_usd

    assert cost_usd("claude", 1_000_000, 0) == pytest.approx(3.0)


# ---------------- record_cost_event ----------------


def test_record_cost_event_created_and_idempotent():
    s = _store()
    row, created = s.record_cost_event(
        idempotency_key="k1",
        agent_id="a1",
        cost_status="estimated",
        input_tokens=100,
        output_tokens=50,
        cost_microusd=123,
        pricing_version=PRICING_VERSION,
    )
    assert created is True and row is not None
    assert row["cost_microusd"] == 123 and row["cost_status"] == "estimated"
    # 동일 키 재전송 → 기존 행, created=False, 중복 없음
    row2, created2 = s.record_cost_event(
        idempotency_key="k1",
        cost_status="estimated",
        cost_microusd=999,
    )
    assert created2 is False and row2["id"] == row["id"]
    assert row2["cost_microusd"] == 123  # 첫 행 유지
    assert len(s.list_cost_events()) == 1


def test_record_cost_event_unpriced_null():
    s = _store()
    row, _ = s.record_cost_event(
        idempotency_key="u1",
        cost_status="unpriced",
        input_tokens=10,
        output_tokens=5,
    )
    assert row is not None and row["cost_microusd"] is None
    assert row["cost_status"] == "unpriced"


@pytest.mark.parametrize(
    "kwargs",
    [
        # float/str/bool/음수 microusd 거부
        {"cost_status": "estimated", "cost_microusd": 1.5},
        {"cost_status": "estimated", "cost_microusd": "100"},
        {"cost_status": "estimated", "cost_microusd": True},
        {"cost_status": "estimated", "cost_microusd": -1},
        # priced인데 NULL
        {"cost_status": "reported"},
        {"cost_status": "estimated"},
        # unpriced인데 금액
        {"cost_status": "unpriced", "cost_microusd": 0},
        # 토큰 검증
        {"cost_status": "unpriced", "input_tokens": -1},
        {"cost_status": "unpriced", "input_tokens": 1.5},
        {"cost_status": "unpriced", "output_tokens": True},
        # 잘못된 enum
        {"cost_status": "bogus"},
        {"cost_status": "estimated", "cost_microusd": 1, "billing_type": "bogus"},
    ],
)
def test_record_cost_event_validation(kwargs):
    s = _store()
    with pytest.raises(ValueError):
        s.record_cost_event(idempotency_key="bad", **kwargs)
    assert s.list_cost_events() == []


def test_record_cost_event_cost_zero_is_priced():
    """실제로 0원인 priced 이벤트는 cost_microusd=0으로 기록 가능 — NULL과 구분."""
    s = _store()
    row, created = s.record_cost_event(
        idempotency_key="z1",
        cost_status="estimated",
        cost_microusd=0,
    )
    assert created and row is not None and row["cost_microusd"] == 0


def test_list_cost_events_filters():
    s = _store()
    s.record_cost_event(
        idempotency_key="f1",
        agent_id="a1",
        model="claude-x",
        cost_status="estimated",
        cost_microusd=10,
        occurred_at="2026-01-05T00:00:00+00:00",
    )
    s.record_cost_event(
        idempotency_key="f2",
        agent_id="a2",
        model="gemini-y",
        cost_status="unpriced",
        input_tokens=3,
        occurred_at="2025-12-01T00:00:00+00:00",
    )
    assert len(s.list_cost_events(agent_id="a1")) == 1
    assert len(s.list_cost_events(model="gemini-y")) == 1
    assert len(s.list_cost_events(cost_status="unpriced")) == 1
    assert len(s.list_cost_events(occurred_from="2026-01-01")) == 1
    assert len(s.list_cost_events(occurred_to="2026-01-01")) == 1
    # 최신순 정렬
    items = s.list_cost_events()
    assert items[0]["idempotency_key"] == "f1"  # occurred_at DESC


def test_cost_summary():
    s = _store()
    s.record_cost_event(
        idempotency_key="s1",
        agent_id="a1",
        cost_status="estimated",
        cost_microusd=1000,
        occurred_at="2026-02-01T00:00:00+00:00",
    )
    s.record_cost_event(
        idempotency_key="s2",
        agent_id="a1",
        cost_status="unpriced",
        input_tokens=5,
        occurred_at="2026-02-02T00:00:00+00:00",
    )
    summ = s.cost_summary("agent", "a1")
    assert summ["total_microusd"] == 1000
    assert summ["event_count"] == 2
    assert summ["unpriced_count"] == 1
    assert summ["by_status"]["estimated"]["microusd"] == 1000


def test_cost_summary_by_agent():
    """M2 — by_agent 집계: ce.agent_id 우선, runs.agent_id 폴백,
    unpriced 이벤트만 있는 에이전트도 unpriced_count로 포함."""
    s = _store()
    s.record_cost_event(
        idempotency_key="ba1",
        agent_id="a1",
        cost_status="estimated",
        cost_microusd=1000,
    )
    s.record_cost_event(
        idempotency_key="ba2",
        agent_id="a1",
        cost_status="unpriced",
        input_tokens=5,
    )
    # agent_id 미태깅이지만 run.agent_id=a2로 귀속되는 이벤트
    run = s.create_run("dev_review", "1", {"task": "t"}, "tr-ba", agent_id="a2")
    s.record_cost_event(
        idempotency_key="ba3",
        run_id=run.id,
        agent_id="",
        cost_status="estimated",
        cost_microusd=500,
    )
    summ = s.cost_summary()
    by = {a["agent_id"]: a for a in summ["by_agent"]}
    assert by["a1"] == {"agent_id": "a1", "total_microusd": 1000, "unpriced_count": 1}
    assert by["a2"] == {"agent_id": "a2", "total_microusd": 500, "unpriced_count": 0}
    # total_microusd 내림차순 정렬
    assert summ["by_agent"][0]["agent_id"] == "a1"
    # agent 스코프 summary의 by_agent는 해당 에이전트만 반환
    summ_a1 = s.cost_summary("agent", "a1")
    assert [a["agent_id"] for a in summ_a1["by_agent"]] == ["a1"]


def test_cost_summary_unknown_scope_returns_zeros():
    """L3 — 알 수 없는 scope_type은 sibling 함수들과 동일하게 0행.
    global로 강제 변환해 전체 합계를 보여주지 않는다."""
    s = _store()
    s.record_cost_event(
        idempotency_key="g1",
        agent_id="a1",
        cost_status="estimated",
        cost_microusd=100,
    )
    summ = s.cost_summary("bogus", "x")
    assert summ["total_microusd"] == 0
    assert summ["event_count"] == 0
    assert summ["by_status"] == {}
    assert summ["by_agent"] == []
    # global은 여전히 정상 집계
    assert s.cost_summary()["total_microusd"] == 100


# ---------------- emit / backfill ----------------


def test_emit_stage_cost_event_estimated():
    s = _store()
    run = s.create_run("dev_review", "1", {"task": "t"}, trace_id="x")
    sid = _finished_stage(s, run.id, tokens_in=1000, tokens_out=500)
    row = cost_ledger.emit_stage_cost_event(
        s,
        run_id=run.id,
        stage_run_id=sid,
        model="claude-x",
        provider="anthropic",
        tokens_in=1000,
        tokens_out=500,
    )
    assert row is not None
    assert row["cost_status"] == "estimated"
    assert row["cost_microusd"] == cost_microusd("claude-x", 1000, 500)
    assert row["pricing_version"] == PRICING_VERSION
    assert row["billing_type"] == "metered_api"
    assert row["idempotency_key"] == f"cost:{sid}"
    assert row["run_id"] == run.id


def test_emit_stage_cost_event_unpriced_without_model():
    s = _store()
    run = s.create_run("dev_review", "1", {"task": "t"}, trace_id="x")
    sid = _finished_stage(s, run.id, tokens_in=10, tokens_out=5)
    row = cost_ledger.emit_stage_cost_event(
        s,
        run_id=run.id,
        stage_run_id=sid,
        model="",
        tokens_in=10,
        tokens_out=5,
    )
    assert row is not None
    assert row["cost_status"] == "unpriced" and row["cost_microusd"] is None
    assert row["billing_type"] == "unknown"


def test_emit_stage_cost_event_zero_tokens_noop():
    s = _store()
    assert (
        cost_ledger.emit_stage_cost_event(
            s, run_id="r", stage_run_id="s", model="claude", tokens_in=0, tokens_out=0
        )
        is None
    )
    assert s.list_cost_events() == []


def test_emit_stage_cost_event_idempotent_retry():
    s = _store()
    run = s.create_run("dev_review", "1", {"task": "t"}, trace_id="x")
    sid = _finished_stage(s, run.id, tokens_in=100, tokens_out=100)
    r1 = cost_ledger.emit_stage_cost_event(
        s, run_id=run.id, stage_run_id=sid, model="claude", tokens_in=100, tokens_out=100
    )
    r2 = cost_ledger.emit_stage_cost_event(
        s, run_id=run.id, stage_run_id=sid, model="claude", tokens_in=100, tokens_out=100
    )
    assert r1["id"] == r2["id"] and len(s.list_cost_events()) == 1


def test_backfill_cost_events_idempotent():
    s = _store()
    run = s.create_run("dev_review", "1", {"task": "t"}, trace_id="x")
    # model 없는 stage_run 2건 + model 있는 1건
    _finished_stage(s, run.id, tokens_in=0, tokens_out=0)
    sid_model = s.start_stage(run.id, "llm", 1, {}, model="claude")
    s.finish_stage(sid_model.id, status="succeeded", output={}, tokens_in=2000, tokens_out=1000)

    res1 = cost_ledger.backfill_cost_events(s)
    assert res1["created"] >= 1
    # 재실행해도 중복 없음 — stage_run_id 연결 행이 있으면 대상 제외
    res2 = cost_ledger.backfill_cost_events(s)
    assert res2["created"] == 0
    # 백필 행은 backfill: 키, model 있는 행은 estimated
    rows = s.list_cost_events()
    assert any(r["idempotency_key"].startswith("backfill:stage:") for r in rows)


def test_maybe_backfill_runs_once():
    s = _store()
    run = s.create_run("dev_review", "1", {"task": "t"}, trace_id="x")
    _finished_stage(s, run.id, tokens_in=100, tokens_out=100)
    cost_ledger.maybe_backfill_cost_events(s)
    assert len(s.list_cost_events()) == 1
    # done 가드 — 두 번째 호출은 추가 행 없음
    cost_ledger.maybe_backfill_cost_events(s)
    assert len(s.list_cost_events()) == 1


# ---------------- spend / admission ----------------


def test_scope_spend_microusd_ledger_plus_uncovered():
    s = _store()
    run = s.create_run("dev_review", "1", {"task": "t", "project": "pA"}, trace_id="a")
    sid = _finished_stage(s, run.id, tokens_in=1_000_000, tokens_out=0)  # $3 uncovered
    # 원장 이벤트 기록 → uncovered 합산에서 제외돼야 함
    cost_ledger.emit_stage_cost_event(
        s,
        run_id=run.id,
        stage_run_id=sid,
        model="claude",
        tokens_in=1_000_000,
        tokens_out=0,
    )
    month, _ = budget_scope.month_window()
    mu = budget_scope.scope_spend_microusd(s, "global", "", month)
    assert mu == cost_microusd("claude", 1_000_000, 0)
    # 이중 계상 없음 — 이벤트 1건만 존재
    assert len(s.list_cost_events()) == 1


def test_scope_spend_counts_unpriced_as_zero_but_flagged():
    s = _store()
    run = s.create_run("dev_review", "1", {"task": "t"}, trace_id="a")
    sid = _finished_stage(s, run.id, tokens_in=100, tokens_out=100)
    cost_ledger.emit_stage_cost_event(
        s, run_id=run.id, stage_run_id=sid, model="", tokens_in=100, tokens_out=100
    )
    month, _ = budget_scope.month_window()
    assert budget_scope.scope_spend_microusd(s, "global", "", month) == 0
    # unpriced 카운트는 별도로 추적
    _, ws = budget_scope.month_window()
    assert s.unpriced_cost_event_count(ws, "global", "") == 1


def test_admission_microusd_limit_and_reservation():
    s = _store()
    run = s.create_run("dev_review", "1", {"task": "t"}, trace_id="a")
    _finished_stage(s, run.id, tokens_out=1_000_000)  # $15 uncovered
    # amount_microusd 권위: $20 = 20,000,000
    p = budget_scope.upsert_policy(s, "workflow", "dev_review", amount_microusd=20_000_000)
    assert p["amount_microusd"] == 20_000_000
    # spend $15 + reservation $6 >= $20 → 차단
    budget_scope.upsert_policy(s, "workflow", "dev_review", reservation_microusd=6_000_000)
    budget_scope._spend_cache.clear()
    blocks = budget_scope.evaluate_admission(s, "dev_review", "")
    assert len(blocks) == 1
    assert blocks[0]["amount_microusd"] == 20_000_000
    assert blocks[0]["reservation_microusd"] == 6_000_000


def test_admission_unpriced_block_policy():
    s = _store()
    run = s.create_run("dev_review", "1", {"task": "t"}, trace_id="a")
    sid = _finished_stage(s, run.id, tokens_in=10, tokens_out=10)
    cost_ledger.emit_stage_cost_event(
        s, run_id=run.id, stage_run_id=sid, model="", tokens_in=10, tokens_out=10
    )
    # 한도는 넉넉하지만 unpriced=block → hard stop
    budget_scope.upsert_policy(
        s,
        "workflow",
        "dev_review",
        amount_microusd=1_000_000_000,
        unpriced_usage_policy="block",
    )
    blocks = budget_scope.evaluate_admission(s, "dev_review", "")
    assert len(blocks) == 1
    assert blocks[0]["reason_code"] == "unpriced_usage"
    # allow이면 차단 없음
    budget_scope.upsert_policy(s, "workflow", "dev_review", unpriced_usage_policy="allow")
    budget_scope._spend_cache.clear()
    assert budget_scope.evaluate_admission(s, "dev_review", "") == []


def test_agent_monthly_budget_microusd_precedence():
    s = _store()
    agent = s.create_agent("Cap", "claude", monthly_budget_usd=1000.0)
    assert agent is not None
    aid = agent["id"]
    # microusd가 usd보다 우선 — $1000 usd지만 microusd $5
    s.update_agent(aid, monthly_budget_microusd=5_000_000)
    run = s.create_run("dev_review", "1", {"task": "t"}, trace_id="a", agent_id=aid)
    _finished_stage(s, run.id, tokens_out=1_000_000)  # $15 > $5
    blocks = budget_scope.evaluate_admission(s, "dev_review", "", agent_id=aid)
    assert len(blocks) == 1
    assert blocks[0]["scope_type"] == "agent"
    assert blocks[0]["amount_microusd"] == 5_000_000


def test_task_scope_policy_and_admission():
    s = _store()
    run = s.create_run("dev_review", "1", {"task": "t"}, trace_id="a")
    task = s.create_task("T")
    assert task is not None
    tid = str(task["id"])
    # task 범위 정책 — task_id로 평가 시에만 매칭
    budget_scope.upsert_policy(s, "task", tid, amount_microusd=100)
    # task에 귀속된 원장 이벤트 $0.001 = 1000 microusd > 한도 100
    s.record_cost_event(
        idempotency_key="task-spend",
        task_id=tid,
        run_id=run.id,
        cost_status="estimated",
        cost_microusd=1000,
    )
    blocks = budget_scope.evaluate_admission(s, "dev_review", "", task_id=tid)
    assert len(blocks) == 1
    assert blocks[0]["scope_type"] == "task"
    # task_id 없으면 task 정책은 적용되지 않는다
    assert budget_scope.evaluate_admission(s, "dev_review", "") == []


def test_upsert_policy_microusd_rejects_bad_types():
    s = _store()
    for bad in (1.5, "100", True, -1):
        with pytest.raises(ValueError):
            budget_scope.upsert_policy(s, "global", "", amount_microusd=bad)
        with pytest.raises(ValueError):
            budget_scope.upsert_policy(s, "global", "", reservation_microusd=bad)


def test_upsert_policy_both_amounts_microusd_wins():
    s = _store()
    p = budget_scope.upsert_policy(s, "global", "", amount_usd=100.0, amount_microusd=7_500_000)
    assert p["amount_microusd"] == 7_500_000
    assert p["amount_usd"] == pytest.approx(7.5)


def test_lifetime_window_kind():
    s = _store()
    # 지난달 stage_run — calendar_month_utc면 집계 제외, lifetime이면 포함
    run = s.create_run("dev_review", "1", {"task": "t"}, trace_id="a")
    s.record_cost_event(
        idempotency_key="old1",
        run_id=run.id,
        model="claude",
        cost_status="estimated",
        cost_microusd=50_000_000,
        occurred_at="2020-01-15T00:00:00+00:00",
    )
    budget_scope.upsert_policy(s, "global", "", amount_microusd=100_000_000, window_kind="lifetime")
    month, _ = budget_scope.month_window()
    assert budget_scope.scope_spend_microusd(s, "global", "", month, "lifetime") == 50_000_000
    # calendar_month_utc 기본값은 이번 달만
    assert budget_scope.scope_spend_microusd(s, "global", "", month) == 0


# ---------------- API 스키마 ----------------


def test_cost_event_request_schema_strict():
    import pydantic

    from jobmate.api.schemas import CostEventCreateRequest

    # 정상
    body = CostEventCreateRequest(
        idempotency_key="k",
        cost_status="estimated",
        input_tokens=1,
        output_tokens=2,
        cost_microusd=3,
    )
    assert body.cost_microusd == 3
    # float/str/bool/음수 거부
    for bad in (1.5, "100", True, -1):
        with pytest.raises(pydantic.ValidationError):
            CostEventCreateRequest(idempotency_key="k", cost_status="estimated", cost_microusd=bad)
    for bad in (-1, 1.5, "5", True):
        with pytest.raises(pydantic.ValidationError):
            CostEventCreateRequest(idempotency_key="k", input_tokens=bad)


def test_budget_policy_request_microusd_fields():
    import pydantic

    from jobmate.api.schemas import BudgetIncidentResolveRequest, BudgetPolicyRequest

    body = BudgetPolicyRequest(
        amount_microusd=1000,
        reservation_microusd=50,
        unpriced_usage_policy="block",
        window_kind="lifetime",
    )
    assert body.amount_microusd == 1000 and body.unpriced_usage_policy == "block"
    for bad in (1.5, "100", True, -1):
        with pytest.raises(pydantic.ValidationError):
            BudgetPolicyRequest(amount_microusd=bad)
    with pytest.raises(pydantic.ValidationError):
        BudgetPolicyRequest(unpriced_usage_policy="bogus")
    with pytest.raises(pydantic.ValidationError):
        BudgetIncidentResolveRequest(action="dismiss", amount_microusd=-1)


def test_management_agent_microusd_strict_int():
    """L4 — monthly_budget_microusd는 다른 microusd 필드와 동일하게 StrictInt."""
    import pydantic

    from jobmate.api.schemas import (
        ManagementAgentCreateRequest,
        ManagementAgentPatchRequest,
    )

    ok = ManagementAgentCreateRequest(name="x", adapter_type="claude", monthly_budget_microusd=1000)
    assert ok.monthly_budget_microusd == 1000
    for bad in (1.5, "100", True, -1):
        with pytest.raises(pydantic.ValidationError):
            ManagementAgentCreateRequest(
                name="x", adapter_type="claude", monthly_budget_microusd=bad
            )
        with pytest.raises(pydantic.ValidationError):
            ManagementAgentPatchRequest(monthly_budget_microusd=bad)
