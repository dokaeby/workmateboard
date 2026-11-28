# Spec 009 — OTLP 시그널 확장 (메트릭·로그)

## 배경

spec 008 US1에서 OTLP traces 인제스트를 완료했다. 같은 ingest 토큰 체계로
metrics·logs 시그널도 수집해 외부 계측의 커버리지를 넓힌다. OTLP gRPC는
의존성/인프라 비용이 커서 계속 보류(HTTP/JSON만).

## User Stories

### US1 — OTLP 메트릭 인제스트
- `POST /api/v1/otlp/v1/metrics` — OTLP/HTTP JSON metrics 수집.
  인증은 traces와 동일(Bearer/`x-jobmate-ingest-key` → otlp_ingest_tokens).
- 파싱: resourceMetrics → scopeMetrics → metrics[] → gauge/sum/histogram
  데이터포인트. gauge는 value 직행, sum은 (value, isMonotonic, temporality),
  histogram은 count/sum/bucket 합산값만 저장(버킷 원문은 attributes에 요약).
- `external_metrics` 테이블 — metric_name/type/value/unit/attributes/timestamp.
- 가드: 요청당 최대 데이터포인트 수, 기존 body cap/레이트리밋/민감키 필터 재사용.
- `GET /api/v1/observability/external-metrics?days&metric_name&limit` — lead+.

### US2 — OTLP 로그 인제스트
- `POST /api/v1/otlp/v1/logs` — OTLP/HTTP JSON logs 수집(동일 인증).
- 파싱: resourceLogs → scopeLogs → logRecords[] → timeUnixNano, severityNumber/
  severityText, body(AnyValue→문자열), attributes, traceId/spanId.
- `external_logs` 테이블 — trace/span 링크로 트레이스 상관관계 유지.
- `GET /api/v1/observability/external-logs?days&trace_id&severity&limit` — lead+.
- 프론트: 트레이스 뷰 또는 관측 센터에 외부 메트릭/로그 섹션(간단 리스트).

## Non-Goals
- OTLP gRPC, protobuf 바이너리, metrics→Prometheus remote write 변환
- 로그 본문의 전문 검색/필터 DSL
- 알림 연동(외부 메트릭 임계 알림은 추후)

## 공통 요구사항
- traces 인제스트와 동일한 보안 모델(토큰 해시, rate limit, 민감키 제거,
  org 스코프, run_in_threadpool)
- 신규 테이블 org 스코프 등록, i18n, pytest+vitest, 전체 스위트 통과
