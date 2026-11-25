"""통합 승인 큐 집계 — approval_queue (GET /approvals).

Store 읽기 + 순수 함수, 상태 변경 없음.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from .lead_common import (
    _APPROVAL_STATES,
    _MAX_ITEMS,
    _age_seconds,
    _now,
    _proj_raw,
    _run_project,
)

if TYPE_CHECKING:
    from .store.db import Store


# ---------- 통합 승인 큐 (GET /approvals) ----------


def approval_queue(store: Store) -> dict[str, Any]:
    """GET /approvals — run waiting_* + 세션 decision/ask + 트리거 오류."""
    now = _now()
    items: list[dict[str, Any]] = []

    for r in store.list_runs(limit=200):
        st = str(getattr(r, "status", "") or "")
        if st not in _APPROVAL_STATES:
            continue
        waiting = getattr(r, "updated_at", None)
        task = ""
        if isinstance(getattr(r, "input", None), dict):
            task = str(r.input.get("task") or "")[:80]
        items.append(
            {
                "kind": "run",
                "id": r.id,
                "title": f"{getattr(r, 'workflow_key', '')}: {task or r.id}".strip(": "),
                "waiting_since": waiting.isoformat()
                if isinstance(waiting, datetime)
                else str(waiting or ""),
                "age_seconds": _age_seconds(waiting, now),
                "project": _run_project(r),
                "status": st,
            }
        )

    for s in store.list_agent_sessions(limit=200):
        needs = str(s.get("status") or "") == "decision" or bool(str(s.get("ask") or "").strip())
        if not needs:
            continue
        waiting = s.get("last_event_at") or s.get("updated_at")
        title = " · ".join(
            x
            for x in (
                str(s.get("tool") or ""),
                str(s.get("title") or s.get("project") or str(s.get("session_key") or "")[:12]),
            )
            if x
        )
        items.append(
            {
                "kind": "session",
                "id": f"{s.get('tool')}:{s.get('session_key')}",
                "session_key": s.get("session_key"),
                "tool": s.get("tool"),
                "title": title,
                "waiting_since": str(waiting or ""),
                "age_seconds": _age_seconds(waiting, now),
                "project": _proj_raw(s.get("project")),
                "status": str(s.get("status") or ""),
            }
        )

    for t in store.list_triggers():
        if not str(t.get("last_error") or "").strip():
            continue
        waiting = t.get("last_fired_at") or t.get("created_at")
        m = t.get("match")
        match = m if isinstance(m, dict) else {}
        items.append(
            {
                "kind": "trigger",
                "id": str(t.get("id") or ""),
                "title": f"트리거 오류: {str(t.get('id') or '')[:12]}",
                "title_key": "appr.triggerError.title",
                "title_params": {"id": str(t.get("id") or "")[:12]},
                "waiting_since": str(waiting or ""),
                "age_seconds": _age_seconds(waiting, now),
                "project": _proj_raw(match.get("project")),
                "status": "error",
            }
        )

    # 가장 오래 기다린 순
    items.sort(key=lambda x: (x.get("waiting_since") or "9999", -(x.get("age_seconds") or 0)))
    return {"items": items[:_MAX_ITEMS]}
