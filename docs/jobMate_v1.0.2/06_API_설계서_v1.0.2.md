# jobMate API 설계서

| 항목 | 값 |
|---|---|
| 문서 버전 | 1.0.2 |
| 기준일 | 2026-11-19 |
| API 기준 | `/api/v1`, JSON, Django REST 계층 |
| 기준 문서 | 기본기획서, 시스템상세설계서, 기능별 상세설계서, DB 설계서 |

## 문서 이력

| 버전 | 기준일 | 수정 내용 |
|---|---|---|
| 1.0.0 | 2026-11-19 | 최초 작성 |
| 1.0.1 | 2026-11-27 | 부록 A 추가: 하네스 MVP API 명세(docs/harness/02_API_명세.md) 통합 |
| 1.0.2 | 2026-11-29 | v1.0.1 문서 세트에서 분기 — 다크 테마/Agent Office 셸(spec 010), 관제센터 모듈·신규 관측 엔드포인트(spec 011), OTLP 시그널(spec 009) 반영 |

## 1. 공통 규칙

- 모든 업무 API는 인증이 필요하며 조직 멤버십을 검사한다.
- 날짜/시간은 ISO 8601 형식과 명시적 timezone을 사용한다.
- 오류 응답: `{"error":{"code":"...","message":"...","request_id":"..."}}`
- 생성 응답은 서버 생성 ID와 상태를 반환한다. 백그라운드 작업은 `202 Accepted`를 반환한다.
- 외부 부작용 API는 idempotency key를 요구하거나 서버 측 키를 계산한다.

## 2. 주요 엔드포인트

| Method | Path | 목적 |
|---|---|---|
| GET | `/api/v1/workflows/templates` | 활성 템플릿 목록 |
| POST | `/api/v1/workflows/runs` | 업무 실행 생성 |
| GET | `/api/v1/workflows/runs` | 사용자 실행 목록 |
| GET | `/api/v1/workflows/runs/{run_id}` | 실행 및 단계 조회 |
| POST | `/api/v1/workflows/runs/{run_id}/cancel` | 실행 취소 |
| GET | `/api/v1/integrations/google` | 연결 상태와 scope 조회 |
| GET | `/api/v1/integrations/google/oauth/start` | OAuth 시작 URL |
| GET | `/api/v1/integrations/google/oauth/callback` | OAuth 완료 처리 |
| DELETE | `/api/v1/integrations/google` | 연결 해제 및 토큰 폐기 |
| PATCH | `/api/v1/automations/gmail-calendar` | 일정 자동화 활성화/설정 |
| GET | `/api/v1/automations/gmail-calendar/status` | 감지 상태·마지막 실행 조회 |
| GET | `/api/v1/calendar-events` | 등록 이벤트 이력 |
| GET | `/api/v1/notifications` | 앱 내 알림 조회 |
| POST | `/api/v1/notifications/push-subscriptions` | Web Push 구독 등록 |
| DELETE | `/api/v1/notifications/push-subscriptions/{id}` | 구독 해제 |
| POST | `/api/v1/notifications/{id}/open` | 알림 클릭 감사·허용 URL 반환 |

## 3. 업무 실행 계약

`POST /workflows/runs` 요청은 `template_id`, `template_version`, `input`을 받는다. 응답은 `run_id`, `status=queued`, `created_at`을 반환한다. 클라이언트는 run 상세를 polling한다. MVP 실시간 스트리밍은 선택 후속 기능이다.

## 4. Google 자동화 설정 계약

설정 필드: `enabled`, `mail_query`, `poll_interval_minutes`, `timezone`, `confidence_threshold`. 허용 query는 서버에서 보안 검증한다. `enabled=true`는 유효한 Google 연결과 필요한 OAuth scope가 있을 때만 가능하다.

## 5. 내부 처리 이벤트

Gmail/Calendar/푸시 작업은 외부에서 직접 공개 호출하지 않고 task queue 메시지로 실행한다. Queue payload에는 조직 ID, 연결 ID, 실행 ID만 넣고 토큰이나 메일 본문은 포함하지 않는다. 중복 전달은 작업 멱등 처리로 흡수한다.

## 6. 보안·오류

401 인증 실패, 403 권한 부족, 404 비공개 대상 없음, 409 중복/상태 충돌, 422 입력 검증, 429 제한, 502 외부 API 실패, 503 일시 불가. OAuth callback은 state와 PKCE를 검증한다. 원본 Gmail URL은 저장된 Google message ID로 서버가 생성하고 임의 URL 리다이렉트는 금지한다.

## 부록 A. 하네스 MVP API 명세 — 구현 기준 (구 docs/harness/02_API_명세.md)

> 원본: `docs/harness/02_API_명세.md` (문서 버전 0.2, 기준일 2026-11-19). docs/harness/는 폐기되고 본 문서로 통합됨.

> 이 API는 상용 전 Django 전환 전 임시 제공층이다. 인증·조직 권한·OAuth 관리는 포함하지 않는다.
> 오류 형식·상태 코드 원칙은 v1.0.2 API 설계서 본문을 따른다.

### 1. 공통 규칙

- 성공 응답은 JSON. 오류 응답: `{"error": {"code": "...", "message": "...", "request_id": "..."}}`.
- 비동기 실행은 `202 Accepted`. `request_id`는 `x-request-id` 헤더 또는 생성 UUID.
- `limit` 기본 50.

### 2. 엔드포인트

#### 2.1 GET `/` 

상태 페이지(`api/static/index.html`) 서빙. run 제출·목록·상세·승인 UI.

#### 2.2 GET `/api/v1/health`

`200`: `{"status": "ok"}`

#### 2.3 GET `/api/v1/workflows`

`200`: `[{"key": "dev_review", "version": "0.1.0"}, {"key": "gmail_calendar", "version": "0.1.0"}]`

#### 2.4 POST `/api/v1/runs` — `202`

본문: `{"workflow": "dev_review", "task": "...", "metadata": {}}`
- `workflow`는 등록된 키만 허용 — 아니면 `404 unknown_workflow`.
- `task`는 1~20000자 — 위반 시 `422`.
- run 생성 후 백그라운드 스레드에서 `WorkflowRunner.invoke` 실행.

응답 `202`: `{"id": "...", "status": "queued"}`

#### 2.5 GET `/api/v1/runs?limit=50`

run 목록(최신순): `[{"id","workflow","status","created_at"}]`

#### 2.6 GET `/api/v1/runs/{run_id}`

run 상세:
```json
{
  "id": "...", "workflow": "dev_review", "version": "0.1.0",
  "status": "waiting_approval", "trace_id": "...", "error": "",
  "input": {}, "metadata": {"pr_summary": "..."},
  "created_at": "...", "updated_at": "...",
  "stages": [{"stage_key": "...", "attempt": 1, "status": "succeeded",
              "input": {}, "output": {}, "error": "", "model": "...",
              "tokens_in": 0, "tokens_out": 0, "started_at": "...", "finished_at": "..."}],
  "approvals": [{"decision": "approve", "comment": "...", "decided_at": "..."}],
  "audit": [{"actor": "...", "action": "...", "object_ref": "...", "detail": {}, "created_at": "..."}]
}
```
없으면 `404 not_found`.

#### 2.7 POST `/api/v1/runs/{run_id}/approve`

본문: `{"decision": "approve|reject", "comment": "..."}`
- run이 `waiting_approval`이 아니면 `409 invalid_state`.
- `runner.resume(run_id, {decision, comment})`로 LangGraph interrupt 재개.
- 완료/거부 후 최신 run 상세 반환(`200`).

#### 2.8 GET `/api/v1/notifications?limit=50`

알림 목록: `[{"id","run_id","link_id","title","body","source_url","status","created_at","sent_at"}]`

#### 2.9 GET `/api/v1/event-links?limit=50`

이벤트 링크 이력: `[{"id","idempotency_key","gmail_message_id","status","calendar_event_id","notification_id","source_url","attempts","last_error","created_at","updated_at"}]`

#### 2.10 GET `/api/v1/gmail-calendar`

자동화 상태: `{"enabled": bool, "last_poll_at": ..., "last_poll_result": "..."}`

#### 2.11 POST `/api/v1/gmail-calendar/toggle`

본문 `{"enabled": bool}` → `{"enabled": bool}`. `automation_state` 테이블에 저장.

#### 2.12 POST `/api/v1/gmail-calendar/poll`

`202`: 수동 폴링 요청. MVP는 어댑터 미구성 시 안내 응답 —
실제 폴링은 gmail_calendar 워크플로 run을 통해 엔진에서 실행된다.

### 3. 오류 코드

| HTTP | code | 상황 |
|---|---|---|
| 404 | `unknown_workflow` | 미등록 workflow 키 |
| 404 | `not_found` | run 없음 |
| 409 | `invalid_state` | waiting_approval이 아닌 run에 승인 요청 |
| 422 | (FastAPI 기본) | 요청 본문 검증 실패 |
| 500 | `resume_failed` | resume 실행 예외 |

### 4. 내부 상태 코드

`runs.status`: `queued | running | waiting_approval | waiting_review | completed |
failed | rejected | cancelled | budget_exhausted | needs_human`
`stage_runs.status`: `pending | running | succeeded | failed | skipped`
`event_links.status`: `discovered | extracted | validated | calendar_created |
notification_queued | notified | needs_review | duplicate | connection_error | retry_wait`
`notifications.status`: `pending | sent | failed`

### 5. 구현 노트

- `create_app(settings=..., store=..., llm=...)`로 fake 주입 테스트 가능 —
  `tests/api/test_api.py`가 `FakeLLM`으로 전체 HTTP 흐름을 검증한다.
- run 실행은 `threading.Thread` 데몬 — MVP 단일 프로세스 전제.
  다중 워커 환경에서는 Django 전환 시 task queue로 교체한다.
- 승인 resume은 동기 실행 — LLM 재호출 없이 interrupt 이후 단계만 진행한다.
