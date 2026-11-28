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
from . import orgctx
from .util import _clamp_limit, _dt, _iso, _loads, _now


def _org_filter() -> tuple[str, list[Any]]:
    """ctx 조직이 있으면 ` AND org_id=?` 조건 — 시스템/미인증(ctx='')은 무필터."""
    oid = orgctx.current_org_id()
    return (" AND org_id=?", [oid]) if oid else ("", [])


class CommsStoreMixin:
    def _parent_org(self, table: str, column: str, value: str) -> str:
        """부모 행의 org_id 조회 — (table,column)은 고정 리터럴 호출만."""
        v = (value or "").strip()[:64]
        if not v:
            return ""
        row = self._conn.execute(
            f"SELECT org_id FROM {table} WHERE {column}=?",  # noqa: S608 - 고정 리터럴
            (v,),
        ).fetchone()
        return str(row["org_id"] or "") if row is not None else ""

    def reserve_link(
        self,
        idempotency_key: str,
        gmail_message_id: str,
        connection_id: str,
        source_url: str,
        org_id: str | None = None,
    ) -> EventLink | None:
        """멱등 키로 pending 링크 예약. 이미 있으면 None 반환."""
        now = _now()
        # org 귀속: 명시 > connector 부모 > ctx — run/웹훅 실행(ctx='')에서도
        # 링크가 올바른 조직에 기록되게 한다.
        oid = (org_id or "").strip() or self._parent_org("connectors", "id", connection_id)
        if not oid:
            oid = orgctx.org_for_write()
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
                    "connection_id,status,source_url,created_at,updated_at,org_id)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        link.id,
                        link.idempotency_key,
                        link.gmail_message_id,
                        link.connection_id,
                        link.status.value,
                        link.source_url,
                        _iso(link.created_at),
                        _iso(link.updated_at),
                        oid,
                    ),
                )
        except self._integrity_errors:
            return None
        return link

    def get_link_by_key(self, key: str) -> EventLink | None:
        # spec 003 US3/FR-018 — UNIQUE(org_id,idempotency_key)로 재스코프.
        # 같은 키가 조직별로 공존할 수 있으므로 조회도 조직 범위로 제한한다
        # (ctx='' 시스템 경로는 무필터 — 다른 조직 행을 임의로 집을 수
        # 있으나 호출 경로는 org_scope 아래에서 실행된다).
        frag, params = _org_filter()
        row = self._conn.execute(
            "SELECT * FROM event_links WHERE idempotency_key=?" + frag,  # noqa: S608
            (key, *params),
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
        frag, params = _org_filter()
        limit = _clamp_limit(limit)
        rows = self._conn.execute(
            "SELECT * FROM event_links"  # noqa: S608
            + frag.replace(" AND ", " WHERE ", 1)
            + " ORDER BY created_at DESC LIMIT ?",
            (*params, limit),
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
        org_id: str | None = None,
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
        # org 귀속: 명시 > run/link 부모 > ctx — 알림 발신 경로(ctx='')에서도
        # 부모 리소스의 조직으로 기록된다.
        oid = (org_id or "").strip()
        if not oid:
            oid = self._parent_org("runs", "id", run_id)
        if not oid:
            oid = self._parent_org("event_links", "id", link_id)
        if not oid:
            oid = orgctx.org_for_write()
        with self.tx() as c:
            c.execute(
                "INSERT INTO notifications(id,run_id,link_id,channel,title,body,source_url,"
                "status,created_at,org_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
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
                    oid,
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
        frag, params = _org_filter()
        limit = _clamp_limit(limit)
        rows = self._conn.execute(
            "SELECT * FROM notifications"  # noqa: S608
            + frag.replace(" AND ", " WHERE ", 1)
            + " ORDER BY created_at DESC LIMIT ?",
            (*params, limit),
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
        org_id: str | None = None,
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
                "processing_status,source_text,retry_at,created_at,org_id)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    provider,
                    source_id,
                    thread_id,
                    json.dumps(extraction),
                    status,
                    source_text,
                    retry_at,
                    _iso(now),
                    orgctx.org_for_write(org_id),
                ),
            )
            return cur.rowcount > 0

    def has_message(self, provider: str, source_id: str) -> bool:
        # spec 003 US3 — message_extractions PK가 (provider,source_id,org_id)로
        # 재스코프되므로 중복 판정도 같은 조직 범위로 제한한다.
        frag, params = _org_filter()
        row = self._conn.execute(
            "SELECT 1 FROM message_extractions WHERE source_provider=? AND source_id=?"  # noqa: S608
            + frag,
            (provider, source_id, *params),
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

    def claim_message(self, provider: str, source_id: str, org_id: str | None = None) -> bool:
        """메시지 원자 claim — pending(또는 리스 만료 processing)을 processing으로.

        UPDATE ... WHERE processing_status 조건 + retry_at 리스 설정을 한
        문장으로 수행해, 복수 worker/cron tick이 같은 메시지를 동시에
        처리해 run을 중복 생성하는 것을 막는다. rowcount>0이면 claim 성공.
        org_id 명시 시 ctx와 무관하게 그 조직 행만 claim한다('' 포함) —
        ctx='' 배치 루프에서 같은 키의 타 조직 행까지 뒤집히는 것을 막는다."""
        lease_until = _iso(_now() + timedelta(seconds=self._MESSAGE_LEASE_SECONDS))
        now_iso = _iso(_now())
        # spec 003 US3 — 복합 PK(provider,source_id,org_id)에서 같은 키가
        # 조직별로 존재할 수 있으므로 조직 범위로 claim을 제한한다.
        if org_id is not None:
            frag, fparams = " AND org_id=?", [org_id]
        else:
            frag, fparams = _org_filter()
        with self.tx() as c:
            cur = c.execute(
                "UPDATE message_extractions SET processing_status='processing',"  # noqa: S608
                " retry_at=?"
                " WHERE source_provider=? AND source_id=?"
                " AND processing_status IN ('pending','processing')"
                " AND retry_at <= ?" + frag,  # noqa: S608
                (lease_until, provider, source_id, now_iso, *fparams),
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
        org_id: str | None = None,
    ) -> None:
        # org_id 명시면 ctx와 무관하게 그 조직 행만 갱신한다('' 포함) —
        # claim과 동일하게 복합 PK의 타 조직 행을 건드리지 않기 위함.
        if org_id is not None:
            frag, fparams = " AND org_id=?", [org_id]
        else:
            frag, fparams = _org_filter()
        with self.tx() as c:
            c.execute(
                "UPDATE message_extractions SET processing_status=?,"  # noqa: S608
                " error_message=?, retry_at=?, attempts=?"
                " WHERE source_provider=? AND source_id=?" + frag,  # noqa: S608
                (status, error, retry_at, attempts, provider, source_id, *fparams),
            )

    def list_message_extractions(
        self, provider: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        limit = _clamp_limit(limit, 20, 500)
        frag, fparams = _org_filter()
        q = "SELECT * FROM message_extractions"
        conds: list[str] = []
        args: list[Any] = []
        if frag:
            conds.append(frag.strip().removeprefix("AND").strip())
            args.extend(fparams)
        if provider:
            conds.append("source_provider=?")
            args.append(provider)
        if conds:
            q += " WHERE " + " AND ".join(conds)
        q += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        out = []
        for r in self._conn.execute(q, args).fetchall():
            d = dict(r)
            d["extraction"] = _loads(d.pop("extraction_json"))
            out.append(d)
        return out

    def _source_message_org(self, provider: str, source_id: str) -> str:
        """message_extractions 부모 행의 org_id — ctx 조직 범위 우선.

        복합 PK(source_provider,source_id,org_id)라 같은 키가 조직별로 존재할
        수 있으므로 ctx 조직의 행을 먼저 보고, 없으면 org_id 오름차순 첫 행을
        결정적으로 선택한다('' org 행이 가장 먼저 온다)."""
        prov = (provider or "")[:64]
        sid = (source_id or "")[:64]
        ctx_oid = orgctx.current_org_id()
        if ctx_oid:
            row = self._conn.execute(
                "SELECT org_id FROM message_extractions"
                " WHERE source_provider=? AND source_id=? AND org_id=?",
                (prov, sid, ctx_oid),
            ).fetchone()
            if row is not None:
                return ctx_oid
        row = self._conn.execute(
            "SELECT org_id FROM message_extractions"
            " WHERE source_provider=? AND source_id=? ORDER BY org_id LIMIT 1",
            (prov, sid),
        ).fetchone()
        return str(row["org_id"] or "") if row is not None else ""

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
        org_id: str | None = None,
    ) -> str:
        now = _iso(_now())
        # org 귀속: 명시 > message_extractions 부모 > ctx — 캘린더/이메일
        # 파이프라인(ctx='')에서도 후보 이벤트가 올바른 조직에 기록된다.
        oid = (org_id or "").strip()
        if not oid:
            oid = self._source_message_org(source_provider, source_id)
        if not oid:
            oid = orgctx.org_for_write()
        # scheduled_events.id는 전역 PK — 같은 메시지를 조직별로 처리할 때
        # event_id가 충돌해 타 조직 행을 덮어쓰지 않도록 org를 접두한다.
        if oid and not event_id.startswith(f"{oid}:"):
            event_id = f"{oid}:{event_id}"
        with self.tx() as c:
            c.execute(
                "INSERT INTO scheduled_events(id,source_provider,source_id,"
                "thread_id,calendar_event_id,title,start_at,end_at,timezone,"
                "location,attendees_json,summary,status,confidence,reason,"
                "source_refs,created_at,updated_at,org_id)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
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
                    oid,
                ),
            )
        return event_id

    def get_scheduled_by_source(
        self, provider: str, source_id: str, org_id: str | None = None
    ) -> dict[str, Any] | None:
        # 조직 범위 필수 — 무필터면 같은 메시지의 타 조직 이벤트 행을 반환한다.
        # org 해석 순서는 save_scheduled_event와 동일(명시 > 부모 > ctx).
        oid = (org_id or "").strip()
        if not oid:
            oid = self._source_message_org(provider, source_id)
        if not oid:
            oid = orgctx.org_for_write()
        frag = " AND org_id=?" if oid else ""
        params: list[Any] = [oid] if oid else []
        # 동일 조직에 구버전(무접두) id 행과 신규(접두) id 행이 공존할 수
        # 있으므로 최신 행을 결정적으로 반환한다.
        row = self._conn.execute(
            "SELECT * FROM scheduled_events WHERE source_provider=? AND source_id=?"  # noqa: S608
            + frag
            + " ORDER BY updated_at DESC LIMIT 1",
            (provider, source_id, *params),
        ).fetchone()
        return dict(row) if row else None

    def list_scheduled_events(
        self, status: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        limit = _clamp_limit(limit)
        frag, fparams = _org_filter()
        q = "SELECT * FROM scheduled_events"
        conds: list[str] = []
        args: list[Any] = []
        if frag:
            conds.append(frag.strip().removeprefix("AND").strip())
            args.extend(fparams)
        if status:
            conds.append("status=?")
            args.append(status)
        if conds:
            q += " WHERE " + " AND ".join(conds)
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
        frag, fparams = _org_filter()
        with self.tx() as c:
            cur = c.execute(
                "UPDATE scheduled_events SET status='registering', updated_at=?"  # noqa: S608
                " WHERE id=? AND (status IN"
                " ('needs_review','needs_confirmation','candidate')"
                " OR (status='registering' AND updated_at < ?))" + frag,
                (now, event_id, stale_before, *fparams),
            )
        return cur.rowcount > 0

    def get_scheduled_event(self, event_id: str) -> dict[str, Any] | None:
        frag, params = _org_filter()
        row = self._conn.execute(
            "SELECT * FROM scheduled_events WHERE id=?" + frag,  # noqa: S608
            (event_id, *params),
        ).fetchone()
        return dict(row) if row else None

    def update_scheduled_event(self, event_id: str, **fields: Any) -> bool:
        sets = {k: v for k, v in fields.items() if k in self._SCHEDULED_EVENT_FIELDS}
        if not sets:
            return False
        sets["updated_at"] = _iso(_now())
        frag, fparams = _org_filter()
        cols = ", ".join(f"{k}=?" for k in sets)
        with self.tx() as c:
            cur = c.execute(
                f"UPDATE scheduled_events SET {cols}, version=version+1 WHERE id=?"  # noqa: S608 - cols는 화이트리스트 필드명만 사용
                + frag,
                (*sets.values(), event_id, *fparams),
            )
        return cur.rowcount > 0

    # ---- notification_jobs ----
    def save_notification_job(self, scheduled_event_id: str, notify_type: str, fire_at: str) -> str:
        # org 귀속은 부모 scheduled_events 행에서 상속 — scheduled_events.id는
        # `oid:provider:source` 네임스페이스라 여기 전달되는 키는 비접두
        # `provider:source_id`이므로 source 컬럼으로 조회한다. 동일 소스를
        # 공유하는 조직이 있어도 ctx 조직 행을 우선하고 결정적으로 폴백한다.
        oid = ""
        prov, _, sid = scheduled_event_id.partition(":")
        if prov and sid:
            cur_oid = orgctx.current_org_id()
            if cur_oid:
                row = self._conn.execute(
                    "SELECT org_id FROM scheduled_events"
                    " WHERE source_provider=? AND source_id=? AND org_id=?",
                    (prov, sid, cur_oid),
                ).fetchone()
            else:
                row = None
            if row is None:
                row = self._conn.execute(
                    "SELECT org_id FROM scheduled_events"
                    " WHERE source_provider=? AND source_id=?"
                    " ORDER BY org_id LIMIT 1",
                    (prov, sid),
                ).fetchone()
            if row is not None:
                oid = str(row["org_id"] or "")
        if not oid:
            oid = orgctx.org_for_write()
        # job_id는 org 네임스페이스 — 동일 provider:source를 공유하는 조직의
        # 알림 job이 INSERT OR IGNORE로 서로를 drop하지 않게 한다.
        job_id = f"job:{oid}:{scheduled_event_id}:{notify_type}:{fire_at}"
        now = _iso(_now())
        with self.tx() as c:
            c.execute(
                "INSERT OR IGNORE INTO notification_jobs"
                "(id,scheduled_event_id,notify_type,fire_at,status,"
                "created_at,updated_at,org_id) VALUES(?,?,?,?,'pending',?,?,?)",
                (job_id, scheduled_event_id, notify_type, fire_at, now, now, oid),
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
        frag, params = _org_filter()
        limit = _clamp_limit(limit)
        rows = self._conn.execute(
            "SELECT * FROM notification_jobs"  # noqa: S608
            + frag.replace(" AND ", " WHERE ", 1)
            + " ORDER BY fire_at DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- radar (skillRadar) ----
