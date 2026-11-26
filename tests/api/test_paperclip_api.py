"""paperclip integration API 테스트 — snapshot/status 엔드포인트
(spec 3차 E/F): machine auth, idempotency, 검증."""

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jobmate.api.app import create_app
from jobmate.config import Settings
from jobmate.security import hash_token
from jobmate.store.db import Store
from tests.integration.helpers import default_outputs, scripted_llm

API_KEY = "pc-test-key"
SNAPSHOT_URL = "/api/v1/management/integrations/paperclip/snapshot"
STATUS_URL = "/api/v1/management/integrations/paperclip/status"


def _client(api_key: str | None = None):
    db = str(Path(tempfile.mkdtemp()) / "t.db")
    settings = Settings(db_path=db, budget_max_tokens=100_000)
    store = Store(db)
    llm = scripted_llm(default_outputs())
    app = create_app(settings=settings, store=store, llm=llm, adapters={})
    return TestClient(app), store


def _machine() -> dict[str, str]:
    return {"X-API-Key": API_KEY}


def _session_cookie(client: TestClient, store: Store, email: str = "u@b.com") -> None:
    user = store.create_user(email, password_hash="x" * 32, email_verified=True)
    assert user is not None
    token = f"sess-{email}"
    store.create_session(user["id"], hash_token(token))
    client.cookies.set("jobmate_session", token)


def _body(**kw) -> dict:
    base = {
        "provider": "paperclip",
        "company_id": "co1",
        "agents": [{"external_id": "a1", "status": "idle"}],
        "tasks": [
            {
                "external_id": "t1",
                "title": "hello",
                "status": "open",
                "assignee_external_id": "a1",
            }
        ],
        "runs": [{"external_id": "r1", "status": "done", "agent_external_id": "a1"}],
        "cursors": {"agents": "2026-01-01T00:00:00Z"},
    }
    base.update(kw)
    return base


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.setenv("JOBMATE_API_KEY", API_KEY)
    client, store = _client()
    return client, store


# ---- 인증 ----


def test_snapshot_requires_auth(env):
    client, _ = env
    r = client.post(SNAPSHOT_URL, json=_body(), headers={"Idempotency-Key": "k"})
    assert r.status_code == 401


def test_snapshot_rejects_session_only(env):
    client, store = env
    _session_cookie(client, store)
    r = client.post(SNAPSHOT_URL, json=_body(), headers={"Idempotency-Key": "k"})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "forbidden"


def test_snapshot_accepts_machine_key(env):
    client, _ = env
    r = client.post(SNAPSHOT_URL, json=_body(), headers={**_machine(), "Idempotency-Key": "k1"})
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["agents"] == 1 and data["tasks"] == 1 and data["runs"] == 1
    assert data["replayed"] is False
    assert data["company_id"] == "co1"


# ---- 멱등 ----


def test_snapshot_idempotent_replay(env):
    client, _ = env
    h = {**_machine(), "Idempotency-Key": "same-key"}
    r1 = client.post(SNAPSHOT_URL, json=_body(), headers=h)
    assert r1.status_code == 201
    r2 = client.post(SNAPSHOT_URL, json=_body(), headers=h)
    assert r2.status_code == 200
    assert r2.json()["replayed"] is True


def test_snapshot_idempotency_conflict(env):
    client, _ = env
    h = {**_machine(), "Idempotency-Key": "same-key"}
    client.post(SNAPSHOT_URL, json=_body(), headers=h)
    r = client.post(
        SNAPSHOT_URL,
        json=_body(agents=[{"external_id": "a2", "status": "x"}]),
        headers=h,
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "idempotency_conflict"


def test_snapshot_body_idempotency_key_works(env):
    client, _ = env
    r = client.post(SNAPSHOT_URL, json=_body(idempotency_key="body-key"), headers=_machine())
    assert r.status_code == 201


def test_snapshot_requires_idempotency_key(env):
    client, _ = env
    r = client.post(SNAPSHOT_URL, json=_body(), headers=_machine())
    assert r.status_code == 422


# ---- 검증 ----


def test_snapshot_validation_errors(env):
    client, _ = env
    h = {**_machine(), "Idempotency-Key": "k"}
    # company_id 누락 → 422 (pydantic)
    b = _body()
    del b["company_id"]
    assert client.post(SNAPSHOT_URL, json=b, headers=h).status_code == 422
    # external_id 없는 항목 → 422
    assert (
        client.post(
            SNAPSHOT_URL,
            json=_body(agents=[{"status": "idle"}]),
            headers=h,
        ).status_code
        == 422
    )
    # items 상한 초과 → 422
    big = _body(agents=[{"external_id": f"a{i}"} for i in range(1001)])
    assert client.post(SNAPSHOT_URL, json=big, headers=h).status_code == 422


# ---- status ----


def test_status_machine_and_counts(env):
    client, _ = env
    client.post(SNAPSHOT_URL, json=_body(), headers={**_machine(), "Idempotency-Key": "k"})
    r = client.get(STATUS_URL, headers=_machine())
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["provider"] == "paperclip"
    assert items[0]["company_id"] == "co1"
    assert items[0]["counts"] == {"agents": 1, "tasks": 1, "runs": 1}
    assert items[0]["stale"] is False
    assert items[0]["cursors"]["agents"] == "2026-01-01T00:00:00Z"


def test_status_session_lead_allowed_user_rejected(env):
    client, store = env
    _session_cookie(client, store, email="lead@b.com")
    # 일반 유저 세션 — lead 아님 → 403
    r = client.get(STATUS_URL)
    assert r.status_code == 403
    # 머신 키는 통과
    assert client.get(STATUS_URL, headers=_machine()).status_code == 200


def test_status_empty(env):
    client, _ = env
    r = client.get(STATUS_URL, headers=_machine())
    assert r.status_code == 200
    assert r.json()["items"] == []


def test_status_provider_filter(env):
    client, _ = env
    client.post(SNAPSHOT_URL, json=_body(), headers={**_machine(), "Idempotency-Key": "k"})
    r = client.get(STATUS_URL, params={"provider": "other"}, headers=_machine())
    assert r.status_code == 200
    assert r.json()["items"] == []
    r2 = client.get(STATUS_URL, params={"provider": "paperclip"}, headers=_machine())
    assert len(r2.json()["items"]) == 1
