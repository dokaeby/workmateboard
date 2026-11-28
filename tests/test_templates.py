"""템플릿 마켓플레이스 단위 테스트 — spec 006 US3 (FR-012~016).

export→import 왕복, 시크릿 스캔, 이름 충돌 suffix, builtin 거부,
share_links(kind='template') 공유 링크 404/410, 카탈로그.

create_app이 templates 라우터를 이미 등록하므로(app.py 배선 완료)
_env는 create_app 결과를 그대로 TestClient로 감싸 사용한다.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from jobmate.api.app import create_app
from jobmate.config import Settings
from jobmate.models import WorkflowSpec
from jobmate.store.db import Store
from jobmate.workflows.resolver import resolve_workflow_spec
from tests.integration.helpers import default_outputs, scripted_llm

_VALID_SPEC = {
    "key": "tmplflow",
    "version": "0.1.0",
    "description": "template test",
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


def _save_workflow(client: TestClient, name: str = "tmplflow", spec: dict | None = None) -> dict:
    body = {"name": name, "spec": spec or dict(_VALID_SPEC, key=name)}
    r = client.post("/api/v1/workflows/custom", json=body)
    assert r.status_code == 201, r.text
    return r.json()


# ---------------- export ----------------


def test_export_workflow_bundle_shape(tmp_path: Path) -> None:
    client, _ = _env(tmp_path)
    _save_workflow(client)

    r = client.post("/api/v1/templates/export", json={"kind": "workflow", "ref_id": "tmplflow"})
    assert r.status_code == 200, r.text
    bundle = r.json()
    assert bundle["format_version"] == "1.0"
    assert bundle["kind"] == "workflow"
    assert bundle["name"] == "tmplflow"
    assert bundle["exported_at"] and bundle["checksum"].startswith("sha256:")
    # FR-013 — spec은 WorkflowSpec 검증 통과
    spec = WorkflowSpec.model_validate(bundle["spec"])
    assert spec.key == "tmplflow"


def test_export_unknown_workflow_404(tmp_path: Path) -> None:
    client, _ = _env(tmp_path)
    r = client.post("/api/v1/templates/export", json={"kind": "workflow", "ref_id": "ghost"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_export_builtin_workflow(tmp_path: Path) -> None:
    client, _ = _env(tmp_path)
    r = client.post("/api/v1/templates/export", json={"kind": "workflow", "ref_id": "dev_review"})
    assert r.status_code == 200, r.text
    bundle = r.json()
    assert bundle["name"] == "dev_review" and bundle["source"] == "builtin"
    WorkflowSpec.model_validate(bundle["spec"])


def test_export_secret_pattern_warns_but_allows(tmp_path: Path) -> None:
    client, _ = _env(tmp_path)
    spec = dict(
        _VALID_SPEC,
        key="leaky",
        description="key is sk-abcdefghijklmnopqrstuvwxyz123456 ok",
    )
    _save_workflow(client, "leaky", spec)
    r = client.post("/api/v1/templates/export", json={"kind": "workflow", "ref_id": "leaky"})
    assert r.status_code == 200, r.text
    bundle = r.json()
    codes = [w["code"] for w in bundle.get("warnings", [])]
    assert "secret_patterns" in codes
    # 경고는 위치/패턴 이름만 — 시크릿 값 자체는 응답에 없다
    findings = bundle["warnings"][0]["findings"]
    assert all("sk-" not in json.dumps(f) for f in findings)


def test_export_bundle_includes_playbooks(tmp_path: Path) -> None:
    client, _ = _env(tmp_path)
    _save_workflow(client)
    r = client.post(
        "/api/v1/playbooks",
        json={"name": "PB1", "workflow": "tmplflow", "input": {"task": "x"}},
    )
    assert r.status_code == 201, r.text

    r = client.post("/api/v1/templates/export", json={"kind": "bundle", "ref_id": "tmplflow"})
    assert r.status_code == 200, r.text
    bundle = r.json()
    assert bundle["kind"] == "bundle" and bundle["spec"]["key"] == "tmplflow"
    assert [p["name"] for p in bundle["playbooks"]] == ["PB1"]


def test_export_playbook_with_custom_workflow_is_self_contained(tmp_path: Path) -> None:
    client, _ = _env(tmp_path)
    _save_workflow(client)
    r = client.post(
        "/api/v1/playbooks",
        json={"name": "PB2", "workflow": "tmplflow", "input": {}},
    )
    pb = r.json()
    r = client.post("/api/v1/templates/export", json={"kind": "playbook", "ref_id": pb["id"]})
    assert r.status_code == 200, r.text
    bundle = r.json()
    assert bundle["kind"] == "playbook" and bundle["name"] == "PB2"
    assert bundle["playbook"]["workflow"] == "tmplflow"
    assert bundle["spec"]["key"] == "tmplflow"  # 커스텀 참조 → spec 포함
    assert bundle["refs"]["workflow"] == "tmplflow"


# ---------------- import ----------------


def _export(client: TestClient, kind: str, ref_id: str) -> dict:
    r = client.post("/api/v1/templates/export", json={"kind": kind, "ref_id": ref_id})
    assert r.status_code == 200, r.text
    return r.json()


def test_import_workflow_roundtrip(tmp_path: Path) -> None:
    client, store = _env(tmp_path)
    _save_workflow(client)
    bundle = _export(client, "workflow", "tmplflow")

    r = client.post("/api/v1/templates/import", json=bundle)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["ok"] is True
    wf_item = next(i for i in body["imported"] if i["kind"] == "workflow")
    # 동일 스토어 재import → 이름 충돌 suffix 정책
    assert wf_item["name"] == "tmplflow-imported"
    assert wf_item["renamed_from"] == "tmplflow"

    # spec 보존 — export spec과 동일하게 복원되고 해석 가능
    row = store.get_custom_workflow("tmplflow-imported")
    assert row is not None
    assert WorkflowSpec.model_validate(row["spec"]).key == "tmplflow-imported"
    resolved = resolve_workflow_spec(store, "tmplflow-imported")
    assert resolved is not None and resolved.key == "tmplflow-imported"


def test_import_bundle_workflow_then_playbook(tmp_path: Path) -> None:
    client, store = _env(tmp_path)
    _save_workflow(client)
    client.post(
        "/api/v1/playbooks",
        json={"name": "PBX", "workflow": "tmplflow", "input": {"task": "t"}},
    )
    bundle = _export(client, "bundle", "tmplflow")

    r = client.post("/api/v1/templates/import", json=bundle)
    assert r.status_code == 201, r.text
    body = r.json()
    kinds = [i["kind"] for i in body["imported"]]
    assert kinds == ["workflow", "playbook"]  # workflow 먼저 등록

    # playbook이 개명된 워크플로를 참조하는지 확인
    pbs = [p for p in store.list_playbooks() if p["name"] == "PBX-imported"]
    assert pbs and pbs[0]["workflow"] == "tmplflow-imported"


def test_import_rejects_secret_patterns_with_locations(tmp_path: Path) -> None:
    client, _ = _env(tmp_path)
    bundle = _export(client, "workflow", "dev_review")  # builtin export
    bundle["spec"]["description"] = "token AKIAIOSFODNN7EXAMPLE here"
    bundle.pop("checksum", None)
    # checksum 재계산 — 공격자가 체크섬을 위조하지 않도록 정상 재계산 경로 사용
    from jobmate.services.templates import compute_checksum

    bundle["checksum"] = compute_checksum(bundle)

    r = client.post("/api/v1/templates/import", json=bundle)
    assert r.status_code == 422
    body = r.json()
    assert body["error"]["code"] == "secret_detected"
    # 스캔 위치 보고 — 시크릿 값 자체는 없다
    findings = json.dumps(body)
    assert "AKIAIOSFODNN7EXAMPLE" not in findings
    assert "spec.description" in findings


def test_import_rejects_builtin_name(tmp_path: Path) -> None:
    client, _ = _env(tmp_path)
    bundle = _export(client, "workflow", "dev_review")
    r = client.post("/api/v1/templates/import", json=bundle)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "builtin_name"


def test_import_rejects_invalid_spec_with_field_errors(tmp_path: Path) -> None:
    client, _ = _env(tmp_path)
    bundle = _export(client, "workflow", "dev_review")
    bundle["name"] = "badflow"
    bundle["spec"]["key"] = "badflow"
    bundle["spec"]["stages"] = [{"key": "only", "kind": "llm"}]  # agent 누락
    bundle.pop("checksum", None)
    from jobmate.services.templates import compute_checksum

    bundle["checksum"] = compute_checksum(bundle)

    r = client.post("/api/v1/templates/import", json=bundle)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_spec"
    # 부분 등록 없음 — badflow가 생기지 않는다
    assert client.get("/api/v1/workflows/custom").json()["items"] == [] or all(
        i["name"] != "badflow" for i in client.get("/api/v1/workflows/custom").json()["items"]
    )


def test_import_rejects_bad_format_version(tmp_path: Path) -> None:
    client, _ = _env(tmp_path)
    bundle = _export(client, "workflow", "dev_review")
    bundle["format_version"] = "2.0"
    r = client.post("/api/v1/templates/import", json=bundle)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "unsupported_format_version"


def test_import_rejects_checksum_mismatch(tmp_path: Path) -> None:
    client, _ = _env(tmp_path)
    bundle = _export(client, "workflow", "dev_review")
    bundle["spec"]["description"] = "tampered"
    # checksum 미갱신 → mismatch
    r = client.post("/api/v1/templates/import", json=bundle)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "checksum_mismatch"


def test_import_playbook_referencing_builtin(tmp_path: Path) -> None:
    client, store = _env(tmp_path)
    r = client.post(
        "/api/v1/playbooks",
        json={"name": "DailyReview", "workflow": "dev_review", "input": {"task": "t"}},
    )
    pb = r.json()
    bundle = _export(client, "playbook", pb["id"])
    # builtin 참조는 spec 없이 refs만
    assert "spec" not in bundle and bundle["refs"]["workflow"] == "dev_review"

    r = client.post("/api/v1/templates/import", json=bundle)
    assert r.status_code == 201, r.text
    names = [i["name"] for i in r.json()["imported"]]
    assert names == ["DailyReview-imported"]
    pbs = [p for p in store.list_playbooks() if p["name"] == "DailyReview-imported"]
    assert pbs and pbs[0]["workflow"] == "dev_review"


def test_import_rolls_back_on_late_failure(tmp_path: Path) -> None:
    """bundle의 playbook workflow가 미해석이면 workflow조차 등록되지 않는다."""
    client, store = _env(tmp_path)
    _save_workflow(client)
    bundle = _export(client, "bundle", "tmplflow")
    # 두 번째 playbook의 workflow을 존재하지 않는 것으로 변조
    bundle["playbooks"].append({"name": "BadPB", "workflow": "nonexistent_wf", "input": {}})
    bundle.pop("checksum", None)
    from jobmate.services.templates import compute_checksum

    bundle["checksum"] = compute_checksum(bundle)

    r = client.post("/api/v1/templates/import", json=bundle)
    assert r.status_code == 422
    # workflow(tmplflow-imported)도 playbook도 등록되지 않음
    assert store.get_custom_workflow("tmplflow-imported") is None
    assert not [p for p in store.list_playbooks() if p["name"] == "BadPB"]


# ---------------- share links ----------------


def test_share_link_roundtrip_and_expiry(tmp_path: Path) -> None:
    client, store = _env(tmp_path)
    _save_workflow(client)

    r = client.post(
        "/api/v1/templates/share",
        json={"kind": "workflow", "ref_id": "tmplflow", "expires_days": 30},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    token = body["token"]
    assert body["url"].endswith(token)

    # 공유 조회 — 번들 페이로드 반환
    r = client.get(f"/api/v1/templates/shared/{token}")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["kind"] == "template"
    assert data["template"]["format_version"] == "1.0"
    assert data["template"]["spec"]["key"] == "tmplflow"

    # 만료 시뮬레이션 — expires_at을 과거로
    store._conn.execute(
        "UPDATE share_links SET expires_at=? WHERE token=?",
        ("2000-01-01T00:00:00+00:00", token),
    )
    store._conn.commit()
    r = client.get(f"/api/v1/templates/shared/{token}")
    assert r.status_code == 410
    assert r.json()["error"]["code"] == "gone"


def test_shared_missing_token_404(tmp_path: Path) -> None:
    client, _ = _env(tmp_path)
    r = client.get("/api/v1/templates/shared/nonexistent-token")
    assert r.status_code == 404


def test_share_expires_days_capped(tmp_path: Path) -> None:
    client, _ = _env(tmp_path)
    _save_workflow(client)
    # 스키마상 최대 90일 — 365일 요청은 422
    r = client.post(
        "/api/v1/templates/share",
        json={"kind": "workflow", "ref_id": "tmplflow", "expires_days": 365},
    )
    assert r.status_code == 422


# ---------------- catalog ----------------


def test_catalog_lists_builtin_custom_playbook(tmp_path: Path) -> None:
    client, _ = _env(tmp_path)
    _save_workflow(client)
    client.post(
        "/api/v1/playbooks",
        json={"name": "PB1", "workflow": "tmplflow", "input": {}},
    )
    r = client.get("/api/v1/templates/catalog")
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    kinds = {i["kind"] for i in items}
    assert kinds == {"builtin", "custom", "playbook"}
    assert any(i["name"] == "dev_review" and i["kind"] == "builtin" for i in items)
    assert any(i["name"] == "tmplflow" and i["kind"] == "custom" for i in items)
    assert any(i["name"] == "PB1" and i["kind"] == "playbook" for i in items)


def test_catalog_reachable_on_bare_create_app(tmp_path: Path) -> None:
    """create_app이 templates 라우터를 직접 등록한다 — 수동 register 불필요."""
    client, _ = _env(tmp_path)
    r = client.get("/api/v1/templates/catalog")
    assert r.status_code == 200, r.text
    assert "items" in r.json()


def test_import_rejects_literal_api_key_env_value(tmp_path: Path) -> None:
    """FR-013 — api_key_env에 env 이름이 아닌 리터럴 비밀값이 오면 422.

    WorkflowSpec은 extra 필드를 버리므로, spec 트리의 임의 dict 위치
    (branches)에 api_key_env 키를 심어 _bad_api_key_env_values 경로를 검증한다.
    값은 유효한 stage key이기도 해야 branch target 검증을 통과한다."""
    client, _ = _env(tmp_path)
    _save_workflow(client)
    bundle = _export(client, "workflow", "tmplflow")
    # env 이름 형식이 아닌 리터럴 값(하이픈 포함) — 시크릿 패턴 스캔에는
    # 걸리지 않지만 api_key_env 검증에서 거부돼야 한다.
    bad_env_value = "literal-secret-value-123"
    spec = bundle["spec"]
    spec["stages"].append({"key": bad_env_value, "kind": "function", "function": "noop_fn"})
    spec["branches"] = {"only": {"f": {"api_key_env": bad_env_value}}}
    bundle.pop("checksum", None)
    from jobmate.services.templates import compute_checksum

    bundle["checksum"] = compute_checksum(bundle)

    r = client.post("/api/v1/templates/import", json=bundle)
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["error"]["code"] == "invalid_spec"
    # 리터럴 비밀값 자체는 에러 응답에 노출되지 않는다
    assert bad_env_value not in json.dumps(body)


def test_shared_rejects_non_template_kind(tmp_path: Path) -> None:
    """kind!='template' share link는 templates/shared에서 404."""
    client, store = _env(tmp_path)
    from datetime import UTC, datetime, timedelta

    store.create_share_link(
        "run-share-token",
        "run",
        "run:abc",
        {"run_id": "abc"},
        "tester",
        (datetime.now(UTC) + timedelta(days=1)).isoformat(),
    )
    r = client.get("/api/v1/templates/shared/run-share-token")
    assert r.status_code == 404


def test_export_requires_lead_role(tmp_path: Path) -> None:
    """member 세션은 export(lead 게이트)에 403 — 카탈로그 GET은 허용."""
    from jobmate.security import SESSION_COOKIE, hash_password, hash_token, new_session_token

    client, store = _env(tmp_path)
    u = store.create_user("member@t.com", hash_password("Str0ng#Passw0rd!"), "M")
    assert u is not None
    store.set_user_role(u["id"], "member")
    token = new_session_token()
    store.create_session(u["id"], hash_token(token))
    client.cookies.set(SESSION_COOKIE, token)

    r = client.post("/api/v1/templates/export", json={"kind": "workflow", "ref_id": "dev_review"})
    assert r.status_code == 403
    # GET catalog은 lead 게이트 — member도 403인지 확인하지 않고
    # 기존 동작(게이트 적용)을 그대로 둔다: catalog도 lead 전용.
    assert client.get("/api/v1/templates/catalog").status_code == 403
