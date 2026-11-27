# Agent-Office 멀티툴 세션 모드 — 설계

날짜: 2026-11-22
상태: 승인 대기
선행: `2026-11-21-office-sprites-design.md` (스프라이트 사무실 모드)

## 의도와 이유

Agent-Office 스프라이트 사무실을 claude-3d-frog-office처럼 실제 코딩 에이전트
세션을 보여주는 관제 화면으로 확장한다. Claude Code 외에 Codex CLI,
Gemini CLI(antigravity), Devin CLI의 활성 세션을 모두 표시한다.

사용자 결정:
- Agent-Office 뷰에 **별도 모드(서브탭) 추가** — 기존 워크플로 관제는 유지
- 수집은 **로컬 브릿지 push** — Vercel 서버가 로컬 파일을 못 읽으므로
  사용자의 머신에서 돌아가는 수집기가 API로 상태를 전송

## 목표 / 비목표

목표:
- `agent_sessions` 저장소 + push/pull API.
- `jobmate agent-bridge` 로컬 수집기: 4개 도구의 세션 파일을 스캔해
  {status, doing, ask} 휴리스틱을 계산, 변경분만 API로 push.
- Agent-Office 뷰에 "워크플로 / 세션" 서브탭. 세션 탭은 스프라이트
  사무실 재사용, people = 활성 세션.

비목표:
- 각 도구의 승인 프롬프트에 대한 원격 승인/거절 — 세션 표시는 읽기 전용.
- Devin Cloud 세션 API 폴링 — 로컬 transcripts만 우선.
- Claude hooks 정밀 감지 — 파일 tailing 휴리스틱으로 시작, hooks는 후속.
- 세션 대화 내용 수집/저장 — 상태·요약 텍스트만 (개인정보 최소화).

## 제약

- 브릿지에 새 외부 의존성 추가 금지 (stdlib + zstd CLI 사용).
- 세션 파일에서 **메시지 본문 전체를 전송하지 않** — doing/ask는
  60~120자 요약만. title은 첫 유저 프롬프트 앞부분.
- push 인증은 기존 `JOBMATE_API_KEY` 재사용.
- 스프라이트 엔진(JMOffice2D)은 싱글턴 — provider 교체 방식으로 재사용.

## 수집 원본 (조사 완료)

| tool | 경로 | 형식 |
|---|---|---|
| claude | `~/.claude/projects/*/*.jsonl` | type: user/assistant/attachment/system, message.content[] |
| codex | `~/.codex/sessions/**/*.jsonl.zst` | timestamp/type/payload, `zstd -dc` 필요 |
| gemini | `~/.gemini/antigravity-cli/conversations/*.db` | sqlite, steps 테이블 |
| devin | `~/.local/share/devin/cli/transcripts/*.json` + `session_locks/` | ATIF JSON, locks로 활성 판별 |

## 상태 휴리스틱 (공통 계약)

`status`: `working` | `decision` | `rest` | `ended`
- `working`: 세션 파일 mtime < ~45초 AND 마지막 의미 이벤트가 에이전트 활동
- `decision`: 마지막 assistant 이벤트가 미실행 tool_use/function_call
  (권한 대기 추정) — 부정확하면 working으로 둬도 무방
- `rest`: mtime >= 45초 (사용자 응답/idle 대기)
- `ended`: 스캔에서 사라지거나 mtime > ~10분 — 브릿지가 1회 push 후 로컬에서 제거

`doing`: 마지막 assistant 텍스트/tool 이름 요약 (≤80자)
`ask`: decision일 때 tool/행동 요약
`title`: 첫 user 프롬프트 앞 60자
`project`: 세션의 cwd/프로젝트 경로 basename

## 구현 내용

### A. 백엔드

1. `store/db.py`: `agent_sessions` 테이블 (tool, session_key UNIQUE,
   project, title, status, doing, ask, last_event_at, created_at,
   updated_at) + `upsert_agent_session`, `list_agent_sessions`,
   기존 이중 스키마/마이그레이션 패턴 따름.
2. `api/app.py`:
   - `POST /api/v1/agent-sessions` — upsert (API key 인증 경로).
     body: {tool, session_key, project?, title?, status, doing?, ask?,
     event_at?}. status=ended면 행 삭제 또는 status만 갱신(→삭제 선택:
     ended는 목록에서 제외되므로 DELETE와 동일 효과, 감사용으로
     status='ended' 유지 + list에서 제외).
   - `GET /api/v1/agent-sessions?limit=50` — ended 제외 목록,
     last_event_at desc. 세션 쿠키/API 키 모두 허용(기존 api() 경로).
3. 로깅: push/pull 접근 로그는 기존 미들웨어가 처리. 민감값 미포함 확인.

### B. 브릿지 (`src/jobmate/agent_bridge.py` + CLI `agent-bridge`)

- `jobmate agent-bridge [--url URL] [--interval 15] [--once]`
  - url 기본: `JOBMATE_URL` env 또는 `http://127.0.0.1:8000`
  - api key: `JOBMATE_API_KEY` env
- 스캐너 4종 (`agent_bridge/` 하위 모듈 또는 단일 파일 + collector 함수):
  - `scan_claude(root=~/.claude/projects)` — jsonl tail 파싱
  - `scan_codex(root=~/.codex/sessions)` — `.zst`를 `zstd -dc` 서브프로세스로
    끝부분만 필요하면 임시파일/파이프 tail. 세션 파일이 크므로
    `zstd -dc | tail -c 200KB` 파이프 방식.
  - `scan_gemini(root=~/.gemini/antigravity-cli/conversations)` —
    sqlite3 stdlib로 각 db의 최신 step 조회 (스키마는 구현 시 확인)
  - `scan_devin(root=~/.local/share/devin/cli)` — transcripts/*.json의
    mtime + session_locks 유무로 활성 판별, ATIF steps tail에서 상태 추출
- 각 스캐너는 `SessionState` dataclass 리스트 반환.
- diff push: 이전 스캔 대비 status/doing/ask 바뀐 세션 + 60초 하트비트만
  POST. ended 전이 세션은 status=ended로 1회 push.
- `--once`: 1회 스캔+push 후 종료 (테스트/수동용). 기본은 루프.

### C. UI (`index.html`)

- `#view-office` 상단에 서브탭: `워크플로` | `세션`.
  - 워크플로 탭 = 현재 전체 내용(칩+모드토글+스테이지/스프라이트+상세/로그).
  - 세션 탭 = 스프라이트 사무실만 (별도 래퍼 `#office-sessions`).
- provider 전환: `window.JM_OFFICE_PROVIDER`를 탭에 따라 교체
  - 워크플로: 기존 `JM_OFFICE_PROVIDER`(jobmate-bridge.js)
  - 세션: `jmSessionProvider()` — `api("/agent-sessions")` → people 매핑
    - id = `${tool}:${session_key}`, name = `🐸 Claude · proj` 형태
      (tool별 이모지/이름 prefix), status/doing/ask 그대로,
      subs/subCount=0, kid=false, home=null
- 세션 탭 전환 시 `JMOffice2D.stop()` → provider 교체 → `start()`.
  people id가 바뀌므로 office-life가 새 사람으로 재스폰(기존 동작).
- 세션 탭 하단 패널: OfficePanel 자체 people 목록+로그가 상세 역할.
  `#office-detail`/`#office-log`는 워크플로 탭에서만 표시.
- 세션 탭 빈 상태: "활성 에이전트 세션이 없습니다 — 로컬에서
  `jobmate agent-bridge`를 실행하세요" 안내.

### D. 문서

- `docs/jobMate_v1.0.1/04_기능별상세설계서_v1.0.1.md 부록 A`에 엔드포인트·브릿지 사용법 추가.
- `AGENTS.md` 검증 명령 그대로. `.env.example`에 `JOBMATE_URL` 등 필요시 추가.

## 검증

- Store/API 단위 테스트: upsert/list/ended 제외, 인증.
- 브릿지 스캐너 단위 테스트: fixture 파일(tmp_path)로 각 tool 파서 검증,
  상태 휴리스틱 경계(mtime 임계) 케이스.
- `--once` + 로컬 uvicorn 대상 통합 테스트.
- UI 테스트: 세션 탭 전환, provider가 `/api/v1/agent-sessions` 호출,
  seeded 세션 → officeDebug people 매핑.
- `uv run pytest` / ruff / mypy / `graphify update .`
