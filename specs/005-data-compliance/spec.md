# Feature Specification: jobMate 데이터·규정 준수 (개인정보보호법 대응)

**Feature Branch**: `005-data-compliance`

**Created**: 2026-11-19

**Status**: Draft

**Input**: 상용 서비스 전환을 위한 P0 규정 준수 기능 — 데이터 보존 정책(유형별 TTL·자동 purge·법정 보관 기본값), 개인정보 처리 완성(계정 삭제 cascade·처리방침 동의 이력·export 완성), 감사 로그 내보내기(필터·CSV/JSON·무결성), 개인정보 마스킹 표준화. 한국 시장 기준으로 개인정보보호법상 접속기록 보관(통상 1년)·파기·열람권·동의 이력을 충족한다.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - 데이터 보존 정책 관리와 자동 purge (Priority: P1)

관리자가 데이터 유형(메시지/세션/감사로그/이벤트 등)별 보존 기간을 env 기본값 또는 DB override로 설정한다. 스케줄 작업이 주기적으로 TTL이 지난 데이터를 purge하고, 각 실행은 건수만 기록한 purge 감사 이벤트를 남긴다. dry-run 모드로 삭제 대상을 미리 확인할 수 있다. 법정 보관이 필요한 항목(접속기록=인증/접근 로그 1년 등)은 하한이 강제되어 설정으로 더 짧게 줄일 수 없다.

**Why this priority**: 개인정보보호법상 저장·이용 목적 달성 후 지체 없이 파기 의무가 있고, 현재 `retention_sweep`는 기간이 코드에 하드코딩돼 있어 운영 중 조정이 불가능하다. 상용 서비스의 최소 규정 준수 기반이다.

**Independent Test**: TTL이 지난 테스트 행을 심고 purge를 실행해 삭제 건수·감사 이벤트·dry-run 무삭제를 검증할 수 있다.

**Acceptance Scenarios**:

1. **Given** 관리자가 `audit_events` 보존 기간을 DB override로 365일로 변경, **When** 설정 조회, **Then** env 기본값 대신 override가 적용되고 변경 이력이 audit에 남는다.
2. **Given** `audit_events`에 100일·400일 전 행 혼재, **When** 보존 기간 365일로 purge 실행, **Then** 400일 전 행만 삭제되고 100일 전 행은 유지된다.
3. **Given** purge 대상 데이터 존재, **When** dry-run 모드로 실행, **Then** 어떤 행도 삭제되지 않고 유형별 예상 삭제 건수가 반환된다.
4. **Given** 접근 로그 보존 기간을 30일로 설정 시도, **When** 저장, **Then** 법정 하한(1년=365일) 미만이라 422로 거부된다.
5. **Given** purge 실행 중 예외 발생, **When** 재실행, **Then** 이미 삭제된 행을 다시 세지 않고 멱등하게 완료된다(커서/배치 재개).

---

### User Story 2 - 개인정보 처리 완성: 동의 이력·계정 삭제·export (Priority: P1)

가입 시 이용약관·개인정보처리방침 버전·동의 시각·IP가 기록되고, 처리방침 버전이 올라가면 재동의 흐름이 작동한다. 계정 삭제는 타입별 처리 매트릭스(삭제/익명화/법정 보존)에 따라 전체 데이터가 처리되고 삭제 영수증이 발급된다. 본인 데이터 export는 현재 누락된 항목(동의 이력, 프로젝트/태스크 소유 데이터 등)을 포함해 완성된다.

**Why this priority**: 동의 이력은 개인정보 수집·이용의 적법성 근거이고, 계정 삭제 불완전은 개인정보보호법 제36조(파기) 위반 소지가 있다. 현재 `agree_terms`는 bool만 저장되고 `delete_user_cascade`는 users/auth_sessions/email_verifications/run_comments만 처리해 나머지 귀속 데이터가 잔존한다.

**Independent Test**: fake 사용자로 가입→동의 조회→export→계정 삭제→잔존 데이터 검사까지 HTTP로 검증할 수 있다.

**Acceptance Scenarios**:

1. **Given** 신규 가입 요청(agree_terms=true), **When** 가입 완료, **Then** `consent_records`에 terms_version·privacy_version·agreed_at·ip가 저장되고 audit에 `auth_signup`이 남는다.
2. **Given** 처리방침 버전이 v1→v2로 상향 배포, **When** 기존 사용자가 로그인/주요 API 호출, **Then** 재동의 필요 상태가 표시되고 동의 전에는 동의 관련 처리가 제한된다.
3. **Given** run_comments·push_subscriptions·share_links·inbox_items 등에 본인 데이터 존재, **When** 계정 삭제 실행, **Then** 매트릭스에 따라 삭제 또는 익명화되고 `deletion_receipts`에 유형별 처리 결과가 기록된다.
4. **Given** 삭제 실행 중 활성 세션 존재, **When** 삭제 완료, **Then** 모든 auth_sessions가 revoke되고 진행 중 run의 created_by는 익명화되며 실행 자체는 중단되지 않는다.
5. **Given** 법정 보관 대상 데이터(audit_events의 본인 행위 등), **When** 계정 삭제, **Then** 행은 보존하되 actor가 `deleted_user`로 마스킹되고 export에는 더 이상 본인 이메일이 노출되지 않는다.
6. **Given** 본인 데이터 export 요청, **When** 다운로드, **Then** users·sessions·audit_events·run_comments·동의 이력·push 구독·생성한 run 목록이 포함되고 password_hash·token_hash는 제외된다.

---

### User Story 3 - 감사 로그 조회·내보내기 (Priority: P2)

관리자가 `/api/v1/audit`을 기간·actor·action·run_id·대상 필터로 조회하고, 결과를 CSV/JSON으로 스트리밍 다운로드한다. 대용량 범위도 메모리에 전체 적재하지 않고 스트리밍한다. 선택적으로 행 해시 체인으로 내보낸 파일의 무결성을 검증할 수 있다. export 행위 자체가 감사 로그에 기록된다.

**Why this priority**: 사고 조사·규제 대응 시 감사 로그 추출은 필수 운영 기능이지만, 조회 UI 확장 없이는 수동 SQL 의존이 된다. P1 기능과 독립적으로 배포 가능하다.

**Independent Test**: 기간 필터로 export를 요청해 CSV 파싱·행 수·필터 정확성과 `audit_export` 감사 이벤트를 검증할 수 있다.

**Acceptance Scenarios**:

1. **Given** audit_events 10만 건, **When** `GET /api/v1/audit/export?format=csv&from=...&to=...`, **Then** 스트리밍 응답으로 전체 행이 반환되고 서버 메모리 사용이 행 수에 비례하지 않는다.
2. **Given** `action=auth_login` 필터, **When** export, **Then** 해당 action 행만 포함되고 헤더에 필터 조건이 명시된다.
3. **Given** export 요청, **When** 완료, **Then** `audit_export` 이벤트가 actor·필터·행 수와 함께 audit_events에 기록된다.
4. **Given** `integrity=hash` 옵션, **When** JSON export, **Then** 각 행의 순번 해시와 전체 파일 해시가 포함되어 변조 검증이 가능하다.
5. **Given** member 역할 사용자, **When** `/api/v1/audit*` 접근, **Then** 403이 반환된다(admin 전용 유지).

---

### User Story 4 - 개인정보(PII) 마스킹 표준화 (Priority: P3)

로그·관찰 이벤트(agent_events detail, stage 출력 요약, inbox/알림 본문 등)에서 이메일·전화번호·이름 등 PII가 정책에 따라 마스킹된다. 기존 `redaction.py`의 secret 키 마스킹과 별개로 PII 패턴 마스킹 계층이 표준화되고, 마스킹 적용 여부가 설정으로 제어된다.

**Why this priority**: 보안상 중요하지만 대부분의 PII 유출 경로는 이미 actor email이 audit에 기록되는 구조라 즉시 위험은 낮다. 표준화는 점진 적용이 가능해 P3.

**Independent Test**: PII가 포함된 문자열을 관찰 이벤트에 주입해 저장된 detail/로그 출력이 마스킹됐는지 검증할 수 있다.

**Acceptance Scenarios**:

1. **Given** agent_events detail에 이메일·전화번호 포함, **When** `pii_mask=on` 설정, **Then** 저장/조회 시 `a***@example.com`·`010-****-1234` 형태로 마스킹된다.
2. **Given** `pii_mask=off`(기본), **When** 동일 이벤트 저장, **Then** 기존 동작과 동일하게 원문이 유지된다(하위 호환).
3. **Given** audit_events actor 필드, **When** 조회, **Then** actor는 행위자 식별을 위해 마스킹 대상에서 제외된다(설정 가능한 예외 목록).

---

### Edge Cases

- **법정 보관 하한 위반 시도**: 접근/인증 로그 보존 기간을 1년 미만으로 설정 → 저장 거부(422). 단 DB 직접 수정 등 우회는 문서화된 운영 책임으로 둔다.
- **계정 삭제 중 활성 세션/run**: 삭제와 동시에 진행 중인 run이 created_by를 참조 → run은 계속 실행하되 created_by를 익명 라벨로 치환; 세션은 즉시 revoke.
- **마지막 admin 삭제**: 기존 409 보호 유지 — 삭제 매트릭스 적용 전 차단.
- **purge와 백업 상호작용**: purge는 라이브 DB만 대상. 백업 스냅샷에는 TTL 지난 데이터가 남을 수 있음을 운영 가이드에 명시하고, 백업 보존 주기(15_가이드)와 연동해 백업 자체도 만료 삭제한다.
- **동의 버전 상향 중 진행 중 요청**: 재동의 요구 상태에서도 이미 인증된 세션의 읽기 API는 허용하되, 쓰기/민감 작업은 재동의 후로 제한(설정 가능).
- **purge 도중 프로세스 크래시**: 배치 커서/건수 단위 tx로 재개 가능해야 하며, 같은 데이터가 두 번 삭제 집계되지 않는다.
- **export 대용량**: audit_events가 수백만 건일 때 export가 타임아웃되지 않도록 서버사이드 커서/스트리밍 필수.
- **삭제 영수증 분실**: 영수증은 사용자에게 이메일/다운로드로 제공하고 서버에는 해시된 식별자만 보관(영수증 자체가 개인정보가 되지 않게).
- **PII 마스킹 오탐**: `display_name` 같은 비-PII 문자열에 이메일 패턴이 우연히 포함되면 과도 마스킹 — 마스킹은 손실 허용 방향이지만 감사 actor 등 예외 목록으로 완화.
- **terms 버전 미설정 환경**: 로컬 개발 모드(api_key·allowed_emails·VERCEL 없음)에서는 동의 기록을 선택으로 유지 — 현재 `_email_verification_required()`와 동일한 게이트 재사용.

## Requirements *(mandatory)*

### Functional Requirements

#### 데이터 보존 정책

- **FR-001**: 시스템은 데이터 유형별 보존 기간을 관리해야 한다(MUST). 유형 최소 집합: `audit_events`(접근·인증·관리자 감사), `llm_usage`, `auth_sessions`, `runs`+자식(`stage_runs`/`approvals`/`run_comments`), `message_extractions`, `agent_session_events`, `wakeup_requests`, `budget_incidents`, `eval_runs`, `agent_events`, `inbox_items`, `notifications`, `email_summaries`, `message 본문 원문`. 각 유형은 TTL(일)·삭제 방식(hard delete/필드 비우기/anonymize)·적용 조건(예: run은 종결 상태만)을 가진다.
- **FR-002**: 보존 설정은 env 기본값 + DB override 2단계여야 한다(MUST). override는 `retention_policies` 테이블(또는 동등한 설정 테이블)에 저장되고 변경 시 `retention_policy_update` audit 이벤트(actor·유형·구/신 값)를 남긴다. API: `GET/PUT /api/v1/admin/retention`(admin 전용).
- **FR-003**: 법정 보관 하한을 강제해야 한다(MUST). 접근·인증 로그에 해당하는 유형의 TTL 하한은 365일(1년)이며, 미만 값 저장 시 422 `retention_below_legal_minimum`. 하한 대상 유형 목록은 설정 상수로 관리하고 변경 시 audit에 남긴다.
- **FR-004**: purge는 멱등·재개 가능해야 한다(MUST). 유형별로 `DELETE ... WHERE created_at < cutoff [AND 조건]` 배치(예: 500행/tx)로 실행하고, 배치 단위 커밋으로 크래시 후 재실행 시 중복 삭제·중복 집계가 없어야 한다.
- **FR-005**: 각 purge 실행은 `purge_runs` 테이블에 실행 ID·시작/종료 시각·유형별 삭제 건수·dry_run 여부·결과(status=completed/failed/partial)를 기록해야 한다(MUST). 건수만 기록하고 삭제된 행 내용은 기록하지 않는다.
- **FR-006**: 매 purge 실행 완료 시 `retention_purge` audit 이벤트를 남겨야 한다(MUST) — detail에는 유형별 건수 합계와 실행 ID만 포함(내용물 금지).
- **FR-007**: dry-run 모드를 지원해야 한다(MUST). `POST /api/v1/admin/retention/purge?dry_run=true`는 삭제 없이 유형별 예상 건수를 반환하고 `purge_runs`에 `dry_run=1`로 기록한다.
- **FR-008**: purge 스케줄은 기존 `run_automation_cycle`(jobs.py)의 `_retention_sweep` 호출 지점을 확장해 하루 1회 실행되어야 한다(MUST). `_automation_loop`와 `/api/v1/cron/tick` 두 진입점이 동일 경로를 공유한다는 기존 계약을 유지한다.
- **FR-009**: 진행 중 상태(queued/running/waiting_approval 등 비종결 run, 미완료 wakeup_requests, open budget_incidents, pending eval_runs, 미사용 email_verifications)는 TTL과 무관하게 삭제하지 않아야 한다(MUST). 현재 `_TERMINAL_RUN_STATUSES_SQL` 조건을 정책 엔진의 기본 조건으로 승격한다.
- **FR-010**: `message_extractions`의 `source_text` 비우기(30일) 같은 "행 유지·본문만 삭제" 방식과 hard delete를 유형별로 구분 지원해야 한다(MUST).

#### 개인정보 처리 완성

- **FR-011**: 가입 시 동의 이력을 기록해야 한다(MUST). 신규 테이블 `consent_records`(id, user_id, terms_version, privacy_version, agreed_at, ip, user_agent, created_at)에 저장하고, `auth_signup` audit detail에 `terms_version`을 포함한다. `agree_terms` bool 요구 조건은 현재와 동일하게 `_email_verification_required()` 게이트를 따른다.
- **FR-012**: 처리방침·이용약관 버전은 설정(env `JOBMATE_TERMS_VERSION`, `JOBMATE_PRIVACY_VERSION`, 기본값 예: `v1.0`)으로 관리하고, `GET /api/v1/auth/config` 응답에 현재 버전과 `privacy_policy_url`을 추가해야 한다(MUST).
- **FR-013**: 버전 상향 시 재동의 흐름을 제공해야 한다(MUST). 사용자의 최신 consent_records 버전이 현재 설정 버전보다 낮으면 `GET /api/v1/auth/me` 응답에 `consent_required: true`와 부족 버전을 포함하고, `POST /api/v1/auth/consent`(세션 인증)로 재동의 시 새 consent_records 행을 추가한다. 재동의 강제 범위(읽기 전용 vs 쓰기 차단)는 설정 `JOBMATE_CONSENT_ENFORCE`(off/read_only/block)로 제어한다.
- **FR-014**: 계정 삭제는 타입별 처리 매트릭스를 구현해야 한다(MUST). 최소 매트릭스:
  - **hard delete**: `users`, `auth_sessions`, `email_verifications`, `run_comments`, `push_subscriptions`, `share_links`(본인 생성분), `inbox_items`(본인 소유), `dashboard_cards`(본인 생성분)
  - **anonymize**: `runs.created_by`·`agent_events` 등 행위자 필드를 `deleted_user`로 치환(행은 업무 이력으로 보존), `audit_events.actor`는 기존과 동일하게 마스킹
  - **retain(법정/정책)**: `audit_events` 본인 행위 기록(기간은 FR-001 정책), `llm_usage`(비용 집계 익명 집계 — 행위자 미포함 확인), `message_extractions` 등 시스템 소유 데이터
  - 각 유형의 분류 근거(법정 보관/분쟁 대비/시스템 무결성)를 코드 주석·문서에 명시해야 한다.
- **FR-015**: 계정 삭제는 `store.exclusive()` 락 안에서 원자적으로 실행되어야 한다(MUST) — 현재 last-admin 체크와 동일 패턴 유지. 처리 결과를 `deletion_receipts` 테이블(id, user_id_hash, requested_at, completed_at, per-type counts JSON, status)에 기록하고 사용자에게 영수증 ID를 반환한다.
- **FR-016**: 삭제 영수증에는 본인 이메일·PII를 포함하지 않아야 한다(MUST) — user_id의 해시와 유형별 건수만 포함.
- **FR-017**: export(`GET /api/v1/auth/export`)를 완성해야 한다(MUST). 현재 반환(users/sessions/audit_events/run_comments/email_verifications)에 추가: `consent_records`, `push_subscriptions`(endpoint 제외 메타만), 본인 `created_by`인 runs 목록(id·workflow·상태·시각), 본인 생성 share_links 메타, inbox_items 본인 소유분. 비밀값(password_hash·token_hash·endpoint 원문)은 계속 제외.
- **FR-018**: export·삭제·재동의 등 개인정보 처리 행위는 모두 audit_events에 기록해야 한다(MUST) — `auth_data_export`(기존), `auth_account_delete`(기존), `auth_consent`(신규), `auth_account_delete_failed`(기존) 패턴 유지.

#### 감사 로그 내보내기

- **FR-019**: `GET /api/v1/audit`에 필터 파라미터를 추가해야 한다(MUST): `from`, `to`(ISO8601), `actor`, `action`(prefix 매칭 허용), `run_id`, `target`, `limit`, `cursor`(keyset pagination — `created_at,id` 복합).
- **FR-020**: `GET /api/v1/audit/export?format=csv|json&from&to&actor&action&run_id&target`를 추가해야 한다(MUST). admin 전용. `StreamingResponse`로 행 단위 yield하여 대용량 범위에서도 상주 메모리가 상한 내에 유지되어야 한다.
- **FR-021**: CSV export는 스프레드시트 수식 인젝션 방지(`'` 접두, 기존 `_csv_safe` 패턴)와 UTF-8 BOM을 적용해야 한다(MUST). JSON export는 `{generated_at, filters, rows: [...]}` 구조.
- **FR-022**: `integrity=hash` 옵션 시 JSON export에 행 순번 해시 체인(`prev_hash` 포함)과 파일 전체 해시를 포함해야 한다(MUST). 해시 알고리즘·정규화 규칙을 문서화한다.
- **FR-023**: audit export 실행 자체를 `audit_export` 이벤트로 기록해야 한다(MUST) — detail: 필터 요약·형식·반환 행 수·요청자. export 대상 데이터 내용은 기록하지 않는다.
- **FR-024**: audit 조회/Export의 접근 제어는 admin(`_admin_user`)으로 유지해야 한다(MUST). member/lead는 403.
- **FR-025**: 장기 보관 정책: audit_events의 TTL은 법정 하한(365일) 이상만 허용(FR-003과 동일 장치)하고, 장기 보관이 필요한 조직은 export→오프라인/WORM 저장 절차를 운영 가이드에 명시해야 한다(MUST — 문서 작업 포함).

#### 개인정보 마스킹

- **FR-026**: PII 마스킹 계층을 `services/redaction.py`에 추가하거나 인접 모듈로 표준화해야 한다(MUST). 패턴 최소 집합: 이메일, 국내 전화번호(010/02/지역번호), 주민등록번호 형태(차단 대상 — 저장 자체를 경고), 신용카드 형태. 마스킹 함수는 순수 함수로 시그니처 `mask_pii(text: str, config) -> str` 형태.
- **FR-027**: 마스킹 적용 지점은 `agent_events.detail`, `agent_session_events`의 doing/ask 등 관찰 이벤트 직렬화 경로와 로그 포맷터에 한정한다(MUST). `audit_events.actor`·`users.email` 등 행위자 식별 필드는 예외 목록으로 마스킹하지 않는다.
- **FR-028**: PII 마스킹은 env `JOBMATE_PII_MASK`(기본 `off`)로 제어하고, `on` 시 위 패턴이 적용되어야 한다(MUST). off가 기본인 이유는 기존 동작·테스트 하위 호환 — 상용 배포 가이드에서 on 권장.
- **FR-029**: 기존 secret 마스킹(`redact_adapter_config`, `_SECRET_KEY_RE`)과 PII 마스킹은 독립 계층으로 공존해야 한다(MUST) — secret 키 마스킹은 값 단위, PII 마스킹은 자유 텍스트 단위.

### Key Entities

- **RetentionPolicy**: 데이터 유형 키·TTL 일수·삭제 방식(delete/clear_field/anonymize)·적용 조건·출처(env 기본/DB override)·법정 하한 여부
- **PurgeRun**: 실행 ID·시작/종료·dry_run·유형별 건수·status — 내용물 없이 건수만
- **ConsentRecord**: user_id·terms_version·privacy_version·agreed_at·ip·user_agent — 추가만 가능(UPDATE/DELETE 없음, 파기 시 사용자 단위 삭제는 FR-014 매트릭스)
- **DeletionReceipt**: user_id_hash·요청/완료 시각·유형별 처리 결과(counts)·status
- **AuditEvent**(기존): id·run_id·actor·action·object_ref·detail·created_at — export의 원천
- **User**(기존): users 테이블 — 삭제 시 hard delete, 참조는 anonymize

## 현재 코드와의 관계

| 영역 | 현재 상태 | 이번 스펙에서의 변경 |
|---|---|---|
| 보존 | `store/admin.py:retention_sweep()` — 기간이 함수 내 하드코딩(30/90/180/365일), 하루 1회 `run_automation_cycle`→`_retention_sweep`(jobs.py:84)에서 호출 | 정책 엔진(유형별 TTL·override·하한·dry-run·purge_runs)으로 승격. 기존 하드코딩 값이 env 기본값의 초기값 |
| 동의 | `auth.py:auth_signup` — `agree_terms` bool만 검사·audit detail에 `terms_agreed` 기록. 버전/시각/IP 미저장 | `consent_records` 테이블 + 버전 설정 + 재동의 플로우(FR-011~013) |
| 계정 삭제 | `store/admin.py:delete_user_cascade()` — users/auth_sessions/email_verifications/run_comments만 삭제, audit actor만 마스킹. push_subscriptions·share_links·inbox_items·runs.created_by 미처리 | 처리 매트릭스(FR-014)로 확장 + `deletion_receipts` |
| export | `auth.py:auth_export` → `store.export_user_data()` — users/sessions/audit_events/run_comments/email_verifications만 | consent·push·runs·share·inbox 추가(FR-017) |
| audit 조회 | `misc.py:/api/v1/audit` — limit만, 최신 500건 상한, 필터 없음 | 필터·keyset 페이지네이션(FR-019) + `/audit/export` 스트리밍(FR-020~023) |
| PII 마스킹 | `services/redaction.py` — secret-like 키 값 마스킹만. PII 패턴 마스킹 없음. `api/sensitive_fields.py` — audit detail 프로젝트 필드 마스킹 | PII 텍스트 마스킹 계층 추가(FR-026~029), 적용 지점은 관찰 이벤트/로그로 한정 |
| 스키마 | `schema_sqlite.py`/`schema_pg.py` 이중 정의 + `migrations.py` ALTER 보강 패턴 | `consent_records`·`purge_runs`·`deletion_receipts`·`retention_policies`(필요 시) 신규 테이블을 양쪽 스키마에 추가하고 기존 DB는 migrations로 보강 |

## 구현 힌트

- **보존 정책 엔진**: `retention_sweep(now)` 시그니처를 유지하되 내부를 `RetentionPolicy` 목록 순회로 교체. 각 정책은 `(key, table, cutoff_column, ttl_days, mode, extra_where)` 튜플/ dataclass. `now` 파라미터 주입으로 테스트 결정성 유지(기존 패턴).
- **purge_runs 기록**: sweep 자체도 DB 쓰기이므로 `purge_runs` INSERT는 sweep 트랜잭션 **밖**에서 별도 tx로 — sweep 실패 시에도 실행 시도 기록이 남아야 한다.
- **동의 버전 비교**: `consent_required` 판정은 `MAX(consent_records.terms_version)`이 아니라 **행 존재 + 버전 문자열 비교**로 — 버전은 `v1.0` 같은 semver 문자열로 두고 tuple 변환 비교. 최초 가입 전 사용자는 consent 없음 → enforce 모드에 따라 처리.
- **삭제 매트릭스 구현 위치**: `store/admin.py:delete_user_cascade` 확장보다는 `store/`에 `deletion.py` 믹스인 신설 후 AdminStoreMixin에 합성 — admin.py가 이미 446줄로 비대.
- **audit export 스트리밍**: `store.list_audit`는 limit 클램프(최대 1000)가 있으므로 export용 `iter_audit(filters, cursor)` 제너레이터를 별도 추가 — keyset은 `(created_at, id)` 내림차순.
- **PII 마스킹 적용 지점**: `agent_events`/`agent_session_events` insert 경로(store 계층)에서 마스킹하면 조회 경로 수정 불필요. 단 actor email이 detail에 들어가는 경우가 있어 예외 패턴 필요 — FR-027 예외 목록과 정합 확인.
- **검증 명령**: `uv run pytest`, `uv run ruff check src/jobmate tests`, `uv run mypy src/jobmate`. 코드 수정 후 `graphify update .`.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 모든 데이터 유형의 보존 기간이 env 또는 API로 조회·변경 가능하고, 변경이 audit에 100% 기록된다.
- **SC-002**: 법정 하한(365일) 미만 보존 기간 설정 시도가 0건 성공한다(전부 422).
- **SC-003**: TTL 경과 데이터가 스케줄 실행 후 다음 사이클 내 삭제되고, `purge_runs`에 유형별 건수가 기록된다. dry-run은 삭제 0건을 보장한다.
- **SC-004**: 가입 시 consent_records에 version·시각·IP가 누락 없이 기록되고, 버전 상향 후 재동의 없이는 enforce 모드 정책이 적용된다.
- **SC-005**: 계정 삭제 후 매트릭스상 삭제/익명화 대상에 본인 PII 잔존이 0건이다(export·직접 SQL 검사로 검증). audit_events 본인 행위는 actor 마스킹 상태로만 남는다.
- **SC-006**: audit export가 10만 건 규모에서도 스트리밍으로 완료되고, 반환 행 수·필터가 요청과 정확히 일치하며 `audit_export` 이벤트가 1건 기록된다.
- **SC-007**: `JOBMATE_PII_MASK=on`에서 관찰 이벤트 detail 내 이메일·전화번호 패턴이 0건 원문 노출된다(테스트 fixture로 검증).
- **SC-008**: 신규 테이블이 SQLite/PostgreSQL 양쪽 스키마에 정의되고, 기존 DB는 migrations로 자동 보강된다(`uv run pytest` 전체 통과).

## Assumptions

- 법정 보관 하한 365일(1년)은 개인정보보호법 접속기록 보관의 통상 기준을 따르며, 시행령·고시 세부(대규모/민감정보 시 2년)는 배포 전 법무 확인이 필요하다 — 스펙은 하한을 설정 상수로 두어 조정 가능하게 한다.
- 계정 삭제는 "탈퇴"가 아닌 "계정 파기"로 해석 — 동의 이력(consent_records)도 파기 대상에 포함하되, 분쟁 대비가 필요한 audit_events는 actor 마스킹으로 처리한다.
- 조직/타 사용자 데이터: 본인 생성 콘텐츠(share_links·cards·inbox_items)만 삭제하고, 타인이 본인 데이터를 참조하는 구조(예: 타인 run의 입력에 본인 이메일 포함)는 구조적 한계로 범위 외 — 문서에 명시.
- purge 대상 백업: DB 백업 스냅샷의 보존은 15_백업_복구_가이드 정책을 따르며, 본 스펙은 라이브 DB purge만 다룬다.
- PII 마스킹 기본값 off는 하위 호환을 위한 것이며, 상용 배포 체크리스트에서 on을 권장한다.
- export 형식은 JSON 단일(현재 `/auth/export`와 동일) — CSV export는 audit 전용(FR-020)으로 한정.
- `retention_policies` 오버라이드는 단일 인스턴스 SQLite/PG 공용 store 계층에 구현 — 멀티 인스턴스 환경의 분산 락은 범위 외.

## Out of Scope

- 백업 파일 자체의 암호화·WORM 저장소 연동(운영 가이드 문서화까지만)
- GDPR 등 해외 규정 대응(국내 개인정보보호법 기준)
- 데이터 주체 권리 자동화 포털(열람/정정/삭제 요청 접수 워크플로) — API 수준만 제공
- 로그 외부 전송(로그 서버/SIEM 연동)
- 암호화 키 로테이션 자동화(`purge-undecryptable` 수동 경로는 유지)
- 관리자 UI 프론트엔드 구현 — API 수준까지만(기존 index.html 대시보드 연동은 후속)
