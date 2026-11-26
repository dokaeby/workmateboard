"""영속 레코드 모델 — store 경계."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from .enums import LinkStatus, NotificationStatus, RunStatus, StageStatus


class Run(BaseModel):
    id: str
    workflow_key: str
    workflow_version: str
    input: dict[str, Any] = Field(default_factory=dict)
    status: RunStatus = RunStatus.QUEUED
    trace_id: str = ""
    created_at: datetime
    updated_at: datetime
    error: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_by: str = ""
    # 프로젝트 태그 — 자유 형식 문자열(마스터 projects.name과 ci 매칭)
    project: str = ""
    # agents 레지스트리 태깅 — ''/미설정이면 미귀속. best-effort 귀속이며
    # 세션→에이전트 자동 추론은 하지 않는다(POST /runs의 agent_id 명시만).
    agent_id: str = ""
    # goals 계층 태깅 — 명시 goal_id → 프로젝트 goal_id → NULL 순으로 해석.
    goal_id: str = ""


class StageRun(BaseModel):
    id: str
    run_id: str
    stage_key: str
    attempt: int = 1
    status: StageStatus = StageStatus.PENDING
    input: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] = Field(default_factory=dict)
    error: str = ""
    model: str = ""
    prompt_version: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None


class Approval(BaseModel):
    id: str
    run_id: str
    stage_key: str
    decision: Literal["approve", "reject"]
    comment: str = ""
    decided_by: str = "local"
    decided_at: datetime
    # 행 생성 시각 — 구 DB 행은 NULL, store가 decided_at으로 폴백
    created_at: datetime | None = None


class AuditEvent(BaseModel):
    id: str
    run_id: str
    actor: str  # system|agent:<role>|human|tool
    action: str  # stage_start|verdict|branch|approval|external_write|error ...
    object_ref: str = ""
    detail: dict[str, Any] = Field(default_factory=dict)  # 본문/토큰 금지
    created_at: datetime


class EventLink(BaseModel):
    """gmail_calendar 워크플로의 메시지↔이벤트 멱등 링크."""

    id: str
    idempotency_key: str  # UNIQUE
    gmail_message_id: str
    connection_id: str = "default"
    status: LinkStatus = LinkStatus.DISCOVERED
    calendar_event_id: str = ""
    notification_id: str = ""
    source_url: str = ""
    attempts: int = 0
    last_error: str = ""
    created_at: datetime
    updated_at: datetime


class Notification(BaseModel):
    id: str
    run_id: str = ""
    link_id: str = ""
    channel: str = "inapp"
    title: str
    body: str = ""
    source_url: str = ""
    status: NotificationStatus = NotificationStatus.PENDING
    created_at: datetime
    sent_at: datetime | None = None


class AutomationState(BaseModel):
    """gmail-calendar 자동화 토글 상태."""

    enabled: bool = False
    last_poll_at: datetime | None = None
    last_poll_result: str = ""
