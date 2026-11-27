# mypy: disable-error-code="attr-defined"
"""Store comms mixin — event_links/notifications/automation/state/messages/scheduled_events."""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from typing import Any

from ..models import (
    EventLink,
    LinkStatus,
    Notification,
    NotificationStatus,
)
from .util import _clamp_limit, _dt, _iso, _loads, _now


class CommsStoreMixin:
    def reserve_link(
        self, idempotency_key: str, gmail_message_id: str, connection_id: str, source_url: str
    ) -> EventLink | None:
        """멱등 키로 pending 링크 예약. 이미 있으면 None 반환."""
        now = _now()
        link = EventLink(
            id=uuid.uuid4().hex[:12],
            idempotency_key=idempotency_key,
            gmail_message_id=gmail_message_id,
            connection_id=connection_id,
            status=LinkStatus.DISCOVERED,
            source_url=source_url,
            created_at=now,
            updated_at=now,
        )
        try:
            with self.tx() as c:
                c.execute(
                    "INSERT INTO event_links(id,idempotency_key,gmail_message_id,"
                    "connection_id,status,source_url,created_at,updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (
                        link.id,
                        link.idempotency_key,
                        link.gmail_message_id,
                        link.connection_id,
                        link.status.value,
                        link.source_url,
                        _iso(link.created_at),
                        _iso(link.updated_at),
                    ),
                )
        except self._integrity_errors:
            return None
        return link

    def get_link_by_key(self, key: str) -> EventLink | None:
        row = self._conn.execute(
            "SELECT * FROM event_links WHERE idempotency_key=?", (key,)
        ).fetchone()
        return self._row_to_link(row) if row else None

    def get_link_by_message(self, message_id: str) -> EventLink | None:
        row = self._conn.execute(
            "SELECT * FROM event_links WHERE gmail_message_id=? ORDER BY created_at DESC LIMIT 1",
            (message_id,),
        ).fetchone()
        return self._row_to_link(row) if row else None

    def update_link(
        self,
        link_id: str,
        *,
        status: LinkStatus | None = None,
        calendar_event_id: str | None = None,
        notification_id: str | None = None,
        last_error: str | None = None,
        attempts_inc: bool = False,
    ) -> None:
        link = self._conn.execute("SELECT * FROM event_links WHERE id=?", (link_id,)).fetchone()
        if not link:
            return
        with self.tx() as c:
            c.execute(
                "UPDATE event_links SET status=?, calendar_event_id=?,"
                "notification_id=?, last_error=?, attempts=attempts+?, updated_at=?"
                " WHERE id=?",
                (
                    (status or LinkStatus(link["status"])).value,
                    calendar_event_id
                    if calendar_event_id is not None
                    else link["calendar_event_id"],
                    notification_id if notification_id is not None else link["notification_id"],
                    last_error if last_error is not None else link["last_error"],
                    1 if attempts_inc else 0,
                    _iso(_now()),
                    link_id,
                ),
            )

    def list_links(self, limit: int = 50) -> list[EventLink]:
        limit = _clamp_limit(limit)
        rows = self._conn.execute(
            "SELECT * FROM event_links ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [self._row_to_link(r) for r in rows]

    def _row_to_link(self, row: Any) -> EventLink:
        return EventLink(
            id=row["id"],
            idempotency_key=row["idempotency_key"],
            gmail_message_id=row["gmail_message_id"],
            connection_id=row["connection_id"],
            status=LinkStatus(row["status"]),
            calendar_event_id=row["calendar_event_id"],
            notification_id=row["notification_id"],
            source_url=row["source_url"],
            attempts=row["attempts"],
            last_error=row["last_error"],
            created_at=_dt(row["created_at"]) or _now(),
            updated_at=_dt(row["updated_at"]) or _now(),
        )

    # ---- notifications ----
    def add_notification(
        self,
        title: str,
        body: str = "",
        source_url: str = "",
        run_id: str = "",
        link_id: str = "",
        channel: str = "inapp",
    ) -> Notification:
        n = Notification(
            id=uuid.uuid4().hex[:12],
            run_id=run_id,
            link_id=link_id,
            channel=channel,
            title=title,
            body=body,
            source_url=source_url,
            status=NotificationStatus.PENDING,
            created_at=_now(),
        )
        with self.tx() as c:
            c.execute(
                "INSERT INTO notifications(id,run_id,link_id,channel,title,body,source_url,"
                "status,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    n.id,
                    n.run_id,
                    n.link_id,
                    n.channel,
                    n.title,
                    n.body,
                    n.source_url,
                    n.status.value,
                    _iso(n.created_at),
                ),
            )
        return n

    def set_notification_status(self, notif_id: str, status: NotificationStatus) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE notifications SET status=?, sent_at=? WHERE id=?",
                (
                    status.value,
                    _iso(_now()) if status == NotificationStatus.SENT else None,
                    notif_id,
                ),
            )

    def list_notifications(self, limit: int = 50) -> list[Notification]:
        limit = _clamp_limit(limit)
        rows = self._conn.execute(
            "SELECT * FROM notifications ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [self._row_to_notif(r) for r in rows]

    def _row_to_notif(self, row: Any) -> Notification:
        return Notification(
            id=row["id"],
            run_id=row["run_id"],
            link_id=row["link_id"],
            channel=row["channel"],
            title=row["title"],
            body=row["body"],
            source_url=row["source_url"],
            status=NotificationStatus(row["status"]),
            created_at=_dt(row["created_at"]) or _now(),
            sent_at=_dt(row["sent_at"]),
        )

    # ---- automation state ----
    def get_automation(self) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT value FROM automation_state WHERE key='gmail_calendar'"
        ).fetchone()
        return _loads(row["value"]) if row else {"enabled": False}

    def set_automation(self, data: dict[str, Any]) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO automation_state(key,value) VALUES('gmail_calendar',?)"
                " ON CONFLICT (key) DO UPDATE SET value=excluded.value",
                (json.dumps(data),),
            )

    # ---- 범용 상태 (automation_state key/value) ----
    def get_state(self, key: str, default: dict[str, Any] | None = None) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT value FROM automation_state WHERE key=?", (key,)
        ).fetchone()
        return _loads(row["value"]) if row else (default or {})

    def set_state(self, key: str, data: dict[str, Any]) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO automation_state(key,value) VALUES(?,?)"
                " ON CONFLICT (key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(data)),
            )

    # ---- message_extractions (멀티채널 수신함) ----
    def save_message_extraction(
        self,
        provider: str,
        source_id: str,
        thread_id: str,
        extraction: dict[str, Any],
        status: str,
        source_text: str = "",
        retry_at: str = "",
    ) -> bool:
        """INSERT OR IGNORE — 재전송이면 False.

        retry_at을 미래로 주면 pending_messages/claim_message가 리스 만료
        전까지 집지 않는다 — 웹훅이 커맨드 처리 동안 행이 intake worker에
        선점되는 경쟁을 막는 용도."""
        now = _now()
        with self.tx() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO message_extractions"
                "(source_provider,source_id,thread_id,extraction_json,"
                "processing_status,source_text,retry_at,created_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (
                    provider,
                    source_id,
                    thread_id,
                    json.dumps(extraction),
                    status,
                    source_text,
                    retry_at,
                    _iso(now),
                ),
            )
            return cur.rowcount > 0

    def has_message(self, provider: str, source_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM message_extractions WHERE source_provider=? AND source_id=?",
            (provider, source_id),
        ).fetchone()
        return row is not None

    # 처리 중 메시지 리스 — claim 시 retry_at을 미래로 밀어 다른 worker가
    # 같은 행을 집지 않게 한다. 프로세스 크래시면 리스 만료 후 재claim된다.
    # gmail_calendar run은 LLM 다단계라 수 분 걸릴 수 있어 리스를 넉넉히 둔다.
    # 크래시 시 이 시간만큼 처리가 지연될 뿐 중복 처리는 없다.
    _MESSAGE_LEASE_SECONDS = 1800

    def pending_messages(self, limit: int = 100) -> list[dict[str, Any]]:
        """처리 대상 메시지 — 'pending'이거나 리스 만료된 'processing' 행.

        retry_at은 백오프(미래) 또는 리스(미래) 표시로도 쓰인다. ''(신규)는
        어떤 ISO 문자열보다 작으므로 즉시 대상이 된다."""
        rows = self._conn.execute(
            "SELECT * FROM message_extractions"
            " WHERE processing_status IN ('pending','processing')"
            " AND retry_at <= ? ORDER BY created_at LIMIT ?",
            (_iso(_now()), max(1, min(int(limit), 1000))),
        ).fetchall()
        return [dict(r) for r in rows]

    def claim_message(self, provider: str, source_id: str) -> bool:
        """메시지 원자 claim — pending(또는 리스 만료 processing)을 processing으로.

        UPDATE ... WHERE processing_status 조건 + retry_at 리스 설정을 한
        문장으로 수행해, 복수 worker/cron tick이 같은 메시지를 동시에
        처리해 run을 중복 생성하는 것을 막는다. rowcount>0이면 claim 성공."""
        lease_until = _iso(_now() + timedelta(seconds=self._MESSAGE_LEASE_SECONDS))
        now_iso = _iso(_now())
        with self.tx() as c:
            cur = c.execute(
                "UPDATE message_extractions SET processing_status='processing',"
                " retry_at=?"
                " WHERE source_provider=? AND source_id=?"
                " AND processing_status IN ('pending','processing')"
                " AND retry_at <= ?",
                (lease_until, provider, source_id, now_iso),
            )
        return cur.rowcount > 0

    def finish_message(
        self,
        provider: str,
        source_id: str,
        status: str,
        error: str = "",
        retry_at: str = "",
        attempts: int = 0,
    ) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE message_extractions SET processing_status=?,"
                " error_message=?, retry_at=?, attempts=?"
                " WHERE source_provider=? AND source_id=?",
                (status, error, retry_at, attempts, provider, source_id),
            )

    def list_message_extractions(
        self, provider: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        limit = _clamp_limit(limit, 20, 500)
        q = "SELECT * FROM message_extractions"
        args: list[Any] = []
        if provider:
            q += " WHERE source_provider=?"
            args.append(provider)
        q += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        out = []
        for r in self._conn.execute(q, args).fetchall():
            d = dict(r)
            d["extraction"] = _loads(d.pop("extraction_json"))
            out.append(d)
        return out

    # ---- scheduled_events ----
    def save_scheduled_event(
        self,
        *,
        event_id: str,
        source_provider: str,
        source_id: str,
        thread_id: str = "",
        calendar_event_id: str = "",
        title: str,
        start_at: str,
        end_at: str,
        timezone: str,
        location: str = "",
        attendees_json: str = "[]",
        summary: str = "",
        status: str = "candidate",
        confidence: float = 0.0,
        reason: str = "",
        source_refs: str = "",
    ) -> str:
        now = _iso(_now())
        with self.tx() as c:
            c.execute(
                "INSERT INTO scheduled_events(id,source_provider,source_id,"
                "thread_id,calendar_event_id,title,start_at,end_at,timezone,"
                "location,attendees_json,summary,status,confidence,reason,"
                "source_refs,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT (id) DO UPDATE SET status=excluded.status,"
                " calendar_event_id=excluded.calendar_event_id,"
                " version=version+1, updated_at=excluded.updated_at",
                (
                    event_id,
                    source_provider,
                    source_id,
                    thread_id,
                    calendar_event_id,
                    title,
                    start_at,
                    end_at,
                    timezone,
                    location,
                    attendees_json,
                    summary,
                    status,
                    confidence,
                    reason,
                    source_refs,
                    now,
                    now,
                ),
            )
        return event_id

    def get_scheduled_by_source(self, provider: str, source_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM scheduled_events WHERE source_provider=? AND source_id=?",
            (provider, source_id),
        ).fetchone()
        return dict(row) if row else None

    def list_scheduled_events(
        self, status: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        limit = _clamp_limit(limit)
        q = "SELECT * FROM scheduled_events"
        args: list[Any] = []
        if status:
            q += " WHERE status=?"
            args.append(status)
        q += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self._conn.execute(q, args).fetchall()]

    _SCHEDULED_EVENT_FIELDS = (
        "title",
        "start_at",
        "end_at",
        "timezone",
        "location",
        "attendees_json",
        "summary",
        "status",
        "confidence",
        "reason",
        "calendar_event_id",
        "source_refs",
    )

    def claim_event_review(self, event_id: str, stale_before: str) -> bool:
        """일정 검토 원자 claim — status를 'registering'으로 조건부 전이.

        needs_review/needs_confirmation/candidate 상태이거나, 이전 claim이
        크래시로 남은 'registering'(updated_at < stale_before)일 때만 성공.
        find_by_marker→create_event 사이의 중복 캘린더 생성을 막는다.
        rowcount>0이면 claim 성공 — 호출자는 작업 후 registered/rejected 등으로
        전이하거나, 실패 시 원래 상태로 복구해야 한다."""
        now = _iso(_now())
        with self.tx() as c:
            cur = c.execute(
                "UPDATE scheduled_events SET status='registering', updated_at=?"
                " WHERE id=? AND (status IN"
                " ('needs_review','needs_confirmation','candidate')"
                " OR (status='registering' AND updated_at < ?))",
                (now, event_id, stale_before),
            )
        return cur.rowcount > 0

    def get_scheduled_event(self, event_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM scheduled_events WHERE id=?", (event_id,)
        ).fetchone()
        return dict(row) if row else None

    def update_scheduled_event(self, event_id: str, **fields: Any) -> bool:
        sets = {k: v for k, v in fields.items() if k in self._SCHEDULED_EVENT_FIELDS}
        if not sets:
            return False
        sets["updated_at"] = _iso(_now())
        cols = ", ".join(f"{k}=?" for k in sets)
        with self.tx() as c:
            cur = c.execute(
                f"UPDATE scheduled_events SET {cols}, version=version+1 WHERE id=?",  # noqa: S608 - cols는 화이트리스트 필드명만 사용
                (*sets.values(), event_id),
            )
        return cur.rowcount > 0

    # ---- notification_jobs ----
    def save_notification_job(self, scheduled_event_id: str, notify_type: str, fire_at: str) -> str:
        job_id = f"job:{scheduled_event_id}:{notify_type}:{fire_at}"
        now = _iso(_now())
        with self.tx() as c:
            c.execute(
                "INSERT OR IGNORE INTO notification_jobs"
                "(id,scheduled_event_id,notify_type,fire_at,status,"
                "created_at,updated_at) VALUES(?,?,?,?,'pending',?,?)",
                (job_id, scheduled_event_id, notify_type, fire_at, now, now),
            )
        return job_id

    # claim 후 프로세스가 죽으면 processing에 고착되므로, updated_at이
    # 이 시간을 넘긴 processing 행은 다시 due로 취급해 재발송을 허용한다.
    _JOB_PROCESSING_STALE_SECONDS = 600

    def due_notification_jobs(self, now: str | None = None) -> list[dict[str, Any]]:
        now_dt = _now() if now is None else (_dt(now) or _now())
        stale_before = _iso(now_dt - timedelta(seconds=self._JOB_PROCESSING_STALE_SECONDS))
        rows = self._conn.execute(
            "SELECT * FROM notification_jobs WHERE fire_at <= ?"
            " AND (status='pending'"
            "      OR (status='processing' AND updated_at <= ?))"
            " ORDER BY fire_at",
            (_iso(now_dt), stale_before),
        ).fetchall()
        return [dict(r) for r in rows]

    def claim_job(self, job_id: str) -> bool:
        """알림 job 원자 claim — pending(또는 stale processing)만 processing으로.

        동시 cron tick/복수 인스턴스가 같은 job을 집어 알림을 중복 발송하는
        것을 막는다. rowcount>0이면 claim 성공."""
        with self.tx() as c:
            cur = c.execute(
                "UPDATE notification_jobs SET status='processing', updated_at=?"
                " WHERE id=? AND (status='pending'"
                "      OR (status='processing' AND updated_at <= ?))",
                (
                    _iso(_now()),
                    job_id,
                    _iso(_now() - timedelta(seconds=self._JOB_PROCESSING_STALE_SECONDS)),
                ),
            )
        return cur.rowcount > 0

    def mark_job(self, job_id: str, status: str, error: str = "") -> None:
        delivered = _iso(_now()) if status == "delivered" else None
        with self.tx() as c:
            c.execute(
                "UPDATE notification_jobs SET status=?, delivered_at=?,"
                " error_message=?, updated_at=? WHERE id=?",
                (status, delivered, error, _iso(_now()), job_id),
            )

    def list_notification_jobs(self, limit: int = 50) -> list[dict[str, Any]]:
        limit = _clamp_limit(limit)
        rows = self._conn.execute(
            "SELECT * FROM notification_jobs ORDER BY fire_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- radar (skillRadar) ----
