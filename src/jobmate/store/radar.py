# mypy: disable-error-code="attr-defined"
"""Radar & push subscription store methods — mixin for Store."""

import json
from typing import Any

from .util import _iso, _loads, _now


class RadarStoreMixin:
    # ---- radar (skillRadar) ----
    def radar_snapshot_exists(self, platform: str, date_str: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM radar_snapshots WHERE platform=? AND collected_date=?",
            (platform, date_str),
        ).fetchone()
        return row is not None

    def save_radar_snapshot(self, platform: str, date_str: str, metrics: dict[str, Any]) -> int:
        with self.tx() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO radar_snapshots"
                "(platform,collected_date,metrics_json,created_at)"
                " VALUES(?,?,?,?) RETURNING id",
                (platform, date_str, json.dumps(metrics), _iso(_now())),
            )
            row = cur.fetchone()
            if row:
                return int(row["id"] if isinstance(row, dict) else row[0])
            row = c.execute(
                "SELECT id FROM radar_snapshots WHERE platform=? AND collected_date=?",
                (platform, date_str),
            ).fetchone()
            return int(row["id"])

    def delete_radar_snapshot(self, snapshot_id: int) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM radar_items WHERE snapshot_id=?", (snapshot_id,))
            c.execute("DELETE FROM radar_snapshots WHERE id=?", (snapshot_id,))

    def add_radar_item(self, snapshot_id: int, item: dict[str, Any]) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO radar_items(snapshot_id,rank,repo_full_name,"
                "repo_url,description,stars,language,topics_json,"
                "readme_excerpt,summary_en,summary_zh,summary_ko,"
                "content_hash,stars_today,license,taxonomy,"
                "taxonomy_confidence,rank_score,summary_status)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    snapshot_id,
                    item.get("rank", 0),
                    item["repo_full_name"],
                    item.get("repo_url", ""),
                    item.get("description", ""),
                    item.get("stars", 0),
                    item.get("language", ""),
                    json.dumps(item.get("topics") or []),
                    item.get("readme_excerpt", ""),
                    item.get("summary_en", ""),
                    item.get("summary_zh", ""),
                    item.get("summary_ko", ""),
                    item.get("content_hash", ""),
                    item.get("stars_today"),
                    item.get("license", ""),
                    item.get("taxonomy", ""),
                    item.get("taxonomy_confidence", 0.0),
                    item.get("rank_score", 0.0),
                    item.get("summary_status", ""),
                ),
            )

    def radar_items(self, snapshot_id: int) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM radar_items WHERE snapshot_id=? ORDER BY rank", (snapshot_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def radar_snapshot(self, platform: str, date_str: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM radar_snapshots WHERE platform=? AND collected_date=?",
            (platform, date_str),
        ).fetchone()
        return dict(row) if row else None

    def radar_dates(self, platform: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT collected_date FROM radar_snapshots WHERE platform=?"
            " ORDER BY collected_date DESC",
            (platform,),
        ).fetchall()
        return [r["collected_date"] for r in rows]

    def radar_prev_stars(self, platform: str, date_str: str) -> dict[str, int]:
        row = self._conn.execute(
            "SELECT id FROM radar_snapshots WHERE platform=?"
            " AND collected_date<? ORDER BY collected_date DESC LIMIT 1",
            (platform, date_str),
        ).fetchone()
        if not row:
            return {}
        return {
            r["repo_full_name"]: r["stars"]
            for r in self._conn.execute(
                "SELECT repo_full_name, stars FROM radar_items WHERE snapshot_id=?", (row["id"],)
            ).fetchall()
        }

    def radar_cached_repo(self, full_name: str, chash: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT summary_en,summary_zh,summary_ko,taxonomy,"
            "taxonomy_confidence,summary_status FROM radar_items"
            " WHERE repo_full_name=? AND content_hash=?"
            " AND summary_en != '' ORDER BY id DESC LIMIT 1",
            (full_name, chash),
        ).fetchone()
        return dict(row) if row else None

    def radar_items_since(self, since_date: str) -> list[dict[str, Any]]:
        """since_date 이후 수집분의 item 행 + platform/collected_date (토픽·랭킹·digest용)."""
        rows = self._conn.execute(
            "SELECT i.*, s.platform AS platform, s.collected_date AS collected_date"
            " FROM radar_items i JOIN radar_snapshots s ON i.snapshot_id = s.id"
            " WHERE s.collected_date >= ?",
            (since_date,),
        ).fetchall()
        return [dict(r) for r in rows]

    def radar_repo_names_before(self, date_str: str) -> set[str]:
        """date_str 이전에 한 번이라도 수집된 repo_full_name 집합."""
        rows = self._conn.execute(
            "SELECT DISTINCT i.repo_full_name FROM radar_items i"
            " JOIN radar_snapshots s ON i.snapshot_id = s.id"
            " WHERE s.collected_date < ?",
            (date_str,),
        ).fetchall()
        return {r["repo_full_name"] for r in rows}

    def radar_repo_latest(self, full_name: str) -> dict[str, Any] | None:
        """repo의 가장 최근 수집 item + platform/collected_date."""
        row = self._conn.execute(
            "SELECT i.*, s.platform AS platform, s.collected_date AS collected_date"
            " FROM radar_items i JOIN radar_snapshots s ON i.snapshot_id = s.id"
            " WHERE i.repo_full_name = ?"
            " ORDER BY s.collected_date DESC, i.rank ASC LIMIT 1",
            (full_name,),
        ).fetchone()
        return dict(row) if row else None

    # ---- push_subscriptions ----
    def save_push_subscription(self, endpoint: str, keys: dict[str, Any]) -> None:
        from ..security import encrypt_str

        with self.tx() as c:
            c.execute(
                "INSERT INTO push_subscriptions(endpoint,keys_json,created_at)"
                " VALUES(?,?,?) ON CONFLICT (endpoint) DO UPDATE SET"
                " keys_json=excluded.keys_json",
                (endpoint, encrypt_str(json.dumps(keys)), _iso(_now())),
            )

    def delete_push_subscription(self, endpoint: str) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM push_subscriptions WHERE endpoint=?", (endpoint,))

    def list_push_subscriptions(self) -> list[dict[str, Any]]:
        from ..security import decrypt_str

        rows = self._conn.execute("SELECT * FROM push_subscriptions").fetchall()
        return [
            {"endpoint": r["endpoint"], "keys": _loads(decrypt_str(r["keys_json"]))} for r in rows
        ]

    def cleanup_radar_waits(self, reaped_out: list[str] | None = None) -> int:
        """skill_radar의 waiting_approval 잔여 run을 cancelled 처리하고
        관련 인박스 승인 알림을 삭제한다.

        radar는 모든 실행 경로(daily 자동수집/cron refresh/수동 refresh)에서
        auto_approve=True로 무인 발행되므로, 승인 대기에 남은 run은
        auto_approve 도입 이전의 비정상 잔여물이다.
        reaped_out이 주어지면 취소된 run id를 append한다 — 호출자가
        wakeup finalize(on_run_finished)로 deferred promote를 이어가게 하기 위함."""
        now = _iso(_now())
        cancelled: list[str] = []
        with self.tx() as c:
            rows = c.execute(
                "SELECT id FROM runs WHERE workflow_key='skill_radar' AND status='waiting_approval'"
            ).fetchall()
            if not rows:
                return 0
            for r in rows:
                rid = str(r["id"])
                c.execute(
                    "UPDATE runs SET status='cancelled', error=?, updated_at=? WHERE id=?",
                    ("radar_auto_cleanup: approval was not required", now, rid),
                )
                cancelled.append(rid)
            c.execute(
                "DELETE FROM inbox_items WHERE kind='approval' AND title LIKE '%skill_radar%'"
            )
        if reaped_out is not None:
            reaped_out.extend(cancelled)
        return len(cancelled)
