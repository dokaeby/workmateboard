# workmateboard 보안 재검사 — 2026-11-24 (신규 기능 대상)

2026-11-23 전수 감사(`2026-11-23-security-audit.md`) 이후 추가된 기능
(function calling, MCP, SSE, 인젝션 방어, OAuth env 정렬, evals, CI)에 대한
표적 보안 검토. 기존 감사 항목(인증, Push SSRF, 경로 이탈, 의존성)은 재검증하지 않았다.

## 확인한 문제와 수정

| 문제 | 심각도 | 수정 |
| --- | --- | --- |
| `run_tests` 도구가 LLM 제어 `command` 인자를 그대로 `subprocess.run(command.split())`에 전달 — 프롬프트 인젝션 또는 모델 오작동 시 임의 명령 실행 가능(`bash -c`, `curl\|sh` 등 인터프리터 재진입) | 높음 | `tools/executor.py`에 명령 allowlist 추가 — `pytest`/`ruff`/`mypy`/`python -m pytest\|ruff\|mypy` 프리픽스만 허용, `&\|;<>`$()`\n` 셸 메타문자 전면 거부, 위반 시 `ToolDeniedError`. `tools/schemas.py` 스키마 설명에 허용 명령 명시 |
| `security.authorized()`의 `?api_key=`/`?token=` 쿼리 폴백이 **모든 `/api/*` 엔드포인트**에 적용 — 쿼리스트링 키는 브라우저 히스토리·프록시/엣지 액세스 로그·Referer로 유출 가능 | 중간 | 쿼리 폴백을 `request.url.path == "/api/v1/stream"`(SSE 전용)으로 제한. 헤더/쿠키 인증은 전 엔드포인트 유지 |

## 검토했으나 문제 없음으로 판단한 항목

- **SSE `Store.activity_since`**: 파라미터 바인딩 쿼리만 사용, SSE 이벤트는
  run/stage/session 메타데이터만 전송(stage output 원문 미포함).
- **tool loop 인자 처리**: `_extract_tool_calls`가 arguments를 dict로 정규화,
  `call(name, allowed, **args)`의 인자 충돌(`allowed` 키 등)은 TypeError →
  ERROR 문자열로 모델에 반환(예외 전파 없음). audit은 `{tool, arg_keys}`만 기록.
- **MCP 클라이언트**: 전용 백그라운드 스레드 asyncio 루프 — 동기 engine 컨텍스트에서
  `asyncio.run` 재진입 문제 없음. `mcp_<server>_<tool>` 프리픽스 + builtin 우선
  병합으로 이름 충돌/덮어쓰기 불가. MCP 서버는 stdio 로컬 신뢰 경계.
- **`wrap_untrusted` 라벨**: `"<>\n\r` 제거 후 80자 절단 — 속성 탈출 불가.
- **OAuth**: 2026-11-23 감사 범위(email_verified, subject 일치, state CSRF) 유지.
  `GOOGLE_OAUTH_*` 별칭 추가만으로 인증 로직 변경 없음. 토큰/코드 로깅 없음 확인.
- **evals 데이터셋**: 실제 키 패턴 제거 완료(`sk_live_****REDACTED_EXAMPLE****`).

## 검증

- 신규 테스트: `tests/unit/test_executor_security.py` 15개(거부 명령 11종 파라미터화,
  허용 명령 실행, 공백 정규화, allowlist 이중 강제), `test_sse.py`에 쿼리 키
  타 엔드포인트 거부 1개 추가.
- `pytest tests/unit tests/integration tests/api`: **317 passed, 4 skipped**.
- `ruff check`/`ruff format --check`: 통과. `mypy src/jobmate`: 64 파일 이슈 없음.

## 후속 조치 (동일 날짜, 미조치 권고 3건 처리)

| 권고 | 조치 |
| --- | --- |
| run_tests 허용 목록 확장 | `JOBMATE_TEST_COMMAND_PREFIXES` env(쉼표 구분) 추가 — 각 접두사는 `[A-Za-z0-9_.\-/ ]`만 허용, 메타문자 포함 시 무시. 기본 pytest/ruff/mypy는 항상 허용. 셸/인터프리터 바이너리는 env로도 불가 |
| SSE 쿼리 인증 | `POST /api/v1/stream/token` 신설 — 헤더/쿠키 인증된 클라이언트에 300초 단기 토큰(`sse_`+32B, sha256 해시만 메모리 저장, lazy 만료) 발급. `?api_key=` 폐기, `/api/v1/stream?token=`만 수용. `index.html`은 토큰 발급→EventSource 연결, 에러 시 재발급 재접속(10s) |
| MCP 원격 노출 | `mcp/server.py` transport 가드 — `JOBMATE_MCP_TRANSPORT`이 stdio가 아니면 `SystemExit`. HTTP/SSE transport 추가 시 인증 구현이 선행 조건임을 코드+README에 명시 |

### 후속 검증

- `pytest tests` 전체: **349 passed, 4 skipped**
- 신규 테스트: `test_stream_token.py` 3개(발급/검증/만료), `test_sse.py` 토큰 플로우·발급 엔드포인트 인증·타 엔드포인트 거부, `test_executor_security.py` env 확장 2개, `test_mcp_tools.py` transport 거부 1개
- ruff check/format, mypy: 전부 통과
