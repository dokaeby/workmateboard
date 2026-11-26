"""프로젝트 마스터 + 태깅 집계 — project_overview/project_detail.

insights.py와 같은 패턴: Store 읽기 + 순수 함수, 상태 변경 없음.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from .lead_common import (
    _APPROVAL_STATES,
    _TERMINAL_BAD,
    _TERMINAL_OK,
    UNTAGGED,
    _now,
    _parse_ts,
    _proj_key,
    _proj_raw,
    _run_project,
    session_est_cost_usd,
    session_health,
    session_view,
)
from .pricing import cost_usd

if TYPE_CHECKING:
    from .store.db import Store


# ---------- 프로젝트 마스터 + 태깅 ----------


def _project_groups(store: Store) -> dict[str, dict[str, Any]]:
    """project_key → {project(master|None), tag, sessions[], runs[]}.

    마스터 프로젝트는 활동이 없어도 그룹을 만든다. 태그 없는 세션/run은
    key='' 그룹("(기타)")에 모은다."""
    groups: dict[str, dict[str, Any]] = {}
    for p in store.list_projects():
        key = _proj_key(p.get("name"))
        groups[key] = {"project": p, "tag": _proj_raw(p.get("name")), "sessions": [], "runs": []}
    for s in store.list_agent_sessions(limit=1000, include_ended=True):
        raw = _proj_raw(s.get("project"))
        key = _proj_key(raw)
        g = groups.setdefault(key, {"project": None, "tag": raw, "sessions": [], "runs": []})
        if not g["tag"] and raw:
            g["tag"] = raw
        g["sessions"].append(s)
    for r in store.list_runs(limit=1000):
        raw = _run_project(r)
        key = _proj_key(raw)
        g = groups.setdefault(key, {"project": None, "tag": raw, "sessions": [], "runs": []})
        if not g["tag"] and raw:
            g["tag"] = raw
        g["runs"].append(r)
    return groups


def _group_name(g: dict[str, Any]) -> str:
    if g["project"] is not None:
        return str(g["project"].get("name") or "")
    return g["tag"] or UNTAGGED


def _group_id(g: dict[str, Any], key: str) -> str:
    if g["project"] is not None:
        return str(g["project"]["id"])
    return "untagged" if key == "" else f"tag:{key}"


def _run_metrics(runs: list[Any]) -> dict[str, Any]:
    """그룹 run 집계 — success_rate/avg_seconds."""
    succ = fail = 0
    durations: list[float] = []
    for r in runs:
        st = str(getattr(r, "status", "") or "")
        if st == "completed":
            succ += 1
        elif st in _TERMINAL_BAD:
            fail += 1
        created = _parse_ts(getattr(r, "created_at", None))
        updated = _parse_ts(getattr(r, "updated_at", None))
        if st in _TERMINAL_OK | _TERMINAL_BAD and created and updated:
            durations.append((updated - created).total_seconds())
    terminal = succ + fail
    return {
        "run_count": len(runs),
        "success_rate": (round(succ / terminal * 100, 1) if terminal else None),
        "avg_seconds": (round(sum(durations) / len(durations), 1) if durations else None),
    }


def _recent_activity(g: dict[str, Any], limit: int = 8) -> list[dict[str, Any]]:
    """그룹의 최근 활동 — run 생성 + 세션 이벤트를 시간순 병합."""
    events: list[dict[str, Any]] = []
    for r in g["runs"][:30]:
        task = ""
        inp = getattr(r, "input", None)
        if isinstance(inp, dict):
            task = str(inp.get("task") or "")[:60]
        text = f"{getattr(r, 'workflow_key', '')}: {task}".strip(": ")
        at_val = getattr(r, "created_at", None)
        at_str = at_val.isoformat() if isinstance(at_val, datetime) else str(at_val or "")
        events.append({"at": at_str, "kind": "run", "text": text[:160]})
    for s in g["sessions"][:30]:
        doing = str(s.get("doing") or s.get("ask") or s.get("title") or "")[:80]
        text = " ".join(x for x in (str(s.get("tool") or ""), doing) if x)
        at = s.get("last_event_at") or s.get("updated_at") or s.get("created_at") or ""
        events.append({"at": str(at), "kind": "session", "text": text[:160]})
    events.sort(key=lambda e: str(e.get("at") or ""), reverse=True)
    return events[:limit]


def project_overview(store: Store) -> dict[str, Any]:
    """GET /projects/overview — 마스터+자유 태그+기타 버킷 전체 집계."""
    items: list[dict[str, Any]] = []
    for key, g in _project_groups(store).items():
        proj = g["project"] or {}
        metrics = _run_metrics(g["runs"])
        active = sum(1 for s in g["sessions"] if session_health(s) == "active")
        waiting_runs = sum(
            1 for r in g["runs"] if str(getattr(r, "status", "")) in _APPROVAL_STATES
        )
        attention_sessions = sum(
            1
            for s in g["sessions"]
            if str(s.get("status") or "") == "decision" or bool(str(s.get("ask") or "").strip())
        )
        cost = sum((session_est_cost_usd(s) or 0.0) for s in g["sessions"])
        items.append(
            {
                "id": _group_id(g, key),
                "name": _group_name(g),
                "color": str(proj.get("color") or ""),
                "description": str(proj.get("description") or ""),
                "goal": str(proj.get("goal") or ""),
                "goal_id": proj.get("goal_id") or None,
                "target_count": int(proj.get("target_count") or 0),
                "created_by": str(proj.get("created_by") or ""),
                "session_count": len(g["sessions"]),
                "active_sessions": active,
                "run_count": metrics["run_count"],
                "success_rate": metrics["success_rate"],
                "avg_seconds": metrics["avg_seconds"],
                "total_cost_usd": round(cost, 6),
                "open_approvals": waiting_runs + attention_sessions,
                "recent_activity": _recent_activity(g),
            }
        )
    items.sort(key=lambda x: (-x["session_count"], -x["run_count"], x["name"]))
    return {"items": items}


def _resolve_project(store: Store, ref: str) -> tuple[str, dict[str, Any] | None, str] | None:
    """id / 'untagged' / 'tag:<key>' / 프로젝트 이름(ci) → (key, master|None, name)."""
    ref = (ref or "").strip()
    if ref in ("", "untagged", UNTAGGED):
        return "", None, UNTAGGED
    if ref.startswith("tag:"):
        key = ref[4:].strip().lower()
        groups = _project_groups(store)
        g = groups.get(key)
        if g is None:
            return None
        return key, g["project"], _group_name(g)
    proj = store.get_project(ref)
    if proj is not None:
        return _proj_key(proj.get("name")), proj, str(proj.get("name") or "")
    # 이름으로도 허용 (ci)
    proj = store.find_project_by_name(ref)
    if proj is not None:
        return _proj_key(proj.get("name")), proj, str(proj.get("name") or "")
    return None


def project_detail(store: Store, ref: str) -> dict[str, Any] | None:
    """GET /projects/{id}/detail — 세션/run 요약 + 타임라인 + 실패/비용 집계."""
    resolved = _resolve_project(store, ref)
    if resolved is None:
        return None
    key, master, name = resolved
    groups = _project_groups(store)
    g = groups.get(key, {"project": master, "tag": name, "sessions": [], "runs": []})

    sessions = [session_view(s) for s in g["sessions"]]
    runs = []
    for r in g["runs"]:
        created = _parse_ts(getattr(r, "created_at", None))
        updated = _parse_ts(getattr(r, "updated_at", None))
        duration = (
            round((updated - created).total_seconds(), 1)
            if created
            and updated
            and str(getattr(r, "status", "")) in (_TERMINAL_OK | _TERMINAL_BAD)
            else None
        )
        task = ""
        if isinstance(getattr(r, "input", None), dict):
            task = str(r.input.get("task") or "")[:160]
        runs.append(
            {
                "id": r.id,
                "workflow": r.workflow_key,
                "status": str(getattr(r, "status", "") or ""),
                "task": task,
                "project": _run_project(r),
                "created_by": getattr(r, "created_by", ""),
                "created_at": created.isoformat() if created else "",
                "updated_at": updated.isoformat() if updated else "",
                "duration_s": duration,
                "error": getattr(r, "error", "") or "",
            }
        )

    # 타임라인 — run 생성 + 세션 이벤트 병합 (최신 50)
    timeline = _recent_activity({"runs": g["runs"], "sessions": g["sessions"]}, limit=50)

    # stage_runs (90일) — 프로젝트 태그 run만 필터해 단계 실패/일별 비용 계산
    since_90 = (_now() - timedelta(days=90)).isoformat()
    since_30 = (_now() - timedelta(days=30)).isoformat()
    stage_rows = [r for r in store.stage_run_rows(since_90) if _proj_key(r.get("project")) == key]
    latest: dict[tuple[str, str], tuple[int, str, str]] = {}
    for r in stage_rows:
        skey = (str(r["run_id"]), str(r["stage_key"]))
        attempt = int(r.get("attempt") or 0)
        if skey not in latest or attempt > latest[skey][0]:
            latest[skey] = (attempt, str(r.get("status") or ""), str(r.get("run_id") or ""))
    stage_failures: dict[str, int] = {}
    for (_rid, stage_key), (_a, st, _r) in latest.items():
        if st == "failed":
            stage_failures[stage_key] = stage_failures.get(stage_key, 0) + 1

    by_day: dict[str, float] = {}
    for r in stage_rows:
        day = str(r.get("started_at") or r.get("run_created_at") or "")[:10]
        if not day or day < since_30[:10]:
            continue
        c = cost_usd(
            str(r.get("model") or ""),
            int(r.get("tokens_in") or 0),
            int(r.get("tokens_out") or 0),
        )
        if c:
            by_day[day] = by_day.get(day, 0.0) + c
    cost_by_day = [{"day": d, "usd": round(by_day[d], 6)} for d in sorted(by_day)]

    project_obj = master or {
        "id": _group_id({"project": None, "tag": name}, key),
        "name": name,
        "color": "",
        "description": "",
        "goal": "",
        "target_count": 0,
        "created_by": "",
    }
    return {
        "project": project_obj,
        "sessions": sessions,
        "runs": runs,
        "timeline": timeline,
        "stage_failures": stage_failures,
        "cost_by_day": cost_by_day,
    }
