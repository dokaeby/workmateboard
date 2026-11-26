"""task comments/artifacts 단위 테스트 — spec 3차 B.

store CRUD + services.task_collab 검증(validate_comment_fields,
is_allowed_artifact_uri, validate_artifact_fields, task_runs_view)을
임시 SQLite Store로 검증한다. 네트워크·LLM 없음.
"""

from __future__ import annotations

from pathlib import Path

from jobmate.services import task_collab as collab
from jobmate.store.db import Store


def _store(tmp_path: Path) -> Store:
    return Store(str(tmp_path / "t.db"))


def _task(store: Store, title: str = "t") -> dict:
    task = store.create_task(title)
    assert task is not None
    return task


# ---- comments ----


def test_comment_crud(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task = _task(store)
    c = store.create_task_comment(task["id"], author_type="human", author_id="u1", body="첫 댓글")
    assert c is not None
    assert c["task_id"] == task["id"]
    assert c["author_type"] == "human" and c["author_id"] == "u1"
    assert c["content_type"] == "text" and c["edited_at"] is None

    got = store.get_task_comment(c["id"])
    assert got is not None and got["body"] == "첫 댓글"

    c2 = store.create_task_comment(
        task["id"],
        author_type="agent",
        author_id="dev",
        body="md",
        content_type="markdown",
    )
    assert c2 is not None and c2["content_type"] == "markdown"

    items = store.list_task_comments(task["id"])
    assert [i["id"] for i in items] == [c["id"], c2["id"]]

    upd = store.update_task_comment(c["id"], body="수정")
    assert upd is not None and upd["body"] == "수정"
    assert upd["edited_at"] is not None
    # 다른 task의 댓글 id로는 조회 불가(고아 방지 아님 — id는 전역 유일)
    assert store.update_task_comment("ghost", body="x") is None


def test_comment_store_sanitizes_vocab(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task = _task(store)
    c = store.create_task_comment(
        task["id"], author_type="bogus", author_id="x" * 300, body="  ok  "
    )
    assert c is not None
    assert c["author_type"] == "system"  # 미지 어휘 → system 폴백
    assert len(c["author_id"]) <= 200
    assert c["body"] == "ok"
    # 빈 body → None
    assert store.create_task_comment(task["id"], author_type="human", body="   ") is None


def test_validate_comment_fields() -> None:
    assert collab.validate_comment_fields({"body": ""}) is not None
    assert collab.validate_comment_fields({"body": "   "}) is not None
    assert collab.validate_comment_fields({"body": "x" * (collab.COMMENT_BODY_MAX + 1)}) is not None
    assert collab.validate_comment_fields({"body": "ok", "content_type": "html"}) is not None
    assert collab.validate_comment_fields({"body": "ok"}) is None
    assert collab.validate_comment_fields({"body": "ok", "content_type": "Markdown"}) is None


# ---- artifact URI 규칙 ----


def test_artifact_uri_allowed() -> None:
    ok = [
        "https://example.com/reports/plan.pdf",
        "https://example.com/a?b=c#d",
        "docs/spec.md",
        "src/jobmate/api/app.py",
        "./relative/path.txt",
        "a/b/c/d.json",
    ]
    for u in ok:
        assert collab.is_allowed_artifact_uri(u), u


def test_artifact_uri_rejected() -> None:
    bad = [
        "http://example.com/x",  # http는 거부 — https만
        "file:///etc/passwd",
        "file://host/share",
        "javascript:alert(1)",
        "data:text/html,<h1>x</h1>",
        "ftp://example.com/f",
        "//evil.example.com/share",  # UNC/프로토콜 상대
        "\\\\server\\share",  # UNC 백슬래시
        "/etc/passwd",  # 절대 경로
        "~/secret.txt",  # 홈 경로
        "../outside.txt",  # 상위 탈출
        "a/../../b",
        "a//b/../c",  # '..' 세그먼트 포함
        "C:\\Windows\\system32",
        "path\twith\ttabs",
        "path\nwith\nnewline",
        "   ",
        "",
        "https://",  # netloc 없음
        "https://user:pass@example.com/x",  # 자격증명 포함 URL 거부
    ]
    for u in bad:
        assert not collab.is_allowed_artifact_uri(u), u


def test_artifact_crud_and_validation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task = _task(store)
    tid = task["id"]

    # run 링크 — artifact run_id 검증용
    run = store.create_run("dev_review", "1", {"task": "t"}, trace_id="")
    store.link_task_run(tid, run.id)

    base = {"kind": "diff", "name": "patch.diff", "uri": "docs/patch.diff"}
    assert collab.validate_artifact_fields(store, tid, base) is None
    a = store.create_task_artifact(
        tid,
        kind="diff",
        name="patch.diff",
        uri="docs/patch.diff",
        run_id=run.id,
        checksum_sha256="a" * 64,
        media_type="text/x-diff",
        size_bytes=123,
        metadata={"lines": 10},
        created_by="tester",
    )
    assert a is not None
    assert a["kind"] == "diff" and a["run_id"] == run.id
    assert a["checksum_sha256"] == "a" * 64
    assert a["metadata"] == {"lines": 10}

    items = store.list_task_artifacts(tid)
    assert [i["id"] for i in items] == [a["id"]]
    assert store.get_task_artifact(a["id"])["name"] == "patch.diff"  # type: ignore[index]

    # 검증 실패 경로
    assert collab.validate_artifact_fields(store, tid, {**base, "uri": "http://x"}) is not None
    assert collab.validate_artifact_fields(store, tid, {**base, "kind": "bogus"}) is not None
    assert (
        collab.validate_artifact_fields(store, tid, {**base, "checksum_sha256": "zz"}) is not None
    )
    assert collab.validate_artifact_fields(store, tid, {**base, "size_bytes": -1}) is not None
    assert (
        collab.validate_artifact_fields(store, tid, {**base, "metadata": {"blob": "x" * 9000}})
        is not None
    )
    # 존재하지 않는 run / 이 task에 링크되지 않은 run → 거부
    other = store.create_run("dev_review", "1", {}, trace_id="")
    assert collab.validate_artifact_fields(store, tid, {**base, "run_id": other.id}) is not None
    assert collab.validate_artifact_fields(store, tid, {**base, "run_id": "ghost"}) is not None


def test_task_runs_view(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task = _task(store)
    run = store.create_run("dev_review", "1", {"task": "t"}, trace_id="")
    store.link_task_run(task["id"], run.id)
    view = collab.task_runs_view(store, task["id"])
    assert len(view) == 1
    assert view[0]["run_id"] == run.id
    assert view[0]["workflow"] == "dev_review"
    assert view[0]["status"] == "queued"
    # 링크 없는 task → 빈 목록
    assert collab.task_runs_view(store, _task(store, "other")["id"]) == []
