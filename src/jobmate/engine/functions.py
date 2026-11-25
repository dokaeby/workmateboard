"""function 단계 구현 — 모델 없이 결정적 검사/외부 부수효과."""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..models import LinkStatus, NotificationStatus
from ..sensors import (
    build_gmail_url,
    git_apply_check,
    idempotency_key,
    is_safe_source_url,
    looks_like_diff,
    scan_secrets,
    validate_event,
)

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


def auto_checks(ctx: FunctionContext) -> dict[str, Any]:
    """구현 산출물의 결정적 검사. diff 형식/시크릿/apply check/설정 명령."""
    impl = _prev_output(ctx, "implement")
    diff = impl.get("diff", "")
    checks: list[dict[str, Any]] = []
    passed = True

    if not diff.strip():
        checks.append({"name": "diff_present", "passed": False, "detail": "empty diff"})
        passed = False
    else:
        checks.append({"name": "diff_present", "passed": True})
        if not looks_like_diff(diff):
            checks.append(
                {"name": "diff_format", "passed": False, "detail": "no 'diff --git' header"}
            )
            passed = False
        else:
            checks.append({"name": "diff_format", "passed": True})
        ok, detail = git_apply_check(diff)
        checks.append({"name": "git_apply_check", "passed": ok, "detail": detail})
        passed = passed and ok

    hits = scan_secrets(diff)
    checks.append(
        {"name": "secret_scan", "passed": not hits, "detail": ",".join(hits) if hits else "clean"}
    )
    passed = passed and not hits

    for cmd in ctx.settings.check_commands:
        try:
            proc = subprocess.run(  # noqa: S603
                cmd.split(),
                capture_output=True,
                text=True,
                timeout=120,
                cwd=Path.cwd(),
            )
            ok = proc.returncode == 0
        except (OSError, subprocess.TimeoutExpired) as e:
            ok, proc = False, None
            detail = str(e)
        else:
            detail = (proc.stdout + proc.stderr)[-500:] if proc else ""
        checks.append({"name": f"cmd:{cmd}", "passed": ok, "detail": detail})
        passed = passed and ok

    return {"passed": passed, "checks": checks}


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


def _event_key(title: str, start_at: str, end_at: str, timezone: str = "Asia/Seoul") -> str:
    """calendar-agent event_key 포팅 — 동일 일정 판별용 정규화 해시."""
    import hashlib
    import json
    from datetime import UTC
    from zoneinfo import ZoneInfo

    def aware(v: str) -> str:
        dt = datetime.fromisoformat(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo(timezone))
        return dt.astimezone(UTC).isoformat()

    data = [" ".join(title.casefold().split()), aware(start_at), aware(end_at)]
    return hashlib.sha256(json.dumps(data).encode()).hexdigest()


def calendar_conflict_check(ctx: FunctionContext) -> dict[str, Any]:
    """calendar-agent RegistrationService의 중복/충돌 검사 (결정적)."""
    validated = _prev_output(ctx, "validate")
    ev = validated.get("event", {})
    if not ev:
        return {"status": "invalid"}
    provider, source_id, conn, thread, _ = _src(ctx)
    cal = ctx.adapters.get("calendar")

    # 동일 소스가 이미 등록/확인 대기면 중복
    existing = ctx.store.get_scheduled_by_source(provider, source_id)
    if existing and existing.get("status") in ("registered", "needs_confirmation"):
        return {"status": "duplicate", "calendar_event_id": existing.get("calendar_event_id", "")}

    sched_id = f"{provider}:{source_id}"
    start_at, end_at = ev.get("start_at", ""), ev.get("end_at", "")
    tz = ev.get("timezone", "Asia/Seoul")

    matches: list[dict[str, Any]] = []
    if cal is not None and hasattr(cal, "search_events"):
        try:
            matches = cal.search_events(start_at, end_at)
        except Exception as e:
            ctx.store.audit(
                ctx.run_id, "tool", "calendar_search_error", "", {"error": str(e)[:200]}
            )
            logger.warning("calendar search_events failed run=%s: %s", ctx.run_id, str(e)[:200])
            matches = []

    key = _event_key(ev.get("title", ""), start_at, end_at, tz)
    for m in matches:
        if m.get("status") == "cancelled":
            continue
        # 같은 마커(우리 멱등 키)거나 동일 title+시간 → 자기 자신 (복구 경로)
        marker = m.get("marker", "")
        if marker == idempotency_key(f"{provider}:{conn}", source_id):
            return {"status": "self", "calendar_event_id": m.get("id", "")}
        try:
            ms = m.get("start", {}).get("dateTime", "")
            me = m.get("end", {}).get("dateTime", "")
            mkey = _event_key(m.get("summary", ""), ms, me, tz)
        except Exception:
            mkey = ""
        if mkey and mkey == key:
            return {"status": "self", "calendar_event_id": m.get("id", "")}

    conflict = any(
        m.get("status") != "cancelled" and m.get("transparency") != "transparent" for m in matches
    )
    status = "conflict" if conflict else "clear"
    # 후보 레지스트리 기록 — 사람 확인 큐의 원천
    ctx.store.save_scheduled_event(
        event_id=sched_id,
        source_provider=provider,
        source_id=source_id,
        thread_id=thread,
        title=ev.get("title", ""),
        start_at=start_at,
        end_at=end_at,
        timezone=tz,
        location=ev.get("location", ""),
        summary=ev.get("summary", ""),
        status="needs_confirmation" if conflict else "candidate",
        confidence=float(ev.get("confidence", 0.0)),
        reason="calendar_conflict" if conflict else "",
        source_refs=source_id,
    )
    return {"status": status, "match_count": len(matches)}


def gmail_validate(ctx: FunctionContext) -> dict[str, Any]:
    """추출 후보를 결정적 검증한다."""
    ext = _prev_output(ctx, "extract")
    from ..models import ExtractedEvent

    try:
        ev = ExtractedEvent.model_validate(ext.get("event", ext))
    except Exception as e:
        return {"status": "invalid", "reason": str(e)[:200]}
    ok, reason = validate_event(ev)
    if not ok:
        # 검토 큐(scheduled_events)에 needs_review로 남겨 사람이 승인/수정할 수 있게 한다
        provider, source_id, _, thread, _ = _src(ctx)
        ctx.store.save_scheduled_event(
            event_id=f"{provider}:{source_id}",
            source_provider=provider,
            source_id=source_id,
            thread_id=thread,
            title=ev.title,
            start_at=ev.start_at,
            end_at=ev.end_at,
            timezone=ev.timezone,
            location=ev.location,
            summary=ev.summary,
            status="needs_review",
            confidence=ev.confidence,
            reason=reason,
            source_refs=source_id,
        )
    return {
        "status": "validated" if ok else "needs_review",
        "reason": reason,
        "event": ev.model_dump(),
    }


def gmail_register(ctx: FunctionContext) -> dict[str, Any]:
    """멱등 Calendar 등록 — pending 예약→marker 조회→생성→ID 저장.
    멀티채널: state의 source_provider/source_id 사용 (기본 gmail)."""
    calendar = ctx.adapters.get("calendar")
    if calendar is None:
        return {"status": "no_adapter"}
    validated = _prev_output(ctx, "validate")
    ev = validated.get("event", {})
    provider, source_id, conn, thread, _ = _src(ctx)
    key = idempotency_key(f"{provider}:{conn}", source_id)

    _, _, conn, _, source_url = _src(ctx)
    link = ctx.store.reserve_link(key, source_id, conn, source_url)
    if link is None:
        # 기존 링크 — 완료됐으면 duplicate, pending이면 marker 복구 시도
        existing = ctx.store.get_link_by_key(key)
        if existing is None:
            return {"status": "duplicate_pending", "idempotency_key": key}
        if existing.calendar_event_id:
            return {
                "status": "duplicate",
                "calendar_event_id": existing.calendar_event_id,
                "idempotency_key": key,
            }
        found = calendar.find_by_marker(key)
        if found:
            ctx.store.update_link(
                existing.id, status=LinkStatus.CALENDAR_CREATED, calendar_event_id=found
            )
            ctx.store.audit(ctx.run_id, "tool", "calendar_recovered", key, {"event_id": found})
            return {"status": "recovered", "calendar_event_id": found, "idempotency_key": key}
        return {"status": "duplicate_pending", "idempotency_key": key}

    # pending 링크 확보 후 외부 호출 — 타임아웃 시 marker 조회로 복구
    try:
        existing = calendar.find_by_marker(key)
        if existing:
            ctx.store.update_link(
                link.id, status=LinkStatus.CALENDAR_CREATED, calendar_event_id=existing
            )
            return {"status": "recovered", "calendar_event_id": existing, "idempotency_key": key}
        event_id = calendar.create_event(
            title=ev.get("title", ""),
            start_at=ev.get("start_at", ""),
            end_at=ev.get("end_at", ""),
            timezone=ev.get("timezone", "Asia/Seoul"),
            location=ev.get("location", ""),
            marker=key,
        )
    except Exception as e:
        ctx.store.update_link(
            link.id, status=LinkStatus.RETRY_WAIT, last_error=str(e)[:300], attempts_inc=True
        )
        ctx.store.audit(ctx.run_id, "tool", "calendar_error", key, {"error": str(e)[:200]})
        logger.warning(
            "calendar create_event failed run=%s key=%s: %s", ctx.run_id, key, str(e)[:200]
        )
        return {"status": "retry_wait", "error": str(e)[:300]}

    ctx.store.update_link(link.id, status=LinkStatus.CALENDAR_CREATED, calendar_event_id=event_id)
    ctx.store.audit(ctx.run_id, "tool", "calendar_created", key, {"event_id": event_id})
    # scheduled_events 레지스트리 갱신 (candidate → registered)
    provider, source_id, _, thread, _ = _src(ctx)
    ctx.store.save_scheduled_event(
        event_id=f"{provider}:{source_id}",
        source_provider=provider,
        source_id=source_id,
        thread_id=thread,
        calendar_event_id=event_id,
        title=ev.get("title", ""),
        start_at=ev.get("start_at", ""),
        end_at=ev.get("end_at", ""),
        timezone=ev.get("timezone", "Asia/Seoul"),
        location=ev.get("location", ""),
        summary=ev.get("summary", ""),
        status="registered",
        confidence=float(ev.get("confidence", 0.0)),
        source_refs=source_id,
    )
    return {
        "status": "created",
        "calendar_event_id": event_id,
        "idempotency_key": key,
        "scheduled_event_id": f"{provider}:{source_id}",
    }


def gmail_notify(ctx: FunctionContext) -> dict[str, Any]:
    """알림 큐잉 + 발송. 실패해도 Calendar 이벤트는 유지된다."""
    reg = _prev_output(ctx, "register")
    link_key = reg.get("idempotency_key", "")
    link = ctx.store.get_link_by_key(link_key) if link_key else None
    if link is None:
        return {"status": "no_link"}
    if reg.get("status") == "duplicate" and link.status == LinkStatus.NOTIFIED:
        return {"status": "already_notified"}

    ev = _prev_output(ctx, "validate").get("event", {})
    _, _, _, _, source_url = _src(ctx)
    if not source_url or not is_safe_source_url(source_url):
        source_url = ""
    notifier = ctx.adapters.get("notifier")
    if notifier is None:
        return {"status": "no_adapter"}
    n = ctx.store.add_notification(
        title=f"일정 등록: {ev.get('title', '')}",
        body=ev.get("start_at", ""),
        source_url=source_url,
        run_id=ctx.run_id,
        link_id=link.id,
    )
    try:
        notifier.send(title=n.title, body=n.body, source_url=n.source_url)
        ctx.store.set_notification_status(n.id, NotificationStatus.SENT)
        ctx.store.update_link(link.id, status=LinkStatus.NOTIFIED, notification_id=n.id)
        jobs = _schedule_event_notifications(ctx, ev, reg)
        _send_web_push(ctx, n)
        return {"status": "notified", "notification_id": n.id, "notification_jobs": jobs}
    except Exception as e:
        ctx.store.set_notification_status(n.id, NotificationStatus.FAILED)
        ctx.store.update_link(
            link.id,
            status=LinkStatus.NOTIFICATION_QUEUED,
            notification_id=n.id,
            last_error=str(e)[:300],
        )
        ctx.store.audit(ctx.run_id, "tool", "notify_failed", link.id, {"error": str(e)[:200]})
        logger.warning("notify failed run=%s link=%s: %s", ctx.run_id, link.id, str(e)[:200])
        return {"status": "notify_failed", "notification_id": n.id, "error": str(e)[:300]}


def send_web_push_text(adapters: dict[str, Any], title: str, body: str, source_url: str) -> None:
    """webpush adapter best-effort 발송 — 실패는 warning으로만 남긴다."""
    push = adapters.get("webpush")
    if push is None:
        return
    try:
        push.send(title=title, body=body, source_url=source_url)
    except Exception as e:
        logger.warning("webpush send failed: %s", str(e)[:200])


def _send_web_push(ctx: FunctionContext, n: Any) -> None:
    """Web Push 채널 — best-effort. 실패해도 알림 상태는 SENT 유지."""
    push = ctx.adapters.get("webpush")
    if push is None:
        return
    try:
        push.send(title=n.title, body=n.body, source_url=n.source_url)
    except Exception as e:
        ctx.store.audit(ctx.run_id, "tool", "webpush_failed", str(n.id), {"error": str(e)[:200]})
        logger.warning("webpush send failed run=%s: %s", ctx.run_id, str(e)[:200])


def _schedule_event_notifications(
    ctx: FunctionContext, ev: dict[str, Any], reg: dict[str, Any]
) -> list[str]:
    """calendar-agent NotificationScheduler 포팅 — 시작 2h/1h 전 예약."""
    sched_id = reg.get("scheduled_event_id") or ""
    if not sched_id:
        provider, source_id, _, _, _ = _src(ctx)
        sched_id = f"{provider}:{source_id}"
    try:
        start = datetime.fromisoformat(ev.get("start_at", ""))
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
    except ValueError:
        return []
    now = datetime.now(UTC)
    jobs: list[str] = []
    # 90분 이내 등록이면 2h 알림 생략, 30분 이내면 1h 알림 생략 (원본 규칙)
    if (start - now).total_seconds() > 90 * 60:
        jobs.append(
            ctx.store.save_notification_job(
                sched_id, "push", (start - timedelta(hours=2)).isoformat()
            )
        )
    if (start - now).total_seconds() > 30 * 60:
        jobs.append(
            ctx.store.save_notification_job(
                sched_id, "email", (start - timedelta(hours=1)).isoformat()
            )
        )
    return jobs


# ---------- skillRadar (radar_*) ----------


def _radar_platforms(ctx: FunctionContext) -> list[str]:
    p = ctx.state.get("platform") or "all"
    if p and p != "all":
        return [p]
    return list(ctx.settings.radar_platforms)


def radar_collect(ctx: FunctionContext) -> dict[str, Any]:
    """GitHub search + README 수집. adapter 'github' 필요."""
    from ..radar import load_queries

    gh = ctx.adapters.get("github")
    if gh is None:
        return {"status": "no_adapter", "repos": []}
    platforms = _radar_platforms(ctx)
    queries = load_queries(ctx.settings.radar_queries_path)
    repos: list[dict[str, Any]] = []
    seen: set[str] = set()
    for platform in platforms:
        for q in queries.get(platform, []):
            for hit in gh.search_repositories(q, ctx.settings.radar_per_query):
                name = hit.get("full_name", "")
                if not name or name in seen:
                    continue
                seen.add(name)
                hit["platform"] = platform
                hit["readme"] = gh.fetch_readme(name)
                repos.append(hit)
    return {"status": "ok", "repos": repos, "platforms": platforms}


def radar_dedupe(ctx: FunctionContext) -> dict[str, Any]:
    """archived/fork/중복 제거 + stars 상위 TOP_K개만 유지 (skillRadar dedupe_filter)."""
    from ..radar import TOP_K

    repos = (_prev_output(ctx, "collect") or {}).get("repos", [])
    before = len(repos)
    best: dict[str, dict[str, Any]] = {}
    for r in repos:
        name = r.get("full_name", "")
        if not name or r.get("archived") or r.get("fork"):
            continue
        prev = best.get(name)
        if prev is None or int(r.get("stars", 0)) > int(prev.get("stars", 0)):
            best[name] = r
    out = sorted(best.values(), key=lambda x: int(x.get("stars", 0)), reverse=True)
    cap = min(ctx.settings.radar_max_repos, TOP_K)
    out = out[:cap]
    return {"status": "ok", "repos": out, "dropped": before - len(out)}


def _radar_platform_name(ctx: FunctionContext) -> str:
    p = ctx.state.get("platform")
    return p if p and p != "all" else "claude"


def _radar_llm_models(ctx: FunctionContext) -> tuple[Any, Any]:
    """(summarize용 default 티어, classify/verify용 judge=important 티어) ModelConfig."""
    return (
        ctx.settings.model_for("_radar", "default"),
        ctx.settings.model_for("_radar_judge", "important"),
    )


def radar_summarize(ctx: FunctionContext) -> dict[str, Any]:
    """sanitize → classify → 3개 언어 요약. content_hash 캐시 우선.

    skillRadar의 sanitize/classify/summarize 노드를 한 stage에 이식했다.
    """
    from ..radar import (
        classify_repo,
        content_hash,
        fallback_summaries_translated,
        new_metrics,
        sanitize_readme,
        summarize_repo,
    )

    repos = (_prev_output(ctx, "dedupe") or {}).get("repos", [])
    if not repos:
        return {"status": "ok", "repos": []}
    if ctx.llm is None:
        for r in repos:
            r["summary_status"] = "failed"
        return {"status": "no_llm", "repos": repos, "metrics": new_metrics()}

    sum_model, judge_model = _radar_llm_models(ctx)
    platform = _radar_platform_name(ctx)
    metrics = new_metrics()
    out_repos: list[dict[str, Any]] = []
    tokens = {"tokens_in": 0, "tokens_out": 0}
    for r in repos:
        item = dict(r)
        sanitized = sanitize_readme(r.get("readme"))
        item["sanitized_readme"] = sanitized
        item["readme_excerpt"] = sanitized[:3000]
        chash = content_hash(r.get("description"), sanitized)
        item["content_hash"] = chash
        name = r.get("full_name", "")
        cached = ctx.store.radar_cached_repo(name, chash)
        if cached:
            item.update(
                {
                    k: cached[k]
                    for k in (
                        "summary_en",
                        "summary_zh",
                        "summary_ko",
                        "taxonomy",
                        "taxonomy_confidence",
                        "summary_status",
                    )
                }
            )
            item["_summary_en_lines"] = (cached.get("summary_en") or "").splitlines()
            item["_cache_verified"] = cached.get("summary_status") == "ok"
            item["summary_source"] = "cache"
            metrics["cache_hits"]["summarize"] += 1
            metrics["cache_hits"]["classify"] += 1
            out_repos.append(item)
            continue
        try:
            cls = classify_repo(
                ctx.llm,
                judge_model,
                name=name,
                description=r.get("description"),
                topics=list(r.get("topics") or []),
                readme=sanitized,
                usage_acc=tokens,
            )
            metrics["llm_calls"] += 1
            item["taxonomy"] = cls.label
            item["taxonomy_confidence"] = cls.confidence
            summ, status = summarize_repo(
                ctx.llm,
                sum_model,
                platform=platform,
                full_name=name,
                description=r.get("description"),
                readme=sanitized,
                usage_acc=tokens,
            )
            metrics["llm_calls"] += 1
            if summ is None:
                fb = fallback_summaries_translated(r.get("description"), name)
                item["summary_en"] = "\n".join(fb["en"])
                item["summary_zh"] = "\n".join(fb["zh"])
                item["summary_ko"] = "\n".join(fb["ko"])
                item["_summary_en_lines"] = fb["en"]
                item["summary_status"] = "failed"
                metrics["summary_fallbacks"] += 1
            else:
                item["summary_en"] = "\n".join(summ.summary_en)
                item["summary_zh"] = "\n".join(summ.summary_zh)
                item["summary_ko"] = "\n".join(summ.summary_ko)
                item["_summary_en_lines"] = list(summ.summary_en)
                item["summary_status"] = status
            item["summary_source"] = "llm"
        except Exception as e:
            fb = fallback_summaries_translated(r.get("description"), name)
            item.update(
                {
                    "summary_en": "\n".join(fb["en"]),
                    "summary_zh": "\n".join(fb["zh"]),
                    "summary_ko": "\n".join(fb["ko"]),
                    "_summary_en_lines": fb["en"],
                    "taxonomy": "other",
                    "taxonomy_confidence": 0.0,
                    "summary_status": "failed",
                }
            )
            metrics["summary_fallbacks"] += 1
            item["summary_source"] = "fallback"
            ctx.store.audit(
                ctx.run_id,
                "tool",
                "radar_summarize_error",
                name,
                {"error": str(e)[:200]},
            )
            logger.warning(
                "radar summarize failed run=%s repo=%s: %s",
                ctx.run_id,
                name,
                str(e)[:200],
            )
        out_repos.append(item)
    metrics["repos_processed"] = len(out_repos)
    return {"status": "ok", "repos": out_repos, "metrics": metrics, **tokens}


def radar_verify(ctx: FunctionContext) -> dict[str, Any]:
    """요약 충실도 judge 검증. 실패 시 재요약 최대 2회, 초과 시 'unverified'.

    skillRadar의 verify 노드 포팅. 캐시로 검증 통과한 요약은 재검증하지 않는다.
    """
    from ..radar import (
        MAX_VERIFY_RETRIES,
        new_metrics,
        summarize_repo,
        verify_summary,
    )

    prev = _prev_output(ctx, "summarize") or {}
    repos = prev.get("repos", [])
    metrics = prev.get("metrics") or new_metrics()
    if not repos or ctx.llm is None:
        return {"status": "ok", "repos": repos, "metrics": metrics}

    _sum_model, judge_model = _radar_llm_models(ctx)
    platform = _radar_platform_name(ctx)
    retries: dict[str, int] = {}
    tokens = {"tokens_in": 0, "tokens_out": 0}

    for item in repos:
        name = item.get("full_name", "")
        if item.get("summary_status") != "ok":
            continue
        if item.get("_cache_verified"):
            metrics["cache_hits"]["verify"] += 1
            continue
        readme = item.get("sanitized_readme") or item.get("readme") or ""
        try:
            ok = verify_summary(
                ctx.llm,
                judge_model,
                readme=readme,
                summary_lines=item.get("_summary_en_lines") or [],
                usage_acc=tokens,
            )
            metrics["llm_calls"] += 1
            metrics["verify_calls"] += 1
            if ok:
                metrics["verify_passed"] += 1
            while not ok and retries.get(name, 0) < MAX_VERIFY_RETRIES:
                retries[name] = retries.get(name, 0) + 1
                summ, status = summarize_repo(
                    ctx.llm,
                    _sum_model,
                    platform=platform,
                    full_name=name,
                    description=item.get("description"),
                    readme=readme,
                    usage_acc=tokens,
                )
                metrics["llm_calls"] += 1
                if summ is not None:
                    item["summary_en"] = "\n".join(summ.summary_en)
                    item["summary_zh"] = "\n".join(summ.summary_zh)
                    item["summary_ko"] = "\n".join(summ.summary_ko)
                    item["_summary_en_lines"] = list(summ.summary_en)
                    item["summary_status"] = status
                ok = verify_summary(
                    ctx.llm,
                    judge_model,
                    readme=readme,
                    summary_lines=item.get("_summary_en_lines") or [],
                    usage_acc=tokens,
                )
                metrics["llm_calls"] += 1
                metrics["verify_calls"] += 1
                if ok:
                    metrics["verify_passed"] += 1
            if not ok:
                item["summary_status"] = "unverified"
        except Exception as e:
            logger.warning("radar verify failed run=%s repo=%s: %s", ctx.run_id, name, str(e)[:200])
            ctx.store.audit(ctx.run_id, "tool", "radar_verify_error", name, {"error": str(e)[:200]})
    return {"status": "ok", "repos": repos, "metrics": metrics, **tokens}


def radar_rank(ctx: FunctionContext) -> dict[str, Any]:
    """플랫폼별 rank_score 정렬·상위 FINAL_TOP_N (skillRadar rank 노드)."""
    from zoneinfo import ZoneInfo

    from ..radar import FINAL_TOP_N, rank_score, stars_delta

    prev_out = _prev_output(ctx, "verify") or _prev_output(ctx, "summarize") or {}
    repos = prev_out.get("repos", [])
    metrics = prev_out.get("metrics")
    today = datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat()
    prev_cache: dict[str, dict[str, int]] = {}
    by_platform: dict[str, list[dict[str, Any]]] = {}
    for r in repos:
        p = r.get("platform", "")
        if p not in prev_cache:
            prev_cache[p] = ctx.store.radar_prev_stars(p, today)
        prev = prev_cache[p].get(r.get("full_name", ""))
        r["stars_today"] = stars_delta(int(r.get("stars", 0)), prev)
        r["rank_score"] = rank_score(
            int(r.get("stars", 0)),
            r.get("summary_status") == "ok",
            float(r.get("taxonomy_confidence", 0.0)),
        )
        by_platform.setdefault(p, []).append(r)
    ranked: list[dict[str, Any]] = []
    for items in by_platform.values():
        items.sort(key=lambda x: x["rank_score"], reverse=True)
        ranked.extend(items[:FINAL_TOP_N])
    for i, r in enumerate(ranked, 1):
        r["rank"] = i
    platforms = sorted(by_platform)
    return {
        "status": "ok",
        "repos": ranked,
        "date": today,
        "platforms": platforms,
        "metrics": metrics,
    }


def _radar_notify(ctx: FunctionContext, title: str, body: str, url: str = "") -> None:
    """notifier adapter 발송 — 실패는 audit+warning으로 격리."""
    notifier = ctx.adapters.get("notifier")
    if notifier is None:
        return
    try:
        notifier.send(title=title, body=body, source_url=url)
        send_web_push_text(ctx.adapters, title, body, url)
    except Exception as e:
        ctx.store.audit(ctx.run_id, "tool", "radar_notify_error", "", {"error": str(e)[:200]})
        logger.warning("radar notify failed run=%s: %s", ctx.run_id, str(e)[:200])


def _radar_weekly_digest(ctx: FunctionContext, today: str) -> None:
    """KST 월요일 수집분에 주간 digest 푸시 (skillRadar notify_weekly_digest 포팅)."""
    since = (datetime.fromisoformat(today).date() - timedelta(days=7)).isoformat()
    rows = ctx.store.radar_items_since(since)
    if not rows:
        return
    gain: dict[str, int] = {}
    window: set[str] = set()
    for row in rows:
        name = row["repo_full_name"]
        window.add(name)
        gain[name] = gain.get(name, 0) + int(row.get("stars_today") or 0)
    prior = ctx.store.radar_repo_names_before(since)
    new_entries = window - prior
    exited = prior - window
    top = sorted(gain.items(), key=lambda kv: kv[1], reverse=True)[:3]
    top_label = ", ".join(f"{n} +{g}" for n, g in top if g > 0) or "-"
    total_gain = sum(gain.values())
    body = (
        f"신규 {len(new_entries)}개 · 이탈 {len(exited)}개 · "
        f"주간 star 증가 {total_gain:,}\n급상승: {top_label}"
    )
    _radar_notify(ctx, "SkillRadar 주간 리포트", body)


def radar_publish(ctx: FunctionContext) -> dict[str, Any]:
    """승인된 결과를 플랫폼별 snapshot/item으로 저장 + Top1/주간 푸시."""
    from zoneinfo import ZoneInfo

    from ..radar import metrics_dict

    approval = ctx.state.get("approval") or {}
    if approval.get("decision") != "approve":
        return {"status": "rejected"}
    rank_out = _prev_output(ctx, "rank") or {}
    repos = rank_out.get("repos", [])
    date_str = rank_out.get("date") or ""
    force = bool(ctx.state.get("force"))

    by_platform: dict[str, list[dict[str, Any]]] = {}
    for r in repos:
        by_platform.setdefault(r.get("platform", "unknown"), []).append(r)

    snap_metrics = {"count": len(repos), "run_id": ctx.run_id}
    if rank_out.get("metrics"):
        snap_metrics["radar"] = metrics_dict(rank_out["metrics"])

    written: dict[str, int] = {}
    for platform, items in by_platform.items():
        if ctx.store.radar_snapshot_exists(platform, date_str):
            if not force:
                continue
            snap = ctx.store.radar_snapshot(platform, date_str)
            if snap:
                ctx.store.delete_radar_snapshot(int(snap["id"]))
        items = sorted(items, key=lambda x: x.get("rank_score", 0), reverse=True)
        snap_id = ctx.store.save_radar_snapshot(platform, date_str, snap_metrics)
        for i, it in enumerate(items, 1):
            ctx.store.add_radar_item(
                snap_id,
                {
                    "rank": i,
                    "repo_full_name": it.get("full_name", ""),
                    "repo_url": it.get("html_url", ""),
                    "description": it.get("description") or "",
                    "stars": it.get("stars", 0),
                    "language": it.get("language") or "",
                    "topics": it.get("topics") or [],
                    "readme_excerpt": it.get("readme_excerpt", ""),
                    "summary_en": it.get("summary_en", ""),
                    "summary_zh": it.get("summary_zh", ""),
                    "summary_ko": it.get("summary_ko", ""),
                    "content_hash": it.get("content_hash", ""),
                    "stars_today": it.get("stars_today"),
                    "license": it.get("license") or "",
                    "taxonomy": it.get("taxonomy", ""),
                    "taxonomy_confidence": it.get("taxonomy_confidence", 0.0),
                    "rank_score": it.get("rank_score", 0.0),
                    "summary_status": it.get("summary_status", ""),
                },
            )
        written[platform] = len(items)

    # skillRadar notify_daily_top: 플랫폼별 Top1 푸시
    for platform, items in by_platform.items():
        if platform not in written or not items:
            continue
        top = max(items, key=lambda x: x.get("rank_score", 0))
        _radar_notify(
            ctx,
            f"{platform.capitalize()} Top 1",
            f"{top.get('full_name', '')} ⭐{top.get('stars', 0)}",
            top.get("html_url", ""),
        )
    if not written and repos:
        _radar_notify(ctx, "SkillRadar 수집 실패", f"{date_str} 스냅샷 저장 실패")
    # KST 월요일 수집분에는 주간 digest 추가 발송
    if written and datetime.now(ZoneInfo("Asia/Seoul")).weekday() == 0:
        _radar_weekly_digest(ctx, date_str)
    logger.info("radar published run=%s written=%s date=%s", ctx.run_id, written, date_str)
    return {"status": "published", "written": written, "date": date_str}


FUNCTIONS: dict[str, Callable[[FunctionContext], dict[str, Any]]] = {
    "auto_checks": auto_checks,
    "gmail_validate": gmail_validate,
    "gmail_register": gmail_register,
    "gmail_notify": gmail_notify,
    "calendar_conflict_check": calendar_conflict_check,
    "radar_collect": radar_collect,
    "radar_dedupe": radar_dedupe,
    "radar_summarize": radar_summarize,
    "radar_verify": radar_verify,
    "radar_rank": radar_rank,
    "radar_publish": radar_publish,
}
