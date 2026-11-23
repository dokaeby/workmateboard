"""LLM 토큰 가격표 — USD/1M 토큰. llm_usage 모델 라벨/세션 tool 기준 추정 비용.

앱 계층(app.py)의 세션 비용 추정과 비용 대시보드(usage/summary, reports)가
공유한다. 실 청구액이 아닌 추정치 — 가격 변경 시 이 표만 수정한다.
"""

from __future__ import annotations

# (input $/1M, output $/1M) — app.py _SESSION_TOKEN_PRICES와 동일 값
MODEL_TOKEN_PRICES: dict[str, tuple[float, float]] = {
    "default": (3.0, 15.0),
    "claude": (3.0, 15.0),
    "codex": (1.5, 6.0),
    "gemini": (1.0, 4.0),
}


def price_for_model(model: str) -> tuple[float, float]:
    """모델 라벨(llm_usage의 'provider/model' 또는 tool명) → (in,out) 단가.

    라벨에 포함된 패밀리 키워드로 매칭하고, 없으면 default 가격을 쓴다."""
    m = (model or "").lower()
    for key in ("claude", "codex", "gemini"):
        if key in m:
            return MODEL_TOKEN_PRICES[key]
    return MODEL_TOKEN_PRICES["default"]


def cost_usd(model: str, tokens_in: int, tokens_out: int) -> float:
    """토큰 수 → USD 추정 비용 (소수 6자리)."""
    price_in, price_out = price_for_model(model)
    return round(((tokens_in or 0) * price_in + (tokens_out or 0) * price_out) / 1_000_000, 6)
