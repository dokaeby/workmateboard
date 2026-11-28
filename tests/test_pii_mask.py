"""spec 005 US4 — PII 마스킹 표준화(mask_pii) + 저장 경계 적용 테스트."""

from __future__ import annotations

import json

import pytest

from jobmate.config import Settings, get_settings
from jobmate.services.redaction import mask_pii
from jobmate.store.db import Store

ON = {"pii_mask": "on"}
OFF = {"pii_mask": "off"}


# ---------------- mask_pii 순수 함수 ----------------


def test_mask_pii_off_returns_original():
    text = "문의 a@b.com / 010-1234-5678"
    assert mask_pii(text, OFF) == text
    assert mask_pii(text, {}) == text
    assert mask_pii(text, None) == text  # get_settings 기본 off


def test_mask_pii_email():
    assert mask_pii("연락처 admin@example.com 주세요", ON) == "연락처 a***@example.com 주세요"
    assert mask_pii("x@y.co.kr", ON) == "x***@y.co.kr"


def test_mask_pii_phones():
    assert mask_pii("010-1234-5678", ON) == "010-****-5678"
    assert mask_pii("01012345678", ON) == "010-****-5678"
    assert mask_pii("02-123-4567", ON) == "02-****-4567"
    assert mask_pii("031-123-4567", ON) == "031-****-4567"
    assert mask_pii("064-987-6543", ON) == "064-****-6543"


def test_mask_pii_rrn_and_card():
    assert mask_pii("주민 900101-1234567", ON) == "주민 ******-*******"
    assert mask_pii("9001011234567", ON) == "******-*******"
    assert mask_pii("카드 4111-1111-1111-1111", ON) == "카드 ****-****-****-1111"
    assert mask_pii("4111111111111111", ON) == "************1111"


def test_mask_pii_non_pii_untouched():
    text = "run completed in 42 steps, cost 1234 tokens"
    assert mask_pii(text, ON) == text
    assert mask_pii("", ON) == ""
    assert mask_pii(None, ON) is None  # type: ignore[arg-type]


def test_mask_pii_accepts_settings_object():
    cfg = Settings(pii_mask="on")
    assert mask_pii("a@b.com", cfg) == "a***@b.com"
    cfg_off = Settings(pii_mask="off")
    assert mask_pii("a@b.com", cfg_off) == "a@b.com"


# ---------------- 저장 경계 적용 (FR-027) ----------------


@pytest.fixture
def pii_on(monkeypatch):
    """get_settings()가 pii_mask=on을 반환하도록 env + 캐시를 제어."""
    monkeypatch.setenv("JOBMATE_PII_MASK", "on")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_agent_event_detail_masked_when_on(tmp_path, pii_on):
    store = Store(str(tmp_path / "pii.db"))
    try:
        store.append_agent_event(
            idempotency_key="k1",
            event_type="note",
            summary="call 010-1234-5678",
            detail={"contact": "a@b.com", "tel": "010-1234-5678"},
            actor="actor@x.com",  # actor 필드는 마스킹 대상 아님
        )
        ev = store.get_agent_event_by_key("k1")
        assert ev is not None
        blob = json.dumps(ev["detail"], ensure_ascii=False)
        assert "a***@b.com" in blob and "a@b.com" not in blob
        assert "010-****-5678" in blob
        assert ev["actor"] == "actor@x.com"  # 행위자 식별 필드 유지
        assert "010-****-5678" in (ev.get("summary") or "")
    finally:
        store.close()


def test_agent_event_detail_raw_when_off(tmp_path):
    store = Store(str(tmp_path / "pii_off.db"))
    try:
        store.append_agent_event(
            idempotency_key="k2",
            event_type="note",
            detail={"contact": "a@b.com"},
        )
        ev = store.get_agent_event_by_key("k2")
        assert ev is not None
        assert ev["detail"]["contact"] == "a@b.com"
    finally:
        store.close()


def test_agent_session_doing_ask_masked(tmp_path, pii_on):
    store = Store(str(tmp_path / "sess.db"))
    try:
        store.upsert_agent_session(
            "claude",
            "s1",
            "working",
            doing="fix bug for a@b.com",
            ask="call 010-1234-5678?",
        )
        sess = store.get_agent_session("claude", "s1")
        assert sess is not None
        assert "a***@b.com" in (sess.get("doing") or "")
        assert "010-****-5678" in (sess.get("ask") or "")
        # agent_session_events 전이 이벤트에도 마스킹 반영
        with store.tx() as c:
            rows = c.execute(
                "SELECT doing,ask FROM agent_session_events"
                " WHERE tool='claude' AND session_key='s1'"
            ).fetchall()
        assert rows, "expected a transition event"
        assert all("a@b.com" not in (r["doing"] or "") for r in rows)
        assert any("010-****-5678" in (r["ask"] or "") for r in rows)
    finally:
        store.close()


def test_agent_session_doing_raw_when_off(tmp_path):
    store = Store(str(tmp_path / "sess_off.db"))
    try:
        store.upsert_agent_session("claude", "s2", "working", doing="mail a@b.com")
        sess = store.get_agent_session("claude", "s2")
        assert sess is not None and sess.get("doing") == "mail a@b.com"
    finally:
        store.close()


# ---------------- 로그 포맷터·설정 검증 (FR-027/리뷰) ----------------


def test_pii_log_formatter_masks_message():
    """configure_logging이 pii_mask=on에서 선택하는 _PIIMaskingFormatter는
    로그 메시지 본문의 PII를 마스킹한다."""
    import logging

    from jobmate.config import _PIIMaskingFormatter

    fmt = _PIIMaskingFormatter(Settings(pii_mask="on"), "%(levelname)s %(message)s")
    rec = logging.LogRecord("t", logging.INFO, "", 0, "연락처 a@b.com / 010-1234-5678", (), None)
    out = fmt.format(rec)
    assert "a***@b.com" in out and "010-****-5678" in out
    assert "a@b.com" not in out

    fmt_off = _PIIMaskingFormatter(Settings(pii_mask="off"), "%(message)s")
    assert fmt_off.format(rec) == "연락처 a@b.com / 010-1234-5678"


def test_settings_empty_version_env_falls_back(monkeypatch):
    """빈 문자열 env는 기본값으로 복원 — ''가 저장되면 재동의가 영구 루프."""
    monkeypatch.setenv("JOBMATE_TERMS_VERSION", "")
    monkeypatch.setenv("JOBMATE_PRIVACY_VERSION", "  ")
    s = Settings()
    assert s.terms_version == "v1.0"
    assert s.privacy_version == "v1.0"


def test_settings_consent_enforce_invalid_falls_back_off(monkeypatch):
    """허용 외 값은 'off' — 강제 모드는 opt-in이므로 오설정은 열어둔다."""
    monkeypatch.setenv("JOBMATE_CONSENT_ENFORCE", "blok")
    assert Settings().consent_enforce == "off"
    monkeypatch.setenv("JOBMATE_CONSENT_ENFORCE", "read_only")
    assert Settings().consent_enforce == "read_only"
