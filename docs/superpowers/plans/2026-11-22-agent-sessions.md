# Agent-Office 멀티툴 세션 모드 — 구현 계획

Spec: `docs/superpowers/specs/2026-11-22-agent-sessions-design.md`
브랜치: `agent-sessions` (main 기준)

## T1 — 백엔드: agent_sessions 저장소 + API (subagent A)

- `src/jobmate/store/db.py`: `agent_sessions` 테이블 —
  tool, session_key, project, title, status, doing, ask,
  last_event_at, created_at, updated_at. UNIQUE(tool, session_key).
  `upsert_agent_session(...)`, `list_agent_sessions(limit, include_ended=False)`.
  기존 이중 스키마(SQLite/PostgreSQL)+마이그레이션 패턴 준수.
- `src/jobmate/api/app.py`:
  - `POST /api/v1/agent-sessions` — upsert. 인증: 기존 API 키 경로
    (`authorized(request)`)만 — 세션 쿠키 불가(머신 push 전용).
    body 검증은 Pydantic 모델. status ∈ working|decision|rest|ended.
    ended → status 저장, list에서 제외.
  - `GET /api/v1/agent-sessions?limit=50` — ended 제외,
    last_event_at desc. 세션 쿠키/API 키 모두 허용.
- 테스트 `tests/api/test_agent_sessions.py` 또는 기존 api 테스트에 추가:
  upsert/list/ended 제외/중복 session_key 갱신/인증 실패.

## T2 — 브릿지: `jobmate agent-bridge` (subagent B, T1과 병렬)

- `src/jobmate/agent_bridge.py` (신규):
  - `SessionState` dataclass: tool, session_key, project, title,
    status, doing, ask, event_at, path.
  - 스캐너 4종 — 각각 `scan_xxx(root, now) -> list[SessionState]`:
    - claude: `~/.claude/projects/*/*.jsonl` tail 파싱
    - codex: `~/.codex/sessions/**/*.jsonl.zst` — `zstd -dc` 서브프로세스,
      출력 tail만 파싱 (임계 크기 제한)
    - gemini: `~/.gemini/antigravity-cli/conversations/*.db` —
      sqlite3 stdlib, steps 테이블 최신 행 (스키마 구현 시 확인)
    - devin: `~/.local/share/devin/cli/transcripts/*.json` +
      `session_locks/` — mtime+lock으로 활성 판별, ATIF steps tail
  - 상태 규칙: mtime < IDLE_SECONDS(45) → working/decision,
    >= → rest. decision = 마지막 assistant 이벤트가 미완료
    tool_use/function_call. doing/ask ≤80자, title ≤60자.
  - ended: 이전 스캔에 있었으나 이번에 없거나 mtime > ENDED_SECONDS(600)
    → status=ended 1회 push 후 로컬 추적 제거.
  - `Bridge` 클래스: scan_all → diff(상태/doing/ask 변경 또는
    HEARTBEAT_SECONDS=60 경과)만 POST /api/v1/agent-sessions.
    httpx 사용(프로젝트 기존 HTTP 클라이언트), timeout 명시.
- `src/jobmate/cli.py`: `agent-bridge` 서브커맨드 추가
  (--url, --api-key-env 기본 JOBMATE_API_KEY, --interval 15, --once).
- 테스트 `tests/unit/test_agent_bridge.py`: fixture 파일로 각 스캐너
  파서 + 상태 경계값 + diff 로직 (httpx mock).

## T3 — UI: 세션 서브탭 (본 세션, T1 완료 후)

- `index.html` `#view-office`: 상단 `워크플로 | 세션` 탭 추가.
  - 워크플로 탭 = 기존 전체 UI.
  - 세션 탭 = `#office-sessions` 래퍼(스프라이트 캔버스+side 패널
    재사용 구조 — jmo- id는 하나만 존재해야 하므로 탭 전환 시
    동일 `#jmo-wrap`을 재사용하고 provider만 교체하는 방식 권장:
    `window.JM_OFFICE_PROVIDER`를 세션용으로 교체 후 start/stop).
  - `jmSessionProvider()`: `api("/agent-sessions")` → people 매핑
    (id=`${tool}:${session_key}`, name=`${toolLabel} · ${project}`). ended 없음.
  - 빈 상태 안내 문구.
- `tests/ui/test_dashboard.py`: 세션 탭 전환 + seeded agent_session
  → officeDebug people 매핑 테스트.

## T4 — 문서·검증 (본 세션)

- `docs/harness/05_내부기능_통합.md` 엔드포인트/브릿지 사용법 추가.
- `.env.example`에 `JOBMATE_URL` 필요시 추가.
- `uv run pytest` / ruff / mypy / `graphify update .`
- 커밋은 태스크별, 푸시는 요청 시.
