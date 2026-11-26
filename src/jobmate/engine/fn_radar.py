"""skillRadar radar_* function — 수집/중복제거/요약/검증/랭킹/발행."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Any

from ..budgets import BudgetTracker
from ..pricing import cost_usd
from .fn_common import FunctionContext, _prev_output, send_web_push_text

logger = logging.getLogger("jobmate.engine.functions")


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


def _radar_budget_tracker(ctx: FunctionContext) -> BudgetTracker:
    """run state의 누적 사용량으로 시드한 BudgetTracker.

    function 단계는 runner의 단계 경계에서만 예산을 검사하므로, radar처럼
    단계 내부에서 수십 회 LLM을 호출하는 루프는 자체적으로 주기적 검사가
    필요하다. state.started_at은 time.time() 기준, BudgetTracker는
    monotonic 기준이므로 경과 시간을 환산해 시드한다.
    """
    s = ctx.settings
    st = ctx.state or {}
    elapsed = max(0.0, time.time() - float(st.get("started_at") or time.time()))
    return BudgetTracker(
        max_tokens=int(getattr(s, "budget_max_tokens", 200_000) or 200_000),
        max_tool_calls=int(getattr(s, "budget_max_tool_calls", 20) or 20),
        max_seconds=float(getattr(s, "budget_max_seconds", 600.0) or 600.0),
        max_cost_usd=float(getattr(s, "budget_max_cost_usd", 5.0) or 5.0),
        tokens_in=int(st.get("tokens_in", 0) or 0),
        tokens_out=int(st.get("tokens_out", 0) or 0),
        tool_calls=int(st.get("tool_calls", 0) or 0),
        cost_usd=float(st.get("cost_usd", 0.0) or 0.0),
        started_at=time.monotonic() - elapsed,
    )


def _tracker_sync(
    tracker: BudgetTracker, acc: dict[str, int], model_name: str, seen: list[int]
) -> float:
    """usage_acc의 마지막 sync 이후 델타를 tracker에 반영. 반환: 추가된 비용(USD)."""
    cur_in = int(acc.get("tokens_in", 0) or 0)
    cur_out = int(acc.get("tokens_out", 0) or 0)
    din, dout = max(0, cur_in - seen[0]), max(0, cur_out - seen[1])
    seen[0], seen[1] = cur_in, cur_out
    if din == 0 and dout == 0:
        return 0.0
    cost = cost_usd(model_name, din, dout)
    tracker.add_usage(din, dout, cost)
    return cost


def _skipped_budget_item(r: dict[str, Any], reason: str) -> dict[str, Any]:
    """예산 소진으로 LLM 처리를 건너뛴 repo 항목 — fallback 요약 + skipped_budget."""
    from ..radar import fallback_summaries_translated

    item = dict(r)
    name = r.get("full_name", "")
    fb = fallback_summaries_translated(r.get("description"), name)
    item.update(
        {
            "summary_en": "\n".join(fb["en"]),
            "summary_zh": "\n".join(fb["zh"]),
            "summary_ko": "\n".join(fb["ko"]),
            "_summary_en_lines": fb["en"],
            "taxonomy": "other",
            "taxonomy_confidence": 0.0,
            "summary_status": "skipped_budget",
            "summary_source": "skipped_budget",
            "budget_reason": reason,
        }
    )
    return item


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
    # 모델별 사용량 분리 — cost_usd는 모델 라벨별 단가로 환산해야 정확하다.
    tokens = {"tokens_in": 0, "tokens_out": 0}  # summarize_repo (sum_model)
    judge_tokens = {"tokens_in": 0, "tokens_out": 0}  # classify_repo (judge_model)
    tracker = _radar_budget_tracker(ctx)
    cost_total = 0.0
    skipped = 0
    for idx, r in enumerate(repos):
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
        # 캐시 미스 — LLM 호출이 필요한 시점에 run 예산을 검사해 조기 종료.
        # 이전에는 단계 종료 후에야 사용량이 합산되어 run 내내 예산 검사가 없었다.
        reason = tracker.exhausted_reason()
        if reason:
            for rest in repos[idx:]:
                out_repos.append(_skipped_budget_item(rest, reason))
                skipped += 1
            ctx.store.audit(
                ctx.run_id,
                "tool",
                "radar_budget_exhausted",
                "summarize",
                {"reason": reason, "skipped": skipped},
            )
            logger.warning(
                "radar summarize budget exhausted run=%s reason=%s skipped=%d",
                ctx.run_id,
                reason,
                skipped,
            )
            break
        sum_seen = [tokens["tokens_in"], tokens["tokens_out"]]
        judge_seen = [judge_tokens["tokens_in"], judge_tokens["tokens_out"]]
        try:
            cls = classify_repo(
                ctx.llm,
                judge_model,
                name=name,
                description=r.get("description"),
                topics=list(r.get("topics") or []),
                readme=sanitized,
                usage_acc=judge_tokens,
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
        cost_total += _tracker_sync(tracker, tokens, sum_model.model, sum_seen)
        cost_total += _tracker_sync(tracker, judge_tokens, judge_model.model, judge_seen)
        out_repos.append(item)
    metrics["repos_processed"] = len(out_repos)
    if skipped:
        metrics["budget_skipped"] = skipped
    return {
        "status": "ok",
        "repos": out_repos,
        "metrics": metrics,
        "tokens_in": tokens["tokens_in"] + judge_tokens["tokens_in"],
        "tokens_out": tokens["tokens_out"] + judge_tokens["tokens_out"],
        "cost_usd": round(cost_total, 6),
    }


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
    # 모델별 사용량 분리 — verify는 judge_model, 재요약은 sum_model 사용.
    tokens = {"tokens_in": 0, "tokens_out": 0}  # summarize_repo 재시도
    judge_tokens = {"tokens_in": 0, "tokens_out": 0}  # verify_summary
    tracker = _radar_budget_tracker(ctx)
    cost_total = 0.0
    skipped = 0

    for idx, item in enumerate(repos):
        name = item.get("full_name", "")
        if item.get("summary_status") != "ok":
            continue
        if item.get("_cache_verified"):
            metrics["cache_hits"]["verify"] += 1
            continue
        # LLM 호출 전 run 예산 검사 — 소진 시 남은 항목을 조기 skipped 처리.
        reason = tracker.exhausted_reason()
        if reason:
            for rest in repos[idx:]:
                if rest.get("summary_status") == "ok" and not rest.get("_cache_verified"):
                    rest["summary_status"] = "skipped_budget"
                    rest["budget_reason"] = reason
                    skipped += 1
            ctx.store.audit(
                ctx.run_id,
                "tool",
                "radar_budget_exhausted",
                "verify",
                {"reason": reason, "skipped": skipped},
            )
            logger.warning(
                "radar verify budget exhausted run=%s reason=%s skipped=%d",
                ctx.run_id,
                reason,
                skipped,
            )
            break
        readme = item.get("sanitized_readme") or item.get("readme") or ""
        sum_seen = [tokens["tokens_in"], tokens["tokens_out"]]
        judge_seen = [judge_tokens["tokens_in"], judge_tokens["tokens_out"]]
        try:
            ok = verify_summary(
                ctx.llm,
                judge_model,
                readme=readme,
                summary_lines=item.get("_summary_en_lines") or [],
                usage_acc=judge_tokens,
            )
            metrics["llm_calls"] += 1
            metrics["verify_calls"] += 1
            if ok:
                metrics["verify_passed"] += 1
            while (
                not ok
                and retries.get(name, 0) < MAX_VERIFY_RETRIES
                and tracker.exhausted_reason() is None
            ):
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
                    usage_acc=judge_tokens,
                )
                metrics["llm_calls"] += 1
                metrics["verify_calls"] += 1
                if ok:
                    metrics["verify_passed"] += 1
            if not ok:
                # 재시도 루프가 예산 소진으로 끊긴 경우 unverified 대신
                # skipped_budget으로 표시해 원인을 구분한다.
                item["summary_status"] = (
                    "skipped_budget" if tracker.exhausted_reason() else "unverified"
                )
                if item["summary_status"] == "skipped_budget":
                    skipped += 1
        except Exception as e:
            logger.warning("radar verify failed run=%s repo=%s: %s", ctx.run_id, name, str(e)[:200])
            ctx.store.audit(ctx.run_id, "tool", "radar_verify_error", name, {"error": str(e)[:200]})
        cost_total += _tracker_sync(tracker, tokens, _sum_model.model, sum_seen)
        cost_total += _tracker_sync(tracker, judge_tokens, judge_model.model, judge_seen)
    if skipped:
        metrics["budget_skipped"] = skipped
    return {
        "status": "ok",
        "repos": repos,
        "metrics": metrics,
        "tokens_in": tokens["tokens_in"] + judge_tokens["tokens_in"],
        "tokens_out": tokens["tokens_out"] + judge_tokens["tokens_out"],
        "cost_usd": round(cost_total, 6),
    }


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
        f"신규 {len(new_entries)}개, 이탈 {len(exited)}개, "
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
