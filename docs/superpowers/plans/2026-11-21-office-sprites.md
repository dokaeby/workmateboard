# Agent-Office 스프라이트 사무실 모드 — 구현 계획

Spec: `docs/superpowers/specs/2026-11-21-office-sprites-design.md`
브랜치: `office-sprites` (agent-office 기반)

## Task 1 — 에셋·JS 포팅 + 브릿지 (subagent)

1. `claude-3d-frog-office/web/office/*.webp` 26개 + `LICENSE` →
   `src/jobmate/api/static/office/` (webp는 `static/office/office/` 아님 —
   플랫 유지, atlas가 `office/` 접두를 쓰므로 경로만 조정).
   최종 구조: `static/office/frog.webp` 등 + `static/office/*.js` + `static/office/office.css`.
2. `web/*.js` 7개 → `static/office/` 로 복사 후 아래 수정:
   - **DOM id 프리픽스**: `office-canvas→jmo-canvas`, `office-box→jmo-box`,
     `notice→jmo-notice`, `summary→jmo-summary`, `demo→jmo-demo`,
     `office-people→jmo-people`, `office-log→jmo-log`, `office-count→jmo-count`.
     (`office-panel.js`, `office-view.js` 양쪽; `#office-log`는 jobMate 감사로그와 충돌)
   - `office-view.js` `img.src` → `/static/office/${n}.webp` (document base가 `/`라
     상대경로 `office/`는 `/office/`로 풀려 404).
   - `load()`: `fetch("/api/office")` → `await window.JM_OFFICE_PROVIDER()`.
     provider 미정의 시 `{people:[]}` 반환.
   - `boot()` 자동실행 제거 → `window.JMOffice2D = {start, stop, onSelect}` 노출.
     `start()`는 멱등, `stop()`은 rAF/interval 해제. `frame()`/`load()`는
     stopped 상태면 early return. `select(id)` 성공 시 `onSelect?.(id)` 호출.
   - `applyText()`의 `document.title` 설정 제거(또는 `#jmo-title` 있을 때만).
   - `#lang button` 관련 코드는 jobMate에 없으므로 no-op 유지(선택).
3. `static/office/jobmate-bridge.js` 신규:
   - `window.JM_OFFICE_PROVIDER = async () => ({people: jmOfficePeople()})`
   - `jmOfficePeople()`: `officeState`(index.html 전역) + `OFFICE_NODES` 기반
     8명 people 생성. person.id = node code("ANA".."ORCH") — 선택 연동용.
   - status: stage `running`→`working`, 그 외 stage→`rest`.
     HUM: `run.status==="waiting_approval"`→`decision`(ask:"사람 승인 대기").
     ORCH: `running|queued`→`working`, `waiting_*|needs_human`→`decision`,
     나머지→`rest`.
   - doing: `{k:"text", a:"..."}` — stage running이면 최신 audit detail 또는
     "<label> 진행 중", succeeded→"완료", failed→"실패: <error 요약>",
     pending→"대기". run 없으면 people=[] 반환.
   - subs:[], subCount:0, kid:false, home:null, commits:null.
   - name: `"ANA 요구사항 분석"` 형태 (code + label).
4. `web/style.css` → `static/office/office.css`: id 셀렉터를 jmo- 프리픽스로
   치환해 복사 (#office-stage→#jmo-stage 등), body/#top 레이아웃 규칙은 제외
   (jobMate 레이아웃과 충돌) — `#jmo-wrap`, `#jmo-side`, 캔버스/people/log 규칙만.

## Task 2 — index.html 통합 + 테스트 (본 세션)

- `#view-office` `.office-status` 행에 모드 토글 버튼 2개 추가:
  `3D 보드` / `스프라이트 사무실` (aria-pressed).
- `#office-stage`(3D)와 `#jmo-wrap`(2D: stage+side 레이아웃)를 형제로 배치,
  모드에 따라 hidden 토글. `.office-split`(detail+log)는 공용 유지.
- 2D 첫 활성화 시 `<link office.css>` + script 8개를 순서대로 동적 삽입
  (i18n→world→atlas→path→life→panel→view→bridge), 로드 완료 후
  `JMOffice2D.start()`.
- `JMOffice2D.onSelect = id => { officeState.selected = id==="ORCH"?
  {type:"orch"}:{type:"stage",code:id}; officeRenderStage(); officeRenderDetail(); }`
- 모드 전환 시 상대 모드는 `JMOffice2D.stop()` / 3D는 그냥 hidden.
- UI 테스트 추가(tests/ui/test_dashboard.py): 토글 전환, `#jmo-canvas` 표시,
  seeded run 시 `officeDebug()` people 8명·status 매핑 확인.

## 검증

`uv run pytest` / `ruff check` / `ruff format --check` / `mypy` /
`graphify update .` — JS만 추가라 Python 검증은 회귀 확인용.
