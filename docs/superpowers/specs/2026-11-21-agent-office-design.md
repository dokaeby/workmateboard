# Agent-Office 관제판 + 코딩 에이전트 기능 제거 — 설계

날짜: 2026-11-21
상태: 승인 대기

## 의도와 이유

jobMate 대시보드를 "하이브리드 3D 관제보드"로 재편한다. 기존 "코딩 에이전트"
기능(독립 코딩 job 큐 + CLI/내장 에이전트 실행 UI)은 제거하고, 그 메뉴 자리에
"Agent-Office"를 둔다. Agent-Office는 dev_review 워크플로의 실제 stage_runs를
ANA/DEV/TEST/SEC/CR/QA/HUM/ORCH 8개 노드의 입체 관제판으로 시각화한다.

사용자 결정 사항:
- 코딩 기능은 **전체 삭제** (tools/coding.py, agent 루프, coding-worker, DB 큐 포함).
  dev_review implement 단계는 코딩 툴 없이 일반 LLM 응답으로 동작하게 한다.
- 데이터 소스는 **dev_review run만**.
- 사이드바는 코딩 에이전트 자리를 **Agent-Office로 교체** (메뉴 5개 유지).

## 목표 / 비목표 / 안티목표

목표:
- 코딩 에이전트 관련 코드·API·UI·테스트·설정 완전 제거.
- Agent-Office 뷰: CSS perspective + SVG 곡선 기반 입체 플랫폼, DOM 노드 8개,
  선택 노드 상세 패널, run audit 기반 실시간 이벤트 로그.
- 노드 상태는 실제 stage_runs/run 상태를 반영.

비목표:
- dev_review 워크플로 자체 변경 (stage 구성·분기 그대로).
- WebGL/Three.js 등 외부 3D 라이브러리 도입.
- 다른 뷰(Gmail/Calendar/SkillRadar/실행 이력) 변경.
- 관제판에서 run 생성·승인 실행 같은 제어 기능 (읽기 전용; 승인 버튼은 아래 "내 판단" 참고).

안티목표:
- 삭제 후 dev_review implement가 깨지는 것.
- 목업 데이터로 관제판을 채우는 것 — 반드시 실제 run 데이터만 표시.

## 제약

- 기존 UI 패턴 준수: 단일 index.html, vanilla JS, `api()` 헬퍼, 주기적 폴링.
- 테스트: `uv run pytest` 전체 통과, ruff/mypy clean.
- 비밀값 로깅 금지 등 docs/jobMate_v1.0.1/17_운영_보안_가이드_v1.0.1.md 부록 A 준수.

## 접근 방식 (검토한 대안)

1. **기존 엔드포인트 재사용 (채택)** — `/api/v1/runs`로 dev_review run 목록을 얻고
   `/api/v1/runs/{id}`의 stages+audit으로 관제판을 구성. 백엔드 변경 최소,
   기존 UI 폴링 패턴과 동일.
2. 전용 `/api/v1/office` 집계 엔드포인트 — 한 번의 호출로 모든 데이터 제공.
   기각: runs/{id} 응답이 이미 stages+audit+approvals를 포함해 중복.
3. SSE/WebSocket 실시간 — 기각: 기존 UI가 폴링 기반, 인프라 추가 불필요.

## 구현 내용

### A. 코딩 기능 제거

삭제 파일:
- `src/jobmate/coding_worker.py`
- `src/jobmate/agent/` (loop.py, tools.py, __init__.py) — CodingAgent 전용
- `src/jobmate/tools/coding.py`
- `src/jobmate/sandbox.py` — coding_worker/agent loop 전용

수정 파일:
- `src/jobmate/api/app.py`: `/api/v1/coding/*` 6개 엔드포인트, `CodingRunRequest`,
  `CodingJobWorker` import/생성/start/stop 제거. `coding_tool`/`coding_model`
  prefs 키 제거.
- `src/jobmate/api/serverless.py`: coding 참조 없음 확인 — 변경 불필요.
- `src/jobmate/tools/__init__.py`: `CODING_TOOLS`, `CodingTool`,
  `CodingToolError`, `CodingToolResult`, `build_coding_tool`, `workspace_diff`
  re-export 제거. `ToolExecutor`는 dev_review function stage가 사용 — 유지.
- `src/jobmate/store/db.py`: `coding_jobs` 스키마가 **두 곳** (SQLite `SCHEMA`,
  PostgreSQL `SCHEMA_PG`) + `_migrate_coding_jobs()`/`_CODING_JOB_NEW_COLS`
  마이그레이션(`__init__`에서 무조건 호출, fresh DB에서 index 생성 실패 위험)
  + CRUD 메서드 8개 + `_CODING_JOB_FIELDS`/`_coding_job_row` 전부 제거.
  기존 DB의 coding_jobs 테이블은 orphan으로 남겨도 무방.
- `src/jobmate/cli.py`: `coding-worker` 서브커맨드, `--coding-tool`/`--coding-model`
  인자, prefs 업데이트의 coding 키 제거.
- `src/jobmate/config.py`: `coding_tool`, `coding_model`, `coding_workspace`,
  `coding_timeout_seconds`, `coding_sandbox`, `coding_sandbox_image` 필드 제거.
- `src/jobmate/models/spec.py`: `AgentSpec.coding_tool` 필드 제거.
- `src/jobmate/workflows/registry_builder.py`: `coding` 파라미터,
  `build_coding_tool` 호출, `build_runner(..., coding=...)` 주입 제거.
- `src/jobmate/engine/runner.py`: `coding` 생성자 파라미터, `self.coding`,
  `agent.coding_tool` 분기(183-184행), `_coding_complete`, `_coding_workspace`
  제거 — LLM stage는 항상 일반 complete 경로.
- `src/jobmate/api/static/index.html`: `#view-coding` 섹션(451-485),
  `/* 코드 어댑터 */` JS 블록(929-1098, `codingPoll`/`activeCodingJob` 포함),
  nav 버튼(431), `VIEWS` 항목(848), 설정 다이얼로그 coding 필드(665-674),
  settings load/save의 coding 키(1823-1824, 1842-1843),
  settings-save 핸들러의 `loadCoding()` 호출(1846-1847) 전부 제거.
- 테스트:
  - 삭제: `tests/unit/test_coding_tools.py`, `tests/unit/test_coding_worker.py`,
    `tests/unit/test_agent.py`, `tests/integration/test_coding_tool_dev_review.py`
  - 수정: `tests/integration/helpers.py` (`make_env`의 `coding_tool="none"` —
    Settings 필드 삭제 시 ValidationError로 전체 integration 테스트 파괴),
    `tests/api/test_api.py` (`coding_tool="none"` kwargs 다수 + coding
    엔드포인트 테스트 + settings prefs coding assertion),
    `tests/ui/test_dashboard.py` (`_ui_llm_handler`는 coding ReAct stub,
    `Settings(coding_tool="none")`, coding 뷰 테스트),
    `tests/unit/test_auth.py` 등 `coding_tool` 인자 사용처 전부.
- `tests/conftest.py`는 이미 정리됨 (해당 없음).
- `.env.example`: `JOBMATE_CODING_*` 항목 제거.
- `AGENTS.md`: `coding_worker.py`, coding job `lease_owner` 펜싱 규칙 언급 갱신.
- 문서: `docs/jobMate_v1.0.1/04_기능별상세설계서_v1.0.1.md 부록 A`,
  `docs/jobMate_v1.0.1/17_운영_보안_가이드_v1.0.1.md 부록 A`의 "코딩 워커" 로깅 섹션,
  `docs/jobMate_v1.0.1/14_UI_자동화_test_procedure_v1.0.1.md`의 coding
  UI 시나리오 갱신.

### B. Agent-Office 뷰

네비게이션: `코딩 에이전트` 버튼 → `Agent-Office` (아이콘 ⬡ 등)로 교체,
`data-view="office"`, `#view-office` 섹션.

레이아웃 (사용자 제공 와이어프레임):
```
┌ JobMate / Agent Command ── 실행시간 · 총 토큰 · 진행률 ┐
│        ANA ───── DEV ───── TEST                        │
│         │          ↖ 실패   │                          │
│       ORCH         SEC ── CR ── QA ── HUM              │
│   입체 플랫폼 + 진행 방향 움직이는 광점                  │
│  선택 에이전트 ──────────── 실시간 이벤트 로그           │
└────────────────────────────────────────────────────────┘
```

노드 매핑 (dev_review stage_key → 노드):
| 노드 | stage_key | 의미 |
|---|---|---|
| ANA | analysis | 요구사항 분석 |
| DEV | implement | 구현 |
| TEST | auto_checks | 자동 검사 |
| SEC | security | 보안 검토 |
| CR | code_review | 코드 리뷰 |
| QA | qa | 최종 검수 |
| HUM | approval | 사람 승인 |
| ORCH | (run 자체) | 오케스트레이터 — run.status 표시 |

데이터 흐름 (백엔드 변경 없음):
1. `GET /api/v1/runs` → `workflow=="dev_review"` 필터. 목록 응답은
   id/workflow/status/created_at만 포함(`updated_at` 없음) — 선택 규칙은
   "running/waiting_* 우선, 아니면 최신 created_at"으로 충분.
2. `GET /api/v1/runs/{id}` → stages(전체 stage_runs, started_at/attempt 순),
   audit, approvals, status, created_at/updated_at.
   - 뷰에서 stage_key별 **최대 attempt**를 직접 골라 노드 상태로 사용.
   - 아직 시작 안 한 stage는 row가 없음 → pending으로 렌더.
   - stage별 started_at/finished_at, tokens_in/out, model, error, output
     모두 payload에 포함.
3. 뷰 활성 중 ~3초 폴링 (기존 loadGmail 10초 패턴과 유사, 필요시 조정).

노드 상태 규칙:
- stage_runs에서 stage_key별 최신 attempt의 status 사용.
  pending=대기, running=실행 중(펄스), succeeded=완료, failed=실패, skipped=스킵.
- attempt>1이면 재시도 배지 표시.
- ORCH 노드: run.status 전체 값 표시 — queued/running/waiting_approval/
  waiting_review/completed/failed/rejected/cancelled/budget_exhausted/
  needs_human 각각 뱃지·색상 매핑 필요.
- 진행률 = succeeded stage 수 / 7. 총 토큰 = Σ(tokens_in+tokens_out).
  실행시간 = updated_at − created_at.

3D 구현:
- 컨테이너에 `perspective`, 내부 플레이트에 `transform: rotateX(...)`.
- 노드는 DOM 요소(position absolute) — 테스트 용이.
- 노드 간 연결은 SVG `<path>` 곡선(베지어) — 실제 진행 방향으로
  `stroke-dasharray` 애니메이션 광점.
- 분기(실패→재시도 등)는 stage 결과에 따라 곡선 강조/회색 처리.
  복잡한 라우팅은 하지 않고 메인 파이프라인 순서대로 연결.

상세 패널: 노드 클릭 시 해당 stage_run 정보 표시
(status/attempt/model/tokens/소요시간/error/output 요약).
ORCH 선택 시 run 전체 정보(input/status/error).

이벤트 로그: run의 audit 배열을 시간순 렌더, 자동 스크롤, 폴링 시 갱신.
actor/action/stage_key/detail을 한 줄로 요약.

빈 상태: dev_review run이 없으면 "실행된 dev_review 워크플로가 없습니다".

내 판단 (승인 필요):
- run.status == waiting_approval이고 HUM 노드/상세 패널에서 승인·거절
  버튼을 제공한다 (`POST /api/v1/runs/{id}/approve` 기존 API 재사용).
  관제 목적상 승인 대기 상태를 방치하지 않기 위함. 원치 않으면 읽기 전용으로.

## 검증

- `uv run pytest` 전체 통과 (coding 테스트 삭제 반영, UI 테스트는
  Agent-Office 빈 상태/네비게이션 케이스로 교체).
- `uv run ruff check src/jobmate tests`, `ruff format --check`, `uv run mypy src/jobmate`.
- 수동: dev_review run 생성 후 관제판에서 노드 상태 전이 확인.
- `graphify update .` 실행.
