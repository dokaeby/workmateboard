"""대시보드 확장 기능 단위 테스트 — playbooks/comments/connectors/custom
workflows/triggers/team/audit/export/share/cards.

네트워크는 monkeypatch로 차단하고, run 실행은 scripted_llm으로 결정적으로 만든다.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from jobmate import webutil
from jobmate.api import support as appmod
from jobmate.api.app import create_app
from jobmate.config import Settings
from jobmate.security import decrypt_str, hash_password
from jobmate.store.db import Store
from tests.integration.helpers import default_outputs, scripted_llm


def _env():
    db = str(Path(tempfile.mkdtemp()) / "t.db")
    settings = Settings(db_path=db, budget_max_tokens=100_000)
    store = Store(db)
    llm = scripted_llm(default_outputs())
    app = create_app(settings=settings, store=store, llm=llm, adapters={})
    return TestClient(app), store, settings, llm


# ---------------- playbooks ----------------


def test_playbooks_crud_and_run():
    client, store, _s, _l = _env()
    r = client.post(
        "/api/v1/playbooks",
        json={"name": "weekly review", "workflow": "dev_review", "input": {"task": "t1"}},
    )
    assert r.status_code == 201
    pb = r.json()
    assert pb["name"] == "weekly review" and pb["workflow"] == "dev_review"
    assert pb["input"] == {"task": "t1"}

    items = client.get("/api/v1/playbooks").json()["items"]
    assert len(items) == 1 and items[0]["id"] == pb["id"]

    # unknown workflow → 404
    assert (
        client.post(
            "/api/v1/playbooks", json={"name": "x", "workflow": "nope", "input": {}}
        ).status_code
        == 404
    )

    # run → run 생성 (POST /runs와 동일 경로)
    r = client.post(f"/api/v1/playbooks/{pb['id']}/run")
    assert r.status_code == 202
    body = r.json()
    run = store.get_run(body["run_id"])
    assert run is not None and run.workflow_key == "dev_review"
    assert run.input["task"] == "t1" and run.input["playbook_id"] == pb["id"]

    # delete
    assert client.delete(f"/api/v1/playbooks/{pb['id']}").status_code == 200
    assert client.get("/api/v1/playbooks").json()["items"] == []
    assert client.post(f"/api/v1/playbooks/{pb['id']}/run").status_code == 404
    assert client.delete(f"/api/v1/playbooks/{pb['id']}").status_code == 404


# ---------------- comments ----------------


def test_run_comments():
    client, store, _s, _l = _env()
    run = store.create_run("dev_review", "0.1.0", {"task": "t"}, trace_id="c1")

    r = client.post(f"/api/v1/runs/{run.id}/comments", json={"body": "  hello  "})
    assert r.status_code == 201
    item = r.json()
    assert item["body"] == "hello" and item["run_id"] == run.id

    items = client.get(f"/api/v1/runs/{run.id}/comments").json()["items"]
    assert len(items) == 1 and items[0]["id"] == item["id"]

    # blank / too long → 422
    assert client.post(f"/api/v1/runs/{run.id}/comments", json={"body": "   "}).status_code == 422
    assert (
        client.post(f"/api/v1/runs/{run.id}/comments", json={"body": "x" * 4001}).status_code == 422
    )
    # missing run → 404
    assert client.get("/api/v1/runs/nonexistent/comments").status_code == 404
    assert client.post("/api/v1/runs/nonexistent/comments", json={"body": "x"}).status_code == 404


# ---------------- connectors ----------------


def _public_dns(monkeypatch):
    """생성/-fetch 시점 SSRF 가드의 DNS 해석을 공인 IP로 결정화."""
    import socket

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
    )
    webutil._dns_cache.clear()


def test_connector_secret_encryption(monkeypatch):
    _public_dns(monkeypatch)
    monkeypatch.setenv("JOBMATE_ENCRYPTION_KEY", "unit-test-key-material")
    client, store, _s, _l = _env()
    r = client.post(
        "/api/v1/connectors",
        json={
            "type": "webhook",
            "name": "hook",
            "config": {"url": "https://example.com/x", "token": "super-secret"},
            "enabled": True,
        },
    )
    assert r.status_code == 201
    item = r.json()
    assert item["has_token"] is True and item["enabled"] is True
    assert "config" not in item  # 목록 응답에 config 비노출

    stored = store.get_connector(item["id"])
    assert stored["config"]["token"].startswith("enc:v1:")
    assert stored["config"]["url"] == "https://example.com/x"
    assert decrypt_str(stored["config"]["token"]) == "super-secret"

    # bad type → 422
    assert (
        client.post(
            "/api/v1/connectors", json={"type": "nope", "name": "n", "config": {}}
        ).status_code
        == 422
    )

    # toggle + delete
    t = client.post(f"/api/v1/connectors/{item['id']}/toggle", json={"enabled": False})
    assert t.status_code == 200 and t.json()["enabled"] is False
    assert client.get(f"/api/v1/connectors/{item['id']}/items").status_code == 409
    assert client.delete(f"/api/v1/connectors/{item['id']}").status_code == 200


def test_connector_items_fetch_and_cache(monkeypatch):
    _public_dns(monkeypatch)
    client, store, _s, _l = _env()
    r = client.post(
        "/api/v1/connectors",
        json={
            "type": "jira",
            "name": "jira",
            "config": {
                "url": "https://jira.example.com/rest/api/2/search",
                "json_path": "issues",
                "fields": {
                    "id": "key",
                    "title": "fields.summary",
                    "state": "fields.status.name",
                },
            },
        },
    )
    cid = r.json()["id"]

    calls = []

    def fake_fetch_json(url, **kw):
        calls.append(url)
        return {
            "issues": [
                {
                    "key": "PROJ-1",
                    "fields": {"summary": "Bug", "status": {"name": "Open"}},
                }
            ]
        }

    monkeypatch.setattr(webutil, "fetch_json", fake_fetch_json)
    r1 = client.get(f"/api/v1/connectors/{cid}/items")
    assert r1.status_code == 200
    body1 = r1.json()
    assert body1["cached"] is False and len(body1["items"]) == 1
    assert body1["items"][0]["title"] == "Bug"
    assert store.get_connector(cid)["last_fetch_at"] is not None

    # 60초 캐시 — 두 번째 호출은 fetch 없이 cached=True
    r2 = client.get(f"/api/v1/connectors/{cid}/items")
    assert r2.json()["cached"] is True
    assert len(calls) == 1

    # fetch 실패 → 502 + last_error 기록
    def boom(url, **kw):
        raise webutil.FetchError("http_500")

    appmod._CONNECTOR_CACHE.clear()
    monkeypatch.setattr(webutil, "fetch_json", boom)
    r3 = client.get(f"/api/v1/connectors/{cid}/items")
    assert r3.status_code == 502
    assert "http_500" in store.get_connector(cid)["last_error"]


def test_connector_github_items(monkeypatch):
    client, store, _s, _l = _env()
    r = client.post(
        "/api/v1/connectors",
        json={
            "type": "github",
            "name": "gh",
            "config": {
                "owner": "o",
                "repo": "r",
                "token": "ghp_x",
                # github는 api.github.com 고정 — config.url은 무시되므로
                # 사설 URL을 넣어도 생성/검증에 영향이 없어야 한다
                "url": "http://169.254.169.254/latest/meta-data",
            },
        },
    )
    cid = r.json()["id"]
    seen = {}

    def fake_fetch_json(url, *, headers=None, **kw):
        seen["url"] = url
        seen["auth"] = (headers or {}).get("Authorization")
        return [
            {
                "number": 7,
                "title": "Issue one",
                "html_url": "https://github.com/o/r/issues/7",
                "state": "open",
                "updated_at": "2026-01-01T00:00:00Z",
                "user": {"login": "alice"},
                "pull_request": {"url": "x"},
            }
        ]

    monkeypatch.setattr(webutil, "fetch_json", fake_fetch_json)
    body = client.get(f"/api/v1/connectors/{cid}/items").json()
    assert "api.github.com/repos/o/r/issues" in seen["url"]
    assert seen["auth"] == "Bearer ghp_x"
    assert body["items"][0]["title"] == "Issue one"
    assert body["items"][0]["author"] == "alice"


def test_connector_create_rejects_private_url():
    """생성 시점 SSRF 가드 — 사설/링크로컬/비http URL은 422 url_not_allowed."""
    client, store, _s, _l = _env()
    for bad in (
        "http://169.254.169.254/latest/meta-data",
        "http://127.0.0.1:8080/internal",
        "http://192.168.1.1/",
        "http://10.0.0.5/",
        "http://[::1]/x",
        "http://localhost/x",
        "ftp://example.com/x",
    ):
        r = client.post(
            "/api/v1/connectors",
            json={"type": "json", "name": "c", "config": {"url": bad}},
        )
        assert r.status_code == 422, bad
        assert r.json()["error"]["code"] == "url_not_allowed", bad
    assert client.get("/api/v1/connectors").json()["items"] == []


def test_connector_url_guard_dns_and_fetch_time(monkeypatch):
    """DNS 사설 해석 차단 + 생성 검증 우회 행의 fetch 시점 방어 심층."""
    import socket

    client, store, _s, _l = _env()
    webutil._dns_cache.clear()

    # 사설 IP로 해석되는 호스트 → 생성 422
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", 443))],
    )
    r = client.post(
        "/api/v1/connectors",
        json={"type": "json", "name": "d", "config": {"url": "https://internal.corp.example/x"}},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "url_not_allowed"

    # 공인 IP로 해석되는 호스트는 생성 허용 (fetch_json은 이 테스트에서 미호출)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
    )
    webutil._dns_cache.clear()
    ok = client.post(
        "/api/v1/connectors",
        json={"type": "json", "name": "ok", "config": {"url": "https://ok.corp.example/x"}},
    )
    assert ok.status_code == 201

    # 생성 검증을 우회한 행(DB 직접 삽입)도 fetch 시점에 차단 — fetch_json 미호출
    conn = store.create_connector(
        "json", "direct", {"url": "http://169.254.169.254/latest"}, True, False
    )
    called = []
    monkeypatch.setattr(webutil, "fetch_json", lambda *a, **k: called.append(a) or [])
    r = client.get(f"/api/v1/connectors/{conn['id']}/items")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "url_not_allowed"
    assert called == []
    assert "url_not_allowed" in store.get_connector(conn["id"])["last_error"]


# ---------------- custom workflows ----------------

_VALID_SPEC = {
    "key": "myflow",
    "version": "0.1.0",
    "description": "custom",
    "stages": [{"key": "only", "kind": "function", "function": "noop_fn"}],
    "entry": "only",
}


def test_custom_workflow_lifecycle_and_run_resolution():
    client, store, settings, llm = _env()

    # invalid spec → 422
    resp = client.post("/api/v1/workflows/custom", json={"name": "bad", "spec": {}})
    assert resp.status_code == 422
    # spec.key != name → 422
    bad = dict(_VALID_SPEC, key="other")
    assert (
        client.post("/api/v1/workflows/custom", json={"name": "myflow", "spec": bad}).status_code
        == 422
    )
    # builtin 충돌 → 409
    assert (
        client.post(
            "/api/v1/workflows/custom",
            json={"name": "dev_review", "spec": dict(_VALID_SPEC, key="dev_review")},
        ).status_code
        == 409
    )

    r = client.post("/api/v1/workflows/custom", json={"name": "myflow", "spec": _VALID_SPEC})
    assert r.status_code == 201
    assert r.json()["name"] == "myflow"

    items = client.get("/api/v1/workflows/custom").json()["items"]
    assert [i["name"] for i in items] == ["myflow"]

    # 재시작 시뮬레이션 — 레지스트리 재동기화 후에도 /runs에서 해석됨
    appmod._sync_workflow_registry(store)
    r = client.post("/api/v1/runs", json={"workflow": "myflow", "task": "go"})
    assert r.status_code == 202
    run = store.get_run(r.json()["id"])
    assert run is not None and run.workflow_key == "myflow"
    assert run.workflow_version == "0.1.0"

    # playbook도 커스텀 workflow 사용 가능
    r = client.post(
        "/api/v1/playbooks",
        json={"name": "cf", "workflow": "myflow", "input": {"task": "pb"}},
    )
    assert r.status_code == 201

    # delete → /runs에서 다시 404
    assert client.delete("/api/v1/workflows/custom/myflow").status_code == 200
    assert client.get("/api/v1/workflows/custom").json()["items"] == []
    assert client.post("/api/v1/runs", json={"workflow": "myflow", "task": "go"}).status_code == 404
    assert client.delete("/api/v1/workflows/custom/myflow").status_code == 404


# ---------------- triggers ----------------


def _make_trigger(client, **over):
    body = {
        "match": {"tool": "claude", "state": "decision"},
        "action": {"type": "report", "report_kind": "daily"},
    }
    body.update(over)
    return client.post("/api/v1/triggers", json=body)


def test_trigger_matching_and_dedupe():
    client, store, settings, llm = _env()
    r = _make_trigger(client)
    assert r.status_code == 201
    trig = r.json()
    assert trig["enabled"] is True and trig["match"]["state"] == "decision"

    # validation
    assert (
        client.post(
            "/api/v1/triggers",
            json={"match": {}, "action": {"type": "bogus"}},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/triggers",
            json={"match": {"state": "nope"}, "action": {"type": "report"}},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/triggers",
            json={"match": {}, "action": {"type": "run_workflow", "workflow": "nope"}},
        ).status_code
        == 404
    )

    # 세션 시드 — 첫 관측은 발사하지 않음
    store.upsert_agent_session("claude", "s1", "working", project="proj")
    assert appmod._evaluate_triggers(store, settings, llm, {}) == 0

    # decision으로 전이 → 발사
    store.upsert_agent_session("claude", "s1", "decision", project="proj", ask="approve?")
    assert appmod._evaluate_triggers(store, settings, llm, {}) == 1
    trig_after = store.get_trigger(trig["id"])
    assert trig_after["last_fired_at"] is not None
    assert store.latest_report("daily") is not None

    # 같은 상태 유지 → 재발사 없음
    assert appmod._evaluate_triggers(store, settings, llm, {}) == 0
    # working → decision 재전이 → 새 fp로 재발사 가능
    store.upsert_agent_session("claude", "s1", "working", project="proj")
    assert appmod._evaluate_triggers(store, settings, llm, {}) == 0
    store.upsert_agent_session("claude", "s1", "decision", project="proj")
    assert appmod._evaluate_triggers(store, settings, llm, {}) == 1

    # match 불일치 tool → 발사 없음
    store.upsert_agent_session("codex", "s2", "working")
    appmod._evaluate_triggers(store, settings, llm, {})
    store.upsert_agent_session("codex", "s2", "decision")
    assert appmod._evaluate_triggers(store, settings, llm, {}) == 0

    # toggle off → 발사 없음
    client.post(f"/api/v1/triggers/{trig['id']}/toggle", json={"enabled": False})
    store.upsert_agent_session("claude", "s1", "working")
    appmod._evaluate_triggers(store, settings, llm, {})
    store.upsert_agent_session("claude", "s1", "decision")
    assert appmod._evaluate_triggers(store, settings, llm, {}) == 0

    # delete
    assert client.delete(f"/api/v1/triggers/{trig['id']}").status_code == 200
    assert client.get("/api/v1/triggers").json()["items"] == []


def test_trigger_run_workflow_action_defers():
    client, store, settings, llm = _env()
    r = client.post(
        "/api/v1/triggers",
        json={
            "match": {"tool": "claude", "state": "ended"},
            "action": {"type": "run_workflow", "workflow": "dev_review"},
        },
    )
    assert r.status_code == 201
    trig_id = r.json()["id"]

    store.upsert_agent_session("claude", "s9", "working")
    appmod._evaluate_triggers(store, settings, llm, {})
    store.upsert_agent_session("claude", "s9", "ended", ended_summary="done")
    jobs = []
    assert appmod._evaluate_triggers(store, settings, llm, {}, defer=jobs) == 1
    assert len(jobs) == 1
    run = store.get_run(jobs[0][0])
    assert run.workflow_key == "dev_review" and run.created_by == f"trigger:{trig_id}"
    # 세션 본문(ended_summary)은 run input에 포함되지 않는다
    assert "done" not in json.dumps(run.input)


# ---------------- team ----------------


def test_team_aggregation():
    client, store, _s, _l = _env()
    from jobmate.security import hash_token, new_session_token

    u1 = store.create_user("a@x.com", hash_password("Str0ng#Passw0rd!"), "Alice")
    store.create_user("b@x.com", hash_password("Str0ng#Passw0rd!"), "Bob")
    store.create_run("dev_review", "0.1.0", {"task": "t"}, trace_id="t1", created_by="a@x.com")
    store.create_run("dev_review", "0.1.0", {"task": "t"}, trace_id="t2", created_by="a@x.com")
    store.create_run(
        "dev_review", "0.1.0", {"task": "t"}, trace_id="t3", created_by="system:intake"
    )
    store.create_session(u1["id"], hash_token(new_session_token()), 30)

    items = {i["email"]: i for i in client.get("/api/v1/team").json()["items"]}
    assert items["a@x.com"]["run_count"] == 2
    assert items["a@x.com"]["session_count"] == 1
    assert items["a@x.com"]["name"] == "Alice"
    assert items["b@x.com"]["run_count"] == 0
    assert items["a@x.com"]["last_active_at"]


# ---------------- audit ----------------


def test_audit_admin_only_and_events():
    client, store, _s, _l = _env()
    # 로컬 dev 모드(키/허용목록 없음) → admin 허용
    r = client.get("/api/v1/audit")
    assert r.status_code == 200
    assert "items" in r.json()

    # 비관리자 세션 → 403
    store.create_user("user@x.com", hash_password("Str0ng#Passw0rd!"), "U")
    login = client.post(
        "/api/v1/auth/login", json={"email": "user@x.com", "password": "Str0ng#Passw0rd!"}
    )
    assert login.status_code == 200
    assert client.get("/api/v1/audit").status_code == 403
    client.post("/api/v1/auth/logout")  # 세션 쿠키 제거 → 다시 로컬 admin 컨텍스트

    # 인증 이벤트가 audit에 기록된다
    store.create_user("admin@x.com", hash_password("Str0ng#Passw0rd!"), "A")
    client.post("/api/v1/auth/login", json={"email": "admin@x.com", "password": "wrong"})
    items = client.get("/api/v1/audit").json()["items"]
    actions = {i["action"] for i in items}
    assert "auth_login" in actions and "auth_login_failed" in actions
    assert all("password" not in json.dumps(i["detail"]) for i in items)


# ---------------- export ----------------


def test_export_csv_and_json():
    client, store, _s, _l = _env()
    run = store.create_run(
        "dev_review", "0.1.0", {"task": "exp"}, trace_id="e1", created_by="a@x.com"
    )
    store.record_llm_usage("gpt-x", 10, 20)
    store.save_report("daily", {"runs": {"total": 1}})

    r = client.get("/api/v1/export?kind=runs&format=csv&days=30")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    assert run.id in r.text and "created_by" in r.text

    r = client.get("/api/v1/export?kind=usage&format=json&days=30")
    assert r.status_code == 200
    payload = r.json()
    assert payload["kind"] == "usage" and payload["rows"][0]["model"] == "gpt-x"

    r = client.get("/api/v1/export?kind=reports&format=json&days=30")
    assert r.json()["rows"][0]["kind"] == "daily"

    r = client.get("/api/v1/export?kind=sessions&format=csv&days=30")
    assert r.status_code == 200

    assert client.get("/api/v1/export?kind=bogus").status_code == 422
    assert client.get("/api/v1/export?kind=runs&format=xml").status_code == 422


# ---------------- share ----------------


def test_share_token_lifecycle():
    client, store, _s, _l = _env()
    run = store.create_run("dev_review", "0.1.0", {"task": "share me"}, trace_id="sh1")

    r = client.post("/api/v1/share", json={"kind": "run", "ref_id": run.id})
    assert r.status_code == 201
    body = r.json()
    assert body["url"] == f"/share/{body['token']}" and body["token"]

    # 공개 GET (인증 없이)
    g = client.get(f"/api/v1/share/{body['token']}")
    assert g.status_code == 200
    snap = g.json()
    assert snap["kind"] == "run" and snap["payload"]["id"] == run.id

    # 없는 토큰 → 404
    assert client.get("/api/v1/share/nope").status_code == 404

    # 만료 → 410
    from datetime import UTC, datetime, timedelta

    store.create_share_link(
        "expired-token",
        "run",
        run.id,
        {"id": run.id},
        "test",
        (datetime.now(UTC) - timedelta(days=1)).isoformat(),
    )
    assert client.get("/api/v1/share/expired-token").status_code == 410

    # /share/<token> 리다이렉트
    assert client.get(f"/share/{body['token']}", follow_redirects=False).status_code == 302

    # 없는 run → 404
    assert client.post("/api/v1/share", json={"kind": "run", "ref_id": "nope"}).status_code == 404


# ---------------- cards ----------------


def test_cards_data_and_ssrf(monkeypatch):
    client, store, _s, _l = _env()

    # SSRF 차단 — 생성 단계에서 422
    for bad in (
        "http://127.0.0.1/x",
        "http://localhost:8080/x",
        "http://10.0.0.5/internal",
        "http://192.168.1.1/",
        "ftp://example.com/x",
        "http://[::1]/x",
    ):
        r = client.post("/api/v1/cards", json={"title": "t", "url": bad})
        assert r.status_code == 422, bad

    # DNS 가드는 실제 해석을 요구 — 테스트 도메인은 가짜 공인 IP로 해석시킨다
    import socket

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
    )

    ok = client.post(
        "/api/v1/cards",
        json={"title": "status", "url": "https://status.example.com/api", "json_path": "a.b.c"},
    )
    assert ok.status_code == 201
    cid = ok.json()["id"]

    monkeypatch.setattr(webutil, "fetch_bytes", lambda url, **kw: b'{"a": {"b": {"c": 42}}}')
    r = client.get(f"/api/v1/cards/{cid}/data")
    assert r.status_code == 200 and r.json()["value"] == 42

    # json_path 없으면 전체 JSON
    client.post("/api/v1/cards", json={"title": "whole", "url": "https://w.example.com"})
    cards = client.get("/api/v1/cards").json()["items"]
    whole = [c for c in cards if c["title"] == "whole"][0]
    r = client.get(f"/api/v1/cards/{whole['id']}/data")
    assert r.json()["value"] == {"a": {"b": {"c": 42}}}

    # 비활성 카드 → 409
    client.post(f"/api/v1/cards/{cid}/toggle", json={"enabled": False})
    assert client.get(f"/api/v1/cards/{cid}/data").status_code == 409
    client.post(f"/api/v1/cards/{cid}/toggle", json={"enabled": True})

    # fetch 실패 → 502
    def boom(url, **kw):
        raise webutil.FetchError("http_503")

    monkeypatch.setattr(webutil, "fetch_bytes", boom)
    assert client.get(f"/api/v1/cards/{cid}/data").status_code == 502

    # toggle/delete
    assert client.delete(f"/api/v1/cards/{cid}").status_code == 200
    assert client.get(f"/api/v1/cards/{cid}/data").status_code == 404
