"""agent_bridge 스캐너/Bridge diff 로직 테스트.

실제 ~/.claude 등은 읽지 않고 tmp_path fixture + root 파라미터 주입만 사용.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

import httpx
import pytest

from jobmate import agent_bridge as ab

NOW = 1_700_000_000.0


def _touch(path: Path, age: float) -> None:
    ts = NOW - age
    os.utime(path, (ts, ts))


def _claude_entry(etype: str, content, **kw):
    e = {"type": etype, "message": {"role": etype, "content": content}}
    e.update(kw)
    return e


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# claude
# ---------------------------------------------------------------------------


def test_scan_claude_working_decision(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    proj = root / "-Users-sec-Documents-foo"
    f = proj / "sess-1.jsonl"
    entries = [
        _claude_entry("user", "대시보드 버그 고쳐줘", cwd="/Users/sec/Documents/foo"),
        _claude_entry(
            "assistant",
            [
                {"type": "thinking", "thinking": "..."},
                {"type": "text", "text": "원인을 찾았습니다."},
                {"type": "tool_use", "name": "Read", "input": {"file_path": "a.py"}},
            ],
        ),
    ]
    _write_jsonl(f, entries)
    _touch(f, 10)

    states = ab.scan_claude(root, NOW)
    assert len(states) == 1
    st = states[0]
    assert st.tool == "claude"
    assert st.session_key == "sess-1"
    assert st.project == "foo"
    assert st.title == "대시보드 버그 고쳐줘"
    assert st.status == "decision"
    assert st.ask == "Read"
    assert st.doing == "원인을 찾았습니다."


def test_scan_claude_rest_and_title_fallback(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    f = root / "-tmp-proj" / "s2.jsonl"
    _write_jsonl(
        f,
        [
            _claude_entry("user", "<command-name>/help</command-name>"),
            _claude_entry("user", "두번째 프롬프트"),
            _claude_entry("assistant", [{"type": "text", "text": "done"}]),
            _claude_entry("user", [{"type": "tool_result", "content": "ok"}]),
        ],
    )
    _touch(f, 120)
    st = ab.scan_claude(root, NOW)[0]
    assert st.status == "rest"
    assert st.title == "두번째 프롬프트"
    assert st.project == "proj"


def test_scan_claude_ended_filtered(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    f = root / "-tmp" / "old.jsonl"
    _write_jsonl(f, [_claude_entry("user", "hi")])
    _touch(f, ab.ENDED_SECONDS + 10)
    assert ab.scan_claude(root, NOW) == []


def test_scan_claude_missing_root(tmp_path: Path) -> None:
    assert ab.scan_claude(tmp_path / "nope", NOW) == []


# ---------------------------------------------------------------------------
# codex
# ---------------------------------------------------------------------------


def _codex_line(ts: str, ordinal: int, etype: str, payload: dict) -> str:
    return json.dumps(
        {"timestamp": ts, "ordinal": ordinal, "type": etype, "payload": payload},
        ensure_ascii=False,
    )


@pytest.fixture()
def codex_root(tmp_path: Path) -> Path:
    import shutil

    if shutil.which("zstd") is None:
        pytest.skip("zstd CLI not installed")
    root = tmp_path / "sessions" / "2026" / "11" / "03"
    root.mkdir(parents=True)
    return root


def _make_zst(root: Path, name: str, lines: list[str]) -> Path:
    import shutil
    import subprocess

    zstd = shutil.which("zstd")
    assert zstd is not None
    raw = root / (name + ".tmp")
    raw.write_text("\n".join(lines) + "\n")
    out = root / (name + ".jsonl.zst")
    subprocess.run(  # noqa: S603 - 테스트 fixture 생성용 고정 argv
        [zstd, "-q", "-f", str(raw), "-o", str(out)], check=True
    )
    raw.unlink()
    return out


def test_scan_codex_decision(codex_root: Path) -> None:
    uuid = "01a1a1bf-486f-7823-99a2-bb67f487b663"
    lines = [
        _codex_line(
            "2026-11-03T14:10:30Z",
            0,
            "session_meta",
            {"cwd": "/Users/sec/work/proj-x", "session_id": uuid},
        ),
        _codex_line(
            "2026-11-03T14:11:00Z",
            1,
            "response_item",
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "codex 작업 시작"}],
            },
        ),
        _codex_line(
            "2026-11-03T14:11:30Z",
            2,
            "response_item",
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "파일을 수정합니다"}],
            },
        ),
        _codex_line(
            "2026-11-03T14:11:40Z",
            3,
            "response_item",
            {"type": "function_call", "name": "exec_command", "call_id": "c1"},
        ),
    ]
    f = _make_zst(codex_root, f"rollout-2026-11-03T23-10-30-{uuid}", lines)
    _touch(f, 5)

    states = ab.scan_codex(codex_root.parent.parent.parent, NOW)
    assert len(states) == 1
    st = states[0]
    assert st.tool == "codex"
    assert st.session_key == uuid
    assert st.project == "proj-x"
    assert st.title == "codex 작업 시작"
    assert st.status == "decision"
    assert st.ask == "exec_command"
    assert st.doing == "파일을 수정합니다"


def test_scan_codex_plain_jsonl(codex_root: Path) -> None:
    """codex exec 등은 압축 없이 .jsonl로 rollout을 쓴다."""
    uuid = "01a203d2-5643-7681-a947-26634175eec6"
    lines = [
        _codex_line(
            "2026-11-03T14:10:30Z",
            0,
            "session_meta",
            {"cwd": "/Users/sec/work/jobmate-demo", "session_id": uuid},
        ),
        _codex_line(
            "2026-11-03T14:11:00Z",
            1,
            "response_item",
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "demo 작업"}],
            },
        ),
        _codex_line(
            "2026-11-03T14:11:30Z",
            2,
            "event_msg",
            {"type": "task_complete", "last_agent_message": "완료"},
        ),
    ]
    f = codex_root / f"rollout-2026-11-03T23-10-30-{uuid}.jsonl"
    f.write_text("\n".join(lines) + "\n")
    _touch(f, 60)

    states = ab.scan_codex(codex_root.parent.parent.parent, NOW)
    assert len(states) == 1
    st = states[0]
    assert st.tool == "codex"
    assert st.session_key == uuid
    assert st.project == "jobmate-demo"
    assert st.title == "demo 작업"
    assert st.status == "rest"


def test_scan_codex_task_complete_rest(codex_root: Path) -> None:
    uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    lines = [
        _codex_line("2026-11-03T14:10:30Z", 0, "session_meta", {"cwd": "/home/u/proj"}),
        _codex_line(
            "2026-11-03T14:11:00Z",
            1,
            "event_msg",
            {"type": "task_complete", "last_agent_message": "끝"},
        ),
    ]
    f = _make_zst(codex_root, f"rollout-2026-11-03T23-10-30-{uuid}", lines)
    _touch(f, 30)
    st = ab.scan_codex(codex_root.parent.parent.parent, NOW)[0]
    assert st.status == "rest"


# ---------------------------------------------------------------------------
# gemini
# ---------------------------------------------------------------------------


def test_scan_gemini_mtime_status(tmp_path: Path) -> None:
    root = tmp_path / "conversations"
    root.mkdir()
    db = root / "abc-123.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE steps (idx integer, step_type integer, status integer, "
        "step_payload blob, PRIMARY KEY (idx))"
    )
    con.execute("INSERT INTO steps VALUES (0, 15, 3, ?)", (b"\x00\x01hello world step",))
    con.commit()
    con.close()
    _touch(db, 10)

    st = ab.scan_gemini(root, NOW)[0]
    assert st.tool == "gemini"
    assert st.session_key == "abc-123"
    assert st.status == "working"
    # blob에서 printable run 추출 — degrade 허용이지만 값은 문자열
    assert isinstance(st.doing, str) and st.doing


def test_scan_gemini_rest_and_missing(tmp_path: Path) -> None:
    root = tmp_path / "conversations"
    root.mkdir()
    db = root / "old.db"
    sqlite3.connect(db).close()
    _touch(db, 200)
    st = ab.scan_gemini(root, NOW)[0]
    assert st.status == "rest"
    assert ab.scan_gemini(tmp_path / "none", NOW) == []


# ---------------------------------------------------------------------------
# devin
# ---------------------------------------------------------------------------


def _devin_root(tmp_path: Path) -> Path:
    root = tmp_path / "cli"
    (root / "transcripts").mkdir(parents=True)
    (root / "session_locks").mkdir(parents=True)
    return root


def test_scan_devin_working(tmp_path: Path) -> None:
    root = _devin_root(tmp_path)
    t = root / "transcripts" / "camp-seat.json"
    (root / "session_locks" / "camp-seat.lock").write_text("1234")
    data = {
        "schema_version": "1",
        "session_id": "s1",
        "steps": [
            {"step_id": 1, "source": "user", "message": "devin 작업 해줘"},
            {
                "step_id": 2,
                "source": "agent",
                "message": "코드를 수정합니다",
                "tool_calls": [{"function_name": "exec"}],
                "observation": {"results": []},
            },
        ],
    }
    t.write_text(json.dumps(data))
    _touch(t, 5)

    st = ab.scan_devin(root, NOW)[0]
    assert st.tool == "devin"
    assert st.session_key == "camp-seat"
    assert st.title == "devin 작업 해줘"
    assert st.status == "working"
    assert st.doing == "코드를 수정합니다"


def test_scan_devin_decision_and_no_lock(tmp_path: Path) -> None:
    root = _devin_root(tmp_path)
    # lock 없음 → 세션으로 취급 안 함
    t1 = root / "transcripts" / "no-lock.json"
    t1.write_text(json.dumps({"steps": [{"source": "user", "message": "hi"}]}))
    _touch(t1, 5)
    # lock 있음 + 미해결 tool_call → decision
    t2 = root / "transcripts" / "pending.json"
    (root / "session_locks" / "pending.lock").write_text("1")
    t2.write_text(
        json.dumps(
            {
                "steps": [
                    {"source": "user", "message": "q"},
                    {
                        "source": "agent",
                        "message": "",
                        "tool_calls": [{"function_name": "ask_user_question"}],
                        "observation": None,
                    },
                ]
            }
        )
    )
    _touch(t2, 5)

    states = ab.scan_devin(root, NOW)
    assert [s.session_key for s in states] == ["pending"]
    assert states[0].status == "decision"
    assert states[0].ask == "ask_user_question"


def test_scan_codex_catalog_desktop_threads(tmp_path: Path) -> None:
    """~/.codex/sqlite/codex.db local_thread_catalog — 데스크톱/IDE 스레드."""
    root = tmp_path / "sessions"
    root.mkdir()
    sqldir = tmp_path / "sqlite"
    sqldir.mkdir()
    db = sqldir / "codex.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE local_thread_catalog (host_id TEXT, thread_id TEXT,"
        " display_title TEXT, source_created_at REAL, source_updated_at REAL,"
        " cwd TEXT, source_kind TEXT, source_detail TEXT,"
        " model_provider TEXT, git_branch TEXT, observation_sequence INTEGER,"
        " missing_candidate INTEGER, thread_source TEXT,"
        " source_recency_at REAL, pending_observed_title INTEGER,"
        " project_id TEXT, conversation_origin TEXT,"
        " trial_conversation_type TEXT, chatgpt_async_status INTEGER)"
    )
    con.execute(
        "INSERT INTO local_thread_catalog VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "h",
            "thread-1",
            "데스크톱 스레드",
            0,
            0,
            "/Users/sec/proj",
            "vscode",
            None,
            None,
            None,
            0,
            0,
            None,
            NOW - 10,
            0,
            None,
            None,
            None,
            None,
        ),
    )
    con.execute(
        "INSERT INTO local_thread_catalog VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "h",
            "thread-old",
            "오래된 스레드",
            0,
            0,
            "/x",
            "chatgpt",
            None,
            None,
            None,
            0,
            0,
            None,
            NOW - 700,
            0,
            None,
            None,
            None,
            None,
        ),
    )
    con.commit()
    con.close()

    states = ab.scan_codex(root, NOW)
    assert len(states) == 1
    st = states[0]
    assert st.tool == "codex"
    assert st.session_key == "thread-1"
    assert st.project == "proj"
    assert st.title == "데스크톱 스레드"
    assert st.status == "working"


def test_scan_gemini_presence_lock_merges(tmp_path: Path) -> None:
    """presence/*.lock — db가 오래돼도 lock이 최근이면 활성 세션으로 병합."""
    root = tmp_path / "conversations"
    root.mkdir()
    pdir = tmp_path / "presence"
    pdir.mkdir()
    # 오래된 db + 최신 lock → 같은 session_key로 상태만 갱신
    db = root / "sess-x.db"
    sqlite3.connect(db).close()
    _touch(db, 500)
    lock = pdir / "sess-x.lock"
    lock.write_text("1")
    _touch(lock, 10)
    # lock만 있는 세션
    lock2 = pdir / "sess-y.lock"
    lock2.write_text("1")
    _touch(lock2, 30)

    states = {s.session_key: s for s in ab.scan_gemini(root, NOW)}
    assert states["sess-x"].status == "working"
    assert states["sess-y"].status == "working"
    assert states["sess-y"].doing == "gemini session"


def test_scan_devin_sessions_db(tmp_path: Path) -> None:
    """sessions.db — transcript 없는 데스크톱 세션도 최근 활동으로 수집."""
    root = _devin_root(tmp_path)
    db = root / "sessions.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, working_directory TEXT,"
        " backend_type TEXT, model TEXT, agent_mode TEXT, created_at INTEGER,"
        " last_activity_at INTEGER, title TEXT, main_chain_id INTEGER,"
        " shell_last_seen_index INTEGER, cogs_json TEXT, workspace_dirs TEXT,"
        " hidden INTEGER, metadata TEXT)"
    )
    con.execute(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "desk-sess",
            "/Users/sec/proj",
            "local",
            "m",
            "a",
            NOW - 100,
            NOW - 10,
            "데스크톱 세션",
            None,
            0,
            None,
            None,
            0,
            None,
        ),
    )
    con.execute(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "hidden-sess",
            "/x",
            "local",
            "m",
            "a",
            NOW - 100,
            NOW - 10,
            "숨김",
            None,
            0,
            None,
            None,
            1,
            None,
        ),
    )
    con.commit()
    con.close()

    states = ab.scan_devin(root, NOW)
    assert len(states) == 1
    st = states[0]
    assert st.session_key == "desk-sess"
    assert st.project == "proj"
    assert st.title == "데스크톱 세션"
    assert st.status == "working"


# ---------------------------------------------------------------------------
# Bridge diff / heartbeat / ended
# ---------------------------------------------------------------------------


def _fake_state(tool: str, key: str, **kw) -> ab.SessionState:
    base = dict(
        tool=tool,
        session_key=key,
        project="p",
        title="t",
        status="working",
        doing="d",
        ask="",
        event_at="2026-01-01T00:00:00.000Z",
        path="/data/x",
    )
    base.update(kw)
    return ab.SessionState(**base)


class _FakeScanner:
    def __init__(self, states: list[ab.SessionState]) -> None:
        self.states = states

    def __call__(self, root: Path, now: float) -> list[ab.SessionState]:
        return list(self.states)


def _bridge_with(states: list[ab.SessionState], **kw) -> tuple[ab.Bridge, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    bridge = ab.Bridge(
        "http://testserver",
        api_key="k",
        scanners={"fake": _FakeScanner(states)},
        roots={"fake": Path("/data")},
        client=client,
        **kw,
    )
    return bridge, requests


def test_bridge_push_diff_and_heartbeat() -> None:
    st = _fake_state("claude", "s1")
    bridge, requests = _bridge_with([st], heartbeat_seconds=60)
    t = [time.time()]
    bridge._clock = lambda: t[0]  # noqa: SLF001 - 테스트용 가변 시계

    c = bridge.push()
    assert c["pushed"] == 1 and len(requests) == 1
    body = json.loads(requests[0].content)
    assert body["tool"] == "claude" and body["session_key"] == "s1"
    assert requests[0].headers["authorization"] == "Bearer k"

    # 변화 없음 + heartbeat 미경과 → skip
    t[0] += 10
    c = bridge.push()
    assert c["skipped"] == 1 and len(requests) == 1

    # doing 변경 → push
    st2 = _fake_state("claude", "s1", doing="new")
    bridge._scanners["fake"] = _FakeScanner([st2])  # noqa: SLF001
    c = bridge.push()
    assert c["pushed"] == 1 and len(requests) == 2

    # heartbeat 경과 → push (내용 동일)
    t[0] += 61
    c = bridge.push()
    assert c["pushed"] == 1 and len(requests) == 3


def test_bridge_ended_on_disappear() -> None:
    st = _fake_state("claude", "s1")
    bridge, requests = _bridge_with([st])
    bridge.push()
    assert len(requests) == 1

    # 세션 사라짐 → ended 1회 push
    bridge._scanners["fake"] = _FakeScanner([])  # noqa: SLF001
    c = bridge.push()
    assert c["ended"] == 1 and len(requests) == 2
    body = json.loads(requests[1].content)
    assert body["status"] == "ended"

    # 다시 push해도 ended 재전송 없음
    c = bridge.push()
    assert c["ended"] == 0 and len(requests) == 2


def test_scan_all_isolates_scanner_failure() -> None:
    def bad(root: Path, now: float) -> list[ab.SessionState]:
        raise RuntimeError("boom")

    bridge, _ = _bridge_with([])
    bridge._scanners = {"bad": bad}  # noqa: SLF001
    assert bridge.scan_all() == {}


def test_status_values() -> None:
    assert set(ab._STATUS_VALUES) == {"working", "decision", "rest", "ended"}
