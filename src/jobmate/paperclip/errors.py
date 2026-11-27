"""paperclip 클라이언트/브리지 예외 계층.

에러 타입 계약은 `jobmate.paperclip_client` 시절과 동일하다 —
호출자(CLI, 테스트)는 이 타입들만 잡으면 된다.
"""

from __future__ import annotations


class PaperclipError(Exception):
    """paperclip 클라이언트/브리지 오류 기본형."""


class PaperclipConfigError(PaperclipError):
    """base URL 등 설정이 정책을 위반 — 재시도해도 소용없다."""


class PaperclipHTTPError(PaperclipError):
    """HTTP 상태 오류 — status_code를 보존한다(로그에는 상태만)."""

    def __init__(self, status_code: int, detail: str = "") -> None:
        super().__init__(f"http_{status_code}" + (f": {detail[:120]}" if detail else ""))
        self.status_code = status_code


class PaperclipTimeoutError(PaperclipError):
    """네트워크 타임아웃 — 재시도 후에도 실패."""
