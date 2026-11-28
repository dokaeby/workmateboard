"""spec 006 US6 — /api/v1/me/onboarding 테스트.

체크리스트 항목(account/connector/first_run/notify_channel)이 실제 store
상태를 반영하는지, dismissed/completed_at 저장과 show_wizard 계산을 검증한다.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from jobmate.api.app import create_app
from jobmate.config import Settings
from jobmate.store.db import Store
from tests.integration.helpers import default_outputs, scripted_llm

_PW = "Str0ng#Passw0rd!"


def _env():
    db = str(Path(tempfile.mkdtemp()) / "t.db")
    settings = Settings(db_path=db, budget_max_tokens=100_000)
    store = Store(db)
    llm = scripted_llm(default_outputs())
    app = create_app(settings=settings, store=store, llm=llm, adapters={})
    # me 라우터는 app.py에 아직 include되지 않았을 수 있다 — 없으면 테스트에서
    # 직접 등록한다(동일 store/ctx 헬퍼를 쓰므로 동작은 동일).
    if not any(getattr(r, "path", "") == "/api/v1/me/onboarding" for r in app.routes):
        from jobmate.api.appctx import build_app_context
        from jobmate.api.routers import me as me_router

        me_router.register(
            app,
            build_app_context(store=store, settings=settings, llm=llm, adapters={}),
        )
    return TestClient(app), store


def _signup(client, store, email):
    r = client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": _PW, "agree_terms": True},
    )
    assert r.status_code == 201, r.text
    with store.tx() as c:
        c.execute("UPDATE users SET email_verified=1 WHERE email=?", (email,))


def _login(client, email):
    r = client.post("/api/v1/auth/login", json={"email": email, "password": _PW})
    assert r.status_code == 200, r.text


def _checklist(body):
    return {item["id"]: item for item in body["checklist"]}


def _personal_org_id(store, email):
    uid = store.get_user_by_email(email)["id"]
    orgs = store.list_user_orgs(uid)
    assert orgs, f"{email} has no org"
    return orgs[0]["id"]


def test_onboarding_requires_session():
    client, _store = _env()
    assert client.get("/api/v1/me/onboarding").status_code == 401
    assert client.post("/api/v1/me/onboarding", json={}).status_code == 401


def test_onboarding_initial_state_new_user():
    """신규 가입 사용자 — account는 done, 나머지는 store 상태에 따라 미완료."""
    client, store = _env()
    _signup(client, store, "new@x.com")
    _login(client, "new@x.com")

    r = client.get("/api/v1/me/onboarding")
    assert r.status_code == 200, r.text
    body = r.json()
    items = _checklist(body)
    assert set(items) == {"account", "connector", "first_run", "notify_channel"}
    # 비밀번호 계정 → 연동 완료
    assert items["account"]["done"] is True
    assert items["account"]["detail"] == "password"
    # run/커넥터/알림 채널 없음 → 미완료, 위저드 표시
    assert items["first_run"]["done"] is False
    assert items["connector"]["done"] is False
    assert items["notify_channel"]["done"] is False
    assert body["all_done"] is False
    assert body["show_wizard"] is True
    assert body["dismissed"] is False
    assert body["completed_at"] is None


def test_onboarding_first_run_reflects_store():
    """FR-031/SC-010 — run 생성 후 first_run 항목이 자동 완료된다."""
    client, store = _env()
    _signup(client, store, "runner@x.com")
    _login(client, "runner@x.com")
    email = "runner@x.com"

    assert _checklist(client.get("/api/v1/me/onboarding").json())["first_run"]["done"] is False

    # created_by는 세션 행위자 라벨(email) — 직접 store에 run을 만든다
    store.create_run("dev_review", "0.1.0", {"task": "t"}, trace_id="t1", created_by=email)
    items = _checklist(client.get("/api/v1/me/onboarding").json())
    assert items["first_run"]["done"] is True
    assert items["first_run"]["detail"] == "run_exists"


def test_onboarding_connector_and_notify_channel():
    client, store = _env()
    _signup(client, store, "conn@x.com")
    _login(client, "conn@x.com")

    org_id = _personal_org_id(store, "conn@x.com")
    # 조직에 enabled 커넥터 생성 → connector done
    store.create_connector("github", "gh", {"repo": "x/y"}, org_id=org_id)
    # push 구독 생성 → notify_channel done
    store.save_push_subscription("https://push.example.com/sub1", {"p256dh": "a", "auth": "b"})

    items = _checklist(client.get("/api/v1/me/onboarding").json())
    assert items["connector"]["done"] is True
    assert items["notify_channel"]["done"] is True
    assert "push" in items["notify_channel"]["detail"]


def test_onboarding_dismiss_and_show_wizard():
    client, store = _env()
    _signup(client, store, "d@x.com")
    _login(client, "d@x.com")

    r = client.post("/api/v1/me/onboarding", json={"dismissed": True})
    assert r.status_code == 200, r.text
    assert r.json()["dismissed"] is True
    assert r.json()["show_wizard"] is False

    # 재접속(재조회)해도 dismissed 유지 — 강제 재표시 없음
    body = client.get("/api/v1/me/onboarding").json()
    assert body["dismissed"] is True
    assert body["show_wizard"] is False

    # 다시 열기
    r = client.post("/api/v1/me/onboarding", json={"dismissed": False})
    assert r.json()["show_wizard"] is True

    # 잘못된 타입 → 422
    assert client.post("/api/v1/me/onboarding", json={"dismissed": "yes"}).status_code == 422


def test_onboarding_all_done_sets_completed_at():
    """모든 항목 done이면 GET에서 completed_at이 자동 기록되고
    show_wizard는 false가 된다."""
    client, store = _env()
    _signup(client, store, "all@x.com")
    _login(client, "all@x.com")
    email = "all@x.com"

    org_id = _personal_org_id(store, email)
    store.create_connector("webhook", "wh", {}, org_id=org_id)
    store.save_push_subscription("https://push.example.com/sub2", {"p256dh": "a", "auth": "b"})
    store.create_run("dev_review", "0.1.0", {"task": "t"}, trace_id="t2", created_by=email)

    body = client.get("/api/v1/me/onboarding").json()
    assert body["all_done"] is True
    assert body["show_wizard"] is False
    assert body["completed_at"] is not None

    # 재조회해도 completed_at 유지
    again = client.get("/api/v1/me/onboarding").json()
    assert again["completed_at"] == body["completed_at"]


def test_onboarding_user_isolation():
    client, store = _env()
    _signup(client, store, "u1@x.com")
    _signup(client, store, "u2@x.com")

    _login(client, "u1@x.com")
    client.post("/api/v1/me/onboarding", json={"dismissed": True})
    assert client.get("/api/v1/me/onboarding").json()["dismissed"] is True

    _login(client, "u2@x.com")
    body = client.get("/api/v1/me/onboarding").json()
    assert body["dismissed"] is False
    assert body["show_wizard"] is True
