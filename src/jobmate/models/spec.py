"""워크플로/에이전트/모델 스펙 — 가이드 계층의 버전 고정 정의."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ModelConfig(BaseModel):
    """역할별 서브모델 설정. api_key는 코드에 두지 않고 env 이름만."""

    model: str
    base_url: str = "https://api.openai.com/v1"
    api_key_env: str = "OPENAI_API_KEY"
    provider: Literal["openai_compat", "codex"] = "openai_compat"
    temperature: float = 0.2
    max_tokens: int = 4096
    timeout_seconds: float = 60.0


class AgentSpec(BaseModel):
    """LLM 에이전트 단계 정의. tools는 allowlist 이름 목록(서버 강제).

    tools가 비어 있지 않으면 runner가 네이티브 function-calling 루프를 돌린다:
    등록된 스키마(tools/schemas.py) ∩ allowlist만 LLM에 노출되고, 실행은
    ToolExecutor.call을 통해 allowlist를 다시 강제한다.
    """

    key: str
    role: str  # analysis|implementer|security|reviewer|qa|extractor
    prompt: str  # guide/prompts/ 내 파일명
    output_schema: str  # models 출력 스키마 이름 (registry에서 해석)
    model: ModelConfig
    tools: list[str] = Field(default_factory=list)
    max_attempts: int = 3
    tier: Literal["default", "important"] = "default"


class StageSpec(BaseModel):
    """워크플로의 한 단계. kind에 따라 실행 방식이 달라진다."""

    key: str
    kind: Literal["llm", "function", "human"]
    agent: str | None = None  # llm 단계의 AgentSpec 키
    function: str | None = None  # function 단계의 등록 함수 이름
    description: str = ""


class WorkflowSpec(BaseModel):
    """버전 고정 워크플로 정의."""

    key: str
    version: str
    description: str = ""
    stages: list[StageSpec]
    entry: str
    # 분기 규칙: stage_key -> {output_field: {value: next_stage}}
    # "__default__" 키로 폴백 지정 가능. 없는 단계는 순차 진행.
    branches: dict[str, dict[str, dict[str, str]]] = Field(default_factory=dict)
    # 루프 상한
    max_review_rounds: int = 2  # 리뷰성 단계(보안/리뷰/QA) 회귀 상한
    max_implement_rounds: int = 3  # 구현 단계 총 실행 상한
    max_fix_rounds: int = 5  # 자동 검사 FAIL → 구현 회귀 상한
