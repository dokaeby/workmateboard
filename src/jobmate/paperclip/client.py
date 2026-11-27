"""Paperclip OrchestratorClient — 외부 control plane 읽기 전용 클라이언트.

agent-management 3차 (paperclip spec E) — Workmateboard는 Paperclip을
재구현하지 않고 외부 상태를 읽기 모델로 투영한다. 이 모듈은:

- `OrchestratorClient` 프로토콜 — 테스트에서 fake를 주입할 수 있게 한다.
- `PaperclipClient` — httpx 기반 REST 클라이언트. base URL 검증
  (`paperclip.urls` — https 공인 호스트 또는 localhost/loopback만 허용,
  spec의 "HTTPS/localhost URL 정책"), 타임아웃 필수, 리다이렉트 비추적,
  429/5xx 지수 백오프 재시도, 인증 헤더는 로그에 남기지 않는다.

엔드포인트 경로는 spec이 정확한 REST 경로를 정의하지 않아 다음을 가정한다
(docs/jobMate_v1.0.1/04_기능별상세설계서_v1.0.1.md 부록 A §26에 명시):
  GET /api/companies/{company_id}/agents|tasks|runs|dashboard|cost-summary
페이지네이션은 `?limit=&cursor=` 파라미터와 응답의 next_cursor/next 계열
필드를 지원한다. `next`가 절대 URL이면 따라가지 않고 cursor 값만 사용한다
(임의 호스트로의 리다이렉트/SSRF 방지).
"""

from __future__ import annotations

import json
import logging
import random
import time
from typing import Any, Protocol, runtime_checkable
from urllib.parse import quote

import httpx

from ..webutil import pinned_transport_for
from .errors import PaperclipError, PaperclipHTTPError, PaperclipTimeoutError
from .urls import validate_paperclip_url

logger = logging.getLogger("jobmate.paperclip")

DEFAULT_TIMEOUT = 10.0
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_PAGES = 50
PAGE_SIZE = 200
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 0.5


@runtime_checkable
class OrchestratorClient(Protocol):
    """외부 오케스트레이터 읽기 인터페이스 — fake 주입용."""

    company_id: str

    def get_dashboard(self) -> dict[str, Any]: ...

    def list_agents(self, updated_after: str | None = None) -> list[dict[str, Any]]: ...

    def list_tasks(self, updated_after: str | None = None) -> list[dict[str, Any]]: ...

    def list_runs(self, updated_after: str | None = None) -> list[dict[str, Any]]: ...

    def get_cost_summary(self) -> dict[str, Any]: ...


def _extract_items(data: Any, entity: str) -> list[dict[str, Any]]:
    """응답에서 엔티티 리스트 추출 — list 본문, {entity:[]},
    {items|data|results:[...]} 순으로 탐색한다."""
    raw: list[Any] = []
    if isinstance(data, list):
        raw = data
    elif isinstance(data, dict):
        v = data.get(entity)
        if isinstance(v, list):
            raw = v
        else:
            for key in ("items", "data", "results", "nodes", "value"):
                cand = data.get(key)
                if isinstance(cand, list):
                    raw = cand
                    break
            if not raw:
                for cand in data.values():
                    if isinstance(cand, list):
                        raw = cand
                        break
    return [it for it in raw if isinstance(it, dict)]


def _extract_next_cursor(data: Any) -> str | None:
    """다음 페이지 커서 — next_cursor/cursor/next_page/page_token/next.

    `next`가 URL이면 절대 호스트로의 임의 요청을 막기 위해 무시한다."""
    if not isinstance(data, dict):
        return None
    for key in ("next_cursor", "cursor", "next_page", "page_token", "after"):
        v = data.get(key)
        if isinstance(v, (str, int)) and str(v).strip():
            return str(v).strip()[:500]
    nxt = data.get("next")
    if (
        isinstance(nxt, str)
        and nxt.strip()
        and not nxt.lstrip().lower().startswith(("http://", "https://", "/"))
    ):
        return nxt.strip()[:500]
    return None


class PaperclipClient:
    """Paperclip REST 클라이언트 — 읽기 전용.

    `client`에 httpx.Client를 주입하면 transport/timeout을 테스트가
    제어할 수 있다(미주입 시 SSRF 고정 transport로 자체 생성).
    """

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None = None,
        company_id: str = "",
        timeout: float = DEFAULT_TIMEOUT,
        client: httpx.Client | None = None,
        max_retries: int = MAX_RETRIES,
        max_pages: int = MAX_PAGES,
        page_size: int = PAGE_SIZE,
        sleep: Any = time.sleep,
    ) -> None:
        self.base_url = validate_paperclip_url(base_url)
        self.company_id = (company_id or "").strip()
        self._api_key = (api_key or "").strip()
        self._timeout = timeout
        self._client = client
        self._owns_client = client is None
        self._max_retries = max(0, max_retries)
        self._max_pages = max(1, max_pages)
        self._page_size = max(1, min(page_size, 500))
        self._sleep = sleep

    # ---- 내부 ----

    def _http(self) -> httpx.Client:
        if self._client is None:
            # 공인 호스트는 검증에 사용한 IP로 connect 고정(DNS 리바인딩
            # 차단). loopback·검증 불가 호스트는 None → 기본 transport.
            self._client = httpx.Client(
                timeout=self._timeout,
                transport=pinned_transport_for(self.base_url),
                follow_redirects=False,
            )
        return self._client

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> PaperclipClient:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/json"}
        if self._api_key:
            # 인증 헤더는 로그/예외 메시지에 절대 포함하지 않는다.
            h["Authorization"] = f"Bearer {self._api_key}"
        return h

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}{path}"
        last_exc: PaperclipError | None = None
        for attempt in range(self._max_retries + 1):
            try:
                resp = self._http().get(url, params=params, headers=self._headers())
            except httpx.TimeoutException as e:
                last_exc = PaperclipTimeoutError(str(type(e).__name__))
            except httpx.HTTPError as e:
                last_exc = PaperclipError(type(e).__name__)
            else:
                status = resp.status_code
                if status in (401, 403):
                    # 인증 실패는 재시도 무의미 — 즉시 실패.
                    raise PaperclipHTTPError(status)
                if status == 429 or status >= 500:
                    last_exc = PaperclipHTTPError(status)
                elif status >= 300:
                    # 리다이렉트 비추적 — 3xx도 오류로 처리(가드 우회 방지).
                    raise PaperclipHTTPError(status)
                else:
                    if len(resp.content) > MAX_RESPONSE_BYTES:
                        raise PaperclipError("response_too_large")
                    try:
                        return resp.json()
                    except (ValueError, json.JSONDecodeError) as e:
                        raise PaperclipError("invalid_json") from e
            if attempt < self._max_retries:
                # 지수 백오프 + 지터 — 429/5xx/타임아웃에만 적용.
                jitter = random.uniform(0, 0.25)  # noqa: S311 - 재시도 지터, 보안 용도 아님
                delay = min(RETRY_BACKOFF_BASE * (2**attempt), 8.0) + jitter
                logger.warning(
                    "paperclip GET %s attempt=%d failed: %s (retry in %.1fs)",
                    path.split("?")[0],
                    attempt + 1,
                    type(last_exc).__name__ if last_exc else "?",
                    delay,
                )
                self._sleep(delay)
                continue
            break
        if isinstance(last_exc, PaperclipTimeoutError):
            raise last_exc
        raise last_exc if last_exc is not None else PaperclipError("request_failed")

    def _company_path(self, entity: str) -> str:
        comp = quote(self.company_id, safe="")
        return f"/api/companies/{comp}/{entity}"

    # ---- OrchestratorClient ----

    def get_dashboard(self) -> dict[str, Any]:
        data = self._get(self._company_path("dashboard"))
        return data if isinstance(data, dict) else {}

    def get_cost_summary(self) -> dict[str, Any]:
        data = self._get(self._company_path("cost-summary"))
        return data if isinstance(data, dict) else {}

    def _list(self, entity: str, updated_after: str | None) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(self._max_pages):
            params: dict[str, Any] = {"limit": self._page_size}
            if cursor:
                params["cursor"] = cursor
            if updated_after:
                params["updated_after"] = updated_after
            data = self._get(self._company_path(entity), params=params)
            items.extend(_extract_items(data, entity))
            cursor = _extract_next_cursor(data)
            if not cursor:
                break
        return items

    def list_agents(self, updated_after: str | None = None) -> list[dict[str, Any]]:
        return self._list("agents", updated_after)

    def list_tasks(self, updated_after: str | None = None) -> list[dict[str, Any]]:
        return self._list("tasks", updated_after)

    def list_runs(self, updated_after: str | None = None) -> list[dict[str, Any]]:
        return self._list("runs", updated_after)
