"""app.py에서 분리한 Pydantic 요청 모델."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field


class RunCreateRequest(BaseModel):
    workflow: str = "dev_review"
    task: str = Field(min_length=1, max_length=20_000)
    # 프로젝트 태그 — runs.project 컬럼 + project 스코프 예산 귀속에 사용.
    # 미지정 시 metadata.project로 폴백한다.
    project: str = Field(default="", max_length=200)
    metadata: dict[str, Any] = Field(default_factory=dict)
    # wakeup coalescing — 동일 (workflow,input) 활성 run이 있으면 합류.
    # force_new=true면 기존 run과 무관하게 deferred로 예약(종결 후 실행).
    force_new: bool = False
    idempotency_key: str = Field(default="", max_length=200)
    # agents 레지스트리 태깅 — runs.agent_id에 기록돼 에이전트별
    # runs_30d/cost_30d_usd 통계에 귀속된다. 미지정 시 NULL.
    agent_id: str = Field(default="", max_length=64)
    # goals 계층 태깅 — runs.goal_id에 기록. 미지정 시 프로젝트의
    # goal_id로 폴백하고, 그것도 없으면 NULL.
    goal_id: str = Field(default="", max_length=64)


class ApprovalRequest(BaseModel):
    decision: str = Field(pattern="^(approve|reject)$")
    comment: str = ""


class ScheduleCreateRequest(BaseModel):
    workflow: str
    input: dict[str, Any] = Field(default_factory=dict)
    interval_minutes: int = Field(ge=5)


class ScheduleToggleRequest(BaseModel):
    enabled: bool


class EventReviewRequest(BaseModel):
    action: str = Field(pattern="^(approve|reject)$")
    title: str | None = None
    start_at: str | None = None
    end_at: str | None = None
    timezone: str | None = None
    location: str | None = None


class AgentSessionRequest(BaseModel):
    """로컬 에이전트 브릿지 push — tool은 알려진 값 외에도 허용(미래 도구 대비)."""

    tool: str = Field(min_length=1, max_length=64)
    session_key: str = Field(min_length=1, max_length=512)
    status: Literal["working", "decision", "rest", "ended"]
    project: str | None = Field(default=None, max_length=512)
    title: str | None = Field(default=None, max_length=500)
    doing: str | None = Field(default=None, max_length=500)
    ask: str | None = Field(default=None, max_length=500)
    event_at: str | None = Field(default=None, max_length=64)
    ended_summary: str | None = Field(default=None, max_length=500)
    artifacts: list[Annotated[str, Field(max_length=300)]] | None = Field(
        default=None, max_length=10
    )
    machine: str | None = Field(default=None, max_length=128)
    tokens_in: int | None = Field(default=None, ge=0)
    tokens_out: int | None = Field(default=None, ge=0)


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


class ReportGenerateRequest(BaseModel):
    kind: Literal["daily", "weekly", "exec_daily", "exec_weekly"] = "daily"


class RunPatchRequest(BaseModel):
    """PATCH /runs/{id}, /agent-sessions/{key} — project/goal 태그만 지원."""

    project: str | None = Field(default=None, max_length=500)
    # runs 전용 — agent-sessions PATCH는 project만 사용한다.
    goal_id: str | None = Field(default=None, max_length=64)


class UserRoleRequest(BaseModel):
    """PATCH /users/{id}/role — member|lead|admin ('user'는 member 별칭)."""

    role: str = Field(min_length=1, max_length=20)


class ProjectCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    color: str = Field(default="", max_length=32)
    description: str = Field(default="", max_length=2000)
    goal: str = Field(default="", max_length=500)
    target_count: int = Field(default=0, ge=0)
    # goals.id 참조 — run 생성 시 goal_id 폴백 해석에 사용. null/빈 값 = 미연결.
    goal_id: str | None = Field(default=None, max_length=64)


class ProjectPatchRequest(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    color: str | None = Field(default=None, max_length=32)
    description: str | None = Field(default=None, max_length=2000)
    goal: str | None = Field(default=None, max_length=500)
    target_count: int | None = Field(default=None, ge=0)
    goal_id: str | None = Field(default=None, max_length=64)


class CommentCreateRequest(BaseModel):
    body: str = Field(min_length=1, max_length=4000)


class PlaybookCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    workflow: str = Field(min_length=1, max_length=120)
    input: dict[str, Any] = Field(default_factory=dict)


class ConnectorCreateRequest(BaseModel):
    type: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=120)
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class TriggerCreateRequest(BaseModel):
    match: dict[str, Any] = Field(default_factory=dict)
    action: dict[str, Any] = Field(default_factory=dict)


class CustomWorkflowRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    spec: dict[str, Any] | str
    # CAS — 현재 latest_revision과 다르면 409 revision_conflict.
    # 생략 시 최신 리비전 위에 그대로 append(자동 저장과 동일).
    base_revision: int | None = None
    note: str = Field(default="", max_length=500)


class GoalCreateRequest(BaseModel):
    """POST /goals — title 필수, parent_id 선택(존재/비archived/무사이클)."""

    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    parent_id: str | None = Field(default=None, max_length=64)
    status: str | None = Field(default=None, max_length=20)


class GoalPatchRequest(BaseModel):
    """PATCH /goals/{id} — model_fields_set에 있는 필드만 부분 갱신.
    parent_id=null/빈 문자열은 루트 해제."""

    title: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    parent_id: str | None = Field(default=None, max_length=64)
    status: str | None = Field(default=None, max_length=20)


class CardCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    url: str = Field(min_length=1, max_length=2000)
    json_path: str = Field(default="", max_length=500)
    enabled: bool = True


class ShareCreateRequest(BaseModel):
    kind: Literal["run", "report", "insights"]
    ref_id: str = Field(default="", max_length=200)


class BudgetPolicyRequest(BaseModel):
    """PUT /budgets/policies/{scope_type}[/{scope_id}] — 부분 갱신.

    amount_usd null/<=0이면 정책 비활성(is_active=0).
    warn_percent/hard_stop_enabled/is_active의 명시 null은 미전송과
    동일 — 기존 값을 유지한다(서비스 계층에서 UNSET으로 정규화)."""

    amount_usd: float | None = Field(default=None, allow_inf_nan=False)
    warn_percent: int | None = Field(default=None, ge=1, le=100)
    hard_stop_enabled: bool | None = None
    is_active: bool | None = None


class AgentCreateRequest(BaseModel):
    """POST /agents — name/adapter_type 필수, 나머지 선택."""

    name: str = Field(min_length=1, max_length=200)
    adapter_type: str = Field(min_length=1, max_length=32)
    role: str | None = Field(default=None, max_length=100)
    description: str | None = Field(default=None, max_length=2000)
    capabilities: list[Annotated[str, Field(max_length=100)]] | None = Field(
        default=None, max_length=50
    )
    reports_to: str | None = Field(default=None, max_length=64)
    status: str | None = Field(default=None, max_length=20)
    monthly_budget_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class AgentPatchRequest(BaseModel):
    """PATCH /agents/{id} — model_fields_set에 있는 필드만 부분 갱신.

    reports_to=null/빈 문자열은 상위 해제, monthly_budget_usd=null은 한도 해제."""

    name: str | None = Field(default=None, max_length=200)
    adapter_type: str | None = Field(default=None, max_length=32)
    role: str | None = Field(default=None, max_length=100)
    description: str | None = Field(default=None, max_length=2000)
    capabilities: list[Annotated[str, Field(max_length=100)]] | None = Field(
        default=None, max_length=50
    )
    reports_to: str | None = Field(default=None, max_length=64)
    status: str | None = Field(default=None, max_length=20)
    monthly_budget_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class BudgetIncidentResolveRequest(BaseModel):
    action: Literal["keep_paused", "raise_budget_and_resume", "dismiss"]
    amount_usd: float | None = Field(default=None, allow_inf_nan=False)
    note: str = Field(default="", max_length=500)
