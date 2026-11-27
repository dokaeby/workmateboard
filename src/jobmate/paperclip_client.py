"""Paperclip 연동 — 호환 shim.

구현은 `jobmate.paperclip` 패키지(errors/urls/client/bridge)로 이동했다.
`from jobmate.paperclip_client import X` 기존 import 경로를 유지하기 위해
공개 심볼을 재수출한다. 새 코드는 `jobmate.paperclip`을 import한다.
"""

from __future__ import annotations

from .paperclip import (
    DEFAULT_TIMEOUT,
    MAX_PAGES,
    MAX_RESPONSE_BYTES,
    MAX_RETRIES,
    PAGE_SIZE,
    PUSH_TIMEOUT,
    RETRY_BACKOFF_BASE,
    OrchestratorClient,
    PaperclipBridge,
    PaperclipClient,
    PaperclipConfigError,
    PaperclipError,
    PaperclipHTTPError,
    PaperclipTimeoutError,
    collect_snapshot,
    validate_jobmate_url,
    validate_paperclip_url,
)

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
