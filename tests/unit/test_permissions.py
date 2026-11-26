"""agent_permissions 단위 테스트 — spec 3차 D 판정 규칙 + store CRUD.

외부 호출 없이 Store + services/permissions + services/agent_management만 사용.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from jobmate.services import agent_management as mgmt
from jobmate.services import permissions as perm_svc
from jobmate.store.db import Store


def _store() -> Store:
    db = str(Path(tempfile.mkdtemp()) / "t.db")
    return Store(db)


def _agent(store: Store, name: str = "dev", adapter_type: str = "claude", **kw) -> dict:
    agent = store.create_agent(name, adapter_type, **kw)
    assert agent is not None
    return agent


def _task(store: Store, title: str = "t", **kw) -> dict:
    task = store.create_task(title, **kw)
    assert task is not None
    return task


def _rule(cap: str, eff: str = "allow", st: str = "global", sid: str = "") -> dict:
    return {"capability": cap, "effect": eff, "scope_type": st, "scope_id": sid}


# ---------- store CRUD ----------


def test_store_replace_and_list():
    store = _store()
    agent = _agent(store)
    rules = [
        _rule("task:read", "allow"),
        _rule("external:send", "deny"),
        _rule("task:update", "deny", "task", "sometask"),
    ]
    saved = store.replace_agent_permissions(agent["id"], rules)
    assert len(saved) == 3
    assert {r["capability"] for r in saved} == {"task:read", "external:send", "task:update"}
    # 교체 의미론 — 두 번째 PUT은 첫 집합을 대체한다.
    saved2 = store.replace_agent_permissions(agent["id"], [_rule("agent:wake", "deny")])
    assert len(saved2) == 1
    assert saved2[0]["capability"] == "agent:wake"


def test_store_normalize_global_scope_id():
    store = _store()
    agent = _agent(store)
    saved = store.replace_agent_permissions(
        agent["id"], [_rule("task:read", "allow", "global", "should-be-cleared")]
    )
    assert saved[0]["scope_id"] == ""


# ---------- evaluate_permission ----------


def test_default_allow_for_normal_capability():
    store = _store()
    agent = _agent(store)
    d = perm_svc.evaluate_permission(store, agent["id"], "task:read")
    assert d["decision"] == "allow"
    assert d["effect"] == "default"


def test_default_deny_for_external_deploy_destructive():
    store = _store()
    agent = _agent(store)
    for cap in ("external:send", "deploy:execute", "destructive:execute"):
        d = perm_svc.evaluate_permission(store, agent["id"], cap)
        assert d["decision"] == "deny", cap


def test_explicit_allow_still_needs_approval_for_dangerous():
    store = _store()
    agent = _agent(store)
    store.replace_agent_permissions(agent["id"], [_rule("external:send", "allow")])
    d = perm_svc.evaluate_permission(store, agent["id"], "external:send")
    assert d["decision"] == "needs_approval"
    assert d["approval_action"] == "external_send"


def test_deny_wins_over_allow_same_and_wider_scope():
    store = _store()
    agent = _agent(store)
    task = _task(store)
    store.replace_agent_permissions(
        agent["id"],
        [
            _rule("task:update", "allow"),  # global allow
            _rule("task:update", "deny", "task", task["id"]),  # task deny
        ],
    )
    d = perm_svc.evaluate_permission(store, agent["id"], "task:update", task_id=task["id"])
    assert d["decision"] == "deny"
    assert d["scope_type"] == "task"


def test_scope_precedence_task_beats_project_beats_global_for_reporting():
    """deny 우선이므로 결정은 같지만, allow 판정의 scope 기록은 가장 좁은 것."""
    store = _store()
    agent = _agent(store)
    project = store.create_project("p1")
    assert project is not None
    task = _task(store, project_id=project["id"])
    store.replace_agent_permissions(
        agent["id"],
        [
            _rule("task:read", "allow", "global"),
            _rule("task:read", "allow", "project", project["id"]),
            _rule("task:read", "allow", "task", task["id"]),
        ],
    )
    d = perm_svc.evaluate_permission(store, agent["id"], "task:read", task_id=task["id"])
    assert d["decision"] == "allow"
    assert d["scope_type"] == "task"
    # 다른 task에서는 task 규칙이 매칭되지 않아 project scope가 보고된다.
    other = _task(store, project_id=project["id"])
    d2 = perm_svc.evaluate_permission(store, agent["id"], "task:read", task_id=other["id"])
    assert d2["decision"] == "allow"
    assert d2["scope_type"] == "project"


def test_project_scope_rule_does_not_match_other_project():
    store = _store()
    agent = _agent(store)
    p1 = store.create_project("pa")
    p2 = store.create_project("pb")
    assert p1 is not None and p2 is not None
    t2 = _task(store, project_id=p2["id"])
    store.replace_agent_permissions(agent["id"], [_rule("task:read", "deny", "project", p1["id"])])
    # p2 task에는 p1 project deny가 매칭되지 않는다 → default allow.
    d = perm_svc.evaluate_permission(store, agent["id"], "task:read", task_id=t2["id"])
    assert d["decision"] == "allow"
    # p1 project 컨텍스트에서는 deny.
    t1 = _task(store, project_id=p1["id"])
    d1 = perm_svc.evaluate_permission(store, agent["id"], "task:read", task_id=t1["id"])
    assert d1["decision"] == "deny"


def test_task_scope_resolves_project_via_task():
    """task_id만 넘겨도 task→project_id를 해석해 project 규칙이 매칭된다."""
    store = _store()
    agent = _agent(store)
    project = store.create_project("px")
    assert project is not None
    task = _task(store, project_id=project["id"])
    store.replace_agent_permissions(
        agent["id"], [_rule("task:update", "deny", "project", project["id"])]
    )
    d = perm_svc.evaluate_permission(store, agent["id"], "task:update", task_id=task["id"])
    assert d["decision"] == "deny"
    assert d["project_id"] == project["id"]


def test_identifier_task_id_resolves_to_canonical():
    """'WMB-n' identifier로 판정해도 uuid scope_id 규칙과 매칭된다."""
    store = _store()
    agent = _agent(store)
    task = _task(store)
    ident = str(task["identifier"])
    store.replace_agent_permissions(agent["id"], [_rule("task:update", "deny", "task", task["id"])])
    d = perm_svc.evaluate_permission(store, agent["id"], "task:update", task_id=ident)
    assert d["decision"] == "deny"
    assert d["task_id"] == task["id"]


# ---------- require_permission / approval ----------


def test_require_permission_denies_with_management_error():
    store = _store()
    agent = _agent(store)
    store.replace_agent_permissions(agent["id"], [_rule("run:execute", "deny")])
    with pytest.raises(mgmt.ManagementError) as ei:
        perm_svc.require_permission(store, agent["id"], "run:execute")
    assert ei.value.status_code == 403
    assert ei.value.code == "permission_denied"


def test_require_permission_needs_approval_then_approved():
    store = _store()
    agent = _agent(store)
    store.replace_agent_permissions(agent["id"], [_rule("deploy:execute", "allow")])
    # 승인 전 → 409 approval_required
    with pytest.raises(mgmt.ManagementError) as ei:
        perm_svc.require_permission(store, agent["id"], "deploy:execute")
    assert ei.value.status_code == 409
    assert ei.value.code == "approval_required"
    # agent resource에 대한 approved deploy 승인이 있으면 통과
    store.create_approval_request(
        action_type="deploy",
        risk_level="high",
        summary="ok",
        safe_payload={},
        requested_by="t",
        resource_type="agent",
        resource_id=agent["id"],
    )
    appr = store.find_pending_approval("deploy", "agent", agent["id"])
    assert appr is not None
    store.decide_approval_request(appr["id"], "approve", decided_by="lead")
    d = perm_svc.require_permission(store, agent["id"], "deploy:execute")
    assert d["decision"] == "needs_approval"


# ---------- enforcement (service level) ----------


def test_wake_denied_by_agent_wake_rule():
    store = _store()
    agent = _agent(store, name="w1")
    task = _task(store)
    store.replace_agent_permissions(agent["id"], [_rule("agent:wake", "deny")])
    with pytest.raises(mgmt.ManagementError) as ei:
        mgmt.wake_agent(store, agent["id"], task_id=task["id"], idempotency_key="k1")
    assert ei.value.status_code == 403
    assert ei.value.code == "permission_denied"


def test_wake_denied_by_run_execute_for_workflow_kind():
    store = _store()
    agent = _agent(store, name="w2")  # kind=workflow 기본
    task = _task(store)
    store.replace_agent_permissions(agent["id"], [_rule("run:execute", "deny")])
    with pytest.raises(mgmt.ManagementError) as ei:
        mgmt.wake_agent(store, agent["id"], task_id=task["id"], idempotency_key="k2")
    assert ei.value.code == "permission_denied"


def test_wake_task_assign_deny_blocks_task_bound_wake():
    store = _store()
    agent = _agent(store, name="w3", kind="external", adapter_type="custom")
    task = _task(store)
    store.replace_agent_permissions(agent["id"], [_rule("task:assign", "deny")])
    with pytest.raises(mgmt.ManagementError) as ei:
        mgmt.wake_agent(store, agent["id"], task_id=task["id"], idempotency_key="k3")
    assert ei.value.code == "permission_denied"


def test_wake_allowed_by_default():
    store = _store()
    agent = _agent(store, name="w4", kind="external", adapter_type="custom")
    task = _task(store)
    res = mgmt.wake_agent(store, agent["id"], task_id=task["id"], idempotency_key="k4")
    assert res["action"] == "wake_recorded"


def test_assign_task_denied_for_assignee():
    store = _store()
    agent = _agent(store, name="a1")
    task = _task(store)
    store.replace_agent_permissions(agent["id"], [_rule("task:assign", "deny")])
    with pytest.raises(mgmt.ManagementError) as ei:
        mgmt.assign_task(store, task["id"], agent["id"])
    assert ei.value.status_code == 403
    assert ei.value.code == "permission_denied"


def test_create_task_with_assignee_denied():
    store = _store()
    agent = _agent(store, name="a2")
    store.replace_agent_permissions(agent["id"], [_rule("task:assign", "deny")])
    with pytest.raises(mgmt.ManagementError) as ei:
        mgmt.create_task(store, title="x", assignee_agent_id=agent["id"])
    assert ei.value.code == "permission_denied"


def test_checkout_denied_by_task_update_rule():
    store = _store()
    agent = _agent(store, name="c1")
    task = _task(store)
    store.replace_agent_permissions(agent["id"], [_rule("task:update", "deny", "task", task["id"])])
    with pytest.raises(mgmt.ManagementError) as ei:
        mgmt.checkout_task(store, task["id"], agent["id"])
    assert ei.value.status_code == 403


def test_checkout_allowed_by_default():
    store = _store()
    agent = _agent(store, name="c2")
    task = _task(store)
    out = mgmt.checkout_task(store, task["id"], agent["id"])
    assert out["checkout_agent_id"] == agent["id"]


def test_patch_task_assignee_change_denied():
    store = _store()
    agent = _agent(store, name="p1")
    other = _agent(store, name="p2")
    task = _task(store)
    store.replace_agent_permissions(other["id"], [_rule("task:assign", "deny")])
    with pytest.raises(mgmt.ManagementError) as ei:
        mgmt.patch_task(store, task["id"], {"assignee_agent_id": other["id"]})
    assert ei.value.code == "permission_denied"
    # 기존 assignee(agent)에게는 deny가 없으므로 정상 배정 가능
    out = mgmt.patch_task(store, task["id"], {"assignee_agent_id": agent["id"]})
    assert out["assignee_agent_id"] == agent["id"]
