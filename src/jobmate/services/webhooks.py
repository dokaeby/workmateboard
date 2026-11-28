"""spec 004 US1 — 아웃바운드 웹훅 이벤트 emit + delivery 처리.

- emit_event: 이벤트 타입/ID/데이터 → 매칭 구독별 delivery 생성의
  단일 진입점(FR-011). payload는 {"v","id","type","created_at","data"}
  스키마이며 data는 식별자 수준 정보만 담는다(FR-007 — 프롬프트/본문/
  토큰/이메일 금지).
- process_webhook_deliveries: due delivery claim → HTTP POST →
  성공/실패/재시도/backoff/dead 전이(FR-006/FR-008/FR-010).

수신자 검증용 서명 규약(문서화 — 라우터 docstring에도 명시):
  X-JobMate-Timestamp: <unix seconds>
  X-JobMate-Signature: t=<unix>,v1=<hex>
  v1 = hex( HMAC_SHA256( secret, "<t>.<raw_body_utf8>" ) )
  즉 "{t}." + 요청 본문 UTF-8 바이트열을 secret 키로 서명한다.

로그 규칙(FR-034): 성공/실패·HTTP 상태·소요시간만 기록하고,
시크릿·응답 본문·URL userinfo는 절대 남기지 않는다(호스트만 host_of).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

import httpx

from .. import webutil
from ..config import Settings
from ..security import decrypt_str
from ..store import orgctx
from ..store.db import Store

logger = logging.getLogger("jobmate.webhooks")

PAYLOAD_VERSION = "1.0"
# FR-008 — 재시도 백오프(1m→5m→30m→2h→6h), 최대 5회 재시도 후 dead.
RETRY_BACKOFF_SECONDS = (60, 300, 1800, 7200, 21600)
MAX_RETRIES = len(RETRY_BACKOFF_SECONDS)
_TEST_EVENT_TYPE = "webhook.test"
# delivery 종결 상태 — 이 상태의 행은 _finish_failure로 덮어쓰지 않는다.
_TERMINAL_STATUSES = frozenset({"success", "dead", "cancelled"})


def _loopback_allowed_ports() -> set[int] | None:
    """loopback 호스트에 허용할 포트 집합 — None이면 제한 없음.

    JOBMATE_WEBHOOK_LOOPBACK_PORTS(쉼표 구분 정수)로 설정한다.
    미설정/빈 문자열이면 모든 포트 허용 — 기존 동작·테스트 호환.
    설정됐는데 파싱 가능한 포트가 없으면 빈 집합 = loopback 전면 차단
    (fail-closed). config.py를 건드리지 않기 위해 env를 직접 읽는다."""
    raw = os.environ.get("JOBMATE_WEBHOOK_LOOPBACK_PORTS", "")
    if not raw.strip():
        return None
    ports: set[int] = set()
    for tok in raw.split(","):
        tok = tok.strip()
        if tok.isdigit():
            ports.add(int(tok))
    return ports


def webhook_url_allowed(url: str) -> str | None:
    """loopback 웹훅 URL의 포트 allowlist 검사 — 위반 시 에러 문자열,
    허용 시 None. 비-loopback 호스트는 항상 None(무관).

    loopback은 http 평문 채널이 허용되는 유일한 구간이라 임의 포트
    (localhost:6379 등 내부 TCP 서비스)로의 SSRF를 막기 위해 포트
    allowlist를 둔다. 저장 시점(라우터)과 전송 시점(재검증) 양쪽에서
    호출한다."""
    try:
        parsed = urlparse(url if "://" in url else f"https://{url}")
    except ValueError:
        return None  # 파싱 불가는 validate_outbound_url이 이미 거부
    host = (parsed.hostname or "").strip().lower()
    if not webutil.is_loopback_hostname(host):
        return None
    allowed = _loopback_allowed_ports()
    if allowed is None:
        return None
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return "webhook_url_bad_port"
    if port not in allowed:
        return "webhook_url_loopback_port_not_allowed"
    return None


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def build_event_payload(event_type: str, event_id: str, data: dict[str, Any]) -> str:
    """FR-007 페이로드 렌더링 — 버전 스키마 JSON 문자열."""
    payload = {
        "v": PAYLOAD_VERSION,
        "id": event_id,
        "type": event_type,
        "created_at": _utcnow().isoformat(),
        "data": data if isinstance(data, dict) else {},
    }
    return json.dumps(payload, ensure_ascii=False)


def emit_event(store: Store, event_type: str, event_id: str, data: dict[str, Any]) -> int:
    """이벤트 → 현재 org ctx의 매칭 구독별 delivery 생성 (FR-011).

    구독의 events 배열이 event_type을 포함하거나 '*' 와일드카드를
    포함하면 매칭. (subscription_id, event_id) UNIQUE 덕분에 같은
    event_id 재emit은 멱등 no-op이다. 생성된 delivery 수를 반환한다.
    호출자는 org_scope(행 org) 안에서 호출해야 한다 — ctx=''는 전 조직."""
    event_type = str(event_type or "").strip()[:100]
    event_id = str(event_id or "").strip()[:200]
    if not event_type or not event_id:
        return 0
    payload_json = build_event_payload(event_type, event_id, data)
    now_iso = _iso(_utcnow())
    rows: list[dict[str, Any]] = []
    for sub in store.list_webhook_subscriptions(enabled_only=True):
        events = sub.get("events") or []
        if event_type not in events and "*" not in events:
            continue
        rows.append(
            {
                "subscription_id": str(sub.get("id") or ""),
                "event_type": event_type,
                "event_id": event_id,
                "payload_json": payload_json,
                "next_retry_at": now_iso,
                "org_id": str(sub.get("org_id") or ""),
            }
        )
    if not rows:
        return 0
    return store.create_webhook_deliveries(rows)


def enqueue_test_event(store: Store, subscription: dict[str, Any]) -> str:
    """POST /{id}/test용 — 구독 1건에 webhook.test delivery를 적재한다.

    이벤트 필터를 거치지 않고 직접 insert — 전달 자체는 일반
    delivery 경로(process_webhook_deliveries)가 담당한다."""
    sub_id = str(subscription.get("id") or "")
    event_id = f"test:{sub_id}:{uuid.uuid4().hex[:10]}"
    payload_json = build_event_payload(
        _TEST_EVENT_TYPE,
        event_id,
        {"subscription_id": sub_id, "test": True},
    )
    now_iso = _iso(_utcnow())
    created = store.create_webhook_deliveries(
        [
            {
                "subscription_id": sub_id,
                "event_type": _TEST_EVENT_TYPE,
                "event_id": event_id,
                "payload_json": payload_json,
                "next_retry_at": now_iso,
                "org_id": str(subscription.get("org_id") or ""),
            }
        ]
    )
    return event_id if created else ""


def _sign(secret: str, timestamp: int, body: bytes) -> str:
    """v1 = hex(HMAC_SHA256(secret, f"{t}.{raw_body}")) — body는 UTF-8 JSON."""
    msg = str(timestamp).encode("ascii") + b"." + body
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def _post_delivery(
    url: str,
    body: bytes,
    headers: dict[str, str],
    timeout: float,
    *,
    post: Any = None,
    transport: httpx.BaseTransport | None = None,
) -> Any:
    """웹훅 POST — 리다이렉트 미추적, 공인 호스트는 pinned transport로
    DNS 리바인딩 차단(messenger._post_json과 동일 패턴)."""
    if post is not None:
        return post(url, body, headers, timeout)
    if transport is not None:
        with httpx.Client(transport=transport, timeout=timeout) as client:
            return client.post(url, content=body, headers=headers, follow_redirects=False)
    pinned = webutil.pinned_transport_for(url)
    if pinned is not None:
        with httpx.Client(transport=pinned, timeout=timeout) as client:
            return client.post(url, content=body, headers=headers, follow_redirects=False)
    return httpx.post(url, content=body, headers=headers, timeout=timeout, follow_redirects=False)


def _backoff_at(attempts: int) -> str:
    """attempts(1-base, 실패 직후 시도 횟수) 기준 다음 재시도 시각."""
    idx = min(max(attempts - 1, 0), len(RETRY_BACKOFF_SECONDS) - 1)
    return _iso(_utcnow() + timedelta(seconds=RETRY_BACKOFF_SECONDS[idx]))


def _finish_failure(
    store: Store,
    delivery_id: str,
    attempts: int,
    *,
    last_error: str,
    last_http_status: int | None,
) -> None:
    """실패 전이 — 재시도 잔여 있으면 'failed'+backoff, 소진 시 'dead'.

    이미 종결(success/dead/cancelled)된 행은 덮어쓰지 않는다 —
    성공 finish 이후의 예외(예: last_delivery_at 갱신 실패)가
    success→failed 전이로 되돌려 중복 POST를 유발하는 것을 막는다."""
    try:
        row = store._conn.execute(  # noqa: SLF001 - 서비스 계층 store 내부 조회 허용
            "SELECT status FROM webhook_deliveries WHERE id=?", (delivery_id,)
        ).fetchone()
    except Exception:
        row = None
    if row is not None and str(row["status"]) in _TERMINAL_STATUSES:
        logger.info(
            "webhook failure finish skipped id=%s current=%s",
            delivery_id,
            row["status"],
        )
        return
    if attempts > MAX_RETRIES:
        store.finish_webhook_delivery(
            delivery_id,
            status="dead",
            attempts=attempts,
            next_retry_at=None,
            last_http_status=last_http_status,
            last_error=last_error,
        )
        logger.warning(
            "webhook delivery dead id=%s attempts=%d err=%s",
            delivery_id,
            attempts,
            last_error,
        )
        return
    store.finish_webhook_delivery(
        delivery_id,
        status="failed",
        attempts=attempts,
        next_retry_at=_backoff_at(attempts),
        last_http_status=last_http_status,
        last_error=last_error,
    )
    logger.warning(
        "webhook delivery failed id=%s attempts=%d err=%s retry_at=%s",
        delivery_id,
        attempts,
        last_error,
        _backoff_at(attempts),
    )


def _deliver_one(
    store: Store,
    delivery: dict[str, Any],
    *,
    timeout: float,
    post: Any = None,
    transport: httpx.BaseTransport | None = None,
) -> None:
    """claim된 delivery 1건의 전송 — 조직 컨텍스트를 행 org로 재적용한다."""
    oid = str(delivery.get("org_id") or "")
    if oid and orgctx.current_org_id() != oid:
        with orgctx.org_scope(oid):
            return _deliver_one(store, delivery, timeout=timeout, post=post, transport=transport)
    did = str(delivery.get("id") or "")
    sub_id = str(delivery.get("subscription_id") or "")
    attempts = int(delivery.get("attempts") or 0) + 1
    sub = store.get_webhook_subscription(sub_id)
    if sub is None or not sub.get("enabled"):
        store.finish_webhook_delivery(
            did,
            status="cancelled",
            attempts=attempts,
            last_error="subscription_disabled",
        )
        logger.info("webhook delivery cancelled id=%s reason=subscription_disabled", did)
        return
    url = str(sub.get("url") or "")
    host = webutil.host_of(url)
    try:
        url = webutil.validate_outbound_url(url, what="webhook_url")
    except webutil.FetchError as e:
        _finish_failure(
            store,
            did,
            attempts,
            last_error=str(e)[:80],
            last_http_status=None,
        )
        return
    # 전송 시점 재검증 — 저장 후 env allowlist가 바뀌었거나 우회 저장된
    # loopback 비표준 포트 URL을 여기서 다시 걸러 SSRF를 차단한다.
    port_err = webhook_url_allowed(url)
    if port_err is not None:
        _finish_failure(
            store,
            did,
            attempts,
            last_error=port_err,
            last_http_status=None,
        )
        return
    body = str(delivery.get("payload_json") or "{}").encode("utf-8")
    secret = decrypt_str(str(sub.get("secret_enc") or ""))
    ts = int(time.time())
    headers = {
        "Content-Type": "application/json",
        "X-JobMate-Event": str(delivery.get("event_type") or ""),
        "X-JobMate-Delivery": did,
        "X-JobMate-Timestamp": str(ts),
        "X-JobMate-Signature": f"t={ts},v1={_sign(secret, ts, body)}",
    }
    started = time.monotonic()
    try:
        resp = _post_delivery(url, body, headers, timeout, post=post, transport=transport)
        status_code = int(getattr(resp, "status_code", 0) or 0)
    except Exception as e:
        # 예외 타입만 기록 — 메시지/URL/시크릿 포함 가능성을 배제(FR-034).
        _finish_failure(
            store,
            did,
            attempts,
            last_error=type(e).__name__,
            last_http_status=None,
        )
        logger.warning(
            "webhook delivery error id=%s host=%s err=%s ms=%d",
            did,
            host,
            type(e).__name__,
            int((time.monotonic() - started) * 1000),
        )
        return
    elapsed_ms = int((time.monotonic() - started) * 1000)
    if 200 <= status_code < 300:
        store.finish_webhook_delivery(
            did,
            status="success",
            attempts=attempts,
            last_http_status=status_code,
            last_error="",
            delivered_at=_iso(_utcnow()),
        )
        # best-effort — 이 갱신이 실패해도 delivery는 이미 success로
        # 종결됐으므로 _finish_failure로 되돌리지 않는다(중복 POST 방지).
        try:
            store.touch_webhook_last_delivery(sub_id)
        except Exception:
            logger.debug("webhook last_delivery touch failed id=%s", did, exc_info=True)
        logger.info(
            "webhook delivered id=%s host=%s status=%d attempts=%d ms=%d",
            did,
            host,
            status_code,
            attempts,
            elapsed_ms,
        )
        return
    _finish_failure(
        store,
        did,
        attempts,
        last_error=f"http_{status_code}",
        last_http_status=status_code,
    )
    logger.warning(
        "webhook delivery http_error id=%s host=%s status=%d attempts=%d ms=%d",
        did,
        host,
        status_code,
        attempts,
        elapsed_ms,
    )


def process_webhook_deliveries(
    store: Store,
    settings: Settings,
    *,
    post: Any = None,
    transport: httpx.BaseTransport | None = None,
) -> int:
    """due delivery 최대 settings.webhook_batch건을 claim·전송한다.

    건당 timeout=settings.webhook_timeout_seconds, 구독당 동시 1건
    (store claim의 NOT EXISTS 가드). post/transport는 테스트 주입용 —
    post(url, body, headers, timeout) -> 응답 유사 객체(status_code)."""
    try:
        timeout = float(getattr(settings, "webhook_timeout_seconds", 10.0) or 10.0)
    except (TypeError, ValueError):
        timeout = 10.0
    try:
        batch = int(getattr(settings, "webhook_batch", 20) or 20)
    except (TypeError, ValueError):
        batch = 20
    # claim 리스 — 전송 timeout의 3배(최소 60s). 크래시 시 리스 만료 후 재claim.
    lease = max(60, int(timeout * 3))
    deliveries = store.claim_webhook_deliveries(limit=batch, lease_seconds=lease)
    for d in deliveries:
        try:
            _deliver_one(store, d, timeout=timeout, post=post, transport=transport)
        except Exception:
            # _deliver_one 내부에서 finish까지 갔어야 할 예외 — delivery를
            # 'failed'로 되돌려 리스 만료까지 'delivering'에 고착되지 않게 한다.
            logger.warning(
                "webhook delivery processing failed id=%s",
                d.get("id"),
                exc_info=True,
            )
            try:
                attempts = int(d.get("attempts") or 0) + 1
                _finish_failure(
                    store,
                    str(d.get("id") or ""),
                    attempts,
                    last_error="internal_error",
                    last_http_status=None,
                )
            except Exception:
                logger.debug("webhook failure finish failed", exc_info=True)
    return len(deliveries)
