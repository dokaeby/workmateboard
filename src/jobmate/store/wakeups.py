# mypy: disable-error-code="attr-defined"
"""Wakeup request 스토어 메서드 믹스인 (db.py Store에서 추출)."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from ..models import Run
from .util import _iso, _now

# claim_deferred_wakeup이 status='queued'로 바꾼 뒤 run 생성 전에 프로세스가
# 죽으면 run_id=''인 queued 행이 남는다 — 이 나이가 지난 행은 stale claim으로
# 보고 deferred와 동일하게 재시도 대상으로 취급한다.
STALE_CLAIM_AGE = timedelta(minutes=5)


class WakeupStoreMixin:
    # ---- wakeup_requests (run 생성 단일 비행/코얼레싱 감사) ----
    @staticmethod
    def _wakeup_json(r: Any) -> dict[str, Any]:
        d = dict(r)
        try:
            d["coalesced_count"] = int(d.get("coalesced_count") or 0)
        except (TypeError, ValueError):
            d["coalesced_count"] = 0
        return d

    def create_wakeup(
        self,
        *,
        scope_key: str,
        source: str,
        payload: str = "{}",
        status: str = "queued",
        idempotency_key: str = "",
        run_id: str = "",
        coalesced_count: int = 0,
    ) -> str:
        wid = uuid.uuid4().hex[:12]
        now = _iso(_now())
        with self.tx() as c:
            c.execute(
                "INSERT INTO wakeup_requests(id,source,scope_key,payload,status,"
                "coalesced_count,idempotency_key,run_id,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    wid,
                    source[:40],
                    scope_key[:200],
                    # payload는 TEXT 컬럼 — 자른 JSON은 파싱 불가라
                    # promote 시 입력을 재구성할 수 없다. 절단하지 않는다.
                    payload,
                    status,
                    int(coalesced_count),
                    idempotency_key[:200],
                    run_id,
                    now,
                ),
            )
        return wid

    def get_wakeup(self, wakeup_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM wakeup_requests WHERE id=?", (wakeup_id,)
        ).fetchone()
        return self._wakeup_json(row) if row else None

    def get_wakeup_by_idempotency(self, idempotency_key: str) -> dict[str, Any] | None:
        if not idempotency_key:
            return None
        row = self._conn.execute(
            "SELECT * FROM wakeup_requests WHERE idempotency_key=?"
            " ORDER BY created_at DESC LIMIT 1",
            (idempotency_key,),
        ).fetchone()
        return self._wakeup_json(row) if row else None

    def list_wakeups(
        self,
        limit: int = 50,
        *,
        run_id: str | None = None,
        status: str | None = None,
        source: str | None = None,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM wakeup_requests WHERE 1=1"
        params: list[Any] = []
        if run_id:
            sql += " AND run_id=?"
            params.append(run_id)
        if status:
            sql += " AND status=?"
            params.append(status)
        if source:
            sql += " AND source=?"
            params.append(source)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        return [self._wakeup_json(r) for r in self._conn.execute(sql, params).fetchall()]

    def list_wakeups_for_run(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM wakeup_requests WHERE run_id=? ORDER BY created_at",
            (run_id,),
        ).fetchall()
        return [self._wakeup_json(r) for r in rows]

    def list_active_runs(self, limit: int = 200) -> list[Run]:
        """queued/running run — 코얼레싱 후보 스캔용 (볼륨이 작아 Python 비교)."""
        rows = self._conn.execute(
            "SELECT * FROM runs WHERE status IN ('queued','running')"
            " ORDER BY created_at DESC LIMIT ?",
            (max(1, min(int(limit), 500)),),
        ).fetchall()
        return [self._row_to_run(r) for r in rows]

    def bump_coalesced_for_run(self, run_id: str) -> int:
        """해당 run을 가리키는 wakeup들의 coalesced_count를 +1 — 병합 감사."""
        with self.tx() as c:
            cur = c.execute(
                "UPDATE wakeup_requests SET coalesced_count=coalesced_count+1"
                " WHERE run_id=? AND status IN ('queued','coalesced')",
                (run_id,),
            )
        return int(getattr(cur, "rowcount", 0) or 0)

    def finish_wakeups_for_run(self, run_id: str, status: str) -> int:
        """run 종료 시 연결된 wakeup을 completed/failed로 마감.

        'coalesced' 행은 status를 유지하고 finished_at만 기록한다 —
        run 목록/상세의 병합 뱃지(coalesced_counts_for_runs)가 종결 후에도
        합류 이력을 보여줘야 하기 때문. finished_at이 있어 retention
        sweep의 90일 삭제 대상에는 정상 포함된다."""
        now = _iso(_now())
        with self.tx() as c:
            cur = c.execute(
                "UPDATE wakeup_requests SET finished_at=? WHERE run_id=? AND status='coalesced'",
                (now, run_id),
            )
            n_coalesced = int(getattr(cur, "rowcount", 0) or 0)
            cur = c.execute(
                "UPDATE wakeup_requests SET status=?, finished_at=?"
                " WHERE run_id=? AND status IN ('queued','deferred')",
                (status, now, run_id),
            )
        return int(getattr(cur, "rowcount", 0) or 0) + n_coalesced

    def coalesced_counts_for_runs(self, run_ids: list[str]) -> dict[str, int]:
        """run_id → coalesced 상태 wakeup 수 — run 목록/상세의 병합 뱃지용.

        각 coalesce 요청은 status='coalesced' wakeup 행 1개를 남기므로
        행 수가 곧 합류한 요청 수다(coalesced_count 컬럼은 run 단위
        누적 카운터라 합산하면 중복 계산된다)."""
        ids = [str(r) for r in run_ids if r]
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        rows = self._conn.execute(
            f"SELECT run_id, COUNT(*) AS n FROM wakeup_requests"  # noqa: S608 - placeholders는 '?'만, 값은 바인딩
            f" WHERE run_id IN ({placeholders}) AND status='coalesced'"
            f" GROUP BY run_id",
            ids,
        ).fetchall()
        return {str(r["run_id"]): int(r["n"]) for r in rows}

    @staticmethod
    def _stale_claim_cutoff() -> str:
        return _iso(_now() - STALE_CLAIM_AGE) or ""

    def list_deferred_wakeups(self, scope_key: str, limit: int = 10) -> list[dict[str, Any]]:
        """promote 대상 — status='deferred' + stale claim(queued인데 run_id 없음).

        claim 후 run 생성 전에 프로세스가 죽은 wakeup은 queued/run_id=''로
        남아 아무도 재시도하지 않는다. claimed_at이 STALE_CLAIM_AGE보다 오래된
        행만 회수해 진행 중인 정상 claim은 빼앗지 않는다."""
        cutoff = self._stale_claim_cutoff()
        rows = self._conn.execute(
            "SELECT * FROM wakeup_requests WHERE scope_key=? AND ("
            " status='deferred'"
            " OR (status='queued' AND run_id='' AND claimed_at IS NOT NULL AND claimed_at < ?)"
            ") ORDER BY created_at LIMIT ?",
            (scope_key, cutoff, max(1, min(int(limit), 100))),
        ).fetchall()
        return [self._wakeup_json(r) for r in rows]

    def claim_deferred_wakeup(self, wakeup_id: str) -> bool:
        """deferred(또는 stale queued claim) → queued 원자 claim.

        run 생성 전이 아니라 promotion 잠금. stale queued 회수 시에도
        claimed_at을 갱신해 다음 스캔의 stale 판정 기준을 새로 한다."""
        cutoff = self._stale_claim_cutoff()
        with self.tx() as c:
            cur = c.execute(
                "UPDATE wakeup_requests SET status='queued', claimed_at=?"
                " WHERE id=? AND ("
                "  status='deferred'"
                "  OR (status='queued' AND run_id='' AND claimed_at IS NOT NULL AND claimed_at < ?)"
                " )",
                (_iso(_now()), wakeup_id, cutoff),
            )
        return int(getattr(cur, "rowcount", 0) or 0) > 0

    def fail_wakeup(self, wakeup_id: str) -> None:
        """promote 불가 wakeup을 failed로 마감 — payload 손상 등 재시도 무의미한 경우."""
        with self.tx() as c:
            c.execute(
                "UPDATE wakeup_requests SET status='failed', finished_at=? WHERE id=?",
                (_iso(_now()), wakeup_id),
            )

    def set_wakeup_run(self, wakeup_id: str, run_id: str) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE wakeup_requests SET run_id=? WHERE id=?",
                (run_id, wakeup_id),
            )
