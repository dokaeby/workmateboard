# jobMate UI 자동화 테스트 절차

| 항목 | 값 |
|---|---|
| 문서 버전 | 1.2.0 |
| 기준일 | 2026-11-21 |
| 관련 문서 | 13_UI_자동화_test_cases_v1.0.0.md |

## 문서 이력

| 버전 | 기준일 | 수정 내용 |
|---|---|---|
| 1.0.0 | 2026-11-19 | 최초 작성 |
| 1.1.0 | 2026-11-19 | 실제 구현 반영 — pytest + Playwright chromium, `tests/ui/` 자동화 추가 |
| 1.2.0 | 2026-11-21 | 코딩 에이전트 뷰 제거 → Agent-Office(`office`) 뷰 시나리오로 교체 |

## 환경 준비

1. dev 의존성 설치: `uv sync --extra dev` (playwright 포함).
2. 브라우저 설치: `uv run playwright install chromium` (최초 1회, ~290MB).
3. 별도 서버/DB/worker 준비 불필요 — `tests/ui/test_dashboard.py`의 fixture가
   임시 SQLite DB + `FakeLLM`(`jobmate.llm.FakeLLM`) + fake calendar 어댑터로
   실제 uvicorn 서버를 랜덤 포트에 띄우고 종료 시 정리한다.

## 실행

```bash
uv run pytest tests/ui -v          # UI 자동화만
uv run pytest                      # 전체 (UI 포함)
```

## 커버 범위 (현재 구현 기준)

| 테스트 | 내용 | 대응 케이스(13번 문서) |
|---|---|---|
| test_sidebar_navigation | 사이드바 5개 메뉴 전환·제목 표시 | UI-10 일부 |
| test_gmail_view_empty_state | 메시지 없음 빈 상태 | UI-05 일부 |
| test_calendar_view_empty_state | 등록 일정 없음 빈 상태 | UI-04/05 일부 |
| test_radar_view_platforms | 플랫폼 선택지 표시 | — |
| test_runs_view_table | 실행 이력 테이블 빈 상태 | UI-02 일부 |
| test_office_view_empty_state | dev_review run 없음 빈 상태 안내 | — |
| test_settings_dialog_save | 설정 저장 → GET /settings 반영 확인 | UI-04 일부 |
| test_push_toggle_without_vapid | VAPID 미설정 시 alert 안내·화면 유지 | UI-07 일부 |
| test_office_view_shows_dev_review_run | run+stage 시드→헤더 통계·노드·엣지 렌더링 | — |
| test_office_node_click_shows_stage_detail | 노드 클릭→스테이지 상세·이벤트 로그 표시 | — |
| test_office_waiting_approval_shows_approve_buttons | 승인 대기 run→승인/거절 버튼 표시 | — |
| test_office_approve_buttons_hide_when_run_leaves_waiting | run 상태 변경 시 승인 버튼 숨김 | — |
| test_calendar_review_reject | 검토 필요 카드→거부→rejected | — |
| test_calendar_review_approve_with_edit | 수정값 승인→캘린더 등록→registered | — |
| test_calendar_review_invalid_edit_rejected | 무효 수정값 승인 거부→needs_review 유지 | — |

## 미구현/수동 확인 필요 케이스

- UI-01/02: 로그인·권한 기반 실행 상세 (인증 계층 미구현)
- UI-03/08: Google 계정 연결·해제 UI (OAuth 플로우는 CLI `google-auth`만 존재)
- UI-06: 푸시 클릭 → Gmail 원본 이동 (실제 push 권한·실기기 필요)
- UI-09: 타 사용자 데이터 격리 (멀티유저 미구현)
- UI-10: 키보드 탐색·화면 확대 (부분 — nav 버튼은 `<button>`이라 포커스 가능)

실제 Google 계정/OAuth가 필요한 시나리오는 sandbox 계정 또는 deterministic
fake integration으로 수동 검증하고 결과를 기록한다. 개인 계정·실메일을
자동화 테스트에 사용하지 않는다.
