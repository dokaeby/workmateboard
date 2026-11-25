"""팀장(lead) 기능 단위 테스트 — 역할 정규화/관리, 프로젝트 마스터·태깅,
프로젝트 개요/상세, 세션 회고, 리스크, 승인 큐, 성과 비교, 임원 리포트.

네트워크는 사용하지 않고 scripted_llm + 임시 SQLite Store로 검증한다.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jobmate.api.app import create_app
from jobmate.config import Settings
from jobmate.lead import (
    approval_queue,
    detect_risks,
    perf_compare,
    project_detail,
    project_overview,
    session_history,
)
from jobmate.models import RunStatus, StageStatus
from jobmate.reports import build_report
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


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch | None = None):
    """JOBMATE_API_KEY 설정 — 로컬 관리자 폴백을 꺼 세션 role을 실제로 검증."""
    if monkeypatch is not None:
        monkeypatch.setenv("JOBMATE_API_KEY", "test-api-key")
    return _env(tmp_path)


def _login(store: Store, client: TestClient, email: str, role: str = "member") -> None:
    """사용자 생성/역할 지정 후 세션 쿠키를 client에 심는다."""
    u = store.get_user_by_email(email) or store.create_user(
        email, hash_password("password123"), email
    )
    assert u is not None
    store.set_user_role(u["id"], role)
    token = new_session_token()
    store.create_session(u["id"], hash_token(token))
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE, token)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _mk_run(store: Store, workflow: str = "dev_review", project: str = "", **kw) -> object:
    return store.create_run(
        workflow,
        "v1",
        {"task": "t", **({"project": project} if project else {})},
        trace_id="",
        created_by=kw.get("created_by", ""),
    )


def _fail_stage(store: Store, run_id: str, stage_key: str = "impl", model: str = "claude") -> None:
    sr = store.start_stage(run_id, stage_key, 1, {}, model=model)
    store.finish_stage(sr.id, status=StageStatus.FAILED, error="boom", tokens_in=10, tokens_out=5)


# ---------------- 역할 정규화 / 관리 ----------------


def test_store_role_normalization(tmp_path: Path) -> None:
    store = Store(str(tmp_path / "t.db"))
    u = store.create_user("a@b.com", hash_password("password123"))
    assert u is not None
    # DB 기본값 'user' → 읽을 때 'member'로 정규화
    assert store.get_user_by_email("a@b.com")["role"] == "member"
    # 레거시 'user' 저장값도 member로 읽힘
    store.set_user_role(u["id"], "user")
    assert store.get_user_by_email("a@b.com")["role"] == "member"
    store.set_user_role(u["id"], "lead")
    assert store.get_user_by_email("a@b.com")["role"] == "lead"


def test_auth_me_normalized_role(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, store = _client(tmp_path, monkeypatch)
    r = client.post(
        "/api/v1/auth/signup",
        json={"email": "first@b.com", "password": "password123", "agree_terms": True},
    )
    assert r.status_code == 201
    assert r.json()["user"]["role"] == "admin"  # 첫 사용자 자동 admin
    # API 키 설정 시 이메일 인증이 강제된다 — 인증 링크로 세션 발급
    token = store.create_email_verification("first@b.com")
    assert client.get(f"/api/v1/auth/verify?token={token}").status_code == 200
    me = client.get("/api/v1/auth/me").json()["user"]
    assert me["role"] == "admin" and me["email"] == "first@b.com"

    # 두 번째 사용자는 member — 'user' 별칭으로 PATCH해도 member로 저장/표시
    _login(store, client, "second@b.com", "user")
    me = client.get("/api/v1/auth/me").json()["user"]
    assert me["role"] == "member"


def test_patch_role_admin_only_and_audit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, store = _client(tmp_path, monkeypatch)
    admin = TestClient(client.app)
    member = TestClient(client.app)
    leadc = TestClient(client.app)
    _login(store, admin, "admin@b.com", "admin")
    _login(store, member, "mem@b.com", "member")
    _login(store, leadc, "lead@b.com", "lead")
    target = store.get_user_by_email("mem@b.com")
    assert target is not None

    # member/lead는 role 변경 불가
    assert (
        member.patch(f"/api/v1/users/{target['id']}/role", json={"role": "admin"}).status_code
        == 403
    )
    assert (
        leadc.patch(f"/api/v1/users/{target['id']}/role", json={"role": "admin"}).status_code == 403
    )
    # admin은 변경 가능 + 'user' 별칭 → member
    r = admin.patch(f"/api/v1/users/{target['id']}/role", json={"role": "lead"})
    assert r.status_code == 200 and r.json()["role"] == "lead"
    assert store.get_user_by_email("mem@b.com")["role"] == "lead"
    r = admin.patch(f"/api/v1/users/{target['id']}/role", json={"role": "user"})
    assert r.status_code == 200 and r.json()["role"] == "member"
    # 감사 로그 기록
    actions = [a["action"] for a in store.list_audit(50)]
    assert "user_role" in actions


def test_last_admin_guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, store = _client(tmp_path, monkeypatch)
    admin = TestClient(client.app)
    _login(store, admin, "only@b.com", "admin")
    me = store.get_user_by_email("only@b.com")
    assert me is not None
    r = admin.patch(f"/api/v1/users/{me['id']}/role", json={"role": "member"})
    assert r.status_code == 409
    # 두 번째 admin이 있으면 강등 가능
    _login(store, admin, "second@b.com", "admin")
    r = admin.patch(f"/api/v1/users/{me['id']}/role", json={"role": "member"})
    assert r.status_code == 200


# ---------------- 프로젝트 마스터 + 태깅 ----------------


def test_projects_crud(tmp_path: Path) -> None:
    client, store = _env(tmp_path)  # 로컬 모드 → admin 폴백
    r = client.post(
        "/api/v1/projects",
        json={"name": "Alpha", "color": "#fff", "goal": "g", "target_count": 3},
    )
    assert r.status_code == 201
    proj = r.json()
    assert proj["name"] == "Alpha" and proj["target_count"] == 3

    items = client.get("/api/v1/projects").json()["items"]
    assert [p["name"] for p in items] == ["Alpha"]

    # 이름 중복 — case-insensitive + trim
    assert client.post("/api/v1/projects", json={"name": " alpha "}).status_code == 409
    # PATCH
    r = client.patch(
        f"/api/v1/projects/{proj['id']}",
        json={"name": "Alpha2", "target_count": 5, "color": "#000"},
    )
    assert r.status_code == 200
    assert r.json()["name"] == "Alpha2" and r.json()["target_count"] == 5
    # 이름 충돌 PATCH → 404/409 계열
    client.post("/api/v1/projects", json={"name": "Beta"})
    r = client.patch(f"/api/v1/projects/{proj['id']}", json={"name": "beta"})
    assert r.status_code in (404, 409)
    # DELETE
    assert client.delete(f"/api/v1/projects/{proj['id']}").status_code == 200
    assert client.get("/api/v1/projects").json()["items"][0]["name"] == "Beta"


def test_project_tagging_runs_and_sessions(tmp_path: Path) -> None:
    client, store = _env(tmp_path)
    proj = store.create_project("Alpha")
    assert proj is not None

    # run 태깅 — create_run input.project + PATCH
    run = _mk_run(store, project="alpha")  # ci 매칭으로 컬럼 저장
    assert run.project == "alpha"
    r = client.patch(f"/api/v1/runs/{run.id}", json={"project": "Alpha"})
    assert r.status_code == 200
    assert store.get_run(run.id).project == "Alpha"
    # 태그 해제
    client.patch(f"/api/v1/runs/{run.id}", json={"project": ""})
    assert store.get_run(run.id).project == ""

    # 세션 태깅 — PATCH /agent-sessions/{key} (tool 미지정)
    store.upsert_agent_session("claude", "sess-1", "working", project="old")
    r = client.patch("/api/v1/agent-sessions/sess-1", json={"project": "Alpha"})
    assert r.status_code == 200
    sess = store.get_agent_session("claude", "sess-1")
    assert sess["project"] == "Alpha"
    # 알 수 없는 세션 → 404
    assert client.patch("/api/v1/agent-sessions/nope", json={"project": "x"}).status_code == 404


def test_project_overview_and_detail(tmp_path: Path) -> None:
    client, store = _env(tmp_path)
    proj = store.create_project("Alpha", color="#123456")
    assert proj is not None
    store.upsert_agent_session(
        "claude", "s1", "working", project="Alpha", tokens_in=1000, tokens_out=500
    )
    store.upsert_agent_session("codex", "s2", "ended", project="")  # 기타 버킷
    run = _mk_run(store, project="Alpha")
    store.update_run(run.id, status=RunStatus.COMPLETED)

    ov = client.get("/api/v1/projects/overview").json()["items"]
    names = {i["name"]: i for i in ov}
    assert "Alpha" in names and "(기타)" in names
    alpha = names["Alpha"]
    assert alpha["session_count"] == 1 and alpha["run_count"] == 1
    assert alpha["success_rate"] == 100.0
    assert alpha["color"] == "#123456"
    assert alpha["total_cost_usd"] > 0
    assert alpha["id"] == proj["id"]
    other = names["(기타)"]
    assert other["session_count"] == 1 and other["run_count"] == 0

    # 자유 태그(마스터 없음)도 그룹으로 나타남
    store.upsert_agent_session("gemini", "s3", "rest", project="FreeTag")
    ov = client.get("/api/v1/projects/overview").json()["items"]
    assert any(i["name"] == "FreeTag" for i in ov)

    # detail — id / 이름 / tag: / untagged 모두 허용
    d = client.get(f"/api/v1/projects/{proj['id']}/detail").json()
    assert d["project"]["name"] == "Alpha"
    assert len(d["sessions"]) == 1 and len(d["runs"]) == 1
    assert "timeline" in d and "stage_failures" in d and "cost_by_day" in d
    d2 = client.get("/api/v1/projects/untagged/detail")
    assert d2.status_code == 404 or d2.json()["project"]["name"] == "(기타)"
    assert client.get("/api/v1/projects/nonexistent/detail").status_code == 404


def test_project_detail_direct(tmp_path: Path) -> None:
    store = Store(str(tmp_path / "t.db"))
    store.create_project("Alpha")
    store.upsert_agent_session("claude", "s1", "working", project="alpha")
    store.upsert_agent_session("claude", "s2", "working", project="alpha")
    store.upsert_agent_session("claude", "s3", "working", project="")
    d = project_detail(store, "alpha")  # 이름 ci 조회
    assert d is not None
    assert d["project"]["name"] == "Alpha"
    assert len(d["sessions"]) == 2


# ---------------- 세션 회고 ----------------


def test_session_history_composition(tmp_path: Path) -> None:
    client, store = _env(tmp_path)
    store.upsert_agent_session(
        "claude",
        "sess-h",
        "working",
        project="Alpha",
        doing="코드 작성 중",
        tokens_in=12000,
        tokens_out=3000,
        artifacts=["src/a.py", "src/b.py"],
    )
    # 전이 이벤트 추가 — decision → working → ended
    store.upsert_agent_session("claude", "sess-h", "decision", ask="진행할까요?")
    store.upsert_agent_session("claude", "sess-h", "working", doing="재개")
    store.upsert_agent_session("claude", "sess-h", "ended", ended_summary="완료됨")

    r = client.get("/api/v1/agent-sessions/claude/sess-h/history")
    assert r.status_code == 200
    body = r.json()
    assert body["session"]["session_key"] == "sess-h"
    assert body["session"]["health"] == "ended"
    assert body["events"], "events 호환 필드 유지"
    assert body["timeline"], "timeline 존재"
    stats = body["stats"]
    assert stats["state_counts"].get("working", 0) >= 1
    assert stats["decisions"] >= 1
    assert stats["tokens_in"] == 12000 and stats["tokens_out"] == 3000
    assert stats["est_cost_usd"] is not None
    assert stats["total_seconds"] >= 0
    assert "src/a.py" in body["files_touched"]
    assert "세션" in body["summary"] and "working" in body["summary"]
    assert body["ended_summary"] == "완료됨"

    # tool 미지정 경로도 동작
    r2 = client.get("/api/v1/agent-sessions/sess-h/history")
    assert r2.status_code == 200 and r2.json()["session"]["tool"] == "claude"

    # store 레벨 직접 호출
    hist = session_history(store, "claude", "sess-h")
    assert hist is not None and hist["summary"]


# ---------------- 리스크 ----------------


def test_risks_stage_failures(tmp_path: Path) -> None:
    store = Store(str(tmp_path / "t.db"))
    for _ in range(2):
        run = _mk_run(store)
        _fail_stage(store, run.id, "impl")
    risks = detect_risks(store)["items"]
    sf = [r for r in risks if r["type"] == "stage_failure"]
    assert sf and sf[0]["severity"] == "medium"  # 2회 → medium
    assert sf[0]["ref_kind"] == "run"

    # 3번째 run 실패 → high
    run = _mk_run(store)
    _fail_stage(store, run.id, "impl")
    sf = [r for r in detect_risks(store)["items"] if r["type"] == "stage_failure"]
    assert sf[0]["severity"] == "high"


def test_risks_stale_approval(tmp_path: Path) -> None:
    store = Store(str(tmp_path / "t.db"))
    run = _mk_run(store)
    store.update_run(run.id, status=RunStatus.WAITING_APPROVAL)
    old = _iso(datetime.now(UTC) - timedelta(hours=5))
    store._conn.execute("UPDATE runs SET updated_at=? WHERE id=?", (old, run.id))
    risks = detect_risks(store)["items"]
    sa = [r for r in risks if r["type"] == "stale_approval"]
    assert sa and sa[0]["severity"] == "medium"
    # 25시간 → high
    older = _iso(datetime.now(UTC) - timedelta(hours=25))
    store._conn.execute("UPDATE runs SET updated_at=? WHERE id=?", (older, run.id))
    sa = [r for r in detect_risks(store)["items"] if r["type"] == "stale_approval"]
    assert sa[0]["severity"] == "high"


def test_risks_idle_session(tmp_path: Path) -> None:
    store = Store(str(tmp_path / "t.db"))
    store.upsert_agent_session("claude", "idle-1", "working", doing="멈춤")
    old = _iso(datetime.now(UTC) - timedelta(minutes=10))
    store._conn.execute(
        "UPDATE agent_sessions SET last_event_at=?, updated_at=? WHERE session_key=?",
        (old, old, "idle-1"),
    )
    risks = detect_risks(store)["items"]
    idle = [r for r in risks if r["type"] == "idle_session"]
    assert idle and idle[0]["severity"] == "medium"


def test_risks_cost_spike_and_budget(tmp_path: Path) -> None:
    store = Store(str(tmp_path / "t.db"))
    # 오늘 비용 급증 — 7일간 $0.01/일, 오늘 $5 → 3x avg & >$1
    today = datetime.now(UTC).date().isoformat()
    for d in range(1, 8):
        day = (datetime.now(UTC).date() - timedelta(days=d)).isoformat()
        store._conn.execute(
            "INSERT INTO llm_usage(id,model,tokens_in,tokens_out,created_at) VALUES(?,?,?,?,?)",
            (f"u{d}", "claude", 1000, 1000, f"{day}T01:00:00+00:00"),
        )
    store._conn.execute(
        "INSERT INTO llm_usage(id,model,tokens_in,tokens_out,created_at) VALUES(?,?,?,?,?)",
        ("utoday", "claude", 3_000_000, 3_000_000, f"{today}T01:00:00+00:00"),
    )
    risks = detect_risks(store)["items"]
    cs = [r for r in risks if r["type"] == "cost_spike"]
    assert cs, "cost_spike 리스크 누락"
    # 월 예산 초과
    store.set_state("prefs", {"monthly_budget_usd": 0.001})
    risks = detect_risks(store)["items"]
    assert any(r["type"] == "budget" for r in risks)


def test_risks_trigger_failures(tmp_path: Path) -> None:
    store = Store(str(tmp_path / "t.db"))
    t = store.create_trigger({"tool": "claude"}, {"type": "report"})
    store.mark_trigger_error(t["id"], "boom")
    risks = detect_risks(store)["items"]
    tf = [r for r in risks if r["type"] == "trigger_failure"]
    assert tf and tf[0]["ref_kind"] == "trigger"


def test_risks_sorted_and_capped(tmp_path: Path) -> None:
    store = Store(str(tmp_path / "t.db"))
    # 다수 세션 idle → capped
    for i in range(60):
        store.upsert_agent_session("claude", f"s{i}", "rest")
        store._conn.execute(
            "UPDATE agent_sessions SET last_event_at=? WHERE session_key=?",
            (_iso(datetime.now(UTC) - timedelta(days=2)), f"s{i}"),
        )
    items = detect_risks(store)["items"]
    assert len(items) <= 50
    sev = [r["severity"] for r in items]
    assert sev == sorted(sev, key={"high": 0, "medium": 1, "low": 2}.get)


# ---------------- 승인 큐 ----------------


def test_approval_queue(tmp_path: Path) -> None:
    store = Store(str(tmp_path / "t.db"))
    run = _mk_run(store, project="Alpha")
    store.update_run(run.id, status=RunStatus.WAITING_APPROVAL)
    store.upsert_agent_session("claude", "dec-1", "decision", ask="승인?")
    store.upsert_agent_session("codex", "wrk-1", "working")
    trig = store.create_trigger({}, {"type": "run_workflow", "workflow": "dev_review"})
    store.mark_trigger_error(trig["id"], "err")

    items = approval_queue(store)["items"]
    kinds = {i["kind"] for i in items}
    assert kinds == {"run", "session", "trigger"}
    run_item = next(i for i in items if i["kind"] == "run")
    assert run_item["id"] == run.id and run_item["project"] == "Alpha"
    sess_item = next(i for i in items if i["kind"] == "session")
    assert sess_item["id"] == "claude:dec-1"
    # 가장 오래 기다린 순 정렬
    waits = [i["waiting_since"] for i in items]
    assert waits == sorted(waits)


def test_approval_queue_api(tmp_path: Path) -> None:
    client, store = _env(tmp_path)
    run = _mk_run(store)
    store.update_run(run.id, status=RunStatus.WAITING_REVIEW)
    r = client.get("/api/v1/approvals")
    assert r.status_code == 200
    assert any(i["kind"] == "run" for i in r.json()["items"])


# ---------------- 성과 비교 ----------------


def test_perf_compare_grouping(tmp_path: Path) -> None:
    store = Store(str(tmp_path / "t.db"))
    # model 그룹 — claude 성공 1, codex 실패 1
    r1 = _mk_run(store, created_by="user@x.com")
    _fail_stage(store, r1.id, "impl", model="codex")
    store.update_run(r1.id, status=RunStatus.FAILED)
    r2 = _mk_run(store)
    sr = store.start_stage(r2.id, "impl", 1, {}, model="claude")
    store.finish_stage(sr.id, status=StageStatus.SUCCEEDED, tokens_in=100, tokens_out=50)
    store.update_run(r2.id, status=RunStatus.COMPLETED)
    store.upsert_agent_session("claude", "s1", "ended")
    store.upsert_agent_session("claude", "s2", "working")
    store.upsert_agent_session("codex", "s3", "ended")

    for gb in ("model", "agent", "tool"):
        out = perf_compare(store, group_by=gb)
        assert out["group_by"] == gb and out["items"]
        for item in out["items"]:
            assert item["run_count"] >= 1
            assert "success_rate" in item and "failures_by_stage" in item
        assert out["sessions"]["items"]
    tools = {i["tool"]: i for i in perf_compare(store)["sessions"]["items"]}
    assert tools["claude"]["session_count"] == 2
    assert tools["claude"]["ended"] == 1 and tools["claude"]["active"] == 1

    # workflow 필터
    out = perf_compare(store, workflow="dev_review", group_by="model")
    assert out["workflow"] == "dev_review"


def test_perf_compare_api(tmp_path: Path) -> None:
    client, store = _env(tmp_path)
    _mk_run(store)
    r = client.get("/api/v1/perf/compare?group_by=model")
    assert r.status_code == 200
    assert r.json()["items"]
    assert client.get("/api/v1/perf/compare?group_by=bad").status_code == 422


# ---------------- 임원 리포트 ----------------


def test_exec_reports(tmp_path: Path) -> None:
    store = Store(str(tmp_path / "t.db"))
    run = _mk_run(store)
    store.update_run(run.id, status=RunStatus.COMPLETED)
    store.upsert_agent_session("claude", "s1", "ended")

    for kind in ("exec_daily", "exec_weekly"):
        content = build_report(store, kind)
        assert content["kind"] == kind
        assert "narrative" in content and "highlights" in content
        assert content["runs"]["total"] >= 1
        assert content["sessions"]["total_sessions"] >= 1
        assert "approvals" in content and "risks" in content
        assert content["llm"]["total_cost_usd"] >= 0
        # 인박스/알림 호환 키
        assert "pending" in content["approvals"]

    # 기존 daily/weekly도 여전히 동작
    assert build_report(store, "daily")["kind"] == "daily"
    assert build_report(store, "weekly")["kind"] == "weekly"


def test_exec_report_api(tmp_path: Path) -> None:
    client, store = _env(tmp_path)
    _mk_run(store)
    # 온디맨드 생성 (저장 없이)
    r = client.get("/api/v1/reports?kind=exec_weekly")
    assert r.status_code == 200
    assert r.json()["kind"] == "exec_weekly"
    r = client.get("/api/v1/reports/latest?kind=exec_daily")
    assert r.status_code == 200 and r.json()["kind"] == "exec_daily"
    # 저장 생성 — 로컬 모드는 admin 폴백으로 허용
    r = client.post("/api/v1/reports/generate", json={"kind": "exec_daily"})
    assert r.status_code == 201
    # 리포트 목록/인박스에 나타남
    latest = client.get("/api/v1/reports/latest?kind=exec_daily").json()
    assert latest["content"]["kind"] == "exec_daily"


def test_project_overview_direct(tmp_path: Path) -> None:
    """lead.project_overview 직접 호출 — 빈 스토어에서도 (기타) 버킷."""
    store = Store(str(tmp_path / "t.db"))
    out = project_overview(store)
    assert "items" in out
