"""eval 하네스 테스트 — fake 모드만 사용 (API 키 불필요)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from evals.run_eval import (  # noqa: E402
    DATASET_PATH,
    compare_reports,
    load_dataset,
    run_eval,
)


def test_dataset_loads_and_validates() -> None:
    """데이터셋이 로드되고 id 고유성/필수 필드/decision 값이 유효하다."""
    items = load_dataset(DATASET_PATH)
    assert 20 <= len(items) <= 24
    ids = [i["id"] for i in items]
    assert len(ids) == len(set(ids))
    for item in items:
        assert item["input"].strip()
        assert item["rubric"].strip()
        assert item["expected"]["decision"] in {
            "approve",
            "request_changes",
            "needs_human",
        }


def test_dataset_rejects_duplicate_ids(tmp_path: Path) -> None:
    """중복 id가 있으면 load_dataset이 ValueError를 던진다."""
    bad = tmp_path / "bad.jsonl"
    line = json.dumps(
        {
            "id": "dup",
            "input": "x",
            "expected": {"decision": "approve"},
            "rubric": "r",
        }
    )
    bad.write_text(f"{line}\n{line}\n", encoding="utf-8")
    try:
        load_dataset(bad)
    except ValueError as e:
        assert "중복 id" in str(e)
    else:  # pragma: no cover
        raise AssertionError("중복 id를 허용하면 안 된다")


def test_run_eval_fake_produces_report(tmp_path: Path) -> None:
    """fake 모드로 전체 데이터셋을 실행해 리포트 구조를 검증한다."""
    report = run_eval(DATASET_PATH, fake=True, limit=5)
    assert report["fake"] is True
    assert report["items"] == 5
    assert report["stage"] == "security_review"
    assert 0.0 <= report["avg_score"] <= 1.0
    assert len(report["results"]) == 5
    for r in report["results"]:
        for key in (
            "id",
            "expected_decision",
            "actual_decision",
            "decision_match",
            "must_contain_score",
            "severity_ok",
            "judge_score",
            "score",
        ):
            assert key in r

    out = tmp_path / "report.json"
    out.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    reloaded = json.loads(out.read_text(encoding="utf-8"))
    assert reloaded["items"] == 5


def test_fake_mode_flags_sqli() -> None:
    """fake 휴리스틱이 명백한 SQLi 입력을 request_changes로 판정한다."""
    report = run_eval(DATASET_PATH, fake=True, limit=1)
    r = report["results"][0]
    assert r["id"] == "sec-sqli-01"
    assert r["actual_decision"] == "request_changes"
    assert r["decision_match"] == 1.0


def _synthetic_report(scores: dict[str, float]) -> dict[str, Any]:
    """비교 테스트용 최소 리포트 생성."""
    results = [
        {
            "id": qid,
            "expected_decision": "approve",
            "actual_decision": "approve",
            "decision_match": 1.0,
            "must_contain_score": 1.0,
            "severity_ok": 1.0,
            "judge_score": s,
            "score": s,
        }
        for qid, s in scores.items()
    ]
    n = max(len(results), 1)
    return {
        "items": len(results),
        "avg_decision_accuracy": 1.0,
        "avg_score": round(sum(scores.values()) / n, 3),
        "results": results,
    }


def test_compare_reports_detects_improvement() -> None:
    """현재 리포트 점수가 높으면 improved로 판정한다."""
    baseline = _synthetic_report({"a": 0.5, "b": 0.6})
    current = _synthetic_report({"a": 0.9, "b": 0.7})
    comp = compare_reports(baseline, current)
    assert comp["verdict"] == "improved"
    assert comp["avg_score_delta"] > 0
    changed_ids = {d["id"] for d in comp["changed_items"]}
    assert "a" in changed_ids
    assert "b" in changed_ids


def test_compare_reports_detects_regression() -> None:
    """현재 리포트 점수가 낮으면 regressed로 판정한다."""
    baseline = _synthetic_report({"a": 0.9, "b": 0.8})
    current = _synthetic_report({"a": 0.4, "b": 0.8})
    comp = compare_reports(baseline, current)
    assert comp["verdict"] == "regressed"
    assert comp["avg_score_delta"] < 0
    assert {d["id"] for d in comp["changed_items"]} == {"a"}


def test_compare_reports_same() -> None:
    """동일 점수면 same으로 판정한다."""
    report = _synthetic_report({"a": 0.7})
    comp = compare_reports(report, report)
    assert comp["verdict"] == "same"
    assert comp["changed_items"] == []
