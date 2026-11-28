"""SQLite/PostgreSQL 저장소 — runs/stage_runs/audit/approvals/event_links/notifications.

스토어 계층 구성:
- schema_sqlite.py / schema_pg.py — 신규 DB용 CREATE TABLE 스키마
- pg_dialect.py — SQLite 방언 → PostgreSQL 변환 + 연결 래퍼
- migrations.py — 기존 DB 컬럼/인덱스 보강 (MigrationStoreMixin)
- 이 파일 — Store 클래스 조합 + 커넥션/트랜잭션 생명주기
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg

from ..models import RunStatus as RunStatus
from ..models import StageStatus as StageStatus
from .admin import AdminStoreMixin
from .agent_sessions import AgentSessionStoreMixin
from .agents import AgentStoreMixin
from .alerts import AlertsStoreMixin
from .backups import BackupStoreMixin
from .budgets import BudgetStoreMixin
from .builder import BuilderStoreMixin
from .comms import CommsStoreMixin
from .compliance import ComplianceStoreMixin
from .costs import CostStoreMixin
from .evals import EvalStoreMixin
from .external_sync import ExternalSyncStoreMixin
from .goals import GoalStoreMixin
from .governance import GovernanceStoreMixin
from .inbox import InboxStoreMixin
from .management import ManagementStoreMixin
from .migrations import MigrationStoreMixin
from .observability import ObservabilityStoreMixin
from .orgs import OrgStoreMixin
from .permissions import PermissionStoreMixin
from .pg_dialect import _is_pg_dsn, _PGConn
from .pg_dialect import _pg_translate as _pg_translate
from .projects import ProjectStoreMixin
from .radar import RadarStoreMixin
from .retention import RetentionStoreMixin
from .routines import RoutineStoreMixin
from .runs import RunStoreMixin
from .schema_pg import SCHEMA_PG
from .schema_sqlite import SCHEMA
from .task_collab import TaskCollabStoreMixin
from .usage import UsageStoreMixin
from .userprefs import UserPrefsStoreMixin
from .users import UserStoreMixin
from .wakeups import WakeupStoreMixin
from .webhooks import WebhookStoreMixin

logger = logging.getLogger("jobmate.store")


class Store(
    MigrationStoreMixin,
    BackupStoreMixin,
    AlertsStoreMixin,
    RunStoreMixin,
    CommsStoreMixin,
    ComplianceStoreMixin,
    RadarStoreMixin,
    UserStoreMixin,
    AgentSessionStoreMixin,
    UsageStoreMixin,
    InboxStoreMixin,
    AdminStoreMixin,
    BuilderStoreMixin,
    ProjectStoreMixin,
    BudgetStoreMixin,
    WakeupStoreMixin,
    AgentStoreMixin,
    GoalStoreMixin,
    EvalStoreMixin,
    ManagementStoreMixin,
    CostStoreMixin,
    GovernanceStoreMixin,
    TaskCollabStoreMixin,
    RoutineStoreMixin,
    RetentionStoreMixin,
    PermissionStoreMixin,
    ExternalSyncStoreMixin,
    OrgStoreMixin,
    WebhookStoreMixin,
    UserPrefsStoreMixin,
    ObservabilityStoreMixin,
):
    """동기 store. path가 postgres(ql):// DSN이면 PostgreSQL, 아니면 SQLite 파일."""

    def __init__(self, path: str | Path) -> None:
        dsn = str(path)
        self._pg = _is_pg_dsn(dsn)
        self._integrity_errors: tuple[type[Exception], ...] = (
            (psycopg.errors.IntegrityError,) if self._pg else (sqlite3.IntegrityError,)
        )
        # RLock — tx() 안에서 다른 store 메서드(각자 tx()를 잡는)를 호출하는
        # 중첩 경로가 생겨도 같은 스레드면 재진입 가능해야 한다(비재진입 Lock은
        # 자기 데드락). PG/SQLite 모두 tx 직렬화 의미는 동일하다.
        self._lock = threading.RLock()
        self._local = threading.local()
        # thread ident -> conn. 죽은 스레드의 연결은 다음 생성 시 회수한다 —
        # 목록에 강한 참조를 계속 쌓으면 데몬 스레드 종료 후에도 연결이
        # 해제되지 않아 FD가 고갈된다.
        self._sqlite_conns: dict[int, Any] = {}
        self._closed = False
        if self._pg:
            self._pg_conn = _PGConn(dsn)
            for stmt in SCHEMA_PG.split(";"):
                if stmt.strip():
                    self._conn.execute(stmt)
        else:
            self._path = Path(path)
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # CPython #118172: shared statement caches can mix concurrent query results.
            with self._conn:
                self._conn.executescript(SCHEMA)
        self._migrate_agent_session_columns()
        self._migrate_approval_columns()
        self._migrate_user_columns()
        self._migrate_org_columns()
        self._migrate_run_columns()
        self._migrate_observability_indexes()
        self._migrate_budget_incident_index()
        self._migrate_wakeup_idempotency_index()
        self._migrate_agent_name_index()
        self._migrate_goal_columns()
        self._migrate_deferred_wakeup_scopes()
        self._migrate_workflow_version_columns()
        self._migrate_agent_management_columns()
        self._migrate_cost_ledger_columns()
        self._migrate_recovery_actions_index()
        self._migrate_phase3_columns()
        self._migrate_external_company_columns()
        self._ensure_external_company_indexes()
        # DSN/경로 원문은 자격증명을 포함할 수 있으므로 백엔드 종류만 기록한다
        logger.info("store opened backend=%s", "postgresql" if self._pg else "sqlite")

    @property
    def _conn(self) -> Any:
        """스레드별 커넥션.

        요청 스레드와 _automation_loop 스레드가 같은 sqlite3.Connection을
        동시에 쓰면 간헐적 segfault이 나므로(CPython sqlite3는 연결 단위
        스레드 안전이 없다) 스레드마다 연결을 하나씩 둔다. WAL + busy_timeout으로
        읽기/쓰기 동시성을 확보한다."""
        if self._pg:
            return self._pg_conn
        if self._closed:
            raise RuntimeError("store is closed")
        conn = getattr(self._local, "conn", None)
        if conn is None:
            live = {t.ident for t in threading.enumerate() if t.ident is not None}
            # 죽은 스레드의 ident는 새 스레드에 재사용될 수 있다 — 스냅샷한
            # (ident, conn) 쌍을 닫되, dict 엔트리가 다른 conn으로 교체됐다면
            # pop하지 않는다(새 스레드의 살아있는 conn을 닫는 사고 방지).
            stale = [(i, c) for i, c in self._sqlite_conns.items() if i not in live]
            for ident, stale_conn in stale:
                if self._sqlite_conns.get(ident) is stale_conn:
                    self._sqlite_conns.pop(ident, None)
                try:
                    stale_conn.close()
                except Exception as e:
                    logger.debug("reaped sqlite conn close failed: %s", e)
            conn = sqlite3.connect(
                str(self._path),
                check_same_thread=False,
                timeout=30,
                cached_statements=0,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn = conn
            self._sqlite_conns[threading.get_ident()] = conn
        return conn

    def close(self) -> None:
        """호출 스레드의 커넥션만 닫는다.

        다른 스레드(automation loop 등)의 커넥션을 여기서 닫으면 사용
        중인 연결을 닫아 segfault가 난다 — 각 스레드는 자기 커넥션을
        쓰는 동안 살아 있고, 프로세스 종료 시 함께 회수된다.
        새 커넥션 생성은 _closed 플래그로 차단한다."""
        self._closed = True
        if self._pg:
            self._pg_conn.close()
            return
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            self._local.conn = None
            self._sqlite_conns.pop(threading.get_ident(), None)
            try:
                conn.close()
            except Exception as e:
                logger.debug("sqlite conn close failed: %s", e)

    @contextmanager
    def tx(self) -> Iterator[Any]:
        with self._lock, self._conn:
            yield self._conn

    @contextmanager
    def exclusive(self) -> Iterator[None]:
        """check-then-act 구간을 프로세스 내에서 직렬화한다.

        마지막 admin 삭제/강등처럼 "조건 확인 → 변경"이 원자적이어야 하는
        경로에서 사용 — 각 store 메서드의 개별 tx()는 호출 사이에 락을
        놓으므로, 여러 호출을 하나의 임계구역으로 묶을 때 이 컨텍스트를 쓴다.
        멀티 인스턴스 배포에서는 인스턴스 간 경합이 남는다(한계로 문서화)."""
        with self._lock:
            yield

    # ---- runs ----

    # ---- stage_runs ----

    # ---- approvals ----

    # ---- audit ----

    # ---- event_links (gmail_calendar 멱등) ----

    # ---- notifications ----

    # ---- automation state ----

    # ---- 범용 상태 (automation_state key/value) ----

    # ---- message_extractions (멀티채널 수신함) ----

    # 처리 중 메시지 리스 — claim 시 retry_at을 미래로 밀어 다른 worker가
    # 같은 행을 집지 않게 한다. 프로세스 크래시면 리스 만료 후 재claim된다.
    # gmail_calendar run은 LLM 다단계라 수 분 걸릴 수 있어 리스를 넉넉히 둔다.
    # 크래시 시 이 시간만큼 처리가 지연될 뿐 중복 처리는 없다.
    _MESSAGE_LEASE_SECONDS = 1800

    # ---- scheduled_events ----

    _SCHEDULED_EVENT_FIELDS = (
        "title",
        "start_at",
        "end_at",
        "timezone",
        "location",
        "attendees_json",
        "summary",
        "status",
        "confidence",
        "reason",
        "calendar_event_id",
        "source_refs",
    )

    # ---- notification_jobs ----

    # ---- radar (skillRadar) ----

    # ---- push_subscriptions ----

    # ---- users / auth_sessions ----

    # ---- email_verifications (가입 이메일 인증) ----

    # ---- email_summaries (3줄 요약 카드) ----

    # ---- agent_sessions (로컬 에이전트 브릿지 push 세션) ----
    _SESSION_EVENT_DEDUP_SECONDS = 60.0
    _WORKING_GAP_CAP_SECONDS = 3600.0

    # ---- llm_usage (LLM 토큰 사용량) ----

    # ---- workflow_schedules (주기적 workflow run) ----

    # ---- run stats (GET /runs/stats) ----
    _STATS_FAILED = {"failed", "rejected", "budget_exhausted", "needs_human", "cancelled"}
    _STATS_TERMINAL = _STATS_FAILED | {"completed"}

    # ---- inbox_items (통합 인박스) ----

    # ---- session_activity (세션 활동 녹화/리플레이) ----
    _ACTIVITY_THROTTLE_SECONDS = 60.0
    _ACTIVITY_RETENTION_DAYS = 7

    # ---- reports (daily/weekly 리포트) ----

    # ---- llm_usage 상세 (비용 대시보드용 행 단위 조회) ----

    # ---- agent_sessions 브릿지/헬스 집계 ----

    # ---- users 관리 ----

    # ---- playbooks (원클릭 작업 템플릿) ----

    # ---- run_comments (run/diff 토론) ----

    # ---- connectors (외부 데이터 소스) ----

    # ---- custom_workflows (비주얼 빌더 WorkflowSpec) ----

    # ---- triggers (세션 상태 전이 자동화) ----

    # ---- share_links (공개 스냅샷 공유) ----

    # ---- dashboard_cards (사용자 등록 JSON 위젯) ----

    # ---- 감사 로그 뷰 (audit_events 전역 조회) ----

    # ---- team 뷰 (다중 사용자 활동 집계) ----

    # ---- export 헬퍼 ----

    # ---- projects (팀장 프로젝트 마스터/태깅) ----

    # ---- 프로젝트 태깅 (runs.project / agent_sessions.project) ----

    # ---- 리드 대시보드 집계용 raw row 조회 ----

    # ---- SSE 스트림용 증분 조회 ----

    # ---- db 메타 (헬스 상세) ----
