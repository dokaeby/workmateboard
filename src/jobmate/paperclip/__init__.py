"""paperclip 연동 패키지 — OrchestratorClient, PaperclipBridge, URL 정책.

구현은 모듈별로 분리됐다:
- `errors` — PaperclipError 계열 예외
- `urls` — base URL 정책 (webutil.validate_outbound_url 위임)
- `client` — OrchestratorClient 프로토콜 + PaperclipClient (읽기 전용)
- `bridge` — snapshot 수집/정규화 + PaperclipBridge (push)

`jobmate.paperclip_client`는 이 패키지의 호환 shim이다.
"""

from __future__ import annotations

from .bridge import PUSH_TIMEOUT, PaperclipBridge, collect_snapshot
from .client import (
    DEFAULT_TIMEOUT,
    MAX_PAGES,
    MAX_RESPONSE_BYTES,
    MAX_RETRIES,
    PAGE_SIZE,
    RETRY_BACKOFF_BASE,
    OrchestratorClient,
    PaperclipClient,
)
from .errors import (
    PaperclipConfigError,
    PaperclipError,
    PaperclipHTTPError,
    PaperclipTimeoutError,
)
from .urls import validate_jobmate_url, validate_paperclip_url

__all__ = [
    "DEFAULT_TIMEOUT",
    "MAX_PAGES",
    "MAX_RESPONSE_BYTES",
    "MAX_RETRIES",
    "PAGE_SIZE",
    "PUSH_TIMEOUT",
    "RETRY_BACKOFF_BASE",
    "OrchestratorClient",
    "PaperclipBridge",
    "PaperclipClient",
    "PaperclipConfigError",
    "PaperclipError",
    "PaperclipHTTPError",
    "PaperclipTimeoutError",
    "collect_snapshot",
    "validate_jobmate_url",
    "validate_paperclip_url",
]
