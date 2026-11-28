"""LLM 사용량 계측 — MeteredLLM 기록 + /usage/llm 집계 API."""

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jobmate.api.app import create_app
from jobmate.config import Settings
from jobmate.llm import MeteredLLM
from jobmate.llm.client import FakeLLM
from jobmate.models.spec import ModelConfig
from jobmate.store import orgctx
from jobmate.store.db import Store


def _store() -> Store:
    return Store(str(Path(tempfile.mkdtemp()) / "t.db"))


def _client(llm=None):
    db = str(Path(tempfile.mkdtemp()) / "t.db")
    settings = Settings(db_path=db, budget_max_tokens=100_000)
    store = Store(db)
    app = create_app(settings=settings, store=store, llm=llm, adapters={})
    return TestClient(app), store


def _headers(key: str = "secret-key") -> dict[str, str]:
    return {"X-API-Key": key}


# ---- MeteredLLM ----


def test_metered_llm_records_usage_on_complete():
    store = _store()
    fake = FakeLLM()
    metered = MeteredLLM(fake, store)
    model = ModelConfig(model="fake-model")
    out, usage = metered.complete(model=model, system="s", user="u")
    assert out == {"result": "ok"}
    assert usage == {"tokens_in": 10, "tokens_out": 10}
    summary = store.llm_usage_summary(days=7)
    m = summary["models"]["fake-model"]
    assert m == {"calls": 1, "tokens_in": 10, "tokens_out": 10}
    assert len(summary["daily"]) == 1
    assert summary["daily"][0]["tokens_in"] == 10
    store.close()


def test_metered_llm_tolerates_missing_store_and_failure():
    """store=None이어도 LLM 경로는 정상 동작해야 한다."""
    fake = FakeLLM()
    metered = MeteredLLM(fake, None)
    out, _ = metered.complete(model=ModelConfig(model="m"), system="s", user="u")
    assert out == {"result": "ok"}

    class BadStore:
        def record_llm_usage(self, *a, **kw):
            raise RuntimeError("db down")

    metered2 = MeteredLLM(FakeLLM(), BadStore())
    out2, _ = metered2.complete(model=ModelConfig(model="m"), system="s", user="u")
    assert out2 == {"result": "ok"}


def test_metered_llm_codex_provider_label():
    store = _store()
    metered = MeteredLLM(FakeLLM(), store)
    model = ModelConfig(model="codex", provider="codex")
    metered.complete(model=model, system="s", user="u")
    summary = store.llm_usage_summary(days=7)
    assert "codex/codex" in summary["models"]
    store.close()


def test_metered_llm_no_record_on_error():
    store = _store()

    class Boom:
        def complete(self, **kw):
            raise RuntimeError("llm down")

    metered = MeteredLLM(Boom(), store)
    with pytest.raises(RuntimeError):
        metered.complete(model=ModelConfig(model="m"), system="s", user="u")
    assert store.llm_usage_summary(days=7)["models"] == {}
    store.close()


# ---- Store 요약 ----


def test_llm_usage_summary_days_filter():
    store = _store()
    store.record_llm_usage("m1", 100, 50)
    store.record_llm_usage("m1", 200, 100)
    store.record_llm_usage("m2", 10, 5)
    s = store.llm_usage_summary(days=7)
    assert s["models"]["m1"] == {"calls": 2, "tokens_in": 300, "tokens_out": 150}
    assert s["models"]["m2"]["calls"] == 1
    total_daily = sum(d["tokens_in"] for d in s["daily"])
    assert total_daily == 310
    store.close()


# ---- API ----


def test_usage_endpoint_aggregates(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("JOBMATE_API_KEY", "secret-key")
    client, store = _client(llm=FakeLLM())
    # spec 003 US3 — API 키(ctx=기본 조직)가 보는 행과 같은 조직에 기록
    _oid = store.list_organizations()[0]["id"]
    with orgctx.org_scope(_oid):
        store.record_llm_usage("gemini-x", 1000, 200)
        store.record_llm_usage("gemini-x", 500, 100)
        store.record_llm_usage("codex/gpt", 50, 25)
    r = client.get("/api/v1/usage/llm?days=7", headers=_headers())
    assert r.status_code == 200
    d = r.json()
    assert d["days"] == 7
    assert d["models"]["gemini-x"] == {"calls": 2, "tokens_in": 1500, "tokens_out": 300}
    assert d["models"]["codex/gpt"]["calls"] == 1
    assert d["daily"] and d["daily"][0]["date"]
    # days 파라미터 클램프
    r2 = client.get("/api/v1/usage/llm?days=9999", headers=_headers())
    assert r2.json()["days"] == 90
    store.close()


def test_usage_endpoint_auth_required(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("JOBMATE_API_KEY", "secret-key")
    client, store = _client()
    assert client.get("/api/v1/usage/llm").status_code == 401
    assert client.get("/api/v1/usage/llm", headers=_headers()).status_code == 200
    store.close()


def test_app_wraps_llm_with_metering(monkeypatch: pytest.MonkeyPatch):
    """create_app이 주입된 llm을 MeteredLLM로 감싸고 complete()가 기록된다."""
    monkeypatch.setenv("JOBMATE_API_KEY", "secret-key")
    client, store = _client(llm=FakeLLM())
    with client:  # lifespan 실행 → app.state.llm 설정 (종료 시 store.close)
        llm = client.app.state.llm
        assert isinstance(llm, MeteredLLM)
        _oid = store.list_organizations()[0]["id"]
        with orgctx.org_scope(_oid):
            llm.complete(model=ModelConfig(model="via-app"), system="s", user="u")
        d = client.get("/api/v1/usage/llm", headers=_headers()).json()
    assert d["models"]["via-app"]["calls"] == 1
    store.close()
