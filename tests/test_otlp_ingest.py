"""spec 008 US1 — OTLP 인제스트 + ingest 토큰 관리 테스트.

- POST /api/v1/otlp/v1/traces — Bearer ingest 토큰 인증, span 파싱/저장,
  민감 속성 제거, 상한(1000 spans/요청), run_id 링크
- GET/POST /api/v1/otlp/tokens, POST /{id}/revoke — lead+, 해시 비노출
- GET /api/v1/observability/external-spans — org 스코프 목록/필터
- GET /api/v1/runs/{id}/timeline — external_spans 확장

app.py 미들웨어는 /api/*에 세션/키 인증을 요구하므로 인제스트 테스트는
X-API-Key(환경 키, 미들웨어 통과용) + Authorization: Bearer <ingest 토큰>
(라우트 자체 인증)을 함께 보낸다. 라우터는 create_app에 아직 등록되지
않았으므로(부모 에이전트가 app.py/db.py를 배선) 여기서 수동 등록한다.
"""

from __future__ import annotations

import hashlib
import json as _json
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jobmate.api import appctx
from jobmate.api.app import create_app
from jobmate.api.routers import otlp as otlp_router
from jobmate.config import Settings
from jobmate.store import orgctx
from jobmate.store.db import Store
from jobmate.store.otlp_ingest import OtlpIngestStoreMixin
from tests.integration.helpers import default_outputs, scripted_llm

_KEY = "secret-key"
_HEADERS = {"X-API-Key": _KEY}


class _TestStore(Store, OtlpIngestStoreMixin):
    """db.py가 OtlpIngestStoreMixin을 Store에 배선하기 전까지 테스트용 서브클래스.
    배선 후에는 Store를 직접 써도 동작한다(메서드 동일)."""


def _env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("JOBMATE_API_KEY", _KEY)
    db = str(Path(tempfile.mkdtemp()) / "t.db")
    settings = Settings(db_path=db, budget_max_tokens=100_000)
    store = _TestStore(db)
    llm = scripted_llm(default_outputs())
    app = create_app(settings=settings, store=store, llm=llm, adapters={})
    # app.py 배선 전 — 라우터를 수동 등록해 엔드포인트를 검증한다.
    otlp_router.register(app, appctx.build_app_context(store, settings, llm, {}))
    return TestClient(app), store


def _oid(store: Store) -> str:
    return str(store.list_organizations()[0]["id"])


def _create_token(client: TestClient, name: str = "ci") -> dict:
    r = client.post("/api/v1/otlp/tokens", headers=_HEADERS, json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()


def _ingest_headers(token: str) -> dict[str, str]:
    # Bearer가 X-API-Key보다 우선하므로 미들웨어 인증은 세션 쿠키가 담당한다
    # (_login으로 client에 세션을 심는다). 여기서는 라우트 자체 인증만 검증.
    return {"Authorization": f"Bearer {token}"}


def _login(
    client: TestClient, store: Store, role: str = "org_admin", email: str = "u@b.com"
) -> dict:
    """세션 쿠키 직접 발급 — 미들웨어 인증 통과용(ingest는 Bearer가 API 키를 덮어씀)."""
    from jobmate.security import SESSION_COOKIE, hash_token

    user = store.get_user_by_email(email) or store.create_user(
        email, password_hash="x" * 32, email_verified=True
    )
    assert user is not None
    orgs = store.list_organizations()
    assert orgs
    store.add_org_member(orgs[0]["id"], user["id"], role)
    token = f"sess-{role}"  # noqa: S105 - 테스트 세션 식별자(비밀 아님)
    store.create_session(user["id"], hash_token(token))
    client.cookies.set(SESSION_COOKIE, token)
    return user


def _span(**kw) -> dict:
    # 최근 타임스탬프 — list_external_spans의 days 윈도우 필터를 통과해야 한다
    now_ns = int(time.time() * 1e9)
    base = {
        "traceId": "trace-aaa",
        "spanId": "span-1",
        "parentSpanId": "span-0",
        "name": "llm.call",
        "kind": 3,
        "startTimeUnixNano": str(now_ns),
        "endTimeUnixNano": str(now_ns + 1_000_000_000),
        "attributes": [
            {"key": "gen_ai.request.model", "value": {"stringValue": "gpt-4o"}},
            {"key": "gen_ai.usage.input_tokens", "value": {"intValue": "120"}},
        ],
        "status": {"code": 1},
    }
    base.update(kw)
    return base


def _otlp(spans: list[dict]) -> dict:
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [{"key": "service.name", "value": {"stringValue": "agent-x"}}]
                },
                "scopeSpans": [{"scope": {"name": "test"}, "spans": spans}],
            }
        ]
    }


def _seed_run(store: Store, oid: str) -> str:
    with orgctx.org_scope(oid):
        run = store.create_run("dev_review", "0.1.0", {"task": "t"}, trace_id="rt")
        return run.id


# ---------------- 인증 ----------------


def test_ingest_no_token_401(monkeypatch: pytest.MonkeyPatch):
    client, _store = _env(monkeypatch)
    # API 키 없이 ingest 토큰만 → 미들웨어 단계에서 401
    r = client.post("/api/v1/otlp/v1/traces", json=_otlp([_span()]))
    assert r.status_code == 401
    # API 키는 통과하지만 Bearer ingest 토큰 없음 → 라우트 401
    r2 = client.post("/api/v1/otlp/v1/traces", headers=_HEADERS, json=_otlp([_span()]))
    assert r2.status_code == 401


def test_ingest_bad_and_revoked_token(monkeypatch: pytest.MonkeyPatch):
    client, store = _env(monkeypatch)
    _login(client, store)
    r = client.post(
        "/api/v1/otlp/v1/traces",
        headers={"X-API-Key": _KEY, "Authorization": "Bearer jmo_bogus"},
        json=_otlp([_span()]),
    )
    assert r.status_code == 401

    tok = _create_token(client)
    client.post(f"/api/v1/otlp/tokens/{tok['id']}/revoke", headers=_HEADERS)
    r2 = client.post(
        "/api/v1/otlp/v1/traces",
        headers=_ingest_headers(tok["token"]),
        json=_otlp([_span()]),
    )
    assert r2.status_code == 401


# ---------------- 파싱/저장 ----------------


def test_ingest_parses_and_stores_spans(monkeypatch: pytest.MonkeyPatch):
    client, store = _env(monkeypatch)
    _login(client, store)
    oid = _oid(store)
    run_id = _seed_run(store, oid)
    tok = _create_token(client)

    spans = [
        _span(
            spanId="s-linked",
            attributes=[
                {"key": "gen_ai.request.model", "value": {"stringValue": "gpt-4o"}},
                {"key": "jobmate.run_id", "value": {"stringValue": run_id}},
                {"key": "llm.cost_usd", "value": {"doubleValue": 0.0123}},
            ],
        ),
        _span(
            traceId="trace-bbb",
            spanId="s-free",
            name="tool.call",
            kind="SPAN_KIND_INTERNAL",
            startTimeUnixNano=str(int(time.time() * 1e9)),
            attributes=[{"key": "tool.name", "value": {"stringValue": "grep"}}],
        ),
        # spanId 없는 스팬 → rejected
        {"traceId": "trace-ccc", "name": "broken"},
    ]
    r = client.post(
        "/api/v1/otlp/v1/traces",
        headers=_ingest_headers(tok["token"]),
        json=_otlp(spans),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {"accepted": 2, "rejected": 1}

    with orgctx.org_scope(oid):
        rows = store.list_external_spans(days=7)
    assert len(rows) == 2
    by_span = {r_["span_id"]: r_ for r_ in rows}

    linked = by_span["s-linked"]
    assert linked["org_id"] == oid
    assert linked["trace_id"] == "trace-aaa"
    assert linked["parent_span_id"] == "span-0"
    assert linked["kind"] == "client"  # kind=3 매핑
    assert linked["run_id"] == run_id
    assert linked["started_at"] and linked["ended_at"]
    assert linked["ended_at"] > linked["started_at"]
    attrs = _json.loads(linked["attributes"])
    assert attrs["gen_ai.request.model"] == "gpt-4o"
    assert attrs["llm.cost_usd"] == pytest.approx(0.0123)

    free = by_span["s-free"]
    assert free["kind"] == "internal"  # 문자열 kind 매핑
    assert free["run_id"] == ""  # jobmate.run_id 없음

    # last_used_at 스로틀 갱신 확인(첫 호출 직후 NULL→값)
    trow = store.find_otlp_ingest_token_by_hash(hashlib.sha256(tok["token"].encode()).hexdigest())
    assert trow is not None and trow.get("last_used_at")


def test_ingest_run_link_only_same_org(monkeypatch: pytest.MonkeyPatch):
    """다른 조직의 run_id는 링크되지 않는다."""
    client, store = _env(monkeypatch)
    _login(client, store)
    oid = _oid(store)
    other = store.create_organization("other", "other-org")
    assert other
    with orgctx.org_scope(other):
        other_run = store.create_run("dev_review", "0.1.0", {"task": "x"}, trace_id="or")
    tok = _create_token(client)
    r = client.post(
        "/api/v1/otlp/v1/traces",
        headers=_ingest_headers(tok["token"]),
        json=_otlp(
            [_span(attributes=[{"key": "jobmate.run_id", "value": {"stringValue": other_run.id}}])]
        ),
    )
    assert r.json()["accepted"] == 1
    with orgctx.org_scope(oid):
        rows = store.list_external_spans(days=7)
    assert rows[0]["run_id"] == ""
    assert rows[0]["org_id"] == oid


def test_ingest_sensitive_attrs_stripped(monkeypatch: pytest.MonkeyPatch):
    client, store = _env(monkeypatch)
    _login(client, store)
    oid = _oid(store)
    tok = _create_token(client)
    r = client.post(
        "/api/v1/otlp/v1/traces",
        headers=_ingest_headers(tok["token"]),
        json=_otlp(
            [
                _span(
                    attributes=[
                        {"key": "http.authorization", "value": {"stringValue": "Bearer x"}},
                        {"key": "access_token", "value": {"stringValue": "sekrit"}},
                        {"key": "db.password", "value": {"stringValue": "pw"}},
                        # camelCase 민감 키도 제거돼야 한다
                        {"key": "accessToken", "value": {"stringValue": "sekrit2"}},
                        {"key": "apiToken", "value": {"stringValue": "sekrit3"}},
                        {"key": "clientSecret", "value": {"stringValue": "sekrit4"}},
                        # 계량 키는 보존돼야 한다
                        {"key": "llm.token_count", "value": {"intValue": "5"}},
                        {"key": "gen_ai.usage.input_tokens", "value": {"intValue": "9"}},
                    ]
                )
            ]
        ),
    )
    assert r.json()["accepted"] == 1
    with orgctx.org_scope(oid):
        rows = store.list_external_spans(days=7)
    attrs = _json.loads(rows[0]["attributes"])
    assert "http.authorization" not in attrs
    assert "access_token" not in attrs
    assert "db.password" not in attrs
    assert "accessToken" not in attrs
    assert "apiToken" not in attrs
    assert "clientSecret" not in attrs
    assert attrs["llm.token_count"] == 5
    assert attrs["gen_ai.usage.input_tokens"] == 9


def test_ingest_attr_count_and_value_caps(monkeypatch: pytest.MonkeyPatch):
    client, store = _env(monkeypatch)
    _login(client, store)
    oid = _oid(store)
    tok = _create_token(client)
    attrs = [{"key": f"k{i}", "value": {"stringValue": "x" * 5000}} for i in range(100)]
    r = client.post(
        "/api/v1/otlp/v1/traces",
        headers=_ingest_headers(tok["token"]),
        json=_otlp([_span(attributes=attrs)]),
    )
    assert r.json()["accepted"] == 1
    with orgctx.org_scope(oid):
        rows = store.list_external_spans(days=7)
    stored = _json.loads(rows[0]["attributes"])
    assert len(stored) == 64  # 속성 수 상한
    assert all(len(v) <= 4096 for v in stored.values())  # 값 길이 상한


def test_ingest_span_count_cap_413(monkeypatch: pytest.MonkeyPatch):
    client, store = _env(monkeypatch)
    _login(client, store)
    tok = _create_token(client)
    spans = [_span(spanId=f"s{i}", traceId=f"t{i}") for i in range(1001)]
    r = client.post(
        "/api/v1/otlp/v1/traces",
        headers=_ingest_headers(tok["token"]),
        json=_otlp(spans),
    )
    assert r.status_code == 413


def test_ingest_invalid_json_400(monkeypatch: pytest.MonkeyPatch):
    client, store = _env(monkeypatch)
    _login(client, store)
    tok = _create_token(client)
    r = client.post(
        "/api/v1/otlp/v1/traces",
        headers=_ingest_headers(tok["token"]),
        content=b"{not json",
    )
    assert r.status_code == 400


# ---------------- 토큰 CRUD ----------------


def test_token_crud_no_hash_leak(monkeypatch: pytest.MonkeyPatch):
    client, store = _env(monkeypatch)
    tok = _create_token(client, "prod-agent")
    assert tok["token"].startswith("jmo_")
    assert tok["name"] == "prod-agent"
    assert tok["id"] and tok["created_at"]

    r = client.get("/api/v1/otlp/tokens", headers=_HEADERS)
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    item = items[0]
    assert item["id"] == tok["id"]
    assert item["prefix"] == tok["id"][:8] or item.get("prefix")
    # 해시/원문 토큰은 어떤 필드에도 없어야 한다
    blob = str(item)
    assert "token_hash" not in blob
    assert tok["token"] not in blob
    assert "last_used_at" in item and "revoked_at" in item

    # lead 게이트 — member 세션 없이 env 키는 admin 취급이라 통과.
    # 비인증은 401.
    assert client.get("/api/v1/otlp/tokens").status_code == 401
    assert client.post("/api/v1/otlp/tokens", json={"name": "x"}).status_code == 401


def test_token_revoke_idempotent_and_404(monkeypatch: pytest.MonkeyPatch):
    client, _store = _env(monkeypatch)
    tok = _create_token(client)
    r = client.post(f"/api/v1/otlp/tokens/{tok['id']}/revoke", headers=_HEADERS)
    assert r.status_code == 200 and r.json()["ok"] is True
    # 재폐기도 성공(멱등)
    r2 = client.post(f"/api/v1/otlp/tokens/{tok['id']}/revoke", headers=_HEADERS)
    assert r2.status_code == 200
    # 없는 id → 404
    r3 = client.post("/api/v1/otlp/tokens/nope/revoke", headers=_HEADERS)
    assert r3.status_code == 404


# ---------------- 조회 / 타임라인 ----------------


def test_external_spans_list_filters(monkeypatch: pytest.MonkeyPatch):
    client, store = _env(monkeypatch)
    _login(client, store)
    oid = _oid(store)
    run_id = _seed_run(store, oid)
    tok = _create_token(client)
    client.post(
        "/api/v1/otlp/v1/traces",
        headers=_ingest_headers(tok["token"]),
        json=_otlp(
            [
                _span(
                    spanId="s1",
                    traceId="trace-x",
                    attributes=[{"key": "jobmate.run_id", "value": {"stringValue": run_id}}],
                ),
                _span(spanId="s2", traceId="trace-y"),
            ]
        ),
    )

    r = client.get("/api/v1/observability/external-spans?days=7", headers=_HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["days"] == 7
    assert len(body["items"]) == 2
    # 최신 started_at 순 — s2가 더 늦은 start? 둘 다 같은 base; spanId로 확인
    ids = {i["span_id"] for i in body["items"]}
    assert ids == {"s1", "s2"}
    item = next(i for i in body["items"] if i["span_id"] == "s1")
    assert item["run_id"] == run_id
    assert item["trace_id"] == "trace-x"
    assert isinstance(item["attributes"], dict)

    # trace_id 필터
    r2 = client.get(
        "/api/v1/observability/external-spans?days=7&trace_id=trace-y", headers=_HEADERS
    )
    assert [i["span_id"] for i in r2.json()["items"]] == ["s2"]
    # run_id 필터
    r3 = client.get(
        f"/api/v1/observability/external-spans?days=7&run_id={run_id}", headers=_HEADERS
    )
    assert [i["span_id"] for i in r3.json()["items"]] == ["s1"]
    # days 클램프 — 0 → 1, 999 → 90
    r4 = client.get("/api/v1/observability/external-spans?days=0", headers=_HEADERS)
    assert r4.json()["days"] == 1
    r5 = client.get("/api/v1/observability/external-spans?days=999", headers=_HEADERS)
    assert r5.json()["days"] == 90


def test_external_spans_lead_gate(monkeypatch: pytest.MonkeyPatch):
    """M2 — external-spans는 observability 형제 엔드포인트와 동일하게 lead+."""
    client, store = _env(monkeypatch)
    _login(client, store, role="member", email="m@b.com")
    r = client.get("/api/v1/observability/external-spans", headers=_HEADERS)
    assert r.status_code == 403
    _login(client, store, role="org_admin", email="a@b.com")
    r2 = client.get("/api/v1/observability/external-spans", headers=_HEADERS)
    assert r2.status_code == 200


def test_timeline_includes_external_spans(monkeypatch: pytest.MonkeyPatch):
    client, store = _env(monkeypatch)
    _login(client, store)
    oid = _oid(store)
    run_id = _seed_run(store, oid)
    tok = _create_token(client)
    client.post(
        "/api/v1/otlp/v1/traces",
        headers=_ingest_headers(tok["token"]),
        json=_otlp(
            [
                _span(
                    spanId="linked",
                    attributes=[{"key": "jobmate.run_id", "value": {"stringValue": run_id}}],
                ),
                _span(spanId="unlinked", traceId="trace-other"),
            ]
        ),
    )
    r = client.get(f"/api/v1/runs/{run_id}/timeline", headers=_HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "external_spans" in body
    ext_ids = {s["span_id"] for s in body["external_spans"]}
    assert ext_ids == {"linked"}  # run_id 링크된 스팬만
    assert body["external_spans"][0]["run_id"] == run_id


def test_external_spans_org_isolation(monkeypatch: pytest.MonkeyPatch):
    """store 레벨 — 다른 org 컨텍스트에서는 스팬이 보이지 않는다."""
    _client, store = _env(monkeypatch)
    oid = _oid(store)
    other = store.create_organization("other2", "other-org-2")
    assert other
    with orgctx.org_scope(oid):
        store.insert_external_spans(
            [
                {
                    "trace_id": "t1",
                    "span_id": "s1",
                    "name": "x",
                    "kind": "internal",
                    "started_at": datetime.now(UTC).isoformat(),
                    "attributes": {"a": 1},
                }
            ]
        )
    with orgctx.org_scope(oid):
        assert len(store.list_external_spans(days=7)) == 1
    with orgctx.org_scope(other):
        assert store.list_external_spans(days=7) == []
        assert store.list_otlp_ingest_tokens() == []
