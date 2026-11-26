"""Wakeup coalescing — 동일 (workflow, canonical input) run의 단일 비행.

Paperclip agent_wakeup_requests의 단순 포팅. 모든 run 생성 요청은
wakeup_requests 행을 남기고(감사), 동일 scope의 active run이 있으면
새 run 대신 기존 run에 coalesce한다. force_new 요청은 deferred로
쌓았다가 활성 run 종료 시 promote한다.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from typing import Any

from . import budget_scope
from .models import AgentCapability, Run, RunStatus, canonical_agent_status
from .services import permissions as perm_svc
from .store.db import Store
from .store.util import _iso, _now

logger = logging.getLogger("jobmate.wakeups")

# run이 더 이상 재개되지 않는 상태 — deferred promote/ wakeup 마감 기준.
# waiting_approval/waiting_review는 사람 승인 대기라 비종결.
_TERMINAL_STATUSES = {
    RunStatus.COMPLETED.value,
    RunStatus.FAILED.value,
    RunStatus.REJECTED.value,
    RunStatus.CANCELLED.value,
    RunStatus.BUDGET_EXHAUSTED.value,
    RunStatus.NEEDS_HUMAN.value,
}
_ACTIVE_STATUSES = {RunStatus.QUEUED.value, RunStatus.RUNNING.value}

WAKEUP_SOURCES = ("api", "trigger", "schedule", "automation")


def canonical_input(input_data: dict[str, Any]) -> str:
    """코얼레싱 비교용 정규화 JSON — 키 정렬·공백 제거."""
    return json.dumps(
        input_data or {}, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )


def scope_key_for(
    workflow_key: str,
    input_data: dict[str, Any],
    agent_id: str = "",
    goal_id: str = "",
) -> str:
    """코얼레싱 scope — workflow + canonical input + agent_id + goal_id.

    agent_id/goal_id가 다르면 별도 scope — 동일 input이라도 다른 태그의
    run이 서로를 coalesce해 태그가 유실되는 것을 막는다. 둘 다 runs 컬럼에서
    오며 input_data에는 들어가지 않는다(명시 input.goal_id는 canonical
    input에 그대로 포함된다). input["goal"]은 create_run이 주입하는
    표시용 컨텍스트({id,title})라 scope 비교에서 제외한다 — 저장된
    run.input과 요청 input_data의 canonical 형태를 일치시키기 위함."""
    inp = {k: v for k, v in (input_data or {}).items() if k != "goal"}
    canon = canonical_input({"agent_id": agent_id or "", "goal_id": goal_id or "", "input": inp})
    digest = hashlib.sha256(canon.encode()).hexdigest()[:16]
    return f"{workflow_key}:{digest}"


def _payload_json(
    input_data: dict[str, Any],
    version: str,
    created_by: str,
    job_payload: dict[str, Any],
    agent_id: str = "",
    goal_id: str = "",
) -> str:
    return json.dumps(
        {
            "input": input_data,
            "version": version,
            "created_by": created_by,
            "job": job_payload,
            # deferred promote 시 runs.agent_id/goal_id 태그를 유지하기 위해 payload에 포함.
            "agent_id": agent_id,
            "goal_id": goal_id,
        },
        ensure_ascii=False,
        default=str,
    )


def _replay_result(store: Store, prev: dict[str, Any]) -> dict[str, Any]:
    """기존 wakeup 행에 대한 idempotent 재생 응답을 만든다."""
    rid = str(prev.get("run_id") or "")
    run = store.get_run(rid) if rid else None
    st = str(prev.get("status") or "")
    if st == "deferred" and not rid:
        # deferred wakeup은 아직 run이 없다 — 'coalesced'로
        # 응답하면 run_id=''가 돼 API가 {"id":"","status":"unknown"}
        # 을 반환한다. 대기 상태를 그대로 돌려준다.
        return {
            "action": "deferred",
            "run_id": "",
            "wakeup_id": str(prev.get("id") or ""),
            "run": None,
            "job_payload": None,
        }
    if st in ("completed", "failed") or (
        run is not None and run.status.value not in _ACTIVE_STATUSES
    ):
        return {
            "action": "replayed",
            "run_id": rid,
            "wakeup_id": str(prev.get("id") or ""),
            "run": run,
            "job_payload": None,
        }
    return {
        "action": "coalesced",
        "run_id": rid,
        "wakeup_id": str(prev.get("id") or ""),
        "run": run,
        "job_payload": None,
    }


def _conflict_result(prev: dict[str, Any]) -> dict[str, Any]:
    return {
        "action": "conflict",
        "run_id": "",
        "wakeup_id": str(prev.get("id") or ""),
        "run": None,
        "job_payload": None,
    }


def _create_wakeup_or_replay(
    store: Store,
    *,
    idempotency_key: str,
    scope_key: str,
    source: str,
    payload_json: str,
    status: str,
    run_id: str = "",
    orphan_run_id: str = "",
) -> dict[str, Any] | str:
    """wakeup INSERT — idempotency_key UNIQUE 위반 시 승자 행으로 replay.

    반환: 성공 시 wakeup id(str), 동시 삽입 충돌 시 replay result(dict).
    orphan_run_id는 run을 먼저 만들고 wakeup을 나중에 INSERT하는 경로의
    패자 run — 충돌 시 cancelled로 마감해 고아 queued run을 남기지 않는다."""
    try:
        return store.create_wakeup(
            scope_key=scope_key,
            source=source,
            payload=payload_json,
            status=status,
            idempotency_key=idempotency_key,
            run_id=run_id,
        )
    except store._integrity_errors:
        prev = store.get_wakeup_by_idempotency(idempotency_key) if idempotency_key else None
        if prev is None:
            raise
        if str(prev.get("scope_key") or "") != scope_key:
            # 동일 키·다른 입력의 동시 요청 — 재생하지 않고 충돌로 돌려준다.
            if orphan_run_id:
                try:
                    store.update_run(
                        orphan_run_id,
                        status=RunStatus.CANCELLED,
                        error="idempotency_conflict",
                    )
                except Exception:
                    logger.warning(
                        "orphan run %s cancel after idempotency conflict failed",
                        orphan_run_id,
                        exc_info=True,
                    )
            return _conflict_result(prev)
        if orphan_run_id:
            try:
                store.update_run(
                    orphan_run_id,
                    status=RunStatus.CANCELLED,
                    error="idempotency_conflict",
                )
            except Exception:
                logger.warning(
                    "orphan run %s cancel after idempotency conflict failed",
                    orphan_run_id,
                    exc_info=True,
                )
        logger.info(
            "idempotency_key concurrent insert conflict key=%s wakeup=%s",
            idempotency_key[:12],
            prev.get("id"),
        )
        return _replay_result(store, prev)


def _active_run_for_scope(
    store: Store,
    workflow_key: str,
    input_data: dict[str, Any],
    agent_id: str = "",
    goal_id: str = "",
) -> Run | None:
    """동일 scope의 queued/running run — 볼륨이 작아 Python 비교."""
    scope_key = scope_key_for(workflow_key, input_data, agent_id, goal_id)
    for run in store.list_active_runs():
        if run.workflow_key != workflow_key:
            continue
        if run.status.value not in _ACTIVE_STATUSES:
            continue
        if scope_key_for(workflow_key, run.input, run.agent_id, run.goal_id) == scope_key:
            return run
    return None


def admit_run(
    store: Store,
    *,
    workflow_key: str,
    input_data: dict[str, Any],
    source: str,
    idempotency_key: str = "",
    force_new: bool = False,
    created_by: str = "",
    version: str = "",
    job_payload: dict[str, Any] | None = None,
    agent_id: str = "",
    goal_id: str = "",
) -> dict[str, Any]:
    """run 생성 요청 접수 — 코얼레싱/디퍼 판정.

    반환 dict:
      action: 'created'|'coalesced'|'deferred'|'replayed'|'conflict'
      run_id: 생성/재사용 run id (deferred/conflict는 '')
      wakeup_id: 기록된 wakeup_requests id
      run: 'created'/'replayed' 시 Run 객체(없으면 None)
      job_payload: 'created' 시 실행에 넘길 payload

    - idempotency_key 재생: 기존 wakeup의 run을 그대로 반환.
      동일 키인데 scope_key(입력)가 다르면 'conflict' — 호출자는 409로 매핑.
    - 동일 scope active run 존재: coalesce(기본) 또는 deferred(force_new).
    - 그 외: run 생성 + wakeup 'queued'.
    """
    input_data = dict(input_data or {})
    # project 태그는 budget policy scope_id와 동일 정규화 — runs.project
    # 컬럼·예산 귀속·코얼레싱 scope_key가 모두 같은 값을 쓰게 한다.
    if isinstance(input_data.get("project"), str):
        input_data["project"] = budget_scope.clean_scope_id(input_data["project"])
    # "goal"/"goal_id"는 예약 키 — 호출자가 임의 값을 넣어도 scope/payload/
    # run.input에 새지 않게 제거한다("goal_id"는 아래에서 goal_id 파라미터로
    # 흡수). create_run도 동일하게 제거하므로 promote 경로와 형태가 일치한다.
    input_data = {k: v for k, v in input_data.items() if k != "goal"}
    # input_data.goal_id를 명시 goal_id 파라미터로 흡수 — runs.goal_id
    # 컬럼·scope_key·payload가 모두 같은 태그를 쓰게 하고, canonical
    # input에서는 goal_id 키를 제거해 create_run이 저장하는 input과
    # 형태를 일치시킨다.
    goal_explicit = bool((goal_id or "").strip())
    if "goal_id" in input_data:
        gv = input_data.get("goal_id")
        if isinstance(gv, str) and gv.strip():
            goal_explicit = True
            if not goal_id:
                goal_id = gv.strip()[:64]
        input_data = {k: v for k, v in input_data.items() if k != "goal_id"}
    # goal_id 해석은 create_run과 같은 단일 경로(resolve_run_goal_id) —
    # task effective goal·프로젝트 폴백 goal까지 scope_key/payload/run이
    # 일치해야 coalescing과 deferred promote가 깨지지 않는다(H1).
    # 명시 goal은 존재+비cancelled 검증(위반 시 거부), 폴백의 무효 goal은
    # 태그 해제(strip).
    goal_id = store.resolve_run_goal_id(input_data, goal_id)
    if goal_id:
        g = store.get_goal(goal_id)
        if g is None or str(g.get("status") or "") == "cancelled":
            if goal_explicit:
                return {
                    "action": "invalid_goal",
                    "run_id": "",
                    "wakeup_id": "",
                    "run": None,
                    "job_payload": None,
                    "error": "goal not found" if g is None else "goal is cancelled",
                }
            logger.warning(
                "admit_run: project-fallback goal %s unusable — stripped workflow=%s",
                goal_id,
                workflow_key,
            )
            goal_id = ""
    source = source if source in WAKEUP_SOURCES else "api"
    job = (
        dict(job_payload)
        if job_payload is not None
        else {"task": str(input_data.get("task") or "")}
    )
    payload_json = _payload_json(input_data, version, created_by, job, agent_id, goal_id)
    scope_key = scope_key_for(workflow_key, input_data, agent_id, goal_id)

    # a. idempotency replay — 동일 키라도 scope(입력)가 다르면 재생이 아니라 충돌
    if idempotency_key:
        prev = store.get_wakeup_by_idempotency(idempotency_key)
        if prev is not None:
            if str(prev.get("scope_key") or "") != scope_key:
                logger.warning(
                    "idempotency_key scope mismatch key=%s wakeup=%s",
                    idempotency_key[:12],
                    prev.get("id"),
                )
                return _conflict_result(prev)
            return _replay_result(store, prev)

    # b/c/d. 동일 scope active run 판정
    active = _active_run_for_scope(store, workflow_key, input_data, agent_id, goal_id)
    if active is not None and not force_new:
        res = _create_wakeup_or_replay(
            store,
            idempotency_key=idempotency_key,
            scope_key=scope_key,
            source=source,
            payload_json=payload_json,
            status="coalesced",
            run_id=active.id,
        )
        if isinstance(res, dict):
            return res
        wid = res
        store.bump_coalesced_for_run(active.id)
        logger.info(
            "run coalesced workflow=%s into run=%s wakeup=%s source=%s",
            workflow_key,
            active.id,
            wid,
            source,
        )
        return {
            "action": "coalesced",
            "run_id": active.id,
            "wakeup_id": wid,
            "run": active,
            "job_payload": None,
        }
    if active is not None and force_new:
        res = _create_wakeup_or_replay(
            store,
            idempotency_key=idempotency_key,
            scope_key=scope_key,
            source=source,
            payload_json=payload_json,
            status="deferred",
        )
        if isinstance(res, dict):
            return res
        wid = res
        logger.info(
            "run deferred workflow=%s behind run=%s wakeup=%s source=%s",
            workflow_key,
            active.id,
            wid,
            source,
        )
        return {
            "action": "deferred",
            "run_id": "",
            "wakeup_id": wid,
            "run": None,
            "job_payload": None,
        }

    # e. 신규 run 생성
    run = store.create_run(
        workflow_key,
        version,
        input_data,
        trace_id=uuid.uuid4().hex,
        created_by=created_by,
        agent_id=agent_id,
        goal_id=goal_id,
    )
    res = _create_wakeup_or_replay(
        store,
        idempotency_key=idempotency_key,
        scope_key=scope_key,
        source=source,
        payload_json=payload_json,
        status="queued",
        run_id=run.id,
        orphan_run_id=run.id,
    )
    if isinstance(res, dict):
        return res
    wid = res
    store.audit(
        run.id,
        source,
        "run_admitted",
        wid,
        {"wakeup_id": wid, "source": source, "coalesced": False},
    )
    logger.info(
        "run admitted id=%s workflow=%s wakeup=%s source=%s",
        run.id,
        workflow_key,
        wid,
        source,
    )
    return {
        "action": "created",
        "run_id": run.id,
        "wakeup_id": wid,
        "run": run,
        "job_payload": job,
    }


def on_run_finished(
    store: Store,
    run_id: str,
    schedule: Any = None,
) -> list[tuple[Run, dict[str, Any]]]:
    """run 종결 시 wakeup 마감 + deferred 1건 promote.

    schedule(run, job_payload) 콜백이 주어지면 생성된 run의 실행을
    예약한다(없으면 run은 queued로 남는다 — 호출자 책임).
    반환: promote로 생성된 (run, job_payload) 목록."""
    run = store.get_run(run_id)
    if run is None:
        return []
    status = run.status.value
    if status not in _TERMINAL_STATUSES:
        return []
    wstatus = "completed" if status == RunStatus.COMPLETED.value else "failed"
    store.finish_wakeups_for_run(run_id, wstatus)

    created: list[tuple[Run, dict[str, Any]]] = []
    # 단일 비행: scope당 활성 run 1개 — 가장 오래된 deferred 1건만 promote.
    # scope_key는 (workflow, input, agent_id, goal_id) — finishing run의
    # 태그로 조회하므로 다른 태그의 deferred는 해당 run 종결 시 promote된다.
    for w in store.list_deferred_wakeups(
        scope_key_for(run.workflow_key, run.input, run.agent_id, run.goal_id), limit=10
    ):
        try:
            payload = json.loads(str(w.get("payload") or ""))
        except (ValueError, TypeError):
            payload = None
        inp = payload.get("input") if isinstance(payload, dict) else None
        if not isinstance(inp, dict):
            # payload 파싱 실패/구형식 — run.input을 복제해 "추측 실행"하면
            # 다른 작업이 side effect와 함께 돌고 감사가 어긋난다.
            # 재시도해도 복구되지 않으므로 wakeup을 failed로 마감한다.
            store.fail_wakeup(str(w["id"]))
            logger.warning(
                "deferred wakeup %s failed: payload missing input workflow=%s",
                w["id"],
                run.workflow_key,
            )
            continue
        input_data = dict(inp)
        w_agent_id = str(payload.get("agent_id") or "")
        w_goal_id = str(payload.get("goal_id") or "")
        # 아직 같은 scope의 활성 run이 있으면 promote 보류
        if (
            _active_run_for_scope(store, run.workflow_key, input_data, w_agent_id, w_goal_id)
            is not None
        ):
            continue
        # defer 이후 대상 에이전트가 terminal(archived/terminated)이면
        # promote하지 않는다 — POST /runs의 terminal 거부와 동일 규칙.
        if w_agent_id:
            w_agent = store.get_agent(w_agent_id)
            if (
                w_agent is not None
                and canonical_agent_status(w_agent.get("status")) == "terminated"
            ):
                store.fail_wakeup(str(w["id"]))
                logger.warning(
                    "deferred wakeup %s failed: agent %s archived workflow=%s",
                    w["id"],
                    w_agent_id,
                    run.workflow_key,
                )
                continue
        # defer 이후 목표가 cancelled됐으면 promote하지 않는다 — admit_run의
        # cancelled goal 거부/strip과 동일 규칙을 promote 경로에도 적용.
        # (missing goal은 create_run이 strip하므로 여기서는 cancelled만 마감)
        if w_goal_id:
            w_goal = store.get_goal(w_goal_id)
            if w_goal is not None and str(w_goal.get("status") or "") == "cancelled":
                store.fail_wakeup(str(w["id"]))
                logger.warning(
                    "deferred wakeup %s failed: goal %s cancelled workflow=%s",
                    w["id"],
                    w_goal_id,
                    run.workflow_key,
                )
                continue
        # 예산 재심사 — defer 시점 통과 후 한도가 소진됐을 수 있다.
        # 차단 시 이 wakeup은 budget_exceeded로 마감하고 run을 만들지 않는다.
        # agent-scope 정책/agents.monthly_budget_usd도 여기서 재심사된다.
        if budget_scope.evaluate_admission(
            store,
            run.workflow_key,
            str(input_data.get("project") or ""),
            agent_id=w_agent_id,
        ):
            with store.tx() as c:
                c.execute(
                    "UPDATE wakeup_requests SET status='budget_exceeded', finished_at=?"
                    " WHERE id=? AND status='deferred'",
                    (_iso(_now()), str(w["id"])),
                )
            logger.warning(
                "deferred wakeup %s blocked by budget workflow=%s",
                w["id"],
                run.workflow_key,
            )
            continue
        # defer 이후 agent permission(run:execute)이 회수됐으면 promote하지
        # 않는다 — 생성 시점(POST /runs, wake_agent)과 동일한 서버 재검증을
        # promote 경로에도 적용한다(spec 3차 D TOCTOU). task 컨텍스트는
        # input.meta.task_id에서 해석한다.
        if w_agent_id:
            from .services.agent_management import ManagementError

            _w_meta_task = ""
            _w_meta = input_data.get("meta")
            if isinstance(_w_meta, dict):
                _w_meta_task = str(_w_meta.get("task_id") or "")
            try:
                perm_svc.require_permission(
                    store,
                    w_agent_id,
                    AgentCapability.RUN_EXECUTE.value,
                    task_id=_w_meta_task,
                    actor="wakeup-promote",
                )
            except ManagementError as e:
                store.fail_wakeup(str(w["id"]))
                logger.warning(
                    "deferred wakeup %s failed: permission %s agent=%s",
                    w["id"],
                    e.code,
                    w_agent_id,
                )
                continue
        if not store.claim_deferred_wakeup(str(w["id"])):
            continue
        try:
            new_run = store.create_run(
                run.workflow_key,
                str(payload.get("version") or run.workflow_version),
                input_data,
                trace_id=uuid.uuid4().hex,
                created_by=str(payload.get("created_by") or "wakeup"),
                agent_id=w_agent_id,
                goal_id=w_goal_id,
            )
        except Exception:
            # 생성 실패 시 deferred로 복원 — 다음 종결 이벤트에 재시도
            with store.tx() as c:
                c.execute(
                    "UPDATE wakeup_requests SET status='deferred', claimed_at=NULL WHERE id=?",
                    (w["id"],),
                )
            raise
        job = payload.get("job")
        job_payload = (
            dict(job) if isinstance(job, dict) else {"task": str(input_data.get("task") or "")}
        )
        store.set_wakeup_run(str(w["id"]), new_run.id)
        store.audit(
            new_run.id,
            "system",
            "wakeup_promoted",
            str(w["id"]),
            {"from_run": run_id, "wakeup_id": w["id"]},
        )
        logger.info(
            "deferred wakeup promoted wakeup=%s -> run=%s workflow=%s",
            w["id"],
            new_run.id,
            run.workflow_key,
        )
        if schedule is not None:
            try:
                schedule(new_run, job_payload)
            except Exception:
                logger.warning("wakeup schedule callback failed run=%s", new_run.id, exc_info=True)
        created.append((new_run, job_payload))
        break  # 1건만 — 나머지는 새 run 종결 시 이어서 promote
    return created
