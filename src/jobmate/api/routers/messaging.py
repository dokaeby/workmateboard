"""Messaging/calendar-agent routes — webhooks, inbox, gmail digest, cron tick, email check."""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse

from ...sensors import (
    allowed_identifier,
    verify_slack_signature,
    verify_telegram_secret,
)
from ..jobs import (
    _check_waiting_approvals,
    _maybe_daily_radar,
    _maybe_generate_reports,
    _maybe_poll_gmail,
    _process_due_notification_jobs,
    _process_due_schedules,
    _process_pending_messages,
)
from ..support import (
    _mark_cron_tick,
    _maybe_check_triggers,
    _reap_stale_runs,
    _retention_sweep,
)

logger = logging.getLogger("jobmate.api")


def register(app: FastAPI, ctx: Any) -> None:
    store = ctx.store
    settings = ctx.settings
    llm = ctx.llm
    adapters = ctx.adapters
    err = ctx.err
    _messenger_handle = ctx._messenger_handle
    _lead_gate = ctx._lead_gate
    _lead_user = ctx._lead_user
    _digest_rate_limited = ctx._digest_rate_limited
    _email_check_rate_limited = ctx._email_check_rate_limited
    _cron_authorized = ctx._cron_authorized
    _summarize_email = ctx._summarize_email
    _schedule_run_jobs = ctx._schedule_run_jobs
    _admin_user = ctx._admin_user

    # ---- calendar-agent: 멀티채널 웹훅·수신함·이벤트·알림예약 ----
    @app.post("/api/v1/webhooks/slack")
    async def slack_webhook(request: Request, background_tasks: BackgroundTasks) -> JSONResponse:
        body = await request.body()
        if not verify_slack_signature(
            body,
            request.headers.get("X-Slack-Request-Timestamp", ""),
            request.headers.get("X-Slack-Signature", ""),
            settings.slack_signing_secret,
        ):
            logger.warning("slack webhook rejected: bad signature")
            return err(401, "invalid_signature", "bad slack signature", request)
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return err(400, "bad_json", "invalid JSON", request)
        if payload.get("type") == "url_verification":
            return JSONResponse(content={"challenge": payload.get("challenge", "")})
        event = payload.get("event") or {}
        if (
            payload.get("type") != "event_callback"
            or event.get("type") != "message"
            or event.get("bot_id")
            or event.get("subtype") not in (None, "file_share")
            or not (event.get("text") or "").strip()
        ):
            return JSONResponse(content={"status": "ignored"})
        channel = event.get("channel", "")
        if not allowed_identifier(channel, settings.slack_channel_ids):
            return JSONResponse(content={"status": "ignored"})
        team = payload.get("team_id", "")
        ts = event.get("ts", "")
        source_id = f"{team}:{channel}:{ts}"
        text = event.get("text", "")
        # 메신저 커맨드 — 인식되면 답장만 하고 캘린더 수신함 처리로는 넘기지 않는다
        if _messenger_handle(
            "slack", channel, text, background_tasks, sender_id=str(event.get("user") or "")
        ):
            return JSONResponse(content={"status": "command"})
        created = store.save_message_extraction(
            "slack", source_id, event.get("thread_ts") or ts, {}, "pending", source_text=text[:8000]
        )
        return JSONResponse(
            content={"status": "processed" if created else "duplicate", "source_id": source_id}
        )

    @app.post("/api/v1/webhooks/telegram")
    async def telegram_webhook(request: Request, background_tasks: BackgroundTasks) -> JSONResponse:
        if not verify_telegram_secret(
            request.headers.get("X-Telegram-Bot-Api-Secret-Token", ""),
            settings.telegram_webhook_secret,
        ):
            logger.warning("telegram webhook rejected: bad secret")
            return err(401, "invalid_secret", "bad telegram secret", request)
        try:
            update = json.loads(await request.body())
        except json.JSONDecodeError:
            return err(400, "bad_json", "invalid JSON", request)
        msg = update.get("message") or update.get("channel_post") or {}
        sender = msg.get("from") or {}
        if sender.get("is_bot"):
            return JSONResponse(content={"status": "ignored"})
        text = msg.get("text") or msg.get("caption") or ""
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        if not text.strip() or not allowed_identifier(chat_id, settings.telegram_chat_ids):
            return JSONResponse(content={"status": "ignored"})
        source_id = f"{chat_id}:{msg.get('message_id', '')}"
        # 메신저 커맨드 — 인식되면 답장만 하고 캘린더 수신함 처리로는 넘기지 않는다
        if _messenger_handle(
            "telegram",
            chat_id,
            text,
            background_tasks,
            sender_id=str((msg.get("from") or {}).get("id") or ""),
        ):
            return JSONResponse(content={"status": "command"})
        created = store.save_message_extraction(
            "telegram",
            source_id,
            str(msg.get("message_thread_id") or msg.get("message_id", "")),
            {},
            "pending",
            source_text=text[:8000],
        )
        return JSONResponse(
            content={"status": "processed" if created else "duplicate", "source_id": source_id}
        )

    @app.get("/api/v1/messages")
    def list_messages(
        request: Request, provider: str | None = None, limit: int = 20
    ) -> dict[str, Any]:
        """메시지 수신함 목록.

        source_text(메일/Slack/Telegram 원문, 최대 8000자)는 민감 정보이므로
        gmail provider만 그대로 노출한다(Gmail Checker UI가 사용).
        그 외 provider는 lead 이상만 원문을 보고, member/비로그인에게는
        필드를 유지한 채 빈 문자열로 마스킹한다."""
        limit = max(1, min(int(limit), 200))
        msgs = store.list_message_extractions(provider, limit)
        if _lead_user(request) is None:
            for m in msgs:
                if m.get("source_provider") != "gmail":
                    m["source_text"] = ""
        return {"messages": msgs}

    @app.post("/api/v1/gmail/digest")
    def gmail_digest(request: Request, body: dict[str, Any] | None = None) -> JSONResponse:
        """최근 Gmail 메시지들을 LLM으로 요약. 실패 시 빈 summary + error.
        LLM 비용이 드는 요약 생성이므로 lead 이상 + 사용자/IP당 일일 상한."""
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        if _digest_rate_limited(request):
            return err(429, "rate_limited", "daily digest limit reached", request)
        raw_limit = (body or {}).get("limit", 20)
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            return err(422, "validation_error", "limit must be an integer", request)
        limit = max(1, min(limit, 100))
        msgs = [
            m
            for m in store.list_message_extractions("gmail", limit)
            if (m.get("source_text") or "").strip()
        ]
        if not msgs:
            return JSONResponse(content={"summary": "", "count": 0})
        lines = []
        for m in msgs[:15]:
            text = (m.get("source_text") or "").replace("\n", " ")[:300]
            lines.append(f"- [{m.get('processing_status')}] {text}")
        system = (
            "너는 이메일 비서다. 다음 Gmail 메시지 목록을 한국어로 "
            "3~5개 bullet로 요약해. 각 bullet은 '- '로 시작하고, "
            "일정/행동 필요한 내용 중심으로 작성해."
        )
        try:
            model = settings.model_for("_digest", "default")
            out, _usage = llm.complete(model=model, system=system, user="\n".join(lines))
            summary = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
        except Exception as e:
            logger.warning("gmail digest failed: %s", str(e)[:200])
            return JSONResponse(content={"summary": "", "count": len(msgs), "error": str(e)[:200]})
        return JSONResponse(content={"summary": summary, "count": len(msgs)})

    @app.api_route("/api/v1/cron/tick", methods=["GET", "POST"])
    def cron_tick(request: Request, background_tasks: BackgroundTasks) -> JSONResponse:
        """서버리스 환경용 1회 자동화 사이클 (Vercel Cron 등에서 호출)."""
        secret = os.environ.get("CRON_SECRET") or os.environ.get("JOBMATE_CRON_SECRET", "")
        from ...security import api_key, authorized

        # Vercel Cron requires its dedicated secret; other hosts also accept a machine key.
        if os.environ.get("VERCEL"):
            valid = bool(secret) and request.headers.get("authorization") == f"Bearer {secret}"
        else:
            # 인증 엣지 정리: CRON_SECRET·JOBMATE_API_KEY가 모두 없고
            # allowed_emails만 설정된 환경에서는 authorized()가 항상 False라
            # secret도 key도 없는 호출자가 tick을 실행할 수 없었다.
            # 이 경우에 한해 admin 세션 사용자(_admin_user)도 허용해 관리자
            # 수동 트리거 경로를 남긴다. CRON_SECRET/API 키가 설정된 환경에서는
            # 적용하지 않는다 — 로컬 개발 폴백(local admin)이 시크릿을
            # 무력화하면 안 되기 때문. 완전 무인증 개발 모드(모두 미설정)는
            # authorized()가 True를 반환하므로 기존대로 통과한다.
            valid = (
                (bool(secret) and request.headers.get("authorization") == f"Bearer {secret}")
                or (not secret and authorized(request))
                or (bool(api_key()) and authorized(request))
                or (not secret and not api_key() and _admin_user(request) is not None)
            )
        if not valid:
            logger.warning("cron tick rejected: invalid credentials")
            return err(401, "unauthorized", "invalid cron credentials", request)
        _mark_cron_tick(store)
        # 고착 run 복구 + 보존 기간 정리 — 틱마다 호출하되 내부 스로틀 적용
        _reap_stale_runs(store)
        _retention_sweep(store)
        _process_pending_messages(store, settings, llm, adapters)
        _process_due_notification_jobs(store, adapters)
        schedule_jobs: list = []
        _process_due_schedules(store, settings, llm, adapters, defer=schedule_jobs)
        _schedule_run_jobs(schedule_jobs, background_tasks)
        _check_waiting_approvals(store, adapters)
        _maybe_poll_gmail(store, settings, adapters)
        radar_jobs: list = []
        _maybe_daily_radar(store, settings, llm, adapters, defer=radar_jobs)
        _schedule_run_jobs(radar_jobs, background_tasks)
        trigger_jobs: list = []
        _maybe_check_triggers(store, settings, llm, adapters, defer=trigger_jobs)
        _schedule_run_jobs(trigger_jobs, background_tasks)
        _maybe_generate_reports(store, settings, adapters)
        logger.info("cron tick cycle completed")
        return JSONResponse(content={"ok": True})

    @app.post("/api/v1/email/check")
    def email_check(request: Request, body: dict[str, Any] | None = None) -> JSONResponse:
        """Gmail 최근 메일을 읽어 3줄 요약 카드를 생성·저장.
        cron-job.org 등 외부 스케줄러가 30분마다 POST로 호출.

        인증: cron secret Bearer 또는 lead 이상(세션/API 키). 둘 다 없으면 거부.
        _cron_authorized 단독으로는 secret 미설정+allowed_emails만 있는
        환경에서 인증이 무력화되므로 lead 검사를 병행한다."""
        cron_secret = os.environ.get("CRON_SECRET") or os.environ.get("JOBMATE_CRON_SECRET", "")
        secret_ok = bool(cron_secret) and (
            request.headers.get("authorization") == f"Bearer {cron_secret}"
        )
        if not secret_ok:
            if _lead_user(request) is None:
                logger.warning("email check rejected: lead role or cron secret required")
                return err(403, "forbidden", "lead role or cron secret required", request)
            if not _cron_authorized(request):
                logger.warning("email check rejected: invalid cron secret")
                return err(401, "unauthorized", "invalid cron secret", request)
        if _email_check_rate_limited():
            return err(429, "rate_limited", "email check min interval not reached", request)
        gmail = adapters.get("gmail")
        if gmail is None:
            return err(503, "no_gmail_adapter", "gmail adapter not configured", request)
        payload = body or {}
        try:
            limit = int(payload.get("limit", 5))
        except (TypeError, ValueError):
            return err(422, "validation_error", "limit must be an integer", request)
        limit = max(1, min(limit, 20))
        query = str(payload.get("query", "in:inbox"))
        cards: list[dict[str, Any]] = []
        errors: list[str] = []
        try:
            listing = gmail.list_messages(query=query, max_results=limit)
        except Exception as e:
            logger.warning("email check gmail list failed: %s", str(e)[:200])
            return JSONResponse(
                status_code=502, content={"ok": False, "error": str(e)[:300], "cards": []}
            )
        for m in listing:
            mid = m["id"]
            try:
                msg = gmail.get_message(mid)
            except Exception as e:
                errors.append(f"{mid}: {str(e)[:120]}")
                logger.warning("email check get_message failed id=%s: %s", mid, str(e)[:120])
                continue
            summary = _summarize_email(msg)
            ts = msg.get("internal_date", "")
            message_at = ""
            if ts.isdigit():
                message_at = datetime.fromtimestamp(int(ts) / 1000, UTC).isoformat()
            store.save_email_summary(
                "gmail",
                mid,
                subject=msg.get("subject", ""),
                sender=msg.get("from_addr", ""),
                summary=summary,
                message_at=message_at,
            )
            cards.append(
                {
                    "source_id": mid,
                    "subject": msg.get("subject", ""),
                    "sender": msg.get("from_addr", ""),
                    "summary": summary,
                    "message_at": message_at,
                    "url": f"https://mail.google.com/mail/u/0/#all/{mid}",
                }
            )
        if errors:
            logger.warning("email check partial failures: %d/%d", len(errors), len(listing))
        logger.info("email check done cards=%d errors=%d", len(cards), len(errors))
        return JSONResponse(
            content={"ok": True, "checked": len(cards), "cards": cards, "errors": errors}
        )

    @app.get("/api/v1/email/summaries")
    def email_summaries(limit: int = 30) -> dict[str, Any]:
        cards = store.list_email_summaries("gmail", min(max(limit, 1), 100))
        for c in cards:
            c["url"] = f"https://mail.google.com/mail/u/0/#all/{c['source_id']}"
        return {"cards": cards}

    @app.post("/api/v1/email/purge-undecryptable")
    def purge_undecryptable(request: Request) -> JSONResponse:
        """복호화 불가한 email_summaries 행 삭제 (키 로테이션 복구용). lead 이상."""
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        removed = store.purge_undecryptable_email_summaries("gmail")
        logger.info("purged %d undecryptable email_summaries rows", removed)
        store.audit("", "api", "email_purge_undecryptable", "", {"removed": removed})
        return JSONResponse(content={"ok": True, "removed": removed})
