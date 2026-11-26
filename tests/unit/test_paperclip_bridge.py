"""paperclip-bridge 단위 테스트 — 클라이언트 파싱/페이지네이션/에러,
snapshot ingest 멱등·커서, 브리지 push (spec 3차 E/G)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from jobmate import cli
from jobmate.paperclip_client import (
    PaperclipBridge,
    PaperclipClient,
    PaperclipConfigError,
    PaperclipError,
    PaperclipHTTPError,
    PaperclipTimeoutError,
    collect_snapshot,
    validate_paperclip_url,
)
from jobmate.store.db import Store
from jobmate.store.external_sync import SnapshotConflictError

# ---------------------------------------------------------------------------
# URL 정책
# ---------------------------------------------------------------------------


def test_validate_url_allows_loopback_and_public_https():
    assert validate_paperclip_url("http://localhost:3100").endswith("localhost:3100")
    assert validate_paperclip_url("http://127.0.0.1:8080") is not None
    # 공인 IP 리터럴 https — webutil SSRF 가드 통과
    assert validate_paperclip_url("https://8.8.8.8/paperclip") is not None


def test_validate_url_rejects_unsafe():
    with pytest.raises(PaperclipConfigError):
        validate_paperclip_url("http://169.254.169.254/latest")  # link-local
    with pytest.raises(PaperclipConfigError):
        validate_paperclip_url("ftp://example.com")
    with pytest.raises(PaperclipConfigError):
        validate_paperclip_url("https://user:pass@localhost:3000")  # userinfo 금지
    with pytest.raises(PaperclipConfigError):
        validate_paperclip_url("")
    with pytest.raises(PaperclipConfigError):
        validate_paperclip_url("http://192.168.1.5:3000")  # 사설 비루프백


# ---------------------------------------------------------------------------
# PaperclipClient — MockTransport
# ---------------------------------------------------------------------------


def _client(handler, **kw) -> PaperclipClient:
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport, follow_redirects=False)
    kw.setdefault("sleep", lambda s: None)
    return PaperclipClient("http://localhost:3100", company_id="co1", client=http, **kw)


def test_client_sends_auth_and_paginates():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.headers["authorization"] == "Bearer secret-key"
        cursor = request.url.params.get("cursor")
        if cursor is None:
            return httpx.Response(
                200,
                json={"items": [{"id": "a1", "status": "idle"}], "next_cursor": "p2"},
            )
        return httpx.Response(200, json={"items": [{"id": "a2"}]})

    c = _client(handler, api_key="secret-key")
    items = c.list_agents()
    assert [i["id"] for i in items] == ["a1", "a2"]
    assert len(seen) == 2
    assert seen[1].url.params.get("cursor") == "p2"
    # api_key가 URL/로그에 새지 않는다 — 요청 라인에 키 없음
    assert "secret-key" not in str(seen[0].url)
    c.close()


def test_client_updated_after_and_list_shapes():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("updated_after") == "2026-01-01T00:00:00Z"
        # {tasks: [...]} 형태도 지원
        return httpx.Response(200, json={"tasks": [{"id": "t1", "title": "x"}]})

    c = _client(handler)
    assert c.list_tasks(updated_after="2026-01-01T00:00:00Z") == [{"id": "t1", "title": "x"}]
    c.close()


def test_client_401_no_retry():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, json={"error": "no"})

    c = _client(handler)
    with pytest.raises(PaperclipHTTPError) as ei:
        c.list_agents()
    assert ei.value.status_code == 401
    assert calls["n"] == 1  # 인증 실패는 재시도하지 않는다
    c.close()


def test_client_5xx_retries_then_succeeds():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, json={})
        return httpx.Response(200, json=[{"id": "a1"}])

    c = _client(handler)
    assert c.list_agents() == [{"id": "a1"}]
    assert calls["n"] == 2
    c.close()


def test_client_429_exhausts_retries():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, json={})

    c = _client(handler, max_retries=2)
    with pytest.raises(PaperclipHTTPError) as ei:
        c.list_agents()
    assert ei.value.status_code == 429
    assert calls["n"] == 3  # 최초 + 재시도 2
    c.close()


def test_client_timeout_and_invalid_json():
    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("boom", request=request)

    c = _client(timeout_handler, max_retries=0)
    with pytest.raises(PaperclipTimeoutError):
        c.list_agents()
    c.close()

    def bad_json(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not-json{")

    c2 = _client(bad_json)
    with pytest.raises(PaperclipError, match="invalid_json"):
        c2.list_agents()
    c2.close()


def test_client_rejects_next_url_cursor():
    """next가 절대 URL이면 cursor로 채택하지 않는다(임의 호스트 요청 방지)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": [{"id": "a1"}], "next": "https://evil.example/x"})

    c = _client(handler)
    # next URL 무시 → 1페이지만 조회하고 종료
    assert c.list_agents() == [{"id": "a1"}]
    c.close()


# ---------------------------------------------------------------------------
# collect_snapshot — 정규화 + secret 제거
# ---------------------------------------------------------------------------


class _FakeOrchestrator:
    company_id = "co1"

    def __init__(self, agents=None, tasks=None, runs=None):
        self._a, self._t, self._r = agents or [], tasks or [], runs or []
        self.calls: list[tuple[str, str | None]] = []

    def get_dashboard(self):
        return {}

    def get_cost_summary(self):
        return {}

    def list_agents(self, updated_after=None):
        self.calls.append(("agents", updated_after))
        return self._a

    def list_tasks(self, updated_after=None):
        self.calls.append(("tasks", updated_after))
        return self._t

    def list_runs(self, updated_after=None):
        self.calls.append(("runs", updated_after))
        return self._r


def test_collect_snapshot_maps_and_drops_secrets():
    orch = _FakeOrchestrator(
        agents=[
            {
                "id": "ag1",
                "status": "idle",
                "name": "dev",
                "api_key": "sk-secret",
                "adapter_config": {"env": {"TOKEN": "x"}},
                "transcript": "long text",
                "updated_at": "2026-02-01T00:00:00Z",
            }
        ],
        tasks=[{"id": "tk1", "title": "do it", "assignee_id": "ag1", "status": "open"}],
        runs=[
            {
                "id": "rn1",
                "task_id": "tk1",
                "agent_id": "ag1",
                "status": "done",
                "usage": {"input_tokens": 10, "output_tokens": 5},
                "updated_at": "2026-02-01T01:00:00Z",
            }
        ],
    )
    snap = collect_snapshot(orch)
    assert snap["agents"][0]["external_id"] == "ag1"
    summary = snap["agents"][0]["payload_summary"]
    assert "api_key" not in summary
    assert "adapter_config" not in summary
    assert "transcript" not in summary
    assert summary.get("name") == "dev"
    assert snap["tasks"][0]["assignee_external_id"] == "ag1"
    assert snap["runs"][0]["token_cost_summary"]["input_tokens"] == 10
    assert snap["cursors"] == {"agents": "2026-02-01T00:00:00Z", "runs": "2026-02-01T01:00:00Z"}


def test_collect_snapshot_passes_updated_after():
    orch = _FakeOrchestrator()
    collect_snapshot(orch, updated_after={"agents": "t1", "tasks": None, "runs": "t3"})
    assert ("agents", "t1") in orch.calls
    assert ("tasks", None) in orch.calls
    assert ("runs", "t3") in orch.calls


# ---------------------------------------------------------------------------
# Store ingest — dedupe/멱등/커서
# ---------------------------------------------------------------------------


def _store() -> Store:
    import tempfile
    from pathlib import Path

    return Store(str(Path(tempfile.mkdtemp()) / "t.db"))


def _snap(**kw) -> dict[str, Any]:
    base = {
        "provider": "paperclip",
        "company_id": "co1",
        "agents": [{"external_id": "a1", "status": "idle", "payload_summary": {"name": "x"}}],
        "tasks": [
            {
                "external_id": "t1",
                "title": "task",
                "status": "open",
                "assignee_external_id": "a1",
            }
        ],
        "runs": [{"external_id": "r1", "status": "done", "agent_external_id": "a1"}],
        "cursors": {"agents": "2026-01-01", "tasks": "2026-01-01", "runs": "2026-01-01"},
        "idempotency_key": "k1",
        "payload_hash": "h1",
    }
    base.update(kw)
    return base


def test_ingest_counts_and_dedupe_upsert():
    s = _store()
    r = s.ingest_external_snapshot(**_snap())
    assert r["agents"] == 1 and r["tasks"] == 1 and r["runs"] == 1
    assert r["replayed"] is False
    # 동일 external_id 재-ingest → 행 수 증가 없음 (upsert)
    r2 = s.ingest_external_snapshot(
        **_snap(
            idempotency_key="k2",
            payload_hash="h2",
            agents=[{"external_id": "a1", "status": "running"}],
        )
    )
    assert r2["agents"] == 1
    st = s.external_sync_status()
    item = st["items"][0]
    assert item["counts"] == {"agents": 1, "tasks": 1, "runs": 1}
    assert item["stale"] is False
    assert item["cursors"]["agents"] == "2026-01-01"


def test_ingest_idempotent_replay_and_conflict():
    s = _store()
    s.ingest_external_snapshot(**_snap())
    # 동일 키+동일 해시 → replay
    r = s.ingest_external_snapshot(**_snap())
    assert r["replayed"] is True
    # 동일 키+다른 해시 → conflict
    with pytest.raises(SnapshotConflictError):
        s.ingest_external_snapshot(**_snap(payload_hash="different"))


def test_ingest_cursor_merges_per_entity():
    s = _store()
    s.ingest_external_snapshot(**_snap(cursors={"agents": "ta"}))
    s.ingest_external_snapshot(
        **_snap(idempotency_key="k2", payload_hash="h2", cursors={"tasks": "tt"})
    )
    cur = s.get_external_cursor("paperclip", "co1")
    assert cur is not None
    assert cur["cursors"]["agents"] == "ta"
    assert cur["cursors"]["tasks"] == "tt"
    assert cur["last_synced_at"]


def test_mark_sync_error_preserves_snapshot_and_cursor():
    s = _store()
    s.ingest_external_snapshot(**_snap())
    s.mark_external_sync_error("paperclip", "co1", "http_500")
    cur = s.get_external_cursor("paperclip", "co1")
    assert cur is not None
    assert cur["last_error_code"] == "http_500"
    assert cur["last_synced_at"]  # 마지막 정상 동기화 시각 유지
    # 행도 유지 — stale 표시일 뿐 삭제하지 않는다
    st = s.external_sync_status()
    assert st["items"][0]["counts"]["agents"] == 1


def test_payload_sanitized_on_ingest():
    s = _store()
    s.ingest_external_snapshot(
        **_snap(
            agents=[
                {
                    "external_id": "a9",
                    "payload_summary": {
                        "name": "ok",
                        "api_key": "sekret",
                        "nested": {"password": "pw", "safe": 1},
                        "transcript": "big text",
                    },
                }
            ]
        )
    )
    with s.tx() as c:
        row = c.execute(
            "SELECT payload_summary FROM external_agents WHERE external_id='a9'"
        ).fetchone()
    summary = json.loads(row["payload_summary"])
    assert "api_key" not in summary
    assert "transcript" not in summary
    assert summary["nested"] == {"safe": 1}


def test_run_cost_summary_allowlist():
    s = _store()
    s.ingest_external_snapshot(
        **_snap(
            runs=[
                {
                    "external_id": "r9",
                    "token_cost_summary": {
                        "input_tokens": 10,
                        "cost_usd": 0.01,
                        "evil_blob": "x" * 100,
                        "nested": {"a": 1},
                    },
                }
            ]
        )
    )
    with s.tx() as c:
        row = c.execute(
            "SELECT token_cost_summary FROM external_runs WHERE external_id='r9'"
        ).fetchone()
    summary = json.loads(row["token_cost_summary"])
    assert summary == {"inputtokens": 10, "costusd": 0.01}


# ---------------------------------------------------------------------------
# PaperclipBridge.sync_once
# ---------------------------------------------------------------------------


def test_bridge_sync_once_posts_snapshot():
    orch = _FakeOrchestrator(
        agents=[{"id": "a1", "status": "idle"}],
        tasks=[{"id": "t1", "title": "hi"}],
        runs=[],
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201, json={"replayed": False, "agents": 1})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    bridge = PaperclipBridge(
        orch, jobmate_url="http://127.0.0.1:8000", api_key="jm-key", client=http
    )
    result = bridge.sync_once()
    assert result["agents"] == 1
    assert len(requests) == 1
    req = requests[0]
    assert req.url.path == "/api/v1/management/integrations/paperclip/snapshot"
    assert req.headers["x-api-key"] == "jm-key"
    assert req.headers["idempotency-key"].startswith("pc-")
    body = json.loads(req.content)
    assert body["provider"] == "paperclip"
    assert body["company_id"] == "co1"
    assert body["agents"][0]["external_id"] == "a1"


def test_bridge_idempotency_key_stable_and_incremental():
    orch = _FakeOrchestrator(agents=[{"id": "a1", "status": "idle", "updated_at": "u1"}])
    keys: list[str] = []
    updated_afters: list[Any] = []

    orig_list = orch.list_agents

    def spy(updated_after=None):
        updated_afters.append(updated_after)
        return orig_list(updated_after)

    orch.list_agents = spy  # type: ignore[method-assign]

    def handler(request: httpx.Request) -> httpx.Response:
        keys.append(request.headers["idempotency-key"])
        return httpx.Response(201, json={"replayed": False})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    bridge = PaperclipBridge(orch, jobmate_url="http://x", client=http)
    bridge.sync_once()
    bridge.sync_once()
    # 동일 페이로드 → 동일 멱등키 (서버 replay로 수렴)
    assert keys[0] == keys[1]
    # 두 번째 폴링은 updated_after를 넘긴다(커서 전진)
    assert updated_afters[0] is None
    assert updated_afters[1] == "u1"


def test_bridge_propagates_http_error():
    orch = _FakeOrchestrator()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "forbidden"})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    bridge = PaperclipBridge(orch, jobmate_url="http://x", client=http)
    with pytest.raises(PaperclipHTTPError) as ei:
        bridge.sync_once()
    assert ei.value.status_code == 403


# ---------------------------------------------------------------------------
# CLI --once (모의 클라이언트)
# ---------------------------------------------------------------------------


def test_cli_paperclip_bridge_once(monkeypatch):
    calls = {"sync": 0}

    class _FakeBridge:
        def __init__(self, *a, **kw):
            pass

        def sync_once(self):
            calls["sync"] += 1
            return {"agents": 1, "tasks": 0, "runs": 0, "replayed": False}

        def close(self):
            pass

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        def close(self):
            pass

    import jobmate.paperclip_client as pc

    monkeypatch.setattr(pc, "PaperclipClient", _FakeClient)
    monkeypatch.setattr(pc, "PaperclipBridge", _FakeBridge)
    monkeypatch.setenv("PAPERCLIP_URL", "http://localhost:3100")
    monkeypatch.setenv("PAPERCLIP_COMPANY_ID", "co1")
    rc = cli.main(
        [
            "paperclip-bridge",
            "--once",
            "--jobmate-url",
            "http://127.0.0.1:8000",
        ]
    )
    assert rc == 0
    assert calls["sync"] == 1


def test_cli_paperclip_bridge_requires_config(monkeypatch):
    monkeypatch.delenv("PAPERCLIP_URL", raising=False)
    monkeypatch.delenv("PAPERCLIP_COMPANY_ID", raising=False)
    rc = cli.main(["paperclip-bridge", "--once"])
    assert rc == 2
