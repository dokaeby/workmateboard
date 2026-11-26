"""대시보드 사이드바 뷰용 읽기 전용 집계 — /insights, /automation/overview,
/archive, /developer/status 엔드포인트의 순수 집계 로직.

모든 함수는 Store(읽기)와 env/설정만 사용하며 상태를 변경하지 않는다.
비밀값(토큰·키·webhook URL)은 절대 반환하지 않고 configured 여부(bool)만
노출한다.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .pricing import cost_usd

if TYPE_CHECKING:
    from .config import Settings
    from .store.db import Store


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _clamp_days(days: int) -> int:
    return max(1, min(int(days), 365))


# ---------- GET /api/v1/insights ----------


def insights_summary(store: Store, workflow_keys: list[str], days: int) -> dict[str, Any]:
    """인사이트 뷰 — 세션/런/비용/실패 집계.

    success_rate = succeeded / (succeeded + failed) — 종료된 run만 분모,
    종료 run이 0이면 None. by_workflow는 workflow별 run_stats 재사용."""
    days = _clamp_days(days)
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()

    sess = store.session_work_seconds(cutoff)
    by_tool = [
        {
            "tool": tool,
            "sessions": int(b.get("sessions") or 0),
            "working_seconds": int(b.get("working_seconds") or 0),
            "decisions": int(b.get("decisions") or 0),
        }
        for tool, b in sess["tools"].items()
    ]
    by_tool.sort(key=lambda x: (-x["working_seconds"], x["tool"]))
    by_project = [
        {
            "project": project,
            "sessions": int(b.get("sessions") or 0),
            "working_seconds": int(b.get("working_seconds") or 0),
        }
        for project, b in sess["projects"].items()
    ]
    by_project.sort(key=lambda x: (-x["working_seconds"], x["project"]))
    total_working = sum(int(b.get("working_seconds") or 0) for b in sess["tools"].values())

    stats = store.run_stats(None, days)
    succeeded = int(stats.get("succeeded") or 0)
    failed = int(stats.get("failed") or 0)
    terminal = succeeded + failed
    stage_failures = stats.get("stage_failures") or {}
    top_failures = [
        {"stage_key": k, "count": int(v)}
        for k, v in sorted(stage_failures.items(), key=lambda kv: (-int(kv[1]), str(kv[0])))
    ]

    by_workflow: list[dict[str, Any]] = []
    for key in workflow_keys:
        ws = store.run_stats(key, days)
        w_succ = int(ws.get("succeeded") or 0)
        w_fail = int(ws.get("failed") or 0)
        w_terminal = w_succ + w_fail
        by_workflow.append(
            {
                "workflow": key,
                "runs": int(ws.get("runs") or 0),
                "success_rate": (round(w_succ / w_terminal, 4) if w_terminal else None),
            }
        )

    total_cost = 0.0
    tokens_in = tokens_out = 0
    for r in store.llm_usage_rows(cutoff):
        tin = int(r.get("tokens_in") or 0)
        tout = int(r.get("tokens_out") or 0)
        total_cost += cost_usd(str(r.get("model") or ""), tin, tout)
        tokens_in += tin
        tokens_out += tout

    return {
        "days": days,
        "sessions": {
            "by_tool": by_tool,
            "by_project": by_project,
            "total_sessions": int(sess.get("total_sessions") or 0),
            "total_working_seconds": total_working,
        },
        "runs": {
            "total": int(stats.get("runs") or 0),
            "succeeded": succeeded,
            "failed": failed,
            "running": int(stats.get("running") or 0),
            "success_rate": (round(succeeded / terminal, 4) if terminal else None),
            "avg_duration_s": stats.get("avg_duration_s"),
            "by_workflow": by_workflow,
        },
        "cost": {
            "total_cost_usd": round(total_cost, 6),
            "llm_tokens_in": tokens_in,
            "llm_tokens_out": tokens_out,
        },
        "top_failures": top_failures,
    }


# ---------- GET /api/v1/automation/overview ----------


def _telegram_configured() -> bool:
    return bool(
        os.environ.get("JOBMATE_TELEGRAM_BOT_TOKEN", "").strip()
        and os.environ.get("JOBMATE_TELEGRAM_CHAT_ID", "").strip()
    )


def automation_overview(
    store: Store, notify_rules: Any, last_cron_tick_at: str | None
) -> dict[str, Any]:
    """자동화 허브 뷰 — 스케줄/알림 규칙/cron 틱/메신저/리포트 상태.

    메신저 configured는 env 존재 여부(bool)만 — 값 자체는 절대 반환하지 않는다."""
    schedules = [
        {
            "id": s.get("id"),
            "workflow": s.get("workflow"),
            "interval_minutes": s.get("interval_minutes"),
            "enabled": bool(s.get("enabled")),
            "last_run_at": s.get("last_run_at"),
            "next_run_at": s.get("next_run_at"),
            "input": s.get("input") or {},
        }
        for s in store.list_schedules()
    ]
    last_daily = last_weekly = None
    for r in store.list_reports(limit=50):
        kind = str(r.get("kind") or "")
        if kind == "daily" and last_daily is None:
            last_daily = r.get("created_at")
        elif kind == "weekly" and last_weekly is None:
            last_weekly = r.get("created_at")
        if last_daily and last_weekly:
            break
    return {
        "schedules": schedules,
        "notify_rules": notify_rules if isinstance(notify_rules, list) else [],
        "cron": {"last_tick_at": last_cron_tick_at},
        "messenger": {
            "slack": bool(os.environ.get("JOBMATE_SLACK_WEBHOOK_URL", "").strip()),
            "telegram": _telegram_configured(),
        },
        "reports": {"last_daily": last_daily, "last_weekly": last_weekly},
        "generated_at": _iso_now(),
    }


# ---------- GET /api/v1/archive ----------

_ARCHIVE_KINDS = ("report", "run", "session")


def _session_artifacts_text(raw: Any) -> str:
    """agent_sessions.artifacts(JSON 문자열) → 표시용 텍스트."""
    if not raw:
        return ""
    try:
        items = json.loads(str(raw))
    except (ValueError, TypeError):
        return str(raw)[:200]
    if isinstance(items, list):
        return ", ".join(str(x)[:120] for x in items[:5])
    return str(items)[:200]


def archive_search(store: Store, query: str, kind: str, limit: int) -> dict[str, Any]:
    """리포트/run/종료 세션을 아카이브로 검색.

    q는 title+snippet에 대한 대소문자 무시 부분 문자열 매칭(데이터셋이 작아
    Python 필터 사용 — DB 방언 차이 없음). kind가 비어 있으면 전체.
    link는 프론트 뷰 힌트: report→"reports", run→"runs", session→"office"."""
    limit = max(1, min(int(limit), 200))
    needle = (query or "").strip().casefold()
    items: list[dict[str, Any]] = []

    if kind in ("", "report"):
        for r in store.list_reports(limit=200):
            rkind = str(r.get("kind") or "")
            created = str(r.get("created_at") or "")
            label = "일일" if rkind == "daily" else "주간" if rkind == "weekly" else rkind
            title = f"{label} 리포트 {created[:10]}".strip()
            snippet = f"{rkind} · 생성 {created}"
            items.append(
                {
                    "kind": "report",
                    "id": str(r.get("id") or ""),
                    "title": title,
                    "snippet": snippet,
                    "created_at": created,
                    "link": "reports",
                }
            )

    if kind in ("", "run"):
        for run in store.list_runs(limit=200):
            task = str((run.input or {}).get("task") or "")
            title = f"{run.workflow_key}: {task[:60]}" if task else run.workflow_key
            created = run.created_at.isoformat() if run.created_at else ""
            snippet = f"{run.status.value} · {created}"
            items.append(
                {
                    "kind": "run",
                    "id": run.id,
                    "title": title,
                    "snippet": snippet,
                    "created_at": created,
                    "link": "runs",
                }
            )

    if kind in ("", "session"):
        for s in store.list_agent_sessions(limit=300, include_ended=True):
            if str(s.get("status") or "") != "ended":
                continue
            summary = str(s.get("ended_summary") or "").strip()
            artifacts = _session_artifacts_text(s.get("artifacts"))
            if not summary and not artifacts:
                continue
            tool = str(s.get("tool") or "")
            project = str(s.get("project") or "")
            skey = str(s.get("session_key") or "")
            title = " · ".join(x for x in (tool, project or skey[:12]) if x)
            snippet = summary or artifacts
            created = str(
                s.get("updated_at") or s.get("last_event_at") or s.get("created_at") or ""
            )
            items.append(
                {
                    "kind": "session",
                    "id": f"{tool}:{skey}",
                    "title": title,
                    "snippet": snippet[:300],
                    "created_at": created,
                    "link": "office",
                }
            )

    if needle:
        items = [it for it in items if needle in f"{it['title']} {it['snippet']}".casefold()]
    items.sort(key=lambda it: str(it.get("created_at") or ""), reverse=True)
    return {"items": items[:limit], "query": query or ""}


# ---------- GET /api/v1/developer/status ----------


def developer_status(store: Store, settings: Settings, adapters: dict[str, Any]) -> dict[str, Any]:
    """개발자 뷰 — 연동/인증 구성 상태.

    configured는 bool만 — 토큰·키·URL 원문은 절대 반환하지 않는다."""
    from .security import admin_emails, api_key

    def _env(name: str) -> bool:
        return bool(os.environ.get(name, "").strip())

    llm_default_ok = _env(settings.llm_api_key_env)
    codex_ok = (
        _env(settings.codex_api_key_env) or Path(settings.codex_auth_path).expanduser().is_file()
    )
    bridge = store.agent_sessions_bridge_info()

    integrations = [
        {
            "name": "gmail",
            "configured": bool(adapters.get("gmail")),
            "note": "Gmail 토큰 파일이나 env JSON이 있으면 수집됩니다",
        },
        {
            "name": "calendar",
            "configured": bool(adapters.get("calendar")),
            "note": "Google Calendar 토큰 파일이나 env JSON을 설정하면 활성화됩니다",
        },
        {
            "name": "github_radar",
            "configured": _env(settings.github_token_env),
            "note": "GitHub API 토큰으로 skillRadar 저장소를 수집합니다",
        },
        {
            "name": "slack",
            "configured": _env("JOBMATE_SLACK_WEBHOOK_URL"),
            "note": "Slack Incoming Webhook으로 알림과 커맨드를 처리합니다",
        },
        {
            "name": "telegram",
            "configured": _telegram_configured(),
            "note": "Telegram 봇 토큰과 chat_id로 알림과 커맨드를 처리합니다",
        },
        {
            "name": "llm_default",
            "configured": llm_default_ok,
            "note": f"기본 LLM ({settings.default_model}), {settings.llm_api_key_env} 사용",
        },
        {
            "name": "llm_codex",
            "configured": codex_ok,
            "note": f"Codex important 티어 ({settings.important_model})",
        },
        {
            "name": "codex_bridge",
            "configured": bool(bridge.get("last_push_at")),
            "note": "agent-bridge의 최근 push 여부 (agent_sessions)",
        },
    ]
    return {
        "integrations": integrations,
        "auth": {
            "google": _env("GOOGLE_CLIENT_ID"),
            "password": True,
            "api_key": bool(api_key()),
            "admin_emails_configured": bool(admin_emails()),
        },
        "generated_at": _iso_now(),
    }
