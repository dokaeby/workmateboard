"""통합 테스트 공통 헬퍼."""

from __future__ import annotations

import tempfile
from typing import Any

from jobmate.config import Settings
from jobmate.llm import FakeLLM
from jobmate.models import (
    AnalysisResult,
    ChangeProposal,
    QaVerdict,
    Verdict,
)
from jobmate.store.db import Store
from jobmate.workflows.registry_builder import build_runner


def make_env(**settings_kw: Any):
    db = str(__import__("pathlib").Path(tempfile.mkdtemp()) / "t.db")
    kw = {"budget_max_tokens": 100_000, **settings_kw}
    settings = Settings(db_path=db, **kw)
    store = Store(db)
    return settings, store


def scripted_llm(script: dict[str, Any]):
    """schema 이름별 응답 큐. 값은 단일 응답 또는 리스트. 큐가 비면 마지막 반복."""
    from pydantic import BaseModel

    queues = {}
    for k, v in script.items():
        if isinstance(v, list):
            queues[k] = list(v)
        elif isinstance(v, BaseModel):
            queues[k] = [v]
        else:
            queues[k] = [v]

    def handler(system, user, model, schema):
        name = schema.__name__ if schema else ""
        q = queues.get(name)
        if not q:
            return {}
        item = q.pop(0) if len(q) > 1 else q[0]
        return item

    return FakeLLM(handler)


VALID_DIFF = (
    "diff --git a/x.txt b/x.txt\n"
    "new file mode 100644\n"
    "index 0000000..3b18e51\n"
    "--- /dev/null\n"
    "+++ b/x.txt\n"
    "@@ -0,0 +1 @@\n"
    "+hello\n"
)


def default_outputs(**over):
    out = {
        "AnalysisResult": AnalysisResult(requirements="req", scope="scope"),
        "ChangeProposal": ChangeProposal(summary="impl", diff=VALID_DIFF),
        "Verdict": Verdict(decision="approve", findings=[], summary="ok"),
        "QaVerdict": QaVerdict(decision="pass", rationale="all good"),
    }
    out.update(over)
    return out


def run_dev_review(store, settings, llm, task="fix bug"):
    run = store.create_run("dev_review", "0.1.0", {"task": task}, trace_id="test")
    runner = build_runner("dev_review", store=store, llm=llm, settings=settings)
    state = runner.invoke(run.id, {"task": task})
    return run.id, runner, state
