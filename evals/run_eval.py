"""보안 리뷰 단계 평가 실행기 — dataset.jsonl 기반 Verdict 품질 측정.

대상 단계: dev_review 워크플로의 `security` 에이전트
(guide/prompts/security_review.md + models.Verdict 스키마).

지표:
1. decision_match: 기대 decision과 실제 decision의 일치 여부 (0/1)
2. must_contain_score: 기대 findings 키워드가 실제 출력에 포함된 비율 (0~1)
3. severity_ok: 기대 최소 severity 이상의 finding이 있는지 (0/1, 기대 없으면 1)
4. judge_score: LLM-as-judge 또는 fake 휴리스틱의 0~1 품질 점수

사용:
  python -m evals.run_eval --fake                 # API 키 없이 스모크 실행
  python -m evals.run_eval                        # 실제 LLM (기본 모델: Settings)
  python -m evals.run_eval --model gemini-3.5-flash-lite
  python -m evals.run_eval --baseline evals/report_xxx.json
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from jobmate.engine.schemas import SCHEMAS  # noqa: E402
from jobmate.guide.loader import load_prompt  # noqa: E402
from jobmate.llm.client import LLMClient, OpenAICompatLLM, extract_json  # noqa: E402
from jobmate.models import ModelConfig, Verdict  # noqa: E402

logger = logging.getLogger("jobmate.evals")

DATASET_PATH = Path(__file__).resolve().parent / "dataset.jsonl"
STAGE_PROMPT = "security_review"
STAGE_SCHEMA = "Verdict"
ALLOWED_DECISIONS = {"approve", "request_changes", "needs_human"}
SEVERITY_RANK = {"blocker": 0, "major": 1, "minor": 2, "nit": 3}

JUDGE_SYSTEM = "당신은 LLM 보안 리뷰 출력의 품질 평가자다. JSON만 출력한다."

JUDGE_PROMPT = """다음은 보안 리뷰 에이전트의 평가 사례다.

[입력 시나리오]
{user_input}

[기대 판정]
{expected}

[실제 출력]
{actual}

[평가 기준]
{rubric}

실제 출력을 0.0~1.0으로 채점하라.
- 1.0: 기대 decision과 findings가 기준을 충족
- 0.5: decision은 맞지만 findings가 부족하거나 과도
- 0.0: decision이 틀리거나 무관한 출력

JSON으로만 답하라: {{"score": 0.8, "reason": "한 줄 이유"}}"""


def load_dataset(path: str | Path = DATASET_PATH) -> list[dict[str, Any]]:
    """JSONL 데이터셋을 읽고 필수 필드를 검증한다."""
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for lineno, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        item = json.loads(line)
        for field in ("id", "input", "expected", "rubric"):
            if field not in item:
                raise ValueError(f"{path}:{lineno} — 필수 필드 누락: {field}")
        if item["id"] in seen:
            raise ValueError(f"{path}:{lineno} — 중복 id: {item['id']}")
        seen.add(item["id"])
        if not isinstance(item["input"], str) or not item["input"].strip():
            raise ValueError(f"{path}:{lineno} — input은 비어있지 않은 문자열이어야 한다")
        expected = item["expected"]
        if not isinstance(expected, dict) or "decision" not in expected:
            raise ValueError(f"{path}:{lineno} — expected.decision이 필요하다")
        if expected["decision"] not in ALLOWED_DECISIONS:
            raise ValueError(f"{path}:{lineno} — 허용되지 않은 decision: {expected['decision']}")
        items.append(item)
    return items


# ---------- fake 모드 ----------
# 실제 LLM 없이도 하네스를 검증할 수 있도록, 입력 텍스트의 알려진 취약 패턴을
# 키워드로 탐지해 결정적 Verdict를 만든다. 스모크 테스트용이며 정확도는 낮다.

_FAKE_RULES: list[tuple[tuple[str, ...], str, str, str]] = [
    (
        ("os.system", "shell=True", 'subprocess.call(f"', 'subprocess.run(f"'),
        "blocker",
        "command_injection",
        "사용자 입력이 셸 명령에 그대로 삽입된다.",
    ),
    (
        (
            'f"select',
            "f'select",
            "% name%",
            "+ name +",
            "format(name)",
            "cursor.execute(f",
            "db.execute(f",
            "conn.execute(f",
            "select * from orders",
            "select * from sales",
            "where id='",
        ),
        "blocker",
        "sql_injection",
        "SQL 쿼리가 문자열 결합/포맷팅으로 생성된다.",
    ),
    (
        (
            "sk-",
            "sk_live",
            "sk_test",
            'api_key = "',
            'password = "',
            "-----begin private key-----",
            "id_rsa",
        ),
        "blocker",
        "secret_exposure",
        "비밀키/토큰이 코드나 저장소에 하드코딩된다.",
    ),
    (
        ("pickle.loads", "yaml.load(", "marshal.loads", "eval(", "exec("),
        "major",
        "unsafe_deserialization",
        "신뢰할 수 없는 입력의 역직렬화/동적 실행.",
    ),
    (
        ("innerhtml", "dangerouslysetinnerhtml", "document.write("),
        "major",
        "xss",
        "사용자 입력이 이스케이프 없이 HTML에 삽입된다.",
    ),
    (
        ('/data/" +', "os.path.join(base, user", "../", "send_file(", "open(user"),
        "major",
        "path_traversal",
        "사용자 제어 경로가 검증 없이 파일 시스템에 접근한다.",
    ),
    (
        (
            "requests.get(user",
            "requests.get(url",
            "fetch(user",
            "urllib.request.urlopen(user",
        ),
        "major",
        "ssrf",
        "사용자 제어 URL로 서버 측 요청이 발생한다.",
    ),
    (
        ("redirect(request", "redirect(next", "return redirect("),
        "minor",
        "open_redirect",
        "리다이렉트 대상이 검증 없이 사용자 입력을 따른다.",
    ),
]


def _heuristic_verdict(user: str) -> dict[str, Any]:
    """입력 텍스트의 취약 키워드를 탐지해 Verdict 형태 dict를 반환한다."""
    # 중첩 JSON 안의 diff는 \n 이스케이프 형태로 들어오므로 실제 줄바꿈으로
    # 변환한 뒤, 백슬래시(이스케이프된 따옴표 등)를 제거해 매칭한다.
    # 제거된(-) diff 라인은 취약 패턴 스캔에서 제외한다.
    text = user.replace("\\n", "\n").replace("\\t", " ")
    lowered = text.lower().replace("\\", "")
    # 제거된(-) diff 라인 제외: 줄 시작의 `-` 또는 `"diff": "-..."` 경계.
    lowered = re.sub(r"(?m)^-[^\n]*", "", lowered)
    lowered = re.sub(r'(: ")-[^\n]*', r"\1", lowered)
    scan = lowered
    # allowlist로 검증된 동적 식별자는 SQLi로 오탐하지 않는다.
    sql_allowed = " in allowed" in scan or "allowlist" in scan
    findings: list[dict[str, Any]] = []
    for keywords, severity, category, summary in _FAKE_RULES:
        if category == "sql_injection" and sql_allowed:
            continue
        matched = [kw for kw in keywords if kw.lower() in scan]
        if not matched:
            continue
        findings.append(
            {
                "severity": severity,
                "confidence": "high",
                "path": "src/app.py",
                "line": 0,
                "category": category,
                "summary": f"{summary} (탐지: {', '.join(matched[:3])})",
                "fix": "입력을 검증하고 파라미터화/이스케이프하라.",
            }
        )
    if findings:
        return {
            "decision": "request_changes",
            "findings": findings[:5],
            "summary": f"fake 휴리스틱: 취약 패턴 {len(findings)}건 탐지",
            "attack_surface": "fake",
            "trust_boundary": "fake",
            "evidence": "keyword heuristic",
        }
    return {
        "decision": "approve",
        "findings": [],
        "summary": "fake 휴리스틱: 탐지된 취약 패턴 없음",
        "attack_surface": "fake",
        "trust_boundary": "fake",
        "evidence": "keyword heuristic",
    }


class FakeVerdictLLM:
    """결정적 fake LLM — security_review 프롬프트에는 휴리스틱 Verdict를 반환한다."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def complete(
        self,
        *,
        model: ModelConfig,
        system: str,
        user: str,
        schema: type[Verdict] | None = None,
    ) -> tuple[Any, dict[str, int]]:
        self.calls.append({"model": model.model, "user_len": len(user)})
        if "security" in system or "보안 검토" in system:
            out: Any = _heuristic_verdict(user)
        else:
            out = {"score": 0.5, "reason": "fake judge"}
        if schema is not None:
            out = schema.model_validate(out)
        return out, {"tokens_in": 120, "tokens_out": 60}


# ---------- 채점 ----------


def _haystack(output: dict[str, Any]) -> str:
    """findings/summary 등 출력 전체를 소문자 문자열로 합친다."""
    parts = [str(output.get("summary", "")), str(output.get("attack_surface", ""))]
    parts.append(json.dumps(output.get("findings", []), ensure_ascii=False))
    return " ".join(parts).lower()


def _must_contain_score(expected: dict[str, Any], output: dict[str, Any]) -> float:
    needles = expected.get("must_contain") or []
    if not needles:
        return 1.0
    hay = _haystack(output)
    hits = sum(1 for n in needles if str(n).lower() in hay)
    return round(hits / len(needles), 3)


def _severity_ok(expected: dict[str, Any], output: dict[str, Any]) -> float:
    want = expected.get("min_severity")
    if not want:
        return 1.0
    limit = SEVERITY_RANK.get(str(want), 3)
    for f in output.get("findings", []):
        if isinstance(f, dict) and SEVERITY_RANK.get(str(f.get("severity", "nit")), 3) <= limit:
            return 1.0
    return 0.0


def _heuristic_judge(
    decision_match: float, must_score: float, severity: float
) -> tuple[float, str]:
    """fake 모드용 결정적 judge — 구조 점수의 가중 평균."""
    score = round(0.5 * decision_match + 0.3 * must_score + 0.2 * severity, 3)
    return score, "fake heuristic judge"


def _llm_judge(
    llm: LLMClient,
    model: ModelConfig,
    item: dict[str, Any],
    output: dict[str, Any],
) -> tuple[float, str]:
    """실제 LLM-as-judge 호출. 실패 시 (0.0, 오류메모)."""
    prompt = JUDGE_PROMPT.format(
        user_input=item["input"][:6000],
        expected=json.dumps(item["expected"], ensure_ascii=False)[:2000],
        actual=json.dumps(output, ensure_ascii=False)[:6000],
        rubric=item["rubric"],
    )
    try:
        raw, _usage = llm.complete(model=model, system=JUDGE_SYSTEM, user=prompt, schema=None)
        data = raw if isinstance(raw, dict) else extract_json(str(raw))
        return float(data.get("score", 0.0)), str(data.get("reason", ""))[:200]
    except Exception as exc:  # noqa: BLE001 — judge 실패가 평가 전체를 죽이면 안 된다
        logger.warning("judge 실패 id=%s: %s", item.get("id"), exc)
        return 0.0, f"judge error: {exc}"


def _to_output_dict(raw: Any) -> dict[str, Any]:
    """LLM 출력을 dict로 정규화한다."""
    if isinstance(raw, Verdict):
        return raw.model_dump()
    if hasattr(raw, "model_dump"):
        return raw.model_dump()
    if isinstance(raw, dict):
        return raw
    return {"result": str(raw)}


# ---------- 실행 ----------


def run_eval(
    dataset_path: str | Path = DATASET_PATH,
    *,
    model: ModelConfig | None = None,
    fake: bool = False,
    limit: int | None = None,
    use_judge: bool = True,
) -> dict[str, Any]:
    """데이터셋을 실행해 평가 리포트를 반환한다."""
    items = load_dataset(dataset_path)
    if limit:
        items = items[:limit]

    if fake:
        llm: LLMClient = FakeVerdictLLM()
        judge_llm: LLMClient | None = None
    else:
        llm = OpenAICompatLLM()
        judge_llm = llm if use_judge else None
    if model is None and not fake:
        from jobmate.config import get_settings

        model = get_settings().model_for("security", "default")
    if model is None:
        model = ModelConfig(model="fake", base_url="http://localhost")

    system_prompt, prompt_version = load_prompt(STAGE_PROMPT)
    schema = SCHEMAS[STAGE_SCHEMA]

    results: list[dict[str, Any]] = []
    tokens_in = tokens_out = 0
    t0 = time.time()
    for i, item in enumerate(items):
        qid = str(item["id"])
        try:
            raw, usage = llm.complete(
                model=model, system=system_prompt, user=item["input"], schema=schema
            )
            output = _to_output_dict(raw)
            tokens_in += usage.get("tokens_in", 0)
            tokens_out += usage.get("tokens_out", 0)

            expected_decision = item["expected"]["decision"]
            actual_decision = str(output.get("decision", ""))
            dm = 1.0 if actual_decision == expected_decision else 0.0
            ms = _must_contain_score(item["expected"], output)
            sv = _severity_ok(item["expected"], output)

            if judge_llm is not None:
                js, reason = _llm_judge(judge_llm, model, item, output)
            else:
                js, reason = _heuristic_judge(dm, ms, sv)

            score = round((dm + ms + js) / 3, 3)
            results.append(
                {
                    "id": qid,
                    "expected_decision": expected_decision,
                    "actual_decision": actual_decision,
                    "decision_match": dm,
                    "must_contain_score": ms,
                    "severity_ok": sv,
                    "judge_score": js,
                    "judge_reason": reason,
                    "score": score,
                    "summary": str(output.get("summary", ""))[:300],
                }
            )
            logger.info(
                "[%d/%d] %s decision=%s/%s score=%.2f",
                i + 1,
                len(items),
                qid,
                actual_decision,
                expected_decision,
                score,
            )
        except Exception as exc:  # noqa: BLE001 — 항목 실패가 전체 평가를 중단시키지 않게 한다
            results.append(
                {
                    "id": qid,
                    "expected_decision": item["expected"]["decision"],
                    "actual_decision": "",
                    "decision_match": 0.0,
                    "must_contain_score": 0.0,
                    "severity_ok": 0.0,
                    "judge_score": 0.0,
                    "judge_reason": "",
                    "score": 0.0,
                    "error": str(exc)[:300],
                }
            )
            logger.error("[%d/%d] %s 실패: %s", i + 1, len(items), qid, exc)

    n = max(len(results), 1)
    return {
        "dataset": str(dataset_path),
        "stage": STAGE_PROMPT,
        "schema": STAGE_SCHEMA,
        "prompt_version": prompt_version,
        "model": model.model,
        "fake": fake,
        "items": len(results),
        "avg_decision_accuracy": round(sum(r["decision_match"] for r in results) / n, 3),
        "avg_score": round(sum(r["score"] for r in results) / n, 3),
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "elapsed_sec": round(time.time() - t0, 1),
        "results": results,
    }


def compare_reports(baseline: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """이전 리포트와 현재 리포트를 비교해 회귀/개선을 판정한다."""

    def _by_id(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {str(r["id"]): r for r in report.get("results", [])}

    b, c = _by_id(baseline), _by_id(current)
    diffs = []
    for qid in sorted(set(b) & set(c)):
        delta = float(c[qid].get("score", 0)) - float(b[qid].get("score", 0))
        if abs(delta) > 0.01:
            diffs.append(
                {
                    "id": qid,
                    "baseline": b[qid].get("score"),
                    "current": c[qid].get("score"),
                    "delta": round(delta, 3),
                }
            )
    acc_delta = float(current.get("avg_decision_accuracy", 0)) - float(
        baseline.get("avg_decision_accuracy", 0)
    )
    score_delta = float(current.get("avg_score", 0)) - float(baseline.get("avg_score", 0))
    return {
        "avg_decision_accuracy_delta": round(acc_delta, 3),
        "avg_score_delta": round(score_delta, 3),
        "changed_items": diffs,
        "verdict": "improved" if score_delta > 0 else "regressed" if score_delta < 0 else "same",
    }


def _main() -> None:
    parser = argparse.ArgumentParser(description="jobMate 보안 리뷰 단계 평가 실행기")
    parser.add_argument("--dataset", default=str(DATASET_PATH), help="JSONL 데이터셋 경로")
    parser.add_argument(
        "--model", default=None, help="모델명 오버라이드 (예: gemini-3.5-flash-lite)"
    )
    parser.add_argument(
        "--fake",
        "--dry-run",
        dest="fake",
        action="store_true",
        help="API 키 없이 결정적 fake LLM으로 실행",
    )
    parser.add_argument("--limit", type=int, default=None, help="처음 N개만 평가")
    parser.add_argument("--no-judge", action="store_true", help="LLM-as-judge 채점 생략")
    parser.add_argument("--baseline", help="비교할 이전 리포트 JSON")
    parser.add_argument("--output", help="결과 저장 경로 (JSON)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    model: ModelConfig | None = None
    if args.model:
        model = ModelConfig(model=args.model)

    report = run_eval(
        args.dataset,
        model=model,
        fake=args.fake,
        limit=args.limit,
        use_judge=not args.no_judge,
    )

    print("\n=== 평가 결과 ===")
    print(f"단계:            {report['stage']} (prompt v{report['prompt_version']})")
    print(f"모델:            {report['model']}  fake={report['fake']}")
    print(f"항목 수:         {report['items']}")
    print(f"decision 정확도: {report['avg_decision_accuracy']:.1%}")
    print(f"평균 점수:       {report['avg_score']:.3f}")
    print(f"토큰(in/out):    {report['tokens_in']}/{report['tokens_out']}")
    print(f"소요 시간:       {report['elapsed_sec']}s")

    if args.baseline:
        baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
        comp = compare_reports(baseline, report)
        print("\n=== baseline 대비 ===")
        print(f"decision 정확도: {comp['avg_decision_accuracy_delta']:+.3f}")
        print(f"점수 변화:       {comp['avg_score_delta']:+.3f}  →  {comp['verdict']}")
        for d in comp["changed_items"][:10]:
            print(f"  {d['id']}: {d['baseline']} → {d['current']} ({d['delta']:+.2f})")

    out = args.output or str(
        Path(__file__).resolve().parent / f"report_{time.strftime('%Y%m%d_%H%M%S')}.json"
    )
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n리포트 저장: {out}")


if __name__ == "__main__":
    _main()
