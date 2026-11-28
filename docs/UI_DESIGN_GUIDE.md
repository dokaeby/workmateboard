# jobMate v2 UI 디자인 가이드

작성일: 2026-11-29 · 상태: 확정 방향 (사용자 지시 반영)
대상: `v2/` React 프론트엔드 전체. 레거시 `src/jobmate/api/static/`는 유지보수
기간 코드이므로 본 가이드 적용 대상에서 제외한다.

## 1. 디자인 컨셉

전체 디자인은 아래 스타일을 결합한다.

- Futuristic AI Mission Control
- Premium Dark Mode SaaS
- Cybersecurity Operations Center
- Real-time Agent Command Center
- Scientific Research Dashboard
- High-density Technical Monitoring Interface
- Minimal Cyberpunk Aesthetic

시각적으로 미래지향적이지만 실제 업무 환경에서 장시간 사용할 수 있도록
구성한다. 화려한 게임 UI가 아닌 **전문적인 엔터프라이즈 관제 시스템**처럼
보여야 한다.

### 시각적 레퍼런스

- FLOWIRE AI Runtime Security 대시보드 (틸/시안 액센트, KPI 카드,
  요청·탐지 추이 차트, 위험 수준 분포, 검사 파이프라인 플로우)
- AI 에이전트 파이프라인 캐러셀 (번호 매긴 단계, LIVE 배지, 터미널 로그)

## 2. 반드시 적용할 디자인 요소

- 거의 검은색에 가까운 전체 배경
- 다크 네이비 계열의 패널
- 얇고 정교한 네온 테두리
- 실행 중인 에이전트의 은은한 발광 효과
- 상태별 컬러 시스템
- 고밀도 데이터 카드
- 실시간 그래프
- 연결선 기반 에이전트 관계도
- 터미널 스타일 이벤트 로그
- 기술적인 느낌의 타이포그래피
- 은은한 그리드 배경
- 작은 상태 표시등
- 모듈형 대시보드
- 단계별 실행 파이프라인
- 실시간 위험 탐지 패널

## 3. 피해야 할 디자인

- 일반적인 Bootstrap 관리자 페이지
- 지나치게 큰 여백
- 불필요하게 큰 카드
- 과도한 둥근 모서리
- 과도한 그라데이션
- 모든 패널에 강한 네온 효과
- 가독성을 해치는 애니메이션
- 의미 없는 장식용 그래프
- 실제 데이터처럼 표시되는 가짜 지표
- 불필요한 3D 효과
- 과도한 유리 질감
- 저해상도 이미지 기반 UI

## 4. 디자인 원칙

- 색상만으로 상태를 구분하지 않는다 — 아이콘과 텍스트 레이블을 함께 제공한다.
- 일반 패널에는 강한 그림자를 쓰지 않는다. Glow는 활성 에이전트,
  선택된 카드, 보안 경고에만 제한적으로 적용한다.
- 정의되지 않은 지표(위험 점수 등)를 임의로 생성하지 않는다. 수집 불가
  지표는 표시하지 않거나 "Not Configured"로 명시한다.
- 실제로 수행하지 않는 동작(실행 차단, 재실행 등)을 UI가 암시하지 않는다.
  Observer 환경에서는 탐지·경고만 제공하고 BLOCKED 표시는 실제 차단이
  연결된 경우에만 사용한다.
- 민감 정보(API 키, 토큰, 개인정보, tool 인자/결과)는 기본 마스킹한다.
- LLM의 비공개 내부 추론을 수집·추측해 표시하지 않는다. 공개된 실행
  이벤트, 계획, 요약, 도구 호출, 응답만 표시한다.

## 5. 디자인 토큰

CSS Variable / Tailwind `@theme` 기반 통합 시스템. 현재 `v2/src/index.css`의
`@theme` 블록을 아래 팔레트로 교체·확장한다.

### 5.1 배경

| 토큰 | 값 | 용도 |
|---|---|---|
| `--color-app-bg` | `#070B12` | 앱 전체 배경 |
| `--color-sidebar-bg` | `#090F18` | 좌측 내비게이션 |
| `--color-panel` | `#0D1520` | 기본 패널 |
| `--color-panel-elevated` | `#131E2C` | elevated 패널(모달·인스펙터) |
| `--color-hover` | `#182638` | 호버 배경 |
| `--color-grid-line` | `#172331` | 은은한 그리드 배경 |
| `--color-border` | `#26394E` | 기본 테두리 |

### 5.2 액센트

| 토큰 | 값 | 용도 |
|---|---|---|
| `--color-accent` | `#00E5FF` | Electric Cyan — 선택·활성·링크 |
| `--color-accent-blue` | `#398BFF` | Neon Blue — 보조 액센트·차트 |
| `--color-accent-purple` | `#A78BFA` | Agent Purple — 플래너/추론 계열 |

### 5.3 상태 색상

| 상태 | 색상 | 토큰 |
|---|---|---|
| Success | `#00E6A0` | `--color-ok` |
| Warning | `#FFB020` | `--color-warn` |
| Running | `#FF8A34` | `--color-running` |
| Critical | `#FF475D` | `--color-err` |
| Muted | `#64748B` | `--color-muted` |

### 5.4 에이전트 상태 → 색상 매핑

| 상태 | 색상 |
|---|---|
| Idle | Gray |
| Connected | Green |
| Running | Cyan |
| Planning | Purple |
| Thinking | Blue |
| Executing Tool | Orange |
| Waiting | Amber |
| Completed | Green |
| Failed | Red |
| Blocked | Red |
| Paused | Yellow |
| Disconnected | Gray |

### 5.5 텍스트

| 토큰 | 값 |
|---|---|
| `--color-ink` (Primary) | `#E8F1FA` |
| `--color-ink-2` (Secondary) | `#A0B2C5` |
| `--color-muted` | `#8295AB` |
| `--color-disabled` | `#526174` |

### 5.6 테두리·발광

- 일반 패널: `1px solid #26394E`
- 활성 패널: `1px solid rgba(0,229,255,0.5)`
- 위험 패널: `1px solid rgba(255,71,93,0.6)`
- Border radius: 6~10px
- Glow (제한적):
  - Cyan `0 0 16px rgba(0,229,255,0.12)`
  - Green `0 0 12px rgba(0,230,160,0.12)`
  - Red `0 0 16px rgba(255,71,93,0.18)`

## 6. 타이포그래피

| 용도 | 폰트 | 비고 |
|---|---|---|
| 일반 UI | Inter 또는 Geist / Pretendard·Noto Sans KR | 기존 Pretendard 스택 유지 가능 |
| 기술적 헤더 | Space Grotesk | 대문자 + letter-spacing |
| 숫자·로그 | JetBrains Mono | tabular-nums 필수 |
| 코드 | IBM Plex Mono | 로그·코드 블록 |

글자 크기:

| 용도 | 크기 |
|---|---|
| 페이지 제목 | 22~28px |
| 섹션 제목 | 14~16px |
| 카드 제목 | 11~13px |
| 주요 KPI 숫자 | 26~36px (`tabular-nums`) |
| 일반 본문 | 13~14px |
| 보조 정보 | 11~12px |
| 터미널 로그 | 11~12px (모노) |

영문 기술 레이블은 대문자 + letter-spacing. 한국어 문구는 자연스럽게.
실시간 숫자 갱신 시 레이아웃이 흔들리지 않도록 KPI에 `tabular-nums` 적용.

## 7. 전체 화면 레이아웃

Desktop First, 반응형 지원. 기준 해상도: 1920×1080 / 2560×1440 /
1440×900 / 1280×800 / Ultrawide / Tablet / Mobile.

### A. Global Header (높이 48~56px)

- 제품 로고, "LLM & Agent Observer" 명칭
- 현재 Workspace / Environment, Local/Remote 구분
- 연결 상태 표시등: SYSTEM ONLINE / DEGRADED / OFFLINE
- 실시간 모드 ON/OFF, 마지막 데이터 수신 시각
- 기간 선택, 글로벌 검색, 알림 센터, 설정, 사용자 프로필

### B. Left Navigation Sidebar (너비 200~220px, 접힘 56~64px)

메뉴: Overview / Live Agents / Agent Swarm / LLM Monitor /
Execution Traces / Tool Calls / MCP Monitor / Sessions /
Security Center / Policy Management / Token & Cost / Performance /
System Health / Alerts / Reports / Settings

- 선택 메뉴: Cyan accent + 얇은 왼쪽 강조선
- 하단 고정 정보: Observer Core 상태, Collector 연결 상태,
  Backend 버전, 현재 Environment, 데이터 수집 지연,
  Local LLM 연결 상태

### C. Main Dashboard — 12-column grid, gap 12~16px

### D. Inspector Panel — 우측 접이식 상세 패널(기본 320~420px).
선택한 에이전트·Trace·Tool Call·보안 이벤트 상세.

### E. Bottom Event Dock — 하단 접이식 실시간 이벤트 패널
(기본 높이 180~240px). 최소화/최대화/크기 조절 지원.

## 8. 화면별 구성

### 8.1 Overview Dashboard

- **KPI Summary Cards (6~8개)**: Active Agents, Running Sessions,
  Total LLM Requests, Tokens Used, Total Cost, Success Rate,
  Average Latency, Security Threats. 구성: 작은 아이콘 + 지표명 +
  큰 숫자 + 이전 기간 대비 변화량 + 미니 Sparkline + 데이터 기준 시간 +
  상태 컬러 + Tooltip. 클릭 시 상세 화면 이동. 실시간 갱신 시 카드 전체
  깜빡임 방지.
- **Live Agent Status Panel**: Agent ID/Name/Provider/Model/Session/
  현재 작업/상태/시작 시각/경과 시간/Token/Cost/Tool Call 수/
  Sub-agent 수/마지막 Heartbeat. 실행 중 에이전트는 상태 표시등 +
  은은한 Pulse. 오류는 Red accent. 클릭 시 Inspector Panel 표시.
- **Agent Swarm Visualization**: 메인 에이전트↔서브에이전트 실행 관계의
  인터랙티브 Node Graph. 노드 타입: User Input / Main Agent / Planner /
  Reasoning / Research / Coding / Reviewer / Testing / Security /
  Tool / MCP Server / Database / External API / LLM Model / Output.
  노드 표시: 이름·역할·상태·현재 작업·진행률·Token·Latency·Error 수.
  Edge: parent-child, delegation, message, tool invocation, data flow,
  dependency, result return. 실제 이벤트 기반으로 연결선 활성화,
  실행 중인 연결선에만 흐름 애니메이션. Zoom/Pan/Fit/선택/접기·펼치기/
  Focus/상태·Provider·Session 필터/그룹화/타임라인 재생/전체화면.
  노드 과다 시 그룹화·클러스터링·레벨별 상세. React Flow 등 검토.
- **Security Radar**: Security Health Score, Critical/High/Medium/Low
  이벤트, Blocked Actions, Policy Violations, Prompt Injection 탐지,
  민감 데이터 접근, 의심 Tool Call. 위험 수준별 Donut Chart.
  점수는 실제 규칙·평가 결과 기반만. 미활성 시 "Not Configured".
- **LLM Request & Token Throughput**: RPS, Tokens/s, Input/Output/Cached
  Tokens, Failed Requests, Active Connections 멀티 시리즈 라인 차트.
  시간 범위·Tooltip·Legend 토글·Zoom·Brush·Model/Provider 필터·
  Auto refresh·Live mode·Pause. 실시간 차트는 Ring Buffer 또는
  다운샘플링으로 메모리 무한 증가 방지.
- **Live Event Stream**: 터미널 스타일 실시간 로그.
  컬럼: Timestamp/Level/Provider/Agent/Session/Event Type/Message/Duration.
  레벨: TRACE/DEBUG/INFO/SUCCESS/WARNING/ERROR/CRITICAL/BLOCKED.
  이벤트 유형: agent.*, llm.*, tool.*, mcp.*, file.*, command.*,
  policy.violation, security.*. Virtualized List, 자동 스크롤(일시정지
  가능), 검색/정규식/레벨/에이전트/시간 필터, 복사, JSON/CSV Export,
  상세 열기. 민감값 기본 마스킹.
- **Execution Pipeline**: User Input → Input Validation → Agent Planning
  → LLM Request → Tool Selection → Tool Execution → Result Validation
  → Security Check → Final Output. 단계별 이름·상태·시작/종료 시각·
  소요 시간·입출력 크기·Token·Error·Retry 수. 완료=Green Check,
  현재=Cyan/Orange Glow, 실패=Red Cross. 분기·반복은 선형 파이프라인
  대신 DAG/반복 구조로 표현.

### 8.2 Live Agents 상세

- Grid View / Table View 전환.
- Agent Card: Provider 아이콘, 이름, Model, Role, Status, 현재 작업,
  Progress, 경과 시간, Tokens, Cost, Tool Calls, Sub-agents,
  Last Activity.
- Agent Detail 탭: Overview / Activity / Execution Trace / Tool Calls /
  LLM Requests / Files / Messages / Security / Metrics / Raw Events.
- 지원 환경에서만 Pause/Resume/Cancel. 권한 없는 에이전트는 Read-only.
  **실제 기능 없는 버튼을 만들지 않는다.**

### 8.3 LLM Monitor

모델·Provider별 성능/비용 비교. 지표: Total/Success/Failed Requests,
Error Rate, Input/Output/Cached Tokens, Tokens/s, TTFT, E2E Latency,
P50/P95/P99, Context Window Usage, Estimated/Actual Cost(구분 표시,
가격 기준일·통화 명시), Rate Limit Usage.
차트: Model Usage Distribution, Provider Request Distribution,
Token Usage Timeline, Cost Timeline, Latency Percentiles,
Error Rate Trend, Throughput Comparison, Context Window Utilization.

Local LLM 전용 지표(Apple Silicon): Model Loaded/Size/Quantization/
Backend, Prompt/Generation Speed, Loading Time, Memory Usage,
GPU Utilization, Active Requests, Queue Length, Context Length.
Unified Memory·CPU·GPU·Metal 중 실제 수집 가능 항목만 표시.
GPU 메모리·온도 등 수집 불가 항목은 추정 표시 금지.

### 8.4 Execution Traces

OTel Trace/Span 구조: Trace ID, Span ID, Parent Span ID, Agent ID,
Session ID, Provider, Model, Operation, Start/End, Duration, Status,
Attributes, Events.
시각화: Waterfall Timeline, Gantt-style, Nested Span Tree,
Agent Dependency Graph, Critical Path, Latency Breakdown.
Span 선택 시: 입력/출력 데이터, 관련 Tool Call, 모델 정보, 토큰,
실행 시간, 오류, Retry 이력, Security Findings, 관련 이벤트.

### 8.5 Tool Calls & MCP Monitor

Tool Call: Name/Type/Provider/Agent/Session/Arguments/Result/Status/
Duration/Retry/Error/Risk Level. Type: File Read/Write, Shell,
Browser, Web Search, Database, HTTP, Git, MCP Tool, Custom.
JSON Viewer + 긴 결과 접기. 민감 Arguments/Result 기본 마스킹.
MCP Monitor: Server Name, Connection Status, Transport, 사용 가능
도구, 호출 수, 평균 지연, 오류율, 마지막 Heartbeat, Permission Scope,
Security Events.

### 8.6 Security Center

위험 탐지 유형: Prompt/Indirect Injection, Sensitive Data Exposure,
Secret Leakage, Unauthorized File Access, Unsafe Shell Command,
Suspicious Network Request, Unauthorized Tool Usage, Excessive
Token/Cost, Infinite Loop, Abnormal Retry, Policy Violation,
Unexpected File Modification.

Red Flag Scanner UI: 검사 항목별 카드(항목·설명·위험 수준·탐지 근거·
관련 Agent·Trace·탐지 시간·결과·권장 대응). 결과 상태: PASSED /
WARNING / FAILED / BLOCKED / NOT CHECKED.
우측 Verdict Panel: Overall Risk, Total Red Flags, Critical Findings,
Policy Violations, Recommended Action.

Security Policy(지원 경로에서만): Allowed/Denied Tools, Allowed/Denied
Paths, Network Allow/Denylist, Max Token/Cost/Execution Time/Retry,
Human Approval Required. **관찰(탐지·경고)과 실제 차단(훅·프록시·
샌드박스·정책 집행 계층 연결 시에만)을 명확히 구분한다.**

### 8.7 Token & Cost Analytics

KPI: Today/Monthly Cost, Total/Cached Tokens, Cost per Agent/Session/
Successful Task, Estimated Monthly Cost, Budget Remaining.
차트: Daily Cost Trend, Cost by Provider/Model/Agent, Token Distribution,
Budget Utilization, Input vs Output Tokens.
비용 한도·경고 임계값 설정. 무료/로컬 모델은 API 비용과 시스템 운영
비용을 구분한다.

### 8.8 System Health

CPU/Memory/GPU/Unified Memory/Disk/Network/Active Processes,
Collector Status, Database Status, WebSocket Connections,
Event Queue Size, Processing Rate/Latency, Dropped Events,
Observer Process CPU/Memory.
Rust Collector 적용 시: 상태, 파싱 처리량, 큐, Worker 수, Batch Size,
평균 처리 시간, 메모리 할당 지표, Python-Rust 경계 오버헤드.
**실측 가능한 지표만 표시한다.**

### 8.9 Session History & Replay

세션 정보: ID/Project/Provider/Agent/Model/Start-End/Duration/Status/
Tokens/Cost/Tool Calls/Errors/Security Events.
Replay: Play/Pause/Seek/Step/속도(0.5x~5x), Event Timeline,
Agent Graph Replay, Execution Pipeline Replay.
재생은 기록된 이벤트의 시각적 재현 — 실제 Tool Call·외부 명령을
재실행하지 않는다.

### 8.10 Global Search & Command Palette

Cmd/Ctrl+K. 검색 대상: Agent/Session/Trace/Tool/MCP Server/LLM Model/
Error/Security Event/Project/Log Message. 유형별 아이콘+메타데이터,
최근 검색·즐겨찾기.

## 9. 기존 v2 구조와의 매핑

| 가이드 영역 | 기존 v2 대응 |
|---|---|
| Global Header | `App.tsx` 상단바 — 연결 상태·기간 선택·검색 추가 필요 |
| Left Sidebar | `app/nav.ts` 기반 사이드바 — 아이콘 레일+하단 상태 정보 추가 |
| Overview | `features/office/` 관측 센터 — KPI/라이브/차트 구조 유지, 다크 리스킨 |
| Live Agents | `features/agents/` + 세션 뷰 |
| Agent Swarm | 신규 — node graph 라이브러리 검토 필요 |
| LLM Monitor | `features/monitor/` + cost/usage 뷰 통합 |
| Execution Traces | `features/traces/` — waterfall 유지, span 상세 확장 |
| Tool Calls | 신규 뷰 또는 traces 하위 — audit 데이터 기반 |
| MCP Monitor | 신규 — MCP 서버 연동 시에만 실데이터 |
| Security Center | `features/risks/` 확장 또는 신규 |
| Token & Cost | `features/cost/` |
| System Health | `features/system/` — 실측 가능 지표만 |
| Session Replay | `features/sessionlog/` |
| Command Palette | 신규 — Cmd+K 글로벌 검색 |
| Inspector/Dock | 신규 레이아웃 요소 — 우측 패널·하단 도크 |

## 10. 구현 시 필수 준수 사항

- 모든 색상은 §5 토큰만 사용 — 하드코딩 색상 금지
- KPI 숫자는 `tabular-nums` 적용
- 실시간 목록은 virtualization 적용(로그·에이전트 목록)
- 실시간 차트는 ring buffer/다운샘플링으로 메모리 상한 확보
- 애니메이션은 상태 표시·pulse 등 의미 있는 것만, `prefers-reduced-motion` 존중
- 모바일에서는 sidebar/dock/inspector를 오버레이 시트로 전환
