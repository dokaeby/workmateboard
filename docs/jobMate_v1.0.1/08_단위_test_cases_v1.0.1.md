# jobMate 단위 테스트 케이스

| 항목 | 값 |
|---|---|
| 문서 버전 | 1.0.1 |
| 기준일 | 2026-11-19 |
| 대상 | 도메인 로직, 검증기, 어댑터 |

## 문서 이력

| 버전 | 기준일 | 수정 내용 |
|---|---|---|
| 1.0.0 | 2026-11-19 | 최초 작성 |
| 1.0.1 | 2026-11-27 | 부록 A 추가: 하네스 MVP 테스트 케이스(docs/harness/03_테스트케이스.md) 통합 |

| ID | 대상 | 입력/상황 | 기대 결과 |
|---|---|---|---|
| UT-AUTH-01 | 조직 권한 검사 | 다른 조직의 run ID | 접근 거부 |
| UT-WF-01 | 템플릿 입력 검증 | 필수 필드 누락 | 필드 오류 반환, 실행 미생성 |
| UT-ORCH-01 | 노드 상태 전이 | queued → running → completed | 허용 전이 저장 |
| UT-ORCH-02 | 노드 재시도 | 외부 API 일시 오류 | 제한 횟수 내 재시도 예약 |
| UT-GMAIL-01 | 메일 일정 추출 | 명확한 날짜와 시각 | timezone 포함 정규화 결과 |
| UT-GMAIL-02 | 모호한 일정 | 시간대/날짜 불명확 | needs_review, Calendar 호출 없음 |
| UT-GMAIL-03 | 반복 일정 | 반복 주기 표현 포함 | MVP 자동 등록 제외 |
| UT-CAL-01 | 중복 키 | 같은 connection/message 재처리 | 기존 링크 반환, 신규 생성 없음 |
| UT-CAL-02 | Calendar timeout | 생성 결과 미확정 | marker 조회 후 멱등 재처리 |
| UT-PUSH-01 | 푸시 클릭 URL | 정상 Gmail message ID | 허용된 mail.google.com URL |
| UT-PUSH-02 | 악성 URL | 임의 외부 redirect URL | 거부 |
| UT-SEC-01 | 로그 마스킹 | OAuth 토큰/메일 본문 포함 오류 | 민감 값이 로그에 없음 |

## 통과 기준

모든 도메인 상태 전이와 경계값이 자동화된 단위 테스트로 검증되고, 실패 케이스에서 외부 부작용 호출이 발생하지 않아야 한다.

## 부록 A. 하네스 MVP 테스트 케이스 (구 docs/harness/03_테스트케이스.md)

> 원본: `docs/harness/03_테스트케이스.md` (문서 버전 0.1, 기준일 2026-11-19). docs/harness/는 폐기되고 본 문서로 통합됨.

### 1. Unit — sensors (`tests/unit/`)

| ID | 케이스 | 기대 |
|---|---|---|
| U-V01 | approve + findings 없음 | decision 유지 approve |
| U-V02 | approve + blocker finding(high) | request_changes 강등, summary에 강등 표기 |
| U-V03 | approve + major finding | request_changes 강등 |
| U-V04 | approve + minor/nit만 | approve 유지 |
| U-V05 | blocker + confidence=very_low | 차단 아님(approve 유지) |
| U-V06 | request_changes + minor | request_changes 유지 |
| U-V07 | filter_findings | very_low confidence finding 제거 |
| U-S01 | AWS 키 패턴 `AKIA...` | aws_key 검출 |
| U-S02 | private key 헤더 | private_key 검출 |
| U-S03 | 일반 텍스트 | 검출 없음 |
| U-D01 | `diff --git` 헤더 존재 | looks_like_diff=true |
| U-D02 | 일반 텍스트 | false |
| U-G01 | build_gmail_url | https://mail.google.com 형식 |
| U-G02 | is_safe_source_url | mail.google.com+https만 true, evil.com/http false |
| U-I01 | idempotency_key | 동일 입력 동일 키, connection 상이 시 상이 |
| U-E01 | 정상 이벤트 | validated |
| U-E02 | is_ambiguous | needs_review(ambiguous) |
| U-E03 | is_recurring | needs_review(recurring) |
| U-E04 | 잘못된 timezone | bad_timezone |
| U-E05 | end<=start | end_before_start |
| U-E06 | 빈 제목 | empty_title |
| U-E07 | confidence<0.5 | low_confidence |
| U-E08 | 깨진 datetime | bad_datetime |

### 2. Unit — llm/store/models

| ID | 케이스 | 기대 |
|---|---|---|
| U-L01 | extract_json 일반/fenced/embedded | JSON 추출 성공 |
| U-L02 | JSON 없는 출력 | ValueError |
| U-L03 | FakeLLM schema 지정 | Pydantic 인스턴스 반환 + usage 기록 |
| U-L04 | FakeLLM 호출 기록 | calls에 system/user 스냅샷 |
| U-DB01 | run 생성→상태 갱신→조회 | status 전이·정렬 정상 |
| U-DB02 | stage_run 시작/완료 | output·tokens 기록 |
| U-DB03 | reserve_link 동일 키 2회 | 두 번째 None(UNIQUE) |
| U-DB04 | update_link calendar_event_id | 조회 반영 |
| U-DB05 | notification pending→sent | 상태·sent_at 기록 |
| U-DB06 | audit 기록 | run_id별 조회·본문 미포함 구조 |
| U-M01 | Verdict 잘못된 decision | ValidationError |
| U-M02 | Finding summary 누락 | ValidationError |

### 3. Integration — dev_review (`tests/integration/test_dev_review.py`)

| ID | 케이스 | 기대 |
|---|---|---|
| I-D01 | 정상 흐름(전원 approve/pass) | analysis→…→approval 7단계 순서, waiting_approval |
| I-D02 | 사람 승인 | completed + pr_summary 메타 + approval 레코드 |
| I-D03 | 사람 거부 | rejected + 사유 audit |
| I-D04 | 리뷰 request_changes | implement 2회 실행(회귀), 피드백 포함 |
| I-D05 | approve+blocker finding | 센서 강등 → implement 회귀 |
| I-D06 | 리뷰 상한 초과(계속 request_changes) | needs_human, 추가 LLM 호출 없이 종료 |
| I-D07 | 깨진 verdict 출력 | 재시도 후 성공, approve 오인 없음 |
| I-D08 | 토큰 예산 소진 | budget_exhausted/failed/needs_human 안전 정지 |
| I-D09 | 자동검사 FAIL(diff 없음/시크릿) | implement 회귀(fix 루프), 리뷰 미진행 |
| I-D10 | implement BLOCKED/NEEDS_CONTEXT | needs_human 에스컬레이션 |
| I-D11 | 재시작 재개 | 완료 stage 재실행 없음(checkpoint), 승인 후 completed |
| I-D12 | run 간 격리 | stage_runs/audit가 run_id로만 조회됨 |

### 4. Integration — gmail_calendar (`tests/integration/test_gmail_calendar.py`)

| ID | 케이스 | 기대 |
|---|---|---|
| I-G01 | 정상 메일 | 이벤트 1건 + notified + source_url |
| I-G02 | 동일 메시지 재처리 | Calendar 생성 1회 유지, duplicate |
| I-G03 | 모호 일정 | needs_review, Calendar 호출 0 |
| I-G04 | 반복 일정 | needs_review, Calendar 호출 0 |
| I-G05 | Calendar 타임아웃 | marker 복구 → 재생성 없이 notified |
| I-G06 | 알림 실패 | notification_queued, Calendar 이벤트 유지 |
| I-G07 | 나쁜 timezone/DST | needs_review |
| I-G08 | end<=start | needs_review |
| I-G09 | confidence 낮음 | needs_review |
| I-G10 | 임의 redirect URL | is_safe_source_url 차단 |

### 5. API (`tests/api/test_api.py`)

| ID | 케이스 | 기대 |
|---|---|---|
| A-01 | GET /api/v1/health | 200 ok |
| A-02 | POST /runs dev_review | 202 → 백그라운드 실행 → waiting_approval, stages 7개 |
| A-03 | POST /runs 미등록 workflow | 404 unknown_workflow |
| A-04 | POST /runs task 빈 문자열 | 422 |
| A-05 | GET /runs/{없음} | 404 + error.request_id |
| A-06 | POST approve (waiting) | 200 completed |
| A-07 | POST approve (queued 상태) | 409 invalid_state |
| A-08 | GET notifications/event-links 빈 목록 | 200 [] |
| A-09 | GET / | index.html 서빙 |

### 6. 보안·적대적 케이스

| ID | 케이스 | 기대 |
|---|---|---|
| S-01 | diff에 AWS키/토큰 포함 | secret_scan FAIL → implement 회귀 |
| S-02 | verdict JSON 파싱 불가 | 재시도/실패, approve 오인 없음 |
| S-03 | 리뷰어가 allowlist 외 도구 호출 | ToolDeniedError |
| S-04 | 프롬프트 인젝션 문자열이 입력에 포함 | 데이터로만 처리, 권한 변경 없음 |
| S-05 | source_url이 evil.com | 알림 시 build_gmail_url로 재생성 |
| S-06 | run 메타/audit에 본문·토큰 | 기록되지 않음(구조적 필드만) |

### 7. 커버리지 목표

- 핵심 분기(approve/request_changes/pass/fail/needs_human)·상한·멱등·복구는 전부 자동화.
- real LLM/Google 호출 경로(OpenAICompatLLM, google 어댑터)는 fake 계약과 동일 Protocol이라
  단위 수준 검증은 fake로 대체 — 실계정 검증은 상용 전환 시 수동 체크리스트로 분리.
