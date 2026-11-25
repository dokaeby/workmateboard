"""상용 배포 감사 수정 회귀 테스트 — checkpointer 재사용, cost 집행,
PG fail-fast, serverless 단일 app, encryption fail-fast, radar 예산 조기종료."""

from __future__ import annotations

import pytest

from jobmate.config import Settings
from jobmate.engine.runner import (
    _checkpointer_target,
    build_checkpointer_for,
    close_checkpointers,
)


@pytest.fixture(autouse=True)
def _clean_checkpointer_cache():
    yield
    close_checkpointers()


def _no_pg_env(monkeypatch):
    for v in ("POSTGRES_URL", "POSTGRES_PRISMA_URL", "JOBMATE_DATABASE_URL", "VERCEL"):
        monkeypatch.delenv(v, raising=False)


# ---------- 1. checkpointer 재사용 ----------


def test_sqlite_checkpointer_reused(tmp_path, monkeypatch):
    """같은 settings로 build_checkpointer_for를 여러 번 호출해도 saver는 1개."""
    _no_pg_env(monkeypatch)
    s = Settings(db_path=str(tmp_path / "t.db"))
    a = build_checkpointer_for(s)
    b = build_checkpointer_for(s)
    assert a is b
    # 다른 db_path는 별도 saver
    s2 = Settings(db_path=str(tmp_path / "u.db"))
    c = build_checkpointer_for(s2)
    assert c is not a


def test_checkpointer_target_uses_store_db_path(tmp_path, monkeypatch):
    """sqlite checkpoint 경로는 database_dsn() 기준 — Vercel이면 /tmp."""
    _no_pg_env(monkeypatch)
    s = Settings(db_path=str(tmp_path / "t.db"))
    kind, target = _checkpointer_target(s)
    assert kind == "sqlite"
    assert target == str(tmp_path / "t.db") + ".checkpoints"
    monkeypatch.setenv("VERCEL", "1")
    kind, target = _checkpointer_target(s)
    assert kind == "sqlite"
    assert target == "/tmp/jobmate.db.checkpoints"  # noqa: S108 - Vercel 임시 경로 계약 검증


def test_pg_checkpointer_connects_once(tmp_path, monkeypatch):
    """PostgresSaver용 psycopg.connect + setup()은 프로세스당 1회만."""
    from unittest.mock import MagicMock

    import langgraph.checkpoint.postgres as pgmod
    import psycopg

    class FakeSaver:
        def __init__(self, conn):
            self.conn = conn
            self.setup_calls = 0

        def setup(self):
            self.setup_calls += 1

    monkeypatch.setattr(pgmod, "PostgresSaver", FakeSaver)
    calls = []

    def fake_connect(*args, **kwargs):
        calls.append((args, kwargs))
        return MagicMock(name="pg_conn")

    monkeypatch.setattr(psycopg, "connect", fake_connect)
    s = Settings(database_url="postgresql://u:p@h:5432/db")
    a = build_checkpointer_for(s)
    b = build_checkpointer_for(s)
    assert a is b
    assert len(calls) == 1
    assert a.setup_calls == 1


# ---------- 2. cost_usd 누적 ----------


def _llm_runner(tmp_path, fn_mod=None):
    from jobmate.engine.registry import Registry
    from jobmate.engine.runner import WorkflowRunner
    from jobmate.llm import FakeLLM
    from jobmate.models import AgentSpec, ModelConfig, StageSpec, WorkflowSpec
    from jobmate.store.db import Store
    from jobmate.tools import ToolExecutor

    db = str(tmp_path / "t.db")
    settings = Settings(db_path=db)
    store = Store(db)
    spec = WorkflowSpec(
        key="wf",
        version="0",
        entry="s1",
        stages=[StageSpec(key="s1", kind="llm", agent="a")],
    )
    reg = Registry()
    reg.register_agent(
        AgentSpec(
            key="a",
            role="analysis",
            prompt="analysis",
            output_schema="AnalysisResult",
            model=ModelConfig(model="gemini-test-model"),
        )
    )
    runner = WorkflowRunner(
        spec=spec,
        registry=reg,
        store=store,
        llm=FakeLLM(handler=lambda **kw: {"requirements": "r", "scope": "s"}),
        tools=ToolExecutor(repo_root=str(tmp_path)),
        settings=settings,
    )
    run = store.create_run("wf", "0", {"task": "t"}, trace_id="t")
    return runner, run


def test_llm_stage_accumulates_cost_usd(tmp_path):
    """LLM 단계 완료 시 pricing.cost_usd로 환산해 state.cost_usd에 누적."""
    from jobmate.pricing import cost_usd

    runner, run = _llm_runner(tmp_path)
    state = runner.invoke(run.id, {"task": "t"})
    assert state.get("tokens_in") == 10
    expected = cost_usd("gemini-test-model", 10, 10)
    assert state.get("cost_usd") == pytest.approx(expected)
    assert state["cost_usd"] > 0


def test_function_stage_accumulates_cost_usd(tmp_path):
    """function 단계가 반환한 cost_usd도 state에 합산되고 output에서는 제거."""
    from jobmate.engine import functions as fn_mod
    from jobmate.engine.registry import Registry
    from jobmate.engine.runner import WorkflowRunner
    from jobmate.llm import FakeLLM
    from jobmate.models import StageSpec, WorkflowSpec
    from jobmate.store.db import Store
    from jobmate.tools import ToolExecutor

    db = str(tmp_path / "t.db")
    settings = Settings(db_path=db)
    store = Store(db)
    spec = WorkflowSpec(
        key="wf",
        version="0",
        entry="f1",
        stages=[StageSpec(key="f1", kind="function", function="radar_dedupe")],
    )
    runner = WorkflowRunner(
        spec=spec,
        registry=Registry(),
        store=store,
        llm=FakeLLM(),
        tools=ToolExecutor(repo_root=str(tmp_path)),
        settings=settings,
    )
    run = store.create_run("wf", "0", {"task": "t"}, trace_id="t")

    def fake_fn(ctx):
        return {"status": "ok", "tokens_in": 100, "tokens_out": 50, "cost_usd": 0.25}

    original = fn_mod.FUNCTIONS["radar_dedupe"]
    fn_mod.FUNCTIONS["radar_dedupe"] = fake_fn
    try:
        state = runner.invoke(run.id, {"task": "t"})
    finally:
        fn_mod.FUNCTIONS["radar_dedupe"] = original
    assert state.get("cost_usd") == pytest.approx(0.25)
    assert "cost_usd" not in state["stage_outputs"]["f1"]


# ---------- 3. PG fail-fast ----------


def test_release_mode_requires_pg_dsn(monkeypatch):
    from jobmate.config import get_settings

    get_settings.cache_clear()
    try:
        _no_pg_env(monkeypatch)
        monkeypatch.setenv("JOBMATE_MODE", "release")
        with pytest.raises(RuntimeError, match="PostgreSQL DSN"):
            get_settings()
    finally:
        get_settings.cache_clear()


def test_vercel_env_requires_pg_dsn(monkeypatch):
    from jobmate.config import get_settings

    get_settings.cache_clear()
    try:
        _no_pg_env(monkeypatch)
        monkeypatch.setenv("JOBMATE_MODE", "debug")
        monkeypatch.setenv("VERCEL", "1")
        with pytest.raises(RuntimeError, match="PostgreSQL DSN"):
            get_settings()
    finally:
        get_settings.cache_clear()


def test_ephemeral_db_opt_out(monkeypatch):
    from jobmate.config import get_settings

    get_settings.cache_clear()
    try:
        _no_pg_env(monkeypatch)
        monkeypatch.setenv("JOBMATE_MODE", "release")
        monkeypatch.setenv("JOBMATE_ALLOW_EPHEMERAL_DB", "1")
        monkeypatch.setenv("JOBMATE_ENCRYPTION_KEY", "test-key-material")
        s = get_settings()
        assert s.allow_ephemeral_db is True
    finally:
        get_settings.cache_clear()


def test_debug_mode_allows_sqlite(monkeypatch):
    from jobmate.config import get_settings

    get_settings.cache_clear()
    try:
        _no_pg_env(monkeypatch)
        monkeypatch.setenv("JOBMATE_MODE", "debug")
        monkeypatch.delenv("VERCEL", raising=False)
        s = get_settings()
        assert s.mode == "debug"
    finally:
        get_settings.cache_clear()


# ---------- 4. serverless 단일 app ----------


def test_serverless_reuses_app_instance():
    """serverless.py는 create_app()을 다시 호출하지 않고 app.py의 app을 재수출."""
    from jobmate.api import app as app_module
    from jobmate.api import serverless

    assert serverless.app is app_module.app


# ---------- 6. encryption fail-fast ----------


def _clear_key_material(monkeypatch):
    from jobmate import security

    for v in (
        security.ENCRYPTION_KEY_ENV,
        security.API_KEY_ENV,
        *security._CRON_SECRET_ENVS,
    ):
        monkeypatch.delenv(v, raising=False)


def test_encrypt_str_raises_in_production_without_key(monkeypatch):
    from jobmate import security

    _clear_key_material(monkeypatch)
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("JOBMATE_MODE", "debug")  # VERCEL만으로도 프로덕션 판정
    with pytest.raises(RuntimeError, match="암호화 키"):
        security.enforce_production_secrets()
    with pytest.raises(RuntimeError, match="암호화 키"):
        security.encrypt_str("super-secret")


def test_encrypt_str_plaintext_allowed_in_debug(monkeypatch):
    from jobmate import security

    _clear_key_material(monkeypatch)
    monkeypatch.delenv("VERCEL", raising=False)
    monkeypatch.setenv("JOBMATE_MODE", "debug")
    security.enforce_production_secrets()  # raise 없어야 함
    assert security.encrypt_str("super-secret") == "super-secret"


def test_encrypt_str_with_key_encrypts(monkeypatch):
    from jobmate import security

    _clear_key_material(monkeypatch)
    monkeypatch.delenv("VERCEL", raising=False)
    monkeypatch.setenv("JOBMATE_MODE", "release")
    monkeypatch.setenv(security.ENCRYPTION_KEY_ENV, "test-key-material")
    security.enforce_production_secrets()
    enc = security.encrypt_str("super-secret")
    assert enc.startswith(security.PREFIX)
    assert security.decrypt_str(enc) == "super-secret"


# ---------- 7. function 단계 LLM 예산 조기종료 ----------


def _radar_ctx(tmp_path, repos, monkeypatch=None, **settings_kw):
    from jobmate.engine.functions import FunctionContext
    from jobmate.store.db import Store

    db = str(tmp_path / "radar.db")
    settings = Settings(db_path=db, **settings_kw)
    store = Store(db)
    return FunctionContext(
        state={
            "stage_outputs": {"dedupe": {"repos": repos}},
            "started_at": __import__("time").time(),
        },
        store=store,
        settings=settings,
        adapters={},
        run_id="t",
        llm=None,  # caller may set
    )


def _repo(name):
    return {
        "full_name": name,
        "html_url": f"https://github.com/{name}",
        "description": f"desc {name}",
        "stars": 10,
        "language": "Python",
        "topics": [],
        "readme": f"# {name} readme",
    }


def _radar_llm():
    from jobmate.llm import FakeLLM
    from jobmate.models import RadarClassification

    def handler(system, user, model, schema):
        if schema is not None and schema.__name__ == "RadarClassification":
            return RadarClassification(label="skills", confidence=0.9)
        return {
            "summary_en": ["en a", "en b", "en c"],
            "summary_zh": ["zh a", "zh b", "zh c"],
            "summary_ko": ["ko 가", "ko 나", "ko 다"],
            "supported": True,
        }

    return FakeLLM(handler)


def test_radar_summarize_budget_early_exit(tmp_path):
    """토큰 예산 소진 시 남은 repo는 LLM 호출 없이 skipped_budget 처리."""
    from jobmate.engine.functions import radar_summarize

    repos = [_repo("a/x"), _repo("b/y"), _repo("c/z")]
    ctx = _radar_ctx(tmp_path, repos, budget_max_tokens=45)
    ctx.llm = _radar_llm()
    out = radar_summarize(ctx)
    statuses = [r["summary_status"] for r in out["repos"]]
    # repo당 classify+summarize = in20/out20 → 2개 처리 후 예산(45) 초과 → 3번째 skip
    assert statuses[0] == "ok"
    assert statuses[1] == "ok"
    assert statuses[2] == "skipped_budget"
    assert len(ctx.llm.calls) == 4  # 2 repos × 2 calls — 3번째 repo는 호출 없음
    assert out["metrics"]["budget_skipped"] == 1
    assert out["cost_usd"] > 0


def test_radar_summarize_budget_exhausted_before_first(tmp_path):
    """run 시작 전 이미 예산 소진 상태면 첫 repo부터 전부 skipped_budget."""
    from jobmate.engine.functions import radar_summarize

    ctx = _radar_ctx(
        tmp_path,
        [_repo("a/x"), _repo("b/y")],
        budget_max_tokens=10,
    )
    ctx.state["tokens_in"] = 20  # 이미 한도 초과
    ctx.llm = _radar_llm()
    out = radar_summarize(ctx)
    assert all(r["summary_status"] == "skipped_budget" for r in out["repos"])
    assert ctx.llm.calls == []
    assert out["tokens_in"] == 0


def test_radar_verify_budget_early_exit(tmp_path):
    """verify 단계도 예산 소진 시 남은 항목을 skipped_budget으로 조기 종료."""
    from jobmate.engine.functions import radar_verify

    repos = [
        {**_repo("a/x"), "summary_status": "ok", "_summary_en_lines": ["a"]},
        {**_repo("b/y"), "summary_status": "ok", "_summary_en_lines": ["b"]},
    ]
    ctx = _radar_ctx(tmp_path, [], budget_max_tokens=10)
    ctx.state["tokens_in"] = 20  # 이미 한도 초과
    ctx.state["stage_outputs"]["summarize"] = {"repos": repos, "metrics": {}}
    ctx.llm = _radar_llm()
    out = radar_verify(ctx)
    assert all(r["summary_status"] == "skipped_budget" for r in out["repos"])
    assert ctx.llm.calls == []
