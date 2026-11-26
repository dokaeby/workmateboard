"""판정 센서 — 결정적 강등/검증. LLM 출력을 신뢰하지 않는다."""

from __future__ import annotations

from ..models import Severity, Verdict

BLOCKING = {Severity.BLOCKER.value, Severity.MAJOR.value}


def has_blocking_findings(verdict: Verdict) -> bool:
    return any(f.severity in BLOCKING and f.confidence != "very_low" for f in verdict.findings)


def downgrade_approve(verdict: Verdict) -> Verdict:
    """approve인데 blocker/major( confidence low 이상) finding이 있으면 request_changes로 강등."""
    if verdict.decision == "approve" and has_blocking_findings(verdict):
        return verdict.model_copy(
            update={
                "decision": "request_changes",
                "summary": (
                    f"[sensor] approve 강등: blocker/major finding이 있어 "
                    f"request_changes로 변경. {verdict.summary}"
                ),
            }
        )
    return verdict


def filter_findings(verdict: Verdict) -> Verdict:
    """confidence 게이트: very_low 제외, low는 기록만."""
    kept = [f for f in verdict.findings if f.confidence != "very_low"]
    if len(kept) != len(verdict.findings):
        return verdict.model_copy(update={"findings": kept})
    return verdict


def normalize_decision(verdict: Verdict, allowed: set[str]) -> Verdict | None:
    """단계별 허용 판정 외 값이면 None (호출자가 실패 처리)."""
    if verdict.decision not in allowed:
        return None
    return verdict
