"""공통 열거형."""

from enum import StrEnum


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_REVIEW = "waiting_review"
    COMPLETED = "completed"
    FAILED = "failed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    BUDGET_EXHAUSTED = "budget_exhausted"
    NEEDS_HUMAN = "needs_human"


class StageStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class AgentStatus(StrEnum):
    """관리 계층 에이전트 라이프사이클 (paperclip-agent-management-spec 1차).

    agents.status 컬럼의 신규 어휘. 레거시 값 'archived'는 terminated의
    별칭으로 계속 읽힌다(models.management.canonical_agent_status 참조)."""

    PENDING_APPROVAL = "pending_approval"
    ACTIVE = "active"
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    ERROR = "error"
    TERMINATED = "terminated"


class AgentKind(StrEnum):
    """에이전트 실행 모델 — spec의 AgentAdapterType(workflow|external).

    기존 adapter_type(claude|codex|...)은 provider 어댑터로 유지하고,
    실행 방식은 agents.kind 컬럼이 담당한다:
    - workflow: 서버 내 기존 workflow run 경로로 실행 (dev_review 등)
    - external: 외부 runner가 pull/push — 서버는 프로세스를 띄우지 않는다
      (Vercel serverless 안전)."""

    WORKFLOW = "workflow"
    EXTERNAL = "external"


class TaskStatus(StrEnum):
    BACKLOG = "backlog"
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    IN_REVIEW = "in_review"
    WAITING_APPROVAL = "waiting_approval"
    BLOCKED = "blocked"
    DONE = "done"
    CANCELLED = "cancelled"


class TaskPriority(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"


class AgentEventType(StrEnum):
    """agent_events.event_type 어휘 (spec 1차)."""

    AGENT_CREATED = "agent_created"
    AGENT_STATUS_CHANGED = "agent_status_changed"
    TASK_CREATED = "task_created"
    TASK_ASSIGNED = "task_assigned"
    TASK_CHECKED_OUT = "task_checked_out"
    TASK_CHECKOUT_RELEASED = "task_checkout_released"
    HEARTBEAT_RECEIVED = "heartbeat_received"
    WAKE_REQUESTED = "wake_requested"
    RUN_LINKED = "run_linked"
    REVIEW_REQUESTED = "review_requested"
    TASK_COMPLETED = "task_completed"
    TASK_BLOCKED = "task_blocked"
    TASK_CANCELLED = "task_cancelled"


class CostStatus(StrEnum):
    """cost_events.cost_status 어휘 (paperclip spec 2차 A).

    reported=provider 실청구, estimated=프로젝트 rate card 추정,
    unpriced=가격 산정 불가 — unpriced는 cost_microusd가 반드시 NULL이며
    0으로 대체 기록하지 않는다."""

    REPORTED = "reported"
    ESTIMATED = "estimated"
    UNPRICED = "unpriced"


class BillingType(StrEnum):
    """cost_events.billing_type 어휘 (paperclip spec 2차 A)."""

    METERED_API = "metered_api"
    SUBSCRIPTION_INCLUDED = "subscription_included"
    SUBSCRIPTION_OVERAGE = "subscription_overage"
    CREDITS = "credits"
    FIXED = "fixed"
    UNKNOWN = "unknown"


class ApprovalStatus(StrEnum):
    """approval_requests.status 어휘 (paperclip spec 2차 D)."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class ApprovalRisk(StrEnum):
    """approval_requests.risk_level 어휘 — high/critical은 로그인된 사람만 결정."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ApprovalActionType(StrEnum):
    """approval_requests.action_type 어휘 (paperclip spec 2차 D)."""

    RUN_RESUME = "run_resume"
    BUDGET_RAISE = "budget_raise"
    AGENT_HIRE = "agent_hire"
    AGENT_TERMINATE = "agent_terminate"
    EXTERNAL_SEND = "external_send"
    DEPLOY = "deploy"
    DESTRUCTIVE_TOOL = "destructive_tool"
    RECOVERY = "recovery"


class RecoveryKind(StrEnum):
    """recovery_actions.kind 어휘 (paperclip spec 2차 E)."""

    STALE_HEARTBEAT = "stale_heartbeat"
    STRANDED_TASK = "stranded_task"
    STUCK_RUN = "stuck_run"
    MISSING_COST = "missing_cost"
    FAILED_RESUME = "failed_resume"


class RecoveryStatus(StrEnum):
    """recovery_actions.status 어휘 — active|escalated는 source당 1건만 허용."""

    ACTIVE = "active"
    ESCALATED = "escalated"
    RESOLVED = "resolved"
    CANCELLED = "cancelled"


class RecoveryOwnerType(StrEnum):
    """recovery_actions.owner_type 어휘."""

    SYSTEM = "system"
    AGENT = "agent"
    HUMAN = "human"


class BudgetWindowKind(StrEnum):
    """budget_policies.window_kind 어휘 (paperclip spec 2차 B)."""

    CALENDAR_MONTH_UTC = "calendar_month_utc"
    LIFETIME = "lifetime"


class LinkStatus(StrEnum):
    DISCOVERED = "discovered"
    EXTRACTED = "extracted"
    VALIDATED = "validated"
    CALENDAR_CREATED = "calendar_created"
    NOTIFICATION_QUEUED = "notification_queued"
    NOTIFIED = "notified"
    NEEDS_REVIEW = "needs_review"
    DUPLICATE = "duplicate"
    CONNECTION_ERROR = "connection_error"
    RETRY_WAIT = "retry_wait"


class NotificationStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


class DoneState(StrEnum):
    """gstack식 단계 완료 프로토콜."""

    DONE = "DONE"
    DONE_WITH_CONCERNS = "DONE_WITH_CONCERNS"
    BLOCKED = "BLOCKED"
    NEEDS_CONTEXT = "NEEDS_CONTEXT"


class Severity(StrEnum):
    BLOCKER = "blocker"
    MAJOR = "major"
    MINOR = "minor"
    NIT = "nit"


class Confidence(StrEnum):
    HIGH = "high"  # 7+
    MEDIUM = "medium"  # 5-6
    LOW = "low"  # 3-4
    VERY_LOW = "very_low"  # 1-2


class VerdictDecision(StrEnum):
    APPROVE = "approve"
    REQUEST_CHANGES = "request_changes"
    PASS = "pass"  # noqa: S105
    FAIL = "fail"
    NEEDS_HUMAN = "needs_human"


class ScheduleKind(StrEnum):
    """routine.schedule_kind 어휘 (paperclip spec 3차 C).

    croniter 의존 없이 interval|cron_daily|cron_weekly 3종만 지원한다 —
    cron 표현식 파서는 services/routines.py의 next-run 계산기가 담당."""

    INTERVAL = "interval"
    CRON_DAILY = "cron_daily"
    CRON_WEEKLY = "cron_weekly"


class RoutineStatus(StrEnum):
    """routine의 파생 상태 — routines.enabled INTEGER 플래그의 API 어휘.

    테이블에 status 컬럼은 없고 enabled=1→active, 0→paused로 해석한다."""

    ACTIVE = "active"
    PAUSED = "paused"


class RoutineRunStatus(StrEnum):
    """routine_runs.status 어휘 (paperclip spec 3차 C/G).

    pending: run 행 생성 직후 / created: task 생성 성공 /
    skipped: max_concurrent 등으로 이번 회차 생략 / failed: task 생성 실패."""

    PENDING = "pending"
    CREATED = "created"
    SKIPPED = "skipped"
    FAILED = "failed"


class GoalStatus(StrEnum):
    """goals.status 어휘 (paperclip spec 3차 A).

    레거시 'done'/'archived'는 _migrate_phase3_columns가 achieved/cancelled로
    변환한다 — 신규 행은 이 어휘만 사용한다."""

    ACTIVE = "active"
    PAUSED = "paused"
    ACHIEVED = "achieved"
    CANCELLED = "cancelled"


class ProjectStatus(StrEnum):
    """projects.status 어휘 (paperclip spec 3차 A)."""

    PLANNED = "planned"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class TaskCommentAuthorType(StrEnum):
    """task_comments.author_type 어휘 (paperclip spec 3차 B).

    서버가 인증 컨텍스트에서만 결정한다 — 클라이언트 지정 author는 신뢰하지
    않는다."""

    HUMAN = "human"
    AGENT = "agent"
    SYSTEM = "system"


class TaskCommentContentType(StrEnum):
    """task_comments.content_type 어휘 — UI는 HTML로 실행하지 않는다."""

    TEXT = "text"
    MARKDOWN = "markdown"


class TaskArtifactKind(StrEnum):
    """task_artifacts.kind 어휘 (paperclip spec 3차 B)."""

    PLAN = "plan"
    DIFF = "diff"
    TEST_REPORT = "test_report"
    SECURITY_REPORT = "security_report"
    REVIEW = "review"
    SCREENSHOT = "screenshot"
    DEPLOYMENT = "deployment"
    OTHER = "other"


class AgentCapability(StrEnum):
    """agent_permissions.capability 어휘 (paperclip spec 3차 D 최소 집합)."""

    TASK_READ = "task:read"
    TASK_CREATE = "task:create"
    TASK_ASSIGN = "task:assign"
    TASK_UPDATE = "task:update"
    AGENT_READ = "agent:read"
    AGENT_MANAGE = "agent:manage"
    AGENT_WAKE = "agent:wake"
    RUN_EXECUTE = "run:execute"
    RUN_APPROVE = "run:approve"
    COST_READ = "cost:read"
    BUDGET_MANAGE = "budget:manage"
    ARTIFACT_CREATE = "artifact:create"
    EXTERNAL_SEND = "external:send"
    DEPLOY_EXECUTE = "deploy:execute"
    DESTRUCTIVE_EXECUTE = "destructive:execute"
    INTEGRATION_MANAGE = "integration:manage"


class PermissionEffect(StrEnum):
    """agent_permissions.effect 어휘 — deny가 allow보다 항상 우선."""

    ALLOW = "allow"
    DENY = "deny"


class PermissionScopeType(StrEnum):
    """agent_permissions.scope_type 어휘 — 좁은 scope가 넓은 scope보다 먼저 평가."""

    GLOBAL = "global"
    PROJECT = "project"
    TASK = "task"
