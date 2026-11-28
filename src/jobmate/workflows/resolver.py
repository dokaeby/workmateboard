"""워크플로 spec 해석 — 내장 전역 레지스트리 + 조직 스코프 커스텀 조회.

spec 003 US3: 커스텀 워크플로 spec의 정본은 custom_workflows 테이블이며
조회 시점의 조직 컨텍스트(ctx)로 필터한다. 과거에는 _sync_workflow_registry가
테이블 내용을 전역 WORKFLOWS dict에 섞어 넣어, (name, org_id) PK를 공유하는
두 조직의 동명 spec이 서로를 덮어쓰고(hijack) 한 조직의 동기화가 다른 조직
항목을 evict했다. 이제 WORKFLOWS는 내장 spec만 담고 커스텀은 여기서
조직 컨텍스트 기준으로 해석한다 — 조직 간 간섭이 구조적으로 불가능하다.
"""

from __future__ import annotations

import logging
from typing import Any

from ..models import WorkflowSpec
from ..store import orgctx
from . import WORKFLOWS

logger = logging.getLogger("jobmate.workflows")


def _first_org_id(store: Any) -> str:
    """ctx='' 경로의 결정적 조직 — 첫 활성 조직(api/jobs._default_org_id와 동일 규칙)."""
    try:
        orgs = store.list_organizations()
    except Exception:
        return ""
    return str(orgs[0]["id"]) if orgs else ""


def resolve_workflow_spec(store: Any, key: str) -> WorkflowSpec | None:
    """workflow key → WorkflowSpec. 내장 우선, 없으면 커스텀 테이블 조회.

    - 내장 spec은 전역 WORKFLOWS에서 즉시 반환(조직 무관).
    - 커스텀은 현재 조직 컨텍스트로 custom_workflows를 조회한다. ctx=''인
      비인증/스케줄러 경로는 호출자가 행의 org_id로 org_scope를 재적용하는
      것이 원칙이며(jobs._execute_run_job 등), 컨텍스트가 정말 비어 있으면
      첫 활성 조직으로 좁혀 결정적으로 해석한다 — 무필터 조회로 임의 조직의
      동명 spec을 집어 오는 것을 막는다.
    - spec.key != name이거나 spec 파손 행은 미해석(None). 저장 경로
      (builder API/restore)가 key==name을 강제하므로 불일치는 비정상 데이터다.
    """
    builtin = WORKFLOWS.get(key)
    if builtin is not None:
        return builtin
    ctx_oid = orgctx.current_org_id()
    oid = ctx_oid or _first_org_id(store)
    row: dict[str, Any] | None = None
    try:
        with orgctx.org_scope(oid):
            row = store.get_custom_workflow(key)
        if row is None and not ctx_oid and oid:
            # ctx='' + 기본 조직에 없음 — 조직 도입 이전/무스코프 쓰기로
            # 남은 레거시 행을 위해 무필터 1회 재조회(단일 조직 배포 호환).
            row = store.get_custom_workflow(key)
    except Exception:
        logger.warning("custom workflow lookup failed key=%s", key, exc_info=True)
        return None
    if row is None:
        return None
    try:
        spec = WorkflowSpec.model_validate(row.get("spec"))
    except Exception as e:
        logger.warning("custom workflow %s spec invalid: %s", key, str(e)[:120])
        return None
    if spec.key != key:
        logger.warning("custom workflow %s spec.key mismatch (%s) — ignored", key, spec.key)
        return None
    return spec
