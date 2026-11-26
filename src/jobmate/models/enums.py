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
