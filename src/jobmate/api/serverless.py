"""Vercel serverless entry — app.py 모듈의 공유 app 인스턴스를 그대로 노출.

`create_app()`를 다시 호출하지 않는다 — Store/LLM/adapter가 이중 생성되는
것을 막기 위해 `jobmate.api.app`의 모듈 레벨 `app`을 재수출한다.
루트 `app.py`(Vercel Python entrypoint)가 이 모듈의 `app`을 import한다.
"""

from __future__ import annotations

from .app import app  # noqa: F401
