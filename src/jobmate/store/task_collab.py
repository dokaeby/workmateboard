# mypy: disable-error-code="attr-defined"
"""Task 협업 스토어 메서드 믹스인 (db.py Store에서 추출).

agent-management 3차 (paperclip spec B) — task_comments/task_artifacts
영속화. 스키마는 schema_sqlite.py/schema_pg.py에 이미 정의돼 있다.
어휘/URI/길이 검증은 서비스 계층(jobmate.services.task_collab)이 담당하고,
여기서는 원자적 쓰기·조회만 제공한다.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from . import orgctx
from .util import _clamp_limit, _iso, _loads, _now


def _org_bind() -> list[str]:
    """org_id 필터 바인딩 — `AND (org_id=? OR ?='')` 조건에 넘길 [oid, oid].

    ctx=''이면 OR 우항이 항상 참이라 전 조직 조회(시스템/스케줄러),
    ctx가 있으면 해당 조직 행만 조회·갱신한다(spec 003 US3/FR-013)."""
    oid = orgctx.current_org_id()
    return [oid, oid]


_COMMENT_AUTHOR_TYPES = ("human", "agent", "system")
_COMMENT_CONTENT_TYPES = ("text", "markdown")
_ARTIFACT_KINDS = (
    "plan",
    "diff",
    "test_report",
    "security_report",
    "review",
    "screenshot",
    "deployment",
    "other",
)


class TaskCollabStoreMixin:
    # ---- task_comments ----
    @staticmethod
    def _comment_json(r: Any) -> dict[str, Any]:
        d = dict(r)
        at = str(d.get("author_type") or "").strip().lower()
        d["author_type"] = at if at in _COMMENT_AUTHOR_TYPES else "system"
        ct = str(d.get("content_type") or "").strip().lower()
        d["content_type"] = ct if ct in _COMMENT_CONTENT_TYPES else "text"
        return d

    def create_task_comment(
        self,
        task_id: str,
        *,
        author_type: str,
        author_id: str = "",
        body: str,
        content_type: str = "text",
        org_id: str | None = None,
    ) -> dict[str, Any] | None:
        """댓글 추가 — task_id는 canonical id(uuid). 실패 시 None."""
        body = body.strip()[:4000]
        if not body:
            return None
        cid = uuid.uuid4().hex[:12]
        now = _iso(_now())
        at = (author_type or "").strip().lower()[:20]
        if at not in _COMMENT_AUTHOR_TYPES:
            at = "system"
        ct = (content_type or "").strip().lower()[:20]
        if ct not in _COMMENT_CONTENT_TYPES:
            ct = "text"
        # org_id는 부모 task의 org를 전파 — 스케줄러·내부 경로(ctx='')에서도
        # task가 속한 조직으로 기록된다(FR-013). 없으면 명시/요청 컨텍스트.
        oid = ""
        trow = self._conn.execute("SELECT org_id FROM tasks WHERE id=?", (task_id,)).fetchone()
        if trow is not None:
            oid = str(trow["org_id"] or "")
        oid = oid or orgctx.org_for_write(org_id)
        with self.tx() as c:
            c.execute(
                "INSERT INTO task_comments(id,task_id,author_type,author_id,body,"
                "content_type,org_id,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (cid, task_id, at, (author_id or "").strip()[:200], body, ct, oid, now),
            )
        return self.get_task_comment(cid)

    def get_task_comment(self, comment_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM task_comments WHERE id=? AND (org_id=? OR ?='')",
            (comment_id, *_org_bind()),
        ).fetchone()
        return self._comment_json(row) if row else None

    def list_task_comments(self, task_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        """댓글 목록 — 작성 순(created_at ASC, id ASC) 고정."""
        rows = self._conn.execute(
            "SELECT * FROM task_comments WHERE task_id=? AND (org_id=? OR ?='')"
            " ORDER BY created_at, id LIMIT ?",
            (task_id, *_org_bind(), _clamp_limit(limit, 100, 500)),
        ).fetchall()
        return [self._comment_json(r) for r in rows]

    def update_task_comment(self, comment_id: str, *, body: str) -> dict[str, Any] | None:
        """댓글 본문 수정 — edited_at을 기록한다(감사 가능 상태 보존)."""
        body = body.strip()[:4000]
        if not body:
            return None
        with self.tx() as c:
            cur = c.execute(
                "UPDATE task_comments SET body=?, edited_at=? WHERE id=? AND (org_id=? OR ?='')",
                (body, _iso(_now()), comment_id, *_org_bind()),
            )
        if int(getattr(cur, "rowcount", 0) or 0) <= 0:
            return None
        return self.get_task_comment(comment_id)

    # ---- task_artifacts ----
    @staticmethod
    def _artifact_json(r: Any) -> dict[str, Any]:
        d = dict(r)
        k = str(d.get("kind") or "").strip().lower()
        d["kind"] = k if k in _ARTIFACT_KINDS else "other"
        try:
            d["size_bytes"] = int(d["size_bytes"]) if d.get("size_bytes") is not None else None
        except (TypeError, ValueError):
            d["size_bytes"] = None
        d["metadata"] = _loads(str(d.get("metadata") or "{}"))
        return d

    def create_task_artifact(
        self,
        task_id: str,
        *,
        kind: str = "other",
        name: str,
        uri: str,
        run_id: str | None = None,
        checksum_sha256: str | None = None,
        media_type: str = "",
        size_bytes: int | None = None,
        metadata: dict[str, Any] | None = None,
        created_by: str = "",
        org_id: str | None = None,
    ) -> dict[str, Any] | None:
        """산출물 메타데이터 추가 — URI는 서비스가 검증한 값만 저장한다.
        서버는 URI를 fetch하지 않는다."""
        name = name.strip()[:200]
        uri = uri.strip()[:1000]
        if not name or not uri:
            return None
        k = (kind or "").strip().lower()[:40]
        if k not in _ARTIFACT_KINDS:
            k = "other"
        # metadata는 TEXT 컬럼 — 손상 JSON이 되지 않게 크기 상한 내에서
        # 직렬화하고, 넘치면 _truncated 래퍼로 대체한다(agent_events와 동일 패턴).
        meta_json = json.dumps(metadata or {}, ensure_ascii=False, default=str)
        if len(meta_json) > 8000:
            meta_json = json.dumps(
                {"_truncated": True, "chars": len(meta_json), "preview": meta_json[:7900]},
                ensure_ascii=False,
            )
        aid = uuid.uuid4().hex[:12]
        now = _iso(_now())
        # org_id는 부모(task → run) org를 전파 — 스케줄러·내부 경로(ctx='')에서도
        # 부모가 속한 조직으로 기록된다(FR-013). 없으면 명시/요청 컨텍스트.
        oid = ""
        trow = self._conn.execute("SELECT org_id FROM tasks WHERE id=?", (task_id,)).fetchone()
        if trow is not None:
            oid = str(trow["org_id"] or "")
        rid = (run_id or "").strip()[:64]
        if not oid and rid:
            rrow = self._conn.execute("SELECT org_id FROM runs WHERE id=?", (rid,)).fetchone()
            if rrow is not None:
                oid = str(rrow["org_id"] or "")
        oid = oid or orgctx.org_for_write(org_id)
        with self.tx() as c:
            c.execute(
                "INSERT INTO task_artifacts(id,task_id,run_id,kind,name,uri,"
                "checksum_sha256,media_type,size_bytes,metadata,created_by,org_id,"
                "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    aid,
                    task_id,
                    rid or None,
                    k,
                    name,
                    uri,
                    (checksum_sha256 or "").strip().lower()[:64] or None,
                    (media_type or "").strip()[:100],
                    int(size_bytes) if size_bytes is not None else None,
                    meta_json,
                    created_by[:200],
                    oid,
                    now,
                ),
            )
        return self.get_task_artifact(aid)

    def get_task_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM task_artifacts WHERE id=? AND (org_id=? OR ?='')",
            (artifact_id, *_org_bind()),
        ).fetchone()
        return self._artifact_json(row) if row else None

    def list_task_artifacts(self, task_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        """산출물 목록 — 등록 순(created_at ASC, id ASC) 고정."""
        rows = self._conn.execute(
            "SELECT * FROM task_artifacts WHERE task_id=? AND (org_id=? OR ?='')"
            " ORDER BY created_at, id LIMIT ?",
            (task_id, *_org_bind(), _clamp_limit(limit, 100, 500)),
        ).fetchall()
        return [self._artifact_json(r) for r in rows]
