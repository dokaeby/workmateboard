# Agent-Office 스프라이트 사무실 모드 — 설계

날짜: 2026-11-21
상태: 승인 대기
참고: `/Users/sec/Documents/python-projects/claude-3d-frog-office` (MIT)

## 의도와 이유

Agent-Office 뷰에 claude-3d-frog-office의 아이소메트릭 스프라이트 사무실을
모드 전환 방식으로 추가한다. dev_review 실행의 8개 주체(7 stage + ORCH)를
사무실 캐릭터로 보여 상태(작업 중/승인 대기/휴식)를 직관적으로 표현한다.

사용자 결정:
- 기존 CSS 3D 보드와 **모드 전환** (3D / 사무실)
- **8개 전부 캐릭터** (ANA~HUM + ORCH)
- 스프라이트 에셋 **그대로 사용** (frog/capybara webp, MIT)

## 목표 / 비목표

목표:
- Agent-Office 뷰 헤더에 모드 토글(3D 보드 ↔ 스프라이트 사무실).
- 스프라이트 사무실: 캔버스 렌더러 + 스프라이트 시트 + 길찾기/생활 시뮬레이션
  포팅, jobMate dev_review 데이터를 people 모델로 변환.
- 캐릭터 클릭 → 기존 상세 패널(#office-detail) 연동, 사람 목록/활동 로그 패널.

비목표:
- claude-3d-frog-office의 훅 서버/세션 카드/대화기록 파이프라인 이식 —
  데이터는 기존 `/api/v1/runs` 폴링 결과만 사용.
- 서브에이전트 미니 캐릭터 (dev_review에 대응 개념 없음 — subs 빈 배열).
- 스프라이트 신규 제작, 외부 의존성 추가.

## 제약

- 정적 파일만 추가/수정 — 백엔드 변경 없음 (people 데이터는 클라이언트에서 생성).
- 외부 의존성 0, CDN 없음 (참고 프로젝트와 동일 원칙).
- 기존 3D 보드와 상세 패널/이벤트 로그 동작 유지.
- MIT 라이선스 고지 포함 (NOTICE 또는 파일 헤더).

## 접근 방식

1. **참고 코드 거의 그대로 포팅 (채택)** — office-view/life/path/world/atlas/
   panel/i18n.js + webp 에셋을 `src/jobmate/api/static/office/`로 복사.
   fetch 부분만 jobMate 데이터 provider로 교체. 검증된 렌더링/시뮬 재사용.
2. office-life.js의 상태기계를 jobMate 상태에 맞게 재작성 — 기각:
   사람 목록→행동 결정 로직이 잘 분리돼 있어 재사용이 더 안전.
3. Three.js/WebGL — 기각: 참고 프로젝트는 2D 캔버스 스프라이트.

## 구현 내용

### A. 에셋 복사
- `web/office/*.webp` 26개 → `src/jobmate/api/static/office/` (1.6MB)
- `web/*.js` 7개 → `src/jobmate/api/static/office/` (i18n, office-atlas,
  office-world, office-path, office-life, office-panel, office-view)
- `web/style.css`의 office 관련 규칙 → index.html에 스코프드 추가 또는
  `static/office/office.css`로 분리 로드

### B. 데이터 브릿지 (`static/office/jobmate-bridge.js`, 신규)

`window.JM_OFFICE_PEOPLE()` → office-view가 기대하는 people[] 생성.
입력: 기존 `officeState` {run, stages(Map), stageRuns, selected} + OFFICE_STAGES.

people 매핑 (id = stage node 코드, name = 한글 라벨):
| node | name | status 규칙 |
|---|---|---|
| ANA~QA | 각 stage 라벨 | stage running→working, succeeded/failed/skipped/pending→rest |
| HUM | 사람 승인 | run.status==waiting_approval→decision(ask="사람 승인 대기"), approval stage running→working, 그 외→rest |
| ORCH | 오케스트레이터 | run.status: running/queued→working, waiting_*/needs_human→decision, completed/failed/rejected/cancelled/budget_exhausted→rest |

- doing: stage_run output 또는 최신 audit detail에서 한 줄 텍스트 →
  `{k:"text", a:"..."}` (Act 형태). stage label + 상태를 기본 문구로.
- ask: decision일 때 표시 문구 (HUM: "승인 대기 중", ORCH: run.status).
- subs: [], subCount: 0, kid: false, commits: null, home: null.
- run이 없으면 people=[] → office-life가 빈 사무실 표시.

### C. office-view.js 적응 (최소 수정)

- `fetch("/api/office")` → `window.JM_OFFICE_PROVIDER()` 호출로 교체
  (기본값: 기존 fetch — 호환 유지).
- DOM id 요구사항: `#office-canvas`, `#office-box`, `#notice`, `#summary`,
  `#office-people`, `#office-log`, `#office-count` — 2D 모드 컨테이너 안에 생성.
- 사람 선택(person.id) → `officeState.selected`에 node 코드 매핑 →
  기존 `#office-detail` 렌더 함수 재사용 (상세 패널은 3D/2D 공용 유지).
- init: 모드 전환 시 lazy 로드 — script 태그 동적 삽입 1회, 이후
  `OfficeView` 시작/정지. 캔버스 리사이즈는 기존 view 코드가 처리.

### D. index.html 통합

- `#view-office` 헤더에 모드 토글 버튼 2개: `3D` / `사무실`.
- `#office-stage` 내부를 두 컨테이너로: `#office-board`(기존 3D) /
  `#office-2d`(캔버스 + 우측 패널 레이아웃).
- 하단 `#office-detail`/`#office-log`는 공용 유지 — 2D 모드에서는
  OfficePanel이 자체 로그를 보여주므로 중복 시 `#office-log` 숨김 처리
  (또는 2D 패널에 통합). → 결정: 2D 모드에서 office-side 패널(people+log)을
  캔버스 우측에 배치하고, 하단 #office-detail만 유지, #office-log는 숨김.
- 폴링: 기존 loadOffice 3초 폴링이 officeState를 갱신 → 브릿지가
  people을 재생성 → OfficeLife.sync. 2D 모드 전용 추가 폴링 없음.

### E. 라이선스

- `static/office/LICENSE` 또는 index.html 주석에 MIT 고지 + 출처 표기.

## 검증

- `uv run pytest` 전체 통과 + UI 테스트 추가:
  - 모드 토글 버튼 존재/전환, 2D 모드에서 `#office-canvas` 표시,
  - seeded dev_review run → people 매핑 상태 확인(officeDebug 또는
    people 목록 텍스트).
- `uv run ruff check`/`format`/`mypy` clean (JS만 추가 — Python 영향 없음).
- `graphify update .`
