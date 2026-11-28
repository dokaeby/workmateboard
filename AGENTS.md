# jobMate — 작업 기준

## 필수 참고 문서 (작업 전/작업 시 항상 확인)

- `docs/jobMate_v1.0.2/04_기능별상세설계서_v1.0.2.md` 부록 A — 현재 아키텍처·엔드포인트·설정 전체
  (구 `docs/harness/05_내부기능_통합.md`, 하네스 문서는 v1.0.1 개발문서로 통합됨)
- `docs/jobMate_v1.0.2/17_운영_보안_가이드_v1.0.2.md` 부록 A — 호스팅 시 필수 로그 목록
  (구 `docs/harness/06_호스팅_필수_로깅.md`).
  **새 기능/수정 시 이 문서의 체크리스트를 적용하고, 새 비동기·외부 호출 경로에는
  필수 로그를 추가한다. 토큰·이메일 본문·push endpoint 원문은 절대 로깅 금지.**

## 검증 명령

- 패키지 매니저: `uv` (Python >=3.12)
- 테스트: `uv run pytest` (UI 테스트는 Playwright chromium 필요 — `uv run playwright install chromium`)
- 린트/포맷: `uv run ruff check src/jobmate tests` / `uv run ruff format --check src/jobmate tests`
- 타입: `uv run mypy src/jobmate`
- 코드 수정 후: `graphify update .` (graphify-out/ 갱신)

## 주요 구조

- `src/jobmate/api/app.py` — FastAPI 앱 조합 루트(create_app + 미들웨어 + 라우터 등록).
  요청 컨텍스트 클로저는 `api/appctx.py`, 코어 라우트는 `api/routers/core.py`,
  스케줄러 잡은 `api/jobs.py`(`run_automation_cycle` 단일 사이클,
  `_automation_loop`/`cron_tick` 두 진입점 공유)
- `src/jobmate/workflows/dev_review.py` — dev_review 워크플로(분석→구현→자동검사→보안→리뷰→QA→사람승인)
- `src/jobmate/api/static/index.html` — 단일 파일 대시보드 UI.
  "Agent Command"(office) 뷰는 `dev_review` run의 스테이지 진행을 3D 보드로
  시각화하고 `POST /api/v1/runs/{id}/approve`로 승인/거절을 처리한다.
- `src/jobmate/store/db.py` — SQLite/PostgreSQL 공용 Store (믹스인 조합 + 커넥션/tx 생명주기)
  - `store/schema_sqlite.py` / `store/schema_pg.py` — 신규 DB용 CREATE TABLE 스키마(이중 정의)
  - `store/pg_dialect.py` — SQLite 방언→PG 변환(`_pg_translate`) + `_PGConn` 재접속 래퍼
  - `store/migrations.py` — `MigrationStoreMixin`: 기존 DB 컬럼/인덱스 ALTER 보강
- `src/jobmate/security.py` — API 키 인증 + Fernet 암호화
- `src/jobmate/services/agent_management.py` — facade. 실제 구현은
  `agent_lifecycle`/`task_ops`/`agent_runtime`/`run_projection`/`management_common`/`redaction`
- `src/jobmate/services/routines.py` — 오케스트레이션. 순수 스케줄 계산(DST/next-run)은 `routine_schedule.py`
- `src/jobmate/services/permissions.py` — capability 평가. `require_permission(target_project_id=)`가 현재+목표 scope 평가
- `src/jobmate/paperclip/` — paperclip bridge 패키지(client/bridge/urls/errors). `paperclip_client.py`는 호환 shim

## 주의

- 비밀값·토큰은 로그/커밋 금지. `.env` 미추적.
- `scheduled_events.status`: candidate/needs_review/needs_confirmation/registered/rejected
