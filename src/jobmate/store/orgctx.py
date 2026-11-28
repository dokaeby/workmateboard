"""요청 단위 조직 컨텍스트 — spec 003 US3.

API 계층이 요청 시작 시 set_org_context()로 현재 조직을 심고,
store 계층의 org-scoped INSERT/SELECT가 이 값을 읽는다.
세션이 없는 경로(스케줄러·크론·내부 잡)는 명시 org_id를 인자로 넘기거나
비워 두고 행의 org_id를 그대로 전파한다.
"""

from __future__ import annotations

import contextlib
import contextvars
from collections.abc import Iterator

_org_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("jobmate_org_id", default="")


def set_org_context(org_id: str) -> contextvars.Token[str]:
    return _org_ctx.set(org_id or "")


def reset_org_context(token: contextvars.Token[str]) -> None:
    _org_ctx.reset(token)


def current_org_id() -> str:
    return _org_ctx.get()


@contextlib.contextmanager
def org_scope(org_id: str) -> Iterator[None]:
    """스케줄러·웹훅 등 요청 컨텍스트가 없는 경로에서 조직 컨텍스트를 임시 설정."""
    tok = _org_ctx.set(org_id or "")
    try:
        yield
    finally:
        _org_ctx.reset(tok)


def org_for_write(explicit: str | None = None) -> str:
    """INSERT용 org_id 결정 — 명시 인자 > 요청 컨텍스트."""
    return explicit or _org_ctx.get()


def scoped_key(key: str) -> str:
    """멱등키를 조직 네임스페이스로 한정 — 전역 UNIQUE 충돌 방지(spec 003 US3).

    반환값은 저장/조회 양쪽에서 동일하게 적용해야 한다. ctx=''는 ':' 접두."""
    return f"{_org_ctx.get()}:{key}" if key else ""
