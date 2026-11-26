# mypy: disable-error-code="attr-defined"
"""Task 협업 서비스 — spec 3차 B (comments/artifacts) 검증.

store는 행 조회·삽입만 담당하고, 어휘·URI·길이·참조 무결성 규칙은 여기서
수행한다(routers는 얇게). URI는 메타데이터로만 저장하고 서버가 fetch하지
않는다.
"""

from __future__ import annotations

import json
import posixpath
import re
from typing import Any
from urllib.parse import urlparse

from ..models import TaskArtifactKind, TaskCommentContentType
from ..store.db import Store

COMMENT_BODY_MAX = 4000
METADATA_MAX_BYTES = 8000
NAME_MAX = 200
URI_MAX = 1000

_COMMENT_CONTENT_TYPES = {c.value for c in TaskCommentContentType}
_ARTIFACT_KINDS = {k.value for k in TaskArtifactKind}
_CHECKSUM_RE = re.compile(r"^[0-9a-fA-F]{64}$")
# 제어문자(C0/C1 + DEL) — repo-relative 경로와 https URL 공통 금지 문자.
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def validate_comment_fields(fields: dict[str, Any]) -> str | None:
    """댓글 create 검증 — 에러 메시지 또는 None."""
    body = str(fields.get("body") or "").strip()
    if not body:
        return "body must not be empty"
    if len(body) > COMMENT_BODY_MAX:
        return f"body must be at most {COMMENT_BODY_MAX} chars"
    ct = fields.get("content_type")
    if ct is not None and str(ct).strip().lower() not in _COMMENT_CONTENT_TYPES:
        return f"content_type must be one of {sorted(_COMMENT_CONTENT_TYPES)}"
    return None


def is_allowed_artifact_uri(uri: str) -> bool:
    """artifact URI 허용 판정 — spec 3차 B 보안 규칙.

    허용:
    - https:// URL (http://, 다른 스킴은 거부)
    - repo-relative 경로 — 스킴 없음, 선행 '/' 없음, '..' 없음,
      백슬래시/제어문자 없음, posixpath.normpath 결과가 루트 아래에 머묾

    거부: file://, javascript:, data:, UNC(//host), 절대 경로, 상위
    디렉터리 탈출, 공백만 있는 값."""
    u = (uri or "").strip()
    if not u or len(u) > URI_MAX or _CTRL_RE.search(u):
        return False
    # 명시적 스킴/프로토콜 상대 판정 — '//' 시작은 UNC/네트워크 경로로 거부.
    if u.startswith("//"):
        return False
    scheme = urlparse(u).scheme
    if scheme:
        # https만 허용 — http/file/javascript/data/ftp 등 모두 거부.
        # urlparse는 'C:\\x'를 scheme='c'로 해석하므로 백슬래시도 여기서 걸린다.
        if scheme.lower() != "https":
            return False
        p = urlparse(u)
        return bool(p.netloc) and not p.username and not p.password
    # 스킴 없는 값 = repo-relative 경로 후보.
    if u.startswith("/") or u.startswith("~"):
        return False  # 절대 경로/홈 경로 거부
    if "\\" in u:
        return False  # Windows 경로/UNC 변형 거부
    # 세그먼트 단위로 '..' 탈출과 빈 세그먼트(연속 슬래시) 검사.
    norm = posixpath.normpath(u)
    if norm in (".", "") or norm.startswith("../") or norm == "..":
        return False
    parts = [seg for seg in u.split("/") if seg not in ("", ".")]
    if any(seg == ".." for seg in parts):
        return False
    # 정규화 결과가 원래 경로를 벗어나지 않아야 한다(normpath가 상위를
    # 만들지 않는다는 것을 위 검사로 보장하지만 방어적으로 재확인).
    if norm.startswith("/") or norm.startswith(".."):
        return False
    return True


def validate_artifact_fields(
    store: Store,
    task_id: str,
    fields: dict[str, Any],
) -> str | None:
    """artifact create 검증 — 에러 메시지 또는 None.

    kind/name/uri 필수, uri는 is_allowed_artifact_uri, checksum은 64hex,
    size_bytes는 0 이상 정수, metadata는 JSON object + 크기 상한,
    run_id는 존재하고 이 task에 연결된(task_runs) run이어야 한다."""
    kind = str(fields.get("kind") or "other").strip().lower()
    if kind not in _ARTIFACT_KINDS:
        return f"kind must be one of {sorted(_ARTIFACT_KINDS)}"
    name = str(fields.get("name") or "").strip()
    if not name:
        return "name must not be empty"
    if len(name) > NAME_MAX:
        return f"name must be at most {NAME_MAX} chars"
    uri = str(fields.get("uri") or "").strip()
    if not is_allowed_artifact_uri(uri):
        return "uri must be an https URL or a safe repo-relative path"
    checksum = fields.get("checksum_sha256")
    if checksum is not None and str(checksum).strip():
        if not _CHECKSUM_RE.match(str(checksum).strip()):
            return "checksum_sha256 must be 64 hex characters"
    size = fields.get("size_bytes")
    if size is not None:
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            return "size_bytes must be a non-negative integer"
    meta = fields.get("metadata")
    if meta is not None:
        if not isinstance(meta, dict):
            return "metadata must be a JSON object"
        try:
            blob = json.dumps(meta, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return "metadata must be JSON-serializable"
        if len(blob.encode("utf-8")) > METADATA_MAX_BYTES:
            return f"metadata must be at most {METADATA_MAX_BYTES} bytes"
    run_id = fields.get("run_id")
    if run_id is not None and str(run_id).strip():
        rid = str(run_id).strip()
        if store.get_run(rid) is None:
            return "run_id: run not found"
        linked = {str(t.get("run_id") or "") for t in store.task_runs_for_task(task_id)}
        if rid not in linked:
            return "run_id: run is not linked to this task"
    return None


def task_runs_view(store: Store, task_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
    """task의 run 이력 — task_runs 링크 + run 요약 (spec F task detail).

    최신 링크 순으로 반환하고 run이 삭제된 고아 링크는 건너뛴다."""
    links = list(reversed(store.task_runs_for_task(task_id)))
    out: list[dict[str, Any]] = []
    for link in links[:limit]:
        run = store.get_run(str(link.get("run_id") or ""))
        if run is None:
            continue
        out.append(
            {
                "run_id": run.id,
                "attempt": int(link.get("attempt") or 0),
                "linked_at": link.get("created_at"),
                "workflow": run.workflow_key,
                "status": run.status.value,
                "goal_id": run.goal_id,
                "project": run.project,
                "created_at": run.created_at.isoformat(),
                "finished_at": run.updated_at.isoformat(),
            }
        )
    return out
