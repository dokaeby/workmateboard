# mypy: disable-error-code="attr-defined"
"""Store webhook mixin — spec 004 US1 아웃바운드 웹훅 구독/전달 이력.

webhook_subscriptions/webhook_deliveries는 org-scoped 테이블 —
모든 읽기는 orgctx 필터를 적용하고, 쓰기는 org_for_write로 org_id를 찍는다
(comms.py의 event_links/notifications와 동일 규칙). secret_enc는 Fernet
암호문만 저장되며 복호화는 서비스 계층(services/webhooks.py) 책임이다.
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from typing import Any

from . import orgctx
from .util import _clamp_limit, _iso, _now

# delivery 상태 어휘 — FR-002.
DELIVERY_PENDING = "pending"
DELIVERY_DELIVERING = "delivering"
DELIVERY_SUCCESS = "success"
DELIVERY_FAILED = "failed"
DELIVERY_DEAD = "dead"
DELIVERY_CANCELLED = "cancelled"

_CLAIMABLE_STATUSES = ("pending", "failed", "delivering")


def _org_filter() -> tuple[str, list[Any]]:
    """ctx 조직이 있으면 ` AND org_id=?` 조건 — 시스템/미인증(ctx='')은 무필터."""
    oid = orgctx.current_org_id()
    return (" AND org_id=?", [oid]) if oid else ("", [])


def _sub_dict(r: Any) -> dict[str, Any]:
    d = dict(r)
    try:
        ev = json.loads(d.get("events") or "[]")
        d["events"] = [str(e) for e in ev] if isinstance(ev, list) else []
    except (ValueError, TypeError):
        d["events"] = []
    d["enabled"] = bool(d.get("enabled"))
    return d


def _delivery_dict(r: Any) -> dict[str, Any]:
    d = dict(r)
    try:
        d["payload"] = json.loads(d.get("payload_json") or "{}")
    except (ValueError, TypeError):
        d["payload"] = {}
    d["attempts"] = int(d.get("attempts") or 0)
    return d


class WebhookStoreMixin:
    # ---- webhook_subscriptions ----
    def create_webhook_subscription(
        self,
        *,
        url: str,
        secret_enc: str,
        events: list[str],
        created_by: str = "",
        enabled: bool = True,
    ) -> dict[str, Any]:
        now = _now()
        sid = uuid.uuid4().hex[:12]
        with self.tx() as c:
            c.execute(
                "INSERT INTO webhook_subscriptions"
                "(id,org_id,url,secret_enc,events,enabled,created_by,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    sid,
                    orgctx.org_for_write(),
                    url,
                    secret_enc,
                    json.dumps([str(e)[:100] for e in events]),
                    1 if enabled else 0,
                    (created_by or "")[:200],
                    _iso(now),
                    _iso(now),
                ),
            )
        row = self.get_webhook_subscription(sid)
        return row or {}

    def list_webhook_subscriptions(
        self, *, enabled_only: bool = False, limit: int = 200
    ) -> list[dict[str, Any]]:
        frag, params = _org_filter()
        cond = frag.replace(" AND ", " WHERE ", 1) if frag else ""
        if enabled_only:
            cond += (" AND " if cond else " WHERE ") + "enabled=1"
        limit = _clamp_limit(limit, 200, 1000)
        rows = self._conn.execute(
            "SELECT * FROM webhook_subscriptions"  # noqa: S608
            + cond
            + " ORDER BY created_at DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [_sub_dict(r) for r in rows]

    def get_webhook_subscription(self, sub_id: str) -> dict[str, Any] | None:
        frag, params = _org_filter()
        row = self._conn.execute(
            "SELECT * FROM webhook_subscriptions WHERE id=?" + frag,  # noqa: S608
            (sub_id, *params),
        ).fetchone()
        return _sub_dict(row) if row else None

    def update_webhook_subscription(
        self,
        sub_id: str,
        *,
        url: str | None = None,
        events: list[str] | None = None,
        enabled: bool | None = None,
    ) -> bool:
        """부분 갱신 — None 필드는 변경하지 않는다. 갱신 성공 시 True."""
        sets: list[str] = []
        vals: list[Any] = []
        if url is not None:
            sets.append("url=?")
            vals.append(url)
        if events is not None:
            sets.append("events=?")
            vals.append(json.dumps([str(e)[:100] for e in events]))
        if enabled is not None:
            sets.append("enabled=?")
            vals.append(1 if enabled else 0)
        if not sets:
            return False
        sets.append("updated_at=?")
        vals.append(_iso(_now()))
        frag, params = _org_filter()
        with self.tx() as c:
            cur = c.execute(
                "UPDATE webhook_subscriptions SET "  # noqa: S608 - sets는 고정 리터럴
                + ", ".join(sets)
                + " WHERE id=?"
                + frag,
                (*vals, sub_id, *params),
            )
        return cur.rowcount > 0

    def rotate_webhook_secret(self, sub_id: str, secret_enc: str) -> bool:
        frag, params = _org_filter()
        with self.tx() as c:
            cur = c.execute(
                "UPDATE webhook_subscriptions SET secret_enc=?, updated_at=?"  # noqa: S608
                " WHERE id=?" + frag,
                (secret_enc, _iso(_now()), sub_id, *params),
            )
        return cur.rowcount > 0

    def touch_webhook_last_delivery(self, sub_id: str) -> None:
        frag, params = _org_filter()
        with self.tx() as c:
            c.execute(
                "UPDATE webhook_subscriptions SET last_delivery_at=? WHERE id=?"  # noqa: S608
                + frag,
                (_iso(_now()), sub_id, *params),
            )

    def delete_webhook_subscription(self, sub_id: str) -> bool:
        frag, params = _org_filter()
        with self.tx() as c:
            cur = c.execute(
                "DELETE FROM webhook_subscriptions WHERE id=?" + frag,  # noqa: S608
                (sub_id, *params),
            )
        return cur.rowcount > 0

    # ---- webhook_deliveries ----
    def create_webhook_deliveries(self, rows: list[dict[str, Any]]) -> int:
        """delivery 벌크 insert — (subscription_id,event_id) UNIQUE로
        동일 이벤트의 재emit은 행 단위로 무시된다(FR-011 멱등).

        rows 항목: subscription_id, event_type, event_id, payload_json,
        next_retry_at(생략 시 즉시 due), org_id(명시 시 그 조직, 아니면 ctx)."""
        if not rows:
            return 0
        now_iso = _iso(_now())
        created = 0
        with self.tx() as c:
            for r in rows:
                cur = c.execute(
                    "INSERT OR IGNORE INTO webhook_deliveries"
                    "(id,org_id,subscription_id,event_type,event_id,payload_json,"
                    "status,attempts,next_retry_at,last_http_status,last_error,"
                    "created_at,delivered_at)"
                    " VALUES(?,?,?,?,?,?,'pending',0,?,NULL,'',?,NULL)",
                    (
                        uuid.uuid4().hex[:12],
                        orgctx.org_for_write(str(r.get("org_id") or "") or None),
                        str(r.get("subscription_id") or "")[:64],
                        str(r.get("event_type") or "")[:100],
                        str(r.get("event_id") or "")[:200],
                        str(r.get("payload_json") or "{}"),
                        str(r.get("next_retry_at") or now_iso),
                        now_iso,
                    ),
                )
                created += cur.rowcount
        return created

    def claim_webhook_deliveries(
        self, limit: int = 20, lease_seconds: int = 120
    ) -> list[dict[str, Any]]:
        """due delivery를 'delivering'으로 원자 claim — 조직 무관(시스템 배치).

        status IN (pending,failed,delivering): 'delivering'은 claim 시
        next_retry_at에 리스를 심으므로, 리스 만료 = 처리 중 크래시로 보고
        재claim한다. 구독당 동시 in-flight 1건(FR-010) — 같은 subscription의
        다른 delivering 행이 있으면 claim하지 않는다. 반환 행은 호출자가
        org_scope(row.org_id)로 감싸 처리해야 한다."""
        limit = _clamp_limit(limit, 20, 500)
        now_iso = _iso(_now())
        lease_iso = _iso(_now() + timedelta(seconds=max(1, int(lease_seconds))))
        # 후보는 넉넉히 읽고 구독당 1건만 골라 claim한다.
        rows = self._conn.execute(
            "SELECT * FROM webhook_deliveries"
            " WHERE status IN ('pending','failed','delivering')"
            " AND next_retry_at IS NOT NULL AND next_retry_at <= ?"
            " ORDER BY created_at LIMIT ?",
            (now_iso, limit * 4),
        ).fetchall()
        claimed: list[dict[str, Any]] = []
        with self.tx() as c:
            for r in rows:
                if len(claimed) >= limit:
                    break
                # 원자 전이 — 조회~갱신 사이 다른 프로세스가 claim했으면
                # rowcount 0으로 건너뛴다. NOT EXISTS로 타 인스턴스의
                # in-flight delivering도 배제한다(자기 행은 id로 제외 —
                # 'delivering' 리스 만료 재claim 경로 보호).
                cur = c.execute(
                    "UPDATE webhook_deliveries SET status='delivering', next_retry_at=?"
                    " WHERE id=? AND status=?"
                    " AND NOT EXISTS ("
                    "   SELECT 1 FROM webhook_deliveries d2"
                    "   WHERE d2.subscription_id=webhook_deliveries.subscription_id"
                    "   AND d2.status='delivering' AND d2.id != webhook_deliveries.id"
                    " )",
                    (lease_iso, r["id"], r["status"]),
                )
                if cur.rowcount <= 0:
                    continue
                d = _delivery_dict(r)
                d["status"] = DELIVERY_DELIVERING
                claimed.append(d)
        return claimed

    def finish_webhook_delivery(
        self,
        delivery_id: str,
        *,
        status: str,
        attempts: int,
        next_retry_at: str | None = None,
        last_http_status: int | None = None,
        last_error: str = "",
        delivered_at: str | None = None,
    ) -> None:
        """delivery 결과 기록 — status/attempts/next_retry_at/last_http_status/
        last_error/delivered_at를 한 번에 갱신한다."""
        with self.tx() as c:
            c.execute(
                "UPDATE webhook_deliveries SET status=?, attempts=?, next_retry_at=?,"
                " last_http_status=?, last_error=?, delivered_at=? WHERE id=?",
                (
                    status,
                    max(0, int(attempts)),
                    next_retry_at,
                    last_http_status,
                    (last_error or "")[:200],
                    delivered_at,
                    delivery_id,
                ),
            )

    def list_webhook_deliveries(
        self, subscription_id: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        frag, params = _org_filter()
        limit = _clamp_limit(limit, 50, 200)
        rows = self._conn.execute(
            "SELECT * FROM webhook_deliveries WHERE subscription_id=?"  # noqa: S608
            + frag
            + " ORDER BY created_at DESC LIMIT ?",
            (subscription_id, *params, limit),
        ).fetchall()
        return [_delivery_dict(r) for r in rows]

    def webhook_delivery_status_counts(self) -> dict[str, int]:
        """상태별 delivery 건수 — 헬스/메트릭용(org 무관 집계)."""
        rows = self._conn.execute(
            "SELECT status, COUNT(*) AS n FROM webhook_deliveries GROUP BY status"
        ).fetchall()
        return {str(r["status"]): int(r["n"]) for r in rows}
