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
    validate_jobmate_url,
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


def test_validate_url_http_public_rejected_https_ok():
    """H3 회귀 — bearer/API 키가 실리므로 공인 호스트는 https 필수.
    http는 loopback에서만 허용한다."""
    # 공인 호스트 http → 거부 (paperclip + jobmate 양쪽)
    with pytest.raises(PaperclipConfigError):
        validate_paperclip_url("http://8.8.8.8/paperclip")
    with pytest.raises(PaperclipConfigError):
        validate_jobmate_url("http://8.8.8.8")
    with pytest.raises(PaperclipConfigError):
        validate_jobmate_url("http://jobmate.example.com")
    # loopback http → 허용
    assert validate_jobmate_url("http://127.0.0.1:8000").endswith("127.0.0.1:8000")
    assert validate_jobmate_url("http://localhost:8000") is not None
    # https 공인 → 허용 (SSRF 가드 통과 호스트)
    assert validate_paperclip_url("https://8.8.8.8/paperclip") is not None
    assert validate_jobmate_url("https://8.8.8.8") is not None
    # userinfo는 양쪽 모두 거부
    with pytest.raises(PaperclipConfigError):
        validate_jobmate_url("https://user:pass@8.8.8.8")


def test_bridge_rejects_unvalidated_jobmate_url():
    """H3 회귀 — PaperclipBridge 생성 시 jobmate_url도 검증한다."""
    orch = _FakeOrchestrator()
    with pytest.raises(PaperclipConfigError):
        PaperclipBridge(orch, jobmate_url="http://8.8.8.8", client=httpx.Client())


def test_bridge_http_pins_public_host_transport():
    """R2-3 회귀 — jobmate_url이 공인 호스트면 push client가 검증 시 해석한
    IP로 connect를 고정한다(DNS 리바인딩으로 X-API-Key가 새지 않게).
    loopback은 pinning 없이 기본 transport를 쓴다."""
    from jobmate.webutil import _PinnedTransport

    orch = _FakeOrchestrator()
    bridge = PaperclipBridge(orch, jobmate_url="https://8.8.8.8", api_key="k")
    client = bridge._http()  # noqa: SLF001 - transport 검증용
    try:
        assert isinstance(client._transport, _PinnedTransport)  # noqa: SLF001
    finally:
        bridge.close()
    loop = PaperclipBridge(orch, jobmate_url="http://127.0.0.1:8000")
    c2 = loop._http()  # noqa: SLF001
    try:
        assert not isinstance(c2._transport, _PinnedTransport)  # noqa: SLF001
    finally:
        loop.close()


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
    # 커서 값은 ISO-8601 타임스탬프여야 한다 — watermark 비교에 쓰인다(R2-4).
    s.ingest_external_snapshot(**_snap(cursors={"agents": "2026-01-01T00:00:00Z"}))
    s.ingest_external_snapshot(
        **_snap(
            idempotency_key="k2",
            payload_hash="h2",
            cursors={"tasks": "2026-01-02T00:00:00Z"},
        )
    )
    cur = s.get_external_cursor("paperclip", "co1")
    assert cur is not None
    assert cur["cursors"]["agents"] == "2026-01-01T00:00:00Z"
    assert cur["cursors"]["tasks"] == "2026-01-02T00:00:00Z"
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


def test_ingest_same_external_id_two_companies():
    """M5 회귀 — (provider, company_id, external_id) UNIQUE로 회사 간
    external_id 충돌 없이 각자 행을 유지하고 status가 회사별로 집계된다."""
    s = _store()
    s.ingest_external_snapshot(**_snap(company_id="co1", idempotency_key="k1", payload_hash="h1"))
    s.ingest_external_snapshot(**_snap(company_id="co2", idempotency_key="k2", payload_hash="h2"))
    with s.tx() as c:
        task_rows = c.execute(
            "SELECT company_id,title FROM external_tasks WHERE external_id='t1'"
        ).fetchall()
        agent_rows = c.execute(
            "SELECT company_id FROM external_agents WHERE external_id='a1'"
        ).fetchall()
        run_rows = c.execute(
            "SELECT company_id FROM external_runs WHERE external_id='r1'"
        ).fetchall()
    assert len(task_rows) == 2 and len(agent_rows) == 2 and len(run_rows) == 2
    assert {r["company_id"] for r in task_rows} == {"co1", "co2"}
    st = s.external_sync_status()
    by_company = {i["company_id"]: i["counts"] for i in st["items"]}
    assert by_company["co1"] == {"agents": 1, "tasks": 1, "runs": 1}
    assert by_company["co2"] == {"agents": 1, "tasks": 1, "runs": 1}


def test_ingest_stale_snapshot_rows_skipped():
    """M4 회귀 — 저장 커서보다 오래된 item.updated_at 행은 upsert하지 않고,
    커서는 단조 전진만 한다(늦게 도착한 오래된 snapshot이 상태를 되돌리지 않음)."""
    s = _store()
    # 최신 스냅샷: cursor=2026-06-01, task updated_at=2026-06-01
    s.ingest_external_snapshot(
        **_snap(
            idempotency_key="k1",
            payload_hash="h1",
            tasks=[
                {
                    "external_id": "t1",
                    "title": "new",
                    "status": "done",
                    "assignee_external_id": "a1",
                    "updated_at": "2026-06-01T00:00:00Z",
                }
            ],
            cursors={"tasks": "2026-06-01T00:00:00Z"},
        )
    )
    # 오래된 스냅샷 재도착: cursor=2026-01-01(회귀) + updated_at=2026-01-01 행.
    s.ingest_external_snapshot(
        **_snap(
            idempotency_key="k2",
            payload_hash="h2",
            tasks=[
                {
                    "external_id": "t1",
                    "title": "old-overwrite",
                    "status": "open",
                    "assignee_external_id": "a1",
                    "updated_at": "2026-01-01T00:00:00Z",
                }
            ],
            cursors={"tasks": "2026-01-01T00:00:00Z"},
        )
    )
    with s.tx() as c:
        row = c.execute("SELECT title,status FROM external_tasks WHERE external_id='t1'").fetchone()
    # stale 행은 무시 — 최신 상태가 유지된다.
    assert row["title"] == "new" and row["status"] == "done"
    cur = s.get_external_cursor("paperclip", "co1")
    assert cur is not None
    # 커서는 회귀하지 않고 2026-06-01을 유지한다.
    assert cur["cursors"]["tasks"] == "2026-06-01T00:00:00Z"


def test_ingest_malformed_cursor_rejected_and_recovers():
    """R2-4 회귀 — malformed 커서 값은 저장되지 않고, 기존 저장값이
    깨져 있어도 이후 정상 ISO 커서가 이를 복구한다(watermark 정지 방지)."""
    s = _store()
    # 1) malformed 커서("zzz")는 저장되지 않는다.
    s.ingest_external_snapshot(
        **_snap(idempotency_key="k1", payload_hash="h1", cursors={"tasks": "zzz"})
    )
    cur = s.get_external_cursor("paperclip", "co1")
    assert cur is not None
    assert "tasks" not in (cur.get("cursors") or {})
    # 2) 정상 커서 저장 후 malformed 재제출 → 기존 값 유지.
    s.ingest_external_snapshot(
        **_snap(
            idempotency_key="k2",
            payload_hash="h2",
            cursors={"tasks": "2026-06-01T00:00:00Z"},
        )
    )
    s.ingest_external_snapshot(
        **_snap(idempotency_key="k3", payload_hash="h3", cursors={"tasks": "yyy"})
    )
    cur = s.get_external_cursor("paperclip", "co1")
    assert cur["cursors"]["tasks"] == "2026-06-01T00:00:00Z"
    # 3) 손상된 기존 값("zzz")이 DB에 있어도 parseable 새 값이 복구한다 —
    #    _cursor_newer의 parse-aware 우선순위(직접 store 경로 방어).
    with s.tx() as c:
        c.execute(
            "UPDATE external_sync_cursors SET cursor=? WHERE provider='paperclip'",
            (json.dumps({"tasks": "zzz"}),),
        )
    s.ingest_external_snapshot(
        **_snap(
            idempotency_key="k4",
            payload_hash="h4",
            cursors={"tasks": "2026-07-01T00:00:00Z"},
        )
    )
    cur = s.get_external_cursor("paperclip", "co1")
    assert cur["cursors"]["tasks"] == "2026-07-01T00:00:00Z"


def test_ingest_equal_timestamp_rows_accepted():
    """M4 — watermark와 동일 시각의 행은 stale이 아니다(경계: it < wm만 제외)."""
    s = _store()
    s.ingest_external_snapshot(
        **_snap(
            idempotency_key="k1",
            payload_hash="h1",
            tasks=[
                {
                    "external_id": "t1",
                    "title": "t",
                    "status": "open",
                    "assignee_external_id": "a1",
                    "updated_at": "2026-06-01T00:00:00Z",
                }
            ],
            cursors={"tasks": "2026-06-01T00:00:00Z"},
        )
    )
    s.ingest_external_snapshot(
        **_snap(
            idempotency_key="k2",
            payload_hash="h2",
            tasks=[
                {
                    "external_id": "t1",
                    "title": "t2",
                    "status": "done",
                    "assignee_external_id": "a1",
                    "updated_at": "2026-06-01T00:00:00Z",
                }
            ],
        )
    )
    with s.tx() as c:
        row = c.execute("SELECT title,status FROM external_tasks WHERE external_id='t1'").fetchone()
    assert row["title"] == "t2" and row["status"] == "done"


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
    bridge = PaperclipBridge(orch, jobmate_url="http://127.0.0.1:8000", client=http)
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
    bridge = PaperclipBridge(orch, jobmate_url="http://127.0.0.1:8000", client=http)
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


def test_cli_paperclip_bridge_invalid_jobmate_url_clean_exit(monkeypatch):
    """R2-3 회귀 — jobmate_url 검증 실패가 traceback이 아니라 rc=2로 종료."""
    monkeypatch.setenv("PAPERCLIP_URL", "http://localhost:3100")
    monkeypatch.setenv("PAPERCLIP_COMPANY_ID", "co1")
    rc = cli.main(["paperclip-bridge", "--once", "--jobmate-url", "http://8.8.8.8"])
    assert rc == 2
