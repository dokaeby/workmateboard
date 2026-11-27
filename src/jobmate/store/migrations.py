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
        has_open_budget_incident 사전 검사가 논리적 중복을 계속 막는다."""
        try:
            rows = self._conn.execute(
                "SELECT id, policy_id, window_start, threshold_type"
                " FROM budget_incidents WHERE status='open'"
                " ORDER BY created_at, id"
            ).fetchall()
            seen: set[tuple[Any, Any, Any]] = set()
            for r in rows:
                key = (r["policy_id"], r["window_start"], r["threshold_type"])
                if key in seen:
                    self.resolve_budget_incident(
                        str(r["id"]), "resolved", note="dedupe before unique index"
                    )
                else:
                    seen.add(key)
            with self.tx() as c:
                c.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_budget_incidents_open"
                    " ON budget_incidents(policy_id, window_start, threshold_type)"
                    " WHERE status='open'"
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
        사전 조회+integrity 재조회가 논리적 중복을 계속 막는다."""
        try:
            rows = self._conn.execute(
                "SELECT id, source_type, source_id FROM recovery_actions"
                " WHERE status IN ('active','escalated')"
                " ORDER BY created_at DESC, id DESC"
            ).fetchall()
            seen: set[tuple[Any, Any]] = set()
            dup_ids: list[str] = []
            for r in rows:
                key = (r["source_type"], r["source_id"])
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
            with self.tx() as c:
                c.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_recovery_actions_open"
                    " ON recovery_actions(source_type, source_id)"
                    " WHERE status IN ('active','escalated')"
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
                "SELECT id, name, updated_at, created_at FROM agents"
                " ORDER BY updated_at DESC, created_at DESC, id DESC"
            ).fetchall()
            seen: set[str] = set()
            renames: list[tuple[str, str]] = []
            for r in rows:
                key = str(r["name"] or "").strip().lower()
                if not key:
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
            with self.tx() as c:
                c.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_agents_name_lower ON agents(lower(name))"
                )
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
                "SELECT name, spec, created_at FROM custom_workflows"
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
        rev_row = self._conn.execute(
            "SELECT COALESCE(MAX(revision),0) AS m FROM workflow_revisions WHERE name=?",
            (name,),
        ).fetchone()
        max_rev = int(rev_row["m"] or 0) if rev_row is not None else 0
        if max_rev == 0:
            now = _iso(_now())
            with self.tx() as c:
                c.execute(
                    "INSERT INTO workflow_revisions"
                    "(id,name,revision,spec,note,created_by,created_at)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (
                        uuid.uuid4().hex[:12],
                        name,
                        1,
                        str(r["spec"]),
                        "backfill",
                        "",
                        now,
                    ),
                )
                c.execute(
                    "UPDATE custom_workflows SET latest_revision=1,"
                    " updated_at=COALESCE(updated_at, created_at) WHERE name=?",
                    (name,),
                )
            logger.info("workflow_revisions backfill: %s -> revision 1", name)
        else:
            # 리비전은 있으나 latest_revision 포인터가 뒤처진 경우 동기화
            with self.tx() as c:
                c.execute(
                    "UPDATE custom_workflows SET latest_revision=?"
                    " WHERE name=? AND (latest_revision IS NULL"
                    " OR latest_revision < ?)",
                    (max_rev, name, max_rev),
                )
