"""팀장(lead) 대시보드 집계 — /projects, /risks, /approvals, /perf, 세션 회고.

insights.py와 같은 패턴: Store 읽기 + 순수 함수, 상태 변경 없음.
app.py의 create_app() 안에서 지연 import되어 순환 참조를 피한다.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from .pricing import cost_usd

if TYPE_CHECKING:
    from .store.db import Store

UNTAGGED = "(기타)"
_MAX_ITEMS = 50

# app.py _SESSION_TOKEN_PRICES와 동일 매핑 — 세션 est_cost는 tool 라벨을
# pricing.price_for_model에 넣어 계산한다(동일 단가표).
_TERMINAL_OK = {"completed"}
_TERMINAL_BAD = {"failed", "rejected", "budget_exhausted", "needs_human", "cancelled"}
_APPROVAL_STATES = {"waiting_approval", "waiting_review"}


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse_ts(value: Any) -> datetime | None:
    """ISO 타임스탬프 파싱 — 실패/naive는 UTC 간주 (app.py와 동일 규칙)."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _age_seconds(at: Any, now: datetime | None = None) -> float | None:
    dt = _parse_ts(at)
    if dt is None:
        return None
    return max(0.0, ((now or _now()) - dt).total_seconds())


def _proj_key(value: Any) -> str:
    """프로젝트 태그 정규화 키 — trim + case-insensitive."""
    return str(value or "").strip().lower()


def _proj_raw(value: Any) -> str:
    return str(value or "").strip()


def _run_project(run: Any) -> str:
    """run의 프로젝트 태그 — project 컬럼 우선, input.project 폴백."""
    col = _proj_raw(getattr(run, "project", ""))
    if col:
        return col
    inp = getattr(run, "input", None)
    if isinstance(inp, dict):
        return _proj_raw(inp.get("project"))
    return ""


# ---------- 세션 헬스/비용 (app.py _session_health/_session_est_cost_usd 미러) ----------

_SESSION_STALE_SECONDS = 300.0
_SESSION_LONG_IDLE_SECONDS = 43200.0


def session_health(row: dict[str, Any]) -> str:
    """ended/stale/long_idle/active — app.py _session_health와 동일 규칙."""
    status = str(row.get("status") or "")
    if status == "ended":
        return "ended"
    ts = _parse_ts(row.get("last_event_at")) or _parse_ts(row.get("updated_at"))
    age = (_now() - ts).total_seconds() if ts is not None else 0.0
    if status in ("working", "decision"):
        return "stale" if age > _SESSION_STALE_SECONDS else "active"
    if status == "rest":
        return "long_idle" if age > _SESSION_LONG_IDLE_SECONDS else "active"
    return "active"


def session_est_cost_usd(row: dict[str, Any]) -> float | None:
    """tokens_in/out → USD 추정. 둘 다 없으면 None (app.py와 동일 값)."""
    tin = row.get("tokens_in")
    tout = row.get("tokens_out")
    if tin is None and tout is None:
        return None
    from .pricing import price_for_model

    price_in, price_out = price_for_model(str(row.get("tool") or ""))
    return round(((tin or 0) * price_in + (tout or 0) * price_out) / 1_000_000, 6)


def session_view(row: dict[str, Any]) -> dict[str, Any]:
    return {**row, "health": session_health(row), "est_cost_usd": session_est_cost_usd(row)}


def _fmt_duration(seconds: float) -> str:
    """초 → '2시간 13분'/'45분'/'30초' 한국어 표기."""
    s = int(round(seconds))
    if s < 60:
        return f"{s}초"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}분" if s == 0 else f"{m}분 {s}초"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}시간 {m}분" if m else f"{h}시간"
    d, h = divmod(h, 24)
    return f"{d}일 {h}시간"


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


# ---------- 세션 회고 (GET /agent-sessions/{key}/history) ----------


def session_history(store: Store, tool: str, session_key: str) -> dict[str, Any] | None:
    """세션별 회고 — 전이 타임라인 + 결정적 한국어 stats/summary."""
    sess = store.get_agent_session(tool, session_key)
    if sess is None:
        return None
    events = store.agent_session_history(tool, session_key, limit=500)

    # 타임라인 — from_state→to_state + 다음 이벤트까지의 duration
    parsed: list[tuple[datetime | None, dict[str, Any]]] = []
    for e in events:
        at = _parse_ts(e.get("event_at")) or _parse_ts(e.get("created_at"))
        parsed.append((at, e))
    timeline: list[dict[str, Any]] = []
    for i, (at, e) in enumerate(parsed):
        nxt = parsed[i + 1][0] if i + 1 < len(parsed) else None
        duration = (
            round((nxt - at).total_seconds(), 1)
            if at is not None and nxt is not None and nxt > at
            else None
        )
        text = str(e.get("doing") or e.get("ask") or "")[:160]
        timeline.append(
            {
                "at": (
                    at.isoformat() if at else str(e.get("event_at") or e.get("created_at") or "")
                ),
                "from_state": str(parsed[i - 1][1].get("status") or "") if i > 0 else "",
                "to_state": str(e.get("status") or ""),
                "text": text,
                "duration_s": duration,
            }
        )

    state_counts = Counter(str(e.get("status") or "") for e in events)
    decisions = sum(1 for e in events if e.get("status") == "decision")
    # 승인 = decision 상태로 들어간 뒤 다른 상태로 빠져나간 횟수(응답된 ask)
    approvals = 0
    for i, e in enumerate(events):
        if e.get("status") != "decision":
            continue
        if i + 1 < len(events) and events[i + 1].get("status") != "decision":
            approvals += 1
    errors = sum(1 for e in events if str(e.get("status") or "") in ("error", "failed", "blocked"))
    tin = int(sess.get("tokens_in") or 0)
    tout = int(sess.get("tokens_out") or 0)
    has_tokens = sess.get("tokens_in") is not None or sess.get("tokens_out") is not None
    est_cost = session_est_cost_usd(sess) if has_tokens else None
    first_at = parsed[0][0] if parsed else None
    last_at = parsed[-1][0] if parsed else None
    if first_at is None:
        first_at = _parse_ts(sess.get("created_at"))
    if last_at is None:
        last_at = _parse_ts(sess.get("updated_at")) or _parse_ts(sess.get("last_event_at"))
    total_seconds = (
        round((last_at - first_at).total_seconds(), 1)
        if first_at is not None and last_at is not None and last_at > first_at
        else 0.0
    )

    # files_touched — ended_summary/artifacts의 경로 목록 (최대 50)
    files_touched: list[str] = []
    raw_artifacts = sess.get("artifacts")
    if raw_artifacts:
        try:
            items = json.loads(str(raw_artifacts))
        except (ValueError, TypeError):
            items = [raw_artifacts]
        if isinstance(items, dict):
            items = list(items.keys())
        if isinstance(items, list):
            files_touched = [str(x)[:300] for x in items[:50]]

    # 결정적 한국어 요약 — LLM 호출 없음
    tool = str(sess.get("tool") or "")
    parts = [f"이 세션({tool})은 {_fmt_duration(total_seconds)} 동안"]
    state_str = "·".join(
        f"{k} {state_counts[k]}회"
        for k in ("working", "decision", "rest", "ended")
        if state_counts.get(k)
    )
    if state_str:
        parts.append(f"{state_str}의 상태 전이를 거쳤고")
    else:
        parts.append("전이 이벤트가 거의 없었고")
    token_bits = []
    if tin:
        token_bits.append(f"토큰 입력 약 {tin / 1000:.1f}k")
    if tout:
        token_bits.append(f"출력 약 {tout / 1000:.1f}k")
    if token_bits:
        parts.append(", ".join(token_bits))
    if est_cost is not None:
        parts.append(f"예상 비용 ${est_cost:.4f}")
    if decisions or approvals or errors:
        extras = []
        if decisions:
            extras.append(f"결정 요청 {decisions}회")
        if approvals:
            extras.append(f"승인 응답 {approvals}회")
        if errors:
            extras.append(f"오류 {errors}건")
        parts.append(". " + ", ".join(extras) + ".")
    else:
        parts.append(".")
    summary = "".join(parts)

    return {
        "session": session_view(sess),
        # 구 이벤트 포맷 호환 — 기존 GET /{tool}/{key}/history 소비자용
        "events": events,
        "timeline": timeline,
        "stats": {
            "state_counts": dict(state_counts),
            "decisions": decisions,
            "approvals": approvals,
            "errors": errors,
            "tokens_in": tin,
            "tokens_out": tout,
            "est_cost_usd": est_cost,
            "total_seconds": total_seconds,
        },
        "files_touched": files_touched,
        "summary": summary,
        "ended_summary": sess.get("ended_summary") or "",
    }


# ---------- 리스크 탐지 (GET /risks) ----------

_SEV_RANK = {"high": 0, "medium": 1, "low": 2}


def _risk(
    rtype: str,
    severity: str,
    title: str,
    detail: str,
    ref_kind: str,
    ref_id: str,
    at: Any,
    now: datetime,
    i18n: dict[str, Any] | None = None,
) -> dict[str, Any]:
    age = _age_seconds(at, now)
    out: dict[str, Any] = {
        "type": rtype,
        "severity": severity,
        "title": title,
        "detail": detail,
        "ref_kind": ref_kind,
        "ref_id": ref_id,
        "at": str(at or ""),
        "age_seconds": round(age, 1) if age is not None else None,
    }
    if i18n:
        # 프론트 i18n용 — title/detail의 언어별 템플릿 키+파라미터.
        # 한국어 title/detail은 리포트/하위호환용으로 그대로 유지한다.
        out["i18n"] = i18n
    return out


def detect_risks(store: Store) -> dict[str, Any]:
    """GET /risks — 라이브 계산, dedupe, 심각도+최신순 최대 50건."""
    now = _now()
    items: list[dict[str, Any]] = []

    # 1) stage_failures — 같은 workflow+stage가 7일 내 서로 다른 run에서 ≥2회 실패
    since_7d = (now - timedelta(days=7)).isoformat()
    stage_rows = store.stage_run_rows(since_7d)
    latest: dict[tuple[str, str, str], tuple[int, str]] = {}
    run_meta: dict[str, dict[str, Any]] = {}
    for row in stage_rows:
        wf = str(row.get("workflow_key") or "")
        rid = str(row.get("run_id") or "")
        skey = str(row.get("stage_key") or "")
        run_meta[rid] = row
        key3 = (wf, rid, skey)
        attempt = int(row.get("attempt") or 0)
        if key3 not in latest or attempt > latest[key3][0]:
            latest[key3] = (attempt, str(row.get("status") or ""))
    fail_groups: dict[tuple[str, str], dict[str, Any]] = {}
    for (wf, rid, skey), (_a, st) in latest.items():
        if st != "failed":
            continue
        g = fail_groups.setdefault((wf, skey), {"runs": [], "latest_at": ""})
        g["runs"].append(rid)
        meta = run_meta.get(rid) or {}
        fail_at = str(meta.get("run_updated_at") or meta.get("started_at") or "")
        if fail_at > g["latest_at"]:
            g["latest_at"] = fail_at
    for (wf, skey), g in fail_groups.items():
        n = len(set(g["runs"]))
        if n < 2:
            continue
        severity = "high" if n >= 3 else "medium"
        latest_rid = sorted(set(g["runs"]))[-1]
        items.append(
            _risk(
                "stage_failure",
                severity,
                f"{wf} · {skey} 단계 반복 실패",
                f"최근 7일 {n}개 run에서 '{skey}' 단계가 실패했습니다.",
                "run",
                latest_rid,
                g["latest_at"],
                now,
                i18n={
                    "title_key": "risk.stage_failure.title",
                    "title_params": {"wf": wf, "stage": skey},
                    "detail_key": "risk.stage_failure.detail",
                    "detail_params": {"n": n, "stage": skey},
                },
            )
        )

    # 2) stale_approval — waiting_* 상태가 4h 초과
    for r in store.list_runs(limit=200):
        st = str(getattr(r, "status", "") or "")
        if st not in _APPROVAL_STATES:
            continue
        age = _age_seconds(getattr(r, "updated_at", None), now)
        if age is None or age < 4 * 3600:
            continue
        severity = "high" if age > 24 * 3600 else "medium"
        task = ""
        if isinstance(getattr(r, "input", None), dict):
            task = str(r.input.get("task") or "")[:60]
        items.append(
            _risk(
                "stale_approval",
                severity,
                f"승인 대기 지연: {getattr(r, 'workflow_key', '')}",
                f"{task or r.id} — {_fmt_duration(age)}째 승인 대기 중입니다.",
                "run",
                r.id,
                getattr(r, "updated_at", None),
                now,
                i18n={
                    "title_key": "risk.stale_approval.title",
                    "title_params": {"wf": str(getattr(r, "workflow_key", "") or "")},
                    "detail_key": "risk.stale_approval.detail",
                    "detail_params": {"task": task or r.id, "dur_s": round(age)},
                },
            )
        )

    # 3) idle_session — health가 stale/long_idle
    for s in store.list_agent_sessions(limit=300, include_ended=True):
        health = session_health(s)
        if health not in ("stale", "long_idle"):
            continue
        severity = "medium" if health == "stale" else "low"
        sess_at = s.get("last_event_at") or s.get("updated_at")
        items.append(
            _risk(
                "idle_session",
                severity,
                f"세션 {health}: {s.get('tool') or ''} {s.get('session_key', '')[:12]}",
                f"상태 {s.get('status')} — 마지막 이벤트 "
                f"{_fmt_duration(_age_seconds(sess_at, now) or 0)} 전.",
                "session",
                str(s.get("session_key") or ""),
                sess_at,
                now,
                i18n={
                    "title_key": "risk.idle_session.title",
                    "title_params": {
                        "health": health,
                        "tool": str(s.get("tool") or ""),
                        "key": str(s.get("session_key") or "")[:12],
                    },
                    "detail_key": "risk.idle_session.detail",
                    "detail_params": {
                        "status": str(s.get("status") or ""),
                        "dur_s": round(_age_seconds(sess_at, now) or 0),
                    },
                },
            )
        )

    # 4) cost_spike — 오늘 추정 비용 > 3× 7일 일평균 (그리고 >$1)
    cutoff_8d = (now - timedelta(days=8)).isoformat()
    daily_cost: dict[str, float] = {}
    for row in store.llm_usage_rows(cutoff_8d):
        day = str(row.get("created_at") or "")[:10]
        if not day:
            continue
        daily_cost[day] = daily_cost.get(day, 0.0) + cost_usd(
            str(row.get("model") or ""),
            int(row.get("tokens_in") or 0),
            int(row.get("tokens_out") or 0),
        )
    today_key = now.date().isoformat()
    today_cost = daily_cost.get(today_key, 0.0)
    prev7 = [
        v
        for d, v in daily_cost.items()
        if d != today_key and d >= (now - timedelta(days=7)).date().isoformat()
    ]
    avg7 = sum(prev7) / len(prev7) if prev7 else 0.0
    if today_cost > 1.0 and today_cost > 3 * avg7:
        items.append(
            _risk(
                "cost_spike",
                "medium",
                "오늘 LLM 비용 급증",
                f"오늘 추정 비용 ${today_cost:.2f} — 7일 일평균 ${avg7:.2f}의 3배 초과.",
                "cost",
                "",
                now.isoformat(),
                now,
                i18n={
                    "title_key": "risk.cost_spike.title",
                    "detail_key": "risk.cost_spike.detail",
                    "detail_params": {
                        "today": f"{today_cost:.2f}",
                        "avg": f"{avg7:.2f}",
                    },
                },
            )
        )

    # 5) trigger_failures — last_error가 남아있는 트리거
    for t in store.list_triggers():
        err_msg = str(t.get("last_error") or "").strip()
        if not err_msg:
            continue
        trig_at = t.get("last_fired_at") or t.get("created_at")
        items.append(
            _risk(
                "trigger_failure",
                "medium",
                f"트리거 오류: {t.get('id')}",
                f"마지막 발화 시 오류 — {err_msg[:160]}",
                "trigger",
                str(t.get("id") or ""),
                trig_at,
                now,
                i18n={
                    "title_key": "risk.trigger_failure.title",
                    "title_params": {"id": str(t.get("id") or "")},
                    "detail_key": "risk.trigger_failure.detail",
                    "detail_params": {"err": err_msg[:160]},
                },
            )
        )

    # 6) budget — 월 누적 비용 > monthly_budget_usd pref
    prefs = store.get_state("prefs", {})
    budget_raw = prefs.get("monthly_budget_usd") if isinstance(prefs, dict) else None
    try:
        budget = float(budget_raw) if budget_raw is not None else None
    except (TypeError, ValueError):
        budget = None
    if budget is not None and budget > 0:
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        mtd = 0.0
        for row in store.llm_usage_rows(month_start.isoformat()):
            mtd += cost_usd(
                str(row.get("model") or ""),
                int(row.get("tokens_in") or 0),
                int(row.get("tokens_out") or 0),
            )
        if mtd > budget:
            items.append(
                _risk(
                    "budget",
                    "high",
                    "월 예산 초과",
                    f"이번 달 추정 비용 ${mtd:.2f} > 예산 ${budget:.2f}.",
                    "cost",
                    "",
                    now.isoformat(),
                    now,
                    i18n={
                        "title_key": "risk.budget.title",
                        "detail_key": "risk.budget.detail",
                        "detail_params": {
                            "mtd": f"{mtd:.2f}",
                            "budget": f"{budget:.2f}",
                        },
                    },
                )
            )

    # 정렬: 심각도 → 최신순, 최대 50, type+ref 중복 제거
    seen: set[tuple[str, str, str]] = set()
    deduped: list[dict[str, Any]] = []
    for it in sorted(
        items,
        key=lambda x: (_SEV_RANK.get(str(x["severity"]), 3), -(x["age_seconds"] or 0)),
    ):
        k = (it["type"], it["ref_kind"], it["ref_id"])
        if k in seen:
            continue
        seen.add(k)
        deduped.append(it)
        if len(deduped) >= _MAX_ITEMS:
            break
    return {"items": deduped}


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


# ---------- 성과 비교 (GET /perf/compare) ----------


def _stage_agent_map(workflow_key: str) -> dict[str, str]:
    """stage_key → agent spec key (WORKFLOWS 기준)."""
    try:
        from .workflows import WORKFLOWS
    except Exception:
        return {}
    spec = WORKFLOWS.get(workflow_key)
    if spec is None:
        return {}
    return {s.key: (s.agent or "") for s in spec.stages}


def _dominant_model(models: list[str]) -> str:
    """run의 stage model 중 최빈값 (동률이면 먼저 등장)."""
    counts: Counter[str] = Counter(m for m in models if m)
    if not counts:
        return ""
    best = counts.most_common()
    top = best[0][1]
    for m, c in best:
        if c == top:
            return m
    return best[0][0]


def perf_compare(store: Store, workflow: str = "", group_by: str = "model") -> dict[str, Any]:
    """GET /perf/compare — run을 model/agent/tool로 그룹핑한 성공률/비용 비교."""
    group_by = group_by if group_by in ("model", "agent", "tool") else "model"
    runs = store.list_runs(limit=500, workflow=workflow or None)
    run_ids = {r.id for r in runs}
    created_dts = [
        d for d in (_parse_ts(getattr(r, "created_at", None)) for r in runs) if d is not None
    ]
    oldest = min(created_dts) if created_dts else None
    since = (
        (oldest - timedelta(days=1)).isoformat()
        if oldest
        else (_now() - timedelta(days=400)).isoformat()
    )
    stage_rows = [r for r in store.stage_run_rows(since) if str(r.get("run_id")) in run_ids]
    by_run: dict[str, list[dict[str, Any]]] = {}
    for r in stage_rows:
        by_run.setdefault(str(r["run_id"]), []).append(r)

    groups: dict[str, dict[str, Any]] = {}
    for run in runs:
        stages = by_run.get(run.id, [])
        models = [str(s.get("model") or "") for s in stages]
        if group_by == "model":
            group = _dominant_model(models) or "(unknown)"
        elif group_by == "agent":
            agent_map = _stage_agent_map(run.workflow_key)
            # 토큰 최다 stage의 agent — 없으면 첫 llm stage, 그래도 없으면 unknown
            best_stage = max(
                stages,
                key=lambda s: int(s.get("tokens_in") or 0) + int(s.get("tokens_out") or 0),
                default=None,
            )
            group = ""
            if best_stage is not None:
                group = agent_map.get(str(best_stage.get("stage_key") or ""), "")
            if not group:
                for s in stages:
                    group = agent_map.get(str(s.get("stage_key") or ""), "")
                    if group:
                        break
            group = group or "(unknown)"
        else:  # tool — 생성 주체 분류 (trigger/schedule/api-key/user)
            cb = str(getattr(run, "created_by", "") or "")
            if cb.startswith("trigger:"):
                group = "trigger"
            elif cb.startswith("schedule:"):
                group = "schedule"
            elif cb.startswith("radar"):
                group = "radar"
            elif cb in ("api-key", "local", ""):
                group = cb or "(system)"
            elif "@" in cb:
                group = "user"
            else:
                group = cb[:40] or "(system)"

        g = groups.setdefault(
            group,
            {
                "group": group,
                "run_count": 0,
                "succeeded": 0,
                "failed": 0,
                "durations": [],
                "tokens": 0,
                "cost": 0.0,
                "failures_by_stage": {},
            },
        )
        g["run_count"] += 1
        st = str(getattr(run, "status", "") or "")
        if st in _TERMINAL_OK:
            g["succeeded"] += 1
        elif st in _TERMINAL_BAD:
            g["failed"] += 1
        created = _parse_ts(getattr(run, "created_at", None))
        updated = _parse_ts(getattr(run, "updated_at", None))
        if st in (_TERMINAL_OK | _TERMINAL_BAD) and created and updated:
            g["durations"].append((updated - created).total_seconds())

        # run 단위 stage 집계 — 최신 attempt 실패만 failures_by_stage에 반영
        latest_attempt: dict[str, tuple[int, str]] = {}
        for s in stages:
            skey = str(s.get("stage_key") or "")
            att = int(s.get("attempt") or 0)
            if skey not in latest_attempt or att > latest_attempt[skey][0]:
                latest_attempt[skey] = (att, str(s.get("status") or ""))
            g["tokens"] += int(s.get("tokens_in") or 0) + int(s.get("tokens_out") or 0)
            g["cost"] += cost_usd(
                str(s.get("model") or ""),
                int(s.get("tokens_in") or 0),
                int(s.get("tokens_out") or 0),
            )
        for skey, (_a, stt) in latest_attempt.items():
            if stt == "failed":
                g["failures_by_stage"][skey] = g["failures_by_stage"].get(skey, 0) + 1

    items: list[dict[str, Any]] = []
    for g in groups.values():
        terminal = g["succeeded"] + g["failed"]
        items.append(
            {
                "group": g["group"],
                "run_count": g["run_count"],
                "success_rate": (round(g["succeeded"] / terminal * 100, 1) if terminal else None),
                "avg_seconds": (
                    round(sum(g["durations"]) / len(g["durations"]), 1) if g["durations"] else None
                ),
                "total_tokens": g["tokens"],
                "total_cost_usd": round(g["cost"], 6),
                "failures_by_stage": g["failures_by_stage"],
            }
        )
    items.sort(key=lambda x: (-x["run_count"], x["group"]))

    # 보조 섹션 — tool별 세션 통계 (agent_session_stats 재사용)
    sess_stats = store.agent_session_stats()
    tool_sessions: dict[str, dict[str, int]] = {}
    for s in store.list_agent_sessions(limit=500, include_ended=True):
        t = str(s.get("tool") or "")
        b = tool_sessions.setdefault(t, {"session_count": 0, "active": 0, "ended": 0})
        b["session_count"] += 1
        if str(s.get("status") or "") == "ended":
            b["ended"] += 1
        elif session_health(s) == "active":
            b["active"] += 1
    session_items: list[dict[str, Any]] = []
    for tool, b in tool_sessions.items():
        tool_stats = sess_stats.get(tool) or {}
        work = int(tool_stats.get("working_seconds") or 0)
        session_items.append(
            {
                "tool": tool,
                "session_count": b["session_count"],
                "active": b["active"],
                "ended": b["ended"],
                "avg_work_seconds": round(work / max(b["session_count"], 1), 1),
            }
        )
    session_items.sort(key=lambda x: (-x["session_count"], x["tool"]))

    return {
        "workflow": workflow or "",
        "group_by": group_by,
        "items": items,
        "sessions": {"items": session_items},
    }
