"""SSE /api/v1/stream 테스트 — 스냅샷, 인증, 이벤트 방출."""

import tempfile
import time
from pathlib import Path

from fastapi.testclient import TestClient

from jobmate.api.app import create_app
from jobmate.config import Settings
from jobmate.store.db import Store
from tests.integration.helpers import default_outputs, scripted_llm


def _client():
    db = str(Path(tempfile.mkdtemp()) / "t.db")
    settings = Settings(db_path=db, budget_max_tokens=100_000)
    store = Store(db)
    app = create_app(settings=settings, store=store, llm=scripted_llm(default_outputs()))
    return TestClient(app), store


def _next_event(it, deadline=15.0):
    """다음 SSE 이벤트의 (event, data) 반환. 라인 이터레이터는 1회만 순회 가능."""
    event, data_lines = "", []
    end = time.time() + deadline
    for line in it:
        if time.time() > end:
            return None
        if line.startswith("event: "):
            event = line[7:]
        elif line.startswith("data: "):
            data_lines.append(line[6:])
        elif line == "" and event:
            return event, "\n".join(data_lines)
    return None


def _skip_snapshot(it):
    ev = _next_event(it)
    assert ev is not None and ev[0] == "snapshot"


def test_stream_content_type_and_snapshot():
    client, _ = _client()
    with client.stream("GET", "/api/v1/stream") as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        ev = _next_event(r.iter_lines())
    assert ev is not None and ev[0] == "snapshot"
    assert '"runs"' in ev[1]


def test_stream_emits_run_event():
    client, store = _client()
    with client.stream("GET", "/api/v1/stream") as r:
        it = r.iter_lines()
        _skip_snapshot(it)
        run = store.create_run("dev_review", "0.1.0", {"task": "t"}, trace_id="x")
        ev = None
        for _ in range(20):
            ev = _next_event(it)
            if ev and ev[0] == "run":
                break
            if ev is None:
                break
        assert ev is not None and ev[0] == "run"
        assert run.id in ev[1]


def test_stream_emits_agent_session_event():
    client, store = _client()
    with client.stream("GET", "/api/v1/stream") as r:
        it = r.iter_lines()
        _skip_snapshot(it)
        store.upsert_agent_session("claude", "sess-1", "working", doing="코딩")
        ev = None
        for _ in range(20):
            ev = _next_event(it)
            if ev and ev[0] == "session":
                break
            if ev is None:
                break
        assert ev is not None and ev[0] == "session"
        assert "sess-1" in ev[1]


def test_stream_requires_auth_when_api_key_set(monkeypatch):
    monkeypatch.setenv("JOBMATE_API_KEY", "stream-secret")
    client, _ = _client()
    assert client.get("/api/v1/stream").status_code == 401
    # ?api_key= 쿼리 폴백 — EventSource는 헤더를 설정할 수 없다
    with client.stream("GET", "/api/v1/stream?api_key=stream-secret") as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
    # 잘못된 키는 여전히 401
    assert client.get("/api/v1/stream?api_key=wrong").status_code == 401


def test_query_api_key_rejected_on_other_endpoints(monkeypatch):
    """?api_key= 폴백은 /api/v1/stream에만 허용 — 다른 엔드포인트는 401."""
    monkeypatch.setenv("JOBMATE_API_KEY", "stream-secret")
    client, _ = _client()
    # stream 외 엔드포인트에서 쿼리 키는 인증되지 않아야 한다
    assert client.get("/api/v1/runs?api_key=stream-secret").status_code == 401
    assert client.get("/api/v1/insights?api_key=stream-secret").status_code == 401
    # 헤더 인증은 여전히 정상 동작
    assert client.get("/api/v1/runs", headers={"X-API-Key": "stream-secret"}).status_code == 200
