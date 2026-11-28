# spec 014 — 목업 화면 리스킨 (알림/활동로그/성능/작업)

## 목표

`~/Downloads/am/` 목업의 나머지 화면을 기존 뷰 데이터로 리스킨.
디자인 기준 `docs/UI_DESIGN_GUIDE.md`, 토큰은 spec 010 적용분 사용.

## 대상 화면과 데이터 소스

### US1: 알림 & 공지 (inbox 뷰)

- 상단 KPI: 전체/미해결(open)/해결(resolved) — `/observability/
  notifications?days=` 재사용(lead) 또는 inbox 자체 집계(member).
- 발생 추이 미니 차트(TrendArea 재사용).
- 알림 테이블: 심각도/유형/상태 컬럼 + 상태 필터 + 키워드 검색.
- 기존 알림 읽음/삭제 액션 유지.

### US2: 활동 로그 (audit 뷰)

- 이벤트 타임라인 터미널 스타일: 타임스탬프/레벨/행위자/액션/
  대상 — 기존 audit API(`/audit` 계열) 데이터.
- 필터: 기간, 행위자, 액션 유형, 키워드. 건수 집계 표시.
- monospace + 레벨 색상, virtualization 없이 최근 N건+더보기.

### US3: 성능 분석 (perf 뷰)

- KPI: 세션 수/성공률/평균 응답시간/RPS — llm_stats,
  agent_session_stats, runs/stats 데이터.
- RPS·응답시간 듀얼 라인 차트 + 작업 유형별 비중 도넛/바.
- 기존 차트 컴포넌트(charts.tsx) 재사용, 시리즈 색은 다크 팔레트.

### US4: 작업·워크플로우 (tasks/runs 뷰)

- 작업 KPI 카드 행.
- 퍼널: spec 011 `/observability/funnel` 재사용(lead) —
  member는 runs 단계 집계로 대체하거나 섹션 숨김.
- 진행 중 작업 카드: runs 목록에서 running 상태 카드형 표시
  (진행률은 stage_runs 기반 계산 가능한 범위만).

## 공통 규칙

- 데이터 없으면 Empty/"-"/"Not Configured" — 가짜 지표 금지.
- 기존 뷰의 액션(버튼/링크)은 유지, 레이아웃·스타일만 변경.
- i18n 키 ko/en 추가.

## 비목표

- 백엔드 변경 없음(기존+spec 011/012 엔드포인트만 사용)
- 새 기능(알림 발송, 로그 export 등)

## 수용 기준

- tsc/lint/vitest/build 통과
- 4개 뷰가 목업 레이아웃 구조(KPI→차트→테이블/카드)를 따름
