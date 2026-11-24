# jobMate MCP 연동

MCP(Model Context Protocol)를 양방향으로 지원한다.

## 1. MCP 서버 — jobMate 데이터를 외부에 노출

`python -m jobmate.mcp.server` (stdio transport). Claude Desktop 등 MCP
클라이언트의 설정 예:

```json
{
  "mcpServers": {
    "jobmate": {
      "command": "python",
      "args": ["-m", "jobmate.mcp.server"],
      "cwd": "C:/Users/Playdata/aio4/workmateboard"
    }
  }
}
```

노출 도구 (모두 읽기 전용, Store 공개 메서드만 사용):

| 도구 | 설명 |
|---|---|
| `list_runs(status?, limit)` | run 목록 (status: queued/running/completed/failed/...) |
| `get_run(run_id)` | run 상세 |
| `list_inbox_items(status?, limit)` | 메시지 인박스 (processing_status: pending/processed/failed) |
| `list_scheduled_events(status?, limit)` | 일정 후보 (candidate/needs_review/registered/rejected/...) |
| `run_stats(days)` | 최근 N일 run 통계 (상태별/워크플로별) |

환경변수:

- `JOBMATE_DB_DSN` — Store DSN/경로. 미설정 시 `Settings.database_dsn()`
  (`database_url` → `db_path`, 기본 `data/jobmate.db`) 사용.
- `DATABASE_URL` / `POSTGRES_URL` 등은 `database_dsn()`이 자동 인식.

## 2. MCP 클라이언트 — 외부 MCP 서버를 에이전트 도구로

`JOBMATE_MCP_*` env로 외부 stdio MCP 서버를 설정하면 `registry_builder`가
`ToolExecutor`에 `mcp_<server>_<tool>` 이름의 도구를 추가한다. 서버 다운/미설정
시 빈 목록 — 파이프라인을 중단시키지 않는다.

설정 형식 (택1):

```bash
# 권장: JSON 배열
JOBMATE_MCP_SERVERS='[{"name":"fs","command":"npx","args":["-y","@modelcontextprotocol/server-filesystem","/tmp"]}]'

# 또는 세미콜론 구분 "name=command args..."
JOBMATE_MCP_SERVERS="fs=npx -y @mcp/server-fs /tmp; gh=uvx mcp-server-github"

# 단일 서버 단축형
JOBMATE_MCP_SERVER_CMD="npx -y @modelcontextprotocol/server-filesystem /tmp"
JOBMATE_MCP_SERVER_NAME="fs"        # 선택, 기본 "default"
JOBMATE_MCP_SERVER_ARGS="--flag x"  # 선택, CMD 뒤에 추가
```

- MCP 호출은 백그라운드 스레드의 전용 asyncio 루프에서 실행된다
  (ToolExecutor의 동기 `fn(**kwargs)` 계약과 호환).
- 각 MCP 도구는 `tools/schemas.py`의 `register_tool_schema`로 스키마가
  등록된다 (파일이 아직 없으면 조용히 건너뜀).

### 에이전트 allowlist에 MCP 도구 쓰기

`AgentSpec.tools`에 `mcp_<server>_<tool>` 이름을 추가하면 해당 에이전트가
사용할 수 있다. 예: `tools=["read_file", "mcp_fs_read_file"]`. 스키마는
`tools/schemas.py`의 `openai_tools()`에 자동 포함된다.
