"""spec 004 US4/US5 — Sentry 연동 + Prometheus 메트릭 (FR-025~FR-033).

검증 범위:
- metrics render: exposition 유효성(TYPE/HELP/라벨 문법), 식별자 비노출,
  route 템플릿 정규화, TTL 캐시, 엔드포인트 인증(API 키/metrics_token).
- sentry: DSN 미설정 no-op, 스크러버(헤더/쿠키/이메일/본문 제거),
  태그 화이트리스트, 샘플 레이트 전달.
"""

import re
import sys
import tempfile
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jobmate.api.app import create_app
from jobmate.config import Settings
from jobmate.services import metrics, sentry
from jobmate.store.db import Store
from tests.integration.helpers import default_outputs, scripted_llm


def _env(monkeypatch):
    monkeypatch.setenv("JOBMATE_API_KEY", "test-api-key")
    for v in (
        "JOBMATE_ALLOWED_EMAILS",
        "VERCEL",
        "JOBMATE_MODE",
        "JOBMATE_ENCRYPTION_KEY",
        "JOBMATE_SENTRY_DSN",
        "JOBMATE_METRICS_TOKEN",
        "JOBMATE_ADMIN_EMAILS",
        "JOBMATE_LEAD_EMAILS",
    ):
        monkeypatch.delenv(v, raising=False)


def _store():
    db = str(Path(tempfile.mkdtemp()) / "t.db")
    return Store(db)


def _client(monkeypatch, store=None, **settings_kw):
    _env(monkeypatch)
    store = store or _store()
    settings = Settings(db_path=str(Path(tempfile.mkdtemp()) / "app.db"), **settings_kw)
    llm = scripted_llm(default_outputs())
    app = create_app(settings=settings, store=store, llm=llm)
    return TestClient(app), store, settings


@pytest.fixture(autouse=True)
def _clean_metrics():
    metrics.reset_cache()
    sentry.reset_for_tests()
    yield
    metrics.reset_cache()
    sentry.reset_for_tests()


# ---------- metrics.render_metrics ----------


def test_render_valid_exposition_and_db_metrics():
    store = _store()
    # run 2건(queued 1 + 완료 처리 1) + llm usage + webhook delivery + inbox
    store.create_run("dev_review", "1", {"x": 1}, "trace-1", created_by="tester@example.com")
    store.record_llm_usage("gemini-2.5-pro", 100, 50)
    store.record_cost_event(
        idempotency_key="cost-evt-1",
        model="gemini-2.5-pro",
        cost_status="estimated",
        cost_microusd=2_500_000,
    )
    store.create_webhook_deliveries(
        [
            {
                "subscription_id": "sub1",
                "event_type": "run.completed",
                "event_id": "evt-1",
                "payload_json": "{}",
            }
        ]
    )
    store.upsert_inbox_item("test", "t", "b", dedupe_key="k1")

    metrics.observe_http_request("GET", "/api/v1/runs/{run_id}", 200, 0.012)
    metrics.observe_http_request("POST", "/api/v1/runs", 500, 0.4)
    out = metrics.render_metrics(store, None)

    # TYPE/HELP 라인 — DB 스냅샷 계열은 비단조라 gauge로 내려가고
    # 이름에서 _total을 뗀다. 진짜 누적 카운터만 _total을 유지한다.
    for name, typ in (
        ("jobmate_http_requests_total", "counter"),
        ("jobmate_http_request_duration_seconds_bucket", "counter"),
        ("jobmate_runs", "gauge"),
        ("jobmate_runs_in_flight", "gauge"),
        ("jobmate_webhook_deliveries", "gauge"),
        ("jobmate_webhook_pending", "gauge"),
        ("jobmate_webhook_dead", "gauge"),
        ("jobmate_llm_tokens", "gauge"),
        ("jobmate_llm_cost_usd", "gauge"),
        ("jobmate_backup_last_success_timestamp", "gauge"),
        ("jobmate_inbox_unread", "gauge"),
    ):
        assert f"# TYPE {name} {typ}" in out, name
        assert f"# HELP {name} " in out, name
    # 구 counter 이름의 비단조 시리즈는 더 이상 노출되지 않는다.
    for stale in (
        "jobmate_runs_total",
        "jobmate_webhook_deliveries_total",
        "jobmate_llm_tokens_total",
        "jobmate_llm_cost_usd_total",
    ):
        assert stale not in out, stale

    # 라벨 값 검증 — 모든 라인 파싱
    for line in out.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        m = re.match(r"^[a-zA-Z_:][a-zA-Z0-9_:]*(\{.*\})?\s+[-0-9.eE+]+$", line)
        assert m, f"bad exposition line: {line!r}"

    assert 'route="/api/v1/runs/{run_id}"' in out
    assert 'status="2xx"' in out and 'status="5xx"' in out
    assert 'workflow="dev_review"' in out and 'status="queued"' in out
    assert 'event="run.completed"' in out and 'status="pending"' in out
    assert 'direction="in",model="gemini-2.5-pro"' in out
    assert 'direction="out",model="gemini-2.5-pro"' in out
    assert re.search(r"^jobmate_runs\{[^}]*status=\"queued\"[^}]*\} 1\.0$", out, re.M)
    assert "jobmate_inbox_unread 1" in out
    # histogram 필수 시리즈
    assert 'le="+Inf"' in out
    assert "jobmate_http_request_duration_seconds_count" in out
    assert "jobmate_http_request_duration_seconds_sum" in out


def test_render_no_identifiers_or_emails_in_output():
    store = _store()
    rid_like = "run_9f8e7d6c5b4a"
    store.create_run("dev_review", "1", {"x": 1}, "trace-x", created_by="user@evil.com")
    # run id 자체가 라벨로 새면 안 된다 — 직접 금지 라벨 주입 시도
    metrics.inc_counter(
        "jobmate_test_bad",
        {
            "run_id": rid_like,
            "email": "user@evil.com",
            "user_id": "u-123",
            "request_id": "req-abc",
            "route": "/api/v1/runs/run_9f8e7d6c5b4a",
            "ok": "fine",
        },
    )
    out = metrics.render_metrics(store, None)
    # 금지 키는 드롭되고 안전한 라벨만 남는다
    assert "run_id=" not in out
    assert "email=" not in out
    assert "user_id=" not in out
    assert "request_id=" not in out
    assert "user@evil.com" not in out
    assert rid_like not in out
    assert 'ok="fine"' in out


def test_route_template_normalization():
    store = _store()

    class _R:
        def __init__(self, path, regex):
            self.path = path
            self.path_regex = regex

    routes = [
        _R("/api/v1/runs/{run_id}", re.compile(r"^/api/v1/runs/[^/]+$")),
        _R("/api/v1/metrics", re.compile(r"^/api/v1/metrics$")),
    ]
    assert metrics.route_template(routes, "/api/v1/runs/abc123") == "/api/v1/runs/{run_id}"
    assert metrics.route_template(routes, "/api/v1/metrics") == "/api/v1/metrics"
    assert metrics.route_template(routes, "/nope/xyz") == "unmatched"
    # uuid 형태 raw 값이 route로 들어와도 _clean_labels가 드롭
    metrics.observe_http_request(
        "GET", "/api/v1/runs/550e8400-e29b-41d4-a716-446655440000", 200, 0.1
    )
    out = metrics.render_metrics(store, None)
    assert "550e8400" not in out


def test_http_observe_status_class_and_histogram():
    metrics.reset_cache()
    metrics.observe_http_request("get", "/api/v1/health", 204, 0.001)
    metrics.observe_http_request("GET", "/api/v1/health", 404, 0.002)
    metrics.observe_http_request("GET", "/api/v1/health", 503, 9.9)
    out = metrics.render_metrics(_store(), None)
    assert 'method="GET",route="/api/v1/health",status="2xx"' in out
    assert 'status="4xx"' in out
    assert 'status="5xx"' in out
    # +Inf 버킷 카운트 = 3
    m = re.search(
        r'jobmate_http_request_duration_seconds_bucket\{le="\+Inf",route="/api/v1/health"\} (\d+)',
        out,
    )
    assert m and int(m.group(1)) == 3


def test_inbox_metric_is_platform_global_across_org_ctx():
    """jobmate_inbox_unread는 org ctx와 무관하게 동일한 전역 값을 낸다.

    store.inbox_unread_count()는 org 필터를 타므로 org_scope 안에서
    렌더하면 조직별 값이 전역 TTL 캐시에 오염될 수 있었다 — 회귀 방지."""
    from jobmate.store import orgctx

    store = _store()
    store.upsert_inbox_item("test", "t", "b", dedupe_key="k-org-global")
    metrics.reset_cache()
    with orgctx.org_scope("org-alpha"):
        out_a = metrics.render_metrics(store, None)
    metrics.reset_cache()
    with orgctx.org_scope("org-beta"):
        out_b = metrics.render_metrics(store, None)
    metrics.reset_cache()
    out_none = metrics.render_metrics(store, None)
    for out in (out_a, out_b, out_none):
        assert "jobmate_inbox_unread 1" in out


def test_db_metrics_ttl_cache():
    store = _store()
    metrics.reset_cache()
    calls = []
    orig = metrics._collect_db_lines

    def spy(s):
        calls.append(1)
        return orig(s)

    metrics._collect_db_lines = spy
    try:
        metrics.render_metrics(store, None)
        metrics.render_metrics(store, None)
        metrics.render_metrics(store, None)
        assert len(calls) == 1  # TTL 내 재쿼리 없음
        # TTL 강제 만료
        metrics._db_cache_at = 0.0
        metrics.render_metrics(store, None)
        assert len(calls) == 2
    finally:
        metrics._collect_db_lines = orig


# ---------- /api/v1/metrics 엔드포인트 인증 ----------


def test_metrics_endpoint_requires_auth(monkeypatch):
    client, _, _ = _client(monkeypatch)
    r = client.get("/api/v1/metrics")
    assert r.status_code == 401


def test_metrics_endpoint_api_key(monkeypatch):
    client, _, _ = _client(monkeypatch)
    r = client.get("/api/v1/metrics", headers={"Authorization": "Bearer test-api-key"})
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]
    assert r.headers["cache-control"] == "no-store"
    assert "# TYPE jobmate_runs_in_flight gauge" in r.text


def test_metrics_endpoint_metrics_token(monkeypatch):
    client, _, _ = _client(monkeypatch, metrics_token="scrape-token-123")  # noqa: S106 - 테스트용 가짜 토큰
    # 토큰 bearer → 200 (API 키 없이)
    r = client.get("/api/v1/metrics", headers={"Authorization": "Bearer scrape-token-123"})
    assert r.status_code == 200
    # 잘못된 토큰 → 401
    r = client.get("/api/v1/metrics", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401
    # metrics_token 미설정 앱에서는 bearer 무시 → 401 (API 키도 없음)
    client2, _, _ = _client(monkeypatch)
    r = client2.get("/api/v1/metrics", headers={"Authorization": "Bearer scrape-token-123"})
    assert r.status_code == 401


def test_metrics_endpoint_counts_http_requests(monkeypatch):
    client, _, _ = _client(monkeypatch)
    client.get("/api/v1/metrics", headers={"Authorization": "Bearer test-api-key"})
    client.get("/api/v1/does-not-exist", headers={"Authorization": "Bearer test-api-key"})
    # 두 번째 스크레이프 — 첫 요청들이 카운터에 반영됐는지
    r = client.get("/api/v1/metrics", headers={"Authorization": "Bearer test-api-key"})
    assert 'route="/api/v1/metrics"' in r.text
    assert 'route="unmatched"' in r.text
    # run_id 같은 원시 id가 라벨로 새지 않는다 — 템플릿만 존재
    assert "run_id=" not in r.text


# ---------- sentry ----------


def test_sentry_no_dsn_is_noop():
    class S:
        sentry_dsn = ""
        sentry_traces_sample_rate = 0.0
        sentry_environment = ""
        mode = "debug"

    assert sentry.configure_sentry(S()) is False
    # 캡처도 조용히 no-op
    sentry.capture_exception(RuntimeError("boom"), tags={"request_id": "r1"})


def test_sentry_missing_package_fails_soft(monkeypatch):
    class S:
        sentry_dsn = "https://abc@example.ingest.sentry.io/1"
        sentry_traces_sample_rate = 0.0
        sentry_environment = ""
        mode = "debug"

    # sentry_sdk 미설치 환경 — import 실패가 warning 후 False
    monkeypatch.setitem(sys.modules, "sentry_sdk", None)
    assert sentry.configure_sentry(S()) is False
    assert sentry.configure_sentry(S()) is False  # 재시도 없이 비활성 유지


def _fake_sentry(monkeypatch):
    """sentry_sdk를 가짜 모듈로 주입 — init 인자와 캡처 이벤트를 기록."""
    captured = []
    init_kwargs = {}
    scopes = []

    mod = types.ModuleType("sentry_sdk")

    class _Scope:
        def __init__(self):
            self.tags = {}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def set_tags(self, t):
            self.tags.update(t)
            scopes.append(dict(self.tags))

    def init(**kwargs):
        init_kwargs.update(kwargs)

    def capture_exception(exc, **kw):
        captured.append(exc)

    mod.init = init
    mod.capture_exception = capture_exception
    mod.push_scope = lambda: _Scope()
    monkeypatch.setitem(sys.modules, "sentry_sdk", mod)
    return mod, captured, init_kwargs, scopes


def test_sentry_configure_and_capture(monkeypatch):
    mod, captured, init_kwargs, scopes = _fake_sentry(monkeypatch)

    class S:
        sentry_dsn = "https://abc@example.ingest.sentry.io/1"
        sentry_traces_sample_rate = 0.25
        sentry_environment = ""
        mode = "release"

    assert sentry.configure_sentry(S()) is True
    assert init_kwargs["send_default_pii"] is False
    assert init_kwargs["traces_sample_rate"] == 0.25
    assert init_kwargs["environment"] == "release"
    assert callable(init_kwargs["before_send"])
    assert callable(init_kwargs["before_send_transaction"])

    exc = RuntimeError("boom")
    sentry.capture_exception(
        exc,
        tags={
            "request_id": "rid-1",
            "path": "/api/v1/x",
            "method": "GET",
            "email": "user@evil.com",  # 화이트리스트 밖 — 드롭돼야 함
            "token": "secret",
        },
    )
    assert captured == [exc]
    # 태그 화이트리스트 — 허용 키만 scope에 설정되고 email/token은 드롭
    assert scopes, "push_scope/set_tags 경로가 호출돼야 한다"
    assert scopes[-1] == {
        "request_id": "rid-1",
        "path": "/api/v1/x",
        "method": "GET",
    }
    assert "email" not in scopes[-1]
    assert "token" not in scopes[-1]


def test_sentry_capture_noop_when_unconfigured(monkeypatch):
    mod, captured, _, _ = _fake_sentry(monkeypatch)
    sentry.capture_exception(RuntimeError("x"))
    assert captured == []


def test_sentry_scrubber_removes_sensitive(monkeypatch):
    _fake_sentry(monkeypatch)

    class S:
        sentry_dsn = "https://abc@example.ingest.sentry.io/1"
        sentry_traces_sample_rate = 0.0
        sentry_environment = "test"
        mode = "debug"

    sentry.configure_sentry(S())
    # 스크러버를 직접 호출해 검증
    from jobmate.services.sentry import _scrub_event

    event = {
        "request": {
            "method": "POST",
            "url": "https://host/api/v1/x?token=abc",
            "headers": {"Authorization": "Bearer sekrit", "Cookie": "s=1"},
            "data": {"password": "p"},
            "cookies": {"session": "x"},
        },
        "user": {"email": "user@evil.com", "ip_address": "1.2.3.4"},
        "tags": {"request_id": "r1", "email": "user@evil.com", "path": "/api/v1/x"},
        "extra": {"api_token": "sekrit", "note": "ok"},
    }
    out = _scrub_event(event, {})
    assert out["request"]["method"] == "POST"
    assert out["request"]["url"] == "https://host/api/v1/x"
    assert "headers" not in out["request"]
    assert "data" not in out["request"]
    assert "cookies" not in out["request"]
    assert "user" not in out
    # extra는 민감 키만 제거되고 일반 키는 유지
    assert out["extra"] == {"note": "ok"}
    assert out["tags"] == {"request_id": "r1", "path": "/api/v1/x"}
    assert "user@evil.com" not in str(out)
    assert "sekrit" not in str(out)
    assert "token=abc" not in str(out)


def test_sentry_env_dsn_flows_through_create_app(monkeypatch):
    """JOBMATE_SENTRY_DSN env → create_app이 configure_sentry로 실제 초기화한다."""
    _, _, init_kwargs, _ = _fake_sentry(monkeypatch)
    _env(monkeypatch)
    monkeypatch.setenv("JOBMATE_SENTRY_DSN", "https://abc@example.ingest.sentry.io/1")
    store = _store()
    settings = Settings(db_path=str(Path(tempfile.mkdtemp()) / "app.db"))
    app = create_app(settings=settings, store=store, llm=scripted_llm(default_outputs()))
    client = TestClient(app)
    assert client.get("/api/v1/health").status_code == 200
    assert init_kwargs.get("dsn") == "https://abc@example.ingest.sentry.io/1"
    assert init_kwargs.get("send_default_pii") is False


def test_sentry_scrubber_redacts_free_text_pii(monkeypatch):
    """예외 메시지·로그엔트리·브레드크럼의 이메일/토큰을 마스킹한다."""
    _fake_sentry(monkeypatch)

    class S:
        sentry_dsn = "https://abc@example.ingest.sentry.io/1"
        sentry_traces_sample_rate = 0.0
        sentry_environment = "test"
        mode = "debug"

    sentry.configure_sentry(S())
    from jobmate.services.sentry import _scrub_event

    event = {
        "exception": {
            "values": [
                {
                    "type": "RuntimeError",
                    "value": "auth failed for admin@corp.example "
                    "Bearer abcdef123456 token=zyx987654321",
                }
            ]
        },
        "message": "send to user@evil.com failed api_key=k3ySecretValue99",
        "logentry": {"message": "retry with password=hunter2hunter2 ok"},
        "breadcrumbs": {
            "values": [
                {
                    "message": "fetch https://x/?token=tok_abcdef123",
                    "data": {"note": "fine", "url": "https://x/?key=abcd1234efgh"},
                }
            ]
        },
    }
    out = _scrub_event(event, {})
    blob = str(out)
    assert "admin@corp.example" not in blob
    assert "user@evil.com" not in blob
    assert "abcdef123456" not in blob
    assert "zyx987654321" not in blob
    assert "k3ySecretValue99" not in blob
    assert "hunter2hunter2" not in blob
    assert "tok_abcdef123" not in blob
    assert "abcd1234efgh" not in blob
    # 정상 텍스트는 보존
    assert "auth failed for" in out["exception"]["values"][0]["value"]
    assert out["breadcrumbs"]["values"][0]["data"]["note"] == "fine"


def test_error_unhandled_webhook_org_scoped_and_path_normalized(monkeypatch):
    """error.unhandled emit — org 없으면 생략, 있으면 해당 org scope + 템플릿 path."""
    from jobmate.services import webhooks as webhooks_mod
    from jobmate.store import orgctx

    calls: list[tuple[str, str, str, dict]] = []

    def spy(store, event_type, event_id, data):
        calls.append((orgctx.current_org_id(), event_type, event_id, data))
        return 0

    monkeypatch.setattr(webhooks_mod, "emit_event", spy)

    _env(monkeypatch)
    store = _store()
    settings = Settings(db_path=str(Path(tempfile.mkdtemp()) / "app.db"))
    app = create_app(settings=settings, store=store, llm=scripted_llm(default_outputs()))

    @app.get("/api/v1/test-boom/{item_id}")
    def _boom(item_id: str):
        raise RuntimeError("kaput")

    @app.get("/boom-plain")
    def _boom_plain():
        raise RuntimeError("kaput")

    client = TestClient(app, raise_server_exceptions=False)
    headers = {"Authorization": "Bearer test-api-key"}

    # /api/ 밖 경로는 org ctx 자체가 없어 request.state.org_id 부재 → emit 생략
    # (ctx='' 팬아웃으로 전 조직 구독에 새는 회귀 방지)
    r = client.get("/boom-plain")
    assert r.status_code == 500
    assert calls == []

    # env 키 인증 → 첫 활성 조직(백필 default 조직) 컨텍스트로 emit
    expected_org = next(
        str(o["id"]) for o in store.list_organizations() if o.get("status") != "deleted"
    )
    r = client.get("/api/v1/test-boom/sekrit-token-123", headers=headers)
    assert r.status_code == 500
    assert len(calls) == 1
    scope_org, event_type, event_id, data = calls[0]
    assert scope_org == expected_org
    assert event_type == "error.unhandled"
    assert data["method"] == "GET"
    assert data["error_type"] == "RuntimeError"
    # 원시 path(토큰 세그먼트)가 아니라 라우트 템플릿만 새야 한다
    assert data["path"] == "/api/v1/test-boom/{item_id}"
    assert "sekrit-token-123" not in event_id
    assert "sekrit-token-123" not in data["path"]
    assert "/api/v1/test-boom/{item_id}" in event_id
    # 서버 생성 suffix로 클라이언트 request_id 조작 dedupe가 무력화된다
    assert len(event_id.rsplit(":", 1)[-1]) == 8
