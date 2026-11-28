# Spec 007 — 관측성 플랫폼 (Observability Platform)

## 배경

Agent Office는 원래 "LLM/agent 사용의 모든 상황(비용·오류·실행상태)을 모니터링"하기 위한 것이나, 현재 UI는 dev_review run 하나를 장식적 3D 스프라이트로 보여주는 수준이다. 백엔드에는 `stage_runs`(모델/토큰/지연/에러), `cost_events`(stage 단위 원장), `llm_stats`(p50/p95/p99), alert_rules, webhook, Prometheus 메트릭 등 상용급 데이터 자산이 이미 존재한다. 이 spec은 그 데이터를 상용수준 UI로 노출하고 부족한 분석 계층을 채운다.

벤치마크: Langfuse(trace/session/cost), Phoenix(OTel), Datadog LLM Obs, AgentOps(replay), Portkey(cost governance). HITL 승인 상태를 실행 차원으로 보여주는 제품은 시장에 없음 — jobMate 고유 차별점.

## User Stories

### US1 — Run 타임라인(트레이스) 뷰
- run 1건의 stage_runs + cost_events를 스팬 트리/waterfall로 표시
- 노드별: stage_key, attempt, model, prompt_version, tokens_in/out, cost_usd, duration, status, error
- GET `/api/v1/runs/{id}/timeline` 신규 — stage+cost 조인, waterfall용 started/finished
- UI: runs 상세 또는 전용 뷰에서 좌측 트리/상단 Gantt, 클릭 시 입출력·에러 상세

### US2 — 비용·사용량 대시보드 상용화
- `/api/v1/observability/costs?days&group_by=model|project|workflow|agent|day` — cost_events 기반 그룹별 집계(cost_microusd 합, 토큰, calls, run 수)
- `/api/v1/observability/usage` — llm_stats 재사용 + 세션 토큰 합산
- UI: 기간 선택, 그룹별 바/라인 차트, top-N 테이블, 예산 대비 사용률 게이지

### US3 — 에러 분류 체계
- `stage_runs.error_class`, `runs.error_class` 컬럼 추가(SQLite/PG 스키마 + 마이그레이션)
- 분류기 `services/error_taxonomy.py`: timeout, rate_limit, auth, tool_error, schema/parse, budget, unknown — 에러 문자열 패턴 매칭, runner 실패 기록 시 자동 분류
- `run.metadata.cost_usd` 누적 기록 수정(alert_rules의 run.cost_usd 조건이 dead인 버그)
- `/api/v1/observability/errors?days` — error_class별 건수/비율/최근 샘플

### US4 — 알림 규칙 확장 + 관리 UI
- 조건 필드 추가: `run.latency_ms`(duration), `error.error_class`, `error.rate_pct`(기간 내), `session.stale_count`, `cost.delta_pct`(전주 대비)
- alert_rules 평가 엔진 확장(services/alert_rules.py) + 신규 이벤트 발생 지점
- v2 알림 규칙 관리 UI: 규칙 CRUD 목록, 채널/조건 폼, 발화 이력(alert-fires)

### US5 — 승인 인지 뷰 + Replay
- `/api/v1/observability/approvals` — 대기 승인 큐 + 대기 시간 통계 + 최근 결정 이력(승인자/소요시간)
- UI: 승인 대기 run 카드(프로젝트/단계/대기시간/경과), 클릭 시 diff 뷰로 연결
- Run replay: 완료 run의 stage 타임라인을 순차 재생(프론트 전용, timeline 데이터 재사용) — 각 단계의 입출력/에러를 시간순 탐색

### US6 — Agent Office → 상용 관측 대시보드
- 기존 스프라이트/3D 보드 UI 제거(v2/src/features/office 스프라이트·board 코드)
- office 뷰를 "관측 대시보드"로 재구성: 실시간 run 상태 보드(SSE 연동), 활성 세션, 비용 스파크라인, 에러 알림, 승인 대기 위젯
- monitor/cost/insights 기존 뷰는 유지하되 office가 관측 허브 역할

### US7 — 이상 탐지-lite
- `services/anomaly.py`: automation cycle에서 비용 일별/에러율 전주 대비 ±임계값 초과 시 inbox 알림(중복 방지 fingerprint)
- `/api/v1/observability/anomalies` — 최근 이상 이벤트 목록

## Non-Goals
- OTLP 인제스트, 외부 SDK 계측 (후속)
- 온라인 LLM-judge eval (evals 기존 기능과 별개)
- 완전한 anomaly detection/ML (규칙 기반 임계값만)

## 공통 요구사항
- 모든 신규 엔드포인트 org 스코프 적용(_org_filter), lead/admin 게이팅은 기존 패턴 준수
- 신규 테이블은 schema_sqlite/schema_pg 양쪽 + _ORG_SCOPED_TABLES 등록
- i18n ko/en 키 등록 (feature i18n.ts 패턴)
- 테스트: 각 US별 pytest + 프론트 vitest, 전체 스위트 통과
