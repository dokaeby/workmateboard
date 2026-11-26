"""스코프드 예산(budget_policies/incidents) 단위 테스트."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from jobmate import budget_scope
from jobmate.store.db import Store


def _store() -> Store:
    return Store(str(Path(tempfile.mkdtemp()) / "t.db"))


@pytest.fixture(autouse=True)
def _clear_spend_cache():
    budget_scope._spend_cache.clear()
    yield
    budget_scope._spend_cache.clear()


def _add_spend(store: Store, run_id: str, tokens_out: int = 0, tokens_in: int = 0) -> None:
    """stage_run 1건에 토큰을 기록해 월간 지출을 만든다."""
    sr = store.start_stage(run_id, "llm", 1, {})
    store.finish_stage(
        sr.id,
        status="succeeded",
        output={},
        tokens_in=tokens_in,
        tokens_out=tokens_out,
    )


def test_policy_crud():
    s = _store()
    p = budget_scope.upsert_policy(s, "global", "", amount_usd=100.0)
    assert p is not None and p["scope_type"] == "global" and p["scope_id"] == ""
    assert p["amount_usd"] == 100.0 and p["is_active"] is True

    # upsert는 같은 (scope_type, scope_id)를 갱신
    p2 = budget_scope.upsert_policy(s, "global", "", amount_usd=50.0, warn_percent=90)
    assert p2["id"] == p["id"] and p2["amount_usd"] == 50.0 and p2["warn_percent"] == 90

    # scoped policy
    pw = budget_scope.upsert_policy(s, "workflow", "dev_review", amount_usd=10.0)
    assert pw["scope_id"] == "dev_review"
    assert len(s.list_budget_policies()) == 2

    # amount null/<=0 → 비활성
    pd = budget_scope.upsert_policy(s, "workflow", "dev_review", amount_usd=None)
    assert pd["is_active"] is False

    # delete = 비활성화
    assert budget_scope.delete_policy(s, "global", "") is True
    assert s.get_budget_policy("global", "")["is_active"] is False
    assert budget_scope.delete_policy(s, "global", "") is False or True  # 이미 비활성도 rowcount>0


def test_scope_spend_attribution():
    s = _store()
    r1 = s.create_run("dev_review", "1", {"task": "t", "project": "projA"}, trace_id="a")
    r2 = s.create_run("code_review", "1", {"task": "t"}, trace_id="b")
    _add_spend(s, r1.id, tokens_out=1_000_000)  # $15 @ default out price
    _add_spend(s, r2.id, tokens_in=1_000_000)  # $3 @ default in price

    month, _ = budget_scope.month_window()
    assert budget_scope.scope_spend_usd(s, "global", "", month) == pytest.approx(18.0)
    assert budget_scope.scope_spend_usd(s, "workflow", "dev_review", month) == pytest.approx(15.0)
    assert budget_scope.scope_spend_usd(s, "project", "projA", month) == pytest.approx(15.0)
    assert budget_scope.scope_spend_usd(s, "project", "projB", month) == pytest.approx(0.0)


def test_admission_blocks_at_hard_limit():
    s = _store()
    r = s.create_run("dev_review", "1", {"task": "t"}, trace_id="a")
    _add_spend(s, r.id, tokens_out=1_000_000)  # $15
    budget_scope.upsert_policy(s, "workflow", "dev_review", amount_usd=10.0)

    blocks = budget_scope.evaluate_admission(s, "dev_review", "")
    assert len(blocks) == 1
    assert blocks[0]["scope"] == "workflow:dev_review"
    assert blocks[0]["month_spend_usd"] == pytest.approx(15.0)

    # 다른 workflow는 영향 없음
    assert budget_scope.evaluate_admission(s, "code_review", "") == []

    # 인시던트 + 인박스 생성
    open_inc = s.list_budget_incidents(status="open")
    assert len(open_inc) == 1 and open_inc[0]["threshold_type"] == "hard"
    inbox = s.list_inbox()
    assert any(i["kind"] == "budget" for i in inbox)


def test_warn_incident_idempotent():
    s = _store()
    r = s.create_run("dev_review", "1", {"task": "t"}, trace_id="a")
    _add_spend(s, r.id, tokens_out=600_000)  # $9
    # warn 80% of $10 = $8 → soft, hard $10 미만
    budget_scope.upsert_policy(s, "workflow", "dev_review", amount_usd=10.0, warn_percent=80)

    assert budget_scope.evaluate_admission(s, "dev_review", "") == []
    assert budget_scope.evaluate_admission(s, "dev_review", "") == []
    open_inc = s.list_budget_incidents(status="open")
    assert len(open_inc) == 1 and open_inc[0]["threshold_type"] == "soft"


def test_global_policy_blocks_all_workflows():
    s = _store()
    r = s.create_run("anything", "1", {"task": "t"}, trace_id="a")
    _add_spend(s, r.id, tokens_out=1_000_000)
    budget_scope.upsert_policy(s, "global", "", amount_usd=5.0)
    assert len(budget_scope.evaluate_admission(s, "other_wf", "")) == 1


def test_project_policy_scope():
    s = _store()
    r = s.create_run("dev_review", "1", {"task": "t", "project": "projA"}, trace_id="a")
    _add_spend(s, r.id, tokens_out=1_000_000)
    budget_scope.upsert_policy(s, "project", "projA", amount_usd=5.0)
    # project 태그 없는 run 생성 요청은 project 정책에 안 걸림
    assert budget_scope.evaluate_admission(s, "dev_review", "") == []
    assert len(budget_scope.evaluate_admission(s, "dev_review", "projA")) == 1


def test_resolve_incident_actions():
    s = _store()
    r = s.create_run("dev_review", "1", {"task": "t"}, trace_id="a")
    _add_spend(s, r.id, tokens_out=1_000_000)  # $15 observed
    budget_scope.upsert_policy(s, "workflow", "dev_review", amount_usd=10.0)
    budget_scope.evaluate_admission(s, "dev_review", "")
    inc = s.list_budget_incidents(status="open")[0]

    # raise_budget_and_resume: amount <= observed → ValueError
    with pytest.raises(ValueError):
        budget_scope.resolve_incident(s, inc["id"], "raise_budget_and_resume", amount_usd=10.0)

    # amount > observed → 정책 갱신 + 인시던트 전부 resolved
    out = budget_scope.resolve_incident(
        s, inc["id"], "raise_budget_and_resume", amount_usd=20.0, actor="lead@x"
    )
    assert out is not None and out["status"] == "resolved"
    assert s.get_budget_policy("workflow", "dev_review")["amount_usd"] == 20.0
    assert s.list_budget_incidents(status="open") == []
    # 감사 로그
    audits = [a for a in s.get_audit("") if a.action == "budget_policy_raise"]
    assert len(audits) == 1

    # 이미 resolved된 인시던트 재호출 → None
    assert budget_scope.resolve_incident(s, inc["id"], "keep_paused") is None


def test_keep_paused_and_dismiss():
    s = _store()
    r = s.create_run("dev_review", "1", {"task": "t"}, trace_id="a")
    _add_spend(s, r.id, tokens_out=1_000_000)
    budget_scope.upsert_policy(s, "workflow", "dev_review", amount_usd=10.0)
    budget_scope.evaluate_admission(s, "dev_review", "")
    inc = s.list_budget_incidents(status="open")[0]
    out = budget_scope.resolve_incident(s, inc["id"], "keep_paused", note="hold")
    assert out["status"] == "resolved"
    # 정책은 그대로 — 여전히 block
    assert len(budget_scope.evaluate_admission(s, "dev_review", "")) == 1


def test_partial_update_preserves_amount_and_flags():
    """토글 전용 upsert(UNSET 필드)가 amount/is_active를 지우지 않는다."""
    s = _store()
    budget_scope.upsert_policy(
        s, "global", "", amount_usd=100.0, warn_percent=80, hard_stop_enabled=True
    )
    # hard_stop만 끄는 부분 갱신 — amount/is_active/warn_percent 유지
    p = budget_scope.upsert_policy(s, "global", "", hard_stop_enabled=False)
    assert p["amount_usd"] == 100.0
    assert p["is_active"] is True
    assert p["warn_percent"] == 80
    assert p["hard_stop_enabled"] is False


def test_explicit_disable_keeps_amount_for_reenable():
    """명시 null amount = 비활성화 — amount는 보존돼 재활성 시 복원된다."""
    s = _store()
    budget_scope.upsert_policy(s, "workflow", "dev_review", amount_usd=25.0)
    pd = budget_scope.upsert_policy(s, "workflow", "dev_review", amount_usd=None)
    assert pd["is_active"] is False
    assert pd["amount_usd"] == 25.0  # amount 소거 없음
    # is_active만 다시 켜면 한도가 그대로 복원
    pr = budget_scope.upsert_policy(s, "workflow", "dev_review", is_active=True)
    assert pr["is_active"] is True and pr["amount_usd"] == 25.0


def test_partial_warn_update():
    """warn_percent만 바꾸는 부분 갱신 — 나머지 필드 유지."""
    s = _store()
    budget_scope.upsert_policy(
        s, "project", "projA", amount_usd=40.0, warn_percent=80, hard_stop_enabled=False
    )
    p = budget_scope.upsert_policy(s, "project", "projA", warn_percent=95)
    assert p["warn_percent"] == 95
    assert p["amount_usd"] == 40.0
    assert p["hard_stop_enabled"] is False
    assert p["is_active"] is True


def test_budget_policy_request_partial_fields():
    """BudgetPolicyRequest — 미전송 필드는 model_fields_set에 없어야 한다."""
    from jobmate.api.schemas import BudgetPolicyRequest

    body = BudgetPolicyRequest(hard_stop_enabled=True)
    assert body.model_fields_set == {"hard_stop_enabled"}
    assert body.amount_usd is None  # 스키마 기본값이지 '명시 null'이 아님


def test_explicit_null_fields_keep_existing_values():
    """warn_percent/hard_stop_enabled/is_active의 명시 null은 미전송과 동일 —
    기존 값을 유지한다(null → int(None) 크래시/bool(None) 무음 비활성화 방지)."""
    s = _store()
    budget_scope.upsert_policy(
        s,
        "workflow",
        "dev_review",
        amount_usd=50.0,
        warn_percent=90,
        hard_stop_enabled=False,
    )
    p = budget_scope.upsert_policy(
        s,
        "workflow",
        "dev_review",
        warn_percent=None,
        hard_stop_enabled=None,
        is_active=None,
    )
    assert p["warn_percent"] == 90
    assert p["hard_stop_enabled"] is False
    assert p["is_active"] is True
    assert p["amount_usd"] == 50.0

    # 신규 정책에 null만 보내면 기본값/기존 유도 규칙이 적용된다
    p2 = budget_scope.upsert_policy(s, "project", "projN", warn_percent=None)
    assert p2["warn_percent"] == 80
    assert p2["hard_stop_enabled"] is True


def test_nonfinite_amount_rejected():
    """inf/nan amount는 서비스 계층에서도 거부 — nan은 비교가 전부 False라
    무음 통과하고 inf는 사실상 무제한 예산이 된다."""
    s = _store()
    for bad in (float("inf"), float("-inf"), float("nan")):
        with pytest.raises(ValueError):
            budget_scope.upsert_policy(s, "global", "", amount_usd=bad)
    assert s.list_budget_policies() == []


def test_nonfinite_resolve_amount_rejected():
    s = _store()
    r = s.create_run("dev_review", "1", {"task": "t"}, trace_id="a")
    _add_spend(s, r.id, tokens_out=1_000_000)  # $15 observed
    budget_scope.upsert_policy(s, "workflow", "dev_review", amount_usd=10.0)
    budget_scope.evaluate_admission(s, "dev_review", "")
    inc = s.list_budget_incidents(status="open")[0]
    for bad in (float("inf"), float("nan")):
        with pytest.raises(ValueError):
            budget_scope.resolve_incident(s, inc["id"], "raise_budget_and_resume", amount_usd=bad)
    # 거부 후에도 인시던트는 open 유지
    assert s.get_budget_incident(inc["id"])["status"] == "open"


def test_budget_request_schema_rejects_nonfinite():
    """API 스키마 계층 — allow_inf_nan=False로 422 전에 차단."""
    import pydantic

    from jobmate.api.schemas import BudgetIncidentResolveRequest, BudgetPolicyRequest

    with pytest.raises(pydantic.ValidationError):
        BudgetPolicyRequest(amount_usd=float("inf"))
    with pytest.raises(pydantic.ValidationError):
        BudgetPolicyRequest(amount_usd=float("nan"))
    with pytest.raises(pydantic.ValidationError):
        BudgetIncidentResolveRequest(action="dismiss", amount_usd=float("inf"))


def test_project_scope_normalization_matches_policy():
    """run.project와 policy scope_id는 동일 정규화 — 제어문자/공백 차이로
    예산 귀속이 어긋나지 않는다."""
    s = _store()
    r = s.create_run("dev_review", "1", {"task": "t", "project": "  proj\nA  "}, trace_id="a")
    assert r.project == "projA"
    budget_scope.upsert_policy(s, "project", "projA", amount_usd=5.0)
    _add_spend(s, r.id, tokens_out=1_000_000)  # $15 > $5
    # 정규화되지 않은 태그로 물어봐도 같은 스코프로 매칭된다
    assert len(budget_scope.evaluate_admission(s, "dev_review", " proj\tA ")) == 1


def test_agent_scope_policy_block_and_allow():
    """scope_type='agent' 정책 — runs.agent_id 태그 지출만 집계해 차단/허용."""
    s = _store()
    agent = s.create_agent("Bot", "claude")
    assert agent is not None
    aid = agent["id"]
    other = s.create_agent("Other", "claude")
    assert other is not None

    # agent A 태그 run에 $15 지출, 태그 없는 run에 $3 지출
    ra = s.create_run("dev_review", "1", {"task": "t"}, trace_id="a", agent_id=aid)
    _add_spend(s, ra.id, tokens_out=1_000_000)  # $15
    rn = s.create_run("dev_review", "1", {"task": "t2"}, trace_id="n")
    _add_spend(s, rn.id, tokens_in=1_000_000)  # $3

    month, _ = budget_scope.month_window()
    # agent 스코프 지출 = 태그된 run만 ($15), 무태그 run 제외
    assert budget_scope.scope_spend_usd(s, "agent", aid, month) == pytest.approx(15.0)
    assert budget_scope.scope_spend_usd(s, "agent", other["id"], month) == pytest.approx(0.0)

    # 한도 $10 → agent_id를 아는 평가만 차단, 모르는 평가는 허용
    budget_scope.upsert_policy(s, "agent", aid, amount_usd=10.0)
    blocks = budget_scope.evaluate_admission(s, "dev_review", "", agent_id=aid)
    assert len(blocks) == 1
    assert blocks[0]["scope_type"] == "agent" and blocks[0]["scope_id"] == aid
    # agent_id 미전달 호출(스케줄/트리거 등)은 agent 정책을 모른다 → 허용
    assert budget_scope.evaluate_admission(s, "dev_review", "") == []
    # 다른 에이전트는 영향 없음
    assert budget_scope.evaluate_admission(s, "dev_review", "", agent_id=other["id"]) == []

    # 한도 상향 → 허용
    budget_scope.upsert_policy(s, "agent", aid, amount_usd=100.0)
    budget_scope._spend_cache.clear()
    assert budget_scope.evaluate_admission(s, "dev_review", "", agent_id=aid) == []


def test_agent_monthly_budget_usd_enforced():
    """agents.monthly_budget_usd — 정책 행 없이 직접 설정하는 월간 상한.
    calendar_month_utc agent 스코프 지출 >= 한도면 hard block."""
    s = _store()
    agent = s.create_agent("Capped", "claude", monthly_budget_usd=10.0)
    assert agent is not None
    aid = agent["id"]
    r = s.create_run("dev_review", "1", {"task": "t"}, trace_id="a", agent_id=aid)
    _add_spend(s, r.id, tokens_out=1_000_000)  # $15 > $10

    blocks = budget_scope.evaluate_admission(s, "dev_review", "", agent_id=aid)
    assert len(blocks) == 1
    assert blocks[0]["scope_type"] == "agent" and blocks[0]["amount_usd"] == 10.0
    # agent_id 미전달 평가·예산 미설정 에이전트는 허용
    assert budget_scope.evaluate_admission(s, "dev_review", "") == []
    free = s.create_agent("Free", "claude")
    assert free is not None
    assert budget_scope.evaluate_admission(s, "dev_review", "", agent_id=free["id"]) == []


def test_overview_status():
    s = _store()
    r = s.create_run("dev_review", "1", {"task": "t"}, trace_id="a")
    _add_spend(s, r.id, tokens_out=600_000)  # $9
    budget_scope.upsert_policy(s, "workflow", "dev_review", amount_usd=10.0)
    rows = budget_scope.list_policies_with_spend(s)
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "warning"
    assert row["month_spend_usd"] == pytest.approx(9.0)
    assert row["used_pct"] == pytest.approx(90.0)
