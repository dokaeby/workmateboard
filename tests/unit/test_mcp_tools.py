"""MCP 클라이언트 브리지/도구 엔트리 변환 테스트 — 실제 서브프로세스 없이 fake 사용."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from jobmate.mcp import client as mcp_client
from jobmate.mcp.client import (
    MCPServerConfig,
    MCPToolSource,
    _parse_servers_env,
    _result_text,
    _safe_name,
    load_server_configs,
    mcp_tool_entries,
)


def _tool(name: str, description: str = "", schema: dict | None = None) -> Any:
    return SimpleNamespace(
        name=name,
        description=description,
        inputSchema=schema or {"type": "object", "properties": {}},
    )


class FakeSource:
    """MCPToolSource와 같은 표면을 가진 in-memory fake."""

    def __init__(self, tools: list[tuple[str, Any]], result: str = "ok") -> None:
        self._tools = tools
        self._result = result
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def configured(self) -> bool:
        return True

    def iter_tools(self):
        return iter(self._tools)

    def call(self, server: str, tool: str, arguments: dict[str, Any]) -> str:
        self.calls.append((server, tool, arguments))
        return self._result


# ---------- env 파싱 ----------


def test_parse_servers_env_json() -> None:
    raw = json.dumps([{"name": "fs", "command": "npx", "args": ["-y", "server-fs", "tmp"]}])
    configs = _parse_servers_env(raw)
    assert configs == [MCPServerConfig(name="fs", command="npx", args=["-y", "server-fs", "tmp"])]


def test_parse_servers_env_semicolon() -> None:
    configs = _parse_servers_env("fs=npx -y server-fs tmp; gh=uvx mcp-github")
    assert configs[0].name == "fs"
    assert configs[0].command == "npx"
    assert configs[0].args == ["-y", "server-fs", "tmp"]
    assert configs[1].name == "gh"
    assert configs[1].command == "uvx"


def test_parse_servers_env_empty() -> None:
    assert _parse_servers_env("") == []
    assert _parse_servers_env("   ") == []


def test_load_server_configs_single_shortcut() -> None:
    env = {
        "JOBMATE_MCP_SERVER_CMD": "npx -y server-fs tmp",
        "JOBMATE_MCP_SERVER_NAME": "fs",
        "JOBMATE_MCP_SERVER_ARGS": "--verbose",
    }
    configs = load_server_configs(env)
    assert len(configs) == 1
    assert configs[0].name == "fs"
    assert configs[0].command == "npx"
    assert configs[0].args == ["-y", "server-fs", "tmp", "--verbose"]


def test_load_server_configs_default_name() -> None:
    configs = load_server_configs({"JOBMATE_MCP_SERVER_CMD": "my-mcp-server"})
    assert configs[0].name == "default"


def test_load_server_configs_none() -> None:
    assert load_server_configs({}) == []


# ---------- 엔트리 변환 ----------


def test_mcp_tool_entries_format() -> None:
    schema = {"type": "object", "properties": {"path": {"type": "string"}}}
    src = FakeSource([("fs", _tool("read_file", "read a file", schema))])
    entries = mcp_tool_entries(source=src)
    assert len(entries) == 1
    e = entries[0]
    assert e["name"] == "mcp_fs_read_file"
    assert e["server"] == "fs"
    assert e["tool"] == "read_file"
    assert e["description"] == "read a file"
    assert e["parameters"] == schema
    assert callable(e["call"])
    assert e["call"]({"path": "tmp/x"}) == "ok"
    assert src.calls == [("fs", "read_file", {"path": "tmp/x"})]


def test_mcp_tool_entries_name_sanitized() -> None:
    src = FakeSource([("my server", _tool("weird.tool/name"))])
    entries = mcp_tool_entries(source=src)
    assert entries[0]["name"] == "mcp_my_server_weird_tool_name"


def test_mcp_tool_entries_missing_schema_defaults() -> None:
    src = FakeSource([("s", SimpleNamespace(name="t", description=None, inputSchema=None))])
    entries = mcp_tool_entries(source=src)
    assert entries[0]["parameters"] == {"type": "object", "properties": {}}
    assert "MCP tool t" in entries[0]["description"]


def test_mcp_tool_entries_unconfigured_returns_empty() -> None:
    class Empty(FakeSource):
        def configured(self) -> bool:
            return False

    assert mcp_tool_entries(source=Empty([])) == []


def test_safe_name() -> None:
    assert _safe_name("mcp", "a b", "c.d") == "mcp_a_b_c_d"


# ---------- 결과 텍스트 추출 ----------


def test_result_text_joins_content() -> None:
    result = SimpleNamespace(
        isError=False,
        content=[SimpleNamespace(text="hello"), SimpleNamespace(text="world")],
        structuredContent=None,
    )
    assert _result_text(result) == "hello\nworld"


def test_result_text_error_flag() -> None:
    result = SimpleNamespace(
        isError=True,
        content=[SimpleNamespace(text="boom")],
        structuredContent=None,
    )
    assert "MCP tool error" in _result_text(result)
    assert "boom" in _result_text(result)


def test_result_text_structured_fallback() -> None:
    result = SimpleNamespace(isError=False, content=[], structuredContent={"a": 1})
    assert json.loads(_result_text(result)) == {"a": 1}


# ---------- graceful failure (실제 서브프로세스 없이) ----------


def test_source_unreachable_server_yields_nothing() -> None:
    src = MCPToolSource(
        [MCPServerConfig(name="dead", command="definitely-not-a-real-mcp-cmd-xyz")],
        timeout=10,
    )
    try:
        assert list(src.iter_tools()) == []
        out = src.call("dead", "whatever", {})
        assert "MCP error" in out
        assert src.call("nope", "t", {}) == "MCP error: unknown server 'nope'"
    finally:
        src.close()


def test_source_not_configured() -> None:
    src = MCPToolSource([], timeout=5)
    assert not src.configured()
    assert list(src.iter_tools()) == []


# ---------- registry_builder 연동 ----------


def test_mcp_extra_tools_empty_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from jobmate.workflows import registry_builder

    monkeypatch.delenv("JOBMATE_MCP_SERVERS", raising=False)
    monkeypatch.delenv("JOBMATE_MCP_SERVER_CMD", raising=False)
    assert registry_builder._mcp_extra_tools() == {}


def test_mcp_extra_tools_wraps_call_kwargs(monkeypatch: pytest.MonkeyPatch) -> None:
    from jobmate.workflows import registry_builder

    monkeypatch.setenv("JOBMATE_MCP_SERVERS", json.dumps([{"name": "s", "command": "x"}]))
    src = FakeSource([("s", _tool("ping"))], result="pong")
    monkeypatch.setattr(
        mcp_client, "mcp_tool_entries", lambda source=None: mcp_tool_entries(source=src)
    )
    # load_server_configs는 진짜 env를 읽으므로 configs 존재 확인만 통과하면 됨
    tools = registry_builder._mcp_extra_tools()
    assert "mcp_s_ping" in tools
    # ToolExecutor 계약: fn(**kwargs) → MCP call은 args dict로 전달
    assert tools["mcp_s_ping"](a=1, b="x") == "pong"
    assert src.calls == [("s", "ping", {"a": 1, "b": "x"})]


def test_mcp_server_rejects_non_stdio_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    """원격 transport는 인증 없이 기동 불가 — stdio만 허용."""
    import jobmate.mcp.server as srv

    monkeypatch.setenv("JOBMATE_MCP_TRANSPORT", "http")
    with pytest.raises(SystemExit):
        srv.main()
    monkeypatch.setenv("JOBMATE_MCP_TRANSPORT", "sse")
    with pytest.raises(SystemExit):
        srv.main()
