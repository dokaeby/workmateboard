# Feature Specification: jobMate 조직(Organization)·테넌시 — 상용 멀티테넌트 SaaS 기반

**Feature Branch**: `003-organization-tenancy`

**Created**: 2026-11-20

**Status**: Draft

**Input**: 현재 jobMate는 단일 테넌트 멀티유저 구조다 — `users` 테이블에 `role`(member|lead|admin)만 있고 조직 개념이 없다(`store/schema_sqlite.py:209`, `store/users.py:15-21`). 상용 SaaS로 전환하려면 조직 엔티티, 멤버 초대, 리소스 단위 인가, 조직 데이터 격리, 조직 설정/삭제 정책이 필요하다. 기존 capability/permission 시스템(`services/permissions.py`, `agent_permissions` 테이블)과 SQLite/PostgreSQL 공용 Store 마이그레이션 체계(`store/migrations.py`의 `_ensure_columns` 패턴)를 재사용해 최소 침습으로 도입한다.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - 조직 생성과 멤버십·역할 (Priority: P1)

조직 관리자가 조직을 생성하고(이름/슬러그), 본인은 "개인 워크스페이스"(가입 시 자동 생성되는 1인 조직)와 "조직 워크스페이스"(팀 단위)를 구분해 사용한다. 조직 역할은 `org_owner`/`org_admin`/`member` 3종이며, 기존 플랫폼 역할(`users.role` = member|lead|admin)과 분리된다 — 플랫폼 역할은 인스턴스 전역 관리자(env `JOBMATE_ADMIN_EMAILS`/`LEAD_EMAILS`, API 키)를 뜻하고, 조직 역할은 해당 조직 스코프의 권한을 뜻한다.

**Why this priority**: 모든 후속 기능(초대·격리·인가·설정)의 기반 엔티티다. 이것 없이는 멀티테넌트가 성립하지 않는다.

**Independent Test**: Store 단위로 조직 생성→멤버 추가→역할 판정을 검증하고, `POST /api/v1/orgs` + `GET /api/v1/orgs`로 HTTP 완결을 확인할 수 있다.

**Acceptance Scenarios**:

1. **Given** 로그인된 사용자, **When** `POST /api/v1/orgs {name, slug}` 호출, **Then** `organizations` 행 + 호출자를 `org_owner`로 하는 `organization_members` 행이 생성되고 201을 반환한다.
2. **Given** 신규 가입 사용자, **When** 회원가입 완료, **Then** `kind='personal'`인 개인 워크스페이스가 자동 생성되고 해당 사용자가 `org_owner` 멤버가 된다.
3. **Given** 멤버가 2개 조직에 속함, **When** `POST /api/v1/orgs/{id}/switch`, **Then** 세션의 현재 조직 컨텍스트가 바뀌고 이후 API 응답은 해당 조직 데이터만 반환한다.
4. **Given** 조직 비멤버가 `GET /api/v1/orgs/{org_id}/members` 호출, **When** 요청 처리, **Then** 403 또는 404(존재 비노출 원칙 적용 시 404)로 거부되고 감사 로그에 `permission_denied`가 남는다.

---

### User Story 2 - 멤버 이메일 초대 플로우 (Priority: P1)

`org_admin`/`org_owner`가 이메일과 조직 역할(`org_admin`|`member`)을 지정해 멤버를 초대한다. 초대는 토큰 링크로 발송되고, 수신자는 로그인 세션의 이메일이 초대 이메일과 일치할 때만 수락할 수 있다. 토큰은 단일 사용·만료·재발송(기존 토큰 무효화) 규칙을 따른다.

**Why this priority**: 상용 SaaS의 핵심 온보딩 경로이며, 초대 보안(토큰 해시 저장·단일 사용·이메일 일치)은 `email_verifications`의 검증된 패턴을 그대로 재사용할 수 있다.

**Independent Test**: fake 메일러로 초대 생성→토큰 수락/거절/만료/재발송을 HTTP로 재현한다.

**Acceptance Scenarios**:

1. **Given** org_admin 세션, **When** `POST /api/v1/orgs/{id}/invitations {email, role:'member'}`, **Then** `org_invitations` 행(token_hash만 저장, 원문 미저장)과 초대 메일이 생성되고 `org_invite_created` 감사 이벤트가 남는다.
2. **Given** 유효한 초대 토큰 링크, **When** 초대 이메일과 동일한 이메일로 로그인한 사용자가 `POST /api/v1/invitations/{token}/accept`, **Then** `organization_members` 행이 원자적으로 생성되고 토큰이 consume되어 재사용 시 410/400으로 거부된다.
3. **Given** 초대 이메일과 다른 계정으로 로그인, **When** accept 시도, **Then** 403 `invite_email_mismatch`로 거부되고 멤버십이 생기지 않는다.
4. **Given** 미수락 pending 초대가 있는 (org,email), **When** 재발송, **Then** 이전 토큰이 무효화되고 새 토큰만 유효하며, 10분 이내 재발송은 429로 차단된다.
5. **Given** 이미 다른 조직의 멤버인 이메일로 초대, **When** 수락, **Then** 기존 `users` 행을 재사용해 멤버십만 추가된다(계정 중복 생성 없음 — `users.email`은 전역 UNIQUE 유지).

---

### User Story 3 - 조직 데이터 격리 (Priority: P1)

모든 조직 스코프 리소스(projects/tasks/agents/goals/runs/custom_workflows/connectors/routines/evals/agent_permissions 등)가 `org_id`로 스코핑되고, 조회·목록·ID 직접 접근 모두 현재 세션 조직으로 필터된다. 기존 단일 테넌트 데이터는 "기본 조직"으로 백필되어 무중단 마이그레이션된다.

**Why this priority**: 격리 없이는 멀티테넌트가 데이터 유출로 직결된다. 상용 출시의 P0 차단 요소.

**Independent Test**: 조직 A/B 두 조직의 동일 리소스를 만들고 교차 조회가 0건임을 API·Store 양쪽에서 검증한다.

**Acceptance Scenarios**:

1. **Given** 기존 단일 테넌트 DB, **When** 마이그레이션 실행, **Then** 기본 조직 1개가 생성되고 기존 `users` 전원이 멤버가 되며, 모든 기존 org-scoped 행의 `org_id`가 기본 조직으로 백필된다.
2. **Given** 조직 A 소속 프로젝트, **When** 조직 B 세션으로 `GET /api/v1/management/tasks?project_id={A프로젝트}`, **Then** 결과에 A 조직 리소스가 0건이고 직접 ID 접근은 404다.
3. **Given** 스케줄러/자동화 잡(routines, workflow_schedules, cron)이 run을 생성, **When** 실행, **Then** 생성된 run의 `org_id`는 스케줄이 속한 조직이며 다른 조직 run 목록에 나타나지 않는다.
4. **Given** `POST /api/v1/runs` 등 신규 리소스 생성, **When** 세션 조직 컨텍스트로 생성, **Then** 해당 `org_id`가 자동 부여되고 요청 body로 다른 org_id를 지정해도 무시/거부된다.

---

### User Story 4 - 리소스 단위 인가 (프로젝트/워크플로/런 권한) (Priority: P2)

조직 내에서 멤버별 세분화 권한을 부여한다 — 프로젝트 단위 grant(사용자×프로젝트×권한)를 `agent_permissions`와 동일한 어휘·판정 규칙(deny 우선, scope 좁은 순, approval-required capability)으로 평가한다. 조직 역할은 기본 권한 집합을 제공하고, 개별 grant/deny가 이를 덮어쓴다.

**Why this priority**: 조직 역할만으로는 "읽기는 되는데 특정 프로젝트는 안 됨" 같은 상용 요구를 못 맞춘다. 기존 capability 시스템의 자연스러운 확장이다.

**Independent Test**: `services/permissions.py`의 `evaluate_permission`을 user principal로 확장해 단위 테스트하고, 프로젝트 grant API로 HTTP 검증한다.

**Acceptance Scenarios**:

1. **Given** member가 프로젝트 P에 `viewer` grant만 보유, **When** P의 task 생성 시도, **Then** `task:create` 판정이 deny되어 403 `permission_denied`가 반환된다.
2. **Given** 프로젝트 스코프 deny 규칙과 global allow 규칙 공존, **When** 해당 프로젝트 task 접근, **Then** deny가 우선하여 거부된다(기존 `_matching_rules`의 deny 우선 규칙과 동일).
3. **Given** `org_admin`, **When** 조직 내 모든 프로젝트 접근, **Then** 개별 grant 없이 조직 역할 기반 기본 권한으로 allow 판정된다.
4. **Given** approval-required capability(`external:send` 등)에 allow 규칙만 존재, **When** user principal로 호출, **Then** 기존과 동일하게 `approval_required` 409가 반환되고 승인 없이는 실행되지 않는다.

---

### User Story 5 - 조직 설정·멤버 관리·삭제/이관 정책 (Priority: P2)

`org_owner`/`org_admin`이 조직 이름/슬러그를 변경하고, 멤버 목록 조회·역할 변경·제거·소유권 이관을 수행한다. 조직 삭제는 소유자의 명시적 요청으로 soft-delete되며, 활성 run이 있으면 차단된다. 멤버는 스스로 조직을 탈퇴할 수 있다.

**Why this priority**: 운영 필수 기능이지만 US1~US3의 격리/초대가 먼저 동작해야 의미가 있다.

**Independent Test**: 멤버십 API + 마지막 owner 보호 + 삭제 차단 조건을 HTTP/Store 테스트로 검증한다.

**Acceptance Scenarios**:

1. **Given** org_owner 세션, **When** `PATCH /api/v1/orgs/{id} {name, slug}`, **Then** 조직이 갱신되고 slug 충돌 시 409 `slug_taken`이다.
2. **Given** 마지막 `org_owner` 한 명, **When** 탈퇴/제거/강등 시도, **Then** 409 `last_org_owner`로 차단되고 소유권 이관(`POST /api/v1/orgs/{id}/transfer`)을 먼저 요구한다.
3. **Given** 조직에 `queued|running|waiting_approval` 상태 run 존재, **When** `DELETE /api/v1/orgs/{id}`, **Then** 409 `org_has_active_runs`와 잔여 run 수가 반환된다.
4. **Given** 삭제 승인된 조직, **When** 삭제 처리, **Then** `organizations.status='deleted'`로 전이되고 멤버의 해당 조직 접근이 차단되며, 30일 유예 후 purge 잡이 org 스코프 행을 삭제한다(audit_events는 법적 보존을 위해 유지).

---

### Edge Cases

- **마지막 org_owner 이탈**: 탈퇴·제거·강등 모두 409 — `store.exclusive()` 임계구역 안에서 owner 수를 세고 변경한다(`auth.py`의 last_admin 보호, `ops.py` patch_user_role과 동일 패턴).
- **활성 run이 있는 조직 삭제**: 삭제를 거부하고 활성 run 수를 반환한다. 강제 삭제는 범위 외 — run을 먼저 취소/종결해야 한다.
- **한 사용자의 다중 조직 멤버십**: `users` 행은 1개(이메일 전역 UNIQUE 유지), `organization_members`가 N개. 세션의 `current_org_id`가 요청 컨텍스트를 결정하고, 멤버가 아닌 조직 ID 접근은 404.
- **초대받은 이메일이 이미 다른 조직/계정에 존재**: 계정 재사용 — `create_user`를 호출하지 않고 기존 user_id에 멤버십만 추가한다.
- **초대받은 이메일의 계정이 미가입**: accept는 세션 필요 — 프론트가 동일 이메일로 signup 유도 후 재시도. signup 시 개인 워크스페이스가 먼저 생기고 이후 조직 멤버십이 추가된다.
- **토큰 재사용/만료/타인 이메일**: 단일 사용 원자 consume(`UPDATE ... WHERE used_at IS NULL AND expires_at>?` rowcount 판정 — `consume_email_verification` 패턴)로 동시 수락 레이스 차단. 만료·사용·타인 이메일 모두 구체적 오류 코드로 구분하되 토큰 원문은 로그 금지.
- **org slug 충돌**: `lower(name)` 기준 UNIQUE — `idx_projects_name_lower`와 동일하게 대소문자 무시 인덱스.
- **기존 env 기반 역할과의 충돌**: `JOBMATE_ADMIN_EMAILS`/`LEAD_EMAILS`와 API 키는 플랫폼 슈퍼관리자로 모든 조직을 우회 조회 가능(기존 `_effective_role`/`_admin_user` 동작 유지). 단 감사 로그에는 항상 실제 actor를 남긴다.
- **org 삭제 후 동일 slug 재사용**: soft-delete 기간(30일) 동안 slug 예약 유지 — `UNIQUE` 제약이 deleted 행도 포함하므로 재생성 시 `slug`에 suffix 또는 상태별 unique partial index 필요(SQLite는 `WHERE status != 'deleted'` 부분 인덱스 가능, PG 동일 문법).
- **개인 워크스페이스 삭제**: 개인 조직은 멤버가 1명 — 탈퇴 대신 계정 삭제(`DELETE /api/v1/auth/account` → `delete_user_cascade`)가 처리하며, cascade에 `organization_members`/`org_invitations`(invited_by) 정리를 추가한다.
- **org 비활성(suspended)**: `organizations.status='suspended'` 시 멤버 API는 403 `org_suspended`, `org_owner`는 읽기 전용 허용. 삭제와 별도 상태로 둔다.
- **스케줄러 컨텍스트 부재**: cron/스케줄 잡은 세션이 없으므로 `org_id`를 스케줄 행에 저장하고 run 생성 시 그대로 전파한다 — "현재 조직" 개념이 없는 경로의 기본값은 행의 `org_id`다.

## Requirements *(mandatory)*

### Functional Requirements

**조직 엔티티·멤버십**

- **FR-001**: `organizations` 테이블을 `schema_sqlite.py`와 `schema_pg.py` 양쪽에 정의해야 한다(MUST). 컬럼: `id TEXT PRIMARY KEY`, `name TEXT NOT NULL`, `slug TEXT NOT NULL`, `kind TEXT NOT NULL DEFAULT 'team'`(`personal`|`team`), `status TEXT NOT NULL DEFAULT 'active'`(`active`|`suspended`|`deleted`), `created_by TEXT NOT NULL DEFAULT ''`, `created_at`, `updated_at`. `slug`는 `lower()` 기준 UNIQUE 인덱스여야 하며 형식은 소문자 영숫자·하이픈 3~48자다.
- **FR-002**: `organization_members` 테이블을 양쪽 스키마에 정의해야 한다(MUST). 컬럼: `org_id TEXT NOT NULL`, `user_id TEXT NOT NULL`, `role TEXT NOT NULL`(`org_owner`|`org_admin`|`member` — 앱 계층 검증), `status TEXT NOT NULL DEFAULT 'active'`, `joined_at TEXT NOT NULL`, `UNIQUE(org_id, user_id)`, `(user_id)`·`(org_id, role)` 인덱스.
- **FR-003**: 사용자 가입 완료 시 `kind='personal'`인 개인 워크스페이스를 자동 생성하고 해당 사용자를 `org_owner`로 등록해야 한다(MUST). 개인 조직명은 `display_name` 또는 이메일 local part 기반 기본값을 사용한다.
- **FR-004**: 조직 역할과 플랫폼 역할을 분리해야 한다(MUST). `users.role`(member|lead|admin)은 인스턴스 전역 의미를 유지하고, 조직 스코프 권한은 `organization_members.role`만으로 평가한다. `org_owner`/`org_admin`은 조직 내 관리자 게이트(`_lead_user` 상당)를 통과해야 한다.
- **FR-005**: 조직 API는 세션의 현재 조직 컨텍스트를 사용해야 한다(MUST). `auth_sessions`에 `current_org_id` 컬럼을 추가(`_ensure_columns` 마이그레이션)하고, `POST /api/v1/orgs/{id}/switch`가 이를 갱신한다. 멤버십이 없는 org_id로의 전환·접근은 404/403으로 거부한다.
- **FR-006**: 조직 엔드포인트를 제공해야 한다(MUST): `POST /api/v1/orgs`(생성, 임의 사용자 가능), `GET /api/v1/orgs`(내 멤버십 목록), `GET /api/v1/orgs/{id}`, `PATCH /api/v1/orgs/{id}`(이름/슬러그, org_admin+), `DELETE /api/v1/orgs/{id}`(org_owner, FR-028 정책), `POST /api/v1/orgs/{id}/switch`, `POST /api/v1/orgs/{id}/transfer`(소유권 이관).

**초대 플로우**

- **FR-007**: `org_invitations` 테이블을 양쪽 스키마에 정의해야 한다(MUST). 컬럼: `id TEXT PRIMARY KEY`, `org_id TEXT NOT NULL`, `email TEXT NOT NULL`, `role TEXT NOT NULL`(`org_admin`|`member` — `org_owner`는 초대로 부여 불가, 이관만 가능), `token_hash TEXT NOT NULL UNIQUE`, `status TEXT NOT NULL DEFAULT 'pending'`(`pending`|`accepted`|`declined`|`revoked`|`expired`), `invited_by TEXT NOT NULL`, `expires_at TEXT NOT NULL`, `used_at TEXT`, `created_at TEXT NOT NULL`. `(org_id, email, status)`·`(email)` 인덱스.
- **FR-008**: 초대 생성 `POST /api/v1/orgs/{id}/invitations`는 org_admin+만 호출 가능해야 하며(MUST), 토큰은 `secrets.token_urlsafe(32)` 생성·SHA-256 해시만 저장하고(`hash_token` 재사용), TTL은 72시간이어야 한다. 초대 메일 발송은 `mailer` 모듈의 기존 경로를 사용하고 토큰 원문은 메일 링크에만 포함한다.
- **FR-009**: 초대 수락 `POST /api/v1/invitations/{token}/accept`는 로그인 세션 필수이며 세션 이메일(소문자 정규화)이 초대 `email`과 정확히 일치해야 한다(MUST). 토큰 consume은 `consume_email_verification`과 동일한 원자적 `UPDATE ... WHERE used_at IS NULL AND expires_at>?` rowcount 판정이어야 한다. 성공 시 기존 user가 있으면 멤버십만 추가하고, 없으면 409 `invite_requires_signup`으로 가입을 유도한다.
- **FR-010**: `POST /api/v1/invitations/{token}/decline`, `DELETE /api/v1/orgs/{id}/invitations/{inv_id}`(revoke), `POST /api/v1/orgs/{id}/invitations/{inv_id}/resend`를 제공해야 한다(MUST). resend는 해당 (org,email)의 기존 pending 토큰을 모두 무효화(`used_at` 마킹 — `create_email_verification`의 기존 토큰 무효화 패턴)한 뒤 새 토큰을 발급하고, `latest_invitation_at` 기준 10분 미만 재발송은 429 `resend_too_soon`이다.
- **FR-011**: 초대 관련 모든 상태 전이(생성/재발송/수락/거절/취소/만료)는 `audit_events`에 기록해야 한다(MUST). 액션명: `org_invite_created`, `org_invite_resent`, `org_invite_accepted`, `org_invite_declined`, `org_invite_revoked`, `org_invite_expired`. 토큰 원문·메일 본문은 detail에 기록하지 않는다(이메일 주소는 actor 필드 관례에 따라 허용).
- **FR-012**: 초대 API에 rate limit을 적용해야 한다(MUST) — 생성/재발송은 조직당 시간당 상한 + IP 기준 버스트 제한(`_rate_hit` 패턴 재사용), accept/decline은 토큰당 시도 상한.

**조직 데이터 격리·마이그레이션**

- **FR-013**: 모든 조직 스코프 테이블에 `org_id TEXT NOT NULL DEFAULT ''` 컬럼을 추가해야 한다(MUST). 대상: `runs`, `stage_runs`(run 경유 간접 스코프 — 직접 컬럼 불필요, run_id 조인), `projects`, `tasks`, `agents`, `goals`, `evals`, `eval_runs`(eval 경유), `custom_workflows`, `workflow_revisions`, `connectors`, `routines`, `routine_runs`(routine 경유), `agent_permissions`, `agent_events`, `task_runs`(task/run 경유), `task_comments`, `task_artifacts`, `cost_events`, `approval_requests`, `recovery_actions`, `budget_policies`, `budget_incidents`, `inbox_items`, `notifications`, `reports`, `playbooks`, `dashboard_cards`, `share_links`, `workflow_schedules`, `triggers`, `event_links`, `llm_usage`, `scheduled_events`, `notification_jobs`, `message_extractions`, `email_summaries`, `agent_sessions`, `session_activity`, `audit_events`. 직접 컬럼이 불필요한 파생 테이블은 부모 FK 경유 스코프로 명시한다.
- **FR-014**: `org_id` 추가는 `_ensure_columns` 기반 `_migrate_org_columns()`로 구현해야 한다(MUST) — `CREATE TABLE IF NOT EXISTS`는 기존 테이블을 변경하지 않으므로 `migrations.py`의 기존 패턴을 따른다. 신규 테이블(FR-001/002/007 및 FR-022의 user_permissions)은 SCHEMA/SCHEMA_PG executescript가 자동 생성한다.
- **FR-015**: 기존 단일 테넌트 데이터는 기본 조직으로 백필해야 한다(MUST). 마이그레이션은 ① `kind='team'`·`slug='default'` 조직 1개 생성, ② 기존 `users` 전원을 멤버로 등록(가장 먼저 생성된 admin 사용자는 `org_owner`, 나머지 admin은 `org_admin`, member는 `member`), ③ `org_id=''`인 모든 행을 기본 조직 id로 UPDATE, ④ `users`의 최초 사용자 판정은 `created_at` 오름차순이다. 백필은 행 단위 best-effort + 실패 시 경고 로그(`_migrate_*` 관용 패턴).
- **FR-016**: org-scoped Store 조회 메서드는 `org_id` 필터 파라미터를 받아야 한다(MUST). `list_projects`/`get_project`/`find_project_by_name`/`list_runs`/`list_tasks`/`get_task` 등 모든 공개 조회 경로가 org_id를 받아 `WHERE org_id=?`를 포함해야 하며, API 계층은 세션의 `current_org_id`를 주입한다. 기계 행위자(API 키, `_admin_user`의 `{"id":"api-key"}`)는 기본 조직 컨텍스트로 동작해 기존 연동을 유지한다.
- **FR-017**: 조직 간 데이터 누출이 없어야 한다(MUST) — 목록 조회는 물론 ID 직접 접근(`GET /api/v1/management/tasks/{id}` 등)에서도 다른 조직 리소스는 404로 응답한다(403은 존재를 노출하므로 금지). 신규 생성 시 요청 body의 org_id 필드는 무시하고 세션 조직을 강제한다.
- **FR-018**: org 단위로 재스코프가 필요한 UNIQUE/PK 제약을 마이그레이션해야 한다(MUST): `idx_projects_name_lower` → `(org_id, lower(name))`, `agents`의 `idx_agents_name_lower` → `(org_id, lower(name))`, `idx_evals_name_lower` → `(org_id, lower(name))`, `custom_workflows.name` PK → 조직별 유일성(테이블 재작성 — `_migrate_external_company_sqlite`의 `__new` 테이블 패턴 재사용), `workflow_revisions UNIQUE(name,revision)` → `(org_id,name,revision)`, `budget_policies UNIQUE(scope_type,scope_id)` → `(org_id,scope_type,scope_id)`, `idx_inbox_items_dedupe` → `(org_id,dedupe_key)`, `message_extractions`/`email_summaries`/`scheduled_events`의 복합 PK → org_id 포함. 재작성 불가한 제약은 앱 계층 검증 + 인덱스로 이중 방어한다.
- **FR-019**: 백그라운드 경로(스케줄러 `run_automation_cycle`, routines, wakeups promote, watchdog, evals lazy 채점)도 조직 컨텍스트를 유지해야 한다(MUST) — 스케줄/룰 행에 `org_id`를 저장하고 run 생성·예산 평가(`budget_scope.evaluate_admission`)에 전파한다. 세션이 없는 경로의 "현재 조직"은 행의 `org_id`가 결정한다.
- **FR-020**: `audit_events`에 `org_id` 컬럼을 추가하고(MUST), 모든 org 관련 행위의 audit에 현재 조직 id를 기록한다. `GET /api/v1/orgs/{id}/audit`(org_admin+)로 조직 단위 감사 이력 조회를 제공해야 한다.

**리소스 단위 인가**

- **FR-021**: 사용자용 권한 테이블 `user_permissions`를 `agent_permissions`와 동일 구조로 정의해야 한다(MUST): `id`, `user_id`, `org_id`, `capability`, `effect`(allow|deny), `scope_type`(global|project|task), `scope_id`, `created_at`, `UNIQUE(user_id, capability, scope_type, scope_id, effect)`. capability 어휘는 `AgentCapability` enum을 재사용한다.
- **FR-022**: `services/permissions.py`의 판정 로직을 user principal로 일반화해야 한다(MUST) — `evaluate_permission`/`require_permission`이 `principal={"type":"agent"|"user","id":...}`를 받아 agent는 `agent_permissions`, user는 `user_permissions`+`organization_members`를 조회한다. deny 우선·scope 좁은 순(`_SCOPE_RANK`)·approval-required capability(`APPROVAL_REQUIRED_CAPABILITIES`/`CAPABILITY_APPROVAL_ACTION`) 규칙은 동일하게 적용한다.
- **FR-023**: 조직 역할 → 기본 capability 매핑을 정의해야 한다(MUST): `org_owner`/`org_admin`은 조직 스코프 내 모든 capability allow(관리 capability 포함), `member`는 읽기·실행 계열(task:read, agent:read, run:execute, cost:read, artifact:create)만 기본 allow하고 관리 계열(agent:manage, budget:manage, integration:manage 등)은 deny. 개별 `user_permissions` 규칙이 역할 기본값보다 우선한다(deny > allow > role default 순).
- **FR-024**: 프로젝트 단위 grant 관리 엔드포인트를 제공해야 한다(MUST): `GET /api/v1/orgs/{id}/projects/{project_id}/permissions`, `PUT /api/v1/orgs/{id}/projects/{project_id}/permissions/{user_id}`(규칙 집합 교체 — `validate_permission_rules`의 검증/정규화 재사용), `DELETE`(해당 사용자 규칙 삭제). grant 관리는 org_admin+ 또는 해당 프로젝트 manager만 가능하다.
- **FR-025**: org_admin+가 아닌 사용자의 관리 API 호출은 기존 `_lead_gate`/`_admin_gate` 패턴을 조직 스코프로 확장해 평가해야 한다(MUST) — `organization_members.role`이 `org_admin`/`org_owner`이면 조직 스코프 리소스의 lead/admin 게이트를 통과한다. 플랫폼 admin(`users.role='admin'`, env 이메일, API 키)은 전 조직 우회한다.
- **FR-026**: `agent_permissions` 평가는 agent가 속한 조직 컨텍스트에서만 매칭되어야 한다(MUST) — `agents.org_id`를 추가하고, project/task scope_id 해석 시 해당 리소스의 org와 agent의 org가 다르면 규칙을 매칭하지 않는다(교차 조직 권한 참조 방지).

**조직 설정·멤버 관리·삭제**

- **FR-027**: 멤버 관리 엔드포인트를 제공해야 한다(MUST): `GET /api/v1/orgs/{id}/members`(org_member+), `PATCH /api/v1/orgs/{id}/members/{user_id}`(role 변경, org_admin+; `org_owner` 지정/해제는 transfer만), `DELETE /api/v1/orgs/{id}/members/{user_id}`(제거, org_admin+; 본인 탈퇴는 `DELETE /api/v1/orgs/{id}/members/me`).
- **FR-028**: 조직 삭제는 `org_owner`만 `DELETE /api/v1/orgs/{id}`로 요청할 수 있어야 하며(MUST), ① 활성 run(`queued|running|waiting_approval`)이 1건 이상이면 409 `org_has_active_runs`+건수를 반환하고, ② 멤버가 2명 이상이면 409 `org_has_members`로 먼저 제거를 요구한다(개인 조직은 계정 삭제 경로 사용). 삭제는 `status='deleted'` soft-delete로 전이하고 멤버 접근을 즉시 차단하며, 30일 후 purge 잡이 org 스코프 행을 삭제한다(`audit_events`는 기존 actor 마스킹 관례로 보존).
- **FR-029**: 소유권 이관 `POST /api/v1/orgs/{id}/transfer {user_id}`는 현재 `org_owner`만 호출 가능해야 하며(MUST), 대상은 해당 조직의 `member` 이상이어야 한다. 이관은 단일 tx로 `organization_members.role`을 갱신하고 `org_ownership_transferred` 감사 이벤트를 남긴다.
- **FR-030**: 멤버 탈퇴/제거/강등 시 마지막 `org_owner` 보호를 적용해야 한다(MUST) — `store.exclusive()` 임계구역 안에서 owner 수를 확인하고 1명이면 409 `last_org_owner`를 반환한다(기존 last_admin 보호와 동일 TOCTOU 방어).
- **FR-031**: `organizations.status='suspended'` 시 멤버의 쓰기 API는 403 `org_suspended`로 차단하고 org_owner에게는 읽기 전용을 허용해야 한다(MUST). suspended/deleted 조직의 신규 run 생성·스케줄 실행도 차단한다.
- **FR-032**: 계정 삭제(`delete_user_cascade`)가 `organization_members`·`org_invitations`(본인 초대 행)·`user_permissions`를 함께 정리해야 한다(MUST) — 마지막 org_owner인 조직이 있으면 계정 삭제를 409로 차단하거나 소유권 이관을 요구한다.
- **FR-033**: 조직 관련 모든 행위(생성/수정/삭제/멤버 추가·제거·역할 변경/초대/이관/스위치)는 `audit_events`에 기록해야 한다(MUST). 액션명: `org_created`, `org_updated`, `org_deleted`, `org_suspended`, `org_member_added`, `org_member_removed`, `org_member_role_changed`, `org_ownership_transferred`, `org_switched`, `org_leave`. detail에는 토큰·비밀번호 등 민감값을 넣지 않는다.

### Non-Functional Requirements

- **NFR-001 (격리 강제)**: org_id 필터는 Store 쿼리 계층에서 강제해야 하며, API가 org_id를 누락해 전달하는 경로가 기본 조직으로 폴백하지 않고 명시적 오류를 내야 한다(잘못된 스코프의 조용한 폴백 금지 — 단 API 키 행위자의 기본 조직 폴백은 FR-016 예외로 명시).
- **NFR-002 (인덱스)**: `org_id`가 추가되는 모든 테이블에 `(org_id)` 또는 `(org_id, ...)` 복합 인덱스를 함께 생성해야 한다 — 목록 쿼리의 full scan 방지. 인덱스는 컬럼 보강 이후 `_migrate_org_columns`에서 생성한다(SCHEMA executescript는 구 DB에서 컬럼 부재로 실패하는 기존 제약과 동일 이유).
- **NFR-003 (호환성)**: 마이그레이션은 기존 배포를 무중단으로 유지해야 한다 — 신규 컬럼은 `DEFAULT ''`로 추가 후 백필, 실패 시 경고만 남기고 기동을 계속한다(`_migrate_*` 관용 패턴). 롤백은 컬럼/테이블 drop으로 가능해야 한다.
- **NFR-004 (성능)**: org 스코프 필터 추가 후 주요 목록 API(p95)의 응답 시간이 기존 대비 20% 이내로 유지되어야 한다 — `org_id` 인덱스 + `WHERE org_id=?` 선행 조건.
- **NFR-005 (보안)**: 초대 토큰·세션 토큰 원문은 DB에 해시만 저장하고 로그/audit/detail에 기록하지 않는다. 이메일 열거 방지를 위해 accept/decline의 오류 응답은 토큰 유효성과 무관하게 일반화된 메시지를 사용하되, 내부 audit에는 구체 사유를 남긴다.
- **NFR-006 (감사 완결성)**: 조직 스코프의 모든 상태 변경은 `audit_events`에 남아 `org_id`로 조회 가능해야 하며, 조직 삭제 후에도 해당 조직의 감사 이력은 보존 기간 동안 조회 가능해야 한다.

### Key Entities

- **Organization**: id·name·slug(전역 UNIQUE, lower)·kind(personal|team)·status(active|suspended|deleted)·감사 필드
- **OrgMembership**: (org_id,user_id) UNIQUE·role(org_owner|org_admin|member)·status·joined_at — 사용자↔조직 N:M
- **OrgInvitation**: org_id·email·role·token_hash(원문 미저장)·status(pending|accepted|declined|revoked|expired)·expires_at·used_at
- **UserPermission**: user_permissions 행 — user_id·org_id·capability(AgentCapability 어휘)·effect·scope_type(global|project|task)·scope_id — agent_permissions의 user 판
- **OrgRole→CapabilityMap**: 역할별 기본 capability 집합(코드 상수 — org_admin/owner=전체 allow, member=읽기/실행 계열)
- **SessionOrgContext**: `auth_sessions.current_org_id` — 요청별 조직 해석의 단일 원천
- **DefaultOrg(백필 산출물)**: 기존 데이터 전체를 수용하는 `slug='default'` 조직 + 멤버십 백필 규칙
- **AuditEvent**: 기존 행에 `org_id` 추가 — 조직 단위 감사 조회의 축

## 현재 코드와의 관계/재사용 포인트

- **권한 판정**: `services/permissions.py`의 `evaluate_permission`/`require_permission`/`_matching_rules`/`_SCOPE_RANK`/`APPROVAL_REQUIRED_CAPABILITIES`/`CAPABILITY_APPROVAL_ACTION`이 그대로 재사용 대상 — actor를 `agent_id` 문자열에서 `{type,id}` principal로 일반화한다. `validate_permission_rules`의 scope_id 정규화(project/task 실존 검증)도 user_permissions 검증에 재사용.
- **인증·세션**: `appctx.py`의 `_session_user`(세션 쿠키→사용자), `_effective_role`(env 오버라이드 포함), `_admin_user`/`_lead_user`/`_lead_gate`/`_admin_gate`, `store/admin.py`의 `promote_to_admin_if_first`(첫 사용자 admin)는 플랫폼 역할 계층으로 유지. 조직 게이트는 이 위에 조직 멤버십 조회를 얹는 새 헬퍼(`_org_user(request)` → `{user, org_id, org_role}`)로 추가한다.
- **토큰/초대 패턴**: `store/users.py`의 `create_email_verification`(기존 토큰 무효화+해시 저장), `consume_email_verification`(원자적 consume), `latest_verification_at`(재발송 throttle), `delete_expired_verifications`(opportunistic 정리)를 `org_invitations`에 그대로 미러링한다 — 신규 테이블 `OrgInvitationStoreMixin`(예: `store/orgs.py`)에 동일 메서드 세트를 구현.
- **마이그레이션**: `store/migrations.py`의 `_ensure_columns`(SQLite PRAGMA/information_schema 분기), `_migrate_phase3_columns`(컬럼+인덱스+백필 혼합 예시), `_migrate_external_company_sqlite`(UNIQUE 변경을 위한 테이블 재작성), `store/db.py` `__init__`의 `_migrate_*` 호출 체인에 `_migrate_org_columns`를 추가한다.
- **스키마 이중 정의**: `schema_sqlite.py` SCHEMA / `schema_pg.py` SCHEMA_PG — 신규 테이블 4종은 양쪽에 추가하고, `org_id` 컬럼은 양쪽 스키마의 신규 행용 DEFAULT와 마이그레이션 백필이 일치해야 한다.
- **감사·로그**: `store.audit(run_id, actor, action, object_ref, detail)`와 `_audit_api` 패턴, `exclusive()` 임계구역(last_admin 보호), `_rate_hit` 인메모리 레이트리밋을 그대로 사용한다.
- **계정 생명주기**: `delete_user_cascade`/`export_user_data`(admin.py)에 멤버십·초대·권한 정리를 추가하고, export에 조직 멤버십 정보를 포함한다.
- **외부 연동**: `external_*` 테이블의 `company_id`(Paperclip 조직 식별자)는 org_id와 별개 네임스페이스 — 매핑 테이블 또는 `company_id = org.slug` 규칙으로 연결하되 혼용하지 않는다.

### 구현 힌트(수정 대상 파일)

- `src/jobmate/store/schema_sqlite.py`, `schema_pg.py` — organizations/organization_members/org_invitations/user_permissions 테이블 + org_id 컬럼·인덱스
- `src/jobmate/store/migrations.py` — `_migrate_org_columns()`(컬럼 보강+백필+인덱스+UNIQUE 재스코프), `store/db.py` `__init__` 호출 체인 추가
- `src/jobmate/store/orgs.py`(신규) — OrgStoreMixin: 조직/멤버십/초대 CRUD, consume_org_invitation, transfer_ownership, last-owner 검사
- `src/jobmate/store/users.py`, `admin.py` — auth_sessions.current_org_id, delete_user_cascade/export 확장
- `src/jobmate/services/permissions.py` — principal 일반화 + role-default capability 맵
- `src/jobmate/api/appctx.py` — `_org_user()` 컨텍스트 헬퍼, 세션 조직 해석, org 게이트
- `src/jobmate/api/routers/orgs.py`(신규) — 조직/멤버/초대 엔드포인트; `api/app.py` 라우터 등록
- `src/jobmate/api/routers/*.py` 전체 — org_id 전파(세션 컨텍스트→store 호출)
- `src/jobmate/api/jobs.py`·routines·wakeups — 백그라운드 경로의 org_id 전파
- `tests/unit`, `tests/api` — 조직 격리·초대·인가 테스트 추가(기존 `test_permissions_api.py`, `test_management.py` 패턴)

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 기존 단일 테넌트 DB로 Store를 열면 기본 조직 1개·전체 사용자 멤버십·전 행 org_id 백필이 완료되고, 기존 API 동작(테스트 스위트)이 회귀 없이 통과한다.
- **SC-002**: 조직 A/B 각각에 프로젝트·task·run을 생성했을 때, 어느 조직의 목록/직접 조회에서도 상대 조직 리소스가 0건 반환된다(교차 접근 테스트 전수 통과).
- **SC-003**: 초대 발급→메일 링크→수락까지 HTTP로 완결되고, 동일 토큰 재사용·만료 토큰·이메일 불일치 수락이 모두 거부되며 각각 대응 audit 이벤트가 1건씩 기록된다.
- **SC-004**: 조직 역할 매핑이 검증된다 — org_admin은 조직 내 관리 API를 통과하고, member는 grant 없는 관리 작업에서 403 `permission_denied`를 받는다.
- **SC-005**: 마지막 org_owner의 탈퇴/제거/강등 시도가 409 `last_org_owner`로 차단되고, `store.exclusive()` 하에서 동시 2요청 레이스 테스트가 단일 owner 유지를 보장한다.
- **SC-006**: 활성 run이 있는 조직의 삭제가 409 `org_has_active_runs`로 거부되고, run 종결 후 soft-delete→멤버 접근 차단→audit 보존이 확인된다.
- **SC-007**: `user_permissions`의 deny 규칙이 role-default allow를 이기는 시나리오와, project 스코프 deny가 global allow를 이기는 시나리오가 `evaluate_permission` 단위 테스트로 통과한다.
- **SC-008**: 조직 생성/수정/삭제/멤버 변경/초대/이관/스위치 전 행위가 `audit_events`에 `org_id`와 함께 조회 가능하다.
- **SC-009**: `ruff check`·`ruff format --check`·`mypy src/jobmate`·`uv run pytest`가 모두 통과한다.

## Assumptions

- 플랫폼 역할(`users.role`)은 인스턴스 전역 관리자 의미를 유지하고, 조직 역할로 대체하지 않는다 — env 이메일/API 키의 우회 권한도 유지하되 감사에 actor를 남긴다.
- 한 사용자는 여러 조직의 멤버가 될 수 있고, `users` 계정은 이메일당 1개(전역 UNIQUE)다.
- 초대는 이메일 링크 방식만 지원한다 — 인앱 알림/Slack 초대는 범위 외. 메일 발송은 기존 `mailer` 모듈 설정을 재사용한다.
- 조직 삭제는 soft-delete + 30일 purge로 정의하며, purge 잡은 기존 `_automation_loop`/cron_tick 사이클에 편입한다.
- 워크스페이스 개념은 "개인 조직"과 "팀 조직"의 2종(`kind`)으로 충분하며, 별도 workspace 엔티티는 도입하지 않는다.
- `stage_runs`/`task_runs`/`eval_runs`/`routine_runs`/`notification_jobs`/`agent_session_events`/`trigger_fires`/`session_activity` 등 자식 테이블은 부모 행의 org_id로 간접 스코프한다 — 비정규화 org_id 컬럼은 읽기 성능이 필요한 경우 후속 최적화로 미룬다.
- 멀티 인스턴스 배포 시 `store.exclusive()`는 프로세스 내 직렬화만 보장한다는 기존 한계를 그대로 계승한다(DB 제약·조건부 UPDATE가 최후 보루).
- UI(대시보드의 조직 선택/멤버 관리 화면)는 별도 프론트엔드 스펙 범위이며, 본 스펙은 API 계약까지를 대상으로 한다.

## Out of Scope

- SSO/SAML/SCIM, 조직 단위 SSO 강제, 도메인 기반 자동 가입
- 조직 간 데이터 이전(migration between orgs), 프로젝트/태스크의 org 간 이동 API — `target_project_id` 패턴의 org 확장은 후속
- 조직별 커스텀 도메인, 화이트라벨, 조직 단위 결제/플랜/billing
- 조직 삭제의 즉시 물리 삭제 및 백업 보존 정책 상세(컴플라이언스 검토 필요)
- 멤버 초대의 링크 외 채널(Slack/메신저), 게스트(비멤버) 외부 협업자 역할
- `external_*`(Paperclip 브릿지)의 company_id와 org_id의 완전한 통합 매핑 — 읽기 전용 projection 호환만 유지
- 대시보드 UI의 조직 스위처·멤버 관리 화면 구현(프론트엔드 별도 작업)
