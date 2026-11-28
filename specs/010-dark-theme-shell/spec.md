# spec 010 — 다크 테마 + Agent Office 셸

## 목표

`docs/UI_DESIGN_GUIDE.md`의 미션 컨트롤 다크 테마를 v2 전체에 적용하고,
목업(`/Users/sec/Downloads/am/*.png`)의 셸 구조 — Global Header 상태바 +
Sidebar 하단 인프라 상태 — 를 구현한다.

## US1: 디자인 토큰 교체

- `v2/src/index.css` `@theme`를 가이드 §5 팔레트로 교체:
  - 배경: app `#070B12`, sidebar `#090F18`, panel `#0D1520`,
    elevated `#131E2C`, hover `#182638`, grid-line `#172331`, border `#26394E`
  - 액센트: `#00E5FF`(주), `#398BFF`, `#A78BFA`
  - 상태: ok `#00E6A0`, warn `#FFB020`, running `#FF8A34`, err `#FF475D`,
    muted `#64748B`
  - 텍스트: ink `#E8F1FA`, ink-2 `#A0B2C5`, muted `#8295AB`, disabled `#526174`
- 기존 토큰명(`bg`, `surface`, `border`, `ink`, `ink-2`, `muted`,
  `accent`, `accent-hover`, `accent-soft`, `accent-border`, `ok`,
  `ok-soft`, `ok-border`, `warn`, `warn-soft`, `warn-border`, `err`,
  `err-soft`, `err-border`)은 유지하되 값을 다크 팔레트로 매핑 —
  컴포넌트 수정 없이 전환되도록 한다.
- `*-soft` 계열은 다크 배경에서 읽히는 저채도 틴트로 재정의
  (예: accent-soft `rgba(0,229,255,0.08)`, border 동계열).
- `color-scheme: dark`로 변경. `html`/`body` 배경·텍스트 갱신.
- 활성/위험 테두리·glow 유틸: `border-accent-active`(rgba(0,229,255,.5)),
  `shadow-glow-cyan/green/red` 토큰 또는 유틸 클래스 추가.
- 모노폰트 스택 추가: `--font-mono` JetBrains Mono 계열
  (시스템 mono 폴백 허용 — 웹폰트 번들 금지).

## US2: Global Header 상태바

- `App.tsx` 헤더를 48~56px로 정리: 로고+제품명, 현재 workspace,
  연결 상태 표시등(SYSTEM ONLINE/DEGRADED/OFFLINE — SSE `/stream`
  연결 상태와 `/health` 계열로 판정), 마지막 데이터 수신 시각,
  실시간 모드 표시(기존 SSE 구독 상태), 기간 표시(뷰별).
- 상태 색상은 상태별 색 + 텍스트 라벨 병행(가이드 §4).

## US3: Sidebar 재편

- 너비 200~220px(데스크톱), 모바일은 기존 오버레이 유지.
- 메뉴를 Agent Office 그룹 순으로 재배치(관제→관리→스윔→이력→도구→
  MCP→보안→비용→성능→보고→프로젝트→설정 방향). 기존 `nav.ts`의
  `visibleNavItems`/prefs 게이트는 유지 — 순서·라벨만 조정.
- 사이드바 하단 인프라 박스: Collector/Backend 상태, Backend 버전,
  CPU/Memory%(시스템 뷰 데이터 소스 재사용, 없으면 "-").
- 선택 메뉴: cyan accent + 왼쪽 2px 강조선.

## 비목표

- 신규 기능 뷰 추가(스웜/퍼널/알림 대시보드는 spec 011)
- 라이트 테마 유지(다크 단일 — 필요 시 추후 토글)
- 차트 라이브러리 교체

## 수용 기준

- `pnpm build`/`tsc`/`oxlint`/`vitest` 통과
- 모든 뷰가 다크 팔레트로 렌더(밝은 배경 잔존 없음)
- 상태 표시등이 실제 연결 상태를 반영
