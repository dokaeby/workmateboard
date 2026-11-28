"""환경 설정 — pydantic-settings. 비밀값은 env 이름만 참조한다."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .models import ModelConfig


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="JOBMATE_", env_file=".env", extra="ignore")

    db_path: Path = Path("data/jobmate.db")
    # postgres(ql):// DSN — 설정 시 db_path 대신 PostgreSQL 사용 (Vercel/Neon 등)
    database_url: str = ""
    # release/VERCEL에서 DSN 없이 /tmp SQLite 휘발 DB를 허용하는 명시적 opt-out.
    # 운영 실수 방지용 — 평소에는 설정하지 않는다.
    allow_ephemeral_db: bool = False
    # debug: 모든 역할 gemini만. release: 가벼운 작업=gemini, 중요 작업=codex
    mode: Literal["debug", "release"] = "debug"
    default_model: str = "gemini-3.5-flash-lite"
    important_model: str = "codex"
    llm_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai"
    llm_api_key_env: str = "GEMINI_API_KEY"
    codex_base_url: str = "https://chatgpt.com/backend-api/codex"
    codex_api_key_env: str = "OPENAI_API_KEY"
    codex_auth_path: Path = Path("~/.codex/auth.json")

    # 역할별 서브모델 오버라이드: role -> "default"|"important"|ModelConfig dict는 env JSON으로
    role_models: dict[str, str] = Field(default_factory=dict)

    # 예산 기본값
    budget_max_tokens: int = 200_000
    budget_max_tool_calls: int = 20
    budget_max_seconds: float = 600.0
    budget_max_cost_usd: float = 5.0

    # 자동 검사 명령 (function 단계가 실행; 빈 목록이면 diff/시크릿 검사만)
    check_commands: list[str] = Field(default_factory=list)

    # gmail-calendar/calendar-agent 어댑터 (MVP: fake 또는 파일 토큰 기반 stub)
    gmail_token_path: Path | None = None
    calendar_token_path: Path | None = None
    gmail_query: str = "in:inbox newer_than:2d"
    gmail_poll_interval_seconds: int = 30
    worker_interval_seconds: int = 2
    auto_register: bool = True
    slack_signing_secret: str = ""
    telegram_webhook_secret: str = ""
    slack_channel_ids: str = ""
    telegram_chat_ids: str = ""

    # skillRadar 수집
    github_token_env: str = "GITHUB_TOKEN"  # noqa: S105 - env 변수 "이름"이지 비밀값이 아님
    radar_platforms: list[str] = Field(default_factory=lambda: ["claude", "chatgpt", "gemini"])
    radar_per_query: int = 10
    radar_max_repos: int = 30
    radar_queries_path: Path | None = None

    # LangSmith 관측성 — API 키가 있으면 LangGraph/LangSmith 트레이싱 활성화
    langsmith_tracing: bool = True
    langsmith_api_key_env: str = "LANGSMITH_API_KEY"
    langsmith_project: str = "jobmate"

    # Google OAuth 로그인(웹) — 비어 있으면 /api/v1/auth/google 비활성.
    # env: GOOGLE_OAUTH_* 우선, 레거시 GOOGLE_CLIENT_ID/SECRET/REDIRECT_URI도 인식
    # (실제 인증 경로는 app.py가 os.environ을 직접 읽는다 — 테스트 monkeypatch 호환).
    google_oauth_client_id: str = Field(
        default="",
        validation_alias=AliasChoices(
            "GOOGLE_OAUTH_CLIENT_ID", "JOBMATE_GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_CLIENT_ID"
        ),
    )
    google_oauth_client_secret: str = Field(
        default="",
        repr=False,  # 비밀값 — 로그/repr 노출 방지
        validation_alias=AliasChoices(
            "GOOGLE_OAUTH_CLIENT_SECRET",
            "JOBMATE_GOOGLE_OAUTH_CLIENT_SECRET",
            "GOOGLE_CLIENT_SECRET",
        ),
    )
    google_oauth_redirect_uri: str = Field(
        default="",
        validation_alias=AliasChoices(
            "GOOGLE_OAUTH_REDIRECT_URI",
            "JOBMATE_GOOGLE_OAUTH_REDIRECT_URI",
            "GOOGLE_REDIRECT_URI",
        ),
    )

    # cron-job.org — 대시보드 설정을 실제 cron 스케줄에 반영
    cronjob_api_key_env: str = "CRONJOB_ORG_API_KEY"
    cronjob_base_url: str = "https://api.cron-job.org"
    cronjob_job_id: str = ""  # 이메일 체크 job id (JOBMATE_CRONJOB_JOB_ID)
    cronjob_radar_job_id: str = ""  # 레이더 수집 job id (선택)
    public_base_url: str = ""  # JOBMATE_PUBLIC_BASE_URL — cron-job.org job URL 생성용

    # spec 004 — 운영·신뢰성 (아웃바운드 웹훅/백업/헬스/Sentry/메트릭)
    webhook_batch: int = 20  # JOBMATE_WEBHOOK_BATCH — 주기당 최대 delivery 처리
    webhook_timeout_seconds: float = 10.0
    backup_interval_hours: float = 0.0  # 0=비활성
    backup_dir: Path = Path("data/backups")
    backup_retention_days: int = 14
    backup_keep_count: int = 10
    backup_encryption: bool = False  # JOBMATE_BACKUP_ENCRYPTION
    backup_verify_interval_hours: float = 24.0
    backup_stale_hours: float = 30.0
    health_check_timeout: float = 2.0  # JOBMATE_HEALTH_CHECK_TIMEOUT
    disk_min_free_pct: float = 15.0  # JOBMATE_DISK_MIN_FREE_PCT — 미만이면 degraded
    sentry_dsn: str = Field(default="", repr=False)  # JOBMATE_SENTRY_DSN
    sentry_traces_sample_rate: float = 0.0  # 에러만 기본, 트레이스는 opt-in
    sentry_environment: str = ""  # 기본 settings.mode
    metrics_token: str = Field(default="", repr=False)  # JOBMATE_METRICS_TOKEN

    def model_for(self, role: str, tier: str = "default") -> ModelConfig:
        """역할별 오버라이드 → 티어 기본값 순으로 ModelConfig를 만든다.
        debug 모드에서는 tier와 무관하게 항상 default(gemini)만 사용한다."""
        if self.mode == "debug":
            tier = "default"
        override = self.role_models.get(role)
        if override == "important":
            return ModelConfig(
                model=self.important_model,
                base_url=self.codex_base_url,
                api_key_env=self.codex_api_key_env,
                provider="codex",
            )
        if override == "default":
            return ModelConfig(
                model=self.default_model,
                base_url=self.llm_base_url,
                api_key_env=self.llm_api_key_env,
            )
        if tier == "important":
            return ModelConfig(
                model=self.important_model,
                base_url=self.codex_base_url,
                api_key_env=self.codex_api_key_env,
                provider="codex",
            )
        return ModelConfig(
            model=self.default_model, base_url=self.llm_base_url, api_key_env=self.llm_api_key_env
        )


def configure_logging(settings: Settings) -> None:
    """mode별 로깅 레벨. debug=DEBUG, release=INFO (DEBUG 출력 숨김).
    토큰/비밀값은 어느 레벨에서도 로깅하지 않는다(호출부 책임)."""
    import logging

    level = logging.DEBUG if settings.mode == "debug" else logging.INFO
    logger = logging.getLogger("jobmate")
    logger.setLevel(level)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logger.addHandler(handler)
    logger.propagate = False


def configure_langsmith(settings: Settings) -> bool:
    """LangSmith 트레이싱 env를 설정. API 키가 없거나 tracing=False면 False."""
    if not settings.langsmith_tracing:
        return False
    api_key = os.environ.get(settings.langsmith_api_key_env, "")
    if not api_key:
        return False
    os.environ.setdefault("LANGCHAIN_TRACING_V2", "true")
    os.environ.setdefault("LANGCHAIN_API_KEY", api_key)
    os.environ.setdefault("LANGCHAIN_PROJECT", settings.langsmith_project)
    return True


def database_dsn(settings: Settings) -> str:
    """Store에 넘길 DSN/경로. Vercel Postgres(POSTGRES_URL)도 인식.
    Vercel에서 postgres DSN이 없으면 /tmp(인스턴스 임시)를 사용한다."""
    dsn = (
        settings.database_url
        or os.environ.get("POSTGRES_URL", "")
        or os.environ.get("POSTGRES_PRISMA_URL", "")
    )
    if dsn:
        return dsn
    if os.environ.get("VERCEL"):
        return "/tmp/jobmate.db"  # noqa: S108 - Vercel 런타임의 유일한 쓰기 가능 임시 경로
    return str(settings.db_path)


def has_pg_dsn(settings: Settings) -> bool:
    """Store/checkpointer가 PostgreSQL을 쓰는지 — DSN 3종 우선순위와 동일."""
    return bool(
        settings.database_url
        or os.environ.get("POSTGRES_URL", "")
        or os.environ.get("POSTGRES_PRISMA_URL", "")
    )


def _enforce_production_db(settings: Settings) -> None:
    """release/VERCEL에서 PG DSN 미설정 시 기동을 중단한다 (fail-fast).

    이전에는 /tmp SQLite로 조용히 휘발되어 run/체크포인트가 인스턴스와 함께
    사라졌다. 로컬 debug 모드는 기존 동작(경고 후 SQLite)을 유지한다.
    """
    if settings.allow_ephemeral_db:
        return
    if not (settings.mode == "release" or os.environ.get("VERCEL")):
        return
    if has_pg_dsn(settings):
        return
    raise RuntimeError(
        "JOBMATE_MODE=release 또는 VERCEL 환경에서는 PostgreSQL DSN이 필수입니다. "
        "JOBMATE_DATABASE_URL, POSTGRES_URL, POSTGRES_PRISMA_URL 중 하나를 설정하세요. "
        "DSN 없이 기동하면 /tmp SQLite를 사용하게 되고 모든 데이터가 휘발됩니다. "
        "일회성 로컬 검증이 필요하면 JOBMATE_ALLOW_EPHEMERAL_DB=1로 명시적으로 "
        "opt-out 할 수 있습니다."
    )


@lru_cache
def get_settings() -> Settings:
    # .env를 os.environ에 로드 — GEMINI_API_KEY/GITHUB_TOKEN 등 비밀값은
    # Settings 필드가 아니라 os.environ에서 직접 읽으므로 반드시 필요.
    load_dotenv()
    settings = Settings()
    _enforce_production_db(settings)
    # 프로덕션에서 암호화 키 없이 평문 시크릿이 저장되는 것을 기동 시점에 차단
    from .security import enforce_production_secrets

    enforce_production_secrets()
    return settings
