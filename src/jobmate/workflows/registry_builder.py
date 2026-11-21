"""Settings + LLMClient로부터 Registry/Runner를 조립한다."""

from __future__ import annotations

from ..config import Settings
from ..engine.registry import Registry, default_agents
from ..engine.runner import WorkflowRunner, build_checkpointer_for
from ..llm import LLMClient
from ..store.db import Store
from . import WORKFLOWS


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
    for role in ("analysis", "implementer", "security", "reviewer", "qa", "extractor"):
        if role in settings.role_models:
            agent = reg.agent(role)
            tier = "important" if agent.tier == "important" else "default"
            reg.register_agent(agent.model_copy(update={"model": settings.model_for(role, tier)}))
    return reg


def build_runner(
    workflow_key: str,
    *,
    store: Store,
    llm: LLMClient,
    settings: Settings,
    tools: object | None = None,
    adapters: dict | None = None,
) -> WorkflowRunner:
    from ..tools.executor import ToolExecutor

    reg = build_registry(settings)
    spec = reg.workflow(workflow_key)
    cp = build_checkpointer_for(settings)
    return WorkflowRunner(
        spec=spec,
        registry=reg,
        store=store,
        llm=llm,
        tools=tools or ToolExecutor(repo_root="."),
        settings=settings,
        checkpointer=cp,
        adapters=adapters or {},
    )
