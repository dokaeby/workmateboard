"""Workflow spec 버전ning 단위 테스트 — append-only 리비전/CAS 409/
restore-as-new-revision/백필/삭제 cascade.

Paperclip document versioning 포팅 — baseRevisionId CAS + 복원은 항상
새 리비전. 임시 SQLite Store + create_app(TestClient)으로 검증한다.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from jobmate.api.app import create_app
from jobmate.config import Settings
from jobmate.store.db import Store
from tests.integration.helpers import default_outputs, scripted_llm

_VALID_SPEC = {
    "key": "verflow",
    "version": "0.1.0",
    "description": "versioned",
    "stages": [{"key": "only", "kind": "function", "function": "noop_fn"}],
    "entry": "only",
}


def _env(tmp_path: Path):
    db = str(tmp_path / "t.db")
    settings = Settings(db_path=db, budget_max_tokens=100_000)
    store = Store(db)
    llm = scripted_llm(default_outputs())
    app = create_app(settings=settings, store=store, llm=llm, adapters={})
    return TestClient(app), store


def _save(client: TestClient, spec: dict, **kw) -> dict:
    body = {"name": "verflow", "spec": spec, **kw}
    r = client.post("/api/v1/workflows/custom", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def test_revision_append_and_list(tmp_path: Path) -> None:
    client, store = _env(tmp_path)

    r1 = _save(client, _VALID_SPEC, note="first save")
    assert r1["revision"] == 1
    r2 = _save(client, dict(_VALID_SPEC, version="0.2.0"), note="bump")
    assert r2["revision"] == 2

    # 목록 — 최신순, spec_preview 포함, spec 전문 없음
    items = client.get("/api/v1/workflows/custom/verflow/revisions").json()["items"]
    assert [i["revision"] for i in items] == [2, 1]
    assert items[0]["note"] == "bump"
    assert "spec_preview" in items[0]
    assert "spec" not in items[0]

    # 상세 — 전체 spec
    d = client.get("/api/v1/workflows/custom/verflow/revisions/1").json()
    assert d["name"] == "verflow" and d["revision"] == 1
    assert d["spec"]["version"] == "0.1.0"
    assert d["note"] == "first save"

    # 최신 spec은 custom_workflows와 동기화
    wf = store.get_custom_workflow("verflow")
    assert wf["latest_revision"] == 2
    assert wf["spec"]["version"] == "0.2.0"
    assert wf["updated_at"]

    # 미존재 워크플로/리비전 → 404
    assert client.get("/api/v1/workflows/custom/ghost/revisions").status_code == 404
    assert client.get("/api/v1/workflows/custom/verflow/revisions/99").status_code == 404


def test_revision_cas_conflict(tmp_path: Path) -> None:
    client, _ = _env(tmp_path)
    _save(client, _VALID_SPEC)  # revision 1
    _save(client, dict(_VALID_SPEC, version="0.2.0"))  # revision 2

    # stale base_revision=1 vs latest=2 → 409 revision_conflict
    r = client.post(
        "/api/v1/workflows/custom",
        json={
            "name": "verflow",
            "spec": dict(_VALID_SPEC, version="0.3.0"),
            "base_revision": 1,
        },
    )
    assert r.status_code == 409
    body = r.json()
    # 표준 err() 엔벨로프 — error.code + 최상위 current_revision
    assert body["error"]["code"] == "revision_conflict"
    assert body["current_revision"] == 2

    # 최신 base_revision → 성공, revision 3
    r = client.post(
        "/api/v1/workflows/custom",
        json={
            "name": "verflow",
            "spec": dict(_VALID_SPEC, version="0.3.0"),
            "base_revision": 2,
            "note": "cas ok",
        },
    )
    assert r.status_code == 201 and r.json()["revision"] == 3

    # base_revision 생략 → 항상 append (revision 4)
    r = client.post(
        "/api/v1/workflows/custom",
        json={"name": "verflow", "spec": dict(_VALID_SPEC, version="0.4.0")},
    )
    assert r.status_code == 201 and r.json()["revision"] == 4

    # 첫 저장에 base_revision=0 → revision 1 허용
    spec2 = dict(_VALID_SPEC, key="fresh")
    r = client.post(
        "/api/v1/workflows/custom",
        json={"name": "fresh", "spec": spec2, "base_revision": 0},
    )
    assert r.status_code == 201 and r.json()["revision"] == 1


def test_restore_as_new_revision(tmp_path: Path) -> None:
    client, store = _env(tmp_path)
    _save(client, _VALID_SPEC)  # rev 1, version 0.1.0
    _save(client, dict(_VALID_SPEC, version="0.2.0"))  # rev 2

    # rev 1 복원 → 새 revision 3 (이력 재작성 아님)
    r = client.post("/api/v1/workflows/custom/verflow/revisions/1/restore")
    assert r.status_code == 200, r.text
    assert r.json() == {"name": "verflow", "revision": 3}

    wf = store.get_custom_workflow("verflow")
    assert wf["latest_revision"] == 3
    assert wf["spec"]["version"] == "0.1.0"  # rev1 spec으로 복원됨

    # 리비전 1~3 모두 보존 — rev2는 여전히 0.2.0
    revs = {i["revision"]: i for i in store.list_custom_workflow_revisions("verflow")}
    assert set(revs) == {1, 2, 3}
    assert store.get_custom_workflow_revision("verflow", 2)["spec"]["version"] == "0.2.0"
    assert "restore" in revs[3]["note"]

    # 미존재 리비전/워크플로 → 404
    assert client.post("/api/v1/workflows/custom/verflow/revisions/99/restore").status_code == 404
    assert client.post("/api/v1/workflows/custom/ghost/revisions/1/restore").status_code == 404


def test_list_includes_latest_revision(tmp_path: Path) -> None:
    """GET /workflows/custom 목록 항목에 latest_revision 포함 — 프론트 CAS 기준."""
    client, _ = _env(tmp_path)
    _save(client, _VALID_SPEC)  # revision 1
    _save(client, dict(_VALID_SPEC, version="0.2.0"))  # revision 2

    items = client.get("/api/v1/workflows/custom").json()["items"]
    wf = next(i for i in items if i["name"] == "verflow")
    assert wf["latest_revision"] == 2


def test_restore_rejects_invalid_stored_spec(tmp_path: Path) -> None:
    """저장된 리비전 spec이 WorkflowSpec이 아니거나 key가 다르면 422 —
    손상 spec이 최신 리비전이 되는 것을 막는다."""
    client, store = _env(tmp_path)
    _save(client, _VALID_SPEC)

    # 손상 spec 리비전을 직접 삽입 (API로는 유효 spec만 저장 가능)
    store._conn.execute(
        "INSERT INTO workflow_revisions(id,name,revision,spec,note,created_by,created_at)"
        " VALUES(?,?,?,?,?,?,?)",
        (
            "badrev1",
            "verflow",
            9,
            json.dumps({"not": "a spec"}),
            "corrupt",
            "",
            "2024-01-01T00:00:00+00:00",
        ),
    )
    store._conn.commit()
    r = client.post("/api/v1/workflows/custom/verflow/revisions/9/restore")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_spec"
    # 최신 리비전은 그대로
    assert store.get_custom_workflow("verflow")["latest_revision"] == 1

    # 유효 WorkflowSpec이지만 key 불일치도 거부
    store._conn.execute(
        "INSERT INTO workflow_revisions(id,name,revision,spec,note,created_by,created_at)"
        " VALUES(?,?,?,?,?,?,?)",
        (
            "badrev2",
            "verflow",
            10,
            json.dumps(dict(_VALID_SPEC, key="other_name")),
            "wrong key",
            "",
            "2024-01-01T00:00:00+00:00",
        ),
    )
    store._conn.commit()
    r = client.post("/api/v1/workflows/custom/verflow/revisions/10/restore")
    assert r.status_code == 422
    assert store.get_custom_workflow("verflow")["latest_revision"] == 1


def test_delete_cascades_revisions(tmp_path: Path) -> None:
    client, store = _env(tmp_path)
    _save(client, _VALID_SPEC)
    _save(client, dict(_VALID_SPEC, version="0.2.0"))
    assert len(store.list_custom_workflow_revisions("verflow")) == 2

    assert client.delete("/api/v1/workflows/custom/verflow").status_code == 200
    assert store.get_custom_workflow("verflow") is None
    assert store.list_custom_workflow_revisions("verflow") == []


def test_revision_backfill_existing_db(tmp_path: Path) -> None:
    """기존 DB(리비전 이력 없음)의 custom_workflows 행 → Store 재오픈 시
    revision 1 백필 + latest_revision 동기화."""
    path = str(tmp_path / "old.db")
    s1 = Store(path)
    s1.close()

    # 구버전 행을 직접 삽입 — latest_revision/updated_at은 DEFAULT/NULL
    spec_json = json.dumps(_VALID_SPEC, ensure_ascii=False)
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO custom_workflows(name,spec,created_at) VALUES(?,?,?)",
        ("legacy", spec_json, "2024-01-01T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()

    s2 = Store(path)
    wf = s2.get_custom_workflow("legacy")
    assert wf is not None and wf["latest_revision"] == 1
    revs = s2.list_custom_workflow_revisions("legacy")
    assert len(revs) == 1 and revs[0]["revision"] == 1
    assert revs[0]["note"] == "backfill"
    assert s2.get_custom_workflow_revision("legacy", 1)["spec"]["key"] == "verflow"

    # 백필 후 저장은 revision 2로 이어진다
    res = s2.save_custom_workflow("legacy", _VALID_SPEC)
    assert res["ok"] and res["revision"] == 2
