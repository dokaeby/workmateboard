"""LangGraph 기반 워크플로 실행기 — spec 해석, 분기, interrupt 승인, 예산."""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal, cast

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, StateGraph
from langgraph.types import Command, interrupt

from ..guide.loader import load_prompt
from ..llm import LLMClient
from ..models import (
    AgentSpec,
    DoneState,
    RunStatus,
    StageSpec,
    StageStatus,
    WorkflowSpec,
)
from ..sensors import downgrade_approve, filter_findings
from ..store.db import Store
from .functions import FUNCTIONS, FunctionContext
from .schemas import SCHEMAS
from .state import HarnessState, initial_state

if TYPE_CHECKING:
    from ..config import Settings

logger = logging.getLogger("jobmate.engine")

ESCALATE_NEEDS_HUMAN = "needs_human"
ESCALATE_BUDGET = "budget_exhausted"
ESCALATE_FAILED = "failed"

# 리뷰성 단계(회귀 시 review_rounds 증가)
REVIEW_DECISION_STAGES = {"security", "code_review", "qa"}


class WorkflowRunner:
    """WorkflowSpec 하나를 LangGraph로 컴파일해 실행한다."""

    def __init__(
        self,
        *,
        spec: WorkflowSpec,
        registry: Any,
        store: Store,
        llm: LLMClient,
        tools: Any,
        settings: Any,
        checkpointer: SqliteSaver | None = None,
        adapters: dict[str, Any] | None = None,
    ) -> None:
        self.spec = spec
        self.registry = registry
        self.store = store
        self.llm = llm
        self.tools = tools
        self.settings = settings
        self.adapters = adapters or {}
        self._checkpointer = checkpointer
        self._graph = self._build()

    # ---------- graph construction ----------
    def _build(self) -> Any:
        g = StateGraph(HarnessState)
        stage_keys = {s.key for s in self.spec.stages}
        for stage in self.spec.stages:
            g.add_node(stage.key, cast(Any, self._make_node(stage)))
        g.add_node("finalize", cast(Any, self._finalize))
        g.set_entry_point(self.spec.entry)
        for stage in self.spec.stages:
            g.add_conditional_edges(
                stage.key,
                lambda s, k=stage.key: self._route(s, k),
                {**{k: k for k in stage_keys}, "finalize": "finalize"},
            )
        g.add_edge("finalize", END)
        return g.compile(checkpointer=self._checkpointer)

    # ---------- routing ----------
    def _route(self, state: HarnessState, stage_key: str) -> str:
        if state.get("escalate"):
            return "finalize"
        out = state.get("stage_outputs", {}).get(stage_key, {})
        if out.get("__retry__"):
            return stage_key  # LLM 실패 재시도 — 같은 단계로 자기 루프
        rules = self.spec.branches.get(stage_key, {})
        for field, mapping in rules.items():
            value = self._resolve(out, field)
            if isinstance(value, bool):
                value = "true" if value else "false"
            if value is not None and str(value) in mapping:
                return mapping[str(value)]
        order = [s.key for s in self.spec.stages]
        idx = order.index(stage_key)
        return order[idx + 1] if idx + 1 < len(order) else "finalize"

    @staticmethod
    def _resolve(out: dict[str, Any], field: str) -> Any:
        cur: Any = out
        for part in field.split("."):
            if isinstance(cur, dict):
                cur = cur.get(part)
            else:
                return None
        return cur

    # ---------- budget ----------
    def _budget_exceeded(self, state: HarnessState) -> str | None:
        if (
            state.get("tokens_in", 0) + state.get("tokens_out", 0)
            >= self.settings.budget_max_tokens
        ):
            return "token_budget"
        if state.get("tool_calls", 0) >= self.settings.budget_max_tool_calls:
            return "tool_call_budget"
        if time.time() - state.get("started_at", time.time()) >= self.settings.budget_max_seconds:
            return "time_budget"
        if state.get("cost_usd", 0.0) >= self.settings.budget_max_cost_usd:
            return "cost_budget"
        return None

    def _loop_cap_check(self, state: HarnessState, stage: StageSpec) -> str | None:
        """단계 진입 전 루프 상한 검사."""
        if stage.key == "implement":
            if state.get("implement_rounds", 0) >= self.spec.max_implement_rounds:
                return "implement_rounds_exceeded"
            if state.get("review_rounds", 0) > self.spec.max_review_rounds:
                return "review_rounds_exceeded"
        return None

    # ---------- stage executors ----------
    def _make_node(self, stage: StageSpec) -> Callable[[HarnessState], dict[str, Any]]:
        if stage.kind == "llm":
            return lambda s: self._run_llm_stage(s, stage)
        if stage.kind == "function":
            return lambda s: self._run_function_stage(s, stage)
        return lambda s: self._run_human_stage(s, stage)

    def _attempt(self, state: HarnessState, stage_key: str) -> int:
        attempts = state.setdefault("stage_attempts", {})
        n = attempts.get(stage_key, 0) + 1
        attempts[stage_key] = n
        return n

    def _run_llm_stage(self, state: HarnessState, stage: StageSpec) -> dict[str, Any]:
        run_id = state["run_id"]
        agent = self.registry.agent(stage.agent or "")

        # 루프 상한·예산 사전 검사 — LLM 호출 전에 안전 정지
        cap = self._loop_cap_check(state, stage)
        if cap:
            state["escalate"] = ESCALATE_NEEDS_HUMAN
            self.store.audit(run_id, "system", "loop_cap", stage.key, {"reason": cap})
            return {"escalate": ESCALATE_NEEDS_HUMAN}
        reason = self._budget_exceeded(state)
        if reason:
            state["escalate"] = ESCALATE_BUDGET
            self.store.audit(run_id, "system", "budget_exhausted", stage.key, {"reason": reason})
            return {"escalate": ESCALATE_BUDGET}

        attempt = self._attempt(state, stage.key)
        prompt_text, prompt_version = load_prompt(agent.prompt)
        stage_run = self.store.start_stage(
            run_id,
            stage.key,
            attempt,
            {"agent": agent.key, "role": agent.role},
            model=agent.model.model,
            prompt_version=prompt_version,
        )
        try:
            schema = SCHEMAS.get(agent.output_schema)
            user = self._build_user_prompt(state, stage, agent)
            out, usage = self.llm.complete(
                model=agent.model, system=prompt_text, user=user, schema=schema
            )
            parsed = (
                out
                if isinstance(out, dict)
                else (out.model_dump() if hasattr(out, "model_dump") else {"result": str(out)})
            )
            parsed = self._apply_sensors(agent.role, parsed)
            self.store.finish_stage(
                stage_run.id,
                status=StageStatus.SUCCEEDED,
                output=parsed,
                tokens_in=usage.get("tokens_in", 0),
                tokens_out=usage.get("tokens_out", 0),
            )
            self.store.audit(
                run_id,
                f"agent:{agent.role}",
                "verdict",
                stage.key,
                self._audit_detail(agent.role, parsed),
            )

            update: dict[str, Any] = {
                "stage_outputs": {stage.key: parsed},
                "tokens_in": state.get("tokens_in", 0) + usage.get("tokens_in", 0),
                "tokens_out": state.get("tokens_out", 0) + usage.get("tokens_out", 0),
            }
            if stage.key == "implement":
                update["implement_rounds"] = state.get("implement_rounds", 0) + 1
            if stage.key in REVIEW_DECISION_STAGES and parsed.get("decision") == "request_changes":
                update["review_rounds"] = state.get("review_rounds", 0) + 1
            if parsed.get("done_state") in (DoneState.BLOCKED.value, DoneState.NEEDS_CONTEXT.value):
                update["escalate"] = ESCALATE_NEEDS_HUMAN
            return update
        except Exception as e:
            self.store.finish_stage(stage_run.id, status=StageStatus.FAILED, error=str(e)[:500])
            self.store.audit(
                run_id, f"agent:{agent.role}", "stage_error", stage.key, {"error": str(e)[:200]}
            )
            logger.warning(
                "run %s stage %s agent=%s error attempt=%d: %s",
                run_id,
                stage.key,
                agent.role,
                attempt,
                str(e)[:200],
            )
            if attempt >= agent.max_attempts:
                return {"escalate": ESCALATE_FAILED}
            # 스키마/파싱 실패 — 재시도는 다음 attempt로 진행하기 위해
            # 에러 출력을 남기고 route가 재진입하도록 한다.
            return {"stage_outputs": {stage.key: {"__retry__": True, "error": str(e)[:300]}}}

    def _run_function_stage(self, state: HarnessState, stage: StageSpec) -> dict[str, Any]:
        run_id = state["run_id"]
        attempt = self._attempt(state, stage.key)
        stage_run = self.store.start_stage(run_id, stage.key, attempt, {"function": stage.function})
        fn = FUNCTIONS.get(stage.function or "")
        if fn is None:
            self.store.finish_stage(
                stage_run.id, status=StageStatus.FAILED, error=f"unknown function {stage.function}"
            )
            return {"escalate": ESCALATE_FAILED}
        try:
            ctx = FunctionContext(
                state=state,
                store=self.store,
                settings=self.settings,
                adapters=self.adapters,
                run_id=run_id,
                llm=self.llm,
            )
            out = fn(ctx)
            self.store.finish_stage(stage_run.id, status=StageStatus.SUCCEEDED, output=out)
            update: dict[str, Any] = {"stage_outputs": {stage.key: out}}
            # 자동 검사 FAIL → fix 회귀 카운터
            if stage.key == "auto_checks" and out.get("passed") is False:
                update["fix_rounds"] = state.get("fix_rounds", 0) + 1
                if update["fix_rounds"] > self.spec.max_fix_rounds:
                    update["escalate"] = ESCALATE_NEEDS_HUMAN
            return update
        except Exception as e:
            self.store.finish_stage(stage_run.id, status=StageStatus.FAILED, error=str(e)[:500])
            self.store.audit(run_id, "tool", "function_error", stage.key, {"error": str(e)[:200]})
            logger.warning(
                "run %s stage %s function=%s error attempt=%d: %s",
                run_id,
                stage.key,
                stage.function,
                attempt,
                str(e)[:200],
            )
            return {"escalate": ESCALATE_FAILED}

    def _run_human_stage(self, state: HarnessState, stage: StageSpec) -> dict[str, Any]:
        run_id = state["run_id"]
        summary = self._approval_summary(state)
        self.store.update_run(run_id, status=RunStatus.WAITING_APPROVAL)
        self.store.audit(
            run_id, "system", "waiting_approval", stage.key, {"summary": summary[:500]}
        )
        # resume 시 interrupt 이전 코드가 재실행되므로, 이미 RUNNING인
        # stage_run이 있으면 재사용해 고아 레코드를 만들지 않는다.
        prior = self.store.latest_stage(run_id, stage.key)
        if prior is not None and prior.status == StageStatus.RUNNING:
            stage_run_id = prior.id
        else:
            attempt = self._attempt(state, stage.key)
            stage_run_id = self.store.start_stage(
                run_id, stage.key, attempt, {"summary": summary[:500]}
            ).id
        if state.get("auto_approve"):
            # cron 등 무인 수집 경로 — 승인 단계 없이 자동 approve하여 publish까지 진행
            decision = {"decision": "approve", "comment": "auto_approve"}
        else:
            decision = interrupt({"run_id": run_id, "stage": stage.key, "summary": summary})
        raw_dec = (decision or {}).get("decision", "reject")
        dec: Literal["approve", "reject"] = "approve" if raw_dec == "approve" else "reject"
        comment = (decision or {}).get("comment", "")
        logger.info("run %s approval stage=%s decision=%s", run_id, stage.key, dec)
        out = {"decision": dec, "comment": comment}
        self.store.finish_stage(stage_run_id, status=StageStatus.SUCCEEDED, output=out)
        self.store.add_approval(run_id, stage.key, dec, comment)
        self.store.update_run(
            run_id, status=RunStatus.RUNNING if dec == "approve" else RunStatus.REJECTED
        )
        return {"stage_outputs": {stage.key: out}, "approval": out}

    # ---------- helpers ----------
    def _build_user_prompt(self, state: HarnessState, stage: StageSpec, agent: AgentSpec) -> str:
        outputs = state.get("stage_outputs", {})
        run = self.store.get_run(state["run_id"])
        task = (run.input if run else {}).get("task", "")
        parts = [f"# Task\n{task}"]
        if agent.role == "implementer":
            feedback = [
                {"from": k, "output": o}
                for k, o in outputs.items()
                if k in ("security", "code_review", "qa", "auto_checks") and "__retry__" not in o
            ]
            if feedback:
                parts.append("# Feedback from review (address these)\n" + _jsonish(feedback))
        for k, o in outputs.items():
            if k != stage.key and "__retry__" not in o:
                parts.append(f"# Output of {k}\n{_jsonish(o)[:8000]}")
        if state.get("gmail_message_id"):
            parts.append(f"# gmail_message_id\n{state['gmail_message_id']}")
        if state.get("message_text"):
            parts.append(f"# Message (untrusted data)\n{state['message_text'][:8000]}")
        return "\n\n".join(parts)

    def _apply_sensors(self, role: str, parsed: dict[str, Any]) -> dict[str, Any]:
        if role in ("reviewer", "security") and "decision" in parsed:
            from ..models import Verdict

            try:
                v = Verdict.model_validate(parsed)
                return downgrade_approve(filter_findings(v)).model_dump()
            except Exception:
                return parsed
        return parsed

    @staticmethod
    def _audit_detail(role: str, parsed: dict[str, Any]) -> dict[str, Any]:
        detail: dict[str, Any] = {"decision": parsed.get("decision", "")}
        findings = parsed.get("findings")
        if isinstance(findings, list):
            detail["finding_count"] = len(findings)
            detail["severities"] = [f.get("severity") for f in findings if isinstance(f, dict)]
        if parsed.get("summary"):
            detail["summary"] = str(parsed["summary"])[:300]
        return detail

    def _approval_summary(self, state: HarnessState) -> str:
        outputs = state.get("stage_outputs", {})
        return "\n".join(
            [
                "# PR 요약 (jobMate dev_review)",
                "",
                "## What changed",
                str(outputs.get("analysis", {}).get("requirements", ""))[:500],
                "",
                "## 구현 요약",
                str(outputs.get("implement", {}).get("summary", ""))[:500],
                "",
                "## 자동 검사",
                _checks_summary(outputs.get("auto_checks", {})),
                "",
                "## 리뷰/보안 findings",
                _findings_summary(outputs),
                "",
                "## QA 판정",
                str(outputs.get("qa", {}).get("rationale", ""))[:500],
            ]
        )

    def _finalize(self, state: HarnessState) -> dict[str, Any]:
        run_id = state["run_id"]
        escalate = state.get("escalate") or ""
        approval = state.get("approval") or {}
        if escalate == ESCALATE_BUDGET:
            status = RunStatus.BUDGET_EXHAUSTED
        elif escalate == ESCALATE_FAILED:
            status = RunStatus.FAILED
        elif escalate:
            status = RunStatus.NEEDS_HUMAN
        elif approval.get("decision") == "reject":
            status = RunStatus.REJECTED
        else:
            status = RunStatus.COMPLETED
        self.store.update_run(run_id, status=status)
        self.store.audit(
            run_id, "system", "run_finalized", "", {"status": status.value, "escalate": escalate}
        )
        logger.info("run %s finalized status=%s escalate=%s", run_id, status.value, escalate or "-")
        run = self.store.get_run(run_id)
        if run is not None and self.spec.key == "dev_review":
            meta = dict(run.metadata or {})
            meta["pr_summary"] = self._approval_summary(state)
            self.store.update_run(run_id, metadata=meta)
            self.store.audit(run_id, "system", "pr_summary", "", {"chars": len(meta["pr_summary"])})
        return {"final_status": status.value}

    # ---------- public API ----------
    def invoke(
        self, run_id: str, input_data: dict[str, Any], gmail_ctx: dict[str, Any] | None = None
    ) -> HarnessState:
        state = initial_state(run_id, self.spec.key, gmail_ctx)
        self.store.update_run(run_id, status=RunStatus.RUNNING)
        logger.info("run %s invoke workflow=%s", run_id, self.spec.key)
        config = {"configurable": {"thread_id": run_id}}
        return self._graph.invoke(state, config)

    def resume(self, run_id: str, decision: dict[str, Any]) -> HarnessState:
        config = {"configurable": {"thread_id": run_id}}
        return self._graph.invoke(Command(resume=decision), config)


def _jsonish(obj: Any) -> str:
    import json

    try:
        return json.dumps(obj, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(obj)


def _checks_summary(checks_out: dict[str, Any]) -> str:
    checks = checks_out.get("checks", [])
    if not checks:
        return "(none)"
    return "\n".join(
        f"- {c.get('name')}: {'PASS' if c.get('passed') else 'FAIL'}"
        for c in checks
        if isinstance(c, dict)
    )


def _findings_summary(outputs: dict[str, dict[str, Any]]) -> str:
    parts = []
    for k in ("security", "code_review"):
        out = outputs.get(k, {})
        fs = out.get("findings", [])
        if isinstance(fs, list) and fs:
            sev: dict[str, int] = {}
            for f in fs:
                if isinstance(f, dict):
                    s = str(f.get("severity", "?"))
                    sev[s] = sev.get(s, 0) + 1
            parts.append(f"{k}: {sev}")
    return "\n".join(parts) if parts else "(none)"


def build_checkpointer(db_path: str) -> SqliteSaver:
    """SqliteSaver 컨텍스트를 열어 반환한다. saver._ctx를 GC 전까지 유지."""
    ctx = SqliteSaver.from_conn_string(db_path)
    saver = ctx.__enter__()
    saver._ctx = ctx  # type: ignore[attr-defined]
    return saver


def build_checkpointer_for(settings: Settings) -> Any:
    """Postgres DSN이 있으면 PostgresSaver, 아니면 SQLite.

    서버리스(Vercel)에서는 /tmp가 인스턴스별로 분리되어 SQLite 체크포인트가
    invoke 인스턴스와 resume 인스턴스 사이에서 유실된다. DB가 Postgres면
    checkpoint도 같은 DB에 두어 어느 인스턴스든 interrupt를 재개할 수 있게 한다.
    """
    dsn = (
        settings.database_url
        or os.environ.get("POSTGRES_URL", "")
        or os.environ.get("POSTGRES_PRISMA_URL", "")
    )
    if dsn:
        import psycopg
        from langgraph.checkpoint.postgres import PostgresSaver
        from psycopg.rows import dict_row

        conn = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
        saver = PostgresSaver(conn)
        saver.setup()  # checkpoint 테이블 생성 (idempotent)
        return saver
    return build_checkpointer(str(settings.db_path) + ".checkpoints")
