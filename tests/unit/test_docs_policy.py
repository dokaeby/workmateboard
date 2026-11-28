"""spec 006 US7 FR-024 — 프로덕션 문서 비공개 정책 테스트.

mode="release"(또는 VERCEL)에서는 /docs, /redoc, /openapi.json이
404로 비활성화되고, debug 모드에서는 기존대로 200을 반환해야 한다.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from jobmate.api.app import create_app
from jobmate.config import Settings
from jobmate.store.db import Store
from tests.integration.helpers import default_outputs, scripted_llm

_DOC_PATHS = ("/openapi.json", "/docs", "/redoc")


def _client(tmp_path: Path, mode: str) -> TestClient:
    db = str(tmp_path / f"docs-{mode}.db")
    settings = Settings(
        db_path=db,
        budget_max_tokens=100_000,
        mode=mode,  # type: ignore[arg-type]
        # release 모드의 PG DSN 강제(fail-fast)를 우회 — 문서 정책만 검증.
        allow_ephemeral_db=True,
    )
    store = Store(db)
    app = create_app(settings=settings, store=store, llm=scripted_llm(default_outputs()))
    return TestClient(app)


def test_release_mode_hides_api_docs(tmp_path: Path) -> None:
    client = _client(tmp_path, "release")
    for path in _DOC_PATHS:
        r = client.get(path)
        assert r.status_code == 404, f"{path} should be 404 in release mode, got {r.status_code}"


def test_debug_mode_serves_api_docs(tmp_path: Path) -> None:
    client = _client(tmp_path, "debug")
    for path in _DOC_PATHS:
        r = client.get(path)
        assert r.status_code == 200, f"{path} should be 200 in debug mode, got {r.status_code}"
