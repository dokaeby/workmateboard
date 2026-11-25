import pytest

from jobmate.llm import FakeLLM, extract_json
from jobmate.models import ModelConfig

MC = ModelConfig(model="m")


def test_extract_json_plain():
    assert extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_fenced():
    assert extract_json('```json\n{"a": 2}\n```') == {"a": 2}


def test_extract_json_embedded():
    assert extract_json('here is {"a": 3} done') == {"a": 3}


def test_extract_json_fail():
    with pytest.raises(ValueError):
        extract_json("no json here")


def test_fake_llm_records_calls():
    llm = FakeLLM(handler=lambda **kw: {"result": "x"})
    out, usage = llm.complete(model=MC, system="s", user="u")
    assert out == {"result": "x"}
    assert usage == {"tokens_in": 10, "tokens_out": 10}
    assert len(llm.calls) == 1


def test_fake_llm_schema():
    from jobmate.models import QaVerdict

    llm = FakeLLM(handler=lambda **kw: {"decision": "pass", "rationale": "ok"})
    out, _ = llm.complete(model=MC, system="s", user="u", schema=QaVerdict)
    assert isinstance(out, QaVerdict)


def test_iter_sse_data():
    from jobmate.llm.codex import iter_sse_data

    lines = [
        "event: response.created",
        'data: {"type":"response.created"}',
        "",
        "data: not-json",
        'data: {"type":"response.output_text.delta","delta":"hi"}',
        "data: [DONE]",
    ]
    evs = list(iter_sse_data(lines))
    assert evs == [
        {"type": "response.created"},
        {"type": "response.output_text.delta", "delta": "hi"},
    ]


def test_codex_auth_missing_file(tmp_path):
    from jobmate.llm import LLMError
    from jobmate.llm.codex import CodexAuth

    with pytest.raises(LLMError, match="codex login"):
        CodexAuth(tmp_path / "nope.json").headers()


def test_composite_llm_dispatch():
    from jobmate.llm.codex import CompositeLLM

    calls = []

    class Rec:
        def __init__(self, tag):
            self.tag = tag

        def complete(self, **kw):
            calls.append(self.tag)
            return {"ok": True}, {"tokens_in": 1, "tokens_out": 1}

    comp = CompositeLLM(compat=Rec("compat"), codex=Rec("codex"))
    codex_model = ModelConfig(model="gpt-5.6-sol", provider="codex")
    compat_model = ModelConfig(model="gemini-3.5-flash-lite")
    comp.complete(model=codex_model, system="s", user="u")
    comp.complete(model=compat_model, system="s", user="u")
    assert calls == ["codex", "compat"]


def test_settings_mode_routing():
    from jobmate.config import Settings

    debug = Settings(mode="debug")
    rel = Settings(mode="release")
    assert debug.model_for("implementer", "important").provider == "openai_compat"
    assert rel.model_for("analysis", "default").provider == "openai_compat"
    imp = rel.model_for("implementer", "important")
    assert imp.provider == "codex" and imp.model == rel.important_model


def test_configure_langsmith(monkeypatch):
    import os

    from jobmate.config import Settings, configure_langsmith

    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2-test")
    monkeypatch.delenv("LANGCHAIN_TRACING_V2", raising=False)
    monkeypatch.delenv("LANGCHAIN_PROJECT", raising=False)
    assert configure_langsmith(Settings(langsmith_tracing=True)) is True
    assert os.environ["LANGCHAIN_TRACING_V2"] == "true"
    assert os.environ["LANGCHAIN_API_KEY"] == "lsv2-test"
    assert os.environ["LANGCHAIN_PROJECT"] == "jobmate"
    assert configure_langsmith(Settings(langsmith_tracing=False)) is False
    for k in ("LANGCHAIN_TRACING_V2", "LANGCHAIN_API_KEY", "LANGCHAIN_PROJECT"):
        os.environ.pop(k, None)


def _codex_jwt(exp: float) -> str:
    import base64
    import json as _json

    payload = base64.urlsafe_b64encode(_json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"h.{payload}.s"


def test_codex_auth_refresh_writes_atomically(tmp_path, monkeypatch):
    """토큰 갱신 시 auth.json이 임시파일+os.replace로 원자적 저장된다."""
    import json as _json
    import os
    import time

    import jobmate.llm.codex as cx
    from jobmate.llm import LLMError

    auth_file = tmp_path / "auth.json"
    auth_file.write_text(
        _json.dumps(
            {
                "tokens": {
                    "access_token": _codex_jwt(time.time() - 10),  # 만료
                    "refresh_token": "rt-old",
                    "account_id": "acc",
                }
            }
        )
    )

    class _Resp:
        status_code = 200

        def json(self):
            return {"access_token": _codex_jwt(time.time() + 3600), "refresh_token": "rt-new"}

    monkeypatch.setattr(cx.httpx, "post", lambda *a, **k: _Resp())
    auth = cx.CodexAuth(auth_file)
    headers = auth.headers()
    assert headers["Authorization"].startswith("Bearer ")
    # 파일이 유효 JSON으로 갱신됐고 tmp 잔여물이 없다
    data = _json.loads(auth_file.read_text())
    assert data["tokens"]["refresh_token"] == "rt-new"  # noqa: S105 - 테스트용 더미 값
    assert not (tmp_path / "auth.json.tmp").exists()
    if os.name != "nt":
        assert oct(auth_file.stat().st_mode & 0o777) == "0o600"
    # 깨진 auth.json은 LLMError
    bad = tmp_path / "bad.json"
    bad.write_text("{")
    with pytest.raises(LLMError):
        cx.CodexAuth(bad).headers()


def test_codex_stream_without_completed_returns_partial(tmp_path, monkeypatch, caplog):
    """response.completed 없이 끝난 SSE도 부분 텍스트를 반환하되 경고를 남긴다."""
    import json as _json
    import logging
    import time

    import jobmate.llm.codex as cx
    from jobmate.models import ModelConfig

    auth_file = tmp_path / "auth.json"
    auth_file.write_text(
        _json.dumps(
            {
                "tokens": {
                    "access_token": _codex_jwt(time.time() + 3600),
                    "account_id": "acc",
                }
            }
        )
    )

    class _Resp:
        status_code = 200
        request = None

        def raise_for_status(self):
            return None

        def iter_lines(self):
            yield 'data: {"type":"response.output_text.delta","delta":"hel"}'
            yield 'data: {"type":"response.output_text.delta","delta":"lo"}'

    class _StreamCtx:
        def __enter__(self):
            return _Resp()

        def __exit__(self, *a):
            return False

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def stream(self, *a, **k):
            return _StreamCtx()

    monkeypatch.setattr(cx.httpx, "Client", _Client)
    llm = cx.CodexResponsesLLM(auth_path=auth_file)
    with caplog.at_level(logging.WARNING, logger="jobmate.llm.codex"):
        text, usage, calls = llm._stream("http://x/responses", {}, ModelConfig(model="m"))
    assert text == "hello"
    assert calls == []
    assert "without response.completed" in caplog.text
