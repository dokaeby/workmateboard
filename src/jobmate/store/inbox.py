# mypy: disable-error-code="attr-defined"
from __future__ import annotations

import json
import uuid
from typing import Any

from .util import _clamp_limit, _iso, _loads, _now


class InboxStoreMixin:
    def upsert_inbox_item(
        self,
        kind: str,
        title: str,
        body: str = "",
        link: str = "",
        dedupe_key: str = "",
        reset_read: bool = False,
    ) -> str:
        """인박스 항목 추가. dedupe_key가 있으면 기존 행을 갱신(읽음 상태는 유지,
        reset_read=True면 미읽음으로 되돌림) — 세션 어텐션/승인 대기처럼
        반복 발생하는 이벤트의 중복을 막는다."""
        now = _iso(_now())
        with self.tx() as c:
            if dedupe_key:
                row = c.execute(
                    "SELECT id FROM inbox_items WHERE dedupe_key=?", (dedupe_key,)
                ).fetchone()
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
                "created_at) VALUES(?,?,?,?,?,0,?,?)",
                (iid, kind, title, body, link, dedupe_key, now),
            )
            return iid

    def list_inbox(self, limit: int = 50, unread_only: bool = False) -> list[dict[str, Any]]:
        cols = "id,kind,title,body,link,read,created_at"
        if unread_only:
            sql = f"SELECT {cols} FROM inbox_items WHERE read=0"  # noqa: S608 - cols는 고정 상수
        else:
            sql = f"SELECT {cols} FROM inbox_items"  # noqa: S608 - cols는 고정 상수
        sql += " ORDER BY created_at DESC LIMIT ?"
        rows = self._conn.execute(sql, (_clamp_limit(limit, 50, 200),)).fetchall()
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
        with self.tx() as c:
            cur = c.execute("UPDATE inbox_items SET read=1 WHERE id=?", (item_id,))
        return cur.rowcount > 0

    def mark_inbox_read_dedupe(self, dedupe_key: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE inbox_items SET read=1 WHERE dedupe_key=?", (dedupe_key,))

    def mark_all_inbox_read(self) -> int:
        with self.tx() as c:
            cur = c.execute("UPDATE inbox_items SET read=1 WHERE read=0")
        return cur.rowcount

    def inbox_unread_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM inbox_items WHERE read=0").fetchone()
        return int(row["n"]) if row else 0

    def sync_notifications_to_inbox(self, limit: int = 200) -> int:
        """notifications 테이블 → inbox_items(kind='notification') 지연 미러링.
        dedupe_key=notif:{id}로 멱등 — GET /inbox 호출 시마다 호출해도 안전."""
        rows = self._conn.execute(
            "SELECT id,title,body,source_url FROM notifications ORDER BY created_at DESC LIMIT ?",
            (_clamp_limit(limit, 200, 1000),),
        ).fetchall()
        for r in rows:
            self.upsert_inbox_item(
                "notification",
                r["title"],
                body=r["body"],
                link=r["source_url"],
                dedupe_key=f"notif:{r['id']}",
            )
        return len(rows)

    def save_report(self, kind: str, content: dict[str, Any]) -> str:
        rid = uuid.uuid4().hex[:12]
        with self.tx() as c:
            c.execute(
                "INSERT INTO reports(id,kind,content,created_at) VALUES(?,?,?,?)",
                (rid, kind, json.dumps(content, ensure_ascii=False), _iso(_now())),
            )
        return rid

    def latest_report(self, kind: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT id,kind,content,created_at FROM reports WHERE kind=?"
            " ORDER BY created_at DESC LIMIT 1",
            (kind,),
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
        rows = self._conn.execute(
            "SELECT id,kind,created_at FROM reports ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_report(self, report_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT id,kind,content,created_at FROM reports WHERE id=?", (report_id,)
        ).fetchone()
        if not row:
            return None
        return {
            "id": row["id"],
            "kind": row["kind"],
            "content": _loads(row["content"]),
            "created_at": row["created_at"],
        }
