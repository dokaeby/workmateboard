"""spec 009 — OTLP metrics/logs 인제스트 + 외부 시그널 조회 테스트.

- POST /api/v1/otlp/v1/metrics — gauge/sum/histogram/summary 파싱·저장,
  데이터포인트 5000 상한, 민감 속성 제거
- POST /api/v1/otlp/v1/logs — severity 매핑, body AnyValue→문자열,
  trace_id/span_id 링크, 레코드 5000 상한
- GET /api/v1/observability/external-metrics|external-logs — lead+,
  days/metric_name/trace_id/severity 필터, org 격리

인증은 traces와 동일(Bearer ingest 토큰). /api/v1/otlp/v1/*는 미들웨어
인증 예외이므로 Bearer만으로 충분하다.
"""

from __future__ import annotations

import json as _json
import tempfile
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jobmate.api import appctx
from jobmate.api.app import create_app
from jobmate.api.routers import otlp as otlp_router
from jobmate.config import Settings
from jobmate.store import orgctx
from jobmate.store.db import Store
from tests.integration.helpers import default_outputs, scripted_llm

_KEY = "secret-key"
_HEADERS = {"X-API-Key": _KEY}


def _env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("JOBMATE_API_KEY", _KEY)
    db = str(Path(tempfile.mkdtemp()) / "t.db")
    settings = Settings(db_path=db, budget_max_tokens=100_000)
    store = Store(db)
    llm = scripted_llm(default_outputs())
    app = create_app(settings=settings, store=store, llm=llm, adapters={})
    otlp_router.register(app, appctx.build_app_context(store, settings, llm, {}))
    return TestClient(app), store


def _oid(store: Store) -> str:
    return str(store.list_organizations()[0]["id"])


def _create_token(client: TestClient, name: str = "ci") -> dict:
    r = client.post("/api/v1/otlp/tokens", headers=_HEADERS, json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()


def _ingest_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _login(
    client: TestClient, store: Store, role: str = "org_admin", email: str = "u@b.com"
) -> dict:
    from jobmate.security import SESSION_COOKIE, hash_token

    user = store.get_user_by_email(email) or store.create_user(
        email, password_hash="x" * 32, email_verified=True
    )
    assert user is not None
    orgs = store.list_organizations()
    assert orgs
    store.add_org_member(orgs[0]["id"], user["id"], role)
    token = f"sess-{role}-{email}"  # noqa: S105 - 테스트 세션 식별자(비밀 아님)
    store.create_session(user["id"], hash_token(token))
    client.cookies.set(SESSION_COOKIE, token)
    return user


def _now_ns() -> str:
    return str(int(time.time() * 1e9))


def _metrics_payload() -> dict:
    return {
        "resourceMetrics": [
            {
                "resource": {
                    "attributes": [{"key": "service.name", "value": {"stringValue": "svc"}}]
                },
                "scopeMetrics": [
                    {
                        "scope": {"name": "test"},
                        "metrics": [
                            {
                                "name": "cpu.utilization",
                                "description": "cpu",
                                "unit": "1",
                                "gauge": {
                                    "dataPoints": [
                                        {
                                            "asDouble": 0.42,
                                            "timeUnixNano": _now_ns(),
                                            "attributes": [
                                                {
                                                    "key": "host",
                                                    "value": {"stringValue": "h1"},
                                                }
                                            ],
                                        }
                                    ]
                                },
                            },
                            {
                                "name": "http.requests",
                                "unit": "{request}",
                                "sum": {
                                    "aggregationTemporality": 2,
                                    "isMonotonic": True,
                                    "dataPoints": [
                                        {
                                            "asInt": "120",
                                            "timeUnixNano": _now_ns(),
                                            "startTimeUnixNano": _now_ns(),
                                            "attributes": [
                                                {
                                                    "key": "route",
                                                    "value": {"stringValue": "/api"},
                                                }
                                            ],
                                        }
                                    ],
                                },
                            },
                            {
                                "name": "latency",
                                "unit": "ms",
                                "histogram": {
                                    "aggregationTemporality": 2,
                                    "dataPoints": [
                                        {
                                            "count": "7",
                                            "sum": 123.5,
                                            "bucketCounts": ["1", "2", "4"],
                                            "explicitBounds": [10.0, 100.0],
                                            "timeUnixNano": _now_ns(),
                                        }
                                    ],
                                },
                            },
                            {
                                "name": "req.size.summary",
                                "summary": {
                                    "dataPoints": [
                                        {
                                            "count": "3",
                                            "sum": 30.0,
                                            "timeUnixNano": _now_ns(),
                                        }
                                    ]
                                },
                            },
                        ],
                    }
                ],
            }
        ]
    }


def _logs_payload() -> dict:
    return {
        "resourceLogs": [
            {
                "resource": {
                    "attributes": [{"key": "service.name", "value": {"stringValue": "svc"}}]
                },
                "scopeLogs": [
                    {
                        "scope": {"name": "test"},
                        "logRecords": [
                            {
                                "timeUnixNano": _now_ns(),
                                "severityNumber": 17,
                                "body": {"stringValue": "db connection failed"},
                                "attributes": [
                                    {"key": "db.host", "value": {"stringValue": "pg1"}},
                                    {
                                        "key": "password",
                                        "value": {"stringValue": "sekrit"},
                                    },
                                ],
                                "traceId": "4bf92f3577b34da6a3ce929d0e0e4736",
                                "spanId": "00f067aa0ba902b7",
                            },
                            {
                                "timeUnixNano": _now_ns(),
                                "severityNumber": 9,
                                "severityText": "info",
                                "body": {"stringValue": "startup ok"},
                                "traceId": "other-trace",
                            },
                            {
                                "timeUnixNano": _now_ns(),
                                # 비표준 severityText — severityNumber가 표준 버킷을 결정
                                "severityNumber": 13,
                                "severityText": "warning",
                                "body": {"stringValue": "disk almost full"},
                                "traceId": "warn-trace",
                            },
                        ],
                    }
                ],
            }
        ]
    }


# ---------------- metrics 인제스트 ----------------


def test_metrics_ingest_bad_token_401(monkeypatch: pytest.MonkeyPatch):
    client, _store = _env(monkeypatch)
    r = client.post("/api/v1/otlp/v1/metrics", json=_metrics_payload())
    assert r.status_code == 401
    r2 = client.post(
        "/api/v1/otlp/v1/metrics",
        headers={"Authorization": "Bearer jmo_bogus"},
        json=_metrics_payload(),
    )
    assert r2.status_code == 401
    # 폐기 토큰도 401
    tok = _create_token(client)
    client.post(f"/api/v1/otlp/tokens/{tok['id']}/revoke", headers=_HEADERS)
    r3 = client.post(
        "/api/v1/otlp/v1/metrics",
        headers=_ingest_headers(tok["token"]),
        json=_metrics_payload(),
    )
    assert r3.status_code == 401


def test_metrics_ingest_parses_and_stores(monkeypatch: pytest.MonkeyPatch):
    client, store = _env(monkeypatch)
    oid = _oid(store)
    tok = _create_token(client)
    r = client.post(
        "/api/v1/otlp/v1/metrics",
        headers=_ingest_headers(tok["token"]),
        json=_metrics_payload(),
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"accepted": 4, "rejected": 0}

    with orgctx.org_scope(oid):
        rows = store.list_external_metrics(days=7)
    assert len(rows) == 4
    by_name = {}
    for row in rows:
        by_name.setdefault(row["metric_name"], []).append(row)

    gauge = by_name["cpu.utilization"][0]
    assert gauge["metric_type"] == "gauge"
    assert gauge["value"] == pytest.approx(0.42)
    assert gauge["unit"] == "1"
    assert gauge["org_id"] == oid
    gattrs = _json.loads(gauge["attributes"])
    assert gattrs["host"] == "h1"
    assert gauge["timestamp"]  # ISO 변환됨

    total = by_name["http.requests"][0]
    assert total["metric_type"] == "sum"
    assert total["value"] == pytest.approx(120.0)
    tattrs = _json.loads(total["attributes"])
    assert tattrs["isMonotonic"] is True
    assert tattrs["aggregationTemporality"] == 2
    assert tattrs["route"] == "/api"

    hist = by_name["latency"][0]
    assert hist["metric_type"] == "histogram"
    assert hist["value"] == 7  # count
    hattrs = _json.loads(hist["attributes"])
    assert hattrs["sum"] == pytest.approx(123.5)
    assert hattrs["bucket_count"] == 3
    assert hattrs["temporality"] == 2

    summary = by_name["req.size.summary"][0]
    assert summary["metric_type"] == "summary"
    assert summary["value"] == 3


def test_metrics_ingest_caps_and_invalid(monkeypatch: pytest.MonkeyPatch):
    client, _store = _env(monkeypatch)
    tok = _create_token(client)
    # 5001 데이터포인트 → 413
    dps = [{"asDouble": 1.0, "timeUnixNano": _now_ns()} for _ in range(5001)]
    payload = {
        "resourceMetrics": [
            {"scopeMetrics": [{"metrics": [{"name": "m", "gauge": {"dataPoints": dps}}]}]}
        ]
    }
    r = client.post(
        "/api/v1/otlp/v1/metrics",
        headers=_ingest_headers(tok["token"]),
        json=payload,
    )
    assert r.status_code == 413
    # 잘못된 JSON → 400
    r2 = client.post(
        "/api/v1/otlp/v1/metrics",
        headers=_ingest_headers(tok["token"]),
        content=b"{bad",
    )
    assert r2.status_code == 400


def test_metrics_ingest_nonfinite_values_rejected(monkeypatch: pytest.MonkeyPatch):
    """NaN/Infinity 데이터포인트는 거부 — strict JSON 응답 오염 방지."""
    client, store = _env(monkeypatch)
    _login(client, store)
    tok = _create_token(client)
    payload = {
        "resourceMetrics": [
            {
                "scopeMetrics": [
                    {
                        "metrics": [
                            {
                                "name": "nan.gauge",
                                "gauge": {
                                    "dataPoints": [
                                        {"asDouble": "NaN", "timeUnixNano": _now_ns()},
                                        {"asDouble": "Infinity", "timeUnixNano": _now_ns()},
                                        {"asDouble": 1.5, "timeUnixNano": _now_ns()},
                                    ]
                                },
                            },
                            {
                                "name": "nan.histogram",
                                "histogram": {
                                    "dataPoints": [
                                        {
                                            "count": "2",
                                            "sum": "NaN",
                                            "timeUnixNano": _now_ns(),
                                        }
                                    ]
                                },
                            },
                        ]
                    }
                ]
            }
        ]
    }
    r = client.post(
        "/api/v1/otlp/v1/metrics",
        headers=_ingest_headers(tok["token"]),
        content=_json.dumps(payload).encode(),
    )
    assert r.status_code == 200, r.text
    # gauge 2개 비유한 → rejected, 유효 1개 + histogram 1개 → accepted
    assert r.json() == {"accepted": 2, "rejected": 2}

    # 조회 응답이 strict JSON이어야 함 — NaN 리터럴이 섞이면 파싱 실패
    r2 = client.get("/api/v1/observability/external-metrics?days=7", headers=_HEADERS)
    assert r2.status_code == 200
    assert "NaN" not in r2.text and "Infinity" not in r2.text
    items = r2.json()["items"]
    hist = next(i for i in items if i["metric_name"] == "nan.histogram")
    assert hist["attributes"]["sum"] is None


# ---------------- logs 인제스트 ----------------


def test_logs_ingest_bad_token_401(monkeypatch: pytest.MonkeyPatch):
    client, _store = _env(monkeypatch)
    r = client.post("/api/v1/otlp/v1/logs", json=_logs_payload())
    assert r.status_code == 401
    r2 = client.post(
        "/api/v1/otlp/v1/logs",
        headers={"Authorization": "Bearer jmo_bogus"},
        json=_logs_payload(),
    )
    assert r2.status_code == 401


def test_logs_ingest_parses_and_stores(monkeypatch: pytest.MonkeyPatch):
    client, store = _env(monkeypatch)
    oid = _oid(store)
    tok = _create_token(client)
    r = client.post(
        "/api/v1/otlp/v1/logs",
        headers=_ingest_headers(tok["token"]),
        json=_logs_payload(),
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"accepted": 3, "rejected": 0}

    with orgctx.org_scope(oid):
        rows = store.list_external_logs(days=7)
    assert len(rows) == 3
    by_trace = {row["trace_id"]: row for row in rows}

    err_log = by_trace["4bf92f3577b34da6a3ce929d0e0e4736"]
    assert err_log["span_id"] == "00f067aa0ba902b7"
    # severityText 없음 → severityNumber 17 → ERROR 폴백
    assert err_log["severity"] == "ERROR"
    assert err_log["severity_number"] == 17
    assert err_log["body"] == "db connection failed"
    assert err_log["org_id"] == oid
    lattrs = _json.loads(err_log["attributes"])
    assert lattrs["db.host"] == "pg1"
    assert "password" not in lattrs  # 민감 키 제거

    info_log = by_trace["other-trace"]
    # severityText "info" → 대문자 정규화
    assert info_log["severity"] == "INFO"
    assert info_log["body"] == "startup ok"

    warn_log = by_trace["warn-trace"]
    # 비표준 severityText("warning")는 severityNumber 13 → 표준 WARN으로 정규화
    assert warn_log["severity"] == "WARN"
    assert warn_log["severity_number"] == 13


def test_logs_ingest_record_cap_413(monkeypatch: pytest.MonkeyPatch):
    client, _store = _env(monkeypatch)
    tok = _create_token(client)
    recs = [{"body": {"stringValue": "x"}, "timeUnixNano": _now_ns()} for _ in range(5001)]
    payload = {"resourceLogs": [{"scopeLogs": [{"logRecords": recs}]}]}
    r = client.post(
        "/api/v1/otlp/v1/logs",
        headers=_ingest_headers(tok["token"]),
        json=payload,
    )
    assert r.status_code == 413


# ---------------- 조회 엔드포인트 ----------------


def _seed_signals(client: TestClient, store: Store) -> None:
    tok = _create_token(client)
    client.post(
        "/api/v1/otlp/v1/metrics",
        headers=_ingest_headers(tok["token"]),
        json=_metrics_payload(),
    )
    client.post(
        "/api/v1/otlp/v1/logs",
        headers=_ingest_headers(tok["token"]),
        json=_logs_payload(),
    )


def test_external_metrics_query(monkeypatch: pytest.MonkeyPatch):
    client, store = _env(monkeypatch)
    _login(client, store)
    _seed_signals(client, store)

    r = client.get("/api/v1/observability/external-metrics?days=7", headers=_HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["days"] == 7
    assert len(body["items"]) == 4
    names = {i["metric_name"] for i in body["items"]}
    assert names == {"cpu.utilization", "http.requests", "latency", "req.size.summary"}
    item = next(i for i in body["items"] if i["metric_name"] == "cpu.utilization")
    assert set(item) >= {
        "id",
        "metric_name",
        "metric_type",
        "value",
        "unit",
        "attributes",
        "timestamp",
    }
    assert isinstance(item["attributes"], dict)

    # metric_name 필터
    r2 = client.get(
        "/api/v1/observability/external-metrics?days=7&metric_name=http.requests",
        headers=_HEADERS,
    )
    assert [i["metric_name"] for i in r2.json()["items"]] == ["http.requests"]
    # days 클램프
    r3 = client.get("/api/v1/observability/external-metrics?days=0", headers=_HEADERS)
    assert r3.json()["days"] == 1
    r4 = client.get("/api/v1/observability/external-metrics?days=999", headers=_HEADERS)
    assert r4.json()["days"] == 90


def test_external_logs_query_filters(monkeypatch: pytest.MonkeyPatch):
    client, store = _env(monkeypatch)
    _login(client, store)
    _seed_signals(client, store)

    r = client.get("/api/v1/observability/external-logs?days=7", headers=_HEADERS)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert len(items) == 3
    assert all(
        set(i)
        >= {
            "id",
            "trace_id",
            "span_id",
            "severity",
            "severity_number",
            "body",
            "attributes",
            "timestamp",
        }
        for i in items
    )

    # trace_id 필터
    r2 = client.get(
        "/api/v1/observability/external-logs?days=7&trace_id=other-trace",
        headers=_HEADERS,
    )
    assert [i["body"] for i in r2.json()["items"]] == ["startup ok"]
    # severity 필터(대소문자 무관) — 비표준 severityText도 표준 버킷으로 매칭
    r3 = client.get("/api/v1/observability/external-logs?days=7&severity=error", headers=_HEADERS)
    assert [i["severity"] for i in r3.json()["items"]] == ["ERROR"]
    r3b = client.get("/api/v1/observability/external-logs?days=7&severity=warn", headers=_HEADERS)
    assert [i["severity"] for i in r3b.json()["items"]] == ["WARN"]
    warn_item = r3b.json()["items"][0]
    assert warn_item["severity_number"] == 13
    assert warn_item["severity_text"] == "warning"
    # body 4KB 응답 상한 — 짧은 body는 그대로
    assert all(len(i["body"]) <= 4096 for i in items)


def test_external_signals_lead_gate(monkeypatch: pytest.MonkeyPatch):
    client, store = _env(monkeypatch)
    _login(client, store, role="member", email="m@b.com")
    assert client.get("/api/v1/observability/external-metrics", headers=_HEADERS).status_code == 403
    assert client.get("/api/v1/observability/external-logs", headers=_HEADERS).status_code == 403
    _login(client, store, role="org_admin", email="a@b.com")
    assert client.get("/api/v1/observability/external-metrics", headers=_HEADERS).status_code == 200
    assert client.get("/api/v1/observability/external-logs", headers=_HEADERS).status_code == 200


def test_external_signals_org_isolation(monkeypatch: pytest.MonkeyPatch):
    """store 레벨 — 다른 org 컨텍스트에서는 metrics/logs가 보이지 않는다."""
    _client, store = _env(monkeypatch)
    oid = _oid(store)
    other = store.create_organization("other-sig", "other-sig-org")
    assert other
    with orgctx.org_scope(oid):
        store.insert_external_metrics(
            [
                {
                    "metric_name": "m",
                    "metric_type": "gauge",
                    "value": 1.0,
                    "timestamp": "2025-01-01T00:00:00+00:00",
                }
            ]
        )
        store.insert_external_logs(
            [
                {
                    "trace_id": "t1",
                    "body": "b",
                    "severity": "INFO",
                    "timestamp": "2025-01-01T00:00:00+00:00",
                }
            ]
        )
    # timestamp가 과거라 days 윈도우 밖 — org 격리 확인을 위해 최근 ts로 재삽입
    from datetime import UTC, datetime

    recent = datetime.now(UTC).isoformat()
    with orgctx.org_scope(oid):
        store.insert_external_metrics(
            [{"metric_name": "m2", "metric_type": "gauge", "value": 2.0, "timestamp": recent}]
        )
        store.insert_external_logs(
            [{"trace_id": "t2", "body": "b2", "severity": "INFO", "timestamp": recent}]
        )
        assert len(store.list_external_metrics(days=7)) == 1
        assert len(store.list_external_logs(days=7)) == 1
    with orgctx.org_scope(other):
        assert store.list_external_metrics(days=7) == []
        assert store.list_external_logs(days=7) == []
