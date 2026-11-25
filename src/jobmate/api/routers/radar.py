"""skillRadar route handlers extracted from api/app.py."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse


def register(app: FastAPI, ctx: Any) -> None:
    _RADAR_LANGS = ctx._RADAR_LANGS
    _RADAR_RANKING_PERIODS = ctx._RADAR_RANKING_PERIODS
    _RADAR_TOPIC_MAX = ctx._RADAR_TOPIC_MAX
    _lead_gate = ctx._lead_gate
    _radar_item_json = ctx._radar_item_json
    _radar_recent = ctx._radar_recent
    _schedule_run_jobs = ctx._schedule_run_jobs
    err = ctx.err
    settings = ctx.settings
    store = ctx.store
    submit_run = ctx.submit_run

    # ---- skillRadar ----
    @app.get("/api/v1/radar/trending")
    def radar_trending(
        platform: str = "claude", date: str | None = None, lang: str = "ko"
    ) -> JSONResponse:
        date_str = date or datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat()
        snap = store.radar_snapshot(platform, date_str)
        if snap is None:
            # 최신 스냅샷으로 폴백
            dates = store.radar_dates(platform)
            if not dates:
                return JSONResponse(
                    content={
                        "platform": platform,
                        "date": date_str,
                        "lang": lang,
                        "status": "no_snapshot",
                        "items": [],
                    }
                )
            date_str = dates[0]
            snap = store.radar_snapshot(platform, date_str)
        if snap is None:
            return JSONResponse(content={"items": [], "date": date_str})
        items = store.radar_items(int(snap["id"]))
        lang_key = {"ko": "summary_ko", "en": "summary_en", "zh": "summary_zh"}.get(
            lang, "summary_ko"
        )
        return JSONResponse(
            content={
                "platform": platform,
                "date": date_str,
                "lang": lang,
                "items": [
                    {
                        "rank": i["rank"],
                        "repo": i["repo_full_name"],
                        "url": i["repo_url"],
                        "description": i["description"],
                        "stars": i["stars"],
                        "stars_today": i["stars_today"],
                        "language": i["language"],
                        "license": i["license"],
                        "taxonomy": i["taxonomy"],
                        "summary": (i.get(lang_key) or i.get("summary_ko", "")).splitlines(),
                    }
                    for i in items
                ],
            }
        )

    @app.get("/api/v1/radar/dates")
    def radar_dates(platform: str = "claude") -> dict[str, Any]:
        return {"platform": platform, "dates": store.radar_dates(platform)}

    @app.get("/api/v1/radar/topics")
    def radar_topics() -> dict[str, Any]:
        counts: dict[str, int] = {}
        for row in _radar_recent():
            for t in set(json.loads(row.get("topics_json") or "[]")):
                counts[t] = counts.get(t, 0) + 1
        top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:_RADAR_TOPIC_MAX]
        return {"topics": [{"topic": t, "count": c} for t, c in top]}

    @app.get("/api/v1/radar/topic-repos")
    def radar_topic_repos(
        request: Request,
        topic: str,
        platform: str = "all",
        lang: str = "ko",
        limit: int = 10,
    ) -> JSONResponse:
        if not topic or len(topic) > 100:
            return err(400, "validation", "topic 파라미터가 필요합니다", request)
        if platform != "all" and platform not in settings.radar_platforms:
            return err(400, "validation", "platform은 all 또는 radar_platforms 중 하나", request)
        if lang not in _RADAR_LANGS:
            return err(400, "validation", "lang은 ko|en|zh 중 하나", request)
        limit = max(1, min(limit, 50))
        best: dict[str, dict[str, Any]] = {}
        for row in _radar_recent():
            if topic not in json.loads(row.get("topics_json") or "[]"):
                continue
            if platform != "all" and row.get("platform") != platform:
                continue
            prev = best.get(row["repo_full_name"])
            if prev is None or (row.get("stars") or 0) > (prev.get("stars") or 0):
                best[row["repo_full_name"]] = row
        ranked = sorted(
            best.values(),
            key=lambda r: (
                r.get("rank_score") if r.get("rank_score") is not None else float("-inf"),
                r.get("stars") or 0,
            ),
            reverse=True,
        )[:limit]
        return JSONResponse(
            content={
                "platform": platform,
                "date": datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat(),
                "lang": lang,
                "items": [_radar_item_json(r, lang) for r in ranked],
            }
        )

    @app.get("/api/v1/radar/related-topics")
    def radar_related_topics(request: Request, topic: str) -> JSONResponse:
        if not topic or len(topic) > 100:
            return err(400, "validation", "topic 파라미터가 필요합니다", request)
        co: dict[str, int] = {}
        for row in _radar_recent():
            topics = set(json.loads(row.get("topics_json") or "[]"))
            if topic not in topics:
                continue
            for other in topics - {topic}:
                co[other] = co.get(other, 0) + 1
        top = sorted(co.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
        return JSONResponse(
            content={"topic": topic, "related": [{"topic": t, "score": c} for t, c in top]}
        )

    @app.get("/api/v1/radar/ranking")
    def radar_ranking(
        request: Request, period: str = "week", platform: str = "all", lang: str = "ko"
    ) -> JSONResponse:
        days = _RADAR_RANKING_PERIODS.get(period)
        if days is None:
            return err(400, "validation", "period은 week|month 중 하나", request)
        if platform != "all" and platform not in settings.radar_platforms:
            return err(400, "validation", "platform은 all 또는 radar_platforms 중 하나", request)
        if lang not in _RADAR_LANGS:
            return err(400, "validation", "lang은 ko|en|zh 중 하나", request)
        agg: dict[str, dict[str, Any]] = {}
        for row in _radar_recent(days):
            if platform != "all" and row.get("platform") != platform:
                continue
            key = row["repo_full_name"]
            cur = agg.get(key)
            if cur is None:
                agg[key] = {
                    "row": row,
                    "appearances": 1,
                    "gain": int(row.get("stars_today") or 0),
                }
                continue
            cur["appearances"] += 1
            cur["gain"] += int(row.get("stars_today") or 0)
            if (row.get("collected_date") or "") > (cur["row"].get("collected_date") or ""):
                cur["row"] = row
        ranked = sorted(
            agg.values(),
            key=lambda a: (a["gain"], a["appearances"], a["row"].get("stars") or 0),
            reverse=True,
        )[:30]
        items = []
        for idx, a in enumerate(ranked, 1):
            payload = _radar_item_json(a["row"], lang)
            payload["rank"] = idx
            payload["appearances"] = a["appearances"]
            items.append(payload)
        return JSONResponse(
            content={
                "platform": platform,
                "date": datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat(),
                "lang": lang,
                "items": items,
            }
        )

    @app.get("/api/v1/radar/repo/{full_name:path}")
    def radar_repo_detail(request: Request, full_name: str, lang: str = "ko") -> JSONResponse:
        if lang not in _RADAR_LANGS:
            return err(400, "validation", "lang은 ko|en|zh 중 하나", request)
        row = store.radar_repo_latest(full_name)
        if row is None:
            return err(404, "not_found", "수집된 저장소가 아닙니다", request)
        topics = set(json.loads(row.get("topics_json") or "[]"))
        co: dict[str, int] = {}
        for other_row in _radar_recent():
            other_topics = set(json.loads(other_row.get("topics_json") or "[]"))
            if not (topics & other_topics):
                continue
            for t in other_topics - topics:
                co[t] = co.get(t, 0) + 1
        related = [t for t, _c in sorted(co.items(), key=lambda kv: (-kv[1], kv[0]))[:8]]
        payload = _radar_item_json(row, lang)
        payload.update(
            {
                "readme_excerpt": row.get("readme_excerpt") or "",
                "summaries": {
                    "en": (row.get("summary_en") or "").splitlines(),
                    "zh": (row.get("summary_zh") or "").splitlines(),
                    "ko": (row.get("summary_ko") or "").splitlines(),
                },
                "related_topics": related,
                "collected_date": row.get("collected_date"),
                "snapshot_platform": row.get("platform"),
            }
        )
        return JSONResponse(content={"item": payload})

    @app.post("/api/v1/radar/refresh", status_code=202)
    def radar_refresh(
        background_tasks: BackgroundTasks,
        request: Request,
        body: dict[str, Any] | None = None,
    ) -> JSONResponse:
        """platforms 수집 run 생성 — LLM/GitHub 호출을 유발하므로 lead 이상.

        platforms는 list여야 하고, 길이는 settings.radar_platforms 이하이며,
        각 원소는 radar_platforms 화이트리스트 값이어야 한다."""
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        body = body or {}
        raw_platforms = body.get("platforms")
        allowed = set(settings.radar_platforms)
        if raw_platforms is None:
            platforms = list(settings.radar_platforms)
        else:
            if not isinstance(raw_platforms, list):
                return err(422, "validation", "platforms must be a list", request)
            if len(raw_platforms) > len(allowed):
                return err(
                    422,
                    "validation",
                    f"platforms must have at most {len(allowed)} items",
                    request,
                )
            bad = [p for p in raw_platforms if p not in allowed]
            if bad:
                return err(
                    422,
                    "validation",
                    f"unknown platforms: {bad} (allowed: {sorted(allowed)})",
                    request,
                )
            platforms = [str(p) for p in raw_platforms]
        force = bool(body.get("force", True))
        # radar는 승인 단계 없이 자동 발행 — daily 수집이 사람 승인을 기다리면
        # waiting_approval run이 쌓여 승인 대기 푸시가 반복된다.
        # body 값과 무관하게 항상 auto_approve (수동 수집도 승인 불필요).
        auto_approve = True
        jobs: list = []
        ids = []
        for p in platforms:
            rid = submit_run(
                "skill_radar",
                f"radar collect {p}",
                {"platform": p, "force": force, "auto_approve": auto_approve},
                defer=jobs,
            )
            ids.append(rid)
        _schedule_run_jobs(jobs, background_tasks)
        return JSONResponse(status_code=202, content={"run_ids": ids, "platforms": platforms})
