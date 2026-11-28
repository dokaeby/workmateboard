# mypy: disable-error-code="attr-defined"
from __future__ import annotations

import json
import uuid
from typing import Any

from . import orgctx
from .util import _clamp_limit, _iso, _loads, _now


def _org_filter() -> tuple[str, list[Any]]:
    """ctx 조직이 있으면 ` AND org_id=?` 조건 — 시스템/미인증(ctx='')은 무필터."""
    oid = orgctx.current_org_id()
    return (" AND org_id=?", [oid]) if oid else ("", [])


class InboxStoreMixin:
    @staticmethod
    def _dedupe_key(dedupe_key: str, oid: str) -> str:
        """dedupe_key는 전역 UNIQUE 인덱스 — 조직별 네임스페이스를 붙여
        다른 조직의 같은 키가 이 조직 행을 갱신하지 못하게 한다.
        org_id=''(시스템/레거시) 행은 기존처럼 원시 키를 유지."""
        return f"{oid}:{dedupe_key}" if dedupe_key and oid else dedupe_key

    def upsert_inbox_item(
        self,
        kind: str,
        title: str,
        body: str = "",
        link: str = "",
        dedupe_key: str = "",
        reset_read: bool = False,
        org_id: str | None = None,
    ) -> str:
        """인박스 항목 추가. dedupe_key가 있으면 기존 행을 갱신(읽음 상태는 유지,
        reset_read=True면 미읽음으로 되돌림) — 세션 어텐션/승인 대기처럼
        반복 발생하는 이벤트의 중복을 막는다."""
        now = _iso(_now())
        oid = orgctx.org_for_write(org_id)
        key = self._dedupe_key(dedupe_key, oid)
        with self.tx() as c:
            if key:
                row = c.execute("SELECT id FROM inbox_items WHERE dedupe_key=?", (key,)).fetchone()
                if row is not None:
                    iid = row["id"]
                    # created_at는 유지 — 반복 이벤트가 목록 순서를 바꾸지 않게
                    c.execute(
                        "UPDATE inbox_items SET kind=?, title=?, body=?, link=?,"
                        " read=CASE WHEN ? THEN 0 ELSE read END WHERE id=?",
                        (kind, title, body, link, 1 if reset_read else 0, iid),
                    )
                    return iid
            iid = uuid.uuid4().hex[:12]
            c.execute(
                "INSERT INTO inbox_items(id,kind,title,body,link,read,dedupe_key,"
                "created_at,org_id) VALUES(?,?,?,?,?,0,?,?,?)",
                (iid, kind, title, body, link, key, now, oid),
            )
            return iid

    def list_inbox(self, limit: int = 50, unread_only: bool = False) -> list[dict[str, Any]]:
        cols = "id,kind,title,body,link,read,created_at"
        conds = ["read=0"] if unread_only else []
        frag, params = _org_filter()
        if frag:
            conds.append(frag.strip().removeprefix("AND").strip())
        sql = f"SELECT {cols} FROM inbox_items"  # noqa: S608 - cols는 고정 상수
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params = [*params, _clamp_limit(limit, 50, 200)]
        rows = self._conn.execute(sql, params).fetchall()
        return [
            {
                "id": r["id"],
                "kind": r["kind"],
                "title": r["title"],
                "body": r["body"],
                "link": r["link"],
                "read": bool(r["read"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    def mark_inbox_read(self, item_id: str) -> bool:
        frag, params = _org_filter()
        with self.tx() as c:
            cur = c.execute(
                "UPDATE inbox_items SET read=1 WHERE id=?" + frag,  # noqa: S608
                (item_id, *params),
            )
        return cur.rowcount > 0

    def mark_inbox_read_dedupe(self, dedupe_key: str) -> None:
        frag, params = _org_filter()
        key = self._dedupe_key(dedupe_key, orgctx.org_for_write())
        with self.tx() as c:
            c.execute(
                "UPDATE inbox_items SET read=1 WHERE dedupe_key=?" + frag,  # noqa: S608
                (key, *params),
            )

    def mark_all_inbox_read(self) -> int:
        frag, params = _org_filter()
        with self.tx() as c:
            cur = c.execute("UPDATE inbox_items SET read=1 WHERE read=0" + frag, tuple(params))  # noqa: S608
        return cur.rowcount

    def inbox_unread_count(self) -> int:
        frag, params = _org_filter()
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM inbox_items WHERE read=0" + frag,  # noqa: S608
            tuple(params),
        ).fetchone()
        return int(row["n"]) if row else 0

    def sync_notifications_to_inbox(self, limit: int = 200) -> int:
        """notifications 테이블 → inbox_items(kind='notification') 지연 미러링.
        dedupe_key=notif:{id}로 멱등 — GET /inbox 호출 시마다 호출해도 안전."""
        frag, params = _org_filter()
        sql = "SELECT id,title,body,source_url,org_id FROM notifications"
        if frag:
            sql += frag.replace(" AND ", " WHERE ", 1)
        sql += " ORDER BY created_at DESC LIMIT ?"
        rows = self._conn.execute(sql, (*params, _clamp_limit(limit, 200, 1000))).fetchall()
        for r in rows:
            self.upsert_inbox_item(
                "notification",
                r["title"],
                body=r["body"],
                link=r["source_url"],
                dedupe_key=f"notif:{r['id']}",
                org_id=r["org_id"] or None,
            )
        return len(rows)

    def save_report(self, kind: str, content: dict[str, Any], org_id: str | None = None) -> str:
        rid = uuid.uuid4().hex[:12]
        with self.tx() as c:
            c.execute(
                "INSERT INTO reports(id,kind,content,created_at,org_id) VALUES(?,?,?,?,?)",
                (
                    rid,
                    kind,
                    json.dumps(content, ensure_ascii=False),
                    _iso(_now()),
                    orgctx.org_for_write(org_id),
                ),
            )
        return rid

    def latest_report(self, kind: str) -> dict[str, Any] | None:
        frag, params = _org_filter()
        row = self._conn.execute(
            "SELECT id,kind,content,created_at FROM reports WHERE kind=?"  # noqa: S608
            + frag
            + " ORDER BY created_at DESC LIMIT 1",
            (kind, *params),
        ).fetchone()
        if not row:
            return None
        return {
            "id": row["id"],
            "kind": row["kind"],
            "content": _loads(row["content"]),
            "created_at": row["created_at"],
        }

    def list_reports(self, limit: int = 20) -> list[dict[str, Any]]:
        frag, params = _org_filter()
        sql = "SELECT id,kind,created_at FROM reports"
        if frag:
            sql += frag.replace(" AND ", " WHERE ", 1)
        sql += " ORDER BY created_at DESC LIMIT ?"
        rows = self._conn.execute(sql, (*params, limit)).fetchall()
        return [dict(r) for r in rows]

    def get_report(self, report_id: str) -> dict[str, Any] | None:
        frag, params = _org_filter()
        row = self._conn.execute(
            "SELECT id,kind,content,created_at FROM reports WHERE id=?" + frag,  # noqa: S608
            (report_id, *params),
        ).fetchone()
        if not row:
            return None
        return {
            "id": row["id"],
            "kind": row["kind"],
            "content": _loads(row["content"]),
            "created_at": row["created_at"],
        }
