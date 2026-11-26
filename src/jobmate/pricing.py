"""LLM 토큰 가격표 — USD/1M 토큰 + microusd 정수 단가.

앱 계층(app.py)의 세션 비용 추정, 비용 대시보드(usage/summary, reports),
스코프드 예산(budget_scope), 비용 원장(cost_events, paperclip spec 2차)이
공유한다. 실 청구액이 아닌 추정치 — 가격 변경 시 이 표만 수정하고
PRICING_VERSION을 올린다.

Global Constraint (paperclip spec): 금액 계산에 float를 쓰지 않는다.
비용은 정수 microusd(1 USD = 1,000,000 microusd)로만 계산·저장한다.
`cost_usd`는 레거시 표시/집계 호환용 파생값이며, 모든 신규 경로는
`cost_microusd`를 사용한다.
"""

from __future__ import annotations

# 단가표 버전 — cost_events.pricing_version에 기록되는 짧은 식별자.
# 단가를 바꾸면 반드시 올린다(과거 이벤트의 재해석 방지).
PRICING_VERSION = "rate-card-2026-11"

# (input $/1M, output $/1M) — app.py _SESSION_TOKEN_PRICES와 동일 값
MODEL_TOKEN_PRICES: dict[str, tuple[float, float]] = {
    "default": (3.0, 15.0),
    "claude": (3.0, 15.0),
    "codex": (1.5, 6.0),
    "gemini": (1.0, 4.0),
}

# (input microusd/1M tokens, output microusd/1M tokens) — MODEL_TOKEN_PRICES의
# 정수 변환($1 = 1,000,000 microusd). 1M 토큰당 $3 → 3,000,000 microusd.
# 토큰당 microusd = microusd_per_1M / 1,000,000 이므로 cost_microusd는
# (tokens * rate) // 1_000_000 의 순수 정수 계산이다.
MODEL_TOKEN_PRICES_MICROUSD: dict[str, tuple[int, int]] = {
    "default": (3_000_000, 15_000_000),
    "claude": (3_000_000, 15_000_000),
    "codex": (1_500_000, 6_000_000),
    "gemini": (1_000_000, 4_000_000),
}

# 1 USD = 1,000,000 microusd (spec Global Constraint)
MICROUSD_PER_USD = 1_000_000


def price_for_model(model: str) -> tuple[float, float]:
    """모델 라벨(llm_usage의 'provider/model' 또는 tool명) → (in,out) 단가.

    라벨에 포함된 패밀리 키워드로 매칭하고, 없으면 default 가격을 쓴다."""
    m = (model or "").lower()
    for key in ("claude", "codex", "gemini"):
        if key in m:
            return MODEL_TOKEN_PRICES[key]
    return MODEL_TOKEN_PRICES["default"]


def price_for_model_microusd(model: str) -> tuple[int, int]:
    """모델 라벨 → (input, output) microusd/1M 토큰 정수 단가.

    price_for_model과 동일한 키워드 매칭 규칙 — 두 표는 항상 함께 갱신한다."""
    m = (model or "").lower()
    for key in ("claude", "codex", "gemini"):
        if key in m:
            return MODEL_TOKEN_PRICES_MICROUSD[key]
    return MODEL_TOKEN_PRICES_MICROUSD["default"]


def cost_microusd(model: str, tokens_in: int, tokens_out: int, cached_input_tokens: int = 0) -> int:
    """토큰 수 → microusd 정수 비용 (floor division).

    - 순수 정수 연산만 사용 — float 금액 계산 금지(spec Global Constraint).
    - 단가표는 $/1M 토큰 단위라 tokens*rate의 곱은 최대 ~1e13으로
      Python int/SQLite INTEGER 범위 안이다.
    - cached_input_tokens는 관측용 부분집합으로 기록되며, rate card에
      캐시 할인 단가가 없으므로 input_tokens 요금에 포함해 계산한다
      (보수적 상한 — 실제 청구보다 과대 추정될 수 있음).
    - 알 수 없는 모델 라벨은 default 단가를 적용한다 — '미가격'이 아니라
      'rate card 추정'이므로 호출부는 cost_status='estimated'로 기록한다.
      정말 가격을 모르는 경우(모델 자체가 없는 외부 stage 등)는 호출부가
      cost_microusd를 호출하지 말고 unpriced로 기록해야 한다.
    """
    price_in, price_out = price_for_model_microusd(model)
    return (
        max(0, int(tokens_in or 0)) * price_in + max(0, int(tokens_out or 0)) * price_out
    ) // MICROUSD_PER_USD


def cost_usd(model: str, tokens_in: int, tokens_out: int) -> float:
    """토큰 수 → USD 추정 비용 (소수 6자리) — 레거시 표시용 파생값.

    microusd 정수 계산을 1e6으로 나눈 값 — 신규 경로는 cost_microusd를 쓴다."""
    return cost_microusd(model, tokens_in, tokens_out) / MICROUSD_PER_USD
