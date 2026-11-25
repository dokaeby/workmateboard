"""워크플로/에이전트/모델 스펙 — 가이드 계층의 버전 고정 정의."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, model_validator

# runner가 stages 외에 항상 제공하는 종결 의사 단계 — branch/entry 타겟으로 허용
RESERVED_STAGE_KEYS = frozenset({"finalize"})
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_MAX_ROUNDS_LIMIT = 100


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

    @model_validator(mode="after")
    def _check_references_and_limits(self) -> WorkflowSpec:
        """구조 검증 — 존재하지 않는 stage 참조·빈 agent/function·비정상 상한을 422로.

        커스텀 워크플로는 lead가 등록하지만, 등록 시점에 잘못된 spec이
        run 실행 중 예외로 죽는 것을 막기 위해 스키마 레벨에서 검증한다.
        agent/function 이름의 레지스트리 존재 여부는 순환 import 위험이 있어
        여기서는 형식만 검증한다(런타임에 runner가 해석)."""
        stage_keys = {s.key for s in self.stages}
        if len(stage_keys) != len(self.stages):
            raise ValueError("duplicate stage keys")
        valid_targets = stage_keys | RESERVED_STAGE_KEYS
        if self.entry not in valid_targets:
            raise ValueError(f"entry '{self.entry}' is not a defined stage")
        for src, rules in self.branches.items():
            if src not in valid_targets:
                raise ValueError(f"branches key '{src}' is not a defined stage")
            for field_map in rules.values():
                for target in field_map.values():
                    if target not in valid_targets:
                        raise ValueError(
                            f"branch target '{target}' (from '{src}') is not a defined stage"
                        )
        for s in self.stages:
            if s.kind == "llm":
                if not (s.agent or "").strip():
                    raise ValueError(f"llm stage '{s.key}' requires a non-empty agent")
                if not _NAME_RE.match(s.agent or ""):
                    raise ValueError(f"llm stage '{s.key}' has invalid agent name '{s.agent}'")
            elif s.kind == "function":
                if not (s.function or "").strip():
                    raise ValueError(f"function stage '{s.key}' requires a non-empty function")
                if not _NAME_RE.match(s.function or ""):
                    raise ValueError(
                        f"function stage '{s.key}' has invalid function name '{s.function}'"
                    )
        for f in ("max_review_rounds", "max_implement_rounds", "max_fix_rounds"):
            v = getattr(self, f)
            if v < 0 or v > _MAX_ROUNDS_LIMIT:
                raise ValueError(f"{f} must be between 0 and {_MAX_ROUNDS_LIMIT}")
        return self
