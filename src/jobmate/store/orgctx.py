"""요청 단위 조직 컨텍스트 — spec 003 US3.

API 계층이 요청 시작 시 set_org_context()로 현재 조직을 심고,
store 계층의 org-scoped INSERT/SELECT가 이 값을 읽는다.
세션이 없는 경로(스케줄러·크론·내부 잡)는 명시 org_id를 인자로 넘기거나
비워 두고 행의 org_id를 그대로 전파한다.
"""

from __future__ import annotations

import contextvars

_org_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("jobmate_org_id", default="")


def set_org_context(org_id: str) -> contextvars.Token[str]:
    return _org_ctx.set(org_id or "")


def reset_org_context(token: contextvars.Token[str]) -> None:
    _org_ctx.reset(token)


def current_org_id() -> str:
    return _org_ctx.get()
