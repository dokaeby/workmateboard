# Agent-Office 관제판 + 코딩 에이전트 제거 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 코딩 에이전트 기능을 완전히 제거하고, 그 메뉴 자리에 dev_review 워크플로를 입체 3D 관제판(Agent-Office)으로 보여주는 뷰를 추가한다.

**Architecture:** 백엔드에서 coding 관련 코드·DB·API를 전부 제거하고 dev_review implement는 기존 LLM 경로로 동작. Agent-Office는 신규 백엔드 없이 기존 `GET /api/v1/runs` + `GET /api/v1/runs/{id}`(stages+audit+approvals 포함)를 ~3초 폴링해 렌더링한다. UI는 기존 단일 `index.html` + vanilla JS 패턴 유지.

**Tech Stack:** Python 3.12+, FastAPI, SQLite/PG Store, vanilla JS/CSS (no framework), pytest + Playwright.

**Spec:** `docs/superpowers/specs/2026-11-21-agent-office-design.md`

## Global Constraints

- `uv run pytest` 전체 통과, `uv run ruff check src/jobmate tests`, `uv run ruff format --check src/jobmate tests`, `uv run mypy src/jobmate` clean 유지.
- `tools/executor.py`(ToolExecutor)는 dev_review function stage가 사용 — **삭제 금지**.
- 비밀값·토큰 로깅 금지 (docs/harness/06).
- stage_runs에는 미시작 stage의 row가 없음 — 뷰에서 pending 처리.
- `GET /api/v1/runs/{id}`의 stages는 전체 stage_runs(시작순) — 뷰에서 stage_key별 max attempt 선택.
- 승인/거절은 기존 `POST /api/v1/runs/{id}/approve` (body: `{decision: "approve"|"reject", comment?}`) 재사용 — run.status가 `waiting_approval`일 때만.

## Review Focus

- `Settings(coding_tool=...)` kwargs가 남은 테스트 파일 → ValidationError. Task 4에서 `grep -rn "coding_tool\|coding_model\|coding_workspace\|coding_timeout\|coding_sandbox" tests/ src/` 결과 0건 확인.
- `AgentSpec.coding_tool` 참조 잔재 → mypy/ruff로 검출.
- index.html에 `loadCoding`/`codingPoll`/`coding-` id 참조 잔재 → grep 0건.
- db.py `_migrate_coding_jobs`가 fresh DB에서 coding_jobs 테이블 없이 index 생성 → 삭제 필수 (그대로 두면 신규 DB 생성 실패).
- run.status 값이 10종 — ORCH 뱃지 매핑 누락 시 표시 깨짐. Task 7 테스트에 전 값 케이스 포함.
- stage_key별 복수 attempt — 최신 attempt만 표시하고 재시도 배지.

---

### Task 1: 코딩 설정·스펙·러너 배선 제거

**Files:**
- Modify: `src/jobmate/config.py:66-72`
- Modify: `src/jobmate/models/spec.py:32`
- Modify: `src/jobmate/workflows/registry_builder.py` (전체 — coding 파라미터·build_coding_tool·coding= 주입 제거)
- Modify: `src/jobmate/engine/runner.py` (생성자 `coding` 파라미터, `self.coding`, 183-184행 `agent.coding_tool` 분기, `_coding_complete`, `_coding_workspace` 메서드 제거)

**Interfaces:**
- Produces: `AgentSpec`에 `coding_tool` 필드 없음. `build_runner(...)` 시그니처에서 `coding` 파라미터 제거. `WorkflowRunner.__init__`에서 `coding` 파라미터 제거.

- [ ] **Step 1:** `grep -rn "coding" src/jobmate/engine/runner.py src/jobmate/workflows/registry_builder.py src/jobmate/models/spec.py src/jobmate/config.py`로 현재 참조 전수 확인.
- [ ] **Step 2:** 위 파일들에서 coding 관련 필드/파라미터/분기/메서드를 제거. `_run_llm_stage`는 항상 `self.llm.complete` 경로 사용.
- [ ] **Step 3:** `uv run mypy src/jobmate` — coding import 에러가 있으면 해당 파일은 Task 2/3에서 삭제될 파일(coding_worker 등)이므로 mypy 에러가 coding_worker.py 자체에서만 나오면 OK, 다른 파일에서 나오면 수정.
- [ ] **Step 4:** `uv run pytest tests/unit tests/integration -q -x --ignore=tests/unit/test_coding_tools.py --ignore=tests/unit/test_coding_worker.py --ignore=tests/unit/test_agent.py --ignore=tests/integration/test_coding_tool_dev_review.py` — dev_review/gmail/radar 테스트 통과 확인 (helpers.py의 `coding_tool="none"`는 Task 4 전까지 실패 예상 — 이 태스크에서는 config 필드를 지우기 전 순서 조정: **config.py 필드 삭제는 Task 4와 함께 하거나, helpers.py 수정도 이 태스크에 포함**). → 결정: 이 태스크에서 config.py의 coding 필드 삭제를 포함하고, `tests/integration/helpers.py`와 `tests/api/test_api.py` 등의 `coding_tool` kwarg도 함께 제거한다 (테스트 파일 수정은 Task 4로 미루지 않고 최소한의 kwarg 제거만 여기서).
- [ ] **Step 5:** Commit `refactor: remove coding tool wiring from runner/config/spec`.

### Task 2: 코딩 모듈 파일 삭제 + store 정리

**Files:**
- Delete: `src/jobmate/coding_worker.py`, `src/jobmate/agent/` (디렉터리), `src/jobmate/tools/coding.py`, `src/jobmate/sandbox.py`, `tests/unit/test_coding_tools.py`, `tests/unit/test_coding_worker.py`, `tests/unit/test_agent.py`, `tests/integration/test_coding_tool_dev_review.py`
- Modify: `src/jobmate/tools/__init__.py` (coding re-export 제거, ToolExecutor 유지)
- Modify: `src/jobmate/store/db.py` — SQLite `SCHEMA`와 `SCHEMA_PG` 양쪽의 `coding_jobs` 테이블, `_migrate_coding_jobs()`+`_CODING_JOB_NEW_COLS`+호출부, CRUD 메서드들(`create_coding_job`, `get_coding_job`, `list_coding_jobs`, `update_coding_job*`, `cancel_coding_job` 등), `_CODING_JOB_FIELDS`, `_coding_job_row` 제거

- [ ] **Step 1:** `grep -rn "coding_jobs\|coding_job\|CodingJob" src/jobmate/store/db.py`로 제거 범위 확정.
- [ ] **Step 2:** 삭제/수정 실행. `Store.__init__`에서 `_migrate_coding_jobs` 호출 제거 필수.
- [ ] **Step 3:** `grep -rn "coding_worker\|from .agent\|from ..agent\|sandbox\|tools.coding\|tools import.*coding" src/` → 0건 확인 (app.py/cli.py 잔여는 Task 3).
- [ ] **Step 4:** `uv run pytest tests/unit/test_store.py -q` 통과.
- [ ] **Step 5:** Commit `refactor: delete coding worker, agent loop, sandbox, coding_jobs store`.

### Task 3: API 엔드포인트·CLI·env 정리

**Files:**
- Modify: `src/jobmate/api/app.py` — `CodingJobWorker` import/생성/start/stop(173-204행 부근), `/api/v1/coding/*` 엔드포인트 전체(757-884행 부근), `CodingRunRequest`, settings prefs의 `coding_tool`/`coding_model` 키(723-724, 738-746행 부근)
- Modify: `src/jobmate/cli.py` — `coding-worker` 서브커맨드, `run`의 `--coding-tool`/`--coding-model` 인자와 model_copy 블록(123-127행)
- Modify: `.env.example` — `JOBMATE_CODING_*` 항목
- Modify: `src/jobmate/api/static/index.html` — coding 관련 제거는 Task 5에서 일괄 처리 (이 태스크에서는 app.py/cli.py만)

- [ ] **Step 1:** `grep -n "coding" src/jobmate/api/app.py` 재확인 후 엔드포인트 블록 삭제. `api/v1/coding` 라우트 0건.
- [ ] **Step 2:** cli.py 정리 후 `uv run jobmate --help` 및 `uv run jobmate workflows` 동작 확인.
- [ ] **Step 3:** `uv run pytest tests/api -q` — coding 엔드포인트 테스트 실패 예상, Task 4에서 정리.
- [ ] **Step 4:** Commit `refactor: remove coding API endpoints and CLI worker`.

### Task 4: 테스트 정리 — 전체 pytest green

**Files:**
- Modify: `tests/integration/helpers.py` (`coding_tool="none"` 제거), `tests/api/test_api.py` (coding kwargs·엔드포인트·prefs assertion), `tests/unit/test_auth.py`, `tests/ui/test_dashboard.py` (`_ui_llm_handler`, `Settings(coding_tool=...)`, coding 뷰 테스트 — Agent-Office 테스트는 Task 8), 기타 `grep -rn "coding" tests/` 잔여 전부

- [ ] **Step 1:** `grep -rn "coding" tests/ --include="*.py"` 전수 확인.
- [ ] **Step 2:** coding 관련 테스트 삭제, kwargs 제거, Settings 생성자 정리.
- [ ] **Step 3:** `uv run pytest -q` 전체 통과 (UI 테스트는 chromium 필요 — playwright 설치돼 있으면 포함).
- [ ] **Step 4:** `uv run ruff check src/jobmate tests && uv run ruff format --check src/jobmate tests && uv run mypy src/jobmate` clean.
- [ ] **Step 5:** Commit `test: remove coding feature tests and settings kwargs`.

### Task 5: index.html — 코딩 뷰 제거 + Agent-Office 뼈대

**Files:**
- Modify: `src/jobmate/api/static/index.html`

- [ ] **Step 1:** 제거 — nav 버튼(431행), `#view-coding` 섹션(451-485행), `/* 코드 어댑터 */` JS 블록 전체(929-1098행: `codingPoll`, `activeCodingJob`, `loadCodingTools`, `syncEngineUI`, `loadCoding`, `renderCodingJobs`, 이벤트 리스너), `VIEWS`의 `coding` 항목(848행), 설정 다이얼로그의 `set-coding-tool`/`set-coding-model` 필드(665-674행), settings load/save의 coding 키(1823-1824, 1842-1843행), settings-save 핸들러의 `loadCoding()` 호출(1846-1847행).
- [ ] **Step 2:** nav 버튼을 `<button class="nav-item" data-view="office"><span class="ico">⬡</span>Agent-Office</button>`로 교체 (코딩 에이전트 위치, radar 다음 gmail 다음 순서 유지).
- [ ] **Step 3:** `#view-office` 섹션 추가 — 헤더(제목 "Agent Command" + 상태 칩: 실행시간/총 토큰/진행률), 3D 플랫폼 컨테이너(`#office-stage`), 하단 분할 패널(`#office-detail` 선택 에이전트 정보, `#office-log` 이벤트 로그). 초기에는 빈 상태 문구만.
- [ ] **Step 4:** `VIEWS`에 `office: loadOffice` 추가, `loadOffice`는 우선 빈 상태 렌더 스텁.
- [ ] **Step 5:** `grep -n "coding\|Coding" src/jobmate/api/static/index.html` → 0건.
- [ ] **Step 6:** `uv run pytest tests/ui/test_dashboard.py -q -k "nav or sidebar"` 통과 확인 (nav 테스트의 뷰 목록은 Task 8에서 갱신).
- [ ] **Step 7:** Commit `feat(ui): replace coding view with Agent-Office skeleton`.

### Task 6: Agent-Office 데이터 레이어

**Files:**
- Modify: `src/jobmate/api/static/index.html` (JS만)

**Interfaces:**
- Produces (JS 내부):
  - `OFFICE_STAGES = [{node:"ANA",key:"analysis"},{node:"DEV",key:"implement"},{node:"TEST",key:"auto_checks"},{node:"SEC",key:"security"},{node:"CR",key:"code_review"},{node:"QA",key:"qa"},{node:"HUM",key:"approval"}]`
  - `officeState = {run, stages: Map<stage_key, latestStageRun>, selected: nodeKey|null}`
  - `async function loadOffice()` — runs 목록에서 dev_review 최신 run 선택 → 상세 fetch → 상태 갱신 → 렌더.
  - run 선택 규칙: status가 running/waiting_approval/waiting_review/queued/needs_human 중 하나면 우선, 복수면 최신 created_at; 아니면 전체 최신 created_at.
  - stage 상태: `stages` 배열에서 stage_key별 max(attempt) 선택. row 없으면 pending.
  - 폴링: view-office가 보이는 동안 3초 간격 `setInterval`, 다른 뷰 전환/숨김 시 clear (기존 `setInterval` + hidden 체크 패턴과 동일 — 1906행 참고).

- [ ] **Step 1:** `loadOffice` 구현 — api("/runs") 필터, api(`/runs/${id}`) fetch, officeState 구성.
- [ ] **Step 2:** 헤더 통계 렌더 — 실행시간(`updated_at - created_at`, 초/분 포맷), 총 토큰(Σ tokens_in+tokens_out), 진행률(succeeded/7 %).
- [ ] **Step 3:** `test_dashboard.py`에 UI 테스트 추가: dev_review run이 없는 빈 DB에서 "dev_review" 안내 문구 표시 + nav 클릭 시 view-office visible. (run 생성은 Task 7 이후 integration fixture로 — 우선 빈 상태만.)
- [ ] **Step 4:** `uv run pytest tests/ui/test_dashboard.py -q` 통과.
- [ ] **Step 5:** Commit `feat(ui): Agent-Office data layer polling dev_review runs`.

### Task 7: Agent-Office 3D 렌더링 + 상세 패널 + 이벤트 로그 + 승인

**Files:**
- Modify: `src/jobmate/api/static/index.html` (CSS + JS + HTML)

- [ ] **Step 1:** CSS — `#view-office` 레이아웃, `.office-perspective`(perspective), `.office-plate`(rotateX 평면), `.office-node`(absolute positioned DOM, 상태별 클래스: pending/running/succeeded/failed/skipped), `.office-node.running` 펄스 애니메이션, `.office-edge` SVG path 스타일 + dash 흐름 애니메이션.
- [ ] **Step 2:** 노드 배치 — 사용자 와이어프레임 기준 좌표(플레이트 % 좌표): ORCH 좌측 중앙, ANA 상단 좌, DEV 상단 중, TEST 상단 우, SEC 중좌, CR 중중, QA 중우, HUM 우측. 정확한 % 좌표는 구현 시 조정하되 2단 계단 배치 유지. 각 노드는 `.office-node` div에 코드+라벨+상태 뱃지.
- [ ] **Step 3:** SVG 곡선 — 노드 중심 간 `<path>` 베지어. 진행 방향(ANA→DEV→TEST→SEC→CR→QA→HUM, 실패/재시도 시 역방향 강조)으로 dashoffset 애니메이션. succeeded 구간은 밝은 색, pending은 dim.
- [ ] **Step 4:** 노드 클릭 → `#office-detail`에 stage_run 상세(status/attempt/model/tokens/소요시간/error/output JSON 요약). ORCH 클릭 → run 정보(input/status/error/approvals). 승인 대기(run.status==waiting_approval && node=="HUM") 시 승인/거절 버튼 → `api(`/runs/${id}/approve`, {method:"POST", body:{decision, comment:""}})` 후 재로드.
- [ ] **Step 5:** `#office-log` — run.audit 시간순 렌더(`created_at`, actor, action, stage_key, detail 요약), 최신이 하단, 자동 스크롤. audit 없으면 "이벤트 없음".
- [ ] **Step 6:** 빈 상태 — dev_review run 없으면 플랫폼 대신 안내 문구.
- [ ] **Step 7:** UI 테스트 추가 — (a) 사이드바 Agent-Office 네비게이션/타이틀, (b) 빈 상태. run이 있는 상태 테스트는 서버 fixture로 dev_review run을 직접 store.create_run+stage_runs 삽입해 검증 가능한지 확인, 가능하면 추가.
- [ ] **Step 8:** `uv run pytest tests/ui/test_dashboard.py -q` 통과.
- [ ] **Step 9:** Commit `feat(ui): Agent-Office 3D control board with detail panel and event log`.

### Task 8: 문서·AGENTS.md·최종 검증

**Files:**
- Modify: `AGENTS.md` (coding_worker/lease_owner 언급 → Agent-Office 구조 반영), `docs/harness/05_내부기능_통합.md`, `docs/harness/06_호스팅_필수_로깅.md` (코딩 워커 로깅 섹션 제거/갱신), `docs/jobMate_v1.0.0/14_UI_자동화_test_procedure_v1.0.0.md` (coding 시나리오 → Agent-Office)
- Modify: `tests/ui/test_dashboard.py` — 사이드바 테스트의 뷰 목록에서 coding 제거, office 추가

- [ ] **Step 1:** 문서 갱신.
- [ ] **Step 2:** `grep -rni "coding" src/ tests/ docs/harness/ AGENTS.md .env.example` — 잔여가 historical 문서 언급뿐인지 확인, 코드 참조 0건.
- [ ] **Step 3:** 전체 검증: `uv run pytest -q`, ruff check/format, mypy.
- [ ] **Step 4:** `graphify update .`
- [ ] **Step 5:** Commit `docs: update harness docs and AGENTS for Agent-Office`.
