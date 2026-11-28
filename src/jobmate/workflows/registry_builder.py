"""Settings + LLMClient로부터 Registry/Runner를 조립한다."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from typing import Any

from ..config import Settings
from ..engine.registry import Registry, default_agents
from ..engine.runner import WorkflowRunner, build_checkpointer_for
from ..llm import LLMClient
from ..store.db import Store
from . import WORKFLOWS
from .resolver import resolve_workflow_spec

logger = logging.getLogger("jobmate.workflows")

_AGENT_MODEL_ROLES = ("analysis", "implementer", "security", "reviewer", "qa", "extractor")


def _agent_model_overrides() -> dict[str, str]:
    """JOBMATE_AGENT_MODELS env JSON — agent 키 또는 stage 키 → 모델명.

    예: {"security": "gpt-5", "qa": "claude-sonnet"}. 잘못된 JSON/형식은
    경고만 남기고 무시한다."""
    raw = os.environ.get("JOBMATE_AGENT_MODELS", "")
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        logger.warning("JOBMATE_AGENT_MODELS is not valid JSON — ignored")
        return {}
    if not isinstance(data, dict):
        logger.warning("JOBMATE_AGENT_MODELS must be a JSON object — ignored")
        return {}
    return {str(k): str(v) for k, v in data.items() if isinstance(v, str)}


def build_registry(settings: Settings) -> Registry:
    reg = Registry()
    for spec in WORKFLOWS.values():
        reg.register_workflow(spec)
    # 역할별 모델 오버라이드 적용
    base_default = settings.model_for("_default", "default")
    base_important = settings.model_for("_important", "important")
    for agent in default_agents(base_default, base_important).values():
        reg.register_agent(agent)
    # 역할별 오버라이드가 있으면 model 교체
    for role in _AGENT_MODEL_ROLES:
        if role in settings.role_models:
            agent = reg.agent(role)
            tier = "important" if agent.tier == "important" else "default"
            reg.register_agent(agent.model_copy(update={"model": settings.model_for(role, tier)}))
    # JOBMATE_AGENT_MODELS — agent/stage 키 → 모델명 직접 지정 (ModelConfig 나머지 유지)
    overrides = _agent_model_overrides()
    if overrides:
        stage_agents = {
            st.key: st.agent for spec in reg.workflows() for st in spec.stages if st.agent
        }
        for key, model_name in overrides.items():
            try:
                agent = reg.agent(key)
            except KeyError:
                agent_key = stage_agents.get(key)
                if not agent_key:
                    logger.warning("JOBMATE_AGENT_MODELS unknown key ignored: %s", key)
                    continue
                agent = reg.agent(agent_key)
            new_model = agent.model.model_copy(update={"model": model_name})
            reg.register_agent(agent.model_copy(update={"model": new_model}))
            logger.info("agent model override key=%s agent=%s model=%s", key, agent.key, model_name)
    return reg


def _mcp_extra_tools() -> dict[str, Callable[..., Any]]:
    """JOBMATE_MCP_* env로 설정된 외부 MCP 서버의 도구를 ToolExecutor용으로 수집한다.

    도구명은 ``mcp_<server>_<tool>``로 프리픽스되고 schemas.py에 등록된다.
    설정이 없거나 MCP 연결에 실패하면 빈 dict — 파이프라인을 중단시키지 않는다.
    """
    try:
        from ..mcp.client import load_server_configs, mcp_tool_entries

        if not load_server_configs():
            return {}
        entries = mcp_tool_entries()
    except Exception as e:  # noqa: BLE001 - MCP 브리지는 선택 기능
        logger.warning("mcp tools unavailable: %s", e)
        return {}
    tools: dict[str, Callable[..., Any]] = {}
    for entry in entries:
        call = entry["call"]

        def _wrap(c: Callable[[dict[str, Any]], str]) -> Callable[..., Any]:
            # ToolExecutor는 fn(**kwargs)로 호출 — MCP call은 args dict를 받는다.
            def fn(**kwargs: Any) -> str:
                return c(kwargs)

            return fn

        tools[entry["name"]] = _wrap(call)
    return tools


def _build_tool_executor() -> Any:
    from ..tools.executor import ToolExecutor

    # extra_tools는 builtin과 병합되며 builtin 이름을 덮어쓸 수 없다.
    return ToolExecutor(repo_root=".", extra_tools=_mcp_extra_tools())


def build_runner(
    workflow_key: str,
    *,
    store: Store,
    llm: LLMClient,
    settings: Settings,
    tools: object | None = None,
    adapters: dict | None = None,
) -> WorkflowRunner:
    reg = build_registry(settings)
    # 커스텀 워크플로 — 전역 WORKFLOWS가 아닌 custom_workflows 테이블에서
    # 현재 조직 컨텍스트(호출자가 run/행 org로 재적용한 org_scope)로 spec을
    # 해석해 이 run 전용 registry에만 등록한다. 조직별 동명 spec이 서로의
    # 실행을 오염시키지 않는다(spec 003 US3).
    try:
        reg.workflow(workflow_key)
    except KeyError:
        custom = resolve_workflow_spec(store, workflow_key)
        if custom is not None:
            reg.register_workflow(custom)
    spec = reg.workflow(workflow_key)
    cp = build_checkpointer_for(settings)
    executor = tools or _build_tool_executor()
    return WorkflowRunner(
        spec=spec,
        registry=reg,
        store=store,
        llm=llm,
        tools=executor,
        settings=settings,
        checkpointer=cp,
        adapters=adapters or {},
    )
