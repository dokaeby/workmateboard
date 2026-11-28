# mypy: disable-error-code="attr-defined"
"""Store 스키마 마이그레이션 — jobmate.store.db에서 분리.

기존 DB에 나중에 추가된 컬럼/인덱스를 ALTER로 보강한다.
신규 스키마 변경은 SCHEMA/SCHEMA_PG에 넣고, 기존 DB 호환 보강만 여기 둔다.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from .util import _iso, _now

logger = logging.getLogger("jobmate.store.migrations")


class MigrationStoreMixin:
    def _ensure_columns(self, table: str, wanted: dict[str, str]) -> None:
        """CREATE TABLE IF NOT EXISTS는 기존 테이블을 변경하지 않으므로
        나중에 추가된 컬럼은 ALTER로 보강한다.
        wanted: {컬럼명: 컬럼 DDL} — 테이블/컬럼명은 내부 상수만 사용."""
        try:
            if self._pg:
                rows = self._conn.execute(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_name=? AND table_schema = current_schema()",
                    (table,),
                ).fetchall()
                have = {r["column_name"] for r in rows}
            else:
                rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
                have = {r["name"] for r in rows}
        except Exception as e:
            logger.warning("%s column check failed: %s", table, e)
            return
        for col, ddl in sorted(wanted.items()):
            if col in have:
                continue
            try:
                if self._pg:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {ddl}")
                else:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
                logger.info("%s column added: %s", table, col)
            except Exception as e:
                logger.warning("%s add column %s failed: %s", table, col, e)

    def _migrate_agent_session_columns(self) -> None:
        """기존 DB의 agent_sessions에 신규 컬럼 보강."""
        self._ensure_columns(
            "agent_sessions",
            {
                "ended_summary": "TEXT",
                "artifacts": "TEXT",
                "machine": "TEXT",
                "tokens_in": "INTEGER",
                "tokens_out": "INTEGER",
            },
        )

    def _migrate_approval_columns(self) -> None:
        """기존 DB의 approvals에 created_at 컬럼 보강."""
        self._ensure_columns("approvals", {"created_at": "TEXT"})

    def _migrate_user_columns(self) -> None:
        """기존 DB의 users에 role/email_verified 컬럼 보강.
        email_verified 기본값 1 — 기존 계정은 이미 로그인 중이므로 검증을
        강요하지 않는다. 신규 비밀번호 가입만 create_user가 명시적으로 0을 넣는다."""
        self._ensure_columns("users", {"role": "TEXT NOT NULL DEFAULT 'user'"})
        self._ensure_columns("users", {"email_verified": "INTEGER NOT NULL DEFAULT 1"})
        # 로그인 보안 정책(spec 002 US3) — 신규 기기 알림용 마지막 로그인 메타
        self._ensure_columns(
            "users",
            {
                "last_login_ip": "TEXT NOT NULL DEFAULT ''",
                "last_login_fingerprint": "TEXT NOT NULL DEFAULT ''",
                "last_login_at": "TEXT NOT NULL DEFAULT ''",
            },
        )
        # 세션 유휴 만료(spec 002 FR-017) — 마지막 활동 시각
        self._ensure_columns("auth_sessions", {"last_seen_at": "TEXT"})

    def _migrate_org_columns(self) -> None:
        """조직 테넌시(spec 003 US1/US3) — 세션 조직 컨텍스트 컬럼 +
        기존 단일 테넌트 데이터의 기본 조직 백필.

        organizations/organization_members 테이블은 SCHEMA executescript가
        이미 생성했다. 여기서는 ① auth_sessions.current_org_id 컬럼 보강,
        ② 조직이 하나도 없으면 default 조직 생성 + 기존 users 전원 멤버 등록,
        ③ current_org_id가 빈 세션을 기본 조직으로 채운다."""
        self._ensure_columns("auth_sessions", {"current_org_id": "TEXT NOT NULL DEFAULT ''"})
        # spec 003 US3/FR-013 — 조직 스코프 테이블의 org_id 컬럼 보강
        # (신규 DB는 SCHEMA가 생성, 기존 DB는 여기서 ALTER).
        _ORG_SCOPED_TABLES = (
            "runs",
            "projects",
            "tasks",
            "agents",
            "goals",
            "evals",
            # eval_runs는 파생 테이블이지만 org_id를 비정규화해 직접 필터한다
            # (spec 003 US3 리뷰 — 조직 간 eval 활동 유출 차단).
            "eval_runs",
            "custom_workflows",
            "workflow_revisions",
            "connectors",
            "routines",
            "agent_permissions",
            "user_permissions",
            "agent_events",
            "task_comments",
            "task_artifacts",
            "cost_events",
            "approval_requests",
            "recovery_actions",
            "budget_policies",
            "budget_incidents",
            "inbox_items",
            "notifications",
            "reports",
            "playbooks",
            "dashboard_cards",
            "share_links",
            "workflow_schedules",
            "triggers",
            "event_links",
            "llm_usage",
            "scheduled_events",
            "notification_jobs",
            "message_extractions",
            "email_summaries",
            "agent_sessions",
            "session_activity",
            "audit_events",
        )
        for _t in _ORG_SCOPED_TABLES:
            try:
                self._ensure_columns(_t, {"org_id": "TEXT NOT NULL DEFAULT ''"})
            except Exception as e:
                logger.warning("org_id column ensure failed on %s: %s", _t, e)
        try:
            n_orgs = self._conn.execute("SELECT COUNT(*) AS n FROM organizations").fetchone()["n"]
            if n_orgs == 0:
                # 사용자가 없어도 default 조직은 생성한다 — API 키/로컬 모드로
                # 먼저 생긴 행이 존재하지 않는 조직 id로 백필되는 것을 막기 위함.
                now = _iso(_now())
                with self.tx() as c:
                    c.execute(
                        "INSERT INTO organizations(id,name,slug,kind,status,"
                        "created_by,created_at,updated_at)"
                        " VALUES(?,?,?,?,?,?,?,?)",
                        (
                            "defaultorg01",
                            "Default",
                            "default",
                            "team",
                            "active",
                            "",
                            now,
                            now,
                        ),
                    )
                users = self._conn.execute(
                    "SELECT id,email,display_name,role,created_at FROM users"
                    " ORDER BY created_at ASC, id ASC"
                ).fetchall()
                if users:
                    # spec FR-015 — 가장 먼저 생성된 admin=org_owner,
                    # 나머지 admin=org_admin, member=member.
                    # admin이 없으면 최초 사용자를 owner로(조직 고아 방지).
                    admins = [u for u in users if str(u["role"] or "") == "admin"]
                    owner_id = str((admins or users)[0]["id"])
                    admin_ids = {str(u["id"]) for u in admins}
                    for u in users:
                        uid = str(u["id"])
                        if uid == owner_id:
                            role = "org_owner"
                        elif uid in admin_ids:
                            role = "org_admin"
                        else:
                            role = "member"
                        self.add_org_member("defaultorg01", uid, role)
                    with self.tx() as c:
                        c.execute(
                            "UPDATE auth_sessions SET current_org_id='defaultorg01'"
                            " WHERE current_org_id='' OR current_org_id IS NULL"
                        )
                    logger.info("org backfill: default org + %d members", len(users))
                else:
                    logger.info("org backfill: default org (no users yet)")
            # spec FR-015③ — org_id=''인 기존 행을 기본 조직으로 백필.
            # 활성 조직이 하나도 없으면(전부 soft-delete) 백필을 건너뛰어
            # 존재하지 않는 조직 id를 찍지 않는다.
            r = self._conn.execute(
                "SELECT id FROM organizations WHERE status != 'deleted' ORDER BY created_at LIMIT 1"
            ).fetchone()
            default_oid = str(r["id"]) if r else ""
            if default_oid:
                with self.tx() as c:
                    for _t in _ORG_SCOPED_TABLES:
                        try:
                            c.execute(
                                f"UPDATE {_t} SET org_id=? WHERE org_id=''",  # noqa: S608
                                (default_oid,),
                            )
                        except Exception as e:
                            logger.warning("org_id backfill failed on %s: %s", _t, e)
            # spec 003 US3 — 조직 스코프 조회용 인덱스. SCHEMA의
            # CREATE TABLE IF NOT EXISTS는 기존 DB에 컬럼을 추가하지 않으므로
            # 인덱스는 컬럼 보강 이후인 여기서 생성한다(신규 DB도 동일 경로).
            for _t in _ORG_SCOPED_TABLES:
                try:
                    with self.tx() as c:
                        c.execute(
                            f"CREATE INDEX IF NOT EXISTS idx_{_t}_org ON {_t}(org_id)"  # noqa: S608
                        )
                except Exception as e:
                    logger.warning("org index failed on %s: %s", _t, e)
        except Exception as e:
            logger.warning("org backfill failed: %s", e)
        # spec 003 US3/FR-018 — org_id 컬럼 보강·백필 이후에 실행해야
        # 새 UNIQUE/PK에 org_id 값이 채워진 상태에서 제약이 잡힌다.
        try:
            self._migrate_org_unique_rescope()
        except Exception as e:
            logger.warning("org unique rescope failed: %s", e)

    # spec 003 US3/FR-018 — org-scoped 테이블의 전역 UNIQUE/PK를 org_id
    # 포함으로 재스코프. 새 제약은 기존 제약의 상위집합이라 데이터
    # 중복 제거 없이 제약만 교체하면 된다.
    #
    # SQLite 재작성 스펙 — old_unique는 "이 컬럼 집합의 UNIQUE가 남아
    # 있으면 재작성" 트리거(PRAGMA index_list의 unique 인덱스 컬럼 집합,
    # PK 자동 인덱스 포함). cols는 신규 테이블 컬럼 순서 — 구 테이블에
    # 아직 없는 컬럼(이후 _ensure_columns가 보강하는 것들)은 INSERT에서
    # 제외하고 DEFAULT로 둔다. indexes는 재작성으로 소실되는 보조 인덱스
    # 복원문(SCHEMA 정의와 동일).
    _ORG_UNIQUE_SQLITE_REBUILDS: dict[str, dict[str, Any]] = {
        "message_extractions": {
            "old_unique": [frozenset({"source_provider", "source_id"})],
            "cols": [
                "source_provider",
                "source_id",
                "thread_id",
                "extraction_json",
                "processing_status",
                "error_message",
                "attempts",
                "retry_at",
                "source_text",
                "created_at",
                "org_id",
            ],
            "ddl": """CREATE TABLE {t} (
  source_provider TEXT NOT NULL,
  source_id TEXT NOT NULL,
  thread_id TEXT NOT NULL DEFAULT '',
  extraction_json TEXT NOT NULL DEFAULT '{}',
  processing_status TEXT NOT NULL DEFAULT 'pending',
  error_message TEXT NOT NULL DEFAULT '',
  attempts INTEGER NOT NULL DEFAULT 0,
  retry_at TEXT NOT NULL DEFAULT '',
  source_text TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  org_id TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (source_provider, source_id, org_id)
)""",
            "indexes": [],
        },
        "email_summaries": {
            "old_unique": [frozenset({"source_provider", "source_id"})],
            "cols": [
                "source_provider",
                "source_id",
                "subject",
                "sender",
                "summary",
                "message_at",
                "checked_at",
                "org_id",
            ],
            "ddl": """CREATE TABLE {t} (
  source_provider TEXT NOT NULL,
  source_id TEXT NOT NULL,
  subject TEXT NOT NULL DEFAULT '',
  sender TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT '',
  message_at TEXT NOT NULL DEFAULT '',
  checked_at TEXT NOT NULL,
  org_id TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (source_provider, source_id, org_id)
)""",
            "indexes": [],
        },
        "custom_workflows": {
            "old_unique": [frozenset({"name"})],
            "cols": [
                "name",
                "spec",
                "created_at",
                "latest_revision",
                "updated_at",
                "org_id",
            ],
            "ddl": """CREATE TABLE {t} (
  name TEXT NOT NULL,
  spec TEXT NOT NULL,
  created_at TEXT NOT NULL,
  latest_revision INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT,
  org_id TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (name, org_id)
)""",
            "indexes": [],
        },
        "workflow_revisions": {
            "old_unique": [frozenset({"name", "revision"})],
            "cols": [
                "id",
                "name",
                "revision",
                "spec",
                "note",
                "created_by",
                "created_at",
                "org_id",
            ],
            "ddl": """CREATE TABLE {t} (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  revision INTEGER NOT NULL,
  spec TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  created_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  org_id TEXT NOT NULL DEFAULT '',
  UNIQUE(org_id, name, revision)
)""",
            "indexes": [
                "CREATE INDEX IF NOT EXISTS idx_workflow_revisions_name ON workflow_revisions(name)"
            ],
        },
        "budget_policies": {
            "old_unique": [frozenset({"scope_type", "scope_id"})],
            "cols": [
                "id",
                "scope_type",
                "scope_id",
                "window_kind",
                "amount_usd",
                "warn_percent",
                "hard_stop_enabled",
                "is_active",
                "created_at",
                "updated_at",
                "org_id",
                "amount_microusd",
                "reservation_microusd",
                "unpriced_usage_policy",
            ],
            "ddl": """CREATE TABLE {t} (
  id TEXT PRIMARY KEY,
  scope_type TEXT NOT NULL,
  scope_id TEXT NOT NULL DEFAULT '',
  window_kind TEXT NOT NULL DEFAULT 'calendar_month_utc',
  amount_usd REAL NOT NULL,
  warn_percent INTEGER NOT NULL DEFAULT 80,
  hard_stop_enabled INTEGER NOT NULL DEFAULT 1,
  is_active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  org_id TEXT NOT NULL DEFAULT '',
  amount_microusd INTEGER NOT NULL DEFAULT 0,
  reservation_microusd INTEGER NOT NULL DEFAULT 0,
  unpriced_usage_policy TEXT NOT NULL DEFAULT 'allow',
  UNIQUE(scope_type, scope_id, org_id)
)""",
            "indexes": [],
        },
        "agent_permissions": {
            "old_unique": [
                frozenset(
                    {
                        "agent_id",
                        "capability",
                        "scope_type",
                        "scope_id",
                        "effect",
                    }
                )
            ],
            "cols": [
                "id",
                "agent_id",
                "capability",
                "effect",
                "scope_type",
                "scope_id",
                "created_at",
                "org_id",
            ],
            "ddl": """CREATE TABLE {t} (
  id TEXT PRIMARY KEY,
  agent_id TEXT NOT NULL,
  capability TEXT NOT NULL,
  effect TEXT NOT NULL,
  scope_type TEXT NOT NULL DEFAULT 'global',
  scope_id TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  org_id TEXT NOT NULL DEFAULT '',
  UNIQUE(agent_id, capability, scope_type, scope_id, effect, org_id)
)""",
            "indexes": [
                "CREATE INDEX IF NOT EXISTS idx_agent_permissions_agent"
                " ON agent_permissions(agent_id)"
            ],
        },
        "user_permissions": {
            "old_unique": [
                frozenset(
                    {
                        "user_id",
                        "capability",
                        "scope_type",
                        "scope_id",
                        "effect",
                    }
                )
            ],
            "cols": [
                "id",
                "user_id",
                "capability",
                "effect",
                "scope_type",
                "scope_id",
                "created_at",
                "org_id",
            ],
            "ddl": """CREATE TABLE {t} (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  capability TEXT NOT NULL,
  effect TEXT NOT NULL,
  scope_type TEXT NOT NULL DEFAULT 'global',
  scope_id TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  org_id TEXT NOT NULL DEFAULT '',
  UNIQUE(user_id, capability, scope_type, scope_id, effect, org_id)
)""",
            "indexes": [
                "CREATE INDEX IF NOT EXISTS idx_user_permissions_user ON user_permissions(user_id)"
            ],
        },
        "cost_events": {
            "old_unique": [frozenset({"idempotency_key"})],
            "cols": [
                "id",
                "idempotency_key",
                "agent_id",
                "task_id",
                "run_id",
                "stage_run_id",
                "provider",
                "model",
                "billing_type",
                "cost_status",
                "input_tokens",
                "cached_input_tokens",
                "output_tokens",
                "cost_microusd",
                "pricing_source",
                "pricing_version",
                "occurred_at",
                "created_at",
                "org_id",
            ],
            "ddl": """CREATE TABLE {t} (
  id TEXT PRIMARY KEY,
  idempotency_key TEXT NOT NULL,
  agent_id TEXT NOT NULL DEFAULT '',
  task_id TEXT,
  run_id TEXT,
  stage_run_id TEXT,
  provider TEXT NOT NULL DEFAULT '',
  model TEXT NOT NULL DEFAULT '',
  billing_type TEXT NOT NULL DEFAULT 'unknown',
  cost_status TEXT NOT NULL DEFAULT 'unpriced',
  input_tokens INTEGER NOT NULL DEFAULT 0,
  cached_input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  cost_microusd INTEGER,
  pricing_source TEXT NOT NULL DEFAULT '',
  pricing_version TEXT NOT NULL DEFAULT '',
  occurred_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  org_id TEXT NOT NULL DEFAULT '',
  UNIQUE(org_id, idempotency_key)
)""",
            "indexes": [
                "CREATE INDEX IF NOT EXISTS idx_cost_events_agent"
                " ON cost_events(agent_id, occurred_at)",
                "CREATE INDEX IF NOT EXISTS idx_cost_events_task"
                " ON cost_events(task_id, occurred_at)",
                "CREATE INDEX IF NOT EXISTS idx_cost_events_run ON cost_events(run_id)",
                "CREATE INDEX IF NOT EXISTS idx_cost_events_stage ON cost_events(stage_run_id)",
                "CREATE INDEX IF NOT EXISTS idx_cost_events_occurred ON cost_events(occurred_at)",
            ],
        },
        "agents": {
            "old_unique": [frozenset({"name"})],
            "cols": [
                "id",
                "name",
                "adapter_type",
                "role",
                "title",
                "description",
                "capabilities",
                "kind",
                "adapter_config",
                "reports_to",
                "status",
                "monthly_budget_usd",
                "monthly_budget_microusd",
                "last_seen_at",
                "created_by",
                "created_at",
                "updated_at",
                "org_id",
            ],
            "ddl": """CREATE TABLE {t} (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  adapter_type TEXT NOT NULL,
  role TEXT,
  title TEXT,
  description TEXT,
  capabilities TEXT NOT NULL DEFAULT '[]',
  kind TEXT NOT NULL DEFAULT 'workflow',
  adapter_config TEXT NOT NULL DEFAULT '{}',
  reports_to TEXT REFERENCES agents(id),
  status TEXT NOT NULL DEFAULT 'active',
  monthly_budget_usd REAL,
  monthly_budget_microusd INTEGER NOT NULL DEFAULT 0,
  last_seen_at TEXT,
  created_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  org_id TEXT NOT NULL DEFAULT ''
)""",
            "indexes": [
                "CREATE INDEX IF NOT EXISTS idx_agents_reports_to ON agents(reports_to)",
                "CREATE INDEX IF NOT EXISTS idx_agents_status ON agents(status)",
            ],
        },
        "event_links": {
            "old_unique": [frozenset({"idempotency_key"})],
            "cols": [
                "id",
                "idempotency_key",
                "gmail_message_id",
                "connection_id",
                "status",
                "calendar_event_id",
                "notification_id",
                "source_url",
                "attempts",
                "last_error",
                "created_at",
                "updated_at",
                "org_id",
            ],
            "ddl": """CREATE TABLE {t} (
  id TEXT PRIMARY KEY,
  idempotency_key TEXT NOT NULL,
  gmail_message_id TEXT NOT NULL,
  connection_id TEXT NOT NULL DEFAULT 'default',
  status TEXT NOT NULL,
  calendar_event_id TEXT NOT NULL DEFAULT '',
  notification_id TEXT NOT NULL DEFAULT '',
  source_url TEXT NOT NULL DEFAULT '',
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  org_id TEXT NOT NULL DEFAULT '',
  UNIQUE(org_id, idempotency_key)
)""",
            "indexes": [
                "CREATE INDEX IF NOT EXISTS idx_links_msg ON event_links(gmail_message_id)"
            ],
        },
        "agent_sessions": {
            "old_unique": [frozenset({"tool", "session_key"})],
            "cols": [
                "id",
                "tool",
                "session_key",
                "project",
                "title",
                "status",
                "doing",
                "ask",
                "ended_summary",
                "artifacts",
                "machine",
                "tokens_in",
                "tokens_out",
                "last_event_at",
                "created_at",
                "updated_at",
                "org_id",
            ],
            "ddl": """CREATE TABLE {t} (
  id TEXT PRIMARY KEY,
  tool TEXT NOT NULL,
  session_key TEXT NOT NULL,
  project TEXT,
  title TEXT,
  status TEXT NOT NULL,
  doing TEXT,
  ask TEXT,
  ended_summary TEXT,
  artifacts TEXT,
  machine TEXT,
  tokens_in INTEGER,
  tokens_out INTEGER,
  last_event_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  org_id TEXT NOT NULL DEFAULT '',
  UNIQUE(org_id, tool, session_key)
)""",
            "indexes": [
                "CREATE INDEX IF NOT EXISTS idx_agent_sessions_status ON agent_sessions(status)"
            ],
        },
        "tasks": {
            "old_unique": [frozenset({"identifier"})],
            "cols": [
                "id",
                "identifier",
                "title",
                "description",
                "status",
                "priority",
                "assignee_agent_id",
                "reviewer_agent_id",
                "parent_id",
                "project_id",
                "goal_id",
                "current_run_id",
                "checkout_agent_id",
                "checkout_at",
                "created_by",
                "created_at",
                "updated_at",
                "org_id",
            ],
            "ddl": """CREATE TABLE {t} (
  id TEXT PRIMARY KEY,
  identifier TEXT NOT NULL,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'backlog',
  priority TEXT NOT NULL DEFAULT 'medium',
  assignee_agent_id TEXT,
  reviewer_agent_id TEXT,
  parent_id TEXT,
  project_id TEXT,
  goal_id TEXT,
  current_run_id TEXT,
  checkout_agent_id TEXT,
  checkout_at TEXT,
  created_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  org_id TEXT NOT NULL DEFAULT '',
  UNIQUE(org_id, identifier)
)""",
            "indexes": [
                "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)",
                "CREATE INDEX IF NOT EXISTS idx_tasks_assignee ON tasks(assignee_agent_id)",
                "CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_id)",
                "CREATE INDEX IF NOT EXISTS idx_tasks_checkout ON tasks(checkout_agent_id)",
            ],
        },
        "agent_events": {
            "old_unique": [frozenset({"idempotency_key"})],
            "cols": [
                "id",
                "idempotency_key",
                "event_type",
                "agent_id",
                "task_id",
                "run_id",
                "actor",
                "summary",
                "detail",
                "occurred_at",
                "created_at",
                "org_id",
            ],
            "ddl": """CREATE TABLE {t} (
  id TEXT PRIMARY KEY,
  idempotency_key TEXT NOT NULL,
  event_type TEXT NOT NULL,
  agent_id TEXT,
  task_id TEXT,
  run_id TEXT,
  actor TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT '',
  detail TEXT NOT NULL DEFAULT '{}',
  occurred_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  org_id TEXT NOT NULL DEFAULT '',
  UNIQUE(org_id, idempotency_key)
)""",
            "indexes": [
                "CREATE INDEX IF NOT EXISTS idx_agent_events_agent"
                " ON agent_events(agent_id, created_at)",
                "CREATE INDEX IF NOT EXISTS idx_agent_events_task"
                " ON agent_events(task_id, created_at)",
                "CREATE INDEX IF NOT EXISTS idx_agent_events_run"
                " ON agent_events(run_id, created_at)",
            ],
        },
    }

    # PG 재스코프 스펙 — (table, constraint_type, old_cols, new_cols|None,
    # add_ddl|None, constraint_name|None). new_cols가 None이면 제약 삭제만
    # (agents.name — per-org 유일성은 idx_agents_name_lower 인덱스가 담당).
    _PG_ORG_UNIQUE_RESPECS: tuple[
        tuple[str, str, tuple[str, ...], tuple[str, ...] | None, str | None, str | None],
        ...,
    ] = (
        (
            "custom_workflows",
            "PRIMARY KEY",
            ("name",),
            ("name", "org_id"),
            "PRIMARY KEY(name, org_id)",
            "custom_workflows_org_pk",
        ),
        (
            "message_extractions",
            "PRIMARY KEY",
            ("source_provider", "source_id"),
            ("source_provider", "source_id", "org_id"),
            "PRIMARY KEY(source_provider, source_id, org_id)",
            "message_extractions_org_pk",
        ),
        (
            "email_summaries",
            "PRIMARY KEY",
            ("source_provider", "source_id"),
            ("source_provider", "source_id", "org_id"),
            "PRIMARY KEY(source_provider, source_id, org_id)",
            "email_summaries_org_pk",
        ),
        (
            "workflow_revisions",
            "UNIQUE",
            ("name", "revision"),
            ("org_id", "name", "revision"),
            "UNIQUE(org_id, name, revision)",
            "workflow_revisions_org_name_rev_key",
        ),
        (
            "budget_policies",
            "UNIQUE",
            ("scope_type", "scope_id"),
            ("scope_type", "scope_id", "org_id"),
            "UNIQUE(scope_type, scope_id, org_id)",
            "budget_policies_scope_org_key",
        ),
        (
            "agent_permissions",
            "UNIQUE",
            ("agent_id", "capability", "scope_type", "scope_id", "effect"),
            ("agent_id", "capability", "scope_type", "scope_id", "effect", "org_id"),
            "UNIQUE(agent_id, capability, scope_type, scope_id, effect, org_id)",
            "agent_permissions_scope_org_key",
        ),
        (
            "user_permissions",
            "UNIQUE",
            ("user_id", "capability", "scope_type", "scope_id", "effect"),
            ("user_id", "capability", "scope_type", "scope_id", "effect", "org_id"),
            "UNIQUE(user_id, capability, scope_type, scope_id, effect, org_id)",
            "user_permissions_scope_org_key",
        ),
        (
            "cost_events",
            "UNIQUE",
            ("idempotency_key",),
            ("org_id", "idempotency_key"),
            "UNIQUE(org_id, idempotency_key)",
            "cost_events_org_idem_key",
        ),
        (
            "event_links",
            "UNIQUE",
            ("idempotency_key",),
            ("org_id", "idempotency_key"),
            "UNIQUE(org_id, idempotency_key)",
            "event_links_org_idem_key",
        ),
        (
            "agent_sessions",
            "UNIQUE",
            ("tool", "session_key"),
            ("org_id", "tool", "session_key"),
            "UNIQUE(org_id, tool, session_key)",
            "agent_sessions_org_tool_key",
        ),
        (
            "tasks",
            "UNIQUE",
            ("identifier",),
            ("org_id", "identifier"),
            "UNIQUE(org_id, identifier)",
            "tasks_org_ident_key",
        ),
        (
            "agent_events",
            "UNIQUE",
            ("idempotency_key",),
            ("org_id", "idempotency_key"),
            "UNIQUE(org_id, idempotency_key)",
            "agent_events_org_idem_key",
        ),
        ("agents", "UNIQUE", ("name",), None, None, None),
    )

    def _migrate_org_unique_rescope(self) -> None:
        """spec 003 US3/FR-018 — org-scoped 테이블의 UNIQUE/PK 재스코프.

        테이블 재작성(SQLite)·제약 교체(PG)·name_lower 인덱스 교체를
        수행한다. 행 단위/테이블 단위 best-effort — 실패는 경고만 남기고
        다음 기동에 재시도한다(_migrate_* 패턴과 동일)."""
        if self._pg:
            self._migrate_org_unique_rescope_pg()
        else:
            self._migrate_org_unique_rescope_sqlite()

    # ---- org unique rescope: 공통 헬퍼 ----

    def _org_name_index_current(self, index_name: str) -> bool:
        """idx_*_name_lower 인덱스가 이미 (org_id, lower(name)) 형태인지."""
        try:
            if self._pg:
                row = self._conn.execute(
                    "SELECT indexdef FROM pg_indexes WHERE indexname=?"
                    " AND schemaname=current_schema()",
                    (index_name,),
                ).fetchone()
                return row is not None and "org_id" in str(row["indexdef"])
            row = self._conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
                (index_name,),
            ).fetchone()
            return row is not None and bool(row["sql"]) and "org_id" in str(row["sql"])
        except Exception as e:
            logger.warning("%s index def check failed: %s", index_name, e)
            return False

    def _ensure_org_name_index(self, table: str) -> None:
        """UNIQUE (org_id, lower(name)) 인덱스 — 구 (lower(name)) 인덱스가
        남아 있으면 DROP 후 재생성한다(매 기동 멱등)."""
        index_name = f"idx_{table}_name_lower"
        if self._org_name_index_current(index_name):
            return
        with self.tx() as c:
            c.execute(f"DROP INDEX IF EXISTS {index_name}")
            c.execute(f"CREATE UNIQUE INDEX {index_name} ON {table}(org_id, lower(name))")
        logger.info("%s rescope: %s recreated with org_id", table, index_name)

    def _ensure_org_scoped_index(self, index_name: str, create_sql: str) -> None:
        """UNIQUE 인덱스를 org_id 포함 형태로 교체 — 정의에 org_id가 없으면
        DROP 후 재생성한다(매 기동 멱등). create_sql은 완성된 CREATE 문.

        _ensure_org_name_index와 달리 컬럼 구성이 고정이 아닌 부분
        UNIQUE 인덱스(dedupe/budget/recovery)용 — org_id 판정은
        _org_name_index_current와 동일하게 정의 문자열의 'org_id' 포함
        여부로 한다."""
        if self._org_name_index_current(index_name):
            return
        with self.tx() as c:
            c.execute(f"DROP INDEX IF EXISTS {index_name}")
            c.execute(create_sql)
        logger.info("org rescope: %s recreated with org_id", index_name)

    # ---- org unique rescope: SQLite ----

    def _sqlite_needs_org_rescope(self, table: str, spec: dict[str, Any]) -> bool:
        """구 UNIQUE 컬럼 집합이 아직 남아 있으면 True — 재실행 시 False."""
        exists = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if exists is None:
            return False
        uniques = {frozenset(c) for c in self._sqlite_table_unique_cols(table)}
        return any(frozenset(o) in uniques for o in spec["old_unique"])

    def _sqlite_rebuild_org_table(self, table: str, spec: dict[str, Any]) -> None:
        """CREATE new → INSERT SELECT(공통 컬럼) → DROP old → RENAME.

        구 테이블에 없는 컬럼은 신규 DDL의 DEFAULT가 적용되고, 스펙에
        없는 구 컬럼은 경고 후 버린다(발생 시 스펙 갱신 필요).
        agents.reports_to 자기참조 FK 때문에 재작성 동안만
        foreign_keys를 끈다 — 삽입 순서와 무관하게 복사가 가능하다."""
        new_table = f"{table}__new"
        rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        old_cols = [str(r["name"]) for r in rows]
        old_set = set(old_cols)
        spec_cols = [str(c) for c in spec["cols"]]
        spec_set = set(spec_cols)
        copy_cols = [c for c in spec_cols if c in old_set]
        dropped = [c for c in old_cols if c not in spec_set]
        if dropped:
            logger.warning("%s org rescope: dropping unknown columns %s", table, dropped)
        if not copy_cols:
            raise RuntimeError(f"{table}: no overlapping columns to copy")
        collist = ",".join(copy_cols)
        fk_row = self._conn.execute("PRAGMA foreign_keys").fetchone()
        fk_on = bool(fk_row and fk_row[0])
        if fk_on:
            self._conn.execute("PRAGMA foreign_keys=OFF")
        try:
            with self.tx() as c:
                c.execute(f"DROP TABLE IF EXISTS {new_table}")
                c.execute(spec["ddl"].replace("{t}", new_table))
                c.execute(
                    f"INSERT INTO {new_table}({collist})"  # noqa: S608 - 내부 상수
                    f" SELECT {collist} FROM {table}"
                )
                c.execute(f"DROP TABLE {table}")
                c.execute(f"ALTER TABLE {new_table} RENAME TO {table}")
                for idx_sql in spec.get("indexes", []):
                    c.execute(idx_sql)
        finally:
            if fk_on:
                self._conn.execute("PRAGMA foreign_keys=ON")
        logger.info("%s org rescope: table rebuilt (%d cols copied)", table, len(copy_cols))

    def _migrate_org_unique_rescope_sqlite(self) -> None:
        for table, spec in self._ORG_UNIQUE_SQLITE_REBUILDS.items():
            try:
                if not self._sqlite_needs_org_rescope(table, spec):
                    continue
                self._sqlite_rebuild_org_table(table, spec)
            except Exception as e:
                logger.warning("org unique rescope %s failed: %s", table, e)
        for t in ("projects", "evals"):
            try:
                self._ensure_org_name_index(t)
            except Exception as e:
                logger.warning("org name index %s failed: %s", t, e)
        # inbox_items dedupe 부분 UNIQUE 인덱스 — 구 (dedupe_key) 전역
        # 인덱스가 남아 있으면 (dedupe_key, org_id)로 교체한다.
        # dedupe_key 자체가 org 네임스페이스를 포함하므로(orgctx 경유
        # 저장) 실질 중복 판정은 동일하고, org_id 컬럼은 명시적 조직
        # 범위 제약을 위한 것이다.
        try:
            self._ensure_org_scoped_index(
                "idx_inbox_items_dedupe",
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_inbox_items_dedupe"
                " ON inbox_items(dedupe_key, org_id) WHERE dedupe_key != ''",
            )
        except Exception as e:
            logger.warning("org dedupe index inbox_items failed: %s", e)

    # ---- org unique rescope: PostgreSQL ----

    def _pg_table_constraints(self, table: str) -> list[tuple[str, str, tuple[str, ...]]]:
        """(constraint_name, constraint_type, cols) — UNIQUE/PK 제약 목록."""
        rows = self._conn.execute(
            "SELECT tc.constraint_name, tc.constraint_type,"
            " array_agg(kcu.column_name ORDER BY kcu.ordinal_position) AS cols"
            " FROM information_schema.table_constraints tc"
            " JOIN information_schema.key_column_usage kcu"
            " ON kcu.constraint_name = tc.constraint_name"
            " AND kcu.table_schema = tc.table_schema"
            " AND kcu.table_name = tc.table_name"
            " WHERE tc.table_name=? AND tc.table_schema = current_schema()"
            " AND tc.constraint_type IN ('UNIQUE','PRIMARY KEY')"
            " GROUP BY tc.constraint_name, tc.constraint_type",
            (table,),
        ).fetchall()
        return [
            (
                str(r["constraint_name"]),
                str(r["constraint_type"]),
                tuple(str(c) for c in (r["cols"] or [])),
            )
            for r in rows
        ]

    def _pg_rescope_one(
        self,
        table: str,
        ctype: str,
        old_cols: tuple[str, ...],
        new_cols: tuple[str, ...] | None,
        add_sql: str | None,
        cname: str | None,
    ) -> None:
        """구 컬럼 집합의 제약을 drop하고 새 집합 제약을 add한다.

        신구 제약은 상호 배타적이지 않을 수 있어(드문 부분 마이그레이션)
        drop/add를 독립적으로 판정한다 — 재실행 시 이미 새 집합이 있으면
        add를 건너뛰고, 구 집합이 없으면 drop을 건너뛴다."""
        old_set = set(old_cols)
        new_set = set(new_cols) if new_cols else set()
        with self.tx() as c:
            for con_name, con_type, cols in self._pg_table_constraints(table):
                if con_type == ctype and set(cols) == old_set:
                    c.execute(f'ALTER TABLE {table} DROP CONSTRAINT "{con_name}"')
            if new_cols is None or add_sql is None or cname is None:
                return
            have_new = any(
                ct == ctype and set(cl) == new_set
                for _, ct, cl in self._pg_table_constraints(table)
            )
            if not have_new:
                c.execute(f"ALTER TABLE {table} ADD CONSTRAINT {cname} {add_sql}")
        logger.info("%s org rescope: %s %s -> %s", table, ctype, old_cols, new_cols)

    def _migrate_org_unique_rescope_pg(self) -> None:
        for spec in self._PG_ORG_UNIQUE_RESPECS:
            table, ctype, old_cols, new_cols, add_sql, cname = spec
            try:
                self._pg_rescope_one(table, ctype, old_cols, new_cols, add_sql, cname)
            except Exception as e:
                logger.warning("org unique rescope %s failed: %s", table, e)
        for t in ("projects", "evals"):
            try:
                self._ensure_org_name_index(t)
            except Exception as e:
                logger.warning("org name index %s failed: %s", t, e)
        try:
            self._ensure_org_scoped_index(
                "idx_inbox_items_dedupe",
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_inbox_items_dedupe"
                " ON inbox_items(dedupe_key, org_id) WHERE dedupe_key != ''",
            )
        except Exception as e:
            logger.warning("org dedupe index inbox_items failed: %s", e)

    def _migrate_run_columns(self) -> None:
        """기존 DB의 runs에 created_by/project/agent_id 컬럼 보강."""
        self._ensure_columns(
            "runs",
            {
                "created_by": "TEXT NOT NULL DEFAULT ''",
                "project": "TEXT NOT NULL DEFAULT ''",
                "agent_id": "TEXT",
            },
        )

    def _migrate_budget_incident_index(self) -> None:
        """open 인시던트 (policy, window, threshold) 부분 UNIQUE 인덱스.

        SCHEMA에 넣지 않고 여기서 생성하는 이유: 기존 DB에 중복 open
        인시던트가 있으면 executescript 전체가 실패해 기동이 멈춘다.
        여기서는 중복을 먼저 정리하고, 그래도 실패하면 경고만 남긴다 —
        has_open_budget_incident 사전 검사가 논리적 중복을 계속 막는다.
        spec 003 US3/FR-018 — 인덱스는 (policy_id, window_start,
        threshold_type, org_id) 부분 UNIQUE: 같은 정책/윈도우/임계라도
        조직이 다르면 open 인시던트가 공존할 수 있다. 중복 정리 키에도
        org_id를 포함한다."""
        try:
            rows = self._conn.execute(
                "SELECT id, policy_id, window_start, threshold_type, org_id"
                " FROM budget_incidents WHERE status='open'"
                " ORDER BY created_at, id"
            ).fetchall()
            seen: set[tuple[Any, Any, Any, Any]] = set()
            for r in rows:
                key = (
                    str(r["org_id"] or ""),
                    r["policy_id"],
                    r["window_start"],
                    r["threshold_type"],
                )
                if key in seen:
                    self.resolve_budget_incident(
                        str(r["id"]), "resolved", note="dedupe before unique index"
                    )
                else:
                    seen.add(key)
            # 구 (policy_id, window_start, threshold_type) 인덱스가 남아
            # 있으면 org_id 포함 형태로 교체한다(멱등).
            self._ensure_org_scoped_index(
                "idx_budget_incidents_open",
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_budget_incidents_open"
                " ON budget_incidents(policy_id, window_start, threshold_type, org_id)"
                " WHERE status='open'",
            )
        except Exception as e:
            logger.warning("budget_incidents open-unique index skipped: %s", e)

    def _migrate_recovery_actions_index(self) -> None:
        """(source_type, source_id)당 active|escalated 1건 부분 UNIQUE 인덱스.

        SCHEMA에 넣지 않는 이유는 _migrate_budget_incident_index와 동일 —
        기존 DB에 중복 open 행이 있으면 executescript 전체가 실패해
        기동이 멈춘다. 여기서는 최신 행만 active|escalated로 남기고
        나머지는 cancelled로 정리한 뒤 인덱스를 만든다. 인덱스 생성마저
        실패하면 경고만 남기고 진행한다 — create_recovery_action의
        사전 조회+integrity 재조회가 논리적 중복을 계속 막는다.
        spec 003 US3/FR-018 — 인덱스는 (source_type, source_id, org_id)
        부분 UNIQUE: 같은 source라도 조직이 다르면 open 복구 액션이
        공존할 수 있다. 중복 정리 키에도 org_id를 포함한다."""
        try:
            rows = self._conn.execute(
                "SELECT id, source_type, source_id, org_id FROM recovery_actions"
                " WHERE status IN ('active','escalated')"
                " ORDER BY created_at DESC, id DESC"
            ).fetchall()
            seen: set[tuple[Any, Any, Any]] = set()
            dup_ids: list[str] = []
            for r in rows:
                key = (str(r["org_id"] or ""), r["source_type"], r["source_id"])
                if key in seen:
                    dup_ids.append(str(r["id"]))
                else:
                    seen.add(key)
            if dup_ids:
                now = _iso(_now())
                with self.tx() as c:
                    for did in dup_ids:
                        c.execute(
                            "UPDATE recovery_actions SET status='cancelled',"
                            " resolution_note='dedupe before unique index',"
                            " updated_at=? WHERE id=? AND status IN ('active','escalated')",
                            (now, did),
                        )
                logger.info(
                    "recovery_actions dedupe: cancelled %d duplicate open rows", len(dup_ids)
                )
            self._ensure_org_scoped_index(
                "idx_recovery_actions_open",
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_recovery_actions_open"
                " ON recovery_actions(source_type, source_id, org_id)"
                " WHERE status IN ('active','escalated')",
            )
        except Exception as e:
            logger.warning("recovery_actions open-unique index skipped: %s", e)

    def _migrate_wakeup_idempotency_index(self) -> None:
        """idempotency_key 부분 UNIQUE 인덱스 — 동시 동일 키 요청의 이중 run 방지.

        SCHEMA에 넣지 않는 이유는 _migrate_budget_incident_index와 동일 —
        기존 DB에 중복 키가 있으면 executescript 전체가 실패해 기동이 멈춘다.
        여기서는 키를 비워 최신 행만 남기고(감사 행은 보존) 인덱스를 만든다.
        인덱스 생성마저 실패하면 경고만 남기고 진행한다 —
        admit_run의 replay 사전 검사와 IntegrityError 재조회가 계속 동작한다."""
        try:
            rows = self._conn.execute(
                "SELECT id, idempotency_key FROM wakeup_requests"
                " WHERE idempotency_key <> '' ORDER BY created_at DESC, id DESC"
            ).fetchall()
            seen: set[str] = set()
            dup_ids: list[str] = []
            for r in rows:
                key = str(r["idempotency_key"])
                if key in seen:
                    dup_ids.append(str(r["id"]))
                else:
                    seen.add(key)
            if dup_ids:
                with self.tx() as c:
                    for did in dup_ids:
                        c.execute(
                            "UPDATE wakeup_requests SET idempotency_key='' WHERE id=?", (did,)
                        )
                logger.info(
                    "wakeup_requests idempotency dedupe: cleared %d duplicate keys",
                    len(dup_ids),
                )
            with self.tx() as c:
                c.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_wakeup_idem_uniq"
                    " ON wakeup_requests(idempotency_key) WHERE idempotency_key <> ''"
                )
        except Exception as e:
            logger.warning("wakeup_requests idempotency unique index skipped: %s", e)

    def _migrate_agent_name_index(self) -> None:
        """agents.lower(name) UNIQUE 인덱스 — 앱 레벨 ci 중복 검사의 레이스 백스톱.

        SCHEMA에 넣지 않는 이유는 _migrate_budget_incident_index와 동일 —
        기존 DB에 대소문자만 다른 중복 이름이 있으면 executescript 전체가
        실패해 기동이 멈춘다. 여기서는 최신 행만 이름을 유지하고 나머지는
        id 접미를 붙여 보존한다(runs.agent_id/reports_to는 id 참조라 영향
        없음). 인덱스 생성마저 실패하면 경고만 남기고 진행한다 —
        find_agent_by_name 사전 검사와 IntegrityError→409 매핑이 계속 동작."""
        try:
            rows = self._conn.execute(
                "SELECT id, name, org_id, updated_at, created_at FROM agents"
                " ORDER BY updated_at DESC, created_at DESC, id DESC"
            ).fetchall()
            # spec 003 US3/FR-018 — 중복 판정도 조직 범위로 좁힌다.
            # 같은 이름이 조직이 다르면 허용된다.
            seen: set[tuple[str, str]] = set()
            renames: list[tuple[str, str]] = []
            for r in rows:
                key = (
                    str(r["org_id"] or ""),
                    str(r["name"] or "").strip().lower(),
                )
                if not key[1]:
                    continue
                if key in seen:
                    # 최신이 아닌 중복 — 이름에 id 접미를 붙여 유일성 확보
                    new_name = f"{r['name']}·dup·{r['id']}"[:100]
                    renames.append((new_name, str(r["id"])))
                else:
                    seen.add(key)
            if renames:
                with self.tx() as c:
                    for new_name, aid in renames:
                        c.execute(
                            "UPDATE agents SET name=?, updated_at=? WHERE id=?",
                            (new_name, _iso(_now()), aid),
                        )
                logger.info("agents name dedupe: renamed %d duplicate rows", len(renames))
            # 인덱스는 (org_id, lower(name)) — 구 (lower(name)) 전역
            # 인덱스가 남아 있으면 여기서 교체한다.
            self._ensure_org_name_index("agents")
        except Exception as e:
            logger.warning("agents name unique index skipped: %s", e)

    def _migrate_goal_columns(self) -> None:
        """기존 DB의 runs/projects에 goal_id 컬럼 보강 (Phase-3 goals)."""
        self._ensure_columns("runs", {"goal_id": "TEXT"})
        self._ensure_columns("projects", {"goal_id": "TEXT"})

    def _migrate_phase3_columns(self) -> None:
        """agent-management 3차 — goals/projects/tasks 컬럼 보강 + 상태 어휘 이행.

        CREATE TABLE IF NOT EXISTS는 기존 테이블을 변경하지 않으므로 신규
        컬럼은 _ensure_columns로 추가한다(두 dialect 공통 — _ensure_columns가
        information_schema/PRAGMA 분기를 처리). 새 테이블(task_comments,
        task_artifacts, routines, routine_runs, agent_permissions,
        external_*)은 SCHEMA/SCHEMA_PG의 executescript가 담당한다.

        상태 어휘 이행: spec 3차 A는 goals.status를 active|paused|achieved|
        cancelled로 정의한다. 기존 행의 done→achieved, archived→cancelled를
        1회 UPDATE로 변환한다 — WHERE 절이 변환 대상을 한정하므로 재실행해도
        멱등이다. 신규 행의 어휘 강제는 서비스 계층(goals.py)의 몫이다.

        projects.updated_at은 spec상 필수지만 기존 행에는 없으므로
        created_at으로 백필한다."""
        self._ensure_columns(
            "goals",
            {
                "owner_agent_id": "TEXT",
                "success_metrics": "TEXT NOT NULL DEFAULT '{}'",
                "target_at": "TEXT",
            },
        )
        self._ensure_columns(
            "projects",
            {
                "status": "TEXT NOT NULL DEFAULT 'active'",
                "owner_agent_id": "TEXT",
                "repository_url": "TEXT",
                "repository_ref": "TEXT",
                "workspace_path": "TEXT",
                "updated_at": "TEXT",
            },
        )
        self._ensure_columns(
            "tasks",
            {
                "project_id": "TEXT",
                "goal_id": "TEXT",
            },
        )
        # 신규 컬럼 위의 인덱스는 SCHEMA에 넣지 않는다 —
        # _migrate_agent_management_columns의 kind 인덱스와 동일한 이유로
        # 기존 DB는 executescript 시점에 컬럼이 없어 전체가 실패한다.
        # 컬럼 보강 후 여기서 만든다(실패해도 경고만).
        try:
            with self.tx() as c:
                c.execute("CREATE INDEX IF NOT EXISTS idx_goals_owner ON goals(owner_agent_id)")
                c.execute("CREATE INDEX IF NOT EXISTS idx_projects_goal ON projects(goal_id)")
                c.execute("CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id)")
                c.execute("CREATE INDEX IF NOT EXISTS idx_tasks_goal ON tasks(goal_id)")
        except Exception as e:
            logger.warning("phase3 column indexes skipped: %s", e)
        try:
            with self.tx() as c:
                c.execute(
                    "UPDATE goals SET status='achieved', updated_at=? WHERE status='done'",
                    (_iso(_now()),),
                )
                c.execute(
                    "UPDATE goals SET status='cancelled', updated_at=? WHERE status='archived'",
                    (_iso(_now()),),
                )
                c.execute(
                    "UPDATE projects SET updated_at=created_at"
                    " WHERE updated_at IS NULL OR updated_at=''"
                )
        except Exception as e:
            logger.warning("phase3 status/updated_at migration failed: %s", e)

    # external_* 테이블의 신구 스키마 판별·재작성용 스펙 —
    # (provider, company_id, external_id) UNIQUE가 없으면 구 스키마.
    # 값: (company_id 이외 컬럼 목록, company_id 백필 SQL 식).
    _EXTERNAL_COMPANY_SPECS = {
        "external_agents": (
            ",payload_summary,status",
            "COALESCE(t.company_id,'')",
        ),
        "external_tasks": (
            ",assignee_external_id,status,title",
            "COALESCE((SELECT a.company_id FROM external_agents a"
            " WHERE a.provider=t.provider AND a.external_id=t.assignee_external_id),'')",
        ),
        "external_runs": (
            ",task_external_id,agent_external_id,status,token_cost_summary",
            "COALESCE((SELECT a.company_id FROM external_agents a"
            " WHERE a.provider=t.provider AND a.external_id=t.agent_external_id),'')",
        ),
    }
    _EXTERNAL_COMPANY_TABLES = tuple(_EXTERNAL_COMPANY_SPECS)

    def _sqlite_table_unique_cols(self, table: str) -> list[list[str]]:
        """SQLite 테이블의 UNIQUE 인덱스 컬럼 목록 — index_list/index_info."""
        out: list[list[str]] = []
        try:
            for ix in self._conn.execute(f"PRAGMA index_list({table})").fetchall():
                if not ix["unique"]:
                    continue
                cols = [
                    r["name"]
                    for r in self._conn.execute(f"PRAGMA index_info({ix['name']})").fetchall()
                ]
                out.append(cols)
        except Exception as e:
            logger.warning("%s index_list failed: %s", table, e)
        return out

    def _external_needs_company_migration(self, table: str) -> bool:
        """(provider,company_id,external_id) UNIQUE가 없으면 True — 구 스키마."""
        want = {"provider", "company_id", "external_id"}
        if self._pg:
            try:
                # 제약별 컬럼 집합 — (provider,company_id,external_id)를
                # 정확히 커버하는 UNIQUE 제약이 있으면 최신 스키마다.
                rows = self._conn.execute(
                    "SELECT tc.constraint_name,"
                    " array_agg(kcu.column_name ORDER BY kcu.ordinal_position) AS cols"
                    " FROM information_schema.table_constraints tc"
                    " JOIN information_schema.key_column_usage kcu"
                    " ON kcu.constraint_name = tc.constraint_name"
                    " AND kcu.table_schema = tc.table_schema"
                    " AND kcu.table_name = tc.table_name"
                    " WHERE tc.table_name=? AND tc.constraint_type='UNIQUE'"
                    " AND tc.table_schema = current_schema()"
                    " GROUP BY tc.constraint_name",
                    (table,),
                ).fetchall()
            except Exception as e:
                logger.warning("%s pg constraint check failed: %s", table, e)
                return False
            return not any(want == set(r["cols"] or []) for r in rows)
        try:
            cols = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
        except Exception as e:
            logger.warning("%s table_info failed: %s", table, e)
            return False
        if "company_id" not in cols:
            return True
        return not any(set(c) == want for c in self._sqlite_table_unique_cols(table))

    def _migrate_external_company_columns(self) -> None:
        """external_tasks/external_runs에 company_id 추가 + UNIQUE를
        (provider, company_id, external_id)로 변경 (Codex M5).

        구 스키마는 UNIQUE(provider, external_id)라 회사 간 external_id가
        충돌한다. SQLite는 UNIQUE 제약을 ALTER로 못 바꾸므로 테 일본을
        재작성하고, PG는 제약 drop/add로 처리한다. company_id 백필은
        assignee/agent external_id → external_agents.company_id 조인
        (best-effort, 매칭 없으면 '')."""
        self._external_company_schema_broken = False
        for table in self._EXTERNAL_COMPANY_TABLES:
            try:
                if not self._external_needs_company_migration(table):
                    continue
            except Exception as e:
                # 판별 실패 시 마이그레이션이 스킵돼 ingest의 3컬럼
                # ON CONFLICT가 runtime-error로 깨진다 — 조용히 넘기지 않는다.
                self._external_company_schema_broken = True
                logger.critical(
                    "%s company migration check failed — external_* 쓰기가 "
                    "깨진 상태로 계속됩니다: %s",
                    table,
                    e,
                )
                continue
            if self._pg:
                self._migrate_external_company_pg(table)
            else:
                self._migrate_external_company_sqlite(table)

    def _ensure_external_company_indexes(self) -> None:
        """external_tasks/runs의 company_id 인덱스 — SCHEMA 문자열이 아니라
        여기서 만든다. 구 스키마 DB는 company_id 컬럼이 없어 executescript
        시점에 인덱스 생성이 실패하므로, 마이그레이션 이후에 생성해야 한다."""
        for table in ("external_tasks", "external_runs"):
            try:
                with self.tx() as c:
                    c.execute(
                        f"CREATE INDEX IF NOT EXISTS idx_{table}_company"
                        f" ON {table}(provider, company_id)"
                    )
            except Exception as e:
                logger.warning("%s company index failed: %s", table, e)

    def _migrate_external_company_pg(self, table: str) -> None:
        """PostgreSQL — 컬럼 추가 + 구 유니크 제약 교체."""
        company_expr = self._EXTERNAL_COMPANY_SPECS[table][1]
        try:
            with self.tx() as c:
                c.execute(
                    f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS company_id"
                    " TEXT NOT NULL DEFAULT ''"
                )
                if table != "external_agents":
                    # company_id 백필 — agent 조인으로 유도(agents는 이미 보유).
                    c.execute(
                        f"UPDATE {table} t SET company_id = ({company_expr})"  # noqa: S608 - table/expr는 내부 상수
                        " WHERE t.company_id='' OR t.company_id IS NULL"
                    )
                # 구 UNIQUE(provider, external_id) 제약 제거 — information_schema에서
                # 컬럼 집합이 정확히 {provider, external_id}인 UNIQUE 제약을 찾아
                # 이름으로 drop한다(자동 생성명이 아니어도 동작).
                legacy = c.execute(
                    "SELECT tc.constraint_name,"
                    " array_agg(kcu.column_name) AS cols"
                    " FROM information_schema.table_constraints tc"
                    " JOIN information_schema.key_column_usage kcu"
                    " ON kcu.constraint_name = tc.constraint_name"
                    " AND kcu.table_schema = tc.table_schema"
                    " AND kcu.table_name = tc.table_name"
                    " WHERE tc.table_name=? AND tc.constraint_type='UNIQUE'"
                    " AND tc.table_schema = current_schema()"
                    " GROUP BY tc.constraint_name",
                    (table,),
                ).fetchall()
                for r in legacy:
                    if {"provider", "external_id"} == set(r["cols"] or []):
                        c.execute(f'ALTER TABLE {table} DROP CONSTRAINT "{r["constraint_name"]}"')
                c.execute(
                    f"ALTER TABLE {table} ADD CONSTRAINT"
                    f" {table}_provider_company_external_key"
                    " UNIQUE(provider, company_id, external_id)"
                )
                c.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_{table}_company"
                    f" ON {table}(provider, company_id)"
                )
            logger.info("%s company_id migration applied (pg)", table)
        except Exception as e:
            self._external_company_schema_broken = True
            logger.critical(
                "%s company_id migration failed — external_* 쓰기가 깨진 "
                "상태로 계속됩니다(재기동 시 재시도): %s",
                table,
                e,
            )

    def _migrate_external_company_sqlite(self, table: str) -> None:
        """SQLite — 테이블 재작성으로 UNIQUE(provider,company_id,external_id)
        + company_id 컬럼을 얻는다(제약 ALTER 불가)."""
        extra_cols, company_expr = self._EXTERNAL_COMPANY_SPECS[table]
        new_table = f"{table}__new"
        try:
            with self.tx() as c:
                c.execute("DROP TABLE IF EXISTS " + new_table)
                c.execute(
                    f"CREATE TABLE {new_table} ("
                    "id TEXT PRIMARY KEY, provider TEXT NOT NULL,"
                    " external_id TEXT NOT NULL,"
                    " company_id TEXT NOT NULL DEFAULT '',"
                    + extra_cols
                    + ", created_at TEXT NOT NULL, updated_at TEXT NOT NULL,"
                    " UNIQUE(provider, company_id, external_id))"
                )
                c.execute(
                    f"INSERT INTO {new_table}"  # noqa: S608 - table/expr는 내부 상수
                    "(id,provider,external_id,company_id" + extra_cols + ",created_at,updated_at)"
                    f" SELECT t.id,t.provider,t.external_id, {company_expr},"
                    + ",".join(f"t.{col.strip()}" for col in extra_cols.split(",") if col.strip())
                    + ",t.created_at,t.updated_at FROM "
                    + table
                    + " t"
                )
                c.execute(f"DROP TABLE {table}")
                c.execute(f"ALTER TABLE {new_table} RENAME TO {table}")
                # 보조 인덱스 복원 — SCHEMA 정의와 동일하게 재생성한다.
                for idx in {
                    "external_agents": [
                        "CREATE INDEX IF NOT EXISTS idx_external_agents_company"
                        " ON external_agents(provider, company_id)"
                    ],
                    "external_tasks": [
                        "CREATE INDEX IF NOT EXISTS idx_external_tasks_assignee"
                        " ON external_tasks(provider, assignee_external_id)",
                        "CREATE INDEX IF NOT EXISTS idx_external_tasks_company"
                        " ON external_tasks(provider, company_id)",
                    ],
                    "external_runs": [
                        "CREATE INDEX IF NOT EXISTS idx_external_runs_task"
                        " ON external_runs(provider, task_external_id)",
                        "CREATE INDEX IF NOT EXISTS idx_external_runs_company"
                        " ON external_runs(provider, company_id)",
                    ],
                }[table]:
                    c.execute(idx)
            logger.info("%s company_id migration applied (sqlite)", table)
        except Exception as e:
            self._external_company_schema_broken = True
            logger.critical(
                "%s company_id migration failed — external_* 쓰기가 깨진 "
                "상태로 계속됩니다(재기동 시 재시도): %s",
                table,
                e,
            )

    def _migrate_deferred_wakeup_scopes(self) -> None:
        """goal_id 도입으로 바뀐 scope_key 형식에 맞춰 deferred wakeup 재계산.

        scope_key_for의 digest가 (agent_id, goal_id, input)을 포함하도록
        바뀌면서, goal_id 미포함 구형식으로 저장된 status='deferred' 행은
        on_run_finished의 scope 조회와 영원히 매칭되지 않아 고착된다.
        payload(input/agent_id/goal_id)에서 scope_key를 재계산해 갱신하고,
        payload에 goal_id가 없으면 프로젝트 폴백 규칙(resolve_run_goal_id와
        동일: input.goal_id → project→projects.goal_id, archived 제외)으로
        보정해 payload에도 기록한다 — promote 경로의 scope 계산과 일치시키기
        위함. payload를 해석할 수 없는 행은 재시도해도 복구되지 않으므로
        failed로 마감한다. 행 단위 best-effort — 실패는 경고만 남긴다
        (_migrate_* 패턴과 동일)."""
        try:
            from ..wakeups import scope_key_for  # 지연 import — 순환 방지
        except Exception as e:
            logger.warning("deferred wakeup scope migration skipped: %s", e)
            return
        try:
            rows = self._conn.execute(
                "SELECT id, scope_key, payload FROM wakeup_requests WHERE status='deferred'"
            ).fetchall()
        except Exception as e:
            logger.warning("deferred wakeup scope scan failed: %s", e)
            return
        fixed = failed = 0
        for r in rows:
            wid = str(r["id"])
            try:
                payload = json.loads(str(r["payload"] or ""))
            except (ValueError, TypeError):
                payload = None
            inp = payload.get("input") if isinstance(payload, dict) else None
            if not isinstance(payload, dict) or not isinstance(inp, dict):
                # promote 경로도 같은 이유로 failed 마감한다 — 여기서 미리
                # 마감해 고착 deferred를 남기지 않는다.
                try:
                    self.fail_wakeup(wid)
                    failed += 1
                except Exception as e:
                    logger.warning("deferred wakeup %s fail-mark failed: %s", wid, e)
                continue
            try:
                # scope_key = "{workflow_key}:{digest}" — digest는 마지막
                # ':' 뒤 16hex. workflow_key는 ':'를 포함하지 않는다.
                wf = str(r["scope_key"] or "").rsplit(":", 1)[0]
                agent_id = str(payload.get("agent_id") or "")
                gid = str(payload.get("goal_id") or "").strip()[:64]
                payload_changed = False
                if not gid:
                    # admit_run과 동일한 폴백 해석 — project 태그의 goal.
                    gid = self.resolve_run_goal_id(dict(inp), "")
                    if gid and not self._goal_usable(gid):
                        gid = ""
                    if gid:
                        payload["goal_id"] = gid
                        payload_changed = True
                new_scope = scope_key_for(wf, inp, agent_id, gid)
                if new_scope == str(r["scope_key"] or "") and not payload_changed:
                    continue
                new_payload = json.dumps(payload, ensure_ascii=False, default=str)
                with self.tx() as c:
                    c.execute(
                        "UPDATE wakeup_requests SET scope_key=?, payload=?"
                        " WHERE id=? AND status='deferred'",
                        (new_scope, new_payload, wid),
                    )
                fixed += 1
            except Exception as e:
                failed += 1
                logger.warning("deferred wakeup %s scope recompute failed: %s", wid, e)
        if fixed or failed:
            logger.info(
                "deferred wakeup scope migration: fixed=%d failed=%d total=%d",
                fixed,
                failed,
                len(rows),
            )

    def _migrate_agent_management_columns(self) -> None:
        """agent-management 1차 — 기존 agents 테이블에 신규 컬럼 보강.

        kind는 실행 모델(workflow|external), adapter_config는 provider별
        실행 설정 JSON, last_seen_at은 머신 heartbeat 시각,
        monthly_budget_microusd는 2차 비용 원장이 쓸 저장 전용 예산.
        status 어휘는 건드리지 않는다 — 'archived'는 terminated의
        레거시 별칭으로 계속 유효(models.management 참조)."""
        # PG는 fresh schema(SCHEMA_PG)와 타입을 맞춰 BIGINT — INTEGER로
        # 추가하면 마이그레이션 DB와 신규 DB의 컬럼 타입이 어긋난다.
        # SQLite는 INTEGER affinity라 BIGINT도 동일하게 동작한다.
        self._ensure_columns(
            "agents",
            {
                "title": "TEXT",
                "kind": "TEXT NOT NULL DEFAULT 'workflow'",
                "adapter_config": "TEXT NOT NULL DEFAULT '{}'",
                "last_seen_at": "TEXT",
                "monthly_budget_microusd": (
                    "BIGINT NOT NULL DEFAULT 0" if self._pg else "INTEGER NOT NULL DEFAULT 0"
                ),
            },
        )
        # kind 인덱스는 컬럼 보강 후에만 생성 가능 — SCHEMA의 executescript는
        # 기존 DB에서 컬럼 부재로 실패하므로 여기서 만든다(실패해도 경고만).
        try:
            with self.tx() as c:
                c.execute("CREATE INDEX IF NOT EXISTS idx_agents_kind ON agents(kind)")
        except Exception as e:
            logger.warning("agents kind index skipped: %s", e)

    def _migrate_cost_ledger_columns(self) -> None:
        """agent-management 2차 — budget_policies/budget_incidents의
        microusd·unpriced 컬럼 보강 + amount_usd → amount_microusd 백필.

        CREATE TABLE IF NOT EXISTS는 기존 테이블을 변경하지 않으므로
        컬럼은 _ensure_columns로 추가한다. 기존 *_usd REAL 컬럼은 호환용으로
        유지하고, microusd 컬럼이 신규 권위 경로다(paperclip spec 2차 B).
        amount_microusd 백필은 ROUND(amount_usd * 1e6) — float 읽기를
        정수로 변환하는 1회성 마이그레이션이며 런타임 금액 계산에는
        float를 쓰지 않는다."""
        big = "BIGINT" if self._pg else "INTEGER"
        self._ensure_columns(
            "budget_policies",
            {
                "amount_microusd": f"{big} NOT NULL DEFAULT 0",
                "reservation_microusd": f"{big} NOT NULL DEFAULT 0",
                "unpriced_usage_policy": "TEXT NOT NULL DEFAULT 'allow'",
            },
        )
        self._ensure_columns(
            "budget_incidents",
            {
                "observed_spend_microusd": big,
                "unpriced_event_count": "INTEGER NOT NULL DEFAULT 0",
                "reason_code": "TEXT NOT NULL DEFAULT ''",
            },
        )
        # amount_usd > 0 이고 amount_microusd가 아직 0인 행만 백필 —
        # 명시적으로 0을 쓴 정책(비활성 의미)과 이미 마이그레이션된 행은
        # 건드리지 않는다. 마이그레이션은 기동 시 1회 실행된다.
        try:
            cast = "BIGINT" if self._pg else "INTEGER"
            with self.tx() as c:
                cur = c.execute(
                    "UPDATE budget_policies SET amount_microusd ="  # noqa: S608 - cast는 'BIGINT'/'INTEGER' 고정 리터럴
                    f" CAST(ROUND(amount_usd * 1000000) AS {cast})"
                    " WHERE amount_microusd = 0 AND amount_usd > 0"
                )
            if getattr(cur, "rowcount", 0):
                logger.info("budget_policies microusd backfill: %d rows", cur.rowcount)
        except Exception as e:
            logger.warning("budget_policies microusd backfill failed: %s", e)

    def _migrate_workflow_version_columns(self) -> None:
        """custom_workflows에 latest_revision/updated_at 보강 + 기존 spec 백필.

        CREATE TABLE IF NOT EXISTS는 기존 테이블을 변경하지 않으므로 컬럼은
        _ensure_columns로 추가하고, 리비전 이력이 없는 기존 행은 현재 spec을
        revision 1(note='backfill')로 복원한다 — 이후 모든 저장이
        latest+1 append-only 리비전으로 이어진다."""
        self._ensure_columns(
            "custom_workflows",
            {
                "latest_revision": "INTEGER NOT NULL DEFAULT 0",
                "updated_at": "TEXT",
            },
        )
        self._backfill_workflow_revisions()

    def _backfill_workflow_revisions(self) -> None:
        """workflow_revisions가 비어 있는 custom_workflows 행에 revision 1 생성.

        관용적 마이그레이션 — 실패해도 기동을 막지 않고 경고만 남긴다
        (_migrate_* 패턴과 동일). latest_revision도 함께 동기화한다."""
        try:
            rows = self._conn.execute(
                "SELECT name, spec, created_at, org_id FROM custom_workflows"
            ).fetchall()
        except Exception as e:
            logger.warning("workflow_revisions backfill scan failed: %s", e)
            return
        for r in rows:
            # 행 단위 best-effort — 깨진 spec/이름 하나가 나머지 백필을
            # 중단시키지 않게 한다.
            try:
                self._backfill_workflow_revision_row(r)
            except Exception as e:
                logger.warning(
                    "workflow_revisions backfill row %s failed: %s",
                    r["name"] if hasattr(r, "keys") else "?",
                    e,
                )

    def _backfill_workflow_revision_row(self, r: Any) -> None:
        name = str(r["name"])
        # spec 003 US3/FR-018 — workflow_revisions UNIQUE가 (org_id,name,
        # revision)로 재스코프됐으므로 조회·갱신도 조직 범위로 제한한다.
        oid = str(r["org_id"] or "")
        rev_row = self._conn.execute(
            "SELECT COALESCE(MAX(revision),0) AS m FROM workflow_revisions"
            " WHERE org_id=? AND name=?",
            (oid, name),
        ).fetchone()
        max_rev = int(rev_row["m"] or 0) if rev_row is not None else 0
        if max_rev == 0:
            now = _iso(_now())
            with self.tx() as c:
                c.execute(
                    "INSERT INTO workflow_revisions"
                    "(id,name,revision,spec,note,created_by,created_at,org_id)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (
                        uuid.uuid4().hex[:12],
                        name,
                        1,
                        str(r["spec"]),
                        "backfill",
                        "",
                        now,
                        oid,
                    ),
                )
                c.execute(
                    "UPDATE custom_workflows SET latest_revision=1,"
                    " updated_at=COALESCE(updated_at, created_at)"
                    " WHERE org_id=? AND name=?",
                    (oid, name),
                )
            logger.info("workflow_revisions backfill: %s -> revision 1", name)
        else:
            # 리비전은 있으나 latest_revision 포인터가 뒤처진 경우 동기화
            with self.tx() as c:
                c.execute(
                    "UPDATE custom_workflows SET latest_revision=?"
                    " WHERE org_id=? AND name=? AND (latest_revision IS NULL"
                    " OR latest_revision < ?)",
                    (max_rev, oid, name, max_rev),
                )
