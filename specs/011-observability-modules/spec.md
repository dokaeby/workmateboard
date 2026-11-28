# spec 011 — 관제센터 모듈 (Agent Office 목업 반영)

## 목표

`~/Downloads/am/` 목업의 관제센터 구성을 기존 관측 데이터로 구현한다:
KPI delta, 실행 퍼널, 알림 대시보드 집계, Agent Swarm 그래프 데이터,
라이브 에이전트 상태 리스트. UI 가이드는 `docs/UI_DESIGN_GUIDE.md`.

## US1: 백엔드 집계 엔드포인트

`src/jobmate/api/routers/observability.py`(또는 신규 모듈)에 추가.
모두 기존 인증/lead 게이트 패턴을 따른다.

### `GET /api/v1/observability/notifications?days=7`

알림함/notification 집계를 반환:

```json
{"days": 7, "total": 42, "open": 14, "resolved": 28,
 "series": [{"t": "2026-11-28T00:00:00Z", "count": 3}],
 "recent": [{"id": "...", "title": "...", "created_at": "...", "status": "open"}]}
```

- 데이터 소스: notifications/inbox 테이블 중 알림 성격의 것.
  심각도가 저장돼 있으면 by_severity 포함, 없으면 생략(무리한 추정 금지).

### `GET /api/v1/observability/funnel?days=7`

실행 파이프라인 단계별 도달 건수:

```json
{"days": 7,
 "stages": [{"key": "received", "label": "요청 수신", "count": 128},
            {"key": "planned", "label": "계획 수립", "count": 124},
            {"key": "assigned", "label": "작업 분배", "count": 124},
            {"key": "executed", "label": "실행", "count": 37}]}
```

- stage_runs/run 상태에서 유도: received=runs 생성, planned=첫
  비엔트리 stage 진입, assigned=실행 stage 진입, executed=성공 종료.
  워크플로별 차이가 크면 `workflow` 파라미터 추가 허용.

### `GET /api/v1/observability/agents/live`

활성 에이전트/세션 카드 데이터:

```json
{"count": 24,
 "items": [{"id": "...", "name": "...", "role": "...", "model": "...",
   "status": "running", "current_task": "...", "progress_pct": 82,
   "started_at": "...", "elapsed_s": 123, "tokens": 15200,
   "cost_usd": 0.12, "tool_calls": 8, "sub_agents": 2,
   "last_heartbeat_at": "..."}]}
```

- 데이터 소스: agent_sessions + stage_runs + llm_usage 집계.
  status는 §5.4 매핑표(running/planning/thinking/executing_tool/
  waiting/completed/failed/blocked/paused/idle)에 맞춘다.

### `GET /api/v1/observability/swarm?run_id=&days=`

노드/엣지 그래프 데이터:

```json
{"nodes": [{"id": "stage:abc", "label": "분석", "kind": "stage",
   "agent": "analyst", "status": "completed", "progress_pct": 100,
   "tokens": 0, "latency_ms": 0, "error_count": 0}],
 "edges": [{"source": "...", "target": "...", "kind": "sequence"}]}
```

- run 지정 시 해당 run의 stage들을 노드로, 실행 순서/분기를 엣지로.
- days 모드는 최근 활성 run들의 stage를 시간대별 클러스터.
- 노드 수 상한(예: 100) 초과 시 그룹 노드로 축약.

## US2: 프론트엔드 — 관측 센터 확장

`features/office/`에 추가(기존 섹션 구조 유지):

- **KpiCard delta**: value 아래 `sub`에 `+N%` 증감 표시(전일/전주 대비).
  데이터 없으면 미표시. `tabular-nums`.
- **FunnelSection**: 단계별 수평 바(건수+비율), 최근 run 링크.
- **NotificationDashboard**: KPI(total/open/resolved) + 발생 추이
  미니 차트 + 최근 알림 리스트. 알림함 뷰 링크.
- **LiveAgents**: 목업의 에이전트 상태 리스트 — 이름/모델/역할/상태 배지/
  진행률 바/경과 시간/토큰/비용. 클릭 시 run 또는 세션 상세로 이동.
- **SwarmGraph**: `/observability/swarm` 데이터를 노드-엣지로 렌더.
  라이브러리: `reactflow`(또는 경량 자체 SVG — 노드<50 수준이면 SVG가
  가볍다. 도입 시 package.json 확인·최신 안정판). 상태색 테두리,
  실행 중 노드 pulse, 엣지 방향 화살표. 줌/팬/핏 최소 지원.
  데이터 없으면 Empty 상태.

## 비목표

- 실시간 WebSocket — 기존 SSE/폴링 재사용
- MCP 모니터, 보안 센터 전용 뷰(별도 spec)
- 스웜의 자유 편집/드래그 배치

## 수용 기준

- 백엔드: 신규 엔드포인트 테스트 포함, `pytest`/`ruff`/`mypy` 통과
- 프론트: `tsc`/`oxlint`/`vitest`/`build` 통과
- 가짜 지표 없음 — 데이터 없으면 Empty/"-" 표시
