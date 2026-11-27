# Implementation Plan: jobMate 에이전트 하네스 엔진 MVP

**Branch**: `001-agent-harness-engine` | **Date**: 2026-11-19 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/001-agent-harness-engine/spec.md`

## Summary

6계층 하네스(가이드·센서·루프·메모리·권한·관찰) 위에 LangGraph 기반 실행 엔진을 구축한다.
`WorkflowSpec`/`AgentSpec`으로 단계를 선언하고, `dev_review`(분석→구현→자동검사→보안검토→코드리뷰→QA→사람승인)와
`gmail_calendar`(폴링→추출→검증→등록→알림) 두 워크플로를 동일 엔진으로 실행한다.
제공층은 FastAPI(임시), 저장은 SQLite(WAL), 모든 LLM/Google 호출은 Protocol 뒤에 두고 fake 주입 테스트를 지원한다.
리뷰·보안·QA 판정 구조와 모델 라우팅은 gstack·ui-ux-pro-max 스킬을 참조해 설계에 반영한다.

## Technical Context

**Language/Version**: Python 3.12+ (환경 3.15.2, uv 사용)

**Primary Dependencies**: fastapi, uvicorn, pydantic v2, pydantic-settings, httpx, langgraph, langgraph-checkpoint-sqlite
(개발: pytest, pytest-asyncio, ruff, mypy 또는 pyright — 스캐폴드 시 확정)

**Storage**: SQLite (WAL) — runs, stage_runs, audit_events, approvals, event_links, notifications, checkpoints

**Testing**: pytest — unit(센서/모델/예산), integration(fake LLM/Gmail/Calendar로 전체 워크플로), api(FastAPI TestClient)

**Target Platform**: macOS/Linux 로컬 개발 서버 (FastAPI + CLI)

**Project Type**: web-service (백엔드 API + CLI)

**Performance Goals**: MVP — run당 LLM 호출 수십 회 이내, 동시 run은 소수. 별도 처리량 목표 없음.

**Constraints**: 외부 호출 타임아웃 필수, 예산 상한(토큰/도구 호출/시간), 비밀값 비노출, 단일 프로세스 SQLite

**Scale/Scope**: 엔진 코어 ~2-3k LOC + 워크플로 2종 + API + 테스트

## Constitution Check

- I. 6계층 하네스: WorkflowSpec(가이드), 센서 모듈(검증), BudgetTracker+상한(루프), SQLite store(메모리),
  tool allowlist(권한), stage_runs/audit_events(trace_id)(관찰) — 설계에 모두 대응.
- II. 결정적 검사 우선: verdict 스키마 검증·심각도 강등·멱등성은 코드 강제, LLM judge는 보조.
- III. 멱등성: event_links.pending + marker 조회 + source 고유 제약.
- IV. Test-first: fake 어댑터로 전체 흐름 결정적 테스트.
- V. 최소 변경: MVP 범위만. Django/PostgreSQL/Web Push/문서 템플릿은 후속.
- VI. 비밀·개인정보: audit/로그에 본문·토큰 미기록.

위반 없음. Complexity Tracking 불필요.

## Project Structure

### Documentation (this feature)

```text
specs/001-agent-harness-engine/
├── plan.md              # 본 문서
├── spec.md              # 기능 스펙
├── checklists/
│   └── requirements.md  # 스펙 품질 체크리스트
└── tasks.md             # /speckit-tasks 출력
```

설계·API 상세는 기존 문서가 research/contracts 역할을 겸한다:
`docs/jobMate_v1.0.1/02_시스템상세설계서_v1.0.1.md 부록 A`, `docs/jobMate_v1.0.1/06_API_설계서_v1.0.1.md 부록 A`

### Source Code (repository root)

```text
jobMate/
├── pyproject.toml
├── src/jobmate/
│   ├── config.py            # pydantic-settings, 모델 라우팅 설정
│   ├── models/              # Pydantic 경계 모델 (runs, verdicts, findings, gmail)
│   ├── store/               # SQLite 저장소 + Protocol (runs/stage_runs/audit/links)
│   ├── sensors/             # 결정적 검증 (verdict 스키마, 심각도 강등, diff, 시크릿, URL, timezone)
│   ├── llm/                 # LLM Protocol, OpenAI호환 클라이언트, fake, 구조화 출력
│   ├── guide/               # prompts/ 에이전트 프롬프트 버전 관리 + 로더
│   ├── tools/               # tool allowlist 실행기 (read_diff/run_tests 등)
│   ├── budgets.py           # 예산/루프 상한 트래커
│   ├── engine/              # WorkflowSpec/AgentSpec, LangGraph runner, interrupt 승인
│   ├── workflows/           # dev_review.py, gmail_calendar.py
│   ├── adapters/            # Gmail/Calendar/Notifier Protocol + fake/real 구현
│   ├── api/                 # FastAPI 앱 + 라우터 + 정적 상태 페이지
│   └── cli.py               # run 제출/조회/승인 CLI
└── tests/
    ├── unit/
    ├── integration/
    └── api/
```

**Structure Decision**: 단일 Python 패키지(`src/jobmate`) + 계층 디렉터리. 제공층(FastAPI/CLI)은 도메인
엔진에 의존하고, 엔진은 HTTP 프레임워크에 의존하지 않는다(Django 전환 대비).

## Key Design Decisions

1. **엔진 중심 재구성**: 모든 워크플로는 `WorkflowSpec`(stage 목록 + 분기 규칙 + 루프 상한)으로 선언.
   LangGraph `StateGraph`는 spec을 해석해 노드를 생성한다. head 에이전트는 배정·분기만 담당.
2. **리뷰/보안/QA 분리**: verdict는 Pydantic 스키마로 강제. approve+blocker/major → request_changes 강등(센서).
   리뷰 findings는 gstack 스키마(severity/confidence/path/line/category/summary/fix/fingerprint/specialist).
3. **모델 라우팅**: `AgentSpec.model` → `ModelConfig{model, base_url, api_key_env, temperature, max_tokens}`.
   기본 티어(경량) + 중요 티어(강한) + 역할별 오버라이드. env/설정 파일 주입.
4. **구현 에이전트는 제안만**: `ChangeProposal`(unified diff 텍스트) 출력. 적용은 후속 샌드박스.
   자동 검사는 baseline 명령(diff 적용 가능성·시크릿 스캔·설정된 lint/test).
5. **사람 승인**: LangGraph `interrupt` + `Command(resume=...)`. SQLite checkpointer로 재개.
6. **관찰**: 모든 stage 입출력·판정·분기·승인을 `stage_runs`/`audit_events`에 run_id·trace_id로 기록.
7. **UI**: FastAPI 정적 단일 페이지. ui-ux-pro-max 미니멀/스위스 — 단일 액센트, 그리드·여백,
   그림자·그라디언트 배제, 상태는 색+텍스트 병행.

## Phase Plan

- Phase A (Foundational): 스캐폴드, config, models, store, sensors, llm, budgets
- Phase B (US1 P1): engine + dev_review 워크플로 + 테스트
- Phase C (US2 P2): gmail_calendar 워크플로 + 어댑터 + 테스트
- Phase D (US3 P3): FastAPI + CLI + 상태 페이지
- Phase E: 적대적 리뷰(사람+gstack 방식) → 수정 → pytest/ruff/mypy 검증 → docs/jobMate_v1.0.1/04 문서화
