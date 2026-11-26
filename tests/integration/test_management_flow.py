"""management 1차 통합 테스트 — task→assign→wake→dev_review run→task_runs,
run 상태(waiting_approval/completed/failed)의 task/agent 투영."""

from jobmate.models import RunStatus
from jobmate.services import agent_management as mgmt
from jobmate.workflows.registry_builder import build_runner

from .helpers import default_outputs, make_env, scripted_llm


def _mk_agent(store, **kw):
    body = {"name": kw.pop("name", "dev"), "adapter_type": "claude", **kw}
    a = store.create_agent(
        body["name"],
        body["adapter_type"],
        kind=body.get("kind", "workflow"),
        adapter_config=body.get("adapter_config") or {"workflow": "dev_review"},
        status=body.get("status", "active"),
    )
    assert a is not None
    return a


def test_task_assign_wake_dev_review_links_task_runs():
    """task 생성 → workflow agent 배정 → wake → dev_review run 생성 → task_runs."""
    settings, store = make_env()
    llm = scripted_llm(default_outputs())
    agent = _mk_agent(store)
    task = mgmt.create_task(store, title="fix login bug", status="todo", actor="test")
    mgmt.assign_task(store, task["id"], agent["id"], actor="test")

    out = mgmt.wake_agent(store, agent["id"], task_id=task["id"], idempotency_key="it-wake-1")
    assert out["action"] == "created"
    run_id = out["run_id"]
    assert run_id

    # task_runs 연결 + task/agent 상태
    links = store.task_runs_for_task(task["id"])
    assert len(links) == 1 and links[0]["run_id"] == run_id
    run = store.get_run(run_id)
    assert run is not None
    assert run.agent_id == agent["id"]
    assert run.workflow_key == "dev_review"
    t = store.get_task(task["id"])
    assert t["status"] == "in_progress"
    assert t["current_run_id"] == run_id
    assert store.get_agent(agent["id"])["status"] == "running"

    # run input.meta에 관리 참조 — 기존 workflow 입력 형식(task) 유지
    assert run.input.get("task", "").startswith("[WMB-1]")
    assert run.input.get("meta", {}).get("task_id") == task["id"]

    # dev_review 실행 → waiting_approval 투영
    runner = build_runner("dev_review", store=store, llm=llm, settings=settings)
    runner.invoke(run_id, {"task": "fix login bug"})
    assert store.get_run(run_id).status == RunStatus.WAITING_APPROVAL
    t2 = store.get_task(task["id"])
    assert t2["status"] == "waiting_approval"
    # agent는 idle로 투영 (사람 결정 대기)
    assert store.get_agent(agent["id"])["status"] == "idle"

    # 승인 → completed 투영
    runner.resume(run_id, {"decision": "approve", "comment": "ok"})
    assert store.get_run(run_id).status == RunStatus.COMPLETED
    t3 = store.get_task(task["id"])
    assert t3["status"] == "done"
    assert t3["current_run_id"] is None


def test_failed_run_projects_task_blocked():
    settings, store = make_env()
    agent = _mk_agent(store)
    task = mgmt.create_task(store, title="will fail", status="todo")
    mgmt.assign_task(store, task["id"], agent["id"])
    out = mgmt.wake_agent(store, agent["id"], task_id=task["id"], idempotency_key="it-wake-2")
    run_id = out["run_id"]

    # run 실패 시뮬레이션 — runner 없이 상태만 failed로 (투영 hook과 동일 경로)
    store.update_run(run_id, status=RunStatus.FAILED, error="boom")
    mgmt.project_run_status(store, run_id)
    t = store.get_task(task["id"])
    assert t["status"] == "blocked"

    # blocked → in_progress 재개 가능 (task_runs에 두 번째 run 링크)
    out2 = mgmt.wake_agent(store, agent["id"], task_id=task["id"], idempotency_key="it-wake-3")
    # 동일 scope coalesce 가능 — 다른 idempotency key라도 active run 없으면 새 run
    # (첫 run은 failed terminal이라 coalesce 대상 아님)
    assert out2["action"] in ("created", "coalesced", "replayed")
    t2 = store.get_task(task["id"])
    # wake가 task를 다시 in_progress로 진행시킨다
    assert t2["status"] == "in_progress"


def test_unlinked_run_does_not_affect_tasks():
    """task_runs에 없는 일반 /runs 실행은 관리 task에 영향 없음."""
    settings, store = make_env()
    task = mgmt.create_task(store, title="unrelated", status="todo")
    # task_runs 링크 없이 run만 생성·실패 처리
    run = store.create_run("dev_review", "0.1.0", {"task": "x"}, "tr-3")
    store.update_run(run.id, status=RunStatus.FAILED, error="x")
    mgmt.project_run_status(store, run.id)
    assert store.get_task(task["id"])["status"] == "todo"
