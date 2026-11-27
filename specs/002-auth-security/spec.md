# Feature Specification: 인증·계정 보안 강화 (P0)

**Feature Branch**: `002-auth-security`

**Created**: 2026-11-28

**Status**: Draft

**Input**: 상용 다중 사용자 SaaS 수준의 인증·계정 보안 강화 — 비밀번호 재설정 플로우, MFA/TOTP(등록·로그인 2단계·백업 코드), 로그인 보안 정책(비밀번호 정책·유출 비밀번호 검사·세션 만료/동시 세션 제한·신규 기기 알림), 사용자/서비스별 스코프 API 키 관리(JOBMATE_API_KEY 단일 키 대체). 기존 세션 쿠키 인증·이메일 인증 인프라(users/auth_sessions/email_verifications, mailer, Fernet 암호화)를 재사용한다.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - 비밀번호 재설정 플로우 (Priority: P1)

비밀번호를 잊은 사용자가 로그인 화면의 "비밀번호 찾기"에서 이메일을 입력하면, 재설정 링크가 메일로 발송된다. 링크를 열어 새 비밀번호를 설정하면 재설정이 완료되고, 모든 기존 세션이 폐기되어 재로그인이 필요하다. 토큰은 해시만 저장되고 1회만 사용 가능하다.

**Why this priority**: 비밀번호 분실은 SaaS 필수 기능이며, 이메일 인증 토큰 인프라(`email_verifications`, `create_email_verification`, `consume_email_verification`)를 그대로 재사용할 수 있어 독립 배포 가능한 최소 단위다. 다른 보안 기능 없이도 단독으로 가치가 있다.

**Independent Test**: TestClient + fake mailer로 forgot→메일 링크→reset→구 세션 무효화까지 HTTP로 완결 검증 가능하다.

**Acceptance Scenarios**:

1. **Given** 가입된 비밀번호 계정, **When** `POST /api/v1/auth/password/forgot`에 이메일 제출, **Then** 200과 `{"sent": true}`가 반환되고 재설정 링크가 해당 이메일로 발송된다. 미가입 이메일에도 동일한 응답 형태를 반환한다(계정 열거 방지).
2. **Given** 발송된 재설정 링크의 토큰, **When** `POST /api/v1/auth/password/reset`에 `{token, new_password}` 제출, **Then** 비밀번호 해시가 갱신되고 `auth_password_reset` 감사 이벤트가 기록되며 기존 세션이 전부 폐기된다.
3. **Given** 이미 사용되거나 만료된 토큰, **When** reset 호출, **Then** 400/410 계열 오류와 `auth_password_reset_failed` 감사 이벤트가 남고 비밀번호는 변경되지 않는다.
4. **Given** 재설정 완료 후 구 세션 쿠키, **When** 인증 필요 API 호출, **Then** 401이 반환된다(세션 전부 revoke).
5. **Given** 같은 이메일로 재설정 메일 반복 요청, **When** 10분 이내 재요청, **Then** 발송은 스킵되지만 응답은 첫 요청과 동일한 200 `{"sent": true}`다(429 반환 시 상태 코드 차이로 계정 존재가 노출되므로 금지). 메일 발송 실패 시 토큰 행이 삭제되어 재시도가 throttle에 막히지 않는다.

---

### User Story 2 - MFA/TOTP 2단계 인증 (Priority: P1)

사용자가 설정 화면에서 TOTP(MFA)를 등록한다: 인증 앱(Google Authenticator 등)에 등록할 수 있는 otpauth URI/QR을 발급받고, 확인 코드 1회 입력으로 활성화한다. 활성화 후 로그인 시 비밀번호 검증을 통과하면 세션 대신 짧은 수명의 MFA 챌린지가 발급되고, TOTP 코드 또는 백업 코드로 2단계를 완료해야 세션이 발급된다. 백업 코드는 1회용이며 재발급 가능하다.

**Why this priority**: 계정 탈취 방지의 핵심 통제다. 로그인 플로우에 챌린지 단계를 추가하는 것이므로 P1이지만, 재설정 플로우와 독립적으로 배포·검증할 수 있다.

**Independent Test**: RFC 6238 TOTP를 결정적으로 생성하는 헬퍼(고정 시각)로 등록→로그인 챌린지→코드 검증→세션 발급 전 과정을 fake 시간/코드로 검증한다.

**Acceptance Scenarios**:

1. **Given** 로그인된 사용자, **When** `POST /api/v1/auth/mfa/setup` 호출, **Then** otpauth URI와 비밀키가 반환되고 비밀키는 `enc:v1:` 접두사로 암호화되어 DB에 저장된다(평문 저장 금지). 아직 활성화되지 않는다.
2. **Given** 등록된 pending 비밀키, **When** `POST /api/v1/auth/mfa/confirm`에 올바른 6자리 코드 제출, **Then** MFA가 활성화되고 백업 코드 N개(기본 10개)가 발급되며 각 코드는 SHA-256 해시로만 저장된다. 틀린 코드는 활성화되지 않고 실패 감사가 남는다.
3. **Given** MFA 활성화 계정, **When** `POST /api/v1/auth/login`에 올바른 이메일/비밀번호 제출, **Then** 세션 쿠키 없이 `{"mfa_required": true, "mfa_ticket": "..."}`가 반환된다.
4. **Given** 유효한 mfa_ticket, **When** `POST /api/v1/auth/mfa/verify`에 TOTP 코드 제출, **Then** 세션 쿠키가 발급되고 `auth_login_mfa` 감사 이벤트가 기록된다.
5. **Given** 백업 코드 사용, **When** `mfa/verify`에 미사용 백업 코드 제출, **Then** 해당 코드가 소진 처리되고 세션이 발급된다. 이미 사용된 코드는 거부된다.
6. **Given** 만료되었거나 존재하지 않는 mfa_ticket/코드, **When** `mfa/verify` 호출, **Then** 실패하고 세션은 발급되지 않는다(무차별 대입 방지로 코드 실패 횟수 제한 적용).
7. **Given** MFA 비활성 계정, **When** 로그인, **Then** 기존과 동일하게 즉시 세션이 발급된다(MFA는 opt-in).

---

### User Story 3 - 로그인 보안 정책 강화 (Priority: P2)

관리자/사용자가 비밀번호 정책(최소 길이·유출 비밀번호 차단·이메일 유사 비밀번호 금지)을 적용받고, 세션은 유휴 만료·동시 세션 수 제한을 가진다. 새 기기/IP에서 로그인하면 경고 메일과 감사 로그가 남는다. 비밀번호 변경·재설정·MFA 변경 시 모든 세션이 폐기된다.

**Why this priority**: 정책 집행은 중요하지만 기존 최소 정책(8자) 위의 점진적 강화이며, US1/US2 완료 후 적용해도 독립 검증이 가능하다.

**Independent Test**: 정책 함수 단위 테스트(약한 비밀번호 거부 목록) + 세션 상한 초과 시나리오 + 로그인 알림 fake mailer 검증.

**Acceptance Scenarios**:

1. **Given** 비밀번호 정책 활성, **When** signup/password/reset/password change에 8자 미만·일반 비밀번호(내장 denylist)·이메일 로컬파트 포함 비밀번호 제출, **Then** 400 `weak_password`가 반환되고 거부 사유가 감사에 남는다.
2. **Given** `JOBMATE_PWNED_CHECK=1` 설정, **When** 유출된 비밀번호(해당 SHA-1 접두사로 HIBP k-anonymity 조회)에 매칭되는 비밀번호 제출, **Then** 가입/변경이 거부된다. 네트워크 실패 시 설정된 fail-open/fail-closed 정책을 따른다.
3. **Given** 사용자당 동시 세션 상한(기본 10) 초과, **When** 새 로그인, **Then** 가장 오래된 세션부터 revoke되고 `auth_session_evicted` 감사가 남는다.
4. **Given** 마지막 활동 후 유휴 만료(기본 7일, `JOBMATE_SESSION_IDLE_DAYS`) 경과, **When** 세션으로 API 호출, **Then** 세션이 무효화되고 재로그인이 필요하다.
5. **Given** 이전 로그인과 다른 IP/기기(User-Agent 해시)에서 로그인 성공, **When** 로그인 완료, **Then** `auth_login_new_device` 감사 이벤트와 알림 메일이 발송된다. 동일 지문 재로그인은 알림하지 않는다.
6. **Given** 비밀번호 재설정 또는 MFA 활성화/비활성화 완료, **When** 처리, **Then** 해당 사용자의 모든 세션이 폐기된다.

---

### User Story 4 - 스코프드 API 키 관리 (Priority: P2)

관리자가 사용자별/서비스별 API 키를 발급·조회·폐기·만료시킨다. 각 키는 이름·스코프(runs:read, runs:write, admin 등)·만료일을 가지며, 원문 키는 발급 시 1회만 표시되고 DB에는 SHA-256 해시만 저장된다. 기존 `JOBMATE_API_KEY` env 키는 하위 호환으로 전체 권한("*")을 유지한다.

**Why this priority**: 단일 env 키는 유출 시 전체 권한이므로 상용 필수이나, 세션/MFA 정책과 독립 배포 가능하고 기존 `authorized()` 경로만 확장하면 된다.

**Independent Test**: 키 발급→해시 저장 확인→스코프별 엔드포인트 허용/거부(TestClient)→폐기 후 401 검증.

**Acceptance Scenarios**:

1. **Given** admin 세션, **When** `POST /api/v1/auth/api-keys`에 `{name, scopes, expires_in_days}` 제출, **Then** 원문 키(1회 표시)와 메타데이터가 반환되고 DB에는 `key_hash`(SHA-256)와 `key_prefix`(앞 8자)만 저장된다. 응답 이후 원문은 재조회 불가다.
2. **Given** `runs:read` 스코프만 가진 키, **When** 읽기 엔드포인트 호출, **Then** 허용되고 쓰기/admin 엔드포인트는 403이다. `*` 또는 `admin` 스코프 키는 기존 admin 동작과 동일하다.
3. **Given** 폐기(`revoked_at`)되었거나 만료된 키, **When** API 호출, **Then** 401이다. 폐기 시 `auth_api_key_revoked` 감사가 남는다.
4. **Given** 일반 member 세션, **When** 타인 소유 키를 삭제 시도, **Then** 404/403이다. 자기 소유 키 관리는 본인 세션으로 가능하다.
5. **Given** `JOBMATE_API_KEY` env 키로 호출, **When** 기존 엔드포인트 호출, **Then** 기존과 동일하게 전체 권한으로 동작한다(하위 호환).

---

### Edge Cases

- 재설정 토큰 재사용·동시 사용 — `consume` 원자 UPDATE(`used_at IS NULL AND expires_at>?`)로 단 1회만 성공; 두 번째 요청은 실패.
- 재설정 요청과 로그인 경쟁 — reset 완료 시 `revoke_user_sessions`로 모든 세션 폐기되므로 구 세션 탈취가 무효화된다.
- 미가입 이메일로 forgot 요청 — 계정 열거 방지를 위해 가입 이메일과 동일한 응답/동일 처리 시간(더미 토큰 발급 없이 응답만 동일화)을 유지한다. 단 실제 발송은 가입 계정에만.
- MFA ticket 탈취·재사용 — ticket은 단기(5분)·1회성 consume, 사용 후 즉시 무효화. TOTP는 ±1 윈도우(30초×3)까지만 허용해 시간 드리프트를 수용하되 재사용된 동일 코드는 거부한다.
- MFA 비활성화 시도 — 현재 비밀번호 또는 유효 TOTP 코드 재확인을 요구해 세션 탈취만으로 MFA를 끄지 못하게 한다.
- 백업 코드 전량 소진 — 재발급 시 기존 미사용 코드 전부 무효화하고 새 세트 발급, 세션은 유지하되 `auth_mfa_backup_regen` 감사 기록.
- API 키 스코프 상승 — 키로 키 관리 엔드포인트 호출 시 `api_keys:manage` 스코프 필요; member 세션은 자기 키의 조회·폐기만 가능(발급 불가). 스코프 문자열은 서버 allowlist로 검증해 임의 값 저장을 거부한다. 권한 상승 스코프(`*`, `admin`, `api_keys:manage`)는 admin 세션 또는 env 키(`JOBMATE_API_KEY`)만 발급 가능 — `api_keys:manage` 키는 리소스 스코프만 발급할 수 있고 발급 키는 부모 키 소유자를 계승한다. 경로 스코프 매칭은 첫 세그먼트 전체 기준이며 하이픈 세그먼트(`agent-sessions` 등)는 `-`→`_` 정규화 후 매칭한다.
- 세션 고정(session fixation) — 로그인/재설정/MFA verify 성공 시 항상 `new_session_token()`으로 새 토큰 발급(기존 `_set_session` 계약 유지), 성공 전후 쿠키 값이 달라야 한다.
- Google 로그인 계정 — Google이 이메일 소유권·2FA를 증명하므로 jobMate TOTP는 비밀번호 계정 로그인에만 적용한다. Google-only 계정(`password_hash` 빈 값)의 비밀번호 재설정은 불가 — `password_reset`은 password_hash 존재 계정만 대상.
- 유휴 만료와 장기 세션 충돌 — `last_seen_at` 갱신은 세션당 최소 간격(예: 5분)으로 쓰기 빈도를 제한한다.
- HIBP 조회 지연 — 외부 호출은 타임아웃(5초) + 실패 시 정책(env)대로 fail-open/fail-closed. 호출 자체는 앱 로그에 기록하되 비밀번호 원문은 절대 전송하지 않는다(SHA-1 k-anonymity 접두사만).

## Requirements *(mandatory)*

### Functional Requirements

**비밀번호 재설정 (US1)**

- **FR-001**: `POST /api/v1/auth/password/forgot`은 `{email}`을 받아 가입된 비밀번호 계정(`password_hash` 비어 있지 않음)에만 재설정 메일을 발송해야 하며, 미가입/Google-only/미인증 이메일에도 계정 열거가 불가능한 동일 응답 형태를 반환해야 한다(MUST). `_remote_open_mode(request)`가 True면 403으로 차단해야 한다.
- **FR-002**: 재설정 토큰은 `secrets.token_urlsafe(32)`로 생성하고 DB에는 SHA-256 해시(`hash_token`)만 저장해야 한다(MUST). `password_reset_tokens` 테이블은 `id, user_id, email, token_hash, expires_at, used_at, created_at` 컬럼을 가지며 `email_verifications`와 동일한 인덱스 패턴(`idx_password_reset_tokens_email`, `idx_password_reset_tokens_hash`)을 따른다. TTL은 30분이다.
- **FR-003**: `POST /api/v1/auth/password/reset`은 `{token, new_password}`를 받아 원자적 consume(`UPDATE ... WHERE token_hash=? AND used_at IS NULL AND expires_at>?`) 성공 시에만 비밀번호를 갱신해야 한다(MUST). 성공 시 `store.revoke_user_sessions(user_id)`로 모든 세션을 폐기하고 `auth_password_reset` 감사 이벤트를 기록해야 한다. 실패는 `auth_password_reset_failed` 감사와 400 계열 응답이다.
- **FR-004**: 같은 이메일의 재설정 요청은 `email_verifications`의 재발송 패턴을 따라 최소 간격(기본 10분, `_VERIFY_RESEND_MIN_INTERVAL`과 동일 값)을 강제하고 `auth_password_reset_throttled` 감사 이벤트를 남겨야 한다(MUST). 재발송 억제 시에도 응답은 `{"sent": true}`로 동일해야 하며(계정 열거 방지), 메일 발송은 `BackgroundTasks`로 응답 이후에 수행해 응답 시간을 균일하게 유지해야 한다. 메일 발송 실패 시 토큰 행을 삭제해 재시도가 throttle에 막히지 않게 해야 한다. 신규 발급 시 해당 이메일의 기존 미사용 토큰은 무효화한다.
- **FR-005**: 재설정 완료 화면은 `auth_verify`의 `_VERIFY_OK_HTML`/`_VERIFY_FAIL_HTML` 패턴을 재사용한 HTML 응답 또는 JSON 응답 중 하나로 제공하되, 링크 클릭 후 새 비밀번호 입력 폼(프론트엔드 라우트)으로 유도해야 한다(MUST). 재설정 링크 URL은 `_verify_url`과 동일하게 `settings.public_base_url` → `JOBMATE_PUBLIC_HOSTS` 검증(`_public_host_allowed`)을 거쳐 생성해야 한다.
- **FR-006**: 재설정 메일 발송은 `mailer.send_verification_email`과 동일한 SMTP→Gmail API 폴백 구조의 `send_password_reset_email(to, reset_url, gmail)` 함수를 `mailer.py`에 추가해야 하며, 토큰 원문은 로그에 남기지 않고 `_log_safe_url` 마스킹을 적용해야 한다(MUST).

**MFA/TOTP (US2)**

- **FR-007**: TOTP는 RFC 6238(HMAC-SHA1, 30초 스텝, 6자리)을 구현해야 한다. 신규 의존성 없이 `security.py`에 `totp_secret()`/`totp_code(secret, at=None)`/`totp_verify(secret, code, window=1)` 헬퍼를 두거나 `pyotp` 의존성을 추가할 수 있으나, 코드베이스의 stdlib 우선 원칙(mailer.py 주석)을 고려해 stdlib 구현을 권장한다. 검증 허용 윈도우는 ±1 스텝이다.
- **FR-008**: `POST /api/v1/auth/mfa/setup`은 로그인된 세션 사용자에게 pending TOTP 비밀키를 발급한다. 비밀키는 `encrypt_str`(Fernet, `enc:v1:` 접두사)로 암호화해 `mfa_secrets` 테이블(`user_id, secret_enc, status(pending|active|disabled), created_at, confirmed_at`)에 저장해야 하며, 평문 비밀키를 DB·로그에 저장/기록해서는 안 된다(MUST). 응답에는 otpauth URI(`otpauth://totp/jobMate:{email}?secret=...&issuer=jobMate`)와 비밀키를 1회 반환한다.
- **FR-009**: `POST /api/v1/auth/mfa/confirm`은 `{code}`를 받아 pending 비밀키의 TOTP와 `hmac.compare_digest` 수준의 안전한 비교로 검증하고, 성공 시 `status=active`로 전이하며 백업 코드를 발급해야 한다(MUST). 실패는 상태를 바꾸지 않고 `auth_mfa_confirm_failed` 감사 이벤트를 남긴다.
- **FR-010**: 백업 코드는 기본 10개, `secrets.token_hex(5)` 형태의 URL-safe 문자열로 생성하고 `mfa_backup_codes` 테이블(`user_id, code_hash, used_at, created_at`)에 SHA-256 해시만 저장해야 한다(MUST). `code_hash`에는 사용자별 salt 또는 `hash_token`과 동등한 단방향 해시를 사용해 DB 유출 시 코드 복구를 불가능하게 한다.
- **FR-011**: `POST /api/v1/auth/login`은 `password_hash`가 있는 계정의 비밀번호 검증 통과 후 `mfa_secrets.status='active'`이면 세션을 발급하지 않고 `{"mfa_required": true, "mfa_ticket": "..."}`를 반환해야 한다(MUST). mfa_ticket은 `secrets.token_urlsafe(32)`로 생성해 해시만 저장하고 TTL 5분, 1회성 consume를 적용한다(전용 테이블 `mfa_tickets` 또는 `password_reset_tokens`와 동형의 일반 토큰 테이블 재사용 — 구현 시 결정하되 스키마 이중 정의 SQLite/PG 모두 반영).
- **FR-012**: `POST /api/v1/auth/mfa/verify`는 `{mfa_ticket, code}`를 받아 ticket consume → TOTP(±1 윈도우) 또는 미사용 백업 코드 검증 → 성공 시 `_set_session`으로 세션 발급 + `auth_login_mfa` 감사 기록을 해야 한다(MUST). 동일 TOTP 코드 재사용·만료 ticket·잘못된 코드 연속 실패는 거부하며, 코드 검증 실패는 IP+ticket 기준 rate limit(예: 5회/5분)을 적용한다.
- **FR-013**: `GET /api/v1/auth/mfa/status`는 현재 사용자의 MFA 활성 여부·백업 코드 잔여 수를 반환해야 한다. `POST /api/v1/auth/mfa/disable`은 `{password}` 또는 `{code}` 재확인을 요구하고 성공 시 `status=disabled` + 모든 백업 코드 무효화 + `auth_mfa_disabled` 감사 기록을 해야 한다(MUST). `POST /api/v1/auth/mfa/backup-codes`는 재인증 후 새 백업 코드 세트를 발급하고 기존 미사용 코드를 전부 무효화해야 한다.
- **FR-014**: Google 로그인(`auth_google_callback`)은 Google의 인증에 위임하므로 jobMate TOTP를 요구하지 않는다(MUST). 단 MFA 활성 비밀번호 계정이 Google sub를 연결하는 경로(`auth_google_claim_unverified` 등 기존 pre-hijacking 방어)에서는 기존 정책을 변경하지 않는다.

**로그인 보안 정책 (US3)**

- **FR-015**: 비밀번호 정책을 `security.py`의 `validate_password(password, email) -> str | None`(거부 사유 코드 반환)로 중앙화해야 한다(MUST). 최소 8자(기존 계약 유지, `JOBMATE_PASSWORD_MIN_LEN`으로 상향 가능), 내장 denylist(상위 약한 비밀번호 목록 — 코드베이스 내 정적 목록), 이메일 로컬파트/전체 포함 금지, 숫자만/연속 문자 등 단순 패턴 금지를 검사한다. signup·password/reset·password change 3개 경로 모두 이 함수를 호출해야 한다.
- **FR-016**: 유출 비밀번호 검사는 Have I Been Pwned k-anonymity API(SHA-1 앞 5자리만 전송)를 선택적 기능으로 구현한다(MUST, `JOBMATE_PWNED_CHECK=1`일 때만 활성). 타임아웃 5초, 네트워크 실패 시 `JOBMATE_PWNED_FAIL_MODE=open|closed`(기본 open + 경고 로그)로 동작한다. 비밀번호 원문·전체 해시는 전송·로그 금지.
- **FR-017**: 세션 정책 — `auth_sessions`에 `last_seen_at` 컬럼을 추가하고(마이그레이션 포함) 유휴 만료 `JOBMATE_SESSION_IDLE_DAYS`(기본 7일, 0=비활성)를 `get_session_user`에서 검사해야 한다(MUST). 동시 세션 상한 `JOBMATE_MAX_SESSIONS`(기본 10)을 `_set_session` 성공 시점에 적용해 초과분을 오래된 순으로 revoke하고 `auth_session_evicted` 감사 이벤트를 남겨야 한다. `last_seen_at` 갱신은 세션당 최소 5분 간격으로 제한해 쓰기를 줄인다.
- **FR-018**: 신규 기기 알림 — 로그인 성공 시 요청의 `client.host`와 User-Agent 해시(sha256 앞 16자)를 `users.last_login_ip`, `users.last_login_fingerprint`, `users.last_login_at` 컬럼과 비교해, 첫 로그인 또는 지문 변경 시 `auth_login_new_device` 감사 이벤트 + 메일 알림(`send_login_alert_email`)을 발송해야 한다(MUST). 알림에는 시각·IP·UA 요약만 포함하고 세션 토큰·비밀번호는 포함하지 않는다.
- **FR-019**: 보안 민감 작업(비밀번호 변경·재설정·MFA 활성/비활성/백업 재발급·계정 삭제)의 완료 시 해당 사용자의 모든 세션을 폐기해야 한다(MUST). 기존 `auth_change_password`의 `revoke_user_sessions` 패턴을 재사용한다.

**스코프드 API 키 (US4)**

- **FR-020**: `api_keys` 테이블(`id, name, owner_user_id, key_hash, key_prefix, scopes_json, expires_at, revoked_at, last_used_at, created_at`)을 SQLite/PG 스키마 이중 정의와 `migrations.py`에 추가해야 한다(MUST). `key_hash`는 SHA-256(`hash_token` 재사용), `key_prefix`는 원문 앞 8자만 저장한다.
- **FR-021**: 키 관리 엔드포인트 — `POST /api/v1/auth/api-keys`(발급), `GET /api/v1/auth/api-keys`(목록, 원문 제외), `DELETE /api/v1/auth/api-keys/{id}`(폐기), 선택적 `POST /api/v1/auth/api-keys/{id}/revoke` — 를 제공해야 한다(MUST). 발급 응답에만 원문 키를 1회 포함하고 목록/조회 응답에는 `key_prefix`·name·scopes·만료·last_used만 반환한다.
- **FR-022**: 스코프 모델 — `*`(전체, admin 상당), `admin`, `runs:read`, `runs:write`, `sessions:read` 등 allowlist된 스코프 문자열을 `scopes_json`에 저장하고, 임의 스코프는 422로 거부해야 한다(MUST). 키 인증 시 `authorized()`가 env 키(`JOBMATE_API_KEY`) 매칭을 먼저 시도하고, 실패 시 `api_keys.key_hash` 조회로 유효(미폐기·미만료) 키를 해석해 `request.state.api_scopes`에 스코프 집합을 주입해야 한다. `JOBMATE_API_KEY`는 스코프 `*`의 레거시 키로 계속 동작한다(하위 호환).
- **FR-023**: 쓰기/admin 엔드포인트는 기존 미들웨어 인증(`authorized`) 통과 후 스코프 검사를 추가해야 한다. 최소한 `runs:read`/`runs:write` 구분을 `POST /api/v1/runs` 등 핵심 엔드포인트에 적용하고, 나머지 엔드포인트는 스코프 미지정 키에 대해 deny-by-default 또는 기존 동작 유지 여부를 구현 계획에서 명시해야 한다(MUST — deny-by-default 권장).
- **FR-024**: API 키 관리 자체는 admin 세션 또는 `api_keys:manage` 스코프 키만 허용하고, member 세션은 자기 소유(`owner_user_id`) 키의 조회·폐기만 가능해야 한다(MUST) — member의 키 발급 요청은 403. `*`/`admin`/`api_keys:manage` 스코프 발급은 admin 세션 또는 env 키로 제한하고(MUST), `api_keys:manage` 키가 발급한 키는 부모 키의 `owner_user_id`를 계승해야 한다. 발급 rate limit(사용자당 분당 10회)을 적용한다.

**공통 보안 요구사항**

- **FR-025**: 모든 신규 엔드포인트는 `_remote_open_mode(request)` 게이트를 기존 auth 라우트와 동일하게 적용해야 한다(MUST) — 무키 원격 오픈 모드에서 세션/MFA/API 키 발급 경로가 열리지 않게 한다.
- **FR-026**: 토큰·비밀번호·TOTP 비밀키·백업 코드 원문은 로그·audit detail·응답(재조회)·이메일 본문 외 어디에도 평문 저장/기록하지 않아야 한다(MUST). 이메일에는 토큰이 포함된 링크가 필요하므로 발송은 허용하되 `_log_safe_url` 마스킹을 적용한다.
- **FR-027**: 모든 토큰 비교는 `hmac.compare_digest` 또는 DB 해시 조회(타이밍이 해시 존재 여부만 노출)로 수행해야 한다(MUST). 로그인 실패·MFA 실패·재설정 실패 등은 `auth_*` 접두사 감사 이벤트로 `store.audit`에 기록하고, 성공 이벤트와 실패 이벤트를 구분한다.
- **FR-028**: 신규 엔드포인트(forgot/reset/mfa/*/api-keys)의 남용 방지 rate limit은 기존 `_rate_hit`/`_login_fails`/`_signup_attempts` 인메모리 패턴을 재사용해 IP·이메일·사용자 키 단위로 적용해야 한다(MUST). 프로세스/인스턴스 단위 한계는 기존과 동일하게 문서화한다.
- **FR-029**: TOTP 비밀키는 반드시 `encrypt_str`로 암호화 저장하고(MUST), `JOBMATE_ENCRYPTION_KEY` 미설정 로컬 모드에서는 `encrypt_str`의 기존 평문 폴백 동작을 따른다(프로덕션은 `enforce_production_secrets`가 이미 강제).
- **FR-030**: 스키마 변경은 `schema_sqlite.py`와 `schema_pg.py`에 이중 정의하고 `migrations.py`의 `MigrationStoreMixin`에 기존 DB용 ALTER/CREATE 보강을 추가해야 한다(MUST) — 신규 테이블은 양쪽 스키마에, 신규 컬럼(`auth_sessions.last_seen_at`, `users.last_login_*`, `users.mfa_*` 필요 시)은 마이그레이션에 반영한다.

### Key Entities

- **PasswordResetToken**: user_id·email·token_hash·expires_at·used_at — 1회용 단기 토큰(`email_verifications`와 동형).
- **MfaSecret**: user_id·secret_enc(Fernet 암호문)·status(pending|active|disabled)·confirmed_at — 계정당 1개.
- **MfaBackupCode**: user_id·code_hash·used_at — 세트 단위 재발급.
- **MfaTicket**: 로그인 2단계 챌린지 토큰 해시·user_id·expires_at·used_at — 5분 1회용.
- **ApiKey**: name·owner_user_id·key_hash·key_prefix·scopes_json·expires_at·revoked_at·last_used_at — 원문 비저장.
- **LoginSecurityState**(users 컬럼 확장): last_login_ip·last_login_fingerprint·last_login_at — 신규 기기 판정용.
- **Session**(auth_sessions 확장): last_seen_at — 유휴 만료 판정용.

## 현재 코드와의 관계/재사용 포인트

| 기능 | 재사용 대상 | 위치 |
|---|---|---|
| 비밀번호 재설정 | `email_verifications` 테이블 패턴 + `create_email_verification`/`consume_email_verification`의 해시 저장·원자 consume·재발송 무효화 로직을 `password_reset_tokens`로 미러 | `store/users.py:148-241`, `store/schema_sqlite.py:233-242` |
| 재설정 메일 | `mailer.send_verification_email`의 SMTP→Gmail 폴백·`_log_safe_url` 마스킹, `_send_verification`/`_verify_url`/`_public_host_allowed`의 Host 검증 | `mailer.py:71-141`, `api/appctx.py:991-1043` |
| 세션 폐기 | `revoke_user_sessions`, `revoke_session`, `_set_session`(httponly·lax·secure 쿠키), `hash_token`, `new_session_token` | `store/users.py:95-146`, `appctx.py:917-928`, `security.py:289-298` |
| 로그인 throttle | `_login_key`/`_login_throttled`/`_login_record`(IP+email 10회/10분), `_dummy_password_hash` 타이밍 오라클 방지, `auth_login_failed` 감사 패턴 | `appctx.py:864-957`, `routers/auth.py:129-175` |
| MFA TOTP | `encrypt_str`/`decrypt_str`(Fernet `enc:v1:`), `hmac.compare_digest`, `hashlib.scrypt` 패턴 참고 | `security.py:122-159, 258-286` |
| 백업 코드/토큰 해시 | `hash_token`(SHA-256) — 세션·인증 토큰과 동일한 원문 비저장 원칙 | `security.py:296-298` |
| API 키 인증 | `authorized()`의 Bearer/X-API-Key 해석 + `hmac.compare_digest` + `/api/v1/stream`의 `?token=` 단기 토큰 패턴(레거시 `?api_key=` 폐기 전례 참고) | `security.py:167-245` |
| 권한 판정 | `_effective_role`(member/lead/admin), `_admin_user`/`_lead_user` 게이트, `email_allowed`/`allowed_emails` | `appctx.py:205-307`, `security.py:301-322` |
| 감사 이벤트 | `store.audit(run_id="", actor=email, action="auth_*", object_ref, detail)` — detail에 비밀값 금지 주석 계약 준수 | `store/runs.py:459-481` |
| 원격 모드 게이트 | `_remote_open_mode(request)` — 모든 신규 인증 발급 경로 첫 줄 적용 | `appctx.py:239-247` |
| 스키마 이중 정의 | `schema_sqlite.py`/`schema_pg.py` 동시 추가 + `MigrationStoreMixin` 보강 + `pg_dialect._pg_translate` 확인 | `store/schema_*.py`, `store/migrations.py` |
| 테스트 | `tests/unit/test_auth.py`의 TestClient+임시 Store 패턴, `test_email_verification.py`, `test_oauth.py`, `test_security_audit_fixes.py`의 env 패치·감사 검증 관례 | `tests/unit/` |

## 구현 힌트 (파일 영향 범위)

- **`src/jobmate/security.py`** — `validate_password`, TOTP 헬퍼(`totp_secret`/`totp_code`/`totp_verify`), API 키 해시/스코프 헬퍼 추가. `authorized()`는 env 키 실패 시 DB 조회 폴백이 필요하므로 시그니처 확장 또는 `authorized_scoped(request, store)` 신설 검토.
- **`src/jobmate/store/users.py`** 또는 신규 **`store/security.py` mixin** — `password_reset_tokens`, `mfa_secrets`, `mfa_backup_codes`, `mfa_tickets`, `api_keys` CRUD + `auth_sessions.last_seen_at` 갱신/유휴 검사 + 동시 세션 상한 revoke. `Store` 믹스인 조합(`db.py`)에 등록.
- **`src/jobmate/store/schema_sqlite.py` / `schema_pg.py` / `migrations.py`** — 신규 테이블 4종 + 컬럼 추가(users.last_login_*, auth_sessions.last_seen_at). PG 방언(`?`→`%s`, `ON CONFLICT` 등)은 `pg_dialect.py` 변환 규칙 확인.
- **`src/jobmate/mailer.py`** — `send_password_reset_email`, `send_login_alert_email`, MFA 관련 안내 메일 템플릿 추가(기존 `_verification_body` 패턴).
- **`src/jobmate/api/routers/auth.py`** — 기존 라우터 확장(추천): forgot/reset/mfa/*/api-keys 엔드포인트 추가. 파일이 커지면 `routers/auth_security.py`로 분리하고 `app.py`에서 함께 register.
- **`src/jobmate/api/appctx.py`** — `_send_password_reset`, `_public_reset_url`, `_mfa_ticket_issue/consume`, rate limit 버킷(`_reset_attempts`, `_mfa_fails`, `_apikey_attempts`), 로그인 성공 후 신규 기기 판정·알림 호출 추가.
- **`src/jobmate/api/app.py`** — 신규 라우터 등록(분리 시), 미들웨어 인증 예외 목록(`/api/v1/auth/*`는 기존과 동일하게 면제되므로 내부에서 개별 인증 처리).
- **`src/jobmate/api/static/index.html`** — 비밀번호 찾기/재설정 폼, MFA 설정 UI, API 키 관리 UI(단일 파일 대시보드 패턴 유지).
- **`tests/unit/test_auth_security.py`**(신규) — US별 독립 테스트. TOTP는 고정 시각 코드 생성으로 결정적 검증.
- **`docs/jobMate_v1.0.1/17_운영_보안_가이드_v1.0.1.md`** — 새 보안 기능의 운영 로그·env(JOBMATE_PWNED_CHECK, JOBMATE_SESSION_IDLE_DAYS, JOBMATE_MAX_SESSIONS 등) 문서 갱신.
- 코드 수정 후 `graphify update .` 실행.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 비밀번호 재설정 완료 후 해당 사용자의 기존 `auth_sessions` 행이 100% `revoked_at` 처리되고, 구 세션 쿠키로의 API 호출이 전부 401이다.
- **SC-002**: 재설정 토큰의 재사용·만료 토큰 사용 시도 100% 실패하고, 같은 토큰으로 성공한 reset은 정확히 1회뿐이다(동시 2요청 레이스 포함).
- **SC-003**: MFA 활성 계정의 로그인은 비밀번호만으로는 세션 쿠키가 발급되지 않고(응답에 `jobmate_session` 없음), TOTP 또는 미사용 백업 코드 검증 후에만 세션이 발급된다.
- **SC-004**: `mfa_secrets.secret_enc`가 `enc:v1:` 접두사 암호문이며 평문 TOTP 비밀키·백업 코드 원문이 DB 어떤 컬럼에도 저장되지 않는다(테스트로 스키마 전수 검사).
- **SC-005**: MFA 비활성 사용자의 로그인 플로우는 기존과 동일하게 1회 요청으로 세션이 발급된다(회귀 0건).
- **SC-006**: 동시 세션 상한 초과 시 초과분 세션이 revoke되고 `auth_session_evicted` 감사 이벤트가 남는다; 유휴 만료 경과 세션은 `get_session_user`가 None을 반환한다.
- **SC-007**: 다른 IP/UA 지문으로의 로그인 성공 시 `auth_login_new_device` 감사 + 알림 메일이 정확히 1회 발송되고, 동일 지문 재로그인에서는 발송되지 않는다.
- **SC-008**: API 키 원문은 발급 응답에만 존재하고 DB(`api_keys`)에는 `key_hash`+`key_prefix`만 저장된다. `runs:read` 전용 키로 쓰기 엔드포인트 호출 시 403, 폐기/만료 키는 401이다.
- **SC-009**: `JOBMATE_API_KEY` env 키만 설정된 기존 배포에서 모든 기존 테스트(`tests/unit/test_auth.py`, `test_oauth.py` 등)가 회귀 없이 통과한다.
- **SC-010**: 모든 신규 엔드포인트가 `_remote_open_mode`에서 403을 반환하고, 토큰/비밀번호/TOTP 원문이 로그·audit detail에 나타나지 않는다(로그 캡처 테스트).

## Assumptions

- TOTP 구현은 stdlib(hmac/hashlib/base64)로 RFC 6238을 직접 구현하는 것을 기본으로 한다 — `pyotp` 도입은 plan 단계에서 결정. "pyotp-style"은 UX(인증 앱 등록·6자리 코드·백업 코드)를 의미한다.
- mfa_ticket 저장은 신규 `mfa_tickets` 테이블 또는 기존 토큰 테이블의 purpose 확장 중 구현 시 선택 — 스펙은 5분 TTL·1회성 consume만 고정한다.
- 비밀번호 최소 길이는 기존 8자를 유지한다(상향은 `JOBMATE_PASSWORD_MIN_LEN` env로 선택 가능). denylist는 저장소 내 정적 목록으로 시작하고 외부 유출 DB 동기화는 범위 외.
- HIBP k-anonymity 검사는 외부 HTTPS 호출이므로 `JOBMATE_PWNED_CHECK` opt-in으로 두고, 미설정 시 기존 동작과 동일하다.
- API 키 스코프의 초기 집합은 `*`, `admin`, `api_keys:manage`, `runs:read`, `runs:write`로 시작하고, 세분화는 후속 확장한다.
- 알림 메일은 기존 SMTP/Gmail 백엔드를 재사용하며, 미설정 환경에서는 `send_verification_email`과 동일하게 로컬 로그 폴백(마스킹 적용)한다.
- MFA는 비밀번호 로그인에만 적용하고 Google OAuth 로그인은 Google의 2FA에 위임한다.
- `auth_sessions`에 `last_seen_at`을 추가하되, 기존 행은 NULL 허용 후 첫 접근 시 현재 시각으로 채운다.

## Out of Scope

- WebAuthn/FIDO2·passkey, SMS 인증, 이메일 OTP 로그인
- 조직/팀 단위 RBAC 세분화(기존 member/lead/admin role 체계 유지), SSO/SAML
- 로그인 이력 조회 UI의 고도화(새 기기 알림·감사 이벤트까지만)
- API 키의 IP allowlist·사용량 쿼터·자동 회전
- breached password의 주기적 재검사(변경 시점 검사만), Have I Been Pwned 외 유출 소스
- 세션의 "모든 기기 로그아웃" 외 원격 세션 관리(기존 sessions 목록/개별 폐기 유지)
- 프론트엔드 디자인 시스템 재구성 — 기존 `index.html` 패턴 내 최소 UI 추가만
