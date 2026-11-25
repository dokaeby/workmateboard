"""function 단계 공용 컨텍스트/헬퍼 — fn_* 모듈들이 공유."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ..sensors import build_gmail_url

logger = logging.getLogger("jobmate.engine.functions")


@dataclass
class FunctionContext:
    state: Any
    store: Any
    settings: Any
    adapters: dict[str, Any]
    run_id: str
    llm: Any = None


def _prev_output(ctx: FunctionContext, stage_key: str) -> dict[str, Any]:
    return ctx.state.get("stage_outputs", {}).get(stage_key, {})


def _src(ctx: FunctionContext) -> tuple[str, str, str, str, str]:
    """(provider, source_id, connection_id, thread_id, source_url)."""
    provider = ctx.state.get("source_provider") or "gmail"
    source_id = ctx.state.get("source_id") or ctx.state.get("gmail_message_id") or ""
    conn = ctx.state.get("connection_id") or "default"
    thread = ctx.state.get("thread_id") or ""
    url = ctx.state.get("source_url") or ""
    if provider == "gmail" and not url:
        url = build_gmail_url(source_id, conn)
    return provider, source_id, conn, thread, url


def send_web_push_text(adapters: dict[str, Any], title: str, body: str, source_url: str) -> None:
    """webpush adapter best-effort 발송 — 실패는 warning으로만 남긴다."""
    push = adapters.get("webpush")
    if push is None:
        return
    try:
        push.send(title=title, body=body, source_url=source_url)
    except Exception as e:
        logger.warning("webpush send failed: %s", str(e)[:200])
