"""connector 라이브 fetch + 60초 인메모리 캐시.

support.py에서 분리. github 전용 API와 generic JSON fetch(config.url)를
제공하며, config 비밀값 복호화는 sensitive_fields에 위임한다.
"""

from __future__ import annotations

from typing import Any

from .. import webutil
from .sensitive_fields import _connector_token, _decrypt_config_secrets


def _fetch_connector_items(connector: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    """커넥터 라이브 fetch — github는 전용 API, 나머지는 config.url JSON fetch.

    jira/notion/linear는 config.url(+선택적 token/json_path/fields)만으로 동작하는
    generic JSON fetch로 지원한다. 실패는 FetchError/ValueError로 전파."""
    ctype = str(connector.get("type") or "")
    cfg = _decrypt_config_secrets(dict(connector.get("config") or {}))
    headers = {"Accept": "application/json", "User-Agent": "jobmate-connector"}
    token = _connector_token(cfg)
    if token:
        headers["Authorization"] = f"Bearer {token}"

    if ctype == "github":
        owner = str(cfg.get("owner") or "").strip()
        repo = str(cfg.get("repo") or cfg.get("repository") or "").strip()
        if not owner or not repo:
            raise ValueError("github connector requires config.owner and config.repo")
        state = str(cfg.get("state") or "all")
        url = (
            f"https://api.github.com/repos/{owner}/{repo}/issues"
            f"?state={state}&per_page={min(limit, 100)}&sort=updated"
        )
        gh_headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "jobmate-connector",
        }
        if token:
            gh_headers["Authorization"] = f"Bearer {token}"
        data = webutil.fetch_json(url, headers=gh_headers, timeout=10.0)
        items = []
        for it in data if isinstance(data, list) else []:
            user = it.get("user") if isinstance(it.get("user"), dict) else {}
            items.append(
                {
                    "id": str(it.get("number") or it.get("id") or ""),
                    "title": str(it.get("title") or ""),
                    "url": str(it.get("html_url") or ""),
                    "state": str(it.get("state") or ""),
                    "updated_at": str(it.get("updated_at") or ""),
                    "author": str(user.get("login") or ""),
                }
            )
        return items[:limit]

    url = str(cfg.get("url") or "").strip()
    if not url:
        raise ValueError(f"{ctype or 'connector'} requires config.url")
    # SSRF 가드(방어 심층) — 생성 시점 검증을 우회한 행(DB 직접 삽입,
    # config 변경 등)도 여기서 차단한다. fetch_json 내부 assert와 이중 확인.
    if not webutil.is_safe_fetch_url(url):
        raise ValueError("url_not_allowed")
    for k, v in (cfg.get("headers") or {}).items():
        if isinstance(k, str) and isinstance(v, str):
            headers[k] = v
    data = webutil.fetch_json(url, headers=headers, timeout=10.0)
    path = str(cfg.get("json_path") or cfg.get("items_path") or "")
    raw = webutil.find_item_list(data, path)
    fields = cfg.get("fields") if isinstance(cfg.get("fields"), dict) else None
    return webutil.normalize_items(raw, fields)[:limit]


# connector items 60초 인메모리 캐시 (connector_id → (fetched_at, items))
_CONNECTOR_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_CONNECTOR_CACHE_TTL = 60.0
