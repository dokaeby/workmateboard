# Spec 008 — 관측성 확장 (외부 계측 수집 · 온라인 평가 · 리포트 전달)

## 배경

spec 007에서 관측성 플랫폼(트레이스/비용/에러/승인/이상 탐지/알림 규칙 UI)을 완료했다. 남은 후속 3개:

1. **외부 계측 유입(OTLP)** — jobMate 밖에서 실행되는 에이전트/LLM 호출을 관측하려면 OpenTelemetry OTLP 수집 엔드포인트가 필요하다. spec 007 Non-Goal로 명시적 유보.
2. **온라인 평가(online evals)** — 프로덕션 트래픽(run/stage 결과)을 eval 파이프라인에 자동 연결. 기존 `evals` 테이블/기능은 오프라인 중심.
3. **스케줄 리포트 전달** — `reports` 테이블(일간/주간 리포트 생성)은 있으나 사용자에게 전달하는 UI/스케줄 구독이 없다.

벤치마크: Langfuse OTLP ingest, Phoenix evals, Datadog scheduled reports.

## User Stories

### US1 — OTLP 인제스트 엔드포인트
- `POST /api/v1/otlp/v1/traces` — OTLP/HTTP JSON 프로tobuf 대체 형식 수용(기존 의존성 최소화: `opentelemetry-proto` 없이 JSON 구조 파싱)
- 인증: 전용 ingest 토큰(`otlp_ingest_tokens` 테이블, org 스코프, 해시 저장, rotate) — `Authorization: Bearer` 또는 `x-jobmate-ingest-key`
- 수용 범위: spans의 `gen_ai.*` / `llm.*` semantic convention 속성(model, tokens, cost) → `external_spans` 테이블(org_id, trace_id, span_id, parent_span_id, name, kind, started/ended, attributes JSON, run_id nullable 링크)
- 크기/속도 가드: 요청당 최대 span 수, 속도 제한, 비동기 배치 insert
- jobMate 자체 run과 연결: span attributes의 `jobmate.run_id`가 있으면 run 타임라인에 외부 스팬으로 표시(트레이스 뷰 확장)
- `/api/v1/otlp/tokens` CRUD(lead 이상) — 발급/폐기/마지막 사용 시각

### US2 — 온라인 평가 (online evals)
- `eval_schedules` 테이블: `{name, target: 'run_completed'|'stage_completed', workflow?, sample_rate(0-1), eval_name, enabled, org_id}`
- run/stage 완료 시 sample_rate 확률로 기존 eval 파이프라인 실행(`services/evals` 재사용), 결과를 `eval_runs`에 `source='online'`으로 기록 + 불합격 시 inbox 알림(규칙 기반, 중복 방지)
- `/api/v1/evals/schedules` CRUD + `/api/v1/evals/schedules/{id}/history`
- UI: evals 뷰에 "온라인 평가" 섹션 — 스케줄 목록/생성 폼/최근 결과

### US3 — 스케줄 리포트 전달
- `report_subscriptions` 테이블: `{kind: daily|weekly, channel: email|slack|webhook, target(이메일/웹훅URL), enabled, org_id, last_sent_at}`
- reports 생성 주기(jobs.py)에 구독별 발송 훅 — email은 기존 mailer, slack/webhook은 spec 004 웹훅 전달 재사용(HMAC 서인)
- `/api/v1/reports/subscriptions` CRUD + `POST /{id}/test`
- UI: 비용/인사이트 뷰에 "리포트 구독" 섹션

## Non-Goals
- OTLP gRPC 수집(HTTP/JSON만), 일반 OTel 메트릭/로그 수집
- 온라인 eval의 LLM-judge 자동 생성(기존 eval 정의 재사용만)
- 리포트 커스텀 템플릿 에디터

## 공통 요구사항
- 모든 신규 엔드포인트/테이블 org 스코프, lead/admin 게이팅 기존 패턴 준수
- 신규 테이블 schema_sqlite/schema_pg 양쪽 + _ORG_SCOPED_TABLES 등록
- 시크릿(ingest 토큰 원문, 웹훅 URL 자격증명)은 해시/암호화 저장, 로깅 금지
- i18n ko/en 키 등록, pytest + vitest, 전체 스위트 통과
