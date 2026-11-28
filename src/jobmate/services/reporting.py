"""spec 008 US3 — 리포트 구독 검증 + 채널별 발송.

- validate_fields: kind/channel/target 스키마 + SSRF 가드(validate_outbound_url
  + webhook_url_allowed — services/webhooks.py와 동일 규칙).
- deliver_subscription: email=mailer._deliver, slack=messenger.send_slack,
  webhook=spec 004 HMAC 서명 헤더로 직접 POST(services/webhooks.py의
  build_event_payload/_sign/_post_delivery 재사용).
- deliver_report_subscriptions: 리포트 생성 주기(jobs._maybe_generate_reports)
  훅 — 구독별 발송, 실패는 warning 로그 후 계속(사이클 비차단).

로그 규칙: webhook/slack URL은 자격증명을 포함할 수 있으므로 호스트만
기록한다(webutil.host_of). 응답 본문·URL 원문은 남기지 않는다.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from .. import mailer, messenger, webutil
from ..config import Settings
from ..security import decrypt_str
from ..store.db import Store
from ..store.reporting_ext import REPORT_SUB_CHANNELS, REPORT_SUB_KINDS
from . import webhooks as webhooks_svc

logger = logging.getLogger("jobmate.reporting")

_MAX_TARGET_LEN = 2000
# 발송 payload의 event type — 수신자가 필터링에 사용.
_EVENT_PREFIX = "report."


def validate_fields(kind: Any, channel: Any, target: Any) -> tuple[str, str, str]:
    """(kind, channel, target) 정규화 — 위반 시 ValueError(라우터가 422)."""
    k = str(kind or "").strip().lower()
    if k not in REPORT_SUB_KINDS:
        raise ValueError(f"kind must be one of {list(REPORT_SUB_KINDS)}")
    ch = str(channel or "").strip().lower()
    if ch not in REPORT_SUB_CHANNELS:
        raise ValueError(f"channel must be one of {list(REPORT_SUB_CHANNELS)}")
    t = str(target or "").strip()
    if not t:
        raise ValueError("target must not be empty")
    if len(t) > _MAX_TARGET_LEN:
        raise ValueError(f"target must be at most {_MAX_TARGET_LEN} chars")
    if ch == "email":
        if "@" not in t or len(t) > 320 or any(c.isspace() for c in t):
            raise ValueError("target must be a valid email address")
    else:
        # slack/webhook — URL 정책 + SSRF 가드(저장 시점 검증).
        # validate_outbound_url은 FetchError를 던진다 — 라우터의 422
        # 매핑(ValueError)에 맞추기 위해 여기서 변환한다.
        try:
            checked = webutil.validate_outbound_url(t, what=f"{ch}_url")
        except webutil.FetchError as e:
            raise ValueError(str(e)) from e
        port_err = webhooks_svc.webhook_url_allowed(checked)
        if port_err is not None:
            raise ValueError(f"{ch} url rejected: {port_err}")
        t = checked
    return k, ch, t


def report_summary(report: dict[str, Any]) -> str:
    """채널 발송용 한 줄 요약 — kind/runs/approvals 중심(민감값 없음)."""
    kind = str(report.get("kind") or "")
    runs = report.get("runs") or {}
    approvals = report.get("approvals") or {}
    llm = report.get("llm") or {}
    total = runs.get("total")
    failed = runs.get("failed")
    pending = approvals.get("pending")
    cost = llm.get("total_cost_usd")
    parts = [f"kind={kind}"]
    if total is not None:
        parts.append(f"runs={total}")
    if failed is not None:
        parts.append(f"failed={failed}")
    if pending is not None:
        parts.append(f"approvals_pending={pending}")
    if cost is not None:
        parts.append(f"llm_cost_usd={cost}")
    return "; ".join(parts)


def _webhook_payload(sub: dict[str, Any], kind: str, report: dict[str, Any]) -> tuple[str, bytes]:
    """spec 004 페이로드 스키마 재사용 — data는 요약/식별자만."""
    event_id = f"report:{sub.get('id')}:{uuid.uuid4().hex[:10]}"
    data: dict[str, Any] = {
        "subscription_id": str(sub.get("id") or ""),
        "kind": kind,
        "summary": report_summary(report),
        "generated_at": str(report.get("generated_at") or ""),
    }
    runs = report.get("runs") or {}
    if isinstance(runs.get("total"), int):
        data["runs_total"] = runs["total"]
    return event_id, webhooks_svc.build_event_payload(
        f"{_EVENT_PREFIX}{kind}", event_id, data
    ).encode("utf-8")


def _deliver_email(target: str, kind: str, report: dict[str, Any]) -> tuple[bool, str]:
    if not mailer.smtp_configured():
        return False, "skipped_unconfigured"
    subject = f"[jobMate] {kind} report"
    body = (
        f"jobMate {kind} report is ready.\n\n{report_summary(report)}\n\n"
        "View the full report in the dashboard: /?view=reports\n"
    )
    try:
        ok = mailer._deliver(target, subject, body, None, "report", "/?view=reports")  # noqa: SLF001
    except Exception as e:
        logger.warning("report email send failed: %s", type(e).__name__)
        return False, "failed"
    return (True, "sent") if ok else (False, "failed")


def _deliver_slack(target: str, kind: str, report: dict[str, Any]) -> tuple[bool, str]:
    try:
        url = webutil.validate_outbound_url(target, what="slack_url")
    except webutil.FetchError:
        logger.warning("report slack url rejected")
        return False, "invalid_url"
    port_err = webhooks_svc.webhook_url_allowed(url)
    if port_err is not None:
        logger.warning("report slack url rejected: %s", port_err)
        return False, "invalid_url"
    try:
        ok = messenger.send_slack(url, f"[jobMate] {report_summary(report)}")
    except Exception:
        logger.warning("report slack send error", exc_info=True)
        return False, "failed"
    return (True, "sent") if ok else (False, "failed")


def _deliver_webhook(
    store: Store,
    settings: Settings,
    sub: dict[str, Any],
    kind: str,
    report: dict[str, Any],
    *,
    post: Any = None,
    transport: Any = None,
) -> tuple[bool, str]:
    """HMAC 서명 POST — spec 004 webhook delivery와 동일 서명 규약.

    재시도 큐(webhook_deliveries)를 타지 않는 동기 1회 발송 — 리포트
    구독은 주기당 1회라 큐잉 오버헤드보다 즉시 결과(ok/detail)가 중요하다.
    서명 규약은 services/webhooks.py docstring과 동일:
      X-JobMate-Signature: t=<unix>,v1=<hex HMAC_SHA256(secret, "t.body")>"""
    target = str(sub.get("target") or "")
    host = webutil.host_of(target)
    try:
        url = webutil.validate_outbound_url(target, what="report_webhook_url")
    except webutil.FetchError as e:
        logger.warning("report webhook url rejected host=%s err=%s", host, str(e)[:60])
        return False, "invalid_url"
    port_err = webhooks_svc.webhook_url_allowed(url)
    if port_err is not None:
        logger.warning("report webhook url rejected host=%s err=%s", host, port_err)
        return False, "invalid_url"
    secret = decrypt_str(str(sub.get("secret_enc") or ""))
    if not secret:
        logger.warning("report webhook skipped: no signing secret sub=%s", sub.get("id"))
        return False, "skipped_unconfigured"
    event_id, body = _webhook_payload(sub, kind, report)
    ts = int(time.time())
    headers = {
        "Content-Type": "application/json",
        "X-JobMate-Event": f"{_EVENT_PREFIX}{kind}",
        "X-JobMate-Delivery": str(sub.get("id") or ""),
        "X-JobMate-Timestamp": str(ts),
        "X-JobMate-Signature": f"t={ts},v1={webhooks_svc._sign(secret, ts, body)}",  # noqa: SLF001
    }
    try:
        timeout = float(getattr(settings, "webhook_timeout_seconds", 10.0) or 10.0)
    except (TypeError, ValueError):
        timeout = 10.0
    try:
        resp = webhooks_svc._post_delivery(  # noqa: SLF001 - 동일 전송 경로 재사용
            url, body, headers, timeout, post=post, transport=transport
        )
        status_code = int(getattr(resp, "status_code", 0) or 0)
    except Exception as e:
        # 예외 타입만 기록 — 메시지/URL/시크릿 포함 가능성 배제.
        logger.warning("report webhook send error host=%s err=%s", host, type(e).__name__)
        return False, "failed"
    if 200 <= status_code < 300:
        logger.info("report webhook delivered host=%s status=%d", host, status_code)
        return True, "sent"
    logger.warning("report webhook http_error host=%s status=%d", host, status_code)
    return False, f"http_{status_code}"


def deliver_subscription(
    store: Store,
    settings: Settings,
    sub: dict[str, Any],
    report: dict[str, Any],
    *,
    post: Any = None,
    transport: Any = None,
) -> tuple[bool, str]:
    """구독 1건 발송 — (ok, detail). 성공 시 last_sent_at 갱신."""
    channel = str(sub.get("channel") or "")
    kind = str(sub.get("kind") or report.get("kind") or "")
    target = str(sub.get("target") or "")
    if channel == "email":
        ok, detail = _deliver_email(target, kind, report)
    elif channel == "slack":
        ok, detail = _deliver_slack(target, kind, report)
    elif channel == "webhook":
        ok, detail = _deliver_webhook(
            store, settings, sub, kind, report, post=post, transport=transport
        )
    else:
        ok, detail = False, "unknown_channel"
    if ok:
        try:
            store.touch_report_subscription_sent(str(sub.get("id") or ""))
        except Exception:
            logger.debug("report sub last_sent touch failed", exc_info=True)
    logger.info(
        "report subscription delivery sub=%s kind=%s channel=%s ok=%s detail=%s",
        sub.get("id"),
        kind,
        channel,
        ok,
        detail,
    )
    return ok, detail


def deliver_report_subscriptions(
    store: Store,
    settings: Settings,
    kind: str,
    report: dict[str, Any],
    *,
    post: Any = None,
    transport: Any = None,
) -> dict[str, int]:
    """리포트 생성 주기 훅 — kind에 매칭되는 enabled 구독 전부 발송.

    반환 {"sent": n, "failed": n, "skipped": n}. 구독/채널별 실패는
    warning 로그만 남기고 사이클을 중단하지 않는다."""
    counts = {"sent": 0, "failed": 0, "skipped": 0}
    try:
        subs = [
            s
            for s in store.list_report_subscriptions(enabled_only=True)
            if str(s.get("kind") or "") == kind
        ]
    except Exception:
        logger.warning("report subscription list failed kind=%s", kind, exc_info=True)
        return counts
    for sub in subs:
        try:
            ok, detail = deliver_subscription(
                store, settings, sub, report, post=post, transport=transport
            )
        except Exception:
            logger.warning(
                "report subscription delivery crashed sub=%s",
                sub.get("id"),
                exc_info=True,
            )
            counts["failed"] += 1
            continue
        if ok:
            counts["sent"] += 1
        elif detail == "skipped_unconfigured":
            counts["skipped"] += 1
        else:
            counts["failed"] += 1
    if subs:
        logger.info(
            "report subscriptions delivered kind=%s subs=%d sent=%d failed=%d skipped=%d",
            kind,
            len(subs),
            counts["sent"],
            counts["failed"],
            counts["skipped"],
        )
    return counts
