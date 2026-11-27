"""Paperclip OrchestratorClient — 외부 control plane 읽기 전용 클라이언트.

agent-management 3차 (paperclip spec E) — Workmateboard는 Paperclip을
재구현하지 않고 외부 상태를 읽기 모델로 투영한다. 이 모듈은:

- `OrchestratorClient` 프로토콜 — 테스트에서 fake를 주입할 수 있게 한다.
- `PaperclipClient` — httpx 기반 REST 클라이언트. base URL 검증
  (https 공인 호스트 또는 localhost/loopback만 허용 — spec의
  "HTTPS/localhost URL 정책"), 타임아웃 필수, 리다이렉트 비추적,
  429/5xx 지수 백오프 재시도, 인증 헤더는 로그에 남기지 않는다.
- `PaperclipBridge` — 폴링 결과를 Workmateboard snapshot 엔드포인트로
  push하는 동기화 루프 1회분. CLI(`jobmate paperclip-bridge`)가 호출한다.

엔드포인트 경로는 spec이 정확한 REST 경로를 정의하지 않아 다음을 가정한다
(docs/harness/05 §26에 명시):
  GET /api/companies/{company_id}/agents|tasks|runs|dashboard|cost-summary
페이지네이션은 `?limit=&cursor=` 파라미터와 응답의 next_cursor/next 계열
필드를 지원한다. `next`가 절대 URL이면 따라가지 않고 cursor 값만 사용한다
(임의 호스트로의 리다이렉트/SSRF 방지).
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import random
import time
from typing import Any, Protocol, runtime_checkable
from urllib.parse import quote, urlparse

import httpx

from .store.external_sync import sanitize_external_payload
from .webutil import _pinned_ip_for, _PinnedTransport, is_safe_fetch_url

logger = logging.getLogger("jobmate.paperclip")

DEFAULT_TIMEOUT = 10.0
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_PAGES = 50
PAGE_SIZE = 200
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 0.5
# bridge → jobmate snapshot POST 타임아웃 — 대량 snapshot 업로드 여유.
PUSH_TIMEOUT = 20.0


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


# ---------------------------------------------------------------------------
# URL 정책 — spec E: HTTPS/localhost URL 정책
# ---------------------------------------------------------------------------


def _is_loopback_host(host: str) -> bool:
    h = (host or "").strip().lower().rstrip(".")
    if not h:
        return False
    if h == "localhost" or h.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False
    return ip.is_loopback


def _validate_url_policy(url: str, *, what: str) -> str:
    """공용 URL 정책 — bearer/API 키가 실리는 엔드포인트용.

    - scheme은 http/https만.
    - **http는 loopback 전용** — 평문 채널로 공인 호스트에 자격증명을
      보내는 것을 금지한다(https 아니면 loopback만).
    - userinfo(내장 credential) 금지 — URL에 비밀이 박히는 것을 차단.
    - 비-loopback 호스트는 webutil SSRF 가드(is_safe_fetch_url) 통과 필수.
    위반 시 PaperclipConfigError(에러 메시지에 URL 원문/자격증명 미포함)."""
    raw = (url or "").strip()
    if not raw:
        raise PaperclipConfigError(f"{what} url is empty")
    if "://" not in raw:
        raw = f"https://{raw}"
    try:
        parsed = urlparse(raw)
    except ValueError as e:
        raise PaperclipConfigError(f"{what} url is invalid") from e
    if parsed.scheme not in ("http", "https"):
        raise PaperclipConfigError(f"{what} url scheme must be http or https")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise PaperclipConfigError(f"{what} url has no host")
    if parsed.username or parsed.password:
        raise PaperclipConfigError(f"{what} url must not contain credentials")
    loopback = _is_loopback_host(host)
    if parsed.scheme == "http" and not loopback:
        raise PaperclipConfigError(f"{what} url must use https for non-loopback hosts")
    if not loopback and not is_safe_fetch_url(raw):
        raise PaperclipConfigError(f"{what} url host is not allowed")
    return raw.rstrip("/")


def validate_paperclip_url(url: str) -> str:
    """paperclip base URL 검증 — 정규화된 base URL을 반환한다.

    허용: https + (loopback 호스트 또는 webutil SSRF 가드 통과 공인
    호스트), 또는 http + loopback만. Bearer 토큰이 실리므로 공인 호스트의
    http는 거부한다(spec의 "HTTPS/localhost URL 정책")."""
    return _validate_url_policy(url, what="paperclip")


def validate_jobmate_url(url: str) -> str:
    """bridge → jobMate snapshot 엔드포인트 base URL 검증.

    X-API-Key가 실리므로 paperclip과 동일 정책 — https 공인 호스트 또는
    http/https loopback만 허용한다."""
    return _validate_url_policy(url, what="jobmate")


# ---------------------------------------------------------------------------
# 프로토콜 + 클라이언트
# ---------------------------------------------------------------------------


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
            transport = None
            pinned = _pinned_ip_for(self.base_url)
            if pinned is not None:
                # 공인 호스트 — 검증에 사용한 IP로 connect 고정(DNS 리바인딩 차단)
                transport = _PinnedTransport(pinned)
            self._client = httpx.Client(
                timeout=self._timeout,
                transport=transport,
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


# ---------------------------------------------------------------------------
# snapshot 수집/정규화 — OrchestratorClient → snapshot payload
# ---------------------------------------------------------------------------

_EXTERNAL_ID_KEYS = ("external_id", "id", "uuid", "key")
_STATUS_KEYS = ("status", "state")
_UPDATED_KEYS = ("updated_at", "updatedAt", "modified_at", "modifiedAt")


def _first_str(item: dict[str, Any], keys: tuple[str, ...]) -> str:
    for k in keys:
        v = item.get(k)
        if isinstance(v, (str, int)) and str(v).strip():
            return str(v).strip()[:500]
    return ""


def _to_snapshot_agent(item: dict[str, Any]) -> dict[str, Any] | None:
    ext = _first_str(item, _EXTERNAL_ID_KEYS)
    if not ext:
        return None
    # 전체 아이템을 payload_summary 후보로 — 서버/브리지 양쪽에서
    # secret·transcript·adapter env를 제거한다.
    summary = sanitize_external_payload(item)
    return {
        "external_id": ext,
        "status": _first_str(item, _STATUS_KEYS)[:60],
        "payload_summary": summary if isinstance(summary, dict) else {},
        "updated_at": _first_str(item, _UPDATED_KEYS)[:64],
    }


def _to_snapshot_task(item: dict[str, Any]) -> dict[str, Any] | None:
    ext = _first_str(item, _EXTERNAL_ID_KEYS)
    if not ext:
        return None
    assignee = item.get("assignee_external_id") or item.get("assignee_id") or item.get("assignee")
    return {
        "external_id": ext,
        "assignee_external_id": str(assignee).strip()[:200] if assignee else None,
        "status": _first_str(item, _STATUS_KEYS)[:60],
        "title": _first_str(item, ("title", "name", "summary"))[:300],
        "updated_at": _first_str(item, _UPDATED_KEYS)[:64],
    }


def _to_snapshot_run(item: dict[str, Any]) -> dict[str, Any] | None:
    ext = _first_str(item, _EXTERNAL_ID_KEYS)
    if not ext:
        return None
    usage_raw = item.get("usage")
    cost_raw = item.get("cost")
    usage: dict[str, Any] = usage_raw if isinstance(usage_raw, dict) else {}
    cost: dict[str, Any] = cost_raw if isinstance(cost_raw, dict) else {}
    summary: dict[str, Any] = {}
    for src in (item, usage, cost):
        for k, v in src.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                summary[str(k)] = v
            elif isinstance(v, str) and len(v) <= 120:
                summary[str(k)] = v
    return {
        "external_id": ext,
        "task_external_id": (
            str(item.get("task_external_id") or item.get("task_id") or "").strip()[:200] or None
        ),
        "agent_external_id": (
            str(item.get("agent_external_id") or item.get("agent_id") or "").strip()[:200] or None
        ),
        "status": _first_str(item, _STATUS_KEYS)[:60],
        "token_cost_summary": summary,
        "updated_at": _first_str(item, _UPDATED_KEYS)[:64],
    }


def collect_snapshot(
    client: OrchestratorClient, *, updated_after: dict[str, str | None] | None = None
) -> dict[str, Any]:
    """오케스트레이터 3종 엔티티를 조회해 snapshot 페이로드 dict로 만든다.

    반환: {"agents": [...], "tasks": [...], "runs": [...],
    "cursors": {"agents": max_updated_at, ...}} — cursors는 서버
    external_sync_cursors에 병합 저장된다."""
    ua = (updated_after or {}).get("agents")
    ut = (updated_after or {}).get("tasks")
    ur = (updated_after or {}).get("runs")
    agents = [_to_snapshot_agent(i) for i in client.list_agents(ua)]
    tasks = [_to_snapshot_task(i) for i in client.list_tasks(ut)]
    runs = [_to_snapshot_run(i) for i in client.list_runs(ur)]
    cursors: dict[str, str] = {}
    for entity, items in (("agents", agents), ("tasks", tasks), ("runs", runs)):
        stamps = [i["updated_at"] for i in items if i and i.get("updated_at")]
        if stamps:
            cursors[entity] = max(stamps)
    return {
        "agents": [i for i in agents if i],
        "tasks": [i for i in tasks if i],
        "runs": [i for i in runs if i],
        "cursors": cursors,
    }


# ---------------------------------------------------------------------------
# Bridge — polling → jobmate snapshot push
# ---------------------------------------------------------------------------


class PaperclipBridge:
    """OrchestratorClient 폴링 결과를 jobMate machine endpoint로 push.

    멱등키는 canonical payload의 sha256 — 재시도(동일 페이로드)는 서버에서
    replay되고, 내용이 바뀌면 새 키가 되어 false conflict가 없다.
    """

    def __init__(
        self,
        orchestrator: OrchestratorClient,
        *,
        jobmate_url: str,
        api_key: str | None = None,
        company_id: str = "",
        provider: str = "paperclip",
        client: httpx.Client | None = None,
        timeout: float = PUSH_TIMEOUT,
        sleep: Any = time.sleep,
    ) -> None:
        self._orch = orchestrator
        self._provider = (provider or "paperclip").strip()[:40] or "paperclip"
        # OrchestratorClient.company_id가 비어 있으면 생성자 인자로 폴백.
        self._company_id = (getattr(orchestrator, "company_id", "") or company_id or "").strip()[
            :200
        ]
        # jobmate_url도 검증한다 — X-API-Key가 실리는 push 대상이므로
        # paperclip과 동일 정책(https 공인 또는 loopback http/https).
        self._jobmate_url = validate_jobmate_url(jobmate_url)
        self._api_key = (api_key or "").strip()
        self._client = client
        self._owns_client = client is None
        self._timeout = timeout
        self._sleep = sleep
        # entity별 마지막 updated_after — 루프 내 증분 폴링.
        self._updated_after: dict[str, str | None] = {"agents": None, "tasks": None, "runs": None}

    def _http(self) -> httpx.Client:
        if self._client is None:
            transport = None
            pinned = _pinned_ip_for(self._jobmate_url)
            if pinned is not None:
                # 공인 호스트 — 검증에 사용한 IP로 connect 고정(DNS 리바인딩
                # 차단). X-API-Key가 실리는 push 대상이므로 PaperclipClient와
                # 동일한 pinning이 필요하다(R2-3). loopback은 pinning 불필요.
                transport = _PinnedTransport(pinned)
            self._client = httpx.Client(
                timeout=self._timeout,
                transport=transport,
                follow_redirects=False,
            )
        return self._client

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    def sync_once(self) -> dict[str, Any]:
        """1회 동기화 — snapshot 수집 → POST → 결과 counts 반환.

        실패 시 PaperclipError/httpx 오류가 그대로 올라간다 — 호출자(CLI)가
        safe 로그 후 다음 주기에 재시도한다."""
        snap = collect_snapshot(self._orch, updated_after=self._updated_after)
        company = self._company_id or str(getattr(self._orch, "company_id", "") or "")
        payload: dict[str, Any] = {
            "provider": self._provider,
            "company_id": company,
            "agents": snap["agents"],
            "tasks": snap["tasks"],
            "runs": snap["runs"],
            "cursors": snap["cursors"],
        }
        canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        idem_key = "pc-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:40]
        headers = {"Idempotency-Key": idem_key}
        if self._api_key:
            headers["X-API-Key"] = self._api_key
        resp = self._http().post(
            f"{self._jobmate_url}/api/v1/management/integrations/paperclip/snapshot",
            json=payload,
            headers=headers,
        )
        if resp.status_code == 409:
            raise PaperclipError("snapshot_conflict")
        if resp.status_code >= 400:
            raise PaperclipHTTPError(resp.status_code)
        try:
            result = resp.json()
        except (ValueError, json.JSONDecodeError) as e:
            raise PaperclipError("invalid_snapshot_response") from e
        # 커서 전진은 push 성공 후에만 — 실패 시 다음 루프가 같은 범위를 재조회.
        for entity, stamp in (snap.get("cursors") or {}).items():
            if entity in self._updated_after and stamp:
                self._updated_after[entity] = stamp
        if isinstance(result, dict):
            return result
        return {
            "agents": len(snap["agents"]),
            "tasks": len(snap["tasks"]),
            "runs": len(snap["runs"]),
        }
