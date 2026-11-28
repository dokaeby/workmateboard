"""Sentry 에러 추적 — spec 004 US4 (FR-025~FR-028).

JOBMATE_SENTRY_DSN이 설정된 경우에만 초기화한다. sentry-sdk는 optional
extra(`uv sync --extra sentry`)이며 미설치/미설정/초기화 실패 모두
no-op — 앱 기동과 요청 처리를 절대 막지 않는다.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any

logger = logging.getLogger("jobmate.sentry")

_lock = threading.Lock()
# unconfigured | ok | failed — failed는 초기화/패키지 부재 후 재시도하지 않는다.
_state = "unconfigured"

# capture_exception 태그 화이트리스트 — 이메일·토큰·PII 키는 여기 없어
# 어떤 경로로 들어와도 Sentry로 전송되지 않는다(FR-026/027).
_ALLOWED_TAGS = frozenset(
    {"request_id", "path", "method", "workflow", "run_id", "component", "status", "event"}
)

# 스크러버가 요청 컨텍스트에서 보존하는 필드 — 나머지(헤더·본문·쿠키·
# query·env·user)는 제거한다.
_REQUEST_KEEP = ("method", "url")

# 자유 텍스트 마스킹 — 키 기반 스크럽을 빠져나간 예외 메시지·로그·
# 브레드크럼에 섞인 이메일/토큰을 걸러낸다(FR-027). 보수적으로
# 긴 토큰 형태의 값만 대상으로 해 일반 문장은 건드리지 않는다.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_BEARER_RE = re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9._~+/=-]{6,}")
_KV_SECRET_RE = re.compile(
    r"(?i)([\"']?\b(?:api[_-]?key|access[_-]?key|token|secret|password|authorization|key)\b"
    r"[\"']?\s*[=:]\s*[\"']?)[A-Za-z0-9._~+/=-]{8,}"
)


def _redact_text(value: Any) -> Any:
    """문자열 내 이메일·Bearer 토큰·key=value 시크릿을 마스킹한다."""
    if not isinstance(value, str):
        return value
    s = _EMAIL_RE.sub("[email]", value)
    s = _BEARER_RE.sub(r"\1[redacted]", s)
    return _KV_SECRET_RE.sub(r"\1[redacted]", s)


def _scrub_event(event: Any, hint: Any = None) -> Any:
    """before_send/before_send_transaction 공용 스크러버 (FR-027).

    - request: method + path만 남기고 headers/data/cookies/query 제거
    - user/email/contexts의 PII 제거
    - tags는 화이트리스트 키만 유지
    """
    if not isinstance(event, dict):
        return event
    req = event.get("request")
    if isinstance(req, dict):
        url = req.get("url") or ""
        path = str(url).split("?", 1)[0] if isinstance(url, str) else ""
        scrubbed = {k: req.get(k) for k in _REQUEST_KEEP if req.get(k)}
        if path:
            scrubbed["url"] = path
        event["request"] = scrubbed
    # 사용자 식별자(이메일·username·ip) 제거
    event.pop("user", None)
    contexts = event.get("contexts")
    if isinstance(contexts, dict):
        for ctx_key in ("user", "response"):
            contexts.pop(ctx_key, None)
    # response 본문 컨텍스트 제거
    event.pop("response", None)
    # extra/contexts 내 민감 키 재귀 제거 — 값 자체는 유지하되
    # email/token/secret/password/authorization/cookie 계열 키는 버린다.
    _SENSITIVE_KEY_PARTS = ("email", "token", "secret", "password", "authorization", "cookie")

    def _scrub_obj(obj: Any) -> None:
        if isinstance(obj, dict):
            for k in list(obj.keys()):
                kl = str(k).lower()
                if any(p in kl for p in _SENSITIVE_KEY_PARTS):
                    del obj[k]
                else:
                    _scrub_obj(obj[k])
        elif isinstance(obj, list):
            for item in obj:
                _scrub_obj(item)

    for key in ("extra", "contexts"):
        if key in event:
            _scrub_obj(event[key])
    tags = event.get("tags")
    if isinstance(tags, dict):
        event["tags"] = {k: v for k, v in tags.items() if k in _ALLOWED_TAGS}
    # 자유 텍스트 필드 마스킹 — 예외 메시지/로그엔트리/브레드크럼은
    # 키 기반 스크럽 대상이 아니므로 여기서 이메일·토큰을 제거한다.
    exception = event.get("exception")
    if isinstance(exception, dict):
        for v in exception.get("values") or []:
            if isinstance(v, dict) and isinstance(v.get("value"), str):
                v["value"] = _redact_text(v["value"])
    if isinstance(event.get("message"), str):
        event["message"] = _redact_text(event["message"])
    logentry = event.get("logentry")
    if isinstance(logentry, dict):
        for k in ("message", "formatted"):
            if isinstance(logentry.get(k), str):
                logentry[k] = _redact_text(logentry[k])
    crumbs = event.get("breadcrumbs")
    crumb_list = crumbs.get("values") if isinstance(crumbs, dict) else crumbs
    if isinstance(crumb_list, list):
        for crumb in crumb_list:
            if not isinstance(crumb, dict):
                continue
            if isinstance(crumb.get("message"), str):
                crumb["message"] = _redact_text(crumb["message"])
            data = crumb.get("data")
            if isinstance(data, dict):
                for k, v in list(data.items()):
                    if isinstance(v, str):
                        data[k] = _redact_text(v)
    return event


def _init_failed(reason: str) -> bool:
    global _state
    with _lock:
        if _state != "failed":
            # FR-028 — 초기화/전송 실패는 warning 1회 후 비활성화
            logger.warning("sentry disabled: %s", reason)
        _state = "failed"
    return False


def configure_sentry(settings: Any) -> bool:
    """Sentry 초기화 — DSN 없으면 no-op False. 실패해도 앱 기동을 막지 않는다."""
    global _state
    dsn = str(getattr(settings, "sentry_dsn", "") or "").strip()
    if not dsn:
        return False
    with _lock:
        if _state == "ok":
            return True
        if _state == "failed":
            return False
    try:
        import sentry_sdk
    except Exception:
        return _init_failed("sentry-sdk package not installed (install with --extra sentry)")
    try:
        rate = float(getattr(settings, "sentry_traces_sample_rate", 0.0) or 0.0)
        rate = min(1.0, max(0.0, rate))
        environment = str(getattr(settings, "sentry_environment", "") or "") or str(
            getattr(settings, "mode", "") or ""
        )
        sentry_sdk.init(
            dsn=dsn,
            send_default_pii=False,
            traces_sample_rate=rate,
            environment=environment or None,
            before_send=_scrub_event,
            before_send_transaction=_scrub_event,
        )
    except Exception as e:
        return _init_failed(f"init failed: {type(e).__name__}")
    with _lock:
        _state = "ok"
    logger.info("sentry enabled environment=%s", environment or "-")
    return True


def _configured() -> bool:
    with _lock:
        return _state == "ok"


def capture_exception(exc: BaseException, *, tags: dict[str, Any] | None = None) -> None:
    """예외 캡처 — 미설정/비활성이면 no-op. 태그는 화이트리스트만 통과."""
    if not _configured() or exc is None:
        return
    filtered = {
        str(k): str(v)[:200]
        for k, v in (tags or {}).items()
        if k in _ALLOWED_TAGS and v is not None and str(v) != ""
    }
    try:
        import sentry_sdk

        if filtered:
            with sentry_sdk.push_scope() as scope:
                scope.set_tags(filtered)
                sentry_sdk.capture_exception(exc)
        else:
            sentry_sdk.capture_exception(exc)
    except Exception:
        # 캡처 실패가 요청/잡 경로를 죽이면 안 된다 — 디버그만 남긴다.
        logger.debug("sentry capture failed", exc_info=True)


def reset_for_tests() -> None:
    """테스트 격리용 — 모듈 상태를 초기화한다."""
    global _state
    with _lock:
        _state = "unconfigured"
