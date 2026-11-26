"""Eval 서비스 — 픽스처/체크 검증 + 결정적 채점 엔진.

Paperclip pillar-3 포인트: 저장된 입력 + 결정적 체크를 일반 run이
끝난 뒤 채점한다. LLM judge는 v1에서 제외 — results 항목이
{"check_type","check","passed","detail"} 형태라 judge 타입을 나중에
추가할 수 있다(check_type은 라벨용 타입 문자열, check는 원본 체크
객체 — 프론트는 check_type으로 표시하고 check를 툴팁에 직렬화한다).

채점은 lazy — run이 terminal 상태가 되면 GET eval-runs 조회 시점에
점수를 매긴다(멱등: status='pending'일 때만). 실패한 run도 채점한다 —
체크가 대부분 실패해 낮은 점수가 되는 것이 올바른 신호다.
"""

from __future__ import annotations

import json
import logging
import math
import re
from typing import Any

from .models import RunStatus, StageStatus
from .pricing import cost_usd
from .store.db import Store

logger = logging.getLogger("jobmate.evals")

EVAL_STATUSES = ("active", "archived")
EVAL_RUN_STATUSES = ("pending", "scored", "error")
CHECK_TYPES = (
    "stage_completed",
    "output_contains",
    "output_not_contains",
    "output_regex",
    "json_path",
    "max_cost_usd",
)
NAME_MAX = 200
# name 중복은 409, 나머지 검증 실패는 422 — 라우터가 이 상수로 구분한다.
ERR_NAME_CONFLICT = "eval name already exists"

# 입력 크기 상한 — ReDoS/대형 페이로드 방지. RunCreateRequest.task(20000)와
# 동일 기준 + eval 전체 직렬화 크기 캡.
TASK_MAX = 20_000
FIELD_MAX = 1_000  # check 문자열 필드(stage/value/pattern/path/contains) 상한
EQUALS_MAX = 10_000  # json_path equals 비문자열 값의 직렬화 크기 상한
EVAL_PAYLOAD_MAX = 200 * 1024  # input+checks 직렬화 합계 상한(바이트)

# 채점 대상 run 종결 상태 — wakeups._TERMINAL_STATUSES와 동일 집합.
# waiting_approval/waiting_review는 사람 승인 대기라 비종결.
TERMINAL_RUN_STATUSES = frozenset(
    {
        RunStatus.COMPLETED.value,
        RunStatus.FAILED.value,
        RunStatus.REJECTED.value,
        RunStatus.CANCELLED.value,
        RunStatus.BUDGET_EXHAUSTED.value,
        RunStatus.NEEDS_HUMAN.value,
    }
)

_PATH_SEG_RE = re.compile(r"^([^\[\]]*)((?:\[\d+\])*)$")
_PATH_INDEX_RE = re.compile(r"\[(\d+)\]")
# ReDoS 휴리스틱 — `(x+)+`/`(a*)*`처럼 반복 그룹 안에 +/* 가중 정량자가
# 중첩되고 그룹 자체도 정량된 패턴을 거부한다. 완전한 판별은 불가하므로
# 길이 캡(FIELD_MAX)과 병행하는 best-effort 방어다.
_NESTED_QUANT_RE = re.compile(r"\([^()]*[+*][^()]*\)[+*]")


# ---------------- 검증 ----------------


def validate_eval_input(input_obj: Any) -> str | None:
    """eval.input 픽스처 검증 — 'task' 비어있지 않은 문자열 필수."""
    if not isinstance(input_obj, dict):
        return "input must be an object"
    task = input_obj.get("task")
    if not isinstance(task, str) or not task.strip():
        return "input.task must be a non-empty string"
    if len(task) > TASK_MAX:
        return f"input.task must be at most {TASK_MAX} chars"
    return None


def validate_checks(checks: Any) -> str | None:
    """checks 배열 검증 — 각 항목의 type/필수 필드/값 형태를 검사.

    빈 배열은 허용(채점 시 score=None, status='scored')."""
    if not isinstance(checks, list):
        return "checks must be an array"
    if len(checks) > 100:
        return "checks must have at most 100 items"
    for i, chk in enumerate(checks):
        err = _validate_check(chk, i)
        if err is not None:
            return err
    return None


def _validate_check(chk: Any, idx: int) -> str | None:
    if not isinstance(chk, dict):
        return f"checks[{idx}] must be an object"
    ctype = str(chk.get("type") or "")
    if ctype not in CHECK_TYPES:
        return f"checks[{idx}].type must be one of {list(CHECK_TYPES)}"
    if ctype == "stage_completed":
        stage = chk.get("stage")
        if not isinstance(stage, str) or not stage.strip():
            return f"checks[{idx}].stage must be a non-empty string"
        if len(stage) > FIELD_MAX:
            return f"checks[{idx}].stage must be at most {FIELD_MAX} chars"
        status = chk.get("status")
        if status is not None:
            st = str(status).strip().lower()
            if st not in {s.value for s in StageStatus}:
                return f"checks[{idx}].status must be one of {[s.value for s in StageStatus]}"
    elif ctype in ("output_contains", "output_not_contains"):
        value = chk.get("value")
        # 빈 문자열은 모든 출력에 trivially 통과/실패하므로 거부한다.
        if not isinstance(value, str) or not value.strip():
            return f"checks[{idx}].value must be a non-empty string"
        if len(value) > FIELD_MAX:
            return f"checks[{idx}].value must be at most {FIELD_MAX} chars"
    elif ctype == "output_regex":
        pattern = chk.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            return f"checks[{idx}].pattern must be a non-empty string"
        if len(pattern) > FIELD_MAX:
            return f"checks[{idx}].pattern must be at most {FIELD_MAX} chars"
        if _NESTED_QUANT_RE.search(pattern):
            return f"checks[{idx}].pattern looks like a nested-quantifier ReDoS risk (e.g. '(a+)+')"
        try:
            re.compile(pattern)
        except re.error as e:
            return f"checks[{idx}].pattern is not a valid regex: {e}"
    elif ctype == "json_path":
        path = chk.get("path")
        if not isinstance(path, str) or not path.strip():
            return f"checks[{idx}].path must be a non-empty string"
        if len(path) > FIELD_MAX:
            return f"checks[{idx}].path must be at most {FIELD_MAX} chars"
        if "equals" not in chk and "contains" not in chk:
            return f"checks[{idx}] needs 'equals' or 'contains'"
        if "contains" in chk:
            contains = chk["contains"]
            if not isinstance(contains, str) or not contains.strip():
                return f"checks[{idx}].contains must be a non-empty string"
            if len(contains) > FIELD_MAX:
                return f"checks[{idx}].contains must be at most {FIELD_MAX} chars"
        if "equals" in chk:
            equals = chk["equals"]
            if isinstance(equals, str):
                if not equals.strip():
                    return f"checks[{idx}].equals must be a non-empty string"
                if len(equals) > FIELD_MAX:
                    return f"checks[{idx}].equals must be at most {FIELD_MAX} chars"
            else:
                try:
                    eq_len = len(json.dumps(equals, ensure_ascii=False, default=str))
                except (TypeError, ValueError):
                    return f"checks[{idx}].equals is not JSON-serializable"
                if eq_len > EQUALS_MAX:
                    return f"checks[{idx}].equals must serialize to at most {EQUALS_MAX} chars"
    elif ctype == "max_cost_usd":
        value = chk.get("value")
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(float(value))
            or float(value) < 0
        ):
            return f"checks[{idx}].value must be a non-negative number"
    return None


def validate_eval_fields(
    store: Store,
    fields: dict[str, Any],
    *,
    eval_id: str | None = None,
) -> tuple[str | None, int]:
    """create/patch 공용 필드 검증 — (에러 메시지, http_status) 또는 (None, 200).

    name 중복은 409 conflict, 나머지는 422 validation."""
    from .workflows import WORKFLOWS  # 지연 import — 순환 방지

    if "name" in fields:
        name = str(fields["name"] or "").strip()
        if not name:
            return "name must not be empty", 422
        if len(name) > NAME_MAX:
            return f"name must be at most {NAME_MAX} chars", 422
        other = store.find_eval_by_name(name)
        if other is not None and other["id"] != eval_id:
            return ERR_NAME_CONFLICT, 409
    if "workflow_key" in fields:
        wk = str(fields["workflow_key"] or "").strip()
        if wk not in WORKFLOWS:
            return f"unknown workflow '{wk}'", 422
    if "status" in fields:
        st = str(fields["status"] or "").strip().lower()
        if st not in EVAL_STATUSES:
            return f"status must be one of {list(EVAL_STATUSES)}", 422
    if "input" in fields:
        err = validate_eval_input(fields["input"])
        if err is not None:
            return err, 422
    if "checks" in fields:
        err = validate_checks(fields["checks"])
        if err is not None:
            return err, 422
    # 전체 페이로드 크기 캡 — 개별 필드 캡 우회(대형 중첩 객체 등)를 막는다.
    payload_len = 0
    for key in ("input", "checks"):
        if key in fields:
            try:
                payload_len += len(
                    json.dumps(fields[key], ensure_ascii=False, default=str).encode("utf-8")
                )
            except (TypeError, ValueError):
                return f"{key} is not JSON-serializable", 422
    if payload_len > EVAL_PAYLOAD_MAX:
        return f"input+checks must serialize to at most {EVAL_PAYLOAD_MAX} bytes", 422
    return None, 200


def eval_out(ev: dict[str, Any], latest: dict[str, Any] | None = None) -> dict[str, Any]:
    """store 행 → API 계약 EvalOut.

    last_run_at은 가장 최근 eval_run의 created_at, last_score는 score가
    null이 아닌 가장 최근 eval_run의 score — 실행 직후(pending)여도 이전
    점수가 "점수 없음"으로 덮이지 않는다.

    latest는 호출자가 latest_eval_runs() 결과에서 꺼내 넘긴다 — 목록에서
    eval마다 재조회(N+1)하지 않기 위함."""
    latest = latest or {}
    return {
        "id": ev.get("id"),
        "name": ev.get("name") or "",
        "workflow_key": ev.get("workflow_key") or "",
        "input": ev.get("input") or {},
        "checks": ev.get("checks") or [],
        "status": ev.get("status") or "active",
        "created_by": ev.get("created_by") or "",
        "created_at": ev.get("created_at"),
        "updated_at": ev.get("updated_at"),
        # latest는 {"created_at": 최신 run 시각, "score": 최신 scored run 점수}
        # 형태 — pending/score-null run이 마지막이어도 마지막 '점수'를 유지한다.
        "last_score": (latest or {}).get("score"),
        "last_run_at": (latest or {}).get("created_at"),
    }


def eval_run_out(er: dict[str, Any]) -> dict[str, Any]:
    """eval_run 행 → API 계약 EvalRunOut."""
    return {
        "id": er.get("id"),
        "eval_id": er.get("eval_id"),
        "run_id": er.get("run_id"),
        "status": er.get("status") or "pending",
        "score": er.get("score"),
        "results": er.get("results") or [],
        "error": er.get("error") or "",
        "created_at": er.get("created_at"),
        "scored_at": er.get("scored_at"),
    }


# ---------------- 채점 ----------------


def run_output_doc(store: Store, run_id: str) -> dict[str, Any]:
    """run의 stage 출력 문서 — {stage_key: 최신 attempt의 output}.

    output_contains/output_regex/json_path 체크가 공유하는 단일 뷰.
    stage별 마지막 attempt만 취한다(재시도 이전 실패 출력은 무시)."""
    doc: dict[str, Any] = {}
    for sr in store.get_stage_runs(run_id):
        doc[sr.stage_key] = sr.output if isinstance(sr.output, dict) else {"value": sr.output}
    return doc


def _render_value(value: Any, path: str, lines: list[str]) -> None:
    """output 노드를 `path: value` 텍스트 라인으로 렌더링 (runner와 동일 규칙)."""
    if isinstance(value, dict):
        if not value:
            lines.append(f"{path}: {{}}")
            return
        for k, v in value.items():
            _render_value(v, f"{path}.{k}" if path else str(k), lines)
    elif isinstance(value, list):
        if not value:
            lines.append(f"{path}: []")
            return
        for i, item in enumerate(value):
            _render_value(item, f"{path}[{i}]", lines)
    else:
        lines.append(f"{path}: {value}")


def run_output_text(store: Store, run_id: str) -> str:
    """output_contains/regex가 검색하는 최종 출력 텍스트.

    stage_key를 루트로 한 렌더링 + run.error(있으면) 1행."""
    doc = run_output_doc(store, run_id)
    lines: list[str] = []
    for stage_key in sorted(doc):
        _render_value(doc[stage_key], stage_key, lines)
    run = store.get_run(run_id)
    if run is not None and run.error:
        lines.append(f"run.error: {run.error}")
    return "\n".join(lines)


def _resolve_json_path(obj: Any, path: str) -> tuple[bool, Any]:
    """`a.b[0].c` 형태 단순 경로 해석 — jsonpath 라이브러리 없이 구현.

    각 세그먼트는 `이름` + `[n]` 인덱스 나열. dict 키와 list 인덱스만 지원.
    관용적으로 선행 `$`/`$.`(JSONPath 표기)를 벗겨내고 해석한다 —
    `$.a.b`와 `a.b`는 동일하다."""
    p = str(path or "").strip()
    if p.startswith("$."):
        p = p[2:]
    elif p.startswith("$"):
        p = p[1:]
    cur = obj
    for seg in p.split("."):
        seg = seg.strip()
        if not seg:
            return False, None
        m = _PATH_SEG_RE.match(seg)
        if m is None:
            return False, None
        name, index_part = m.group(1), m.group(2)
        if name:
            if not isinstance(cur, dict) or name not in cur:
                return False, None
            cur = cur[name]
        for idx_s in _PATH_INDEX_RE.findall(index_part):
            idx = int(idx_s)
            if not isinstance(cur, (list, tuple)) or idx >= len(cur):
                return False, None
            cur = cur[idx]
    return True, cur


def _run_cost_usd(store: Store, run_id: str) -> float:
    """run 총 비용 — stage_runs 토큰×모델 단가 (budget spend와 동일 기준)."""
    total = 0.0
    for sr in store.get_stage_runs(run_id):
        total += cost_usd(sr.model, sr.tokens_in, sr.tokens_out)
    return round(total, 6)


def _latest_stage_status(store: Store, run_id: str, stage_key: str) -> str | None:
    """stage_key의 최신 attempt 상태 — 재시도 시 마지막 시도가 기준."""
    latest: str | None = None
    for sr in store.get_stage_runs(run_id):
        if sr.stage_key == stage_key:
            latest = sr.status.value
    return latest


def _eval_json_value(raw: Any) -> Any:
    """stage output이 문자열 JSON이면 파싱 — json_path가 str output도 다루게."""
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return raw
    return raw


def _check_stage_completed(store: Store, run_id: str, chk: dict[str, Any]) -> tuple[bool, str]:
    stage = str(chk.get("stage") or "")
    expected = str(chk.get("status") or StageStatus.SUCCEEDED.value).strip().lower()
    actual = _latest_stage_status(store, run_id, stage)
    if actual is None:
        return False, f"stage '{stage}' not found in run"
    ok = actual == expected
    return ok, f"stage '{stage}' status={actual} expected={expected}"


def _check_output_contains(
    store: Store, run_id: str, chk: dict[str, Any], *, negate: bool
) -> tuple[bool, str]:
    value = str(chk.get("value") or "")
    if not value.strip():
        # 검증 우회 저장분 — 빈 문자열은 항상 매칭되므로 실패 처리.
        return False, "empty substring value"
    text = run_output_text(store, run_id)
    found = value in text
    ok = (not found) if negate else found
    detail = (
        f"substring {'absent' if negate else 'present'}"
        if ok
        else f"substring {'unexpectedly present' if negate else 'not found'}"
    )
    return ok, f"{detail}: {value[:80]!r}"


def _check_output_regex(store: Store, run_id: str, chk: dict[str, Any]) -> tuple[bool, str]:
    pattern = str(chk.get("pattern") or "")
    text = run_output_text(store, run_id)
    try:
        m = re.search(pattern, text)
    except re.error as e:
        return False, f"invalid regex: {e}"
    return (m is not None), (f"pattern {'matched' if m else 'did not match'}: {pattern[:80]!r}")


def _check_json_path(store: Store, run_id: str, chk: dict[str, Any]) -> tuple[bool, str]:
    path = str(chk.get("path") or "")
    doc = run_output_doc(store, run_id)
    found, value = _resolve_json_path(doc, path)
    if not found:
        return False, f"path not found: {path}"
    value = _eval_json_value(value)
    if "equals" in chk:
        ok = value == chk["equals"]
        return ok, f"path {path} equals check {'passed' if ok else 'failed'}"
    contains = str(chk.get("contains") or "")
    if not contains.strip():
        return False, "empty contains value"
    hay = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    ok = contains in hay
    return ok, f"path {path} contains check {'passed' if ok else 'failed'}"


def _check_max_cost(store: Store, run_id: str, chk: dict[str, Any]) -> tuple[bool, str]:
    limit = float(chk.get("value") or 0)
    total = _run_cost_usd(store, run_id)
    ok = total <= limit
    return ok, f"cost ${total:.6f} vs limit ${limit:.6f}"


def score_eval_run(
    store: Store, eval_row: dict[str, Any], run_id: str
) -> tuple[float | None, list[dict[str, Any]], str]:
    """eval 픽스처 + run → (score, results, error).

    결정적 — LLM 호출 없음. 개별 체크 예외는 해당 체크 실패로 기록하고
    채점 자체는 계속한다. checks가 비어 있으면 score=None."""
    checks = eval_row.get("checks") or []
    results: list[dict[str, Any]] = []
    passed = 0
    for chk in checks:
        if not isinstance(chk, dict):
            results.append(
                {
                    "check_type": "",
                    "check": chk,
                    "passed": False,
                    "detail": "invalid check",
                }
            )
            continue
        ctype = str(chk.get("type") or "")
        try:
            if ctype == "stage_completed":
                ok, detail = _check_stage_completed(store, run_id, chk)
            elif ctype in ("output_contains", "output_not_contains"):
                ok, detail = _check_output_contains(
                    store, run_id, chk, negate=(ctype == "output_not_contains")
                )
            elif ctype == "output_regex":
                ok, detail = _check_output_regex(store, run_id, chk)
            elif ctype == "json_path":
                ok, detail = _check_json_path(store, run_id, chk)
            elif ctype == "max_cost_usd":
                ok, detail = _check_max_cost(store, run_id, chk)
            else:
                ok, detail = False, f"unknown check type: {ctype}"
        except Exception as e:  # 개별 체크 크래시가 전체 채점을 죽이지 않게
            logger.warning("eval check %s failed run=%s: %s", ctype, run_id, e)
            ok, detail = False, f"check error: {str(e)[:200]}"
        if ok:
            passed += 1
        # 계약: check_type은 표시용 타입 문자열, check는 원본 체크 객체
        # (params 보존 — 프론트가 JSON.stringify로 툴팁 표시).
        results.append({"check_type": ctype, "check": chk, "passed": ok, "detail": detail})
    if not checks:
        return None, results, ""
    score = round(passed / len(checks) * 100, 1)
    return score, results, ""


def maybe_score_eval_run(store: Store, eval_run: dict[str, Any]) -> dict[str, Any]:
    """lazy 채점 — pending + run terminal이면 점수를 매기고 행을 갱신한다.

    멱등: 이미 scored/error거나 run이 비종결이면 그대로 반환.
    반환은 갱신 후 행(변경 없으면 입력 행)."""
    if str(eval_run.get("status") or "") != "pending":
        return eval_run
    run_id = str(eval_run.get("run_id") or "")
    run = store.get_run(run_id) if run_id else None
    if run is None:
        store.mark_eval_run_error(str(eval_run.get("id") or ""), "run not found")
        return store.get_eval_run(str(eval_run.get("id") or "")) or eval_run
    if run.status.value not in TERMINAL_RUN_STATUSES:
        return eval_run
    ev = store.get_eval(str(eval_run.get("eval_id") or ""))
    if ev is None:
        store.mark_eval_run_error(str(eval_run.get("id") or ""), "eval not found")
        return store.get_eval_run(str(eval_run.get("id") or "")) or eval_run
    try:
        score, results, err = score_eval_run(store, ev, run_id)
    except Exception as e:
        logger.warning("eval_run %s scoring failed: %s", eval_run.get("id"), e)
        store.mark_eval_run_error(str(eval_run.get("id") or ""), str(e)[:500])
        return store.get_eval_run(str(eval_run.get("id") or "")) or eval_run
    store.mark_eval_run_scored(
        str(eval_run.get("id") or ""), score=score, results=results, error=err
    )
    return store.get_eval_run(str(eval_run.get("id") or "")) or eval_run
