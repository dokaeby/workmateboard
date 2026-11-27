# Feature Specification: jobMate 제품 경험(P1) — 상용 멀티에이전트 관리 대시보드

**Feature Branch**: `006-product-experience`

**Created**: 2026-11-19

**Status**: Draft

**Input**: 상용급 멀티에이전트 관리 대시보드를 위한 제품 경험(P1) 기능 묶음 — ① 워크플로·플레이북 템플릿 마켓플레이스(보내기/가져오기·공유 링크·카탈로그), ② 실행 분석(run 간 diff·실패 지점 재실행·코멘트 연계 인사이트), ③ 알림 규칙 엔진(조건→채널 라우팅), ④ 예산 자동화(임계 도달 시 알림+일시중지/승인 요구), ⑤ 대시보드 커스터마이징, ⑥ API 문서 정책, ⑦ i18n(한/영), ⑧ 온보딩 위저드.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - 알림 규칙 엔진 (Priority: P1)

운영자가 "run이 failed로 끝나면", "특정 워크플로의 주간 실패율이 30%를 넘으면", "비용이 X를 넘으면" 같은 조건을 규칙으로 등록하고, 알림을 inbox·이메일·Slack·Telegram·Web Push 채널로 라우팅한다. 규칙은 CRUD로 관리되고, 동일 조건의 반복 발사는 dedup/cooldown으로 억제된다.

**Why this priority**: 상용 대시보드의 핵심 가치 — "에이전트가 문제 생겼을 때 사람이 모른다"는 최대 페인 포인트를 직접 해결한다. 기존 `inbox_items`(인앱 알림)·`messenger.py`(Slack/Telegram)·`mailer.py`(SMTP)·`push_subscriptions`+`sw.js`(Web Push) 채널이 모두 존재하므로, 신규 작업은 조건 평가·규칙 저장·라우팅 계층에 집중된다. 예산 자동화(US2)와 평가 지점(post-run hook)을 공유한다.

**Independent Test**: fake run 종료 이벤트와 규칙 1건을 만들어 채널 발송 호출을 mock으로 검증한다. run 엔진 없이 규칙 평가기 단위 테스트로도 완결 가능하다.

**Acceptance Scenarios**:

1. **Given** 조건 `run.status == "failed"` + 채널 `[inbox, slack]`인 활성 규칙, **When** run이 failed로 종료, **Then** inbox_items 1건과 Slack 발송 1회가 발생하고 규칙 발사 이력이 기록된다.
2. **Given** 동일 규칙이 cooldown(기본 1시간) 내에 같은 조건으로 다시 평가됨, **When** 두 번째 발사 시도, **Then** 채널 발송 없이 `suppressed_cooldown`으로 기록된다.
3. **Given** 채널 미설정 환경(Slack webhook/env 없음), **When** 규칙 평가, **Then** inbox 채널만 발송되고 미설정 채널은 `skipped_unconfigured`로 기록된다(규칙 전체 실패 아님).
4. **Given** 규칙 `disabled`, **When** 조건 충족 이벤트 발생, **Then** 어떤 채널로도 발송되지 않는다.

---

### User Story 2 - 예산 자동화 (임계 액션) (Priority: P1)

기존 budget_policies의 warn/hard 임계 평가(`budget_scope.evaluate_admission`)에 "임계 도달 시 액션" 개념을 추가한다. 정책별로 `alert`(기존 인박스+규칙 채널 발송) / `pause`(해당 스코프 신규 run 생성 차단 — 현 hard_stop 동작) / `require_approval`(run 생성은 되지만 실행 전 리드 승인 대기) 중 하나를 선택한다. `budget_incidents` 해결 흐름(keep_paused / raise_budget_and_resume)과 연동된다.

**Why this priority**: 예산 인프라(microusd 원장·스코프 정책·인시던트·인박스 알림)가 이미 완성돼 있어 증분 가치가 크고, "비용 폭주"는 상용 서비스의 신뢰 문제다. US1의 규칙 엔진과 post-run/생성 시점 평가를 공유해 함께 설계하면 중복이 없다.

**Independent Test**: `evaluate_admission`에 액션 파라미터를 추가해 정책 임계 초과 시나리오를 DB fixture로 재현한다. run 실행 없이 admission 단계만 검증 가능하다.

**Acceptance Scenarios**:

1. **Given** workflow 스코프 정책 `threshold_action=alert`, warn_percent=80, **When** 월 지출이 80% 도달, **Then** budget_incident + inbox 항목 + (US1 규칙이 연결됐다면) 설정 채널 발송이 발생하고 신규 run은 계속 생성된다.
2. **Given** 정책 `threshold_action=pause`, `hard_stop_enabled`, **When** 지출+예약이 한도 도달, **Then** `POST /api/v1/runs`가 429 `budget_exceeded`로 거부되고 인시던트가 open 상태로 남는다.
3. **Given** 정책 `threshold_action=require_approval`, **When** 임계 초과 상태에서 run 생성, **Then** run이 `waiting_approval`(예산 승인 사유)로 생성되고 승인 전 `execute_run`이 호출되지 않는다. 리드 승인 시 실행, 거절/만료 시 `rejected`/`cancelled`.
4. **Given** open 인시던트가 있는 스코프, **When** `POST /api/v1/budget-incidents/{id}/resolve`에 `raise_budget_and_resume`, **Then** 새 한도가 관측 지출보다 커야 하고, require_approval로 대기 중이던 run은 승인 없이 재개된다(또는 재심사 후 진행).

---

### User Story 3 - 템플릿 마켓플레이스 (보내기/가져오기·공유 링크·카탈로그) (Priority: P2)

팀 리드가 커스텀 워크플로(`custom_workflows`+`workflow_revisions`)와 플레이북을 버전 있는 JSON 번들로보내고, 다른 사용자가 파일/공유 링크로 가져와 검증 후 자신의 인스턴스에 등록한다. 카탈로그 UI에서 사용 가능한 템플릿을 브라우징한다.

**Why this priority**: 워크플로 빌더·커스텀 워크플로 저장소·리비전 CAS가 이미 있어 "템플릿"의 데이터 모델은 존재한다. 마켓플레이스는 그 위의 배포/재사용 계층이며, 단독으로도 "내보내기→가져오기" 왕복은 완결된 가치다. 외부 공개 카탈로그(크로스 인스턴스)는 범위 밖.

**Independent Test**: export→import 왕복을 API로만 검증한다(워크플로 spec이 동일하게 복원되고 run 실행 가능). UI 없이 JSON 계약만으로 테스트 가능하다.

**Acceptance Scenarios**:

1. **Given** 저장된 커스텀 워크플로와 플레이북, **When** `POST /api/v1/templates/export`, **Then** `format_version`, `kind`(workflow|playbook|bundle), 스키마 검증을 통과한 페이로드가 반환된다.
2. **Given** export JSON, **When** `POST /api/v1/templates/import`, **Then** WorkflowSpec/플레이북 스키마 검증 통과 시 새 리비전으로 등록되고, 실패 시 422와 함께 어떤 필드가 문제인지 반환된다(부분 등록 없음).
3. **Given** 공유 링크 생성, **When** 토큰 없는 사용자가 `GET /api/v1/templates/shared/{token}`, **Then** 만료 전까지 페이로드를 조회할 수 있고 만료 후 410이다. 토큰은 `share_links` 테이블 재사용, kind=`template`.
4. **Given** import 페이로드에 `api_key_env` 외 평문 시크릿 패턴(`sk-`, `-----BEGIN`, webhook URL 자격증명 등)이 포함, **When** import, **Then** 거부되고 스캔된 패턴 위치가 보고된다.

---

### User Story 4 - 실행 분석 (diff 비교·retry-from-stage·코멘트 인사이트) (Priority: P2)

실패한 run을 열어 성공한 이전 run과 단계별 출력/diff를 비교하고, 실패한 단계부터 재실행한다. run 코멘트에 남긴 맥락("이 요구사항은 이렇게 해석")이 인사이트/리포트에 반영된다.

**Why this priority**: 디버깅 생산성 기능. `stage_runs`에 단계별 입출력이 이미 영속되고 `GET /runs/{id}/diff`가 implement 산출물을 집계하므로 비교는 읽기 조합이고, retry-from-stage는 실행 재개 경로(`_do_approve`의 resume 패턴)를 재사용할 수 있다. 상용 체감 가치는 높지만 P1 알림/예산보다 후순위.

**Independent Test**: stage_runs fixture 2개로 diff 엔드포인트를 검증하고, 실패 run의 특정 단계부터 재실행되는지 stage_run 시퀀스로 확인한다.

**Acceptance Scenarios**:

1. **Given** 같은 workflow의 run A(성공)와 run B(실패), **When** `GET /api/v1/runs/compare?a=&b=`, **Then** 단계별 status·출력 요약·diff 파일 목록의 비교 뷰가 반환된다.
2. **Given** run B가 `review` 단계에서 failed, **When** `POST /api/v1/runs/{id}/retry?from_stage=review`, **Then** review 이후 단계만 pending으로 재개되고 이전 succeeded 단계는 재실행되지 않는다(멱등 — 외부 쓰기 중복 없음).
3. **Given** retry 대상 run이 running 또는 waiting_approval, **When** retry 요청, **Then** 409 `invalid_state`.
4. **Given** run에 코멘트가 달린 상태, **When** 인사이트 요약 생성, **Then** 코멘트가 요약 입력에 포함되고 코멘트가 없는 run과 결과가 구분된다.

---

### User Story 5 - 대시보드 커스터마이징 + 저장 뷰 (Priority: P2)

사용자가 대시보드 카드(기존 `dashboard_cards` 웹훅 위젯 + 지표 카드)의 순서·숨김·추가를 조정하고, 레이아웃 설정을 "뷰"로 저장해 전환한다. 설정은 사용자별로 저장된다.

**Why this priority**: `prefs.dashboard_widgets`(id/visible/order)·`prefs.sidebar_menu`·`dashboard_cards` 테이블이 이미 존재해 확장만 하면 된다. 단, 현재 prefs는 전역(admin) 설정이라 "사용자별" 요구는 저장소 분리가 필요 — 범위 조정 여지가 있는 P2.

**Independent Test**: prefs PATCH로 카드 순서 변경 → GET 반영을 API로 검증. 뷰 저장/전환도 API 왕복으로 검증 가능.

**Acceptance Scenarios**:

1. **Given** 카드 목록, **When** `PATCH /api/v1/me/dashboard`로 `{card_id: {order, visible}}` 저장, **Then** 재조회 시 순서/숨김이 유지되고 알 수 없는 card_id는 422다.
2. **Given** 저장된 뷰 "경영진용", **When** `POST /api/v1/me/views` + `POST /api/v1/me/views/{id}/apply`, **Then** 해당 레이아웃이 대시보드에 적용되고 기본 뷰는 삭제 불가(또는 삭제 시 기본으로 폴백)다.
3. **Given** member 권한 사용자, **When** 본인 뷰 수정, **Then** 다른 사용자의 뷰는 변경되지 않는다(사용자 격리).

---

### User Story 6 - 온보딩 위저드 (Priority: P2)

첫 로그인(또는 온보딩 미완료 사용자)에게 단계별 가이드가 표시된다: ① 계정/Google 연동 확인 → ② 커넥터·채널 연결 상태 안내 → ③ 첫 워크플로 실행(샘플 태스크) → ④ 알림 채널 설정. 각 단계는 실제 API 상태(연결 여부·run 존재 여부)와 연동된 체크리스트이며, 건너뛰기/나중에가 가능하고 진행 상태는 사용자별로 저장된다.

**Why this priority**: 상용 SaaS의 활성화 지표(activation)에 직결. Google OAuth(`/api/v1/auth/google`)·커넥터·run 생성 API가 이미 있어 체크리스트 항목을 실제 상태에 묶을 수 있다. 완전 자동 강제는 아니고 dismissible 가이드다.

**Independent Test**: 온보딩 상태 API(`GET/POST /api/v1/me/onboarding`)를 fixture로 검증 — checklist 항목이 실제 store 상태를 반영하는지 확인.

**Acceptance Scenarios**:

1. **Given** 신규 가입 사용자(Google 로그인 완료, run 0건), **When** 대시보드 접속, **Then** 위저드가 표시되고 "첫 워크플로 실행" 단계가 미완료로 표시된다.
2. **Given** 사용자가 샘플 run을 생성해 completed, **When** 위저드 재조회, **Then** 해당 체크가 자동 완료된다(수동 체크 불필요).
3. **Given** 위저드를 "나중에"로 닫은 사용자, **When** 재접속, **Then** 자동으로 다시 강제 표시되지 않고 설정에서 재오픈 가능하다.

---

### User Story 7 - API 문서 프로덕션 노출 정책 (Priority: P3)

`/docs`·`/openapi.json`의 노출 정책을 명시한다: 프로덕션에서는 인증된 사용자만 열람 가능(또는 비활성), API 키 발급/관리 문서와 함께 "어떤 엔드포인트가 기계 인증 가능한지"가 문서화된다.

**Why this priority**: 보안/운영 위생 항목. 기능 개발 없이 설정·문서 작업이 대부분이며, 다른 스토리보다 체감 우선순위가 낮다.

**Independent Test**: `JOBMATE_ENV=production`에서 `/openapi.json` 접근이 401/404인지 확인하는 통합 테스트.

**Acceptance Scenarios**:

1. **Given** 프로덕션 환경, **When** 인증 없이 `GET /openapi.json`, **Then** 401 또는 404(정책에 따라)이고 스키마가 노출되지 않는다.
2. **Given** 개발 환경, **When** `GET /docs`, **Then** Swagger UI가 정상 렌더링된다.
3. **Given** API 키 보유 개발자, **When** 문서에서 `Authorization: Bearer <key>` 사용법 확인, **Then** 기계 호출 가능 엔드포인트와 세션 전용 엔드포인트가 구분돼 있다.

---

### User Story 8 - i18n 리소스 분리 완성 (Priority: P3)

v2 React 앱의 기능별 `i18n.ts`(ko/en) 체계를 유지하면서, 남아 있는 하드코딩 문자열을 리소스로 추출해 ko/en 커버리지를 완료한다. 레거시 `index.html`의 `data-jm-i18n` 패턴과 v2 `STR`/feature i18n 패턴을 단일 전략으로 정리한다.

**Why this priority**: i18n 골격(LangToggle, STR, feature i18n.ts, data-jm-i18n)은 이미 존재 — 이번 작업은 "완성"이지 "신규 도입"이 아니다. 전면 재작성 없이 점진 추출로 충분하며, 상용화 필수지만 P1/P2 기능보다 후순위.

**Independent Test**: `en` 로케일로 전환 시 주요 뷰(사이드바·실행 이력·알림·설정)에 한국어 잔여 문자열이 없는지 스냅샷/DOM 검사로 검증한다.

**Acceptance Scenarios**:

1. **Given** `en` 로케일, **When** 실행 이력·알림·설정 뷰 렌더링, **Then** 모든 사용자 노출 문자열이 영어 리소스에서 온다(하드코딩 한국어 0건 — 검증 스크립트 기준).
2. **Given** 신규 기능 뷰 추가, **When** 문자열 작성, **Then** 해당 feature `i18n.ts`에 ko/en 키가 함께 등록된다(CI 체크 또는 리뷰 체크리스트).
3. **Given** 레거시 `/legacy` 경로, **When** 언어 전환, **Then** `data-jm-i18n` 속성 기반 치환이 동작한다(레거시 유지 기간 동안 회귀 없음).

---

### Edge Cases

- **알림 규칙**: 규칙 평가 중 채널 발송이 타임아웃/실패 → 해당 채널만 실패 기록, 나머지 채널은 계속 발송. 규칙 평가 자체가 run 종료를 블로킹하면 안 됨(post-run 비동기). 동일 이벤트에 여러 규칙 매칭 → 규칙별 독립 발송, dedup은 (rule_id, event_fp) 단위.
- **예산 require_approval**: 승인 대기 run이 예산 윈도우(월) 경계를 넘김 → 승인 시점에 `evaluate_admission` 재실행, 여전히 초과면 재대기 또는 거절. 대기 run 무기한 누적 방지 TTL(예: 72h) 필요.
- **마켓플레이스**: import 시 name 충돌 → `-imported` 접미사 또는 409 선택 정책. export한 워크플로가 builtin 이름과 충돌 → export 시 경고, import 시 builtin 이름은 거부(기존 `_BUILTIN_WF_KEYS` 검사 재사용). share token이 workflow spec에 시크릿 포함 → export 시 스캔 후 경고/마스킹.
- **retry-from-stage**: 재실행 중 원본 run의 외부 부수효과(Calendar 등록 등)가 이미 발생 → 멱등 마커/기존 `_do_approve` 재개 경로와 동일한 중복 방지 규칙 적용. failed 단계가 function(비-LLM) 단계면 재실행 가능 여부를 단계 kind별로 정의.
- **대시보드 뷰**: 삭제된 카드 id가 뷰에 남아있음 → 렌더 시 무시+경고, 저장 시 정리. 뷰 이름 중복 → 허용하되 기본 뷰(`default`) 이름은 예약.
- **온보딩**: 사용자가 이미 run/커넥터가 있는 상태로 위저드 표시 조건 충족 → 완료 항목은 자동 체크. 관리자 계정은 온보딩 스킵 옵션.
- **i18n**: 누락 키 → ko 폴백(또는 키 자체 표시 + 빌드 경고). 날짜/숫자 포맷은 로케일 연동(`Intl`).

## Requirements *(mandatory)*

### Functional Requirements

**알림 규칙 엔진**

- **FR-001**: 시스템은 `alert_rules` 테이블에 규칙을 저장해야 한다 — 필드: id, name, enabled, condition(JSON), channels(JSON 배열), cooldown_minutes, last_fired_at, created_by, created_at.
- **FR-002**: condition은 구조화된 `{field, op, value}` 형태여야 하며, 지원 field는 최소 `run.status`, `run.workflow`, `run.project`, `run.cost_usd`, `run.duration_ms`, `stage.status`, `budget.used_pct`, `schedule.missed` 이다. op는 `eq|ne|gt|gte|lt|lte|in|contains`.
- **FR-003**: 규칙 평가는 run 종료·stage 실패·budget 임계·스케줄 미스 등 이벤트 발생 시점의 post-run hook에서 비동기로 수행해야 하며, run 실행 경로를 블로킹하지 않아야 한다.
- **FR-004**: 채널은 `inbox`(기존 `upsert_inbox_item`), `email`(`mailer.py`), `slack`/`telegram`(`messenger.py`), `push`(`push_subscriptions`)를 지원해야 하며, 미설정 채널은 `skipped_unconfigured`로 기록한다.
- **FR-005**: dedup/cooldown은 (rule_id, event_fingerprint) 단위로 `alert_rule_fires` 테이블에 기록하고, cooldown 내 재발사는 채널 호출 없이 suppressed로 남겨야 한다.
- **FR-006**: 규칙 CRUD는 `GET/POST /api/v1/alert-rules`, `PATCH/DELETE /api/v1/alert-rules/{id}`로 제공하고, 생성/수정/삭제는 `_audit_api`로 기록해야 한다. member는 조회만, lead 이상이 쓰기.

**예산 자동화**

- **FR-007**: `budget_policies`에 `threshold_action` 컬럼을 추가해야 한다 — 값: `alert`(기본, 기존 동작+알림 채널) | `pause`(hard_stop 차단) | `require_approval`. 기존 행은 `alert`로 마이그레이션.
- **FR-008**: `require_approval` 정책에서 임계 초과 시 run 생성은 허용하되 `waiting_approval` 상태로 두고, 승인(`/api/v1/runs/{id}/approve` 확장 또는 전용 엔드포인트) 전까지 `execute_run`을 호출하지 않아야 한다.
- **FR-009**: require_approval 대기 run은 TTL(기본 72h, 설정 가능) 초과 시 자동 `cancelled` 처리하고, 예산 윈도우 리셋 시 재심사해야 한다.
- **FR-010**: `POST /api/v1/budgets/policies/{scope_type}/{scope_id}` 요청 스키마에 `threshold_action` 필드를 추가하고, `GET /api/v1/budgets/overview` 응답에 현재 임계 상태와 적용된 action을 포함해야 한다.
- **FR-011**: 임계 도달 알림은 US1 규칙 엔진의 `budget.threshold` 이벤트로 발행되어 등록된 채널로 라우팅되어야 한다(기존 inbox upsert와 병행).

**마켓플레이스**

- **FR-012**: `POST /api/v1/templates/export`는 `{kind: workflow|playbook|bundle, ref_id}`를 받아 `{format_version: "1.0", kind, name, spec|playbook, exported_at, checksum}` 형태의 JSON을 반환해야 한다. workflow는 최신 `workflow_revisions` spec을 포함한다.
- **FR-013**: export 페이로드는 `WorkflowSpec.model_validate`를 통과해야 하며, `api_key_env`는 환경변수 "이름"만 포함하고 값은 절대 포함하지 않는다. 평문 시크릿 패턴 스캔(`sk-`, `AKIA`, `-----BEGIN`, `https://hooks.slack.com/services/` 등)에서 발견 시 경고 필드를 포함하되 export 자체는 허용(사용자 판단), import 시에는 거부한다.
- **FR-014**: `POST /api/v1/templates/import`는 JSON 본문을 받아 `format_version` 호환성 검사 → 스키마 검증 → 이름 충돌 처리(`{name}-imported` 또는 409) → 등록 순서(workflow 먼저, playbook 후 참조 해석)로 처리해야 한다. 실패 시 부분 등록 없이 전체 롤백.
- **FR-015**: 공유 링크는 기존 `share_links` 테이블을 재사용하되 `kind='template'`으로 생성하고, `expires_at`은 기본 30일(최대 90일)로 제한한다. `GET /api/v1/templates/shared/{token}`은 만료/미존재 시 410/404.
- **FR-016**: 카탈로그 UI는 `GET /api/v1/templates/catalog`로 현재 인스턴스에 등록 가능한 내장+커스텀 템플릿 목록을 반환해야 한다(마켓플레이스는 로컬 카탈로그 — 외부 디렉터리 연동은 범위 외).

**실행 분석**

- **FR-017**: `GET /api/v1/runs/compare?run_a=&run_b=`는 두 run의 단계별 status·입출력 요약·diff 파일 목록·소요 시간을 비교한 뷰를 반환해야 한다.
- **FR-018**: `POST /api/v1/runs/{id}/retry?from_stage={key}`는 지정 단계 이후를 pending으로 되돌리고 해당 단계부터 재실행해야 한다. running/waiting_approval 상태의 run은 409. 재실행은 새 stage_run 레코드를 생성하고 이전 시도는 이력으로 보존한다.
- **FR-019**: retry 시 외부 부수효과 단계(function kind 중 Calendar 쓰기 등)는 기존 멱등 마커 규칙을 따르고, 이미 완료된 succeeded 단계는 재실행하지 않는다.
- **FR-020**: run 코멘트(`run_comments`)는 인사이트/리포트 생성 시 입력으로 포함될 수 있어야 하며, 코멘트가 run의 분류/요약에 영향을 주는 경우 그 사실이 인사이트 메타에 표시된다.

**대시보드 커스터마이징**

- **FR-021**: 사용자별 대시보드 설정은 `user_dashboard_prefs`(또는 동등한 per-user 저장소)에 저장해야 한다 — card_id별 order/visible + 저장된 뷰 목록. 전역 `prefs.dashboard_widgets`는 하위 호환 읽기로 유지한다.
- **FR-022**: `GET/PATCH /api/v1/me/dashboard`, `GET/POST/PATCH/DELETE /api/v1/me/views`, `POST /api/v1/me/views/{id}/apply` 엔드포인트를 제공한다. 뷰 이름은 사용자당 고유, 기본 뷰 `default`는 삭제 불가.
- **FR-023**: 카드 순서/숨김 변경은 즉시 반영되고, 다른 사용자의 설정과 격리된다.

**API 문서**

- **FR-024**: `JOBMATE_ENV=production`(또는 동등 플래그)에서 `/docs`, `/redoc`, `/openapi.json`은 인증된 admin만 접근 가능하거나 비활성화해야 한다. 개발 환경에서는 기존대로 공개.
- **FR-025**: API 키 발급·폐기·스코프 문서가 `/api/v1/me/api-keys`(또는 기존 키 관리 경로)와 함께 제공되어야 한다 — 문서에 "어떤 엔드포인트가 세션 전용인지"가 명시된다.

**i18n**

- **FR-026**: v2 앱의 모든 사용자 노출 문자열은 feature별 `i18n.ts`(ko/en)에 키로 등록되어야 하며, 누락 키는 ko로 폴백하고 빌드/테스트에서 경고한다.
- **FR-027**: 레거시 `index.html`은 `data-jm-i18n` 속성 기반 치환을 유지하고, 신규 문자열 추가 시 동일 패턴을 따른다. 레거시 파일 전면 재작성은 하지 않는다.
- **FR-028**: 언어 선택은 사용자별로 저장(`users.locale` 또는 prefs)되어 재접속 시 유지된다.

**온보딩**

- **FR-029**: `GET /api/v1/me/onboarding`은 체크리스트 항목(계정 연동, 커넥터, 첫 run, 알림 채널)과 각 항목의 완료 여부를 실제 store 상태 기반으로 반환해야 한다.
- **FR-030**: `POST /api/v1/me/onboarding`은 `{dismissed: bool, completed_at}`를 저장하고, `GET` 응답에 `show_wizard`(미완료 && !dismissed)를 포함한다.
- **FR-031**: 온보딩 단계는 실제 API 상태와 연동된다 — 예: "첫 워크플로 실행"은 runs 테이블에 해당 사용자의 run이 1건 이상 있을 때 완료.

### Key Entities

- **AlertRule**: id, name, enabled, condition{field,op,value}, channels[], cooldown_minutes, last_fired_at
- **AlertRuleFire**: rule_id, event_fingerprint, fired_at, channels_result{channel: sent|skipped|failed}
- **BudgetPolicy**(기존 확장): + threshold_action(alert|pause|require_approval), + approval_ttl_hours
- **BudgetIncident**(기존): open 상태 + resolve action과 require_approval run 재개 연동
- **TemplateBundle**: format_version, kind, name, spec/playbook, exported_at, checksum
- **ShareLink**(기존 확장): kind에 `template` 추가, scope/expiry 정책
- **DashboardView**: user_id, name, layout(JSON: card_id→{order,visible}), is_default
- **OnboardingState**: user_id, dismissed, completed_at, checklist_snapshots
- **RunRetry**: run_id, from_stage, retry_of(stage_run_id), started_at

## 현재 코드와의 관계

| 기능 | 기존 기반 | 신규 작업 |
|---|---|---|
| 알림 규칙 | `inbox_items`+`upsert_inbox_item`(dedupe_key), `messenger.py`(Slack/Telegram), `mailer.py`(SMTP), `push_subscriptions`+`/api/v1/push/*`, `prefs.notify_rules`(세션 어텐션 필터 — 별개), `triggers` 테이블(match/action — 세션 상태 트리거, 알림 규칙과 다름) | `alert_rules`/`alert_rule_fires` 테이블, 조건 평가기, post-run hook, CRUD 라우터 |
| 예산 자동화 | `budget_scope.evaluate_admission`(warn/hard 인시던트+inbox), `budget_incidents`+resolve, `budget_policies`(warn_percent/hard_stop_enabled/unpriced_usage_policy/window_kind/reservation_microusd), `POST /runs`의 429 경로 | `threshold_action` 컬럼+마이그레이션, require_approval run 상태 전이, TTL 정리 잡 |
| 마켓플레이스 | `custom_workflows`+`workflow_revisions`(CAS), `WorkflowSpec` pydantic 검증, `share_links` 테이블+`/api/v1/share`, `_BUILTIN_WF_KEYS`, `playbooks` 테이블 | export/import 스키마(format_version), 시크릿 스캔, 카탈로그 엔드포인트, share kind=`template` |
| 실행 분석 | `stage_runs`(단계별 입출력·상태), `GET /runs/{id}/diff`, `run_comments`, `_do_approve` 재개 경로, `wakeups` | compare 엔드포인트, retry-from-stage 실행 경로, 인사이트 입력 확장 |
| 대시보드 | `dashboard_cards`(웹훅 위젯), `prefs.dashboard_widgets`/`sidebar_menu`, `PATCH /api/v1/settings` | per-user 저장소 분리, 뷰 CRUD |
| API 문서 | FastAPI 자동 `/docs`·`/openapi.json`, `security.py` API 키 인증 | 프로덕션 노출 게이트, 키 관리 문서 |
| i18n | v2 `app/i18n.ts`+feature별 `i18n.ts`+LangToggle, legacy `data-jm-i18n` | 커버리지 완성, 누락 키 검출, users.locale |
| 온보딩 | `/api/v1/auth/google` OAuth, users/auth_sessions, connectors, runs | onboarding 상태 저장, 체크리스트 API, 위저드 UI |

## 구현 힌트

- **알림 규칙 평가 지점**: `jobs.py`의 `run_automation_cycle` 내 run 종료 처리 이후와 `_execute_run_job` 완료 경로에 post-run hook을 두고, `alert_rules` 조건을 평가한다. budget 이벤트는 `budget_scope.evaluate_admission`의 인시던트 생성 지점에서 발행.
- **require_approval**: `RunStatus.WAITING_APPROVAL` 재사용하되 `approvals`/`approval_requests` 테이블에 `kind='budget'`로 구분하거나 별도 `budget_approvals` 큐. 승인 시 `evaluate_admission` 재실행 후 `execute_run` 예약(`_schedule_run_jobs` 패턴).
- **retry-from-stage**: `stage_runs`에서 `from_stage` 이후를 `pending`으로 리셋하고 `execute_run`을 재호출 — `_do_approve`의 resume 경로와 동일한 "checkpoint 이후만 실행" 의미론을 재사용. 외부 쓰기 단계는 `wakeups`/멱등 마커 규칙 적용.
- **템플릿 export**: `store.get_custom_workflow(name)` → latest revision spec + `store.list_playbooks` 필터. import는 `WorkflowSpec.model_validate` → `store.save_custom_workflow`(base_revision 생략=append) → `_sync_workflow_registry`.
- **i18n 추출**: v2는 feature `i18n.ts`에 키 추가, 누락 검출은 `vitest`에서 STR 키 양쪽 존재 검사. legacy는 `data-jm-i18n` 속성 추가 + `I18N` dict 확장.
- **온보딩**: `users` 테이블에 `onboarding_dismissed_at`/`onboarding_completed_at` 컬럼 또는 `user_prefs` JSON. 체크리스트 항목은 `store` 조회 함수로 실시간 계산.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: run failed 종료 후 등록된 규칙이 60초 이내에 설정 채널로 발송된다(테스트 환경에서 mock 검증).
- **SC-002**: cooldown 내 동일 조건 재발사가 채널 발송 0회로 suppressed 기록된다.
- **SC-003**: `threshold_action=alert` 정책에서 임계 도달 시 inbox+최소 1개 외부 채널로 알림이 발송되고 run 생성은 차단되지 않는다.
- **SC-004**: `threshold_action=require_approval`에서 임계 초과 run이 승인 전 `execute_run` 미호출 상태로 유지되고, 승인 시 정상 실행된다.
- **SC-005**: export→import 왕복 후 워크플로 spec이 byte-equivalent(JSON canonical)하고 run 생성이 가능하다.
- **SC-006**: 시크릿 패턴이 포함된 import 페이로드가 422로 거부되고 부분 등록이 없다.
- **SC-007**: share token 만료 후 `GET /api/v1/templates/shared/{token}`이 410이다.
- **SC-008**: `retry?from_stage=` 실행 시 지정 단계 이전의 succeeded stage_run이 재실행되지 않는다(stage_run 수 비교).
- **SC-009**: `en` 로케일에서 실행 이력·알림·설정 뷰의 하드코딩 한국어 문자열이 0건이다(검증 스크립트).
- **SC-010**: 온보딩 체크리스트의 "첫 run" 항목이 실제 run 생성 후 자동 완료된다.
- **SC-011**: 프로덕션 모드에서 인증 없는 `/openapi.json` 접근이 401/404다.
- **SC-012**: 모든 신규 엔드포인트가 `_audit_api` 또는 동등 감사 로그를 남기고, 토큰·시크릿·메일 원문이 로그에 포함되지 않는다.

## Assumptions

- 알림 채널 중 email/slack/telegram/push는 **기존 env 기반 설정**(JOBMATE_SMTP_*, JOBMATE_SLACK_WEBHOOK_URL, JOBMATE_TELEGRAM_*)을 사용한다 — 사용자별 채널 credential 저장(예: 사용자별 Slack webhook DB 저장)은 범위 외.
- 마켓플레이스는 **단일 인스턴스 내** 템플릿 공유다 — 인스턴스 간 federation/공개 디렉터리는 범위 외.
- `require_approval`은 기존 `waiting_approval` 상태를 재사용한다 — 새 RunStatus 추가 없이 `approvals`/`approval_requests`에 구분 필드로 처리한다.
- 대시보드 커스터마이징은 **사용자별** 설정이다 — 전역 admin prefs와 분리된 per-user 저장소가 필요하며, 기존 `prefs.dashboard_widgets`는 읽기 호환만 유지한다.
- i18n은 **v2 React 앱 우선** — 레거시는 `data-jm-i18n` 유지 수준, 전면 재작성 금지.
- 온보딩은 **dismissible 가이드** — 강제 플로우가 아니며, 관리자 계정은 스킵 가능.
- OpenAPI 정책은 **환경 변수 기반**(JOBMATE_ENV 또는 NODE_ENV 유사) — 런타임 인증 게이트만 추가, 스키마 생성 자체는 유지.
- 모든 신규 테이블은 `schema_sqlite.py`+`schema_pg.py` 이중 정의와 `migrations.py` ALTER 보강이 필요하다(기존 패턴).

## Out of Scope

- 외부 마켓플레이스 디렉터리 연동(공개 카탈로그, 크로스 인스턴스)
- 사용자별 Slack/Telegram/email credential DB 저장(현재 env 기반 유지)
- 알림 규칙의 외부 웹훅 호출(임의 URL POST) — SSRF 위험으로 제외
- 레거시 index.html 전면 재작성 또는 제거
- 온보딩의 강제 단계/튜토리얼 오버레이 복잡 인터랙션
- API 문서의 GraphQL/ gRPC 변환
- 다국어 3개 언어 이상(ko/en만)
