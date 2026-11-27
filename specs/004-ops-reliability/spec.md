# Feature Specification: jobMate 운영·신뢰성 (P0)

**Feature Branch**: `004-ops-reliability`

**Created**: 2026-12-02

**Status**: Draft

**Input**: 상용 서비스 수준의 운영·신뢰성 기능 — 아웃바운드 웹훅(이벤트 알림), 헬스/상태 고도화(공개 상태 페이지용 엔드포인트), 백업 자동화(스케줄·보존·복원 검증), 에러 추적(Sentry 연동 설계), Prometheus 메트릭 표준. 기존 `docs/jobMate_v1.0.1/17_운영_보안_가이드_v1.0.1.md` 부록 A의 필수 로깅 기준(토큰·PII·push endpoint 원문 로깅 금지)을 모든 신규 경로에 적용한다.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - 아웃바운드 웹훅 이벤트 구독과 전달 (Priority: P1)

고객(운영자/외부 시스템)이 jobMate의 run 라이프사이클 이벤트를 자기 HTTPS 엔드포인트로 받아간다. 구독 CRUD API와 대시보드 데이터로 구독을 관리하고, 이벤트 발생 시 HMAC-SHA256 서명된 HTTP POST가 전달된다. 전달 실패는 지수 백오프로 재시도되고, 한계를 넘으면 dead-letter로 격리되어 운영자가 확인·재처리할 수 있다.

**Why this priority**: 상용 서비스의 필수 통합 표면 — 고객이 run 완료/승인 요청/예산 초과를 자기 시스템(Slack 대체, SIEM, 워크플로 트리거)과 연동하는 유일한 push 채널이다. messenger.py의 Slack/Telegram notify는 단방향 고정 채널이라 고객별 URL·이벤트 선택·재시도가 불가하다.

**Independent Test**: fake HTTP 서버(httpx MockTransport 또는 로컬 수신기)로 구독 생성→이벤트 발생→서명 검증→재시도/DLQ 전이를 HTTP 없이 store+서비스 단위로 검증할 수 있다.

**Acceptance Scenarios**:

1. **Given** 활성 구독(https URL, 이벤트 `run.completed`)과 secret, **When** run이 completed로 전이, **Then** `webhook_deliveries`에 pending 레코드가 생기고 다음 automation cycle에서 해당 URL로 POST가 1회 이상 시도된다.
2. **Given** 전달 POST가 500을 반환, **When** 재시도 스케줄 도래, **Then** `X-JobMate-Signature: t=<unix>,v1=<hex>` 헤더가 `HMAC_SHA256(secret, "{t}.{raw_body}")`와 일치하고 재시도 횟수가 증가한다.
3. **Given** 수신기가 연속 실패해 재시도 상한 도달, **When** 마지막 시도 실패, **Then** delivery status가 `dead`가 되고 추가 자동 재시도가 멈추며 운영자 수동 재시도 API로 재큐할 수 있다.
4. **Given** 구독 URL이 사설 IP/루프백/비표준 포트, **When** 구독 생성 API 호출, **Then** 422로 거부되고 어떤 outbound 요청도 발생하지 않는다.
5. **Given** `run.completed`만 구독한 엔드포인트, **When** `approval.requested` 이벤트 발생, **Then** 해당 구독에는 delivery가 생성되지 않는다.

---

### User Story 2 - 헬스/상태 고도화와 장애 알림 (Priority: P1)

외부 상태 페이지와 내부 운영자가 서비스 상태를 계층별로 확인한다. 공개 엔드포인트는 민감정보 없는 요약 상태(ok/degraded/down + 컴포넌트별 상태)만 반환하고, 인증된 상세 엔드포인트는 DB·LLM·Gmail·디스크·백업 등 각 의존성의 실측 상태(지연시간·오류 유형)를 반환한다. 의존성 상태가 ok→down으로 전이하면 웹훅/메신저/인박스로 장애 알림이 발행된다.

**Why this priority**: 상용 서비스의 가용성 신호 — 상태 페이지, 외부 모니터링, 온콜 알림의 기반이며, 기존 `/api/v1/health`가 `{"status":"ok"}` 상수만 반환해 실질적 헬스 신호가 없다. `/api/v1/health/detail`은 이미 admin 게이트가 있으나 의존성 실측(DB ping, LLM, 디스크 여유)이 없다.

**Independent Test**: 각 의존성을 fake(실패하는 store/LLM/디스크 경로)로 주입해 ok/degraded/down 판정과 타임아웃·전이 알림을 개별 검증할 수 있다.

**Acceptance Scenarios**:

1. **Given** DB 연결 정상, **When** `GET /api/v1/health/public` 비인증 호출, **Then** `{"status":"ok","components":{"api":"ok","db":"ok",...}}` 형태의 요약만 반환되고 DB 경로·호스트명·내부 IP·건수가 포함되지 않는다.
2. **Given** DB가 응답하지 않음(SELECT 1 타임아웃), **When** `GET /api/v1/health/detail` admin 호출, **Then** db 컴포넌트가 `down` + latency/오류 유형으로 보고되고 전체 status가 `degraded`/`down`으로 계산된다.
3. **Given** 컴포넌트가 ok→down 전이, **When** 헬스 체크 주기 실행, **Then** `health.degraded` 웹훅 이벤트 + 인박스 항목 + (설정 시) messenger.notify가 발행되고, 동일 상태 지속 중 중복 알림은 발행되지 않는다(지문 dedupe).
4. **Given** 비인증 클라이언트, **When** `/api/v1/health/detail` 호출, **Then** 401 — 공개 엔드포인트는 `/api/v1/health/public`로만 접근 가능하다.

---

### User Story 3 - 백업 자동화와 복원 검증 (Priority: P1)

운영 DB(SQLite 파일 또는 PostgreSQL)를 스케줄에 따라 자동 백업하고, 보존 정책으로 오래된 백업을 정리하며, 생성된 백업의 복원 가능성을 주기적으로 검증한다. 백업 성공/실패/검증 결과는 DB에 이력으로 남고 헬스 상세·리포트·웹훅 이벤트로 노출된다. `docs/jobMate_v1.0.1/15_PostgreSQL_백업_복구_가이드_v1.0.1.md`의 원칙(암호화 백업, 보존 기간, 복구 후 확인, 감사 기록)을 코드화한다.

**Why this priority**: 데이터 손실은 상용 서비스의 존립 문제. 현재 백업은 문서 수준 runbook뿐이며 자동화·검증 장치가 없다.

**Independent Test**: 임시 디렉터리의 SQLite DB로 백업 생성→파일 무결성→보존 삭제→복원 검증(임시 DB에 restore 후 테이블 카운트 대조)을 단위 테스트로 검증할 수 있다. PG 경로는 `pg_dump` 존재 여부에 따라 skip/fake 처리한다.

**Acceptance Scenarios**:

1. **Given** SQLite 백엔드와 `JOBMATE_BACKUP_INTERVAL_HOURS>0`, **When** automation cycle에서 백업 주기 도래, **Then** `JOBMATE_BACKUP_DIR`에 타임스탬프된 백업 파일이 생성되고 `backup_runs`에 success 레코드 + audit(`backup_created`)가 남는다.
2. **Given** `JOBMATE_BACKUP_ENCRYPTION=1`, **When** 백업 생성, **Then** 산출물이 Fernet(`enc` 포맷 또는 암호화 컨테이너)으로 저장되고 평문 DB 내용이 파일에 노출되지 않는다.
3. **Given** 보존 기간 경과 백업 N건, **When** 백업 사이클 실행, **Then** 최신 K건/기간 내만 남기고 오래된 파일이 삭제되며 삭제 내역이 로그에 남는다.
4. **Given** 최근 성공 백업, **When** 복원 검증 잡 실행, **Then** 백업을 임시 경로에 복원해 무결성 검사(SQLite: `PRAGMA integrity_check`; PG: 스키마/핵심 테이블 카운트)를 수행하고 결과를 `backup_runs.verify_status`에 기록한다.
5. **Given** 백업 실패 또는 마지막 성공 백업이 staleness 임계 초과, **When** 헬스 체크/리포트 조회, **Then** backup 컴포넌트가 degraded/down으로 보고되고 `backup.failed` 이벤트가 알림 채널로 발행된다.

---

### User Story 4 - 에러 추적(Sentry) 연동 (Priority: P2)

`JOBMATE_SENTRY_DSN` 설정 시 프로세스 예외와 주요 실패 경로가 Sentry로 전송된다. 미설정 시 완전 비활성(기존 로깅 동작 불변). 전송 전 PII 스크러빙(요청 본문·Authorization·쿠키·사용자 식별자 제거)과 샘플링 레이트를 env로 제어한다.

**Why this priority**: 운영 가시성은 중요하지만 기존 `logger.exception` + 접근 로그로 최소 신호는 존재하고, Sentry는 외부 의존성이므로 P2.

**Independent Test**: sentry-sdk의 transport를 fake로 주입해 예외 캡처·스크러빙·샘플링을 오프라인 검증할 수 있다.

**Acceptance Scenarios**:

1. **Given** `JOBMATE_SENTRY_DSN` 미설정, **When** 앱 기동·예외 발생, **Then** Sentry 초기화 없이 기존 로깅만 동작한다(회귀 없음).
2. **Given** DSN 설정 + `JOBMATE_SENTRY_TRACES_SAMPLE_RATE=0.1`, **When** `/api` 핸들러에서 미처리 예외 발생, **Then** 이벤트가 전송되고 요청 컨텍스트(request_id, path, method)만 태그로 포함되며 body·헤더·쿠키는 제거된다.
3. **Given** DSN 설정, **When** `_automation_loop` 스레드에서 예외, **Then** 해당 예외도 캡처되고 fingerprint가 안정적이다.

---

### User Story 5 - Prometheus 메트릭 표준 (Priority: P2)

Prometheus 스크래이퍼가 `/api/v1/metrics`에서 표준 text exposition(0.0.4)을 수집한다. HTTP 지연/상태코드, run 수·상태 분포, 큐 깊이(웹훅 pending·인박스 미읽음), LLM 토큰/비용 카운터, 백업/헬스 상태 게이지를 노출한다. 라벨 카디널리티는 상한이 강제된다(사용자 ID·이메일·run ID 원문 금지).

**Why this priority**: 알림 파이프라인의 계량 기반 — US1~3의 알림 임계값(큐 깊이, 실패율, 백업 staleness)을 Prometheus/Grafana로 평가하는 표준 경로. 단, 핵심 기능은 자체 헬스/웹훅 알림이므로 P2.

**Independent Test**: TestClient로 엔드포인트를 호출해 exposition 파싱(주석/타입/값)과 라벨 집합을 검증하고, 카디널리티 위반 라벨이 렌더링되지 않음을 확인한다.

**Acceptance Scenarios**:

1. **Given** run 3건 완료 + 1건 실패, **When** `GET /api/v1/metrics` 인증 호출, **Then** `jobmate_runs_total{workflow="dev_review",status="completed"}` 등 카운터가 증가분만큼 정확히 노출된다.
2. **Given** 비인증 클라이언트(기본 정책), **When** `/api/v1/metrics` 호출, **Then** 401; `JOBMATE_METRICS_TOKEN` bearer 또는 세션/API 키로는 200이다.
3. **Given** 임의의 요청 경로 `/api/v1/runs/abc123`, **When** 메트릭 수집, **Then** `path` 라벨이 라우트 템플릿(`/api/v1/runs/{run_id}`)으로 정규화되고 원문 ID가 라벨에 나타나지 않는다.

---

### Edge Cases

- 웹훅 수신기가 리다이렉트(3xx)를 반환 → 리다이렉트는 따라가지 않는다(검증 우회 방지, webutil.fetch_*와 동일 정책). 3xx는 실패로 재시도 대상.
- 웹훅 URL DNS가 검증 시점과 전송 시점 사이에 사설 IP로 리바인딩 → `pinned_transport_for`/`_pinned_ip_for`로 connect IP 고정(TOCTOU 차단).
- 같은 이벤트에 구독 N개 → delivery는 구독별 N건으로 생성되고 한 구독 실패가 다른 구독 전송을 막지 않는다.
- delivery 재시도 중 구독이 비활성화/삭제 → 해당 delivery는 `cancelled`로 종료하고 재시도하지 않는다.
- 이벤트 발생과 delivery 생성 사이 프로세스 크래시 → 이벤트→delivery는 store 트랜잭션 또는 다음 cycle의 스캔으로 복구(최소 1회 전달 보장 방향).
- 백업 디스크 부족/권한 오류 → `backup_runs`에 failed + audit, 헬스 degraded, 예외가 automation loop를 죽이지 않는다.
- SQLite WAL 모드에서 온라인 백업 → `sqlite3.Connection.backup()` API 사용(파일 단순 복사는 WAL 미반영 위험).
- PG 환경에서 `pg_dump` 바이너리 부재 → 명확한 오류로 failed 기록(부분 백업 산출물 방지).
- 헬스 체크 의존성이 모두 느림 → 체크별 타임아웃(기본 2s) + 병렬 실행으로 전체 지연 상한 보장.
- 메트릭 수집이 자주 호출됨 → 카운터는 인메모리 누적 + 주기적 DB 스냅샷 조합, `/metrics` 자체는 store 무거운 쿼리를 매번 실행하지 않는다.
- Sentry DSN이 잘못됨/네트워크 차단 → 초기화 실패는 경고 로그 후 비활성, 앱 기동을 막지 않는다.
- 공개 헬스 엔드포인트가 rate-limit/스캐너에 노출 → 응답은 최소 필드만, 캐시 지시 헤더(`Cache-Control: no-store`) 적용.

## Requirements *(mandatory)*

### Functional Requirements

**아웃바운드 웹훅**

- **FR-001**: 시스템은 웹훅 구독을 저장하는 `webhook_subscriptions` 테이블을 가져야 한다. 필수 컬럼: `id`, `url`(https, 비-loopback은 SSRF 가드 통과), `secret_enc`(Fernet 암호문, `security.encrypt_str` 사용), `events`(JSON 배열 또는 별도 매핑 테이블), `enabled`, `created_by`, `created_at`, `updated_at`, `last_delivery_at`.
- **FR-002**: 웹훅 전달 이력을 `webhook_deliveries` 테이블에 남겨야 한다. 필수 컬럼: `id`, `subscription_id`, `event_type`, `event_id`(멱등/중복 식별), `payload_json`, `status`(`pending|delivering|success|failed|dead|cancelled`), `attempts`, `next_retry_at`, `last_http_status`, `last_error`(상태코드/예외 유형까지만 — 응답 본문·시크릿 금지), `created_at`, `delivered_at`.
- **FR-003**: 이벤트 타입은 최소 `run.completed`, `run.failed`, `run.needs_human`, `run.waiting_approval`, `approval.requested`, `approval.decided`, `budget.exceeded`, `backup.failed`, `backup.verified`, `health.degraded`, `error.unhandled`을 지원해야 한다. 이벤트 발생 지점은 `store.update_run` 종결 경로(`api/jobs.py::_execute_run_job`), 승인 전이(`api/appctx.py::_do_approve`, `api/jobs.py::_check_waiting_approvals`), 예산 차단(`api/jobs.py::_process_due_schedules`의 `schedule_budget_exceeded`, `api/routers/runs.py`의 예산 거부), 백업 잡, 헬스 전이, `_unhandled_exception` 핸들러다.
- **FR-004**: 구독 관리는 `POST/GET/PATCH/DELETE /api/v1/webhooks` + `POST /api/v1/webhooks/{id}/rotate-secret` + `POST /api/v1/webhooks/{id}/test`(샘플 이벤트 dry-run) + `GET /api/v1/webhooks/{id}/deliveries`(최근 N건) 엔드포인트로 제공해야 한다. 생성/수정/삭제/시크릿 로테이션은 admin 또는 lead 역할 + `store.audit`(`webhook_create|webhook_update|webhook_delete|webhook_rotate_secret`) 기록이 필수다.
- **FR-005**: 시크릿은 구독 생성 시 서버가 생성(`secrets.token_urlsafe(32)`)해 1회만 응답에 포함하고, DB에는 `encrypt_str` 암호문만 저장한다. API 응답·로그·delivery payload에 시크릿 원문을 포함하지 않는다.
- **FR-006**: 전달 POST는 `httpx`로 수행하고 헤더는 `Content-Type: application/json`, `X-JobMate-Event: <type>`, `X-JobMate-Delivery: <delivery_id>`, `X-JobMate-Timestamp: <unix>`, `X-JobMate-Signature: t=<unix>,v1=<hex>`를 포함해야 한다. 서명은 `HMAC_SHA256(secret, f"{t}.{raw_body}")`의 hex이며 수신자가 재계산 검증할 수 있어야 한다.
- **FR-007**: 페이로드는 `{"v":"1.0","id":"<event_id>","type":"<event_type>","created_at":"<iso>","data":{...}}` 스키마 버전을 가져야 하며, `data`에는 run_id·workflow·status·오류 유형 등 식별자 수준 정보만 포함하고 프롬프트/메일 본문/토큰/이메일 주소는 포함하지 않는다(17번 가이드 부록 A 준수).
- **FR-008**: 재시도는 지수 백오프(예: 1분→5분→30분→2시간→6시간, 최대 5회)로 `next_retry_at`에 스케줄하고, 상한 초과 시 `dead`로 전이한다. 재시도/전송 처리는 `_automation_loop`/`run_automation_cycle`의 주기 잡(`_process_webhook_deliveries`)에서 수행해 서버리스 cron tick 경로에서도 동작해야 한다.
- **FR-009**: 웹훅 URL은 `webutil.validate_outbound_url` 정책을 통과해야 한다 — http는 loopback 테스트 전용, 비-loopback은 https + `is_safe_fetch_url` 통과, userinfo 금지. 전송 시 `pinned_transport_for`/`_PinnedTransport` 패턴으로 DNS 리바인딩을 차단한다(`messenger._post_json` 참조). 리다이렉트는 따라가지 않는다.
- **FR-010**: 구독별 동시 전송 제한(예: subscription당 in-flight 1건)과 전역 배치 상한(env `JOBMATE_WEBHOOK_BATCH`, 기본 20)을 적용해 delivery 폭주가 API 응답성에 영향을 주지 않아야 한다.
- **FR-011**: 이벤트→delivery 생성은 `services/webhooks.emit_event(store, event_type, event_id, data)` 단일 진입점으로 하고, 구독 매칭·페이로드 렌더링·delivery insert를 한 트랜잭션 또는 멱등 키로 처리해 중복 이벤트가 중복 전송되지 않게 한다(`event_id` + `subscription_id` 유니크 제약 권장).

**헬스/상태**

- **FR-012**: `GET /api/v1/health/public`을 신규 공개 엔드포인트로 추가하고 보안 미들웨어의 인증 예외 목록(`/api/v1/health` 옆)에 등록해야 한다. 응답은 `{"status":"ok|degraded|down","version","uptime_s","components":{"api":"ok","db":"ok|down",...}}` 수준의 요약만 포함하고, DB 경로·DSN·호스트명·내부 IP·사용자/건수 세부는 포함하지 않는다.
- **FR-013**: `GET /api/v1/health/detail`(기존 admin 전용, `api/routers/core.py:74`)을 확장해 컴포넌트별 체크를 추가해야 한다: `db`(SELECT 1 + latency), `llm`(설정 존재 + 최근 `llm_usage` 기록 시각 또는 경량 reachability), `gmail`/`messenger`(어댑터 설정·토큰 만료 여부 — 원문 제외), `disk`(`shutil.disk_usage` 여유율), `backup`(마지막 성공 백업 경과 시간), `webhook`(dead/pending 깊이), `cron`(마지막 tick 경과 — 기존 `_last_cron_tick_at` 유지). 각 체크는 `{status, latency_ms, detail?, checked_at}` 형태다.
- **FR-014**: 각 의존성 체크는 개별 타임아웃(기본 2초, env `JOBMATE_HEALTH_CHECK_TIMEOUT`) 내 완료해야 하고 체크들은 병렬 실행되어 전체 응답 지연이 최대 타임아웃+α를 넘지 않아야 한다. 체크 예외는 해당 컴포넌트 `down`으로 격리하고 전체 엔드포인트는 200으로 응답한다(모니터링 경로 자체가 죽으면 안 됨).
- **FR-015**: 컴포넌트 상태가 `ok→degraded/down`으로 전이할 때 `health.degraded` 웹훅 이벤트 + 인박스 항목(`upsert_inbox_item`, dedupe_key에 컴포넌트+전이일) + 설정된 messenger 채널 알림을 발행해야 한다. 동일 상태 지속 중에는 재알림하지 않는다(approval fingerprint 패턴 재사용).
- **FR-016**: 헬스 상태 판정 규칙을 명시해야 한다 — 컴포넌트 `down` 1개 이상이면 전체 `degraded`, 핵심 컴포넌트(db 또는 api) `down`이면 `down`. degraded 임계(예: 백업 25시간 경과, 디스크 여유 15% 미만)는 env로 조정 가능해야 한다.

**백업 자동화**

- **FR-017**: `services/backup.py`를 신설해 백엔드별 백업을 구현해야 한다. SQLite는 `sqlite3.Connection.backup()` API(WAL 반영 보장), PostgreSQL은 `pg_dump` 서브프로세스(커스텀/표준 포맷, 실패 시 nonzero → failed 기록). 산출물은 `JOBMATE_BACKUP_DIR`(기본 `data/backups/`)에 `<backend>-<UTC타임스탬프>.<ext>`로 저장한다.
- **FR-018**: 백업 스케줄은 env `JOBMATE_BACKUP_INTERVAL_HOURS`(0=비활성)로 제어하고 `run_automation_cycle`에 `_maybe_run_backup` 잡을 추가해 automation loop와 `/api/v1/cron/tick` 양쪽 경로에서 동작해야 한다. 마지막 성공 시각은 `automation_state` 키로 관리한다.
- **FR-019**: 보존 정책은 `JOBMATE_BACKUP_RETENTION_DAYS`(기본 14)와/또는 `JOBMATE_BACKUP_KEEP_COUNT`(기본 10)로 관리하고, 삭제 시 파일 삭제 + `backup_runs`에 `pruned` 표시 또는 로그로 내역을 남긴다. DB에 없는 외부 백업 파일도 디렉터리 스캔으로 함께 정리한다.
- **FR-020**: `JOBMATE_BACKUP_ENCRYPTION=1`이면 산출물을 `security`의 Fernet 키로 암호화한다(예: `backup.jobmate.enc` 컨테이너에 salt/nonce 포함 또는 `enc:v1:` 규약 확장). 복호화에 필요한 키는 DB 백업과 별도 비밀 저장소 원칙(15번 가이드 §1)을 문서화하고, 암호화 백업의 복원 검증은 복호화→임시 복원 경로로 수행한다.
- **FR-021**: `backup_runs` 테이블에 `id`, `backend`, `file_path`(디렉터리 내 파일명만 — 절대경로·자격증명 금지), `size_bytes`, `status`(`running|success|failed|verify_failed|pruned`), `verify_status`(`pending|ok|failed`), `error`(유형/메시지 앞부분), `started_at`, `finished_at`, `verified_at`를 기록해야 한다.
- **FR-022**: 복원 검증 잡(`_verify_latest_backup`)은 주기적으로(기본 백업 성공 후 1회 + `JOBMATE_BACKUP_VERIFY_INTERVAL_HOURS`) 최근 성공 백업을 임시 디렉터리에 복원해 무결성을 검사한다 — SQLite: `PRAGMA integrity_check` + 핵심 테이블(`runs`, `audit_events`) 행 수 >0; PG: 복원 덤프의 스키마/카운트 스팟 체크. 결과를 `verify_status`/`verified_at`에 기록하고 실패 시 `backup.failed` 이벤트 + WARNING 로그.
- **FR-023**: 관리 API `GET /api/v1/backups`(목록), `POST /api/v1/backups/run`(수동 1회, admin), `GET /api/v1/backups/{id}/verify`(수동 검증 트리거, admin)를 제공하고, 생성/수동실행/검증은 `store.audit`(`backup_run`, `backup_verify`) + INFO 로그(파일명·크기·소요시간 — 내용/경로 원문 최소화)를 남긴다.
- **FR-024**: 백업 실패·검증 실패·staleness(마지막 성공 백업이 `JOBMATE_BACKUP_STALE_HOURS`, 기본 30시간 초과)는 헬스 `backup` 컴포넌트 degraded/down과 `backup.failed` 웹훅/인박스 알림으로 연결해야 한다.

**에러 추적**

- **FR-025**: `JOBMATE_SENTRY_DSN` env가 설정된 경우에만 Sentry를 초기화해야 한다(`sentry-sdk`는 optional extra 또는 지연 import — 미설치/미설정 시 앱 기동·테스트에 영향 없어야 한다). DSN 미설정 시 모든 캡처 경로는 no-op이다.
- **FR-026**: 캡처 지점은 최소 `app.py::_unhandled_exception` 핸들러와 `_automation_loop`의 루프 예외 경로, `run_automation_cycle` 내 잡 예외(기존 `watchdog_log.warning(..., exc_info=True)` 패턴 위치)다. 캡처 시 `request_id`(state.request_id), `path`, `method`, `workflow`/`run_id`(있는 경우)를 태그로 포함하되 사용자 이메일·본문·토큰은 제외한다.
- **FR-027**: `before_send`/`before_send_transaction` 스크러버가 Authorization·Cookie·세션 토큰·이메일 주소·요청 본문·민감 헤더를 제거해야 하고, `send_default_pii=False`를 강제한다. env `JOBMATE_SENTRY_TRACES_SAMPLE_RATE`(기본 0.0 — 에러만, 필요 시 트레이스 샘플링)와 `JOBMATE_SENTRY_ENVIRONMENT`(기본 `settings.mode`)를 지원한다.
- **FR-028**: Sentry 초기화/전송 실패는 `logger.warning` 1회 후 비활성화하고 앱 기동·요청 처리를 막지 않아야 한다(외부 의존성 장애 격리).

**메트릭**

- **FR-029**: `GET /api/v1/metrics`가 Prometheus text exposition format 0.0.4(`text/plain; version=0.0.4`)를 반환해야 한다. 신규 런타임 의존성 없이 `services/metrics.py`의 경량 레지스트리(counter/gauge/histogram)로 구현하거나 `prometheus-client` 추가는 계획 단계에서 결정한다 — 단, exposition 형식과 라벨 규칙은 본 스펙을 MUST로 한다.
- **FR-030**: 노출 메트릭 최소 세트: `jobmate_http_requests_total{method,route,status}`(카운터), `jobmate_http_request_duration_seconds{route}`(히스토그램, 버킷 상한), `jobmate_runs_total{workflow,status}`(카운터), `jobmate_runs_in_flight`(게이지), `jobmate_webhook_deliveries_total{event,status}`, `jobmate_webhook_pending`(게이지), `jobmate_llm_tokens_total{model,direction}`, `jobmate_llm_cost_usd_total{model}`, `jobmate_backup_last_success_timestamp`, `jobmate_health_status{component}`(0/1 또는 enum gauge), `jobmate_inbox_unread`(게이지).
- **FR-031**: 라벨 카디널리티 규칙(MUST): `user_id`/`email`/`run_id`/`request_id`/원시 `path`를 라벨 값으로 사용 금지. HTTP `route`는 FastAPI 라우트 템플릿(`request.scope["route"].path` 또는 등록 패턴)으로 정규화하고 매칭 실패 시 `"unmatched"`로 묶는다. `status`는 HTTP 상태 코드 버킷(2xx/4xx/5xx) 또는 고정 enum, `workflow`/`model`/`event`/`component`는 등록된 유한 집합으로 제한한다. 위반 라벨은 렌더링 단계에서 드롭한다.
- **FR-032**: `/api/v1/metrics` 인증 정책: 기본은 기존 인증(API 키/세션) 필요 — 보안 미들웨어의 `/api/*` 규칙을 그대로 적용한다. 스크래이퍼용으로 `JOBMATE_METRICS_TOKEN`(별도 bearer) 설정 시 해당 토큰도 허용한다. 두 경로 모두 `store.audit` 불필요(고빈도) 대신 접근 로그는 기존 미들웨어가 기록한다.
- **FR-033**: 메트릭 수집은 요청 핫패스에 무거운 store 쿼리를 추가하지 않아야 한다 — HTTP 카운터/히스토그램은 미들웨어에서 인메모리 갱신, DB 기반 메트릭(runs/webhook/backup/health)은 마지막 계산값을 TTL(기본 15초) 캐시로 제공한다.

**공통**

- **FR-034**: 모든 신규 비동기·외부 호출 경로(웹훅 전송, 백업 실행, pg_dump, 헬스 외부 체크, Sentry 전송)는 17번 가이드 부록 A 기준의 로그를 남겨야 한다 — 성공/실패·소요시간·지표(건수/크기/HTTP 상태), 실패는 WARNING 이상 + `logger.exception`으로 스택 보존. 토큰·시크릿·이메일 본문·webhook URL의 userinfo·push endpoint 원문·Sentry DSN 원문은 로깅 금지(호스트 추출은 `webutil.host_of` 사용).
- **FR-035**: 모든 신규 테이블(`webhook_subscriptions`, `webhook_deliveries`, `backup_runs`)은 `store/schema_sqlite.py`와 `store/schema_pg.py` 양쪽에 CREATE TABLE을 추가하고, `MigrationStoreMixin`에 기존 DB용 `CREATE TABLE IF NOT EXISTS` 보강을 추가해야 한다(SQLite/PostgreSQL 방언 차이 — `store/pg_dialect._pg_translate` 규칙 준수).
- **FR-036**: 신규 설정은 `Settings`(config.py)에 `JOBMATE_` 접두사 필드로 추가하고, 비밀값(API 토큰류)은 `repr=False` 또는 env 이름 참조 패턴을 따른다. 서버리스(Vercel)에서 동작하지 않는 상주 스레드 전제 기능(백업 대용량 I/O 등)은 cron tick 경로에서도 동일하게 동작하거나 명시적으로 비활성화한다.

### Key Entities

- **WebhookSubscription**: 고객 엔드포인트 구독 — url, 암호화된 secret, 이벤트 필터, enabled, 소유자/감사 정보
- **WebhookDelivery**: 이벤트 1건×구독 1건의 전송 시도 이력 — payload, 서명 대상 body, 상태 머신(pending→delivering→success/failed→dead/cancelled), 재시도 스케줄, HTTP 결과 메타
- **WebhookEvent**: 정규화된 도메인 이벤트 — type, event_id(멱등), occurred_at, data(식별자 수준)
- **BackupRun**: 백업 1회 실행 — backend, 파일명, 크기, 상태, 검증 상태, 소요시간, 오류
- **HealthReport**: 시점별 헬스 스냅샷 — 전체 status + 컴포넌트별 {status, latency_ms, detail}
- **MetricSeries**: 카운터/게이지/히스토그램 시계열 — 이름, 라벨(제한된 집합), 값, 타입
- **ErrorEvent**(Sentry): 캡처된 예외 — fingerprint, 태그(request_id/path/workflow), 스크러빙된 컨텍스트

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 구독 1건 + run completed 이벤트 1건으로 delivery가 생성되고, fake 수신기에서 서명이 `HMAC_SHA256(secret, "{t}.{body}")`와 일치하는 POST가 관측된다.
- **SC-002**: 수신기 500 응답 시 재시도 간격이 백오프 표를 따르고, 상한 후 `dead` 전이 + 수동 재큐 API로 복구된다. dead 상태 delivery의 자동 재시도는 0회다.
- **SC-003**: 사설 IP/루프백/비표준 포트 URL 구독 생성 요청이 100% 422로 거부되고 outbound 연결 시도가 없다.
- **SC-004**: `GET /api/v1/health/public`이 비인증 200을 반환하면서 DB 경로·호스트명·사용자 식별자를 포함하지 않는다(응답 스키마 화이트리스트 검증).
- **SC-005**: DB 다운을 모사한 체크에서 detail의 `db`가 `down`, 전체가 `degraded`/`down`이 되고 `health.degraded` 알림이 정확히 1회 발행된다(지속 상태 중 중복 0회).
- **SC-006**: SQLite 백엔드에서 백업 파일 생성→`backup_runs` success 기록→보존 정책 삭제→복원 검증 `verify_status=ok`까지 자동 사이클 1회로 완결된다.
- **SC-007**: `JOBMATE_SENTRY_DSN` 미설정 환경에서 기존 테스트 스위트가 회귀 없이 통과하고, 설정 환경에서 예외 1건이 스크러빙된 형태로 전송된다(transport fake로 검증).
- **SC-008**: `/api/v1/metrics` 출력이 Prometheus 파서로 유효하고, 임의 run_id/이메일이 어떤 라벨 값에도 나타나지 않는다(렌더링 결과 전수 스캔).
- **SC-009**: 모든 신규 로그 라인에 시크릿/토큰/이메일 본문/URL userinfo가 없음을 grep 수준 테스트로 검증한다.

## 현재 코드와의 관계 및 구현 힌트

- **인증/미들웨어**: `api/app.py:130-162` `security_middleware`의 `/api/*` 인증 예외 목록에 `/api/v1/health/public`을 추가해야 한다(현재 `/api/v1/health`만 공개, `/api/v1/health/detail`은 admin). `/metrics`를 `/api/` 밖으로 둘지는 FR-032 정책과 함께 결정 — `/api/v1/metrics` 권장(기존 인증 재사용).
- **헬스**: `api/routers/core.py:70-93` — `/api/v1/health`(상수 ok), `/api/v1/health/detail`(admin, db_info·bridge·cron·reaper). 확장은 이 라우터 또는 신규 `routers/ops_health.py`에. `store.db_info()`(`store/admin.py:436`)는 백엔드 종류/경로만 제공 — 의존성 실측은 신규 `services/health.py`에서.
- **이벤트 발생점**: run 종결은 `api/jobs.py::_execute_run_job`(status FAILED 마감 + `runner.invoke` 후), 승인 전이는 `api/appctx.py::_do_approve`와 `api/jobs.py::_check_waiting_approvals`(waiting_approval 전이 fingerprint 패턴 — `approval_wait_fp` automation_state 키, `jobs.py:507` 부근), 예산 차단은 `jobs.py::_process_due_schedules`의 `schedule_budget_exceeded`와 `api/routers/runs.py`의 예산 거부 경로. 공통 진입점 `services/webhooks.emit_event`를 이 지점들에서 호출.
- **웹훅 전송 패턴**: `messenger.py::_post_json`(pinned transport POST, timeout 10s)과 `webutil.validate_outbound_url`/`is_safe_fetch_url`/`_pinned_ip_for`를 그대로 재사용 — GET 전용 `fetch_bytes`와 달리 POST 경로는 이미 messenger에 존재하므로 `services/webhooks.py`에서 동일 패턴 구현. 수신 웹훅(인바운드)과 이름 충돌 주의 — 인바운드는 `/api/v1/webhooks/slack|telegram`(`routers/messaging.py:69,136`), 아웃바운드 API 경로는 `/api/v1/webhooks`(구독 관리)로 구분하거나 `/api/v1/webhook-subscriptions` 권장.
- **스케줄 잡**: `api/jobs.py::run_automation_cycle`에 `_process_webhook_deliveries`, `_maybe_run_backup`, `_verify_latest_backup`, `_run_health_checks`를 추가 — `defer`/`flush_jobs` 패턴과 `_maybe_*` 게이트(주기 env)를 따른다. 서버리스는 `/api/v1/cron/tick`이 동일 본체를 호출하므로 자동 호환.
- **스토어**: `store/db.py`는 믹스인 합성(`store/*.py`) — 신규 도메인은 `store/webhooks.py` + `store/backups.py` 믹스인 추가, `schema_sqlite.py`/`schema_pg.py` 양쪽 + `migrations.py`의 `_migrate_*` 패턴. `store.audit`(`store/runs.py:459`) 시그니처 `(run_id, actor, action, object_ref, detail)` 재사용.
- **암호화**: `security.encrypt_str`/`decrypt_str`(Fernet, `enc:v1:` 접두사) — webhook secret과 선택적 백업 암호화에 재사용. `enforce_production_secrets`가 프로덕션 키 부재를 이미 차단.
- **로깅 준수**: `docs/jobMate_v1.0.1/17_운영_보안_가이드_v1.0.1.md` 부록 A — 신규 외부 호출(웹훅 POST, pg_dump, 헬스 체크, Sentry)마다 성공/실패 로그, 실패는 WARNING+예외, 민감값 로그 금지. `webutil.host_of`로 URL은 호스트만 로깅.
- **Sentry**: `pyproject.toml`에 sentry-sdk 없음 — optional-dependencies(extra) 또는 지연 import로 추가. `configure_langsmith`(`config.py:152`)처럼 env 게이트 초기화 패턴을 `create_app` 초기에 추가.
- **메트릭**: prometheus_client 미설치 — 자체 레지스트리 권장(의존성 최소화, exposition 0.0.4는 텍스트 포맷이라 직접 렌더 가능). 접근 로그 미들웨어(`app.py:164-188`)에 route 템플릿 확보 로직 추가.
- **기존 알림 채널과의 관계**: `messenger.notify`(Slack/Telegram), 웹푸시(`engine/functions.send_web_push_text`), 인박스(`store.upsert_inbox_item`)는 유지 — 웹훅은 이들과 병렬인 고객향 채널이며, `health.degraded`/`backup.failed`는 기존 채널에도 발행해 이중 경로를 확보한다.

## Assumptions

- 웹훅/백업/헬스/메트릭 신기능은 단일 프로세스 + `run_automation_cycle` 주기 잡 전제로 설계한다. 멀티 인스턴스 배포 시 delivery 이중 전송은 `event_id`+`subscription_id` 유니크 제약과 `FOR UPDATE SKIP LOCKED` 상당의 claim 패턴으로 완화한다(현 스토어는 프로세스 내 RLock 직렬화 전제 — `store/db.py:188` 주석 참조).
- 아웃바운드 웹훅 URL은 고객이 관리하는 공인 HTTPS 엔드포인트를 전제로 하며, loopback은 `NODE_ENV`/테스트 환경에서만 `validate_outbound_url` 정책에 따라 허용된다.
- 백업 저장소는 서버 로컬 디렉터리(`JOBMATE_BACKUP_DIR`)를 1차로 하고, 오프사이트 업로드(S3 등)는 후속 스펙 — 암호화(FR-020)가 그 전제다.
- `pg_dump`는 PostgreSQL 클라이언트 도구가 설치된 운영 환경을 전제한다. 미설치 시 명확한 실패로 기록하고 대체 논리 덤프는 범위 외.
- Sentry는 선택 연동이며, 미설정 시 기능이 완전히 꺼진다. 자체 호스팅 에러 트래커 대체는 범위 외.
- Prometheus 외 모니터링 백엔드(PagerDuty, Datadog)는 `/metrics`와 웹훅 이벤트로 연동 가능하다고 가정하고 직접 연동은 범위 외.
- 헬스 공개 엔드포인트의 노출 범위는 "컴포넌트별 ok/degraded/down + 버전 + uptime"으로 제한하고, 컴포넌트 수·세부 latency는 공개 응답에서 제외한다(정확한 필드는 구현 시 화이트리스트로 고정).
- UI 노출(대시보드의 웹훅/백업 관리 화면)은 정적 대시보드(`api/static/index.html`) 또는 v2 React 앱의 후속 태스크로, 본 스펙은 API와 데이터 계약까지를 범위로 한다 — 단, "UI 데이터" 요구를 위해 목록/생성/전달 이력 조회 API가 UI가 소비할 수 있는 형태를 반환해야 한다.

## Out of Scope

- 웹훅 수신측 SDK/검증 라이브러리 제공, at-least-once 이상의 exactly-once 보장
- 외부 상태 페이지의 프론트엔드 구현(공개 API까지만)
- 오프사이트/오브젝트 스토리지 백업 업로드, 암호화 키의 외부 KMS 연동, PITR/WAL 아카이빙
- Sentry 외 자체 호스팅 에러 트래커, 크래시 리포트 수집
- OpenTelemetry/OTLP 트레이싱 표준 연동(LangSmith는 기존 유지)
- Grafana 대시보드/알림 규칙 JSON 제공, Prometheus Operator/ServiceMonitor 매니페스트
- 멀티 리전 백업, 크로스 리전 복제
- 웹훅 페이로드 커스터마이징(필드 선택/템플릿), 구독별 rate limit 고객 정의(전역 배치 상한만)
- Django 전환에 인증 체계 재편 — 본 스펙의 API 경로/역할 게이트는 현 FastAPI 제공층 기준
