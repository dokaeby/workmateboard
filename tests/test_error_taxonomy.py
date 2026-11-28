"""spec 007 US3 — services/error_taxonomy.classify_error 단위 테스트."""

from __future__ import annotations

import pytest

from jobmate.services.error_taxonomy import ERROR_CLASSES, classify_error


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # timeout
        ("request timeout after 30s", "timeout"),
        ("Read timed out", "timeout"),
        ("deadline exceeded", "timeout"),
        ("stale_timeout", "timeout"),
        # rate_limit
        ("HTTP 429 Too Many Requests", "rate_limit"),
        ("rate limit exceeded for model", "rate_limit"),
        ("RateLimitError: quota", "rate_limit"),
        # auth
        ("401 Unauthorized", "auth"),
        ("403 Forbidden", "auth"),
        ("invalid api key provided", "auth"),
        ("authentication failed", "auth"),
        # tool_error
        ("tool execution failed: bash", "tool_error"),
        ("unknown tool 'foo'", "tool_error"),
        # schema_parse
        ("JSON parse error: Expecting value", "schema_parse"),
        ("response did not match schema", "schema_parse"),
        ("pydantic ValidationError", "schema_parse"),
        # budget
        ("budget_exhausted", "budget"),
        ("token_budget exceeded", "budget"),
        ("cost_budget limit reached", "budget"),
        # unknown / edge
        ("something weird happened", "unknown"),
        ("", "unknown"),
        (None, "unknown"),
    ],
)
def test_classify_error(text, expected):
    assert classify_error(text) == expected


def test_classify_error_returns_known_vocabulary():
    for text in (
        "timeout",
        "429",
        "401",
        "tool x",
        "json parse",
        "budget",
        "random",
    ):
        assert classify_error(text) in ERROR_CLASSES


def test_budget_checked_before_tool():
    # "tool_call_budget"은 tool이 아니라 budget으로 분류돼야 한다.
    assert classify_error("tool_call_budget exceeded") == "budget"


def test_update_run_error_empty_clears_error_class(tmp_path):
    """error='' 명시 전달은 error_class도 함께 지운다 — error만 비우고
    분류만 남는 stale 상태 방지 (spec 007 US3)."""
    import tempfile
    from pathlib import Path

    from jobmate.models import RunStatus
    from jobmate.store.db import Store

    store = Store(str(Path(tempfile.mkdtemp()) / "t.db"))
    run = store.create_run("wf", "0", {"task": "t"}, trace_id="t")
    store.update_run(run.id, status=RunStatus.FAILED, error="connection timed out")
    row = store._conn.execute(  # noqa: SLF001
        "SELECT error,error_class FROM runs WHERE id=?", (run.id,)
    ).fetchone()
    assert row["error_class"] == "timeout"
    store.update_run(run.id, error="")
    row = store._conn.execute(  # noqa: SLF001
        "SELECT error,error_class FROM runs WHERE id=?", (run.id,)
    ).fetchone()
    assert row["error"] == ""
    assert row["error_class"] == ""
