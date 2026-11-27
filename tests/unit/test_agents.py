"""Agent registry 단위 테스트 — CRUD/조직도 규칙/통계/인증 게이트.

임시 SQLite Store + create_app(TestClient)으로 검증한다.
네트워크·LLM은 사용하지 않는다(run 생성은 store 직접 호출로 통계만 확인).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jobmate.api.app import create_app
from jobmate.config import Settings
from jobmate.models import StageStatus
from jobmate.security import SESSION_COOKIE, hash_password, hash_token, new_session_token
from jobmate.store.db import Store
from tests.integration.helpers import default_outputs, scripted_llm


def _env(tmp_path: Path):
    db = str(tmp_path / "t.db")
    settings = Settings(db_path=db, budget_max_tokens=100_000)
    store = Store(db)
    llm = scripted_llm(default_outputs())
    app = create_app(settings=settings, store=store, llm=llm, adapters={})
    return TestClient(app), store


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """JOBMATE_API_KEY 설정 — 로컬 관리자 폴백을 꺼 role 게이트를 실제로 검증."""
    monkeypatch.setenv("JOBMATE_API_KEY", "test-api-key")
    return _env(tmp_path)


def _login(store: Store, client: TestClient, email: str, role: str = "member") -> None:
    u = store.get_user_by_email(email) or store.create_user(
        email, hash_password("Str0ng#Passw0rd!"), email
    )
    assert u is not None
    store.set_user_role(u["id"], role)
    token = new_session_token()
    store.create_session(u["id"], hash_token(token))
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE, token)


def _mk(client: TestClient, name: str = "Agent", **kw) -> dict:
    body = {"name": name, "adapter_type": "claude", **kw}
    r = client.post("/api/v1/agents", json=body)
    assert r.status_code == 201, r.text
    return r.json()


# ---------------- CRUD ----------------


def test_agent_crud(tmp_path: Path) -> None:
    client, store = _env(tmp_path)
    a = _mk(
        client,
        "Lead Dev",
        role="tech-lead",
        description="오케스트레이터",
        capabilities=["plan", "review"],
        monthly_budget_usd=50.0,
    )
    assert a["name"] == "Lead Dev"
    assert a["adapter_type"] == "claude"
    assert a["role"] == "tech-lead"
    assert a["capabilities"] == ["plan", "review"]
    assert a["status"] == "active"
    assert a["reports_to"] is None
    assert a["monthly_budget_usd"] == 50.0
    assert a["runs_30d"] == 0 and a["cost_30d_usd"] == 0.0
    assert a["last_run_at"] is None
    assert a["created_at"] and a["updated_at"]

    # 목록
    items = client.get("/api/v1/agents").json()["items"]
    assert len(items) == 1 and items[0]["id"] == a["id"]

    # PATCH 부분 갱신
    r = client.patch(f"/api/v1/agents/{a['id']}", json={"status": "paused", "role": "ic"})
    assert r.status_code == 200
    assert r.json()["status"] == "paused" and r.json()["role"] == "ic"
    assert r.json()["name"] == "Lead Dev"  # 미전송 필드 유지

    # PATCH unknown → 404
    assert client.patch("/api/v1/agents/nope", json={"role": "x"}).status_code == 404

    # DELETE → 승인된 agent_terminate 없이는 409 approval_required
    # (spec 2차 D — archived는 canonical terminated). 승인 후에는 200.
    r = client.delete(f"/api/v1/agents/{a['id']}")
    assert r.status_code == 409 and r.json()["error"]["code"] == "approval_required"
    ar = client.post(
        "/api/v1/management/approvals",
        json={
            "action_type": "agent_terminate",
            "resource_type": "agent",
            "resource_id": a["id"],
            "safe_payload": {"agent_id": a["id"]},
        },
    )
    assert ar.status_code == 201
    d = client.post(
        f"/api/v1/management/approvals/{ar.json()['id']}/decision",
        json={"decision": "approve"},
    )
    assert d.status_code == 200
    r = client.delete(f"/api/v1/agents/{a['id']}")
    assert r.status_code == 200 and r.json()["status"] == "archived"
    assert store.get_agent(a["id"])["status"] == "archived"
    assert client.delete("/api/v1/agents/nope").status_code == 404


def test_agent_name_unique_case_insensitive(tmp_path: Path) -> None:
    client, _ = _env(tmp_path)
    _mk(client, "Alpha")
    r = client.post("/api/v1/agents", json={"name": "alpha", "adapter_type": "codex"})
    assert r.status_code == 409
    # PATCH로 다른 에이전트 이름을 중복으로 변경 → 409
    b = _mk(client, "Beta")
    r = client.patch(f"/api/v1/agents/{b['id']}", json={"name": "ALPHA"})
    assert r.status_code == 409


def test_agent_validation(tmp_path: Path) -> None:
    client, _ = _env(tmp_path)
    # 빈 이름 / 공백 이름
    assert (
        client.post("/api/v1/agents", json={"name": "  ", "adapter_type": "claude"}).status_code
        == 422
    )
    # 잘못된 adapter_type
    r = client.post("/api/v1/agents", json={"name": "x", "adapter_type": "gpt"})
    assert r.status_code == 422 and "adapter_type" in r.json()["error"]["message"]
    # 허용 값: claude/codex/gemini/devin/custom
    for at in ("claude", "codex", "gemini", "devin", "custom"):
        r = client.post("/api/v1/agents", json={"name": f"a-{at}", "adapter_type": at})
        assert r.status_code == 201, at
    # 잘못된 status
    r = client.post(
        "/api/v1/agents", json={"name": "bad", "adapter_type": "claude", "status": "dead"}
    )
    assert r.status_code == 422
    # 음수 예산
    r = client.post(
        "/api/v1/agents",
        json={"name": "neg", "adapter_type": "claude", "monthly_budget_usd": -1},
    )
    assert r.status_code == 422
    # capabilities가 문자열 → 422
    r = client.post(
        "/api/v1/agents",
        json={"name": "cap", "adapter_type": "claude", "capabilities": "plan"},
    )
    assert r.status_code == 422


# ---------------- 조직도(reports_to) 규칙 ----------------


def test_reports_to_org_rules(tmp_path: Path) -> None:
    client, store = _env(tmp_path)
    lead = _mk(client, "Lead")
    dev = _mk(client, "Dev", reports_to=lead["id"])
    assert dev["reports_to"] == lead["id"]

    # 미존재 reports_to → 422
    r = client.post(
        "/api/v1/agents",
        json={"name": "orphan", "adapter_type": "claude", "reports_to": "ghost"},
    )
    assert r.status_code == 422 and "not found" in r.json()["error"]["message"]

    # 자기 자신 → 422
    r = client.patch(f"/api/v1/agents/{lead['id']}", json={"reports_to": lead["id"]})
    assert r.status_code == 422 and "itself" in r.json()["error"]["message"]

    # 순환 a→b→a: lead가 dev를 상위로 → cycle
    r = client.patch(f"/api/v1/agents/{lead['id']}", json={"reports_to": dev["id"]})
    assert r.status_code == 422 and "cycle" in r.json()["error"]["message"]

    # 3단계 순환: c→dev→lead 체인에서 lead.reports_to=c 시도 → cycle
    c = _mk(client, "C", reports_to=dev["id"])
    r = client.patch(f"/api/v1/agents/{lead['id']}", json={"reports_to": c["id"]})
    assert r.status_code == 422 and "cycle" in r.json()["error"]["message"]
    # dev.reports_to=c 도 dev→c→dev 순환 → cycle
    r = client.patch(f"/api/v1/agents/{dev['id']}", json={"reports_to": c["id"]})
    assert r.status_code == 422 and "cycle" in r.json()["error"]["message"]

    # 정상 재배치 허용: c.reports_to=lead (c→lead→null, 순환 아님)
    r = client.patch(f"/api/v1/agents/{c['id']}", json={"reports_to": lead["id"]})
    assert r.status_code == 200 and r.json()["reports_to"] == lead["id"]
    # dev→c→lead→null 체인도 유효
    r = client.patch(f"/api/v1/agents/{dev['id']}", json={"reports_to": c["id"]})
    assert r.status_code == 200

    # archived 에이전트는 reports_to 대상 불가
    assert store.archive_agent(c["id"])
    r = client.post(
        "/api/v1/agents",
        json={"name": "new", "adapter_type": "claude", "reports_to": c["id"]},
    )
    assert r.status_code == 422 and "archived" in r.json()["error"]["message"]

    # reports_to=null → 상위 해제
    r = client.patch(f"/api/v1/agents/{dev['id']}", json={"reports_to": None})
    assert r.status_code == 200 and r.json()["reports_to"] is None


def test_archived_parent_noop_patch_allowed(tmp_path: Path) -> None:
    """archived 상위를 가진 행의 다른 필드 PATCH는 reports_to를 재전송해도
    200이어야 한다 — 새로 archived를 상위로 지정하는 것만 422."""
    client, store = _env(tmp_path)
    parent = _mk(client, "Parent")
    child = _mk(client, "Child", reports_to=parent["id"])
    # 상위를 archived로
    assert store.archive_agent(parent["id"])

    # 프론트가 현재 reports_to를 함께 재전송하는 PATCH → 200 (회귀)
    r = client.patch(
        f"/api/v1/agents/{child['id']}",
        json={"role": "ic", "reports_to": parent["id"]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "ic" and r.json()["reports_to"] == parent["id"]

    # reports_to 없이 다른 필드만 PATCH → 200
    r = client.patch(f"/api/v1/agents/{child['id']}", json={"role": "senior"})
    assert r.status_code == 200 and r.json()["role"] == "senior"

    # 다른 archived 에이전트를 새 상위로 지정 → 422
    other = _mk(client, "Other")
    assert store.archive_agent(other["id"])
    r = client.patch(f"/api/v1/agents/{child['id']}", json={"reports_to": other["id"]})
    assert r.status_code == 422 and "archived" in r.json()["error"]["message"]

    # reports_to 해제(null)는 archived 상위와 무관하게 허용
    r = client.patch(f"/api/v1/agents/{child['id']}", json={"reports_to": None})
    assert r.status_code == 200 and r.json()["reports_to"] is None


def test_store_reports_to_existence_check(tmp_path: Path) -> None:
    """store 계층 방어 — 존재하지 않는 reports_to/자기참조는 None 반환
    (SQLite PRAGMA foreign_keys 미강제 대비)."""
    _, store = _env(tmp_path)
    a = store.create_agent("A", "claude")
    assert a is not None
    # 미존재 상위 → None
    assert store.create_agent("B", "claude", reports_to="ghost") is None
    # 자기 자신 → None
    assert store.update_agent(a["id"], reports_to=a["id"]) is None
    # 미존재 상위로 PATCH → None (에이전트 자체는 조회 가능)
    assert store.update_agent(a["id"], reports_to="ghost") is None
    assert store.get_agent(a["id"]) is not None
    # 정상 상위는 허용
    b = store.create_agent("B", "claude")
    assert b is not None
    assert store.update_agent(a["id"], reports_to=b["id"]) is not None


def test_agent_name_unique_index_migration(tmp_path: Path) -> None:
    """대소문자만 다른 중복 이름이 있는 기존 DB — Store 오픈 시 최신 행만
    이름을 유지하고 나머지는 id 접미로 보존, lower(name) UNIQUE 인덱스 생성."""
    import sqlite3

    from jobmate.store.db import SCHEMA

    path = str(tmp_path / "dup.db")
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.executemany(
        "INSERT INTO agents(id,name,adapter_type,created_at,updated_at) VALUES(?,?,?,?,?)",
        [
            ("old", "Alpha", "claude", "2024-01-01T00:00:00+00:00", "2024-01-01T00:00:00+00:00"),
            ("new", "alpha", "codex", "2024-01-02T00:00:00+00:00", "2024-01-02T00:00:00+00:00"),
        ],
    )
    conn.commit()
    conn.close()

    s = Store(path)
    old = s.get_agent("old")
    new = s.get_agent("new")
    assert old is not None and new is not None
    # 최신(updated_at 큰 'new')이 'alpha' 이름 유지, 구 행은 id 접미 부여
    assert new["name"] == "alpha"
    assert old["name"].startswith("Alpha·dup·old")
    # 인덱스가 동작 — 동일 ci 이름 생성은 store 레벨에서 거부
    assert s.create_agent("ALPHA", "claude") is None


def test_agents_status_empty_filter_and_export(tmp_path: Path) -> None:
    """GET /agents?status= (빈 문자열)은 필터 미지정과 동일하게 전체 반환.
    export_runs는 agent_id 컬럼을 포함한다."""
    client, store = _env(tmp_path)
    a = _mk(client, "Exp")
    r = client.get("/api/v1/agents?status=")
    assert r.status_code == 200 and len(r.json()["items"]) == 1
    r = client.get("/api/v1/agents?status=%20%20")
    assert r.status_code == 200

    run = store.create_run("dev_review", "v1", {"task": "t"}, trace_id="x", agent_id=a["id"])
    rows = store.export_runs("2000-01-01T00:00:00+00:00")
    assert any(row["id"] == run.id and row["agent_id"] == a["id"] for row in rows)


# ---------------- 통계 필드 ----------------


def test_agent_stats_fields(tmp_path: Path) -> None:
    client, store = _env(tmp_path)
    a = _mk(client, "Worker")
    run = store.create_run("dev_review", "v1", {"task": "t"}, trace_id="x", agent_id=a["id"])
    sr = store.start_stage(run.id, "impl", 1, {}, model="claude")
    store.finish_stage(sr.id, status=StageStatus.SUCCEEDED, tokens_in=1_000_000, tokens_out=0)
    # 미태깅 run은 통계에 포함되지 않아야 함
    store.create_run("dev_review", "v1", {"task": "t2"}, trace_id="y")

    items = client.get("/api/v1/agents").json()["items"]
    assert len(items) == 1
    item = items[0]
    assert item["runs_30d"] == 1
    assert item["cost_30d_usd"] > 0  # claude 1M input tokens = $3.0
    assert item["last_run_at"] is not None

    # status 필터
    assert client.get("/api/v1/agents?status=paused").json()["items"] == []
    client.patch(f"/api/v1/agents/{a['id']}", json={"status": "paused"})
    assert len(client.get("/api/v1/agents?status=paused").json()["items"]) == 1
    assert client.get("/api/v1/agents?status=bogus").status_code == 422


# ---------------- runs.agent_id 태깅 ----------------


def test_run_agent_id_tagging(tmp_path: Path) -> None:
    client, store = _env(tmp_path)
    a = _mk(client, "Runner")
    # 존재하지 않는 agent_id → 422
    r = client.post(
        "/api/v1/runs",
        json={"workflow": "dev_review", "task": "t", "agent_id": "ghost"},
    )
    assert r.status_code == 422
    # 유효 agent_id → run에 태깅
    r = client.post(
        "/api/v1/runs",
        json={"workflow": "dev_review", "task": "t", "agent_id": a["id"]},
    )
    assert r.status_code == 202
    run = store.get_run(r.json()["id"])
    assert run is not None and run.agent_id == a["id"]

    # terminated(구 archived) 에이전트는 신규 run 태깅 불가 → 422
    assert store.archive_agent(a["id"])
    r = client.post(
        "/api/v1/runs",
        json={"workflow": "dev_review", "task": "t2", "agent_id": a["id"]},
    )
    assert r.status_code == 422 and "terminated" in r.json()["error"]["message"]
    # 태그 없는 run은 archived와 무관하게 생성 가능
    r = client.post("/api/v1/runs", json={"workflow": "dev_review", "task": "t3"})
    assert r.status_code == 202


def test_run_coalescing_includes_agent_id(tmp_path: Path) -> None:
    """동일 input·다른 agent_id는 coalesce되지 않고 별도 run —
    같은 agent_id는 coalesce된다(태그 유실 방지)."""
    client, store = _env(tmp_path)
    a = _mk(client, "AgentA")
    b = _mk(client, "AgentB")

    r1 = client.post(
        "/api/v1/runs",
        json={"workflow": "dev_review", "task": "same", "agent_id": a["id"]},
    )
    assert r1.status_code == 202
    id_a = r1.json()["id"]

    # 다른 agent_id → 별도 run (coalesce 아님)
    r2 = client.post(
        "/api/v1/runs",
        json={"workflow": "dev_review", "task": "same", "agent_id": b["id"]},
    )
    assert r2.status_code == 202
    assert r2.json()["id"] != id_a
    run2 = store.get_run(r2.json()["id"])
    assert run2 is not None and run2.agent_id == b["id"]

    # 같은 agent_id + 같은 input → coalesce (run 수 증가 없음)
    r3 = client.post(
        "/api/v1/runs",
        json={"workflow": "dev_review", "task": "same", "agent_id": a["id"]},
    )
    assert r3.status_code == 202
    assert r3.json()["id"] == id_a and r3.json().get("action") == "coalesced"
    assert len(store.list_runs()) == 2


# ---------------- 인증 게이트 ----------------


def test_agents_auth_gating(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, store = _client(tmp_path, monkeypatch)
    member = TestClient(client.app)
    lead = TestClient(client.app)
    _login(store, member, "mem@b.com", "member")
    _login(store, lead, "lead@b.com", "lead")

    # GET도 lead 전용 — AgentOut에 budget/cost 집계가 포함돼 member 차단
    assert member.get("/api/v1/agents").status_code == 403
    assert lead.get("/api/v1/agents").status_code == 200

    # member의 쓰기는 403
    assert (
        member.post("/api/v1/agents", json={"name": "x", "adapter_type": "claude"}).status_code
        == 403
    )

    # lead는 생성/수정 가능 — 삭제(archive=terminated)는 승인 필요(spec 2차 D)
    r = lead.post("/api/v1/agents", json={"name": "L", "adapter_type": "devin"})
    assert r.status_code == 201
    aid = r.json()["id"]
    assert lead.patch(f"/api/v1/agents/{aid}", json={"status": "paused"}).status_code == 200
    assert lead.delete(f"/api/v1/agents/{aid}").status_code == 409
    ar = lead.post(
        "/api/v1/management/approvals",
        json={
            "action_type": "agent_terminate",
            "resource_type": "agent",
            "resource_id": aid,
            "safe_payload": {"agent_id": aid},
        },
    )
    assert ar.status_code == 201
    assert (
        lead.post(
            f"/api/v1/management/approvals/{ar.json()['id']}/decision",
            json={"decision": "approve"},
        ).status_code
        == 200
    )
    assert lead.delete(f"/api/v1/agents/{aid}").status_code == 200


# ---------------- PATCH status 전이 규칙 (H2) ----------------


def test_patch_agent_status_uses_transition_allowlist(tmp_path: Path) -> None:
    """H2 — PATCH /agents/{id}의 status는 전이 allowlist를 거친다.

    레거시 vocab(active/paused/archived)와 canonical 7값을 모두 받고,
    허용되지 않는 전이·terminal 출발은 409, 어휘 밖 값은 422."""
    client, store = _env(tmp_path)
    a = _mk(client, "Dev")  # status=active

    # active→idle는 allowlist에 없음 → 409, 다른 필드도 커밋되지 않음
    r = client.patch(f"/api/v1/agents/{a['id']}", json={"status": "idle", "role": "nope"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "invalid_transition"
    assert store.get_agent(a["id"])["role"] != "nope"

    # canonical 상태 허용 — active→running→idle
    r = client.patch(f"/api/v1/agents/{a['id']}", json={"status": "running"})
    assert r.status_code == 200 and r.json()["status"] == "running"
    r = client.patch(f"/api/v1/agents/{a['id']}", json={"status": "idle"})
    assert r.status_code == 200 and r.json()["status"] == "idle"

    # 레거시 vocab도 canonical로 매핑 — paused 허용(idle→paused)
    r = client.patch(f"/api/v1/agents/{a['id']}", json={"status": "paused"})
    assert r.status_code == 200 and r.json()["status"] == "paused"

    # 어휘 밖 값 → 422
    r = client.patch(f"/api/v1/agents/{a['id']}", json={"status": "bogus"})
    assert r.status_code == 422


def test_patch_agent_cannot_resurrect_terminated(tmp_path: Path) -> None:
    """H2 — terminated(archived) 에이전트는 어떤 상태로도 되돌릴 수 없다."""
    client, store = _env(tmp_path)
    a = _mk(client, "Dev")
    assert store.archive_agent(a["id"])
    assert store.get_agent(a["id"])["status"] == "archived"

    # 부활 시도 → 409
    r = client.patch(f"/api/v1/agents/{a['id']}", json={"status": "active"})
    assert r.status_code == 409
    # terminal→terminal no-op(동일 canonical)은 허용
    r2 = client.patch(f"/api/v1/agents/{a['id']}", json={"status": "archived"})
    assert r2.status_code == 200
    # status 없이 다른 필드 수정은 terminal이어도 허용(레지스트리 메타)
    r3 = client.patch(f"/api/v1/agents/{a['id']}", json={"role": "former"})
    assert r3.status_code == 200 and r3.json()["role"] == "former"


def test_patch_agent_status_transition_emits_event(tmp_path: Path) -> None:
    """H2 — legacy PATCH의 status 변경도 agent_events에 기록된다."""
    client, store = _env(tmp_path)
    a = _mk(client, "Dev")
    r = client.patch(f"/api/v1/agents/{a['id']}", json={"status": "paused"})
    assert r.status_code == 200
    evs = [
        e
        for e in store.list_agent_events(agent_id=a["id"])
        if e["event_type"] == "agent_status_changed"
    ]
    assert evs and evs[0]["detail"].get("to") == "paused"


# ---------------- status 필터 통합 어휘 (L9) ----------------


def test_agents_status_filter_accepts_canonical_vocab(tmp_path: Path) -> None:
    """L9 — GET /agents?status=는 canonical 7값 + archived 별칭을 받는다."""
    client, store = _env(tmp_path)
    a = _mk(client, "IdleOne")
    store.update_agent(a["id"], status="idle")
    b = _mk(client, "Gone")
    assert store.archive_agent(b["id"])

    # canonical 신규 상태 필터
    items = client.get("/api/v1/agents?status=idle").json()["items"]
    assert [i["id"] for i in items] == [a["id"]]
    # terminated/archived 둘 다 canonical terminated 저장행에 매칭
    for flt in ("terminated", "archived"):
        items = client.get(f"/api/v1/agents?status={flt}").json()["items"]
        assert b["id"] in [i["id"] for i in items]
        assert a["id"] not in [i["id"] for i in items]
    # 어휘 밖 → 422
    assert client.get("/api/v1/agents?status=bogus").status_code == 422
