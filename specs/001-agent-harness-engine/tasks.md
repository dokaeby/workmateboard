# Tasks: jobMate 에이전트 하네스 엔진 MVP

**Input**: Design documents from `/specs/001-agent-harness-engine/`
**Prerequisites**: plan.md, spec.md

## Phase 1: Setup (Shared Infrastructure)

- [ ] T001 pyproject.toml + uv 프로젝트 스캐폴드 (src/jobmate 패키지, dev deps: pytest, pytest-asyncio, ruff, 타입체커)
- [ ] T002 [P] ruff/mypy 설정 (pyproject 내)
- [ ] T003 [P] config.py — pydantic-settings: DB 경로, 모델 티어(기본/중요), 역할별 모델 오버라이드, 예산 기본값
- [ ] T004 [P] models/ — Run, StageRun, Approval, AuditEvent, Verdict, Finding(gstack 스키마), ChangeProposal, EventLink, Notification, WorkflowSpec, StageSpec, AgentSpec, ModelConfig
- [ ] T005 store/ — SQLite 스키마(runs, stage_runs, audit_events, approvals, event_links, notifications) + Store Protocol + SqliteStore 구현(WAL, FK)
- [ ] T006 sensors/ — verdict 스키마 검증, approve+blocker/major 강등, diff 형식 검사, 시크릿 패턴 스캔, Gmail URL 허용목록, timezone 검증
- [ ] T007 llm/ — LLMClient Protocol, OpenAI호환 클라이언트(httpx, 타임아웃, 재시도), FakeLLM, 구조화 출력(JSON 추출+Pydantic 검증)
- [ ] T008 budgets.py — 토큰/도구 호출/시간/회귀 상한(리뷰 2, 전체 3) 트래커 + 안전 정지 판정
- [ ] T009 guide/ — prompts/ 프롬프트 파일 + 로더(버전 주석), 에이전트 역할별 프롬프트 매핑
- [ ] T010 tools/ — ToolExecutor: allowlist 강제, read_diff/run_tests 등 도구 구현 + 결과 크기 제한

**Checkpoint**: 기반 모듈 단위 테스트 가능 상태

---

## Phase 2: Foundational — 엔진 코어

- [ ] T011 engine/spec.py — WorkflowSpec/StageSpec 파싱·검증, 분기 규칙 모델
- [ ] T012 engine/runner.py — LangGraph StateGraph 생성(spec→노드), stage 실행 래퍼(시도/재시도/기록), interrupt 승인 노드
- [ ] T013 engine/state.py — HarnessState(Pydantic): stage 출력 누적, 회귀 카운터, 판정 이력
- [ ] T014 engine/agents.py — llm 단계 실행기: 프롬프트 조립(신뢰/비신뢰 구분), 출력 스키마 검증, 재시도, usage 기록
- [ ] T015 checkpoint 연동 — SqliteSaver/커스텀 store로 완료 stage 재실행 방지 + 재시작 재개
- [ ] T016 분기 규칙 엔진 — verdict→next stage 결정(구현 회귀/진행/needs_human), 상한 초과 시 LLM 호출 없이 에스컬레이션

**Checkpoint**: spec만으로 임의 워크플로를 LangGraph로 실행 가능

---

## Phase 3: User Story 1 — dev_review 파이프라인 (P1) 🎯 MVP

**Goal**: 분석→구현(변경제안)→자동검사→보안검토→코드리뷰→QA→사람승인 전체 흐름

**Independent Test**: fake LLM으로 전체 실행 → stage_runs 순서·분기·승인 게이트 검증

- [ ] T017 [US1] workflows/dev_review.py — WorkflowSpec 정의(7단계+분기), AgentSpec 5종(분석/구현/보안/리뷰/QA)
- [ ] T018 [US1] 분석 에이전트 — 요구사항/설계 분석 출력 스키마
- [ ] T019 [US1] 구현 에이전트 — ChangeProposal(diff) 출력, 피드백 포함 회귀 입력
- [ ] T020 [US1] 자동 검사 function 단계 — diff 형식/시크릿 스캔/설정 명령 실행, 결과를 다음 단계 입력에 첨부
- [ ] T021 [US1] 보안검토 에이전트 — 증거 우선 verdict(attack/boundary/impact 필드), diff 범위 한정
- [ ] T022 [US1] 코드리뷰 에이전트 — 독립 입력(diff+명세+검사결과), gstack findings 스키마, confidence 게이트, NO FINDINGS 허용
- [ ] T023 [US1] QA 에이전트 — 검사 결과+findings 해소 여부+요구사항 종합 → pass/fail/needs_human
- [ ] T024 [US1] 사람 승인 + PR 요약 산출물(What changed/Blast radius/Verification Results/Tests)
- [ ] T025 [P] [US1] tests/unit — sensors, budgets, verdict 강등, findings 파싱
- [ ] T026 [P] [US1] tests/integration — fake LLM 전체 흐름, 회귀, 상한, 재시작 재개, 승인/거부

**Checkpoint**: US1 fake end-to-end 통과

---

## Phase 4: User Story 2 — gmail_calendar 워크플로 (P2)

**Goal**: 폴링→추출→결정적 검증→멱등 Calendar 등록→알림, 동일 엔진 stage/run/audit

**Independent Test**: fake Gmail/Calendar/LLM으로 메시지 1건 discovered→notified + 중복 방지

- [ ] T027 [US2] adapters/gmail.py Protocol + fake — 메시지 목록/본문 조회(본문은 비신뢰 데이터)
- [ ] T028 [US2] adapters/calendar.py Protocol + fake — 이벤트 생성/검색/marker 조회
- [ ] T029 [US2] adapters/notifier.py Protocol + fake — 알림 큐잉/발송 상태
- [ ] T030 [US2] 추출 에이전트 — 일정 후보 구조화 출력(제목/시작/종료/tz/신뢰도/모호성/반복)
- [ ] T031 [US2] 검증 function 단계 — timezone/DST/종료>시작/모호·반복→needs_review, 멱등키 계산
- [ ] T032 [US2] 등록 function 단계 — pending 예약→marker 조회→생성→event_id 저장→알림 큐, 타임아웃 시 marker 복구
- [ ] T033 [US2] workflows/gmail_calendar.py — WorkflowSpec + 분기(needs_review/duplicate/connection_error/retry_wait)
- [ ] T034 [P] [US2] tests/unit — 검증 센서, 멱등키, safe URL, timezone 엣지
- [ ] T035 [P] [US2] tests/integration — 전체 흐름, 중복, 타임아웃 복구, 알림 실패 시 이벤트 유지

**Checkpoint**: US2 fake end-to-end + 멱등성 검증 통과

---

## Phase 5: User Story 3 — FastAPI + CLI + 상태 페이지 (P3)

**Goal**: run 제출/조회/승인/거부, 알림·이벤트 이력, 자동화 토글/수동 폴링, 최소 상태 화면

**Independent Test**: TestClient로 제출→폴링→승인 HTTP 완결

- [ ] T036 [US3] api/app.py — FastAPI 앱, /api/v1 라우터, 에러 형식 {"error":{code,message,request_id}}, 202 비동기 실행
- [ ] T037 [US3] api/routes — runs CRUD/승인, notifications, event-links, gmail automation toggle/poll
- [ ] T038 [US3] api/static — 상태 페이지(미니멀/스위스: 단일 액센트, 그리드·여백, 그림자·그라디언트 배제, 상태 텍스트 병행, 반응형 테이블, 접근성)
- [ ] T039 [US3] cli.py — run submit/status/approve/reject, gmail poll, dev 서버 기동
- [ ] T040 [P] [US3] tests/api — 엔드포인트 계약, 검증 에러, 승인 흐름

**Checkpoint**: HTTP로 전체 사용자 여정 완결

---

## Phase 6: 문서 + 적대적 리뷰 + 검증

- [ ] T041 docs/jobMate_v1.0.1/01·02 갱신 — 엔진 중심 설계, dev_review+보안 단계, 모델 라우팅, FastAPI 임시/Django 전환, UI 토큰 명시
- [ ] T042 docs/jobMate_v1.0.1/08_단위_test_cases_v1.0.1.md 부록 A — unit/integration/api/security/idempotency/recovery 상세 케이스
- [ ] T043 docs/jobMate_v1.0.1/18_화면설계서_v1.0.1.md 부록 A — ui-ux-pro-max 토큰·컴포넌트·접근성 기준
- [ ] T044 적대적 리뷰 수행(gstack 방식: 스페셜리스트 렌즈+레드팀, severity/confidence/evidence) → docs/jobMate_v1.0.1/02_시스템상세설계서_v1.0.1.md 부록 B
- [ ] T045 리뷰 blocker/major 수정 → 재리뷰(최대 루프)
- [ ] T046 pytest + ruff + 타입체크 전체 통과 확인, 실패 시 수정
- [ ] T047 graphify update . (graphify-out 존재 시) + 최종 보고

**Checkpoint**: 모든 테스트 통과 + 리뷰 해소 + 문서 완비

---

## Dependencies & Execution Order

- Phase 1 → Phase 2 순차 (엔진이 기반 모듈 의존)
- Phase 3(US1) → Phase 4(US2)는 독립 가능하나 store/engine 재사용으로 순차 권장
- Phase 5(US3)는 US1/US2 완료 후
- Phase 6은 전체 완료 후
- 테스트는 구현 전 작성 권장(실패 확인 → 구현) — fake 주입으로 결정적 유지

## Parallel Opportunities

- T003/T004/T005 등 [P] 기반 모듈 병렬
- T025/T026, T034/T035, T040 테스트 병렬 작성 가능
- docs 작업(T041-T043)은 구현과 병렬 가능
