# evals — LLM 단계 평가 하네스

dev_review 워크플로의 **보안 리뷰(security) 단계** 출력을 정량 평가한다.
대상 계약은 실제 프로덕션 경로와 동일하다:

- 시스템 프롬프트: `src/jobmate/guide/prompts/security_review.md` (`guide.loader.load_prompt`)
- 출력 스키마: `src/jobmate/models/outputs.py`의 `Verdict`
  (`engine/schemas.py`의 `SCHEMAS["Verdict"]`)
- LLM 호출: `src/jobmate/llm/client.py`의 `OpenAICompatLLM.complete()`

## 평가 항목 (`dataset.jsonl`)

각 줄은 하나의 평가 케이스:

```json
{"id": "sec-sqli-01",
 "input": "# Task\n...\n\n# Output of implement\n{...diff...}",
 "expected": {"decision": "request_changes", "must_contain": ["sql"], "min_severity": "major"},
 "rubric": "기대 판정과 findings에 대한 서술 기준"}
```

- `input`: `engine/runner.py`의 `_build_user_prompt`가 만드는 형태와 유사한
  사용자 프롬프트(task + 이전 단계 출력, diff 포함).
- `expected.decision`: `approve | request_changes | needs_human`.
- `expected.must_contain`: findings/summary에 포함돼야 할 키워드(소문자 부분일치, 선택).
- `expected.min_severity`: 최소 한 개의 finding이 이 severity 이상이어야 함(선택).
- `rubric`: LLM-as-judge 채점 기준(자연어).

## 실행

```bash
# API 키 없이 스모크 실행 (결정적 fake LLM, judge는 휴리스틱)
python -m evals.run_eval --fake

# 실제 LLM — 기본 모델은 Settings(security/default 티어). GEMINI_API_KEY 등 필요
python -m evals.run_eval
python -m evals.run_eval --model gemini-3.5-flash-lite --limit 10

# 이전 리포트와 비교
python -m evals.run_eval --baseline evals/report_20260101_120000.json
```

리포트는 `evals/report_<timestamp>.json`에 저장된다(항목별 점수 + 토큰 사용량 +
평균 decision 정확도/점수).

## 점수 구성

항목별 `score` = 평균(`decision_match`, `must_contain_score`, `judge_score`):

- `decision_match`: 기대 decision과 실제 decision 일치 여부 (0/1)
- `must_contain_score`: 기대 키워드 포함 비율 (0~1)
- `severity_ok`: 기대 최소 severity 충족 여부 (0/1)
- `judge_score`: rubric 기반 LLM-as-judge 0~1 (`--fake`에서는 결정적 휴리스틱)

## 새 항목 추가

`dataset.jsonl`에 한 줄 추가하면 된다. diff는 실제 코드 형태로 작성하고,
`expected.decision`과 `rubric`을 명확히 적는다. 긍정(approve)/부정
(request_changes)/판단 보류(needs_human) 사례를 균형 있게 유지한다.

## baseline 비교

프롬프트(`guide/prompts/*.md`)나 모델 변경 전후로 각각 실행해 리포트를 남긴 뒤:

```bash
python -m evals.run_eval --baseline evals/report_<이전>.json
```

항목별 점수 변화(`changed_items`)와 평균 델타, `improved/regressed/same` 판정을
출력한다. 평균 점수가 낮아지면 프롬프트 회귀를 의심한다.

## 테스트

`tests/eval/test_eval_harness.py`는 fake 모드만 사용하므로 API 키 없이 실행된다:

```bash
uv run pytest tests/eval -q
```
