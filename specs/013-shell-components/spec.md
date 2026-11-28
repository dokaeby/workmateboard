# spec 013 — Agent Office 셸 컴포넌트 (Inspector / Event Dock / Command Palette)

## 목표

`docs/UI_DESIGN_GUIDE.md` §7의 나머지 셸 영역 구현:
D. Inspector Panel(우측 상세), E. Bottom Event Dock(실시간 이벤트),
§8 Command Palette(Cmd+K).

## US1: Inspector Panel

- App 레이아웃에 우측 접이식 패널(320~420px) 추가.
- 전역 선택 상태: appStore에 `inspector: {kind, id} | null` +
  `setInspector` 추가. kind: "run" | "agent" | "session" | "trace".
- 내용은 kind별로 기존 뷰의 상세 데이터를 재사용:
  - run → `/runs/{id}` 요약 + 최근 stage + 승인 상태
  - agent/session → `/agent-sessions/{key}/history` 요약
  - trace → run timeline 요약
- 패널은 선택이 있을 때만 표시, X 버튼/ESC로 닫기.
  모바일에서는 전체 화면 오버레이.
- 기존 뷰에서 항목 클릭 시 `setInspector` 호출 지점 추가는
  최소한으로 — 우선 관측 센터 LiveAgents 카드와 runs 목록 행.

## US2: Bottom Event Dock

- 화면 하단 접이식 패널(기본 180~240px, 최소화/복원).
- 데이터: SSE `/stream` 이벤트 + `/observability/external-logs`
  최근 항목을 시간순 머지한 이벤트 피드.
- 터미널 스타일: monospace, 레벨별 색(TS §5 상태색), 타임스탬프.
- 자동 스크롤 + 일시정지 토글, 레벨 필터(INFO/WARN/ERROR),
  최대 500행 ring buffer(메모리 상한).
- 민감값 마스킹: 토큰/키 패턴은 표시 전 마스킹(기존 redaction
  유틸 있으면 재사용, 없으면 최소 패턴 마스킹).

## US3: Command Palette (Cmd+K)

- Cmd+K / Ctrl+K 전역 단축키로 팔레트 오픈.
- 검색 대상: 뷰 이동(nav 항목), 최근 run(`/runs?limit=20`),
  agent sessions(`/agent-sessions?limit=50`), 알림(`/inbox`).
- 결과: 유형별 아이콘+라벨+메타, Enter로 이동/inspector 오픈.
- 최근 검색/즐겨찾기: localStorage 저장(최대 10개).
- 구현: 자체 컴포넌트(모달+input+결과 리스트), 외부 라이브러리
  추가 없이 — package.json에 cmdk 등 없으면 자체 구현.

## 비목표

- 팔레트 내 실제 명령 실행(run 취소 등) — v1은 네비게이션/조회만
- Inspector의 편집 기능 — read-only 상세
- Dock의 WebSocket — SSE/폴링 재사용

## 수용 기준

- tsc/lint/vitest/build 통과
- 키보드 접근성: ESC 닫기, 화살표 이동, 포커스 트랩
- reduced-motion 존중
