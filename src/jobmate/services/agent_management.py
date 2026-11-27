# mypy: disable-error-code="attr-defined"
"""Agent management 서비스 — spec 1차 C항 (facade).

구현은 역할별 모듈로 분리되어 있다 — 이 모듈은 기존 import 경로
(`services.agent_management`)를 유지하기 위한 재수출 계층이다.

- management_common: ManagementError, 상태 어휘, agent_out, emit_event,
  _require_agent/_require_task
- redaction: adapter_config/이벤트 detail secret 마스킹
- agent_lifecycle: agent status 전이·생성 필드 검증
- task_ops: task CRUD/checkout/assign/release + terminal 정리
- agent_runtime: heartbeat, wake_agent/wake_event_key
- run_projection: run 상태 → task/agent 투영

인증 정책은 라우터 책임 — 이 모듈은 "machine" 여부를 모른다.
"""

from __future__ import annotations

# --- 공용 기반 ---------------------------------------------------------
# underscore 이름은 services 외부(watchdog/approvals 등)의 기존 참조를
# 위해 `as` 별칭으로 명시적 재수출한다 — `import *` 대상은 아니다.
from .agent_lifecycle import (
    _set_agent_status_quiet as _set_agent_status_quiet,
)
from .agent_lifecycle import (
    transition_agent,
    validate_agent_create_fields,
)
from .agent_runtime import (
    _HEARTBEAT_AGENT_STATUS as _HEARTBEAT_AGENT_STATUS,
)
from .agent_runtime import (
    _wake_replay as _wake_replay,
)
from .agent_runtime import (
    heartbeat,
    wake_agent,
    wake_event_key,
)
from .management_common import (
    AGENT_STATUS_VALUES,
    TASK_PRIORITY_VALUES,
    TASK_STATUS_VALUES,
    ManagementError,
    agent_out,
    agent_out_with_task,
    emit_event,
)
from .management_common import (
    _require_agent as _require_agent,
)
from .management_common import (
    _require_task as _require_task,
)
from .redaction import (
    _SECRET_KEY_RE as _SECRET_KEY_RE,
)
from .redaction import (
    _URL_USERINFO_RE as _URL_USERINFO_RE,
)
from .redaction import (
    REDACTED,
    redact_adapter_config,
)
from .redaction import (
    _redact_value as _redact_value,
)
from .redaction import (
    _scrub_url_userinfo as _scrub_url_userinfo,
)
from .run_projection import (
    _RUN_TASK_MAP as _RUN_TASK_MAP,
)
from .run_projection import (
    _RUN_TASK_TERMINAL_BLOCKED as _RUN_TASK_TERMINAL_BLOCKED,
)
from .run_projection import (
    _RUN_TERMINAL as _RUN_TERMINAL,
)
from .run_projection import (
    _agent_has_other_active_run as _agent_has_other_active_run,
)
from .run_projection import (
    _project_agent_status as _project_agent_status,
)
from .run_projection import (
    _project_run_status_inner as _project_run_status_inner,
)
from .run_projection import (
    project_run_status,
)
from .task_ops import (
    _TASK_EVENT_FOR_STATUS as _TASK_EVENT_FOR_STATUS,
)
from .task_ops import (
    _TASK_TERMINAL as _TASK_TERMINAL,
)
from .task_ops import (
    _advance_task_status as _advance_task_status,
)
from .task_ops import (
    _finalize_terminal_task as _finalize_terminal_task,
)
from .task_ops import (
    _resolve_task_uuid as _resolve_task_uuid,
)
from .task_ops import (
    _task_ancestor_is as _task_ancestor_is,
)
from .task_ops import (
    _validate_task_agent_refs as _validate_task_agent_refs,
)
from .task_ops import (
    _validate_task_scope_refs as _validate_task_scope_refs,
)
from .task_ops import (
    assign_task,
    checkout_task,
    create_task,
    patch_task,
    release_task,
    validate_task_fields,
)

__all__ = [
    "AGENT_STATUS_VALUES",
    "ManagementError",
    "REDACTED",
    "TASK_PRIORITY_VALUES",
    "TASK_STATUS_VALUES",
    "agent_out",
    "agent_out_with_task",
    "assign_task",
    "checkout_task",
    "create_task",
    "emit_event",
    "heartbeat",
    "patch_task",
    "project_run_status",
    "redact_adapter_config",
    "release_task",
    "transition_agent",
    "validate_agent_create_fields",
    "validate_task_fields",
    "wake_agent",
    "wake_event_key",
]
