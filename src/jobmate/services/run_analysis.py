"""실행 분석 서비스 — spec 006 US4 (FR-017~FR-020).

- compare_runs: 두 run의 단계별 status/입출력 요약/diff 파일/소요 시간 비교 뷰.
- run_insight: run 단위 인사이트 요약 — run_comments를 입력에 포함하고
  포함 여부를 meta에 표시한다(FR-020).
- retry_plan: retry-from-stage 사전 검증(스테이지 존재·run 상태).

store는 읽기(get_run/get_stage_runs/list_run_comments)만 사용하고,
상태 전이 자체는 store.reset_run_for_retry가 원자적으로 수행한다.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING, Any

from ..models import Run, RunStatus, StageRun, StageStatus
from ..workflows.resolver import resolve_workflow_spec

if TYPE_CHECKING:
    from ..store.db import Store

# retry 허용 종결/정지 상태 — running/queued/waiting_* 는 진행 중이라 409.
RETRYABLE_STATUSES = frozenset(
    {
        RunStatus.COMPLETED.value,
        RunStatus.FAILED.value,
        RunStatus.REJECTED.value,
        RunStatus.BUDGET_EXHAUSTED.value,
        RunStatus.NEEDS_HUMAN.value,
        RunStatus.CANCELLED.value,
    }
)

_SUMMARY_KEYS = ("summary", "decision", "rationale", "done_state", "passed", "status")


def _iso_now() -> str:
    return datetime.now().isoformat()


def _dt(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _duration_ms(started: Any, finished: Any) -> int | None:
    s, f = _dt(started), _dt(finished)
    if s is None or f is None:
        return None
    return max(0, int((f - s).total_seconds() * 1000))


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _jsonish(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def _output_summary(output: dict[str, Any] | None, limit: int = 500) -> str:
    """stage output에서 사람이 읽는 요약 — 대표 키 우선, 없으면 JSON 앞부분."""
    if not output:
        return ""
    parts: list[str] = []
    for k in _SUMMARY_KEYS:
        v = output.get(k)
        if v is not None and v != "":
            parts.append(f"{k}={_jsonish(v)}")
    if parts:
        return _truncate("; ".join(parts), limit)
    return _truncate(_jsonish(output), limit)


def _diff_files(output: dict[str, Any] | None) -> list[dict[str, Any]]:
    """implement 단계 output.diff(unified diff) → 파일 목록.

    파서는 api.jobs의 _parse_diff_files 재사용 — diff 포맷 해석을 한 곳에
    두기 위함(레이어 역참조를 피하려고 함수 내부에서 지연 import)."""
    diff = str((output or {}).get("diff") or "")
    if not diff:
        return []
    try:
        from ..api.jobs import _parse_diff_files
    except Exception:  # pragma: no cover - 방어
        return []
    files = _parse_diff_files(diff)
    return [
        {
            "path": f.get("path") or "",
            "status": f.get("status") or "modified",
            "additions": int(f.get("additions") or 0),
            "deletions": int(f.get("deletions") or 0),
        }
        for f in files
    ]


def _latest_stage_map(stage_runs: list[StageRun]) -> dict[str, StageRun]:
    """stage_key → 최신 attempt StageRun (get_stage_runs는 started_at,attempt 순)."""
    latest: dict[str, StageRun] = {}
    for sr in stage_runs:
        latest[sr.stage_key] = sr
    return latest


def _stage_view(sr: StageRun | None) -> dict[str, Any] | None:
    if sr is None:
        return None
    out = sr.output if isinstance(sr.output, dict) else {}
    started = sr.started_at.isoformat() if sr.started_at else None
    finished = sr.finished_at.isoformat() if sr.finished_at else None
    return {
        "stage_run_id": sr.id,
        "status": sr.status.value,
        "attempt": sr.attempt,
        "model": sr.model,
        "prompt_version": sr.prompt_version,
        "tokens_in": sr.tokens_in,
        "tokens_out": sr.tokens_out,
        "started_at": started,
        "finished_at": finished,
        "duration_ms": _duration_ms(sr.started_at, sr.finished_at),
        "error": _truncate(sr.error or "", 300),
        "input_summary": _truncate(_jsonish(sr.input), 400),
        "output_summary": _output_summary(out),
        "diff_files": _diff_files(out),
    }


def _run_block(run: Run, stage_runs: list[StageRun]) -> dict[str, Any]:
    latest = _latest_stage_map(stage_runs)
    succeeded = sorted(k for k, s in latest.items() if s.status == StageStatus.SUCCEEDED)
    failed = sorted(k for k, s in latest.items() if s.status == StageStatus.FAILED)
    return {
        "id": run.id,
        "workflow": run.workflow_key,
        "workflow_version": run.workflow_version,
        "status": run.status.value,
        "project": run.project,
        "goal_id": run.goal_id,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "updated_at": run.updated_at.isoformat() if run.updated_at else None,
        "duration_ms": _duration_ms(run.created_at, run.updated_at),
        "error": _truncate(run.error or "", 300),
        "stage_attempts": {k: sum(1 for s in stage_runs if s.stage_key == k) for k in latest},
        "succeeded_stages": succeeded,
        "failed_stages": failed,
    }


def _stage_order(store: Store, *workflow_keys: str) -> list[str]:
    """두 run workflow spec의 stage 순서를 합친 비교용 정렬 키 목록."""
    order: list[str] = []
    seen: set[str] = set()
    for wk in workflow_keys:
        spec = resolve_workflow_spec(store, wk)
        if spec is None:
            continue
        for s in spec.stages:
            if s.key not in seen:
                seen.add(s.key)
                order.append(s.key)
    return order


def compare_runs(store: Store, run_a_id: str, run_b_id: str) -> dict[str, Any] | None:
    """FR-017 — 두 run의 단계별 비교 뷰. 어느 한쪽이라도 없으면 None(404).

    org 스코핑은 store.get_run의 _org_bind에 위임 — 요청 조직 컨텍스트에서
    보이지 않는 run은 None으로 수렴한다."""
    run_a = store.get_run(run_a_id)
    run_b = store.get_run(run_b_id)
    if run_a is None or run_b is None:
        return None
    stages_a = store.get_stage_runs(run_a.id)
    stages_b = store.get_stage_runs(run_b.id)
    latest_a = _latest_stage_map(stages_a)
    latest_b = _latest_stage_map(stages_b)

    keys = _stage_order(store, run_a.workflow_key, run_b.workflow_key)
    for k in sorted(set(latest_a) | set(latest_b)):
        if k not in keys:
            keys.append(k)

    rows: list[dict[str, Any]] = []
    for key in keys:
        va = _stage_view(latest_a.get(key))
        vb = _stage_view(latest_b.get(key))
        sa = va["status"] if va else None
        sb = vb["status"] if vb else None
        da = va["duration_ms"] if va else None
        db_ = vb["duration_ms"] if vb else None
        rows.append(
            {
                "stage_key": key,
                "a": va,
                "b": vb,
                "status_changed": sa != sb,
                "duration_delta_ms": (db_ - da) if da is not None and db_ is not None else None,
            }
        )

    return {
        "run_a": {**_run_block(run_a, stages_a), "insight": run_insight(store, run_a)},
        "run_b": {**_run_block(run_b, stages_b), "insight": run_insight(store, run_b)},
        "stages": rows,
        "generated_at": _iso_now(),
    }


def run_insight(store: Store, run: Run) -> dict[str, Any]:
    """FR-020 — run 인사이트 요약. run_comments를 입력에 포함한다.

    코멘트가 있으면 summary에 반영 사실을 명시하고 meta.comments_included를
    true로 둔다 — 코멘트 유무로 결과가 구분된다(US4 시나리오 4)."""
    stage_runs = store.get_stage_runs(run.id)
    latest = _latest_stage_map(stage_runs)
    stage_outcomes = {k: s.status.value for k, s in sorted(latest.items())}

    comments = store.list_run_comments(run.id)
    comment_items = [
        {
            "id": c.get("id"),
            "email": c.get("email") or "",
            "body": _truncate(str(c.get("body") or ""), 300),
            "created_at": c.get("created_at"),
        }
        for c in comments
    ]

    status = run.status.value
    if status == RunStatus.COMPLETED.value:
        classification = "success"
    elif status in (RunStatus.FAILED.value, RunStatus.BUDGET_EXHAUSTED.value):
        classification = "failure"
    elif status in (RunStatus.WAITING_APPROVAL.value, RunStatus.WAITING_REVIEW.value):
        classification = "awaiting_approval"
    elif status in (RunStatus.QUEUED.value, RunStatus.RUNNING.value):
        classification = "in_progress"
    else:
        classification = status  # rejected/cancelled/needs_human 등

    failed = [k for k, s in latest.items() if s.status == StageStatus.FAILED]
    n_succeeded = sum(1 for s in latest.values() if s.status == StageStatus.SUCCEEDED)
    summary = (
        f"run {run.id} ({run.workflow_key}) status={status} "
        f"classification={classification}; "
        f"stages={len(latest)} succeeded={n_succeeded} failed={len(failed)}"
        + (f" first_failure={failed[0]}" if failed else "")
    )
    if comment_items:
        # 코멘트가 요약 입력에 실제로 반영됐음을 명시 — meta와 함께 판별 가능.
        summary += f"; comments={len(comment_items)} reflected in insight"

    return {
        "run_id": run.id,
        "classification": classification,
        "summary": summary,
        "stage_outcomes": stage_outcomes,
        "comments": comment_items,
        "meta": {
            "generated_by": "run_analysis.v1",
            "comments_included": bool(comment_items),
            "comment_count": len(comment_items),
        },
    }


def retry_plan(store: Store, run: Run, from_stage: str) -> dict[str, Any]:
    """FR-018 사전 검증 — 재실행 가능 여부와 spec stage 순서를 반환.

    에러 시 {"error": "unknown_stage"|"invalid_state"|"unknown_workflow"|
    "validation", "message": ...}를 반환하고, 성공 시
    {"from_stage", "stage_keys", "from_index", "spec"}를 반환한다."""
    key = (from_stage or "").strip()
    if not key:
        return {"error": "validation", "message": "from_stage is required"}
    spec = resolve_workflow_spec(store, run.workflow_key)
    if spec is None:
        return {"error": "unknown_workflow", "message": run.workflow_key}
    stage_keys = [s.key for s in spec.stages]
    if key not in stage_keys:
        return {
            "error": "validation",
            "message": f"unknown stage '{key}' for workflow '{run.workflow_key}'",
            "stage_keys": stage_keys,
        }
    if run.status.value not in RETRYABLE_STATUSES:
        return {
            "error": "invalid_state",
            "message": f"run is {run.status.value}, retry requires a finished run",
        }
    return {
        "from_stage": key,
        "stage_keys": stage_keys,
        "from_index": stage_keys.index(key),
        "spec": spec,
    }
