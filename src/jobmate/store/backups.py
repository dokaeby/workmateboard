# mypy: disable-error-code="attr-defined"
"""Store mixin — backup_runs CRUD (spec 004 US3, FR-021).

플랫폼 레벨 테이블 — org_id 없음. file_path에는 디렉터리 내 파일명만
저장한다(절대경로·자격증명 금지).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from .util import _iso, _now


def _row(r: Any) -> dict[str, Any]:
    return {k: r[k] for k in r.keys()}


class BackupStoreMixin:
    # ---- backup_runs ----
    def create_backup_run(
        self,
        *,
        backend: str,
        file_path: str = "",
        size_bytes: int | None = None,
        status: str = "running",
        started_at: datetime | None = None,
    ) -> str:
        """백업 실행 1건 생성 → id 반환. status 기본 'running'."""
        rid = uuid.uuid4().hex[:12]
        with self.tx() as c:
            c.execute(
                "INSERT INTO backup_runs(id,backend,file_path,size_bytes,status,"
                "verify_status,error,started_at,finished_at,verified_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    rid,
                    backend,
                    file_path,
                    size_bytes,
                    status,
                    "pending",
                    "",
                    _iso(started_at or _now()) or "",
                    None,
                    None,
                ),
            )
        return rid

    def finish_backup_run(
        self,
        run_id: str,
        *,
        status: str,
        size_bytes: int | None = None,
        error: str = "",
        file_path: str | None = None,
        finished_at: datetime | None = None,
    ) -> None:
        """실행 종료 — status(success|failed|verify_failed|pruned), 크기/파일명/오류 갱신.

        file_path/size_bytes는 None이면 기존 값 유지(COALESCE), error는 항상 덮어쓴다."""
        with self.tx() as c:
            c.execute(
                "UPDATE backup_runs SET status=?,"
                " size_bytes=COALESCE(?, size_bytes),"
                " file_path=COALESCE(?, file_path),"
                " error=?, finished_at=? WHERE id=?",
                (
                    status,
                    size_bytes,
                    file_path,
                    (error or "")[:500],
                    _iso(finished_at or _now()),
                    run_id,
                ),
            )

    def set_backup_verify(
        self,
        run_id: str,
        verify_status: str,
        *,
        verified_at: datetime | None = None,
    ) -> None:
        """복원 검증 결과 기록 — verify_status(ok|failed|pending)."""
        with self.tx() as c:
            c.execute(
                "UPDATE backup_runs SET verify_status=?, verified_at=? WHERE id=?",
                (verify_status, _iso(verified_at or _now()), run_id),
            )

    def latest_backup_success(self) -> dict[str, Any] | None:
        """가장 최근 성공 백업 행 — 없으면 None."""
        r = self._conn.execute(
            "SELECT * FROM backup_runs WHERE status='success'"
            " ORDER BY finished_at DESC, started_at DESC LIMIT 1"
        ).fetchone()
        return _row(r) if r is not None else None

    def latest_backup_any(self) -> dict[str, Any] | None:
        """가장 최근 백업 행(상태 무관)."""
        r = self._conn.execute(
            "SELECT * FROM backup_runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        return _row(r) if r is not None else None

    def list_backup_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        """최근 백업 실행 목록 — 최신순."""
        try:
            n = max(1, min(int(limit), 500))
        except (TypeError, ValueError):
            n = 50
        rows = self._conn.execute(
            "SELECT * FROM backup_runs ORDER BY started_at DESC LIMIT ?", (n,)
        ).fetchall()
        return [_row(r) for r in rows]

    def get_backup_run(self, run_id: str) -> dict[str, Any] | None:
        r = self._conn.execute("SELECT * FROM backup_runs WHERE id=?", (run_id,)).fetchone()
        return _row(r) if r is not None else None

    def mark_backups_pruned(self, file_names: list[str]) -> int:
        """삭제된 파일명에 대응하는 backup_runs 행을 status='pruned'로 표시.

        success/failed 등 비종결 표시 상태만 전이 — 이미 pruned거나
        running인 행은 건드리지 않는다. 반환: 갱신 행 수."""
        if not file_names:
            return 0
        marks = ",".join("?" for _ in file_names)
        with self.tx() as c:
            cur = c.execute(
                f"UPDATE backup_runs SET status='pruned' WHERE file_path IN ({marks})"  # noqa: S608
                " AND status NOT IN ('pruned','running')",
                tuple(file_names),
            )
            return cur.rowcount if cur.rowcount is not None else 0
