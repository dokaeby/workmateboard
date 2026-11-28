"""트리거 평가/발사 — agent_sessions 상태 전이를 enabled trigger로 자동화.

support.py에서 분리. automation loop(_automation_loop)과 cron tick이
_maybe_check_triggers를 통해 호출한다. run 실행은 jobs.py에 지연 import해
순환 import를 피한다.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from .. import messenger
from ..config import Settings
from ..engine.trace import job_traceable
from ..store import orgctx
from ..store.db import Store
from ..workflows.resolver import resolve_workflow_spec

worker_logger = logging.getLogger("jobmate.worker")

# ---------- triggers (세션 상태 전이 자동화) ----------
_TRIGGER_STATE_KEY = "trigger_session_states"
_TRIGGER_MIN_INTERVAL = 45.0
_TRIGGER_LAST_CHECK = 0.0


def _trigger_match(match: dict[str, Any], sess: dict[str, Any]) -> bool:
    """match {tool?,project?,state?} — 지정된 키만 정확 일치 비교."""
    tool = match.get("tool")
    if tool and sess.get("tool") != tool:
        return False
    project = match.get("project")
    if project and sess.get("project") != project:
        return False
    state = match.get("state")
    if state and (sess.get("status") or "") != state:
        return False
    return True


def _session_transition_fp(sess: dict[str, Any], state: str) -> str:
    """전이 지문 — 같은 상태 재진입도 새 이벤트면 다른 fp가 되도록
    last_event_at/updated_at을 포함한다."""
    return f"{state}:{sess.get('last_event_at') or sess.get('updated_at') or ''}"


def _evaluate_triggers(
    store: Store,
    settings: Settings,
    llm: Any,
    adapters: dict[str, Any],
    defer: list[tuple[str, str, dict[str, Any], dict[str, Any] | None]] | None = None,
) -> int:
    """현재 agent_sessions 스냅샷 vs 이전 스냅샷(automation_state) 비교.

    세션이 match 조건을 '만족하는 상태로 전이'할 때 enabled trigger를 발사한다.
    첫 관측(이전 스냅샷에 없는 세션)은 시드만 하고 발사하지 않아 기동 직후
    기존 세션에 대한 폭주를 막는다. 발사 멱등은 trigger_fires UNIQUE가 보장.
    반환: 이번 평가에서 발사된 trigger 수."""
    triggers = store.list_triggers(enabled_only=True)
    sessions = store.list_agent_sessions(limit=200, include_ended=True)
    prev = store.get_state(_TRIGGER_STATE_KEY, {})
    if not isinstance(prev, dict):
        prev = {}
    new_prev: dict[str, str] = {}
    fired = 0
    for sess in sessions:
        tool = str(sess.get("tool") or "")
        skey = str(sess.get("session_key") or "")
        if not tool or not skey:
            continue
        ident = f"{tool}\x1f{skey}"
        cur = str(sess.get("status") or "")
        new_prev[ident] = cur
        if ident not in prev or prev.get(ident) == cur:
            continue  # 첫 관측 또는 상태 변화 없음 — 전이 아님
        # spec 003 US3 — org가 다른 trigger×session 조합은 매칭하지 않는다.
        # ''(레거시/시스템 행)는 어느 조직과도 매칭 가능한 와일드카드.
        sess_org = str(sess.get("org_id") or "")
        for trig in triggers:
            trig_org = str(trig.get("org_id") or "")
            if sess_org and trig_org and sess_org != trig_org:
                continue
            # FR-031 — suspended/deleted 조직의 trigger는 발사하지 않는다.
            # claim_trigger_fire 전에 건너뛰어 trigger_fires를 소모하지
            # 않는다. 전이 자체는 new_prev에 기록되므로 같은 전이가
            # 재발사되지는 않는다(스케줄의 due 재시도와 달리 1회성 이벤트).
            if store.org_inactive(trig_org):
                worker_logger.info("trigger %s skipped: org %s inactive", trig["id"], trig_org)
                continue
            match = trig.get("match") or {}
            action = trig.get("action") or {}
            if not _trigger_match(match, sess):
                continue
            fp = _session_transition_fp(sess, cur)
            if not store.claim_trigger_fire(trig["id"], tool, skey, cur, fp):
                continue  # 동일 전이 재평가 — 이미 발사됨
            # spec 003 FR-019 — trigger 행의 org로 발사를 스코프한다.
            # admit_run→create_run·report·inbox·audit이 이 조직을 상속한다.
            with orgctx.org_scope(str(trig.get("org_id") or "")):
                try:
                    _fire_trigger_action(store, settings, llm, adapters, trig, sess, defer)
                except Exception as e:
                    store.mark_trigger_error(trig["id"], str(e)[:200])
                    worker_logger.warning("trigger %s fire failed: %s", trig["id"], str(e)[:200])
                    continue
                store.mark_trigger_fired(trig["id"])
                store.audit(
                    "",
                    "trigger",
                    f"trigger_fire:{trig['id']}",
                    ident,
                    {"action": str(action.get("type") or ""), "state": cur},
                )
                fired += 1
                worker_logger.info(
                    "trigger %s fired session=%s state=%s action=%s",
                    trig["id"],
                    ident,
                    cur,
                    action.get("type"),
                )
    store.set_state(_TRIGGER_STATE_KEY, new_prev)
    return fired


def _fire_trigger_action(
    store: Store,
    settings: Settings,
    llm: Any,
    adapters: dict[str, Any],
    trig: dict[str, Any],
    sess: dict[str, Any],
    defer: list[tuple[str, str, dict[str, Any], dict[str, Any] | None]] | None,
) -> None:
    """trigger action 실행 — run_workflow/run_playbook/report.

    run input/audit에는 세션 식별자(tool/session_key/project/status)만 넣고
    doing/ask/ended_summary 등 세션 본문은 절대 포함하지 않는다."""
    from ..reports import REPORT_KIND_LABELS, build_report

    action = trig.get("action") or {}
    atype = str(action.get("type") or "")
    tid = str(trig.get("id") or "")
    tool = str(sess.get("tool") or "")
    skey = str(sess.get("session_key") or "")
    sess_ref = {
        "tool": tool,
        "session_key": skey,
        "project": sess.get("project"),
        "status": sess.get("status"),
    }
    trigger_meta = {"id": tid, "session": sess_ref}

    def _launch(workflow: str, payload: dict[str, Any]) -> None:
        # 호출부(_fire_trigger_action)가 trigger 행의 org로 org_scope를 씌운
        # 상태 — 커스텀 spec은 그 조직 컨텍스트로 해석된다(spec 003 US3).
        wf_spec = resolve_workflow_spec(store, workflow)
        if wf_spec is None:
            raise ValueError(f"unknown workflow {workflow}")
        # 스코프드 예산 — hard_stop 초과 시 트리거 발사 보류(예외 없이 스킵)
        from .. import budget_scope, wakeups

        blocks = budget_scope.evaluate_admission(store, workflow, str(payload.get("project") or ""))
        if blocks:
            store.audit(
                "",
                "trigger",
                "trigger_budget_exceeded",
                tid,
                {"workflow": workflow, "scope": blocks[0]["scope"]},
            )
            worker_logger.warning(
                "trigger %s blocked: budget exceeded scope=%s", tid, blocks[0]["scope"]
            )
            return
        res = wakeups.admit_run(
            store,
            workflow_key=workflow,
            input_data=payload,
            source="trigger",
            created_by=f"trigger:{tid}",
            version=wf_spec.version,
            job_payload={"task": payload.get("task", "")},
        )
        if res["action"] == "invalid_goal":
            worker_logger.warning("trigger %s blocked: invalid goal %s", tid, res.get("error"))
            return
        if res["action"] != "created":
            worker_logger.info(
                "trigger %s %s into run=%s", tid, res["action"], res.get("run_id") or "-"
            )
            return
        run = res["run"]
        job = (run.id, workflow, {"task": payload.get("task", "")}, None)
        if defer is not None:
            defer.append(job)
        else:
            # jobs.py는 support를 import하므로 순환 방지로 지연 import
            from .jobs import _execute_run_job

            threading.Thread(
                target=_execute_run_job,
                args=(store, settings, llm, adapters, *job),
                daemon=True,
            ).start()
        worker_logger.info("trigger %s launched run=%s workflow=%s", tid, run.id, workflow)

    if atype == "run_workflow":
        payload: dict[str, Any] = {
            "task": f"trigger {tid}: {tool}/{skey[:12]}",
            "trigger": trigger_meta,
        }
        # 세션의 project 태그를 run input에 전파 — project 스코프 예산
        # 귀속과 _launch의 예산 평가가 세션 프로젝트를 보게 한다.
        if sess.get("project"):
            payload["project"] = str(sess["project"])
        _launch(str(action.get("workflow") or ""), payload)
    elif atype == "run_playbook":
        pb = store.get_playbook(str(action.get("playbook_id") or ""))
        if pb is None:
            raise ValueError("playbook not found")
        payload = dict(pb.get("input") or {})
        payload.setdefault("task", f"trigger {tid}: {pb.get('name') or pb['id']}")
        payload["trigger"] = {**trigger_meta, "playbook_id": pb["id"]}
        _launch(str(pb.get("workflow") or ""), payload)
    elif atype == "report":
        kind = str(action.get("report_kind") or "daily")
        if kind not in ("daily", "weekly"):
            kind = "daily"
        content = build_report(store, kind, settings)
        rid = store.save_report(kind, content)
        store.upsert_inbox_item(
            "report",
            f"{REPORT_KIND_LABELS.get(kind, kind)} 리포트(트리거)",
            body=(
                f"runs={content['runs']['total']} failed={content['runs']['failed']} "
                f"pending={content['approvals']['pending']}"
            ),
            link="/?view=reports",
            dedupe_key=f"trigger-report:{tid}:{tool}:{skey}:{sess.get('status')}",
        )
        try:
            messenger.notify(
                f"jobMate {REPORT_KIND_LABELS.get(kind, kind)} 리포트(트리거): "
                f"run {content['runs']['total']}건"
            )
        except Exception:
            worker_logger.debug("messenger notify after trigger report failed", exc_info=True)
        worker_logger.info("trigger %s generated report id=%s kind=%s", tid, rid, kind)
    else:
        raise ValueError(f"unknown trigger action type {atype}")


@job_traceable("jobmate.check_triggers")
def _maybe_check_triggers(
    store: Store,
    settings: Settings,
    llm: Any,
    adapters: dict[str, Any],
    defer: list[tuple[str, str, dict[str, Any], dict[str, Any] | None]] | None = None,
) -> None:
    """45초 스로틀로 trigger 평가 — automation loop(2s)와 cron tick 공용."""
    global _TRIGGER_LAST_CHECK
    now = time.time()
    if now - _TRIGGER_LAST_CHECK < _TRIGGER_MIN_INTERVAL:
        return
    _TRIGGER_LAST_CHECK = now
    try:
        _evaluate_triggers(store, settings, llm, adapters, defer=defer)
    except Exception:
        worker_logger.exception("trigger evaluation error")
