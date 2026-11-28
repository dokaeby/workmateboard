"""spec 006 US5 — /api/v1/me/dashboard + /api/v1/me/views 테스트.

세션 사용자별 대시보드 카드 설정과 저장 뷰 CRUD/적용, 사용자 격리,
전역 prefs.dashboard_widgets 읽기 호환 폴백을 검증한다.
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
    if not any(getattr(r, "path", "") == "/api/v1/me/dashboard" for r in app.routes):
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


def _user_id(store, email):
    return store.get_user_by_email(email)["id"]


# ---------------- dashboard prefs ----------------


def test_dashboard_requires_session():
    client, _store = _env()
    # 로컬 오픈 모드(키 없음)라도 세션이 없으면 /me는 401
    assert client.get("/api/v1/me/dashboard").status_code == 401
    assert client.get("/api/v1/me/views").status_code == 401
    assert client.get("/api/v1/me/onboarding").status_code == 401


def test_dashboard_patch_get_roundtrip():
    client, store = _env()
    _signup(client, store, "a@x.com")
    _login(client, "a@x.com")

    r = client.patch(
        "/api/v1/me/dashboard",
        json={
            "inbox": {"order": 0, "visible": True},
            "usage": {"order": 1, "visible": False},
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cards"]["inbox"] == {"order": 0, "visible": True}
    assert body["cards"]["usage"] == {"order": 1, "visible": False}
    assert body["active_view_id"] is None

    # 재조회 시 유지 (FR-023 즉시 반영)
    got = client.get("/api/v1/me/dashboard").json()
    assert got["cards"]["inbox"]["order"] == 0
    assert got["cards"]["usage"]["visible"] is False

    # 부분 병합 — usage만 다시 바꾸면 inbox는 유지
    r = client.patch("/api/v1/me/dashboard", json={"usage": {"visible": True}})
    assert r.status_code == 200
    cards = r.json()["cards"]
    assert cards["inbox"]["order"] == 0
    assert cards["usage"] == {"order": 1, "visible": True}


def test_dashboard_unknown_card_422():
    client, store = _env()
    _signup(client, store, "a@x.com")
    _login(client, "a@x.com")
    r = client.patch("/api/v1/me/dashboard", json={"bogus_card": {"order": 0}})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "unknown_card"


def test_dashboard_bad_entry_422():
    client, store = _env()
    _signup(client, store, "a@x.com")
    _login(client, "a@x.com")
    assert (
        client.patch("/api/v1/me/dashboard", json={"inbox": {"order": "first"}}).status_code == 422
    )
    assert (
        client.patch("/api/v1/me/dashboard", json={"inbox": {"visible": "yes"}}).status_code == 422
    )


def test_dashboard_global_widgets_fallback():
    """per-user 행이 없으면 전역 prefs.dashboard_widgets를 읽기 호환 폴백."""
    client, store = _env()
    store.set_state(
        "prefs",
        {"dashboard_widgets": [{"id": "inbox", "visible": True, "order": 2}]},
    )
    _signup(client, store, "a@x.com")
    _login(client, "a@x.com")

    got = client.get("/api/v1/me/dashboard").json()
    assert got["cards"]["inbox"] == {"order": 2, "visible": True}
    assert got["active_view_id"] is None

    # per-user 저장 후에는 per-user가 우선한다 — PATCH 시점에 폴백 global
    # 카드(inbox)가 병합 기준으로 쓰이고, 이후 GET은 per-user 행만 반환한다.
    client.patch("/api/v1/me/dashboard", json={"usage": {"order": 0}})
    got = client.get("/api/v1/me/dashboard").json()
    assert got["cards"]["usage"]["order"] == 0
    assert got["cards"]["inbox"] == {"order": 2, "visible": True}


def test_dashboard_user_isolation():
    client, store = _env()
    _signup(client, store, "a@x.com")
    _signup(client, store, "b@x.com")

    _login(client, "a@x.com")
    client.patch(
        "/api/v1/me/dashboard",
        json={"inbox": {"order": 5, "visible": False}},
    )
    assert client.get("/api/v1/me/dashboard").json()["cards"]["inbox"]["order"] == 5

    _login(client, "b@x.com")
    got = client.get("/api/v1/me/dashboard").json()
    assert got["cards"] == {}  # b는 빈 설정 — a의 설정이 새지 않는다

    # b가 저장해도 a는 영향 없음
    client.patch("/api/v1/me/dashboard", json={"reports": {"order": 0}})
    _login(client, "a@x.com")
    cards = client.get("/api/v1/me/dashboard").json()["cards"]
    assert cards["inbox"]["order"] == 5
    assert "reports" not in cards


# ---------------- views ----------------


def test_views_crud_roundtrip():
    client, store = _env()
    _signup(client, store, "a@x.com")
    _login(client, "a@x.com")

    r = client.post(
        "/api/v1/me/views",
        json={
            "name": "exec",
            "layout": {"inbox": {"order": 0, "visible": True}},
        },
    )
    assert r.status_code == 201, r.text
    view = r.json()["view"]
    assert view["name"] == "exec"
    assert view["is_default"] is False
    assert view["layout"]["inbox"] == {"order": 0, "visible": True}

    items = client.get("/api/v1/me/views").json()["items"]
    assert [v["id"] for v in items] == [view["id"]]

    # PATCH rename + layout
    r = client.patch(
        f"/api/v1/me/views/{view['id']}",
        json={"name": "exec-v2", "layout": {"usage": {"visible": False}}},
    )
    assert r.status_code == 200, r.text
    assert r.json()["view"]["name"] == "exec-v2"
    assert r.json()["view"]["layout"] == {"usage": {"order": None, "visible": False}}

    # DELETE
    assert client.delete(f"/api/v1/me/views/{view['id']}").status_code == 200
    assert client.get("/api/v1/me/views").json()["items"] == []
    assert client.delete(f"/api/v1/me/views/{view['id']}").status_code == 404


def test_views_name_unique_and_reserved():
    client, store = _env()
    _signup(client, store, "a@x.com")
    _login(client, "a@x.com")

    assert client.post("/api/v1/me/views", json={"name": "v1"}).status_code == 201
    r = client.post("/api/v1/me/views", json={"name": "v1"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "name_taken"

    # "default" 이름은 예약
    assert client.post("/api/v1/me/views", json={"name": "default"}).status_code == 422
    vid = client.get("/api/v1/me/views").json()["items"][0]["id"]
    r = client.patch(f"/api/v1/me/views/{vid}", json={"name": "Default"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "reserved_name"


def test_views_unknown_card_dropped_on_save():
    """뷰 저장 시 알 수 없는 card_id는 거부가 아니라 제거된다(저장 시 정리)."""
    client, store = _env()
    _signup(client, store, "a@x.com")
    _login(client, "a@x.com")

    r = client.post(
        "/api/v1/me/views",
        json={
            "name": "v",
            "layout": {
                "inbox": {"order": 0},
                "deleted_card_xyz": {"order": 1},
            },
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["view"]["layout"] == {"inbox": {"order": 0, "visible": True}}
    assert r.json()["dropped_card_ids"] == ["deleted_card_xyz"]


def test_views_default_undeletable():
    client, store = _env()
    _signup(client, store, "a@x.com")
    _login(client, "a@x.com")

    r = client.post("/api/v1/me/views", json={"name": "home", "is_default": True})
    assert r.status_code == 201
    vid = r.json()["view"]["id"]

    # 두 번째 뷰를 default로 만들면 이전 default는 내려간다
    r2 = client.post("/api/v1/me/views", json={"name": "ops", "is_default": True})
    assert r2.status_code == 201
    vid2 = r2.json()["view"]["id"]

    views = {v["id"]: v for v in client.get("/api/v1/me/views").json()["items"]}
    assert views[vid]["is_default"] is False
    assert views[vid2]["is_default"] is True

    # default 뷰 삭제 → 400
    r = client.delete(f"/api/v1/me/views/{vid2}")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "default_view_undeletable"

    # is_default 해제 후 삭제 가능
    client.patch(f"/api/v1/me/views/{vid2}", json={"is_default": False})
    assert client.delete(f"/api/v1/me/views/{vid2}").status_code == 200


def test_views_apply_sets_dashboard():
    client, store = _env()
    _signup(client, store, "a@x.com")
    _login(client, "a@x.com")

    r = client.post(
        "/api/v1/me/views",
        json={"name": "v", "layout": {"reports": {"order": 0, "visible": True}}},
    )
    vid = r.json()["view"]["id"]

    r = client.post(f"/api/v1/me/views/{vid}/apply")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["view"]["id"] == vid
    dash = body["dashboard"]
    assert dash["active_view_id"] == vid
    assert dash["cards"]["reports"] == {"order": 0, "visible": True}

    # GET /me/dashboard도 적용 상태를 반영한다
    got = client.get("/api/v1/me/dashboard").json()
    assert got["active_view_id"] == vid
    assert got["cards"]["reports"]["order"] == 0

    # 수동 PATCH는 active_view_id를 해제한다
    client.patch("/api/v1/me/dashboard", json={"inbox": {"order": 1}})
    assert client.get("/api/v1/me/dashboard").json()["active_view_id"] is None

    # apply된 뷰 삭제 → active_view_id 정리
    client.post(f"/api/v1/me/views/{vid}/apply")
    client.delete(f"/api/v1/me/views/{vid}")
    assert client.get("/api/v1/me/dashboard").json()["active_view_id"] is None


def test_views_user_isolation():
    client, store = _env()
    _signup(client, store, "a@x.com")
    _signup(client, store, "b@x.com")

    _login(client, "a@x.com")
    r = client.post("/api/v1/me/views", json={"name": "secret", "layout": {}})
    vid = r.json()["view"]["id"]

    _login(client, "b@x.com")
    assert client.get("/api/v1/me/views").json()["items"] == []
    # 다른 사용자의 뷰는 404(존재 여부 비노출)
    assert client.patch(f"/api/v1/me/views/{vid}", json={"name": "x"}).status_code == 404
    assert client.delete(f"/api/v1/me/views/{vid}").status_code == 404
    assert client.post(f"/api/v1/me/views/{vid}/apply").status_code == 404

    # a의 뷰는 그대로
    _login(client, "a@x.com")
    assert client.get("/api/v1/me/views").json()["items"][0]["name"] == "secret"
