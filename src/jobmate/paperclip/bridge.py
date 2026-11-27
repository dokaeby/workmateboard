"""PaperclipBridge — OrchestratorClient 폴링 결과를 jobMate snapshot으로 push.

- snapshot 수집/정규화: 오케스트레이터 3종 엔티티(agents/tasks/runs)를
  snapshot 페이로드 dict로 변환한다.
- `PaperclipBridge` — 폴링 결과를 Workmateboard snapshot 엔드포인트로
  push하는 동기화 루프 1회분. CLI(`jobmate paperclip-bridge`)가 호출한다.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

import httpx

from ..store.external_sync import sanitize_external_payload
from ..webutil import pinned_transport_for
from .client import OrchestratorClient
from .errors import PaperclipError, PaperclipHTTPError
from .urls import validate_jobmate_url

# bridge → jobmate snapshot POST 타임아웃 — 대량 snapshot 업로드 여유.
PUSH_TIMEOUT = 20.0

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
            # 공인 호스트는 검증에 사용한 IP로 connect 고정(DNS 리바인딩
            # 차단). X-API-Key가 실리는 push 대상이므로 PaperclipClient와
            # 동일한 pinning이 필요하다(R2-3). loopback은 pinning 불필요.
            self._client = httpx.Client(
                timeout=self._timeout,
                transport=pinned_transport_for(self._jobmate_url),
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
