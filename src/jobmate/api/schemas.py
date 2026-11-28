"""app.py에서 분리한 Pydantic 요청 모델."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, StrictInt, field_validator


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
    # int4 상한 — 초과 시 PG INTEGER 오버플로로 500이 되므로 미리 거부
    tokens_in: int | None = Field(default=None, ge=0, le=2_147_483_647)
    tokens_out: int | None = Field(default=None, ge=0, le=2_147_483_647)
    # spec 012 US1 — 라이브 에이전트 카드 필드. 전부 optional, 미전송 시
    # 기존 값 유지(스토어 COALESCE). 범위는 느슨하게 검증한다.
    model: str | None = Field(default=None, max_length=128)
    progress_pct: float | None = Field(default=None, ge=0, le=100, allow_inf_nan=False)
    cost_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    tool_calls: int | None = Field(default=None, ge=0, le=2_147_483_647)
    sub_agents: int | None = Field(default=None, ge=0, le=2_147_483_647)


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
    # spec 3차 A — planned|active|paused|completed|cancelled
    status: str | None = Field(default=None, max_length=20)
    owner_agent_id: str | None = Field(default=None, max_length=64)
    repository_url: str | None = Field(default=None, max_length=500)
    repository_ref: str | None = Field(default=None, max_length=200)
    # 로컬 절대경로 — 저장은 되지만 API 응답은 명시적 lead에게만 노출.
    workspace_path: str | None = Field(default=None, max_length=1000)


class ProjectPatchRequest(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    color: str | None = Field(default=None, max_length=32)
    description: str | None = Field(default=None, max_length=2000)
    goal: str | None = Field(default=None, max_length=500)
    target_count: int | None = Field(default=None, ge=0)
    goal_id: str | None = Field(default=None, max_length=64)
    status: str | None = Field(default=None, max_length=20)
    owner_agent_id: str | None = Field(default=None, max_length=64)
    repository_url: str | None = Field(default=None, max_length=500)
    repository_ref: str | None = Field(default=None, max_length=200)
    workspace_path: str | None = Field(default=None, max_length=1000)


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
    """POST /goals — title 필수, parent_id 선택(존재/비cancelled/무사이클).

    spec 3차 A: status는 active|paused|achieved|cancelled."""

    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    parent_id: str | None = Field(default=None, max_length=64)
    status: str | None = Field(default=None, max_length=20)
    owner_agent_id: str | None = Field(default=None, max_length=64)
    success_metrics: dict[str, Any] | None = None
    target_at: str | None = Field(default=None, max_length=64)


class GoalPatchRequest(BaseModel):
    """PATCH /goals/{id} — model_fields_set에 있는 필드만 부분 갱신.
    parent_id=null/빈 문자열은 루트 해제."""

    title: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    parent_id: str | None = Field(default=None, max_length=64)
    status: str | None = Field(default=None, max_length=20)
    owner_agent_id: str | None = Field(default=None, max_length=64)
    success_metrics: dict[str, Any] | None = None
    target_at: str | None = Field(default=None, max_length=64)


class CardCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    url: str = Field(min_length=1, max_length=2000)
    json_path: str = Field(default="", max_length=500)
    enabled: bool = True


class ShareCreateRequest(BaseModel):
    kind: Literal["run", "report", "insights"]
    ref_id: str = Field(default="", max_length=200)


class CostEventCreateRequest(BaseModel):
    """POST /management/cost-events — 외부 runner의 비용 보고 (머신 키 전용).

    spec 2차 A: 비용 입력은 음수/NaN/float/float 문자열/bool을 거부하고
    정수 microusd만 받는다 — StrictInt가 bool·str·float을 모두 차단한다.
    cost_status='unpriced'이면 cost_microusd는 반드시 null(0 대체 금지),
    priced 상태는 정수 필수 — store 계층이 불변식을 다시 강제한다."""

    idempotency_key: str = Field(min_length=1, max_length=200)
    agent_id: str = Field(default="", max_length=64)
    task_id: str | None = Field(default=None, max_length=64)
    run_id: str | None = Field(default=None, max_length=64)
    stage_run_id: str | None = Field(default=None, max_length=64)
    provider: str = Field(default="", max_length=64)
    model: str = Field(default="", max_length=200)
    billing_type: str = Field(default="unknown", max_length=40)
    cost_status: str = Field(default="unpriced", max_length=20)
    input_tokens: StrictInt = Field(default=0, ge=0)
    cached_input_tokens: StrictInt = Field(default=0, ge=0)
    output_tokens: StrictInt = Field(default=0, ge=0)
    cost_microusd: StrictInt | None = Field(default=None, ge=0)
    pricing_source: str = Field(default="", max_length=100)
    pricing_version: str = Field(default="", max_length=40)
    occurred_at: str | None = Field(default=None, max_length=64)


class BudgetPolicyRequest(BaseModel):
    """PUT /budgets/policies/{scope_type}[/{scope_id}] — 부분 갱신.

    amount_usd null/<=0이면 정책 비활성(is_active=0).
    amount_microusd가 amount_usd보다 우선하는 microusd 권위 필드 —
    둘 다 보내면 microusd가 한도가 되고 amount_usd는 표시용 파생으로 기록.
    warn_percent/hard_stop_enabled/is_active의 명시 null은 미전송과
    동일 — 기존 값을 유지한다(서비스 계층에서 UNSET으로 정규화)."""

    amount_usd: float | None = Field(default=None, allow_inf_nan=False)
    amount_microusd: StrictInt | None = Field(default=None, ge=0)
    reservation_microusd: StrictInt | None = Field(default=None, ge=0)
    unpriced_usage_policy: Literal["block", "allow"] | None = None
    window_kind: Literal["calendar_month_utc", "lifetime"] | None = None
    warn_percent: int | None = Field(default=None, ge=1, le=100)
    hard_stop_enabled: bool | None = None
    is_active: bool | None = None
    # spec 006 US2 FR-007/FR-010 — 임계 도달 시 액션.
    # alert(기본, 알림만) | pause(hard_stop 차단) | require_approval(승인 대기).
    threshold_action: Literal["alert", "pause", "require_approval"] | None = None


class BudgetIncidentResolveRequest(BaseModel):
    action: Literal["keep_paused", "raise_budget_and_resume", "dismiss"]
    amount_usd: float | None = Field(default=None, allow_inf_nan=False)
    # microusd 권위 신규 한도 — amount_usd보다 우선한다.
    amount_microusd: StrictInt | None = Field(default=None, ge=0)
    note: str = Field(default="", max_length=500)


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


class EvalCreateRequest(BaseModel):
    """POST /evals — name/workflow_key/input/checks. input.task 필수(서비스 검증)."""

    name: str = Field(min_length=1, max_length=200)
    workflow_key: str = Field(min_length=1, max_length=120)
    input: dict[str, Any]
    checks: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    status: str | None = Field(default=None, max_length=20)


class EvalPatchRequest(BaseModel):
    """PATCH /evals/{id} — model_fields_set에 있는 필드만 부분 갱신."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    workflow_key: str | None = Field(default=None, min_length=1, max_length=120)
    input: dict[str, Any] | None = None
    checks: list[dict[str, Any]] | None = Field(default=None, max_length=100)
    status: str | None = Field(default=None, max_length=20)


# ---------- agent-management 1차 (/api/v1/management) ----------


class ManagementAgentCreateRequest(BaseModel):
    """POST /management/agents — name/adapter_type 필수.

    status는 신규 7값 어휘, kind는 workflow|external 실행 모델,
    adapter_config는 provider별 JSON(secret 키는 응답에서 마스킹)."""

    name: str = Field(min_length=1, max_length=200)
    adapter_type: str = Field(min_length=1, max_length=32)
    kind: str | None = Field(default=None, max_length=20)
    adapter_config: dict[str, Any] | None = None
    role: str | None = Field(default=None, max_length=100)
    title: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    capabilities: list[Annotated[str, Field(max_length=100)]] | None = Field(
        default=None, max_length=50
    )
    reports_to: str | None = Field(default=None, max_length=64)
    status: str | None = Field(default=None, max_length=20)
    monthly_budget_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    # microusd 우선 예산 컬럼 — monthly_budget_usd보다 우선하며
    # budget_scope.evaluate_admission의 agent 스코프 검사에서 실제로 집행된다.
    monthly_budget_microusd: StrictInt | None = Field(
        default=None, ge=0, description="microusd monthly budget; enforced via evaluate_admission"
    )


class ManagementAgentPatchRequest(BaseModel):
    """PATCH /management/agents/{id} — status는 transition 규칙을 거친다."""

    name: str | None = Field(default=None, max_length=200)
    adapter_type: str | None = Field(default=None, max_length=32)
    kind: str | None = Field(default=None, max_length=20)
    adapter_config: dict[str, Any] | None = None
    role: str | None = Field(default=None, max_length=100)
    title: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    capabilities: list[Annotated[str, Field(max_length=100)]] | None = Field(
        default=None, max_length=50
    )
    reports_to: str | None = Field(default=None, max_length=64)
    status: str | None = Field(default=None, max_length=20)
    monthly_budget_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    # microusd 우선 예산 컬럼 — monthly_budget_usd보다 우선하며
    # budget_scope.evaluate_admission의 agent 스코프 검사에서 실제로 집행된다.
    monthly_budget_microusd: StrictInt | None = Field(
        default=None, ge=0, description="microusd monthly budget; enforced via evaluate_admission"
    )


class AgentTransitionRequest(BaseModel):
    status: str = Field(min_length=1, max_length=20)


class AgentWakeRequest(BaseModel):
    """POST /management/agents/{id}/wake — Idempotency-Key 헤더 또는
    body.idempotency_key 필수. workflow kind는 task_id 필수."""

    task_id: str = Field(default="", max_length=64)
    # 150자 상한 — 서비스가 "wake:{agent_id}:{key}" 복합키를 만들고
    # 저장 계층은 200자로 자르므로, 사용자 키에 여유분을 남긴다(M1).
    idempotency_key: str = Field(default="", max_length=150)


class AgentHeartbeatRequest(BaseModel):
    """POST /management/agents/{id}/heartbeat — 머신 전용(API key).

    status 어휘: working|idle|rest|paused|decision|error|failed|running.
    task_id/task_status로 checkout task의 상태도 함께 갱신할 수 있다."""

    status: str = Field(default="", max_length=20)
    task_id: str = Field(default="", max_length=64)
    task_status: str = Field(default="", max_length=20)
    detail: dict[str, Any] = Field(default_factory=dict)


class ManagementTaskCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=8000)
    status: str | None = Field(default=None, max_length=20)
    priority: str | None = Field(default=None, max_length=20)
    assignee_agent_id: str | None = Field(default=None, max_length=64)
    reviewer_agent_id: str | None = Field(default=None, max_length=64)
    parent_id: str | None = Field(default=None, max_length=64)
    # spec 3차 A — task의 effective goal은 task.goal_id → project.goal_id → none.
    project_id: str | None = Field(default=None, max_length=64)
    goal_id: str | None = Field(default=None, max_length=64)


class ManagementTaskPatchRequest(BaseModel):
    """PATCH /management/tasks/{id} — model_fields_set 필드만 부분 갱신.
    status 전이는 allowlist 검증을 거치고, terminal task는 409."""

    title: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=8000)
    status: str | None = Field(default=None, max_length=20)
    priority: str | None = Field(default=None, max_length=20)
    assignee_agent_id: str | None = Field(default=None, max_length=64)
    reviewer_agent_id: str | None = Field(default=None, max_length=64)
    parent_id: str | None = Field(default=None, max_length=64)
    project_id: str | None = Field(default=None, max_length=64)
    goal_id: str | None = Field(default=None, max_length=64)


class TaskAssignRequest(BaseModel):
    agent_id: str = Field(min_length=1, max_length=64)
    reviewer_agent_id: str | None = Field(default=None, max_length=64)


class TaskCheckoutRequest(BaseModel):
    agent_id: str = Field(min_length=1, max_length=64)


class TaskReleaseRequest(BaseModel):
    """POST /management/tasks/{id}/release — 비-lead(머신) 호출 시
    checkout 소유 agent_id와 일치해야 한다. lead는 생략 가능."""

    agent_id: str = Field(default="", max_length=64)


class ManagementApprovalCreateRequest(BaseModel):
    """POST /management/approvals — 범용 승인 요청 (spec 2차 D).

    safe_payload는 secret-like key(token/secret/password/api_key 등)를
    포함하면 서비스가 422로 거부한다 — 값이 아니라 키 이름 기준."""

    action_type: str = Field(min_length=1, max_length=40)
    risk_level: str = Field(default="medium", max_length=20)
    summary: str = Field(default="", max_length=300)
    safe_payload: dict[str, Any] = Field(default_factory=dict)
    requested_by_agent_id: str | None = Field(default=None, max_length=64)
    task_id: str | None = Field(default=None, max_length=64)
    run_id: str | None = Field(default=None, max_length=64)
    resource_type: str = Field(default="", max_length=40)
    resource_id: str = Field(default="", max_length=64)
    expires_at: str | None = Field(default=None, max_length=64)


class ManagementApprovalDecisionRequest(BaseModel):
    """POST /management/approvals/{id}/decision — pending에서 단 한 번."""

    decision: str = Field(pattern="^(approve|reject)$")
    comment: str = Field(default="", max_length=500)


class RecoveryActionResolveRequest(BaseModel):
    """POST /management/recovery-actions/{id}/resolve — 수동 종결."""

    status: Literal["resolved", "cancelled"] = "resolved"
    note: str = Field(default="", max_length=500)


# ---- paperclip integration (agent-management 3차 E/F) ----


# snapshot 임의 dict 필드의 직렬화 크기 상한 — 외부 입력이 무제한 dict로
# DB(JSON 컬럼)/로그 경로에 들어가는 것을 막는다(8KB ≈ artifact metadata 한도).
_SNAPSHOT_DICT_MAX_CHARS = 8192


def _bounded_snapshot_dict[V: dict[str, Any]](v: V, field: str) -> V:
    """snapshot dict 필드 크기 검증 — json.dumps 길이가 상한을 넘으면 ValueError."""
    import json

    if len(json.dumps(v, ensure_ascii=False, default=str)) > _SNAPSHOT_DICT_MAX_CHARS:
        raise ValueError(f"{field} exceeds {_SNAPSHOT_DICT_MAX_CHARS} chars")
    return v


class PaperclipExternalAgent(BaseModel):
    """외부 agent projection 1건 — payload_summary는 서버가 secret/
    transcript/adapter env를 제거한 뒤 저장한다."""

    external_id: str = Field(min_length=1, max_length=200)
    status: str = Field(default="", max_length=60)
    payload_summary: dict[str, Any] = Field(default_factory=dict)
    updated_at: str | None = Field(default=None, max_length=64)

    @field_validator("payload_summary")
    @classmethod
    def _payload_summary_bounded(cls, v: dict[str, Any]) -> dict[str, Any]:
        return _bounded_snapshot_dict(v, "payload_summary")


class PaperclipExternalTask(BaseModel):
    external_id: str = Field(min_length=1, max_length=200)
    assignee_external_id: str | None = Field(default=None, max_length=200)
    status: str = Field(default="", max_length=60)
    title: str = Field(default="", max_length=300)
    updated_at: str | None = Field(default=None, max_length=64)


class PaperclipExternalRun(BaseModel):
    external_id: str = Field(min_length=1, max_length=200)
    task_external_id: str | None = Field(default=None, max_length=200)
    agent_external_id: str | None = Field(default=None, max_length=200)
    status: str = Field(default="", max_length=60)
    token_cost_summary: dict[str, Any] = Field(default_factory=dict)
    updated_at: str | None = Field(default=None, max_length=64)

    @field_validator("token_cost_summary")
    @classmethod
    def _token_cost_summary_bounded(cls, v: dict[str, Any]) -> dict[str, Any]:
        return _bounded_snapshot_dict(v, "token_cost_summary")


class PaperclipSnapshotRequest(BaseModel):
    """POST /management/integrations/paperclip/snapshot — machine auth +
    idempotency. 멱등키는 Idempotency-Key 헤더 또는 body.idempotency_key.
    항목 수 상한은 외부 입력 크기 제한용."""

    provider: str = Field(default="paperclip", max_length=40)
    company_id: str = Field(min_length=1, max_length=200)
    agents: list[PaperclipExternalAgent] = Field(default_factory=list, max_length=1000)
    tasks: list[PaperclipExternalTask] = Field(default_factory=list, max_length=5000)
    runs: list[PaperclipExternalRun] = Field(default_factory=list, max_length=5000)
    # entity별 동기화 커서(선택) — external_sync_cursors.cursor JSON에 병합.
    cursors: dict[str, str] = Field(default_factory=dict)
    idempotency_key: str = Field(default="", max_length=200)

    @field_validator("cursors")
    @classmethod
    def _cursors_bounded(cls, v: dict[str, str]) -> dict[str, str]:
        return _bounded_snapshot_dict(v, "cursors")


class RoutineCreateRequest(BaseModel):
    """POST /management/routines — paperclip spec 3차 C.

    schedule_kind별 schedule JSON 형태는 서비스 계층
    (services/routines.validate_schedule)이 검증한다:
    - interval: {"minutes": int}
    - cron_daily: {"time": "HH:MM"}
    - cron_weekly: {"weekday": 0-6 (Mon=0), "time": "HH:MM"}
    timezone은 IANA name — 서비스가 ZoneInfo로 검증한다."""

    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    project_id: str | None = Field(default=None, max_length=64)
    task_template: dict[str, Any] = Field(default_factory=dict)
    assignee_agent_id: str | None = Field(default=None, max_length=64)
    schedule_kind: str = Field(default="interval", max_length=20)
    schedule: dict[str, Any] = Field(default_factory=dict)
    timezone: str = Field(default="UTC", max_length=64)
    max_concurrent: int = Field(default=1, ge=1, le=100)
    enabled: bool = True


class RoutinePatchRequest(BaseModel):
    """PATCH /management/routines/{id} — model_fields_set 필드만 부분 갱신.

    enabled 전환/스케줄 변경 시 next_run_at은 서비스가 재계산한다."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    project_id: str | None = Field(default=None, max_length=64)
    task_template: dict[str, Any] | None = None
    assignee_agent_id: str | None = Field(default=None, max_length=64)
    schedule_kind: str | None = Field(default=None, max_length=20)
    schedule: dict[str, Any] | None = None
    timezone: str | None = Field(default=None, max_length=64)
    max_concurrent: int | None = Field(default=None, ge=1, le=100)
    enabled: bool | None = None


# ---------- agent-management 3차 (/api/v1/management — task collab) ----------


class TaskCommentCreateRequest(BaseModel):
    """POST /management/tasks/{id}/comments — spec 3차 B.

    author_type/author_id는 서버가 인증 컨텍스트에서 결정한다 —
    클라이언트가 보낸 값은 신뢰하지 않는다(스키마에 필드 자체가 없다)."""

    body: str = Field(min_length=1, max_length=4000)
    content_type: str | None = Field(default=None, max_length=20)


class TaskArtifactCreateRequest(BaseModel):
    """POST /management/tasks/{id}/artifacts — spec 3차 B.

    uri는 https URL 또는 repo-relative 경로만 허용 — file://, javascript:,
    data:, UNC, 상위 디렉터리 탈출, 절대 경로는 서비스가 422로 거부한다.
    서버는 URI를 fetch하지 않고 메타데이터만 저장한다."""

    kind: str = Field(default="other", max_length=40)
    name: str = Field(min_length=1, max_length=200)
    uri: str = Field(min_length=1, max_length=1000)
    run_id: str | None = Field(default=None, max_length=64)
    checksum_sha256: str | None = Field(default=None, max_length=64)
    media_type: str | None = Field(default=None, max_length=100)
    size_bytes: StrictInt | None = Field(default=None, ge=0)
    metadata: dict[str, Any] | None = None


# ---------- agent-management 3차 (/api/v1/management — agent permissions) ----------


class AgentPermissionRuleIn(BaseModel):
    """PUT /management/agents/{id}/permissions의 개별 규칙 — spec 3차 D.

    capability/effect/scope_type 어휘와 scope_id 필수 조건은 라우터가
    services.permissions 상수 + store 참조 검증으로 확인한다."""

    capability: str = Field(min_length=1, max_length=60)
    effect: str = Field(default="allow", max_length=10)
    scope_type: str = Field(default="global", max_length=10)
    scope_id: str = Field(default="", max_length=200)


class AgentPermissionsPutRequest(BaseModel):
    """PUT /management/agents/{id}/permissions — 규칙 전체 교체(replace).

    프론트엔드는 {permissions:[{capability,effect,scope_type,scope_id}]}
    형태로 전체 집합을 보낸다."""

    permissions: list[AgentPermissionRuleIn] = Field(default_factory=list, max_length=200)
