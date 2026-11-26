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
    """PATCH /runs/{id}, /agent-sessions/{key} — project 태그만 지원."""

    project: str | None = Field(default=None, max_length=500)


class UserRoleRequest(BaseModel):
    """PATCH /users/{id}/role — member|lead|admin ('user'는 member 별칭)."""

    role: str = Field(min_length=1, max_length=20)


class ProjectCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    color: str = Field(default="", max_length=32)
    description: str = Field(default="", max_length=2000)
    goal: str = Field(default="", max_length=500)
    target_count: int = Field(default=0, ge=0)


class ProjectPatchRequest(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    color: str | None = Field(default=None, max_length=32)
    description: str | None = Field(default=None, max_length=2000)
    goal: str | None = Field(default=None, max_length=500)
    target_count: int | None = Field(default=None, ge=0)


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


class BudgetIncidentResolveRequest(BaseModel):
    action: Literal["keep_paused", "raise_budget_and_resume", "dismiss"]
    amount_usd: float | None = Field(default=None, allow_inf_nan=False)
    note: str = Field(default="", max_length=500)
