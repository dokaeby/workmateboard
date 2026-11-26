# Paperclip-Inspired Agent Management Implementation Prompts

> **For agentic workers:** REQUIRED SUB-SKILL: Use
superpowers:subagent-driven-development (recommended) or
superpowers:executing-plans to implement this plan task-by-task. Steps use
checkbox (`- [ ]`) syntax for tracking.

**Goal:** Workmateboard에 Paperclip의 핵심 관리 기능을 세 단계로 추가해, 실제 에이전트의 업무
배정·실행·관찰·승인·비용·복구·목표 정렬을 한 대시보드에서 운영할 수 있게 한다.

**Architecture:** 기존 FastAPI/Store/LangGraph 실행 엔진과 Agent Office를 유지하고 그 위에
관리 계층을 추가한다. `task`는 업무의 영속적 소유권, 기존 `run`은 한 번의 실행, `stage_run`은 실행 내부 단계,
`agent_session`은 관찰된 도구 세션으로 분리한다. Vercel에서는 장시간 CLI 프로세스를 실행하지 않으며, 로컬/상주
runner 또는 기존 workflow 실행 경로가 실제 작업을 수행한다.

**Tech Stack:** Python 3.12, FastAPI, Pydantic, SQLite/PostgreSQL 이중 Store,
LangGraph, 단일 파일 대시보드 HTML/JS, Playwright, pytest, Ruff, mypy.

**Spec:** 이 문서 자체가 승인된 기능 범위와 단계별 실행 프롬프트다. 함께 읽을 필수 문서는
`docs/harness/05_내부기능_통합.md`, `docs/harness/06_호스팅_필수_로깅.md`,
`docs/superpowers/specs/2026-11-21-agent-office-design.md`,
`docs/superpowers/specs/2026-11-22-agent-sessions-design.md`다.

## Global Constraints

- 구현은 1차 → 2차 → 3차 순서로 진행하며 각 차수는 독립적으로 테스트·리뷰·커밋 가능한 상태로 끝낸다.
- Paperclip 소스나 Node/React UI를 복사하지 않는다. 개념과 운영 패턴만 현재 Python 구조에 맞게 구현한다.
- 기존 `runs`, `stage_runs`, `approvals`, `audit_events`, `agent_sessions`의
공개 동작과 기존 테스트를 깨뜨리지 않는다.
- `tasks`와 `runs`를 같은 개념으로 취급하지 않는다. 한 task는 0개 이상의 run을 가질 수 있다.
- Vercel 요청 안에서 Codex/Claude CLI 같은 장시간 로컬 프로세스를 실행하지 않는다.
- SQLite와 PostgreSQL 스키마를 동시에 추가·수정하고 두 구현의 필드와 제약을 동일하게 유지한다.
- 외부 이벤트 및 재시도 가능한 쓰기는 idempotency key와 데이터베이스 UNIQUE 제약으로 중복을 막는다.
- API 키, OAuth 토큰, provider credential, 이메일/사용자 메시지 원문, push endpoint 원문,
전체 프롬프트·transcript를 로그에 남기지 않는다.
- 새로운 외부 호출·비동기 실행·재시도·watchdog에는 `docs/harness/06_호스팅_필수_로깅.md` 기준의
INFO/WARNING/ERROR 로그를 추가한다.
- 브라우저에는 Paperclip/provider/runner API 키를 전달하지 않는다. 머신 쓰기 API는 세션 쿠키만으로
허용하지 않는다.
- 금액 계산에 `float`를 사용하지 않는다. 비용은 정수 `microusd`(1 USD = 1,000,000 microusd)로
저장한다.
- 시간은 저장 시 UTC ISO-8601로 정규화하고, UI에서만 사용자 시간대로 표시한다.
- 자유 텍스트 입력에는 길이 제한을 두고, UI 출력 시 `textContent` 또는 기존 안전 렌더링 경로를 사용한다.
- 새 기능은 TDD로 구현한다. 실패 테스트 → 최소 구현 → 통과 → 관련 전체 테스트 순서를 지킨다.
- 코드 수정 완료 후 `graphify update .`를 시도한다. Windows 정책/설치 문제로 실패하면 우회하지 말고 정확한
오류를 보고한다.
- 검증 명령은 `uv run pytest`, `uv run ruff check src/jobmate tests`, `uv run
ruff format --check src/jobmate tests`, `uv run mypy src/jobmate`다.
- 사용자가 명시하지 않은 운영 배포, 실제 외부 메시지 발송, 비밀값 변경은 수행하지 않는다.

## Review Focus

- 같은 task를 두 agent가 동시에 checkout할 때 정확히 하나만 성공하고 다른 요청은 `409`여야 한다.
- 동일 idempotency key로 wake/event/cost 요청이 재전송돼도 run, event, charge가 중복 생성되지
않아야 한다.
- 종료·일시정지·예산 초과 agent는 새 작업을 시작할 수 없고, 기존 읽기 기록은 보존돼야 한다.
- runner가 죽거나 heartbeat가 끊겨도 task가 영원히 `in_progress`로 남지 않고 recovery 또는 사람
개입 상태로 전이돼야 한다.
- PostgreSQL과 SQLite에서 상태 전이, UNIQUE 제약, 정렬, NULL 처리 결과가 동일해야 한다.

---

## 이 문서 사용법

각 차수를 구현하는 에이전트에게 다음 두 블록을 순서대로 제공한다.

1. 아래의 **공통 실행 프롬프트** 전체
2. 구현할 차수의 **차수별 실행 프롬프트** 전체

차수를 건너뛰지 않는다. 이전 차수의 커밋 SHA와 검증 결과를 다음 차수 프롬프트의 `<PREVIOUS_COMMIT>`와
`<PREVIOUS_VERIFICATION>`에 넣는다. 구현 에이전트는 먼저 저장소를 조사하고 계획을 짧게 보고한 뒤 작업하되, 이미
결정된 인터페이스를 임의로 재설계하지 않는다.

## 공통 실행 프롬프트

```text
당신은 Workmateboard 저장소의 구현 담당자다. 요청한 차수만 구현하고 다음 차수 기능을 미리 만들지 마라.

작업을 시작하기 전에 다음을 반드시 수행하라.

1. 루트 AGENTS.md와 다음 문서를 끝까지 읽어라.
   - docs/harness/05_내부기능_통합.md
   - docs/harness/06_호스팅_필수_로깅.md
   - docs/superpowers/specs/2026-11-21-agent-office-design.md
   - docs/superpowers/specs/2026-11-22-agent-sessions-design.md
2. git status, 현재 브랜치, 최근 커밋을 확인하라. 사용자 변경을 덮어쓰거나 정리하지 마라.
3. 관련 코드와 테스트를 먼저 읽고 현재 동작을 설명하라.
   - src/jobmate/api/app.py
   - src/jobmate/store/db.py
   - src/jobmate/engine/runner.py
   - src/jobmate/agent_bridge.py
   - src/jobmate/models/
   - src/jobmate/api/static/index.html
   - src/jobmate/api/static/office/jobmate-bridge.js
   - tests/api/test_agent_sessions.py
   - tests/unit/test_store.py
   - tests/integration/test_dev_review.py
   - tests/ui/test_dashboard.py
4. graphify-out/graph.json이 있으면 먼저 graphify query를 사용하라. 없으면 graphify 실행 가능
여부를 확인하되, Windows 보안 정책을 우회하거나 임의 설치하지 마라.
5. 수정 전 관련 테스트를 실행해 기준 상태를 기록하라. 환경 문제와 코드 실패를 구분하라.

설계 원칙:

- Workmateboard가 기존 dev_review workflow 실행의 source of truth다.
- Agent 관리 계층은 task를 소유하고, task 실행이 필요할 때 기존 run을 생성하거나 외부 runner가 가져갈 wake
command를 만든다.
- task는 업무, run은 실행 시도, stage_run은 실행 단계, agent_session은 관찰된 CLI 세션이다.
- API route는 얇게 유지하고 상태 전이와 권한 판단은 service 계층에 둔다.
- app.py가 더 비대해지지 않도록 새 관리 API는 APIRouter 또는 명확히 분리된 모듈로 구성하라. 기존
create_app 의존성 주입 패턴과 충돌하면 작은 router factory를 사용하라.
- Store 메서드는 SQL 세부사항을 감추고, service 계층은 Store의 공개 메서드만 사용하라.
- 모든 상태 전이는 allowlist로 검증하고 잘못된 전이는 409를 반환하라.
- 모든 머신 이벤트는 서버가 actor/provider를 검증하고 audit event를 남겨라.
- 원문 prompt/transcript를 agent event나 운영 로그에 저장하지 마라. 화면에는 최대 200자의 안전한 진행
요약만 전달하라.
- 새 dependency는 정말 필요한 경우가 아니면 추가하지 마라. 표준 라이브러리와 현재 의존성을 우선하라.

TDD 규칙:

- 각 기능은 먼저 실패하는 테스트를 작성하라.
- Store 단위 테스트, API 테스트, 상태 전이 테스트, 보안 회귀 테스트를 분리하라.
- UI 변경에는 Playwright 테스트를 추가하되 transient 애니메이션 문구가 아니라 durable DOM 상태를 검증하라.
- 동시성/멱등성 요구에는 실제 병렬 호출 또는 동일 요청 재전송 테스트를 포함하라.
- 테스트 편의를 위해 운영 코드의 인증을 약화하지 마라.

보안 및 로깅:

- 머신 쓰기 endpoint는 JOBMATE_API_KEY 인증을 요구하라. 세션 쿠키만으로는 거부하라.
- API 키·Bearer token·secret·provider 응답 원문을 detail/audit/log에 넣지 마라.
- 성공 로그는 entity id, action, duration, result count만 포함하라.
- 실패 로그는 entity id, HTTP status, exception class 또는 짧은 안전 오류코드만 포함하라.
- 외부 URL을 추가한다면 HTTPS, 허용 호스트 또는 명시적 base URL 검증, timeout, redirect 정책을
정의하라.

완료 보고에는 반드시 다음을 포함하라.

- 실제 수정 파일
- 구현한 상태 전이와 API
- 실행한 테스트와 정확한 결과
- 실행하지 못한 검증과 이유
- 보안·운영상 남은 제한
- 커밋 SHA

문서를 먼저 읽고 현재 구조를 확인한 후 구현을 시작하라. 질문 없이 안전하게 결정할 수 있는 세부사항은 기존 패턴에 맞춰 결정하고,
제품 범위를 바꾸는 결정만 사용자에게 물어라.
```

---

# 1차 구현 프롬프트: Agent Registry, Tasks, Heartbeat, Live Office

## 1차 목표

영구적인 에이전트 명부와 작업 티켓을 만들고, 작업 배정·checkout·heartbeat·wakeup·실행 이벤트가 Agent
Office에 실제로 반영되게 한다. 이 단계가 끝나면 사용자는 데모 데이터 없이 에이전트가 대기→작업→검토/승인→완료로 움직이는
흐름을 확인할 수 있어야 한다.

## 1차 실행 프롬프트

```text
<PREVIOUS_COMMIT> 없음 또는 현재 기준 커밋
<PREVIOUS_VERIFICATION> 현재 기준 테스트 결과

1차 범위만 구현하라: Agent Registry, Task 관리, checkout, heartbeat/wakeup, 통합 agent
event, Agent Office 실제 상태 표시.

절대 하지 말 것:

- 비용 원장, 예산 incident, 일반 승인함, recovery watchdog, goal/project, routine,
Paperclip connector는 구현하지 마라.
- Paperclip 패키지를 설치하거나 Paperclip DB/UI를 복사하지 마라.
- Vercel 함수에서 Codex/Claude CLI 프로세스를 실행하지 마라.
- 기존 agent_sessions를 agents 테이블로 재사용하지 마라.

### A. 모델과 상태

다음 enum을 기존 models 패키지의 관례에 맞춰 추가하라.

- AgentStatus: pending_approval, active, idle, running, paused, error,
terminated
- AgentAdapterType: workflow, external
- TaskStatus: backlog, todo, in_progress, in_review, waiting_approval,
blocked, done, cancelled
- TaskPriority: low, medium, high, urgent
- AgentEventType:
  agent_created, agent_status_changed, task_created, task_assigned,
  task_checked_out, heartbeat_received, wake_requested, run_linked,
  review_requested, task_completed, task_blocked, task_cancelled

허용 상태 전이를 상수 또는 순수 함수로 명시하라. 최소 규칙:

- pending_approval → active|terminated
- active|idle → running|paused|error|terminated
- running → idle|paused|error|terminated
- paused → active|terminated
- error → active|paused|terminated
- terminated에서는 다른 상태로 전이할 수 없다.
- backlog → todo|cancelled
- todo → in_progress|blocked|cancelled
- in_progress → in_review|waiting_approval|blocked|done|cancelled
- in_review → in_progress|waiting_approval|done|blocked|cancelled
- waiting_approval → in_progress|done|cancelled
- blocked → todo|in_progress|cancelled
- done/cancelled은 terminal이다.

### B. 데이터베이스

SQLite SCHEMA와 PostgreSQL SCHEMA_PG에 동일한 의미로 다음 테이블을 추가하라.

agents:
- id TEXT/UUID-compatible primary key
- name, role, title
- adapter_type
- adapter_config JSON text; API 응답에서는 secret-like key를 제거하거나 REDACTED 처리
- reports_to nullable self reference
- status
- monthly_budget_microusd default 0 (2차에서 사용, 지금은 저장만 허용)
- created_at, updated_at, last_seen_at nullable
- name은 빈 문자열 금지, reports_to cycle 금지

tasks:
- id primary key와 사람이 읽는 identifier(예: WMB-1) UNIQUE
- title, description, status, priority
- assignee_agent_id nullable
- reviewer_agent_id nullable
- parent_id nullable self reference
- current_run_id nullable
- checkout_agent_id nullable
- checkout_at nullable
- created_by, created_at, updated_at
- title 200자, description 합리적 상한 적용

agent_events:
- id primary key
- idempotency_key UNIQUE
- event_type
- agent_id/task_id/run_id nullable
- actor
- summary 최대 200자
- detail JSON text, 민감 필드 금지
- occurred_at, created_at

task_runs:
- task_id, run_id 복합 UNIQUE
- attempt
- created_at

필요한 foreign key와 조회 인덱스를 추가하라. 기존 DB를 삭제하거나 재생성하지 않고 CREATE TABLE IF NOT
EXISTS와 기존 프로젝트의 additive migration 방식에 맞춰라.

Store 공개 메서드를 추가하라. 정확한 이름은 기존 naming과 충돌하면 조정할 수 있지만 다음 책임은 분리돼야 한다.

- create/list/get/update agent
- validate and transition agent status
- create/list/get/update task
- atomic checkout_task(task_id, agent_id)
- assign_task(task_id, agent_id)
- link_task_run(task_id, run_id)
- append_agent_event(..., idempotency_key)
- list_agent_events(filters, limit)

동일 idempotency key의 append는 기존 이벤트를 반환하고 새 row를 만들지 않아야 한다.

### C. 서비스 계층

`src/jobmate/services/agent_management.py` 또는 프로젝트 구조에 맞는 집중된 모듈을 만든다.

서비스가 담당할 것:

- 상태 전이 검증
- reports_to cycle 검증
- task 배정 가능 여부 검증
- atomic checkout 결과를 409 domain error로 변환
- heartbeat에 따른 agent status/last_seen/task 상태 갱신
- workflow adapter wake 처리
- external adapter wake command/event 처리
- audit event 기록

workflow adapter의 adapter_config는 최소 `{ "workflow": "dev_review" }`를 지원한다.
wake 요청이 들어오면 기존 run 생성 경로를 재사용하고 task_runs에 연결한다. run input metadata에는
task_id/agent_id를 넣되 기존 workflow 입력 형식을 깨지 마라.

external adapter는 서버에서 프로세스를 실행하지 않는다. wake event를 기록하고 202를 반환한다. 외부
runner가 task를 조회·checkout하고 heartbeat를 보내는 pull/push 계약만 제공한다.

### D. API

새 route는 `/api/v1/management` 아래에 모은다.

최소 endpoint:

- POST /agents
- GET /agents
- GET /agents/{agent_id}
- PATCH /agents/{agent_id}
- POST /agents/{agent_id}/transition
- POST /agents/{agent_id}/wake
- POST /agents/{agent_id}/heartbeat
- POST /tasks
- GET /tasks?status=&assignee=&limit=
- GET /tasks/{task_id}
- PATCH /tasks/{task_id}
- POST /tasks/{task_id}/assign
- POST /tasks/{task_id}/checkout
- GET /events?agent_id=&task_id=&run_id=&after=&limit=

응답 규칙:

- create는 201, wake는 202, 조회는 200
- validation 422, not found 404, invalid transition/checkout conflict 409
- 머신 heartbeat와 외부 runner checkout은 API key 인증 필수
- 일반 UI 조회와 사람이 수행하는 관리 동작은 기존 세션/API key 정책을 따른다.
- cursor를 만들지 않는다면 limit 최대값을 명시하고 최신순 정렬을 고정하라.

wake는 `Idempotency-Key` 헤더 또는 body idempotency_key를 필수로 받고 동일 키 재전송 시 같은
결과를 반환해야 한다.

### E. 기존 run과 연동

- 기존 POST /api/v1/runs 동작은 그대로 유지한다.
- management wake가 workflow run을 만들 때 공통 helper를 추출해 중복을 제거한다.
- run이 완료/실패/승인대기로 바뀔 때 task와 agent 상태가 함께 갱신되도록 최소 hook을 추가한다.
- task 상태 갱신 실패가 run 결과를 유실시키지 않게 audit/log를 남기고 복구 가능한 상태를 보존한다.
- 기존 dev_review 단계 정의를 변경하지 마라.

### F. Agent Office UI

기존 워크플로/세션 탭을 깨지 말고 관리 모드를 추가하거나 세션 탭에 management provider를 명확히 통합하라.

화면에 최소 표시:

- agent 이름, role, status, 현재 task
- task identifier/title/priority/status
- 마지막 heartbeat 상대시간
- 실제 run 진행 상태와 경과시간
- pause/error/approval 상태의 구분

상태 매핑:

- running → working animation
- pending_approval 또는 waiting_approval → decision
- active/idle/paused → rest, paused badge 별도
- error → error badge/색상
- terminated → 기본 목록 제외, 필터로 조회 가능

데모 fallback은 명시적 Demo 토글에서만 사용한다. 실제 agents/tasks가 있으면 demo data를 섞지 마라.

### G. 테스트

최소 테스트를 먼저 작성하라.

Store:
- agent CRUD와 reports_to cycle 거부
- agent/task 모든 허용·거부 상태 전이
- 두 스레드가 같은 task checkout 시 하나만 성공
- 동일 idempotency event가 한 건만 저장
- SQLite 기존 schema upgrade 후 데이터 보존

API:
- 인증 없는 machine heartbeat 거부
- agent 생성/전이/조회
- task 생성/배정/checkout
- invalid transition 409
- workflow wake가 run을 정확히 한 번 생성하고 task와 연결
- external wake가 프로세스 없이 202/event를 생성
- 동일 wake key 재전송이 run을 중복 생성하지 않음

Integration:
- task 생성 → workflow agent 배정 → wake → dev_review run 생성 → task_runs 연결
- run waiting_approval/completed/failed가 task/agent 상태로 투영됨

UI:
- demo가 아닌 API agent가 캐릭터로 표시
- heartbeat 후 working 상태가 durable DOM에 반영
- paused/error/waiting approval badge
- 기존 workflow와 session UI 회귀 없음

### H. 문서와 검증

`docs/harness/05_내부기능_통합.md`에 모델, API, 상태 전이, Vercel 제한, 운영 방법을 추가하라.
`06_호스팅_필수_로깅.md`에 heartbeat/wake/task/run 연동 로그를 추가하라.

검증 순서:

1. 새 단위/API 테스트
2. dev_review integration 테스트
3. 전체 pytest
4. Ruff check/format check
5. mypy
6. Playwright UI 테스트
7. graphify update .

완료 조건:

- API로 agent와 task를 만들 수 있다.
- task를 workflow agent에 배정하고 wake하면 실제 dev_review run이 한 번만 생성된다.
- Agent Office에서 실제 상태가 10초 이내 polling 주기에 반영된다.
- external agent heartbeat가 working/rest/decision 상태를 갱신한다.
- 동시 checkout과 재전송이 중복 실행을 만들지 않는다.
- 기존 테스트가 모두 통과한다.

완료 후 다음 범위는 구현하지 말고 아래 커밋을 만든다.

git commit -m "feat: add agent task and heartbeat management"
```

---

# 2차 구현 프롬프트: Costs, Budgets, Approvals, Recovery

## 2차 목표

1차 관리 계층 위에 정확한 비용 원장, 예산 경고/강제 정지, 범용 승인함, 멈춘 작업 감지와 제한된 복구를 추가한다. 운영자는 어떤
에이전트가 비용을 쓰는지 확인하고 폭주를 막으며, 위험 행동과 복구 요청을 한곳에서 처리할 수 있어야 한다.

## 2차 실행 프롬프트

```text
<PREVIOUS_COMMIT> 1차 구현 커밋 SHA
<PREVIOUS_VERIFICATION> 1차 전체 검증 결과

먼저 PREVIOUS_COMMIT이 현재 history에 있고 worktree가 의도한 상태인지 확인하라. 1차 API와 모델을
재설계하지 말고 확장하라.

2차 범위만 구현하라: cost ledger, budget policy/incident, generalized approval
request, watchdog/recovery action, 운영 UI.

절대 하지 말 것:

- goal/project/routine/comment/artifact/Paperclip connector를 구현하지 마라.
- 가격을 알 수 없는 사용량을 0원으로 기록하지 마라.
- 예산 초과를 이유로 기록을 삭제하거나 task를 자동 완료하지 마라.
- 사람 승인 없이 운영 배포, 데이터 삭제, 외부 발송을 실행하지 마라.

### A. 비용 모델

enum:

- CostStatus: reported, estimated, unpriced
- BillingType: metered_api, subscription_included, subscription_overage,
credits, fixed, unknown
- BudgetScopeType: global, agent, task
- BudgetWindowKind: calendar_month_utc, lifetime
- BudgetIncidentLevel: warning, hard_stop
- BudgetIncidentStatus: open, resolved, dismissed

테이블 cost_events:

- id primary key
- idempotency_key UNIQUE
- agent_id 필수
- task_id/run_id/stage_run_id nullable
- provider, model, billing_type, cost_status
- input_tokens, cached_input_tokens, output_tokens nonnegative integer
- cost_microusd nullable: unpriced면 NULL이어야 하고 0으로 대체하지 않는다.
- pricing_source와 짧은 pricing_version nullable
- occurred_at, created_at

비용 입력은 음수, NaN, float 문자열을 거부하고 정수 microusd만 받는다. API display layer에서만 USD
문자열로 변환한다.

### B. 예산 정책과 incident

테이블 budget_policies:

- id
- scope_type, scope_id
- amount_microusd
- window_kind
- warn_percent 기본 80, 1..99
- hard_stop_enabled
- unpriced_usage_policy: block|allow
- reservation_microusd 기본 0
- active
- created_at, updated_at
- 동일 active scope/window 정책 중복 금지

테이블 budget_incidents:

- id
- policy_id
- level
- status
- observed_spend_microusd nullable
- unpriced_event_count
- reason_code
- opened_at, resolved_at, resolved_by, resolution
- 같은 policy/level/open incident 중복 금지

규칙:

- 월간 window는 UTC 월 경계다.
- warn_percent 도달 시 warning incident 한 건 생성, agent를 멈추지 않는다.
- 100% 도달 또는 block 정책에서 unpriced usage가 생기면 hard_stop을 생성한다.
- agent scope hard stop은 해당 agent만 paused로 만들고 다른 agent는 계속 실행한다.
- task scope hard stop은 task를 blocked로 만들고 agent의 다른 task는 막지 않는다.
- global hard stop은 새 wake를 거부하되 현재 데이터 조회는 허용한다.
- reservation은 새 run 시작 가능 여부만 판단한다. 실제 비용으로 기록하지 않는다.
- incident resolve에서 새 예산은 관측 지출보다 커야 resume할 수 있다.

### C. 기존 LLM/run 사용량 연결

- stage_run의 tokens_in/out을 cost event와 연결한다.
- provider가 실제 비용을 제공하면 reported, 프로젝트 rate card로 계산하면 estimated, 둘 다 아니면
unpriced다.
- 현재 provider가 비용을 제공하지 않는 경우 억지 rate를 만들지 마라.
- 같은 run/stage usage가 재처리돼도 idempotency key로 한 건만 기록한다.
- run 시작 전 활성 budget 정책과 reservation을 검사한다.
- budget hard stop 발생 시 새 실행을 거부하고 안전하게 상태를 기록한다.

### D. 범용 승인함

기존 approvals 테이블과 `/runs/{id}/approve` 호환성을 유지한다. 범용 승인을 위해 별도
approval_requests를 추가하거나 무손실 additive migration을 사용하라.

enum:

- ApprovalStatus: pending, approved, rejected, expired, cancelled
- ApprovalRisk: low, medium, high, critical
- ApprovalActionType: run_resume, budget_raise, agent_hire,
agent_terminate, external_send, deploy, destructive_tool, recovery

approval_requests 필드:

- id, action_type, risk_level, status
- requested_by_agent_id nullable
- task_id/run_id nullable
- resource_type/resource_id
- summary 최대 300자
- safe_payload JSON: secret-like key를 validation 단계에서 거부
- expires_at nullable
- decided_by, decision_comment, decided_at nullable
- created_at, updated_at

규칙:

- pending 상태에서만 approve/reject 가능
- high/critical은 API key만으로 자동 승인하지 말고 로그인된 사람 또는 명시적 admin actor를 요구한다.
- 승인 결과는 같은 transaction에서 대상 상태 변경과 audit 기록까지 처리한다.
- 승인 후 실행 재개가 실패하면 승인 기록을 되돌리지 말고 follow-up error event를 남긴다.

### E. Watchdog와 Recovery

테이블 recovery_actions:

- id
- source_type: agent|task|run
- source_id
- kind: stale_heartbeat|stranded_task|stuck_run|missing_cost|failed_resume
- status: active|escalated|resolved|cancelled
- attempt_count, max_attempts
- next_attempt_at, timeout_at
- owner_type: system|agent|human
- outcome, resolution_note
- created_at, updated_at, resolved_at
- source당 active/escalated 한 건만 허용

cron/tick 또는 automation loop의 한 cycle에 bounded watchdog를 추가한다.

최소 감지:

- running agent의 last_seen_at이 임계값을 넘김
- in_progress task에 active run/check-out owner가 없음
- run이 제한 시간을 넘겼으나 terminal이 아님
- terminal run에 cost event가 없거나 unpriced 상태
- 승인 완료 후 task가 재개되지 않음

복구 규칙:

- 자동 retry는 명시적으로 안전한 workflow wake에만 적용한다.
- destructive/external action은 자동 retry하지 않고 approval/recovery 요청을 만든다.
- max_attempts 초과 시 escalated + task blocked + 사람 알림을 만든다.
- watchdog 한 cycle의 처리 건수에 상한을 둔다.
- 같은 문제를 매 tick마다 중복 생성하지 않는다.

### F. API와 UI

최소 API:

- POST/GET /management/cost-events
- GET /management/costs/summary?scope=&from=&to=
- POST/GET/PATCH /management/budgets
- GET /management/budget-incidents
- POST /management/budget-incidents/{id}/resolve
- POST/GET /management/approvals
- POST /management/approvals/{id}/decision
- GET /management/recovery-actions
- POST /management/recovery-actions/{id}/resolve
- POST /management/watchdog/run (관리자/테스트용, 운영 cron과 같은 service 사용)

Agent Office 또는 대시보드에 다음 durable UI를 추가한다.

- 이번 달 총 비용, 예산 사용률
- agent별 비용과 unpriced 표시
- warning/hard-stop banner
- pending approval queue
- stale/error/recovery 상태
- 승인·거절·예산 상향·pause 유지 동작

돈은 locale formatting된 문자열로 표시하되 내부 계산은 microusd integer를 유지한다.

### G. 테스트

Cost:
- reported/estimated/unpriced 구분
- 0 비용과 unknown 비용 구분
- 중복 idempotency key 차단
- 음수/overflow/잘못된 타입 거부
- 기간·agent·task별 합계 정확성

Budget:
- 79% no incident, 80% warning, 100% hard stop
- warning 중복 방지
- agent hard stop이 다른 agent를 멈추지 않음
- unpriced block/allow 정책
- 동시 run reservation으로 oversubscription 방지
- 월 경계 UTC 테스트
- observed spend 이하 예산으로 resume 거부

Approval:
- pending에서 단 한 번만 결정
- 동시 approve/reject 중 하나만 성공
- safe_payload의 token/secret/password/api_key 계열 key 거부
- high/critical의 권한 검증
- 기존 run approve 회귀 테스트

Recovery:
- stale heartbeat 감지
- stranded task 감지
- 같은 source 중복 recovery 방지
- 안전 retry 횟수 제한
- destructive action 자동 retry 금지
- max attempts 후 escalated/blocked
- watchdog batch limit

UI:
- 비용·unpriced·incident·approval 표시
- hard stop 상태에서 wake 버튼 비활성
- approve/reject 더블클릭 중복 방지
- 기존 Agent Office UI 회귀 없음

### H. 로깅과 검증

운영 필수 로그:

- cost event accepted: ids, provider, model, cost_status; 금액은 허용하되
credential 금지
- budget warning/hard stop: policy/scope/reason
- approval requested/resolved: approval id/action/risk/decision
- watchdog cycle: scanned/detected/retried/escalated counts
- recovery failure: recovery id, exception class, safe error code

문서 두 개를 업데이트하고 전체 검증을 실행한다. graphify 실패는 우회하지 말고 보고한다.

완료 조건:

- 실제/추정/미가격 사용량을 구분해 조회할 수 있다.
- 예산 80%와 100% 동작이 테스트로 고정된다.
- hard stop이 정확한 scope만 중지한다.
- 일반 승인 요청을 UI에서 결정할 수 있다.
- 멈춘 task/run을 watchdog가 중복 없이 감지하고 제한된 복구 또는 escalation한다.
- 1차 기능과 기존 전체 테스트가 통과한다.

완료 후 다음 커밋을 만든다.

git commit -m "feat: add agent cost governance and recovery"
```

---

# 3차 구현 프롬프트: Goals, Projects, Routines, Permissions, Integrations

## 3차 목표

업무를 목표와 프로젝트에 연결하고, 반복 업무·작업 대화·산출물·도구 권한·외부 오케스트레이터 연결을 추가한다. 이 단계가 끝나면
Workmateboard가 단순 실행 모니터가 아니라 목표에서 실제 결과까지 추적 가능한 운영 시스템이 된다.

## 3차 실행 프롬프트

```text
<PREVIOUS_COMMIT> 2차 구현 커밋 SHA
<PREVIOUS_VERIFICATION> 2차 전체 검증 결과

먼저 1·2차 모델/API/테스트가 존재하고 통과하는지 확인하라. 기존 인터페이스를 삭제하거나 이름을 바꾸지 말고 확장하라.

3차 범위: Goals/Projects, task context와 artifacts, routines, role/tool
permissions, Paperclip-compatible external integration projection.

비범위:

- 다중 회사 billing, 사용자 초대 시스템, SaaS 구독 결제, Paperclip UI 복제
- arbitrary file hosting과 대용량 artifact 업로드
- 브라우저에서 외부 provider API 직접 호출
- 외부 Paperclip 상태를 기존 Workmateboard run으로 재실행

### A. Goal과 Project

테이블 goals:

- id, title, description
- parent_id nullable
- status: active|paused|achieved|cancelled
- owner_agent_id nullable
- success_metrics JSON text
- target_at nullable
- created_at, updated_at
- parent cycle 금지

테이블 projects:

- id, goal_id nullable
- name, description
- status: planned|active|paused|completed|cancelled
- owner_agent_id nullable
- repository_url nullable, repository_ref nullable
- workspace_path nullable; API 응답에서 로컬 절대경로 노출 여부를 권한으로 제한
- created_at, updated_at

tasks에 project_id와 goal_id를 additive migration으로 추가한다. task의 effective
goal은 task.goal_id → project.goal_id → none 순서로 해석한다.

API와 UI에서 Goal → Project → Task → Run → Stage/Event 경로를 한 번에 추적할 수 있게 한다.

### B. Task comments와 artifacts

task_comments:

- id, task_id
- author_type: human|agent|system
- author_id
- body 최대 길이와 content type text/markdown
- created_at, edited_at nullable

task_artifacts:

- id, task_id, run_id nullable
- kind:
plan|diff|test_report|security_report|review|screenshot|deployment|other
- name, uri, checksum_sha256 nullable
- media_type, size_bytes nullable
- metadata JSON text
- created_by, created_at

보안 규칙:

- artifact는 메타데이터와 안전한 URI만 저장한다. 서버가 임의 URI를 fetch하지 않는다.
- 허용 URI는 https URL 또는 승인된 repo-relative 경로다.
- file://, javascript:, data:, UNC, 상위 디렉터리 탈출을 거부한다.
- comment/artifact UI는 HTML 원문을 실행하지 않는다.
- prompt/transcript 자동 전체 저장은 금지한다. agent는 사람이 검토할 요약과 산출물만 등록한다.

### C. Routines

routines:

- id, name, enabled
- project_id/task_template JSON
- assignee_agent_id
- schedule_kind: interval|cron_daily|cron_weekly
- timezone IANA name
- schedule JSON
- next_run_at, last_run_at
- max_concurrent 기본 1
- created_at, updated_at

routine_runs:

- id, routine_id, scheduled_for
- task_id nullable
- status: pending|created|skipped|failed
- idempotency_key UNIQUE
- error_code
- created_at, finished_at

기존 automation loop와 `/cron/tick`에 bounded routine scheduler를 통합한다.

규칙:

- 동일 routine/scheduled_for는 task를 한 번만 만든다.
- missed schedule을 무제한 catch-up하지 않는다. 기본적으로 가장 최근 1회만 처리한다.
- max_concurrent를 넘으면 skipped 또는 다음 tick으로 연기하되 중복 task를 만들지 않는다.
- timezone/DST 경계를 테스트한다.
- routine task 생성 후 기존 assign/wake 정책을 재사용한다.

### D. Role과 Tool Permission

agent_permissions:

- id, agent_id
- capability
- effect: allow|deny
- scope_type: global|project|task
- scope_id nullable
- created_at
- UNIQUE(agent_id, capability, scope_type, scope_id, effect)

capability 최소 집합:

- task:read, task:create, task:assign, task:update
- agent:read, agent:manage, agent:wake
- run:execute, run:approve
- cost:read, budget:manage
- artifact:create
- external:send
- deploy:execute
- destructive:execute
- integration:manage

판정 규칙:

- explicit deny가 allow보다 우선한다.
- scope가 좁은 규칙(task > project > global)을 먼저 평가하되 deny 우선은 유지한다.
- 사람 관리자와 머신 agent의 권한 모델을 구분한다.
- 외부발송·배포·파괴 작업은 기본 deny이며 명시적 allow와 2차 approval을 모두 요구한다.
- UI에서 버튼 숨김은 편의 기능일 뿐, 서버가 반드시 다시 검증한다.

### E. Paperclip-compatible Integration

목적은 Paperclip을 Workmateboard 안에서 재구현하는 것이 아니라 외부 control plane의 상태를 읽기 모델로
투영하는 것이다.

공통 interface를 만든다.

- OrchestratorClient protocol
  - get_dashboard()
  - list_agents()
  - list_tasks(updated_after)
  - list_runs(updated_after)
  - get_cost_summary()
- PaperclipClient 구현은 httpx timeout, HTTPS/localhost URL 정책, redirect 제한,
인증 헤더 redaction을 적용한다.

projection 테이블:

- external_agents(provider, external_id, company_id, payload_summary,
status, updated_at)
- external_tasks(provider, external_id, assignee_external_id, status,
title, updated_at)
- external_runs(provider, external_id, task_external_id, agent_external_id,
status, token/cost summary, updated_at)
- external_sync_cursors(provider, company_id, cursor/last_synced_at,
last_error_code)
- provider+external_id UNIQUE

브리지 방식:

- 로컬/상주 `jobmate paperclip-bridge`가 Paperclip API를 polling한다.
- 브리지는 변경분만 Workmateboard machine endpoint로 push한다.
- Vercel은 Paperclip/Codex CLI를 실행하지 않는다.
- 외부 agent/task/run은 기존 native agents/tasks/runs와 ID namespace를 섞지 않는다.
- 외부 task를 native run으로 자동 생성하거나 재실행하지 않는다.
- 상세 prompt/transcript, adapter env, credential은 동기화하지 않는다.

최소 CLI 옵션:

- --paperclip-url
- --company-id
- --jobmate-url
- --interval (최소값 적용)
- --once

secret은 command line argument보다 환경변수를 우선하고 로그에서 가린다.

### F. API와 UI

추가 API:

- CRUD /management/goals
- CRUD /management/projects
- GET/POST /management/tasks/{id}/comments
- GET/POST /management/tasks/{id}/artifacts
- CRUD /management/routines
- GET/PUT /management/agents/{id}/permissions
- POST /management/integrations/paperclip/snapshot (machine auth +
idempotency)
- GET /management/integrations/paperclip/status

UI:

- Goal/Project/Task 계층 탐색
- project별 Agent Office 필터
- task detail의 comments/artifacts/run history
- routine 상태와 다음 실행 시간
- agent permissions 요약
- native와 Paperclip external agent를 provider badge로 구분
- 외부 연결 끊김/stale 상태 표시

외부 상태가 stale이어도 마지막 정상 snapshot을 삭제하지 말고 stale badge와 last_synced_at을 표시한다.

### G. 테스트

Goals/Projects:
- parent cycle 거부
- effective goal precedence
- project cancel 시 task 기록 보존
- repository/workspace 민감 경로 권한별 응답

Comments/Artifacts:
- 길이 제한과 XSS 문자열 안전 표시
- unsafe URI/path traversal 거부
- checksum/size validation
- 삭제 대신 감사 가능한 상태 보존 정책

Routines:
- 동일 scheduled_for 중복 task 방지
- DST 및 timezone
- missed schedule 1회 제한
- max concurrent
- cron/tick 재전송 멱등성

Permissions:
- deny 우선
- task/project/global scope precedence
- 서버 측 권한 재검증
- external/deploy/destructive 기본 deny + approval 필요

Paperclip bridge:
- API key가 로그/응답에 노출되지 않음
- timeout/401/429/5xx 처리와 안전한 retry/backoff
- snapshot 중복 upsert
- partial failure 시 cursor를 잘못 전진시키지 않음
- native와 external ID 충돌 없음
- transcript/adapterConfig.env 필드 drop
- stale 상태와 마지막 정상 snapshot 유지

UI:
- 계층 탐색과 provider badge
- routine 다음 실행 시간
- permission에 따라 버튼 disabled/hidden이지만 직접 API 호출도 거부됨
- external stale badge
- 기존 Agent Office, Calendar, Radar 회귀 없음

### H. 운영·문서·검증

호스팅 문서에 다음 topology를 명시한다.

- Vercel: Workmateboard UI/API, cron tick
- PostgreSQL: native 관리 데이터와 external projection
- 로컬/VPS: Paperclip/Codex와 paperclip-bridge
- outbound bridge → HTTPS Workmateboard machine API

로그:

- routine cycle counts
- permission denial: actor/capability/resource id, 원문 payload 없음
- external sync: provider/company, counts, duration, safe status
- external failure: HTTP status/exception class, URL credential/path/query
없음

전체 pytest, Ruff, mypy, Playwright, graphify update를 실행한다. bridge는 fake
Paperclip server 또는 MockTransport로 검증하고 실제 credential이나 운영 서비스에 호출하지 않는다.

완료 조건:

- goal에서 실제 task/run/artifact까지 추적 가능하다.
- routine이 중복 없이 task를 생성하고 기존 wake 흐름을 사용한다.
- 위험 capability는 서버에서 deny되고 승인 없이는 실행되지 않는다.
- Paperclip agents/tasks/runs/cost summary가 안전하게 투영돼 Agent Office에 표시된다.
- Paperclip 장애 시 Workmateboard native 기능은 계속 동작한다.
- 1·2차 및 기존 전체 테스트가 통과한다.

완료 후 다음 커밋을 만든다.

git commit -m "feat: add goals routines permissions and integrations"
```

---

## 전체 프로그램 최종 검증 프롬프트

3차까지 완료한 뒤 별도의 리뷰 에이전트 또는 새 컨텍스트에서 다음 프롬프트를 실행한다.

```text
Workmateboard의 Paperclip-inspired agent management 1·2·3차 구현을 전체 검토하라. 기능을
추가하지 말고 결함을 찾아 수정·검증하라.

검토 대상:

1. task/run/stage/session 경계가 코드와 DB에서 유지되는가
2. 상태 전이가 원자적이고 동시 요청에 안전한가
3. idempotency가 application check가 아니라 UNIQUE 제약으로도 보장되는가
4. SQLite/PostgreSQL schema와 Store 동작이 일치하는가
5. Vercel 경로가 장시간 프로세스를 시작하지 않는가
6. API key/token/transcript/user message가 로그·audit·API에 노출되지 않는가
7. 비용 unknown이 0으로 계산되지 않는가
8. 예산 hard stop이 올바른 scope만 멈추는가
9. approval 결정과 대상 상태 변경이 transaction으로 일치하는가
10. watchdog/routine/bridge 재시도가 중복 실행을 만들지 않는가
11. permission deny가 UI뿐 아니라 서버에서 강제되는가
12. external Paperclip 장애가 native Workmateboard 실행을 방해하지 않는가
13. Agent Office가 demo와 실제 데이터를 섞지 않는가
14. 기존 Calendar/Radar/Auth/Security 기능에 회귀가 없는가

필수 공격·실패 테스트:

- 동일 task 동시 checkout 20회
- 동일 wake/cost/snapshot key 동시 재전송
- 종료 agent wake
- stale heartbeat와 늦게 도착한 정상 heartbeat race
- budget threshold 직전의 동시 run reservation
- approve/reject 동시 요청
- malicious summary/comment/artifact URI
- secret-like key가 든 approval/external payload
- Paperclip 401/429/500/timeout/invalid JSON
- cron tick 중간 실패 후 재실행
- PostgreSQL에서 NULLS LAST와 transaction behavior

검증:

- uv run pytest
- uv run ruff check src/jobmate tests
- uv run ruff format --check src/jobmate tests
- uv run mypy src/jobmate
- UI 테스트용 Chromium이 있으면 uv run pytest tests/ui -v
- graphify update .

모든 검증 결과를 정확히 기록하라. 환경 때문에 실행하지 못한 항목을 통과했다고 말하지 마라. 발견된 결함은 최소 변경으로 수정하고
회귀 테스트를 먼저 추가하라. 최종적으로 변경 파일, 테스트 결과, 남은 위험, 운영 전 필수 설정을 보고하라.
```

## 단계별 완료 체크리스트

### 1차

- [ ] 영구 agent와 ephemeral agent_session이 분리됐다.
- [ ] task와 run이 1:N으로 연결된다.
- [ ] checkout과 wake가 원자적·멱등이다.
- [ ] heartbeat가 실제 Agent Office 상태를 바꾼다.
- [ ] Vercel에서 로컬 CLI를 실행하지 않는다.
- [ ] 전체 검증과 문서 갱신이 끝났다.

### 2차

- [ ] reported/estimated/unpriced 비용이 구분된다.
- [ ] 예산 warning/hard stop과 정확한 scope 정지가 동작한다.
- [ ] 범용 승인함이 기존 run 승인을 깨뜨리지 않는다.
- [ ] watchdog가 stranded/stale 상태를 중복 없이 감지한다.
- [ ] 위험 작업은 자동 재시도되지 않는다.
- [ ] 전체 검증과 문서 갱신이 끝났다.

### 3차

- [ ] Goal → Project → Task → Run → Artifact 경로가 조회된다.
- [ ] Routine이 시간대와 재시도에도 중복 task를 만들지 않는다.
- [ ] 권한은 서버에서 deny 우선으로 강제된다.
- [ ] Paperclip projection이 native 데이터를 오염시키지 않는다.
- [ ] 외부 장애 중에도 native 기능이 유지된다.
- [ ] 전체 검증과 운영 topology 문서가 완성됐다.

## 구현 순서에 대한 최종 권고

각 차수를 별도 브랜치 또는 연속된 작은 커밋으로 진행한다. 1차가 실제 Agent Office의 핵심 가치이므로 먼저 운영 가능한
수직 흐름을 완성하고, 2차에서 안전장치를 넣은 뒤, 3차에서 조직·자동화·외부 연동을 확장한다. 1차가 검증되기 전에 2·3차
스키마를 미리 추가하지 않는다.
