# spec 012 — 세션 필드 확장 + Tool 통계 + 조회 필터

## 목표

① 라이브 에이전트 카드의 null 필드(model/progress/cost/tool_calls/
sub_agents)를 채울 수 있게 스키마·수집 경로를 확장.
② Tool Call 통계 엔드포인트+관측 센터 섹션.
③ runs/external-logs 조회 필터(백로그 P0-2, P0-1의 경량판).

## US1: agent_sessions 선택 컬럼 추가

- 스키마(schema_sqlite.py + schema_pg.py)에 nullable 컬럼 추가:
  `model TEXT`, `progress_pct REAL`, `cost_usd REAL`,
  `tool_calls INTEGER`, `sub_agents INTEGER`
- migrations.py에 기존 DB용 ALTER 보강(IF NOT EXISTS 패턴).
- `upsert_agent_session` 페이로드에서 위 필드를 받아 저장.
  브릿지가 안 보내면 null 유지 — 추정값 생성 금지.
- `/observability/agents/live` 응답에 그대로 노출
  (현재 null로 하드코딩된 부분을 실제 컬럼으로 교체).

## US2: Tool Call 통계

`GET /api/v1/observability/tool-stats?days=7`

데이터 소스: `external_spans`(OTLP 인제스트 span — name/kind/
attributes/started_at/ended_at) + 필요시 stage_runs.

```json
{"days": 7,
 "total_calls": 1234,
 "by_name": [{"name": "file.read", "kind": "tool", "count": 400,
              "error_count": 3, "avg_duration_ms": 12.5}],
 "by_kind": [{"kind": "tool", "count": 800}, {"kind": "llm", "count": 434}],
 "error_rate": 0.02}
```

- kind가 비어 있으면 attributes/ name prefix로 추정하지 말고
  "" 그룹으로 둔다.
- 상위 N개(50) + 나머지 "기타" 집계. 오류 판정: attributes 내
  error/status 코드가 있으면 사용, 없으면 error_count=0.
- lead 게이트 동일 적용.

## US3: 조회 필터

- `GET /api/v1/runs`에 쿼리 파라미터 추가: `status`, `days`,
  `q`(workflow/제목 부분일치), `agent`. 기존 limit/workflow 유지,
  파라미터는 AND 조건. days는 1~365 클램프.
- `GET /api/v1/observability/external-logs`에 `q` 파라미터 추가:
  body/severityText 부분일치(LIKE, `%`/`_` 이스케이프, 상한 바운드).
  FTS5는 SQLite 전용이라 PG 호환성을 위해 LIKE 사용.

## 프론트엔드 (같은 spec)

- office: ToolStatsSection — by_kind 비중 + by_name 테이블
  (name/kind/count/error/avg duration), days 선택.
- runs 뷰: status/days/q/agent 필터 UI → 쿼리 파라미터로 전달.
- telemetry 뷰(office/telemetry.tsx): external-logs 검색창 → q 파라미터.
- 라이브 에이전트 카드는 필드가 채워지면 자동 표시(추가 작업 최소).

## 비목표

- FTS5 전문 검색 인덱스(PG 마이그레이션 복잡 — LIKE로 시작)
- 로그 실시간 테일(SSE), 알림 규칙 확장(별도 spec)
- 브릿지 측 전송 구현(스키마/수신만 — 브릿지는 외부 프로젝트)

## 수용 기준

- 백엔드 pytest 통과(신규 테스트: 컬럼 마이그레이션, upsert 필드,
  tool-stats 집계, runs 필터, logs q 검색)
- 프론트 tsc/lint/vitest/build 통과
- null 필드는 계속 null — 추정 금지
