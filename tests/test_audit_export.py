"""spec 005 US3 — 감사 로그 필터 조회·CSV/JSON 스트리밍 export 테스트."""

from __future__ import annotations

import csv
import hashlib
import io
import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from jobmate.api.app import create_app
from jobmate.api.routers.misc import _AUDIT_HASH_GENESIS, _AUDIT_HASH_SEED
from jobmate.config import Settings
from jobmate.store.db import Store


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("JOBMATE_ALLOWED_EMAILS", raising=False)
    monkeypatch.delenv("VERCEL", raising=False)
    store = Store(str(tmp_path / "audit_export.db"))
    app = create_app(
        settings=Settings(db_path=tmp_path / "audit_export.db"),
        store=store,
        llm=None,
        adapters={},
    )
    yield TestClient(app), store
    store.close()


def _signup(c: TestClient, email: str) -> None:
    r = c.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": "Str0ng#Passw0rd!", "agree_terms": True},
    )
    assert r.status_code == 201, r.text


def _member_client(app, store) -> TestClient:
    """member 역할 클라이언트 — 첫 가입자는 admin, 두 번째는 member로 강등."""
    admin = TestClient(app)
    member = TestClient(app)
    _signup(admin, "admin-ae@example.com")
    _signup(member, "member-ae@example.com")
    mu = store.get_user_by_email("member-ae@example.com")
    for o in store.list_user_orgs(mu["id"]):
        store.set_org_member_role(o["id"], mu["id"], "member")
    return member


def _audit(store: Store, action: str, actor: str = "local", **kw) -> None:
    store.audit(
        kw.get("run_id", ""),
        actor,
        action,
        kw.get("target", ""),
        kw.get("detail", {}),
    )


def _backdate(store: Store, action: str, when: datetime) -> None:
    """특정 action의 created_at을 과거로 이동 — from/to 필터 검증용."""
    with store.tx() as c:
        c.execute(
            "UPDATE audit_events SET created_at=? WHERE action=?",
            (when.isoformat(), action),
        )


# ---------------- GET /api/v1/audit 필터 ----------------


def test_audit_filters_action_prefix(client):
    c, store = client
    _audit(store, "auth_login", actor="a@x.com")
    _audit(store, "auth_login_failed", actor="a@x.com")
    _audit(store, "card_create", actor="b@x.com")

    r = c.get("/api/v1/audit", params={"action": "auth_"})
    assert r.status_code == 200
    actions = {i["action"] for i in r.json()["items"]}
    assert actions == {"auth_login", "auth_login_failed"}


def test_audit_filters_actor_run_target(client):
    c, store = client
    _audit(store, "run_start", actor="a@x.com", run_id="run-1", target="obj-1")
    _audit(store, "run_start", actor="b@x.com", run_id="run-2", target="obj-2")

    r = c.get("/api/v1/audit", params={"actor": "a@x.com"})
    assert {i["actor_email"] for i in r.json()["items"]} == {"a@x.com"}

    r = c.get("/api/v1/audit", params={"run_id": "run-2"})
    items = r.json()["items"]
    assert len(items) == 1 and items[0]["run_id"] == "run-2"

    r = c.get("/api/v1/audit", params={"target": "obj-1"})
    items = r.json()["items"]
    assert len(items) == 1 and items[0]["target"] == "obj-1"


def test_audit_filters_from_to(client):
    c, store = client
    old = datetime.now(UTC) - timedelta(days=400)
    _audit(store, "old_event")
    _backdate(store, "old_event", old)
    _audit(store, "new_event")

    frm = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    r = c.get("/api/v1/audit", params={"from": frm})
    actions = {i["action"] for i in r.json()["items"]}
    assert "new_event" in actions and "old_event" not in actions

    to = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    r = c.get("/api/v1/audit", params={"to": to})
    actions = {i["action"] for i in r.json()["items"]}
    assert "old_event" in actions and "new_event" not in actions


def test_audit_bad_from_rejected(client):
    c, _ = client
    assert c.get("/api/v1/audit", params={"from": "not-a-date"}).status_code == 422


def test_audit_keyset_pagination(client):
    c, store = client
    for i in range(5):
        _audit(store, f"evt_{i}")

    seen: list[str] = []
    cursor = ""
    for _ in range(10):
        params = {"limit": 2}
        if cursor:
            params["cursor"] = cursor
        r = c.get("/api/v1/audit", params=params)
        assert r.status_code == 200
        body = r.json()
        seen.extend(i["action"] for i in body["items"])
        cursor = body.get("next_cursor") or ""
        if not cursor:
            break
    assert len(seen) == len(set(seen)) == 5


def test_audit_invalid_cursor_rejected(client):
    c, _ = client
    r = c.get("/api/v1/audit", params={"cursor": "!!!bad!!!"})
    assert r.status_code == 422


# ---------------- export ----------------


def test_export_csv_streams_rows_and_records_event(client):
    c, store = client
    _audit(store, "auth_login", actor="a@x.com", detail={"ip": "1.2.3.4"})
    _audit(store, "card_create", actor="b@x.com")

    r = c.get("/api/v1/audit/export", params={"format": "csv", "action": "auth_"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers.get("content-disposition", "")
    body = r.content
    assert body.startswith(b"\xef\xbb\xbf")  # UTF-8 BOM
    text = body.decode("utf-8-sig")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    assert lines[0].startswith("# filters:")
    assert '"action": "auth_"' in lines[0]
    header = next(ln for ln in lines if ln.startswith("id,"))
    assert header == "id,ts,run_id,actor,action,target,detail"
    data_lines = [ln for ln in lines if ln.startswith(tuple("0123456789abcdef"))]
    # auth_login 1건 + 이 export가 남긴 audit_export는 action 필터로 제외
    assert len(data_lines) == 1
    assert "auth_login" in data_lines[0] and "a@x.com" in data_lines[0]

    # FR-023 — audit_export 이벤트 기록(필터·형식·행 수·요청자, 내용물 없음)
    exports = [e for e in store.list_audit(limit=100) if e["action"] == "audit_export"]
    assert len(exports) == 1
    d = exports[0]["detail"]
    assert d["format"] == "csv" and d["row_count"] == 1
    assert d["filters"]["action"] == "auth_"
    assert "requester" in d
    assert "1.2.3.4" not in json.dumps(d)  # 행 내용 미포함


def test_export_csv_formula_injection_guarded(client):
    c, store = client
    _audit(store, "=cmd|' /c calc'!A1", actor="+evil")

    r = c.get("/api/v1/audit/export", params={"format": "csv"})
    assert r.status_code == 200
    text = r.content.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))
    # 수식으로 시작하는 셀은 ' 접두 처리
    flat = [cell for row in rows for cell in row]
    assert any(cell.startswith("'=cmd") or cell.startswith("'+evil") for cell in flat)
    for row in rows:
        for cell in row:
            assert not cell.startswith(("=", "+", "-", "@"))


def test_export_json_structure(client):
    c, store = client
    _audit(store, "auth_login", actor="a@x.com")

    r = c.get("/api/v1/audit/export", params={"format": "json", "action": "auth_"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    payload = json.loads(r.content)
    assert payload["generated_at"]
    assert payload["filters"]["action"] == "auth_"
    assert payload["filters"]["actor"] is None
    assert len(payload["rows"]) == 1
    assert payload["rows"][0]["action"] == "auth_login"
    assert "integrity" not in payload


def test_export_json_integrity_hash_chain(client):
    c, store = client
    for i in range(3):
        _audit(store, f"hash_evt_{i}", actor=f"u{i}@x.com")

    r = c.get(
        "/api/v1/audit/export",
        params={"format": "json", "integrity": "hash", "action": "hash_evt_"},
    )
    assert r.status_code == 200
    payload = json.loads(r.content)
    rows = payload["rows"]
    assert len(rows) == 3
    integ = payload["integrity"]
    assert integ["algorithm"] == "sha256-chain-v1"
    assert integ["row_count"] == 3

    # 체인 재계산 — canonical JSON + prev_hash 순서 검증
    prev = hashlib.sha256(_AUDIT_HASH_SEED).hexdigest()
    file_h = hashlib.sha256(_AUDIT_HASH_SEED)
    for i, row in enumerate(rows, start=1):
        assert row["_seq"] == i
        assert row["_prev_hash"] == prev
        canon = json.dumps(
            {k: v for k, v in row.items() if not k.startswith("_")},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        expect = hashlib.sha256((prev + "|" + canon).encode()).hexdigest()
        assert row["_row_hash"] == expect
        file_h.update(expect.encode("ascii"))
        prev = expect
    assert integ["file_hash"] == file_h.hexdigest()
    assert integ["file_hash"] != _AUDIT_HASH_GENESIS or len(rows) == 0


def test_export_integrity_requires_json(client):
    c, _ = client
    r = c.get("/api/v1/audit/export", params={"format": "csv", "integrity": "hash"})
    assert r.status_code == 422
    assert c.get("/api/v1/audit/export", params={"format": "xml"}).status_code == 422
    assert c.get("/api/v1/audit/export", params={"integrity": "md5"}).status_code == 422
    assert (
        c.get("/api/v1/audit/export", params={"format": "json", "from": "bad"}).status_code == 422
    )


def test_export_admin_only(client):
    c, store = client
    member = _member_client(c.app, store)
    assert member.get("/api/v1/audit/export").status_code == 403
    assert member.get("/api/v1/audit").status_code == 403
    # admin(로컬 모드)은 통과
    assert c.get("/api/v1/audit/export", params={"format": "json"}).status_code == 200


def test_export_empty_result_still_records(client):
    c, store = client
    r = c.get("/api/v1/audit/export", params={"format": "json", "actor": "nobody@x.com"})
    assert r.status_code == 200
    payload = json.loads(r.content)
    assert payload["rows"] == []
    exports = [e for e in store.list_audit(100) if e["action"] == "audit_export"]
    assert exports and exports[0]["detail"]["row_count"] == 0


# ---------------- include_platform (org_id='' 플랫폼 행) ----------------


def _audit_row_org(store: Store, action: str, actor: str, org_id: str) -> None:
    """org_id를 명시한 audit_events 행 삽입 — 플랫폼('')/조직 행 구분용."""
    import uuid

    with store.tx() as c:
        c.execute(
            "INSERT INTO audit_events(id,run_id,actor,action,object_ref,detail,org_id,"
            "created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                uuid.uuid4().hex[:12],
                "",
                actor,
                action,
                "",
                "{}",
                org_id,
                datetime.now(UTC).isoformat(),
            ),
        )


def _platform_and_org_admin_clients(app, store):
    """(플랫폼 admin 세션, 조직 admin 세션) 클라이언트 쌍.

    로컬 모드 첫 가입자는 users.role=admin(플랫폼 admin), 두 번째는
    role=user지만 org_owner로 승격 — _admin_user는 통과하고
    _platform_admin_user는 거부되는 세션."""
    padmin = TestClient(app)
    oadmin = TestClient(app)
    _signup(padmin, "padmin-ae@example.com")
    _signup(oadmin, "oowner-ae@example.com")
    mu = store.get_user_by_email("oowner-ae@example.com")
    for o in store.list_user_orgs(mu["id"]):
        store.set_org_member_role(o["id"], mu["id"], "org_owner")
    return padmin, oadmin


def test_audit_include_platform_visibility(client):
    """platform admin은 org_id='' 플랫폼 행을 보고, 조직 admin은 보지 않는다."""
    c, store = client
    padmin, oadmin = _platform_and_org_admin_clients(c.app, store)
    org2 = store.user_default_org_id(store.get_user_by_email("oowner-ae@example.com")["id"])
    _audit_row_org(store, "auth_login", "platform-svc", "")
    _audit_row_org(store, "org_only_event", "o@x.com", org2)

    # 플랫폼 admin — 플랫폼 행이 조회/export 모두에 포함
    r = padmin.get("/api/v1/audit", params={"actor": "platform-svc"})
    assert r.status_code == 200
    assert any(i["actor_email"] == "platform-svc" for i in r.json()["items"])
    rex = padmin.get("/api/v1/audit/export", params={"format": "json", "actor": "platform-svc"})
    payload = json.loads(rex.content)
    assert any(row["actor"] == "platform-svc" for row in payload["rows"])

    # 조직 admin — 같은 플랫폼 행은 조회/export 모두에서 제외
    r2 = oadmin.get("/api/v1/audit", params={"actor": "platform-svc"})
    assert r2.status_code == 200
    assert all(i["actor_email"] != "platform-svc" for i in r2.json()["items"])
    rex2 = oadmin.get("/api/v1/audit/export", params={"format": "json", "actor": "platform-svc"})
    payload2 = json.loads(rex2.content)
    assert all(row["actor"] != "platform-svc" for row in payload2["rows"])
    # 조직 admin은 자기 조직 행은 본다
    r3 = oadmin.get("/api/v1/audit", params={"action": "org_only_event"})
    assert any(i["action"] == "org_only_event" for i in r3.json()["items"])


# ---------------- 복합 필터·배치 경계 ----------------


def test_audit_combined_from_actor_action(client):
    """from+actor+action 복합 필터 — 모든 조건을 동시에 만족하는 행만."""
    c, store = client
    old = datetime.now(UTC) - timedelta(days=400)
    _audit_row_org(store, "keep_me", "b@x.com", "")
    with store.tx() as tx:
        tx.execute(
            "UPDATE audit_events SET created_at=? WHERE action='keep_me' AND actor='b@x.com'",
            (old.isoformat(),),
        )
    _audit(store, "keep_me", actor="a@x.com")
    _audit(store, "drop_me", actor="a@x.com")

    frm = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    r = c.get(
        "/api/v1/audit",
        params={"from": frm, "actor": "a@x.com", "action": "keep_"},
    )
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["action"] == "keep_me" and items[0]["actor_email"] == "a@x.com"


def test_export_batch_boundary_over_500(client):
    """iter_audit batch=500 경계를 넘는 export — 510행 전부 스트리밍되고
    audit_export.row_count가 실제 반환 행 수와 일치한다."""
    c, store = client
    for i in range(510):
        _audit(store, f"bulk_{i:03d}")

    r = c.get("/api/v1/audit/export", params={"format": "json", "action": "bulk_"})
    assert r.status_code == 200
    payload = json.loads(r.content)
    assert len(payload["rows"]) == 510
    exports = [e for e in store.list_audit(100) if e["action"] == "audit_export"]
    assert exports and exports[0]["detail"]["row_count"] == 510
