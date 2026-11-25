"""skillRadar 포팅 — GitHub 수집 클라이언트 (동기 httpx, 재시도/backoff)."""

from __future__ import annotations

import base64
import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("jobmate.radar")

SEARCH_URL = "https://api.github.com/search/repositories"
CONTENTS_URL = "https://api.github.com/repos/{full_name}/readme"
MAX_README_CHARS = 3000

DEFAULT_QUERIES: dict[str, list[str]] = {
    "claude": [
        "topic:claude-skills archived:false fork:false",
        "topic:claude-code-plugin archived:false fork:false",
        '"claude skill" in:name,description,readme archived:false fork:false',
    ],
    "chatgpt": [
        "topic:chatgpt-plugin archived:false fork:false",
        "topic:custom-gpt archived:false fork:false",
        '"chatgpt plugin" in:name,description archived:false fork:false',
    ],
    "gemini": [
        "topic:gemini-extension archived:false fork:false",
        "topic:gemini-cli-extension archived:false fork:false",
        '"gemini extension" in:name,description archived:false fork:false',
    ],
}


def load_queries(path: Path | None = None) -> dict[str, list[str]]:
    """queries.json 로드. 없으면 DEFAULT_QUERIES."""
    if path is None:
        path = Path(__file__).parent / "queries.json"
    if not path.exists():
        return DEFAULT_QUERIES
    data = json.loads(path.read_text())
    return {k: list(v) for k, v in data.items()}


def stars_delta(current: int, previous: int | None) -> int | None:
    if previous is None:
        return None
    return max(0, current - previous)


class GitHubAPI:
    """GitHub Search/Contents 얇은 래퍼 — Protocol GitHubClient 구현."""

    def __init__(
        self, token: str = "", timeout: float = 20.0, max_retries: int = 3, backoff: float = 2.0
    ) -> None:
        self._token = token
        self._client = httpx.Client(timeout=timeout)
        self._max_retries = max_retries
        self._backoff = backoff

    def _headers(self, token: str | None = None) -> dict[str, str]:
        h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        tok = self._token if token is None else token
        if tok:
            h["Authorization"] = f"Bearer {tok}"
        return h

    def _get_json(self, url: str, params: dict | None = None) -> dict | None:
        delay = self._backoff
        path = url.split("?", 1)[0]
        # 401 시 토큰을 영구 클리어하지 않는다 — 일시적 401(전파 지연,
        # GitHub 측 blip)로 유효한 토큰을 잃는 것을 막기 위해 이 호출
        # 안에서만 토큰 없이 1회 재시도한다. 실제로 만료된 토큰이면
        # 다음 호출에서도 동일하게 재시도되지만 성공 경로는 유지된다.
        token = self._token
        for attempt in range(self._max_retries + 1):
            try:
                resp = self._client.get(url, params=params, headers=self._headers(token))
            except httpx.HTTPError as e:
                if attempt == self._max_retries:
                    logger.warning(
                        "github api error path=%s attempt=%d/%d: %s",
                        path,
                        attempt + 1,
                        self._max_retries + 1,
                        str(e)[:120],
                    )
                    return None
                time.sleep(delay)
                delay *= 2
                continue
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 401 and token:
                # 토큰 거부 — 이 요청에 한해 토큰 없이 1회 재시도.
                # self._token은 유지한다(비영구화 — 일시적 401 오인 방지).
                logger.warning("github api 401 — token rejected, retrying unauthenticated")
                token = ""
                continue
            if resp.status_code in (403, 429, 500, 502, 503):
                if attempt == self._max_retries:
                    logger.warning(
                        "github api error path=%s status=%s after %d attempts",
                        path,
                        resp.status_code,
                        self._max_retries + 1,
                    )
                    return None
                ra = resp.headers.get("Retry-After")
                try:
                    wait = min(float(ra), 60.0) if ra else delay
                except ValueError:
                    # Retry-After가 HTTP-date 등 비숫자면 기본 백오프로.
                    wait = delay
                time.sleep(wait)
                delay *= 2
                continue
            logger.warning("github api error path=%s status=%s", path, resp.status_code)
            return None
        return None

    def search_repositories(self, query: str, per_page: int = 30) -> list[dict[str, Any]]:
        data = self._get_json(
            SEARCH_URL, params={"q": query, "sort": "stars", "order": "desc", "per_page": per_page}
        )
        if not data:
            return []
        hits = []
        for item in data.get("items", []):
            if item.get("archived") or item.get("fork"):
                continue
            hits.append(
                {
                    "full_name": item.get("full_name", ""),
                    "html_url": item.get("html_url", ""),
                    "description": item.get("description"),
                    "stars": int(item.get("stargazers_count", 0)),
                    "language": item.get("language"),
                    "topics": list(item.get("topics") or []),
                    "archived": bool(item.get("archived")),
                    "fork": bool(item.get("fork")),
                    "license": (item.get("license") or {}).get("spdx_id"),
                }
            )
        return hits

    def fetch_readme(self, full_name: str) -> str:
        url = CONTENTS_URL.format(full_name=full_name)
        # raw 미디어 타입으로 요청 — 실패 시 JSON content(base64) 디코딩 폴백
        try:
            resp = self._client.get(
                url,
                headers={**self._headers(), "Accept": "application/vnd.github.raw"},
                follow_redirects=True,
            )
            if resp.status_code == 200 and resp.text:
                return resp.text[:MAX_README_CHARS]
        except httpx.HTTPError as e:
            logger.debug("github readme fetch error %s: %s", full_name, str(e)[:120])
        data = self._get_json(url)
        if not data:
            return ""
        try:
            return base64.b64decode(data.get("content") or "").decode("utf-8", errors="replace")[
                :MAX_README_CHARS
            ]
        except Exception as e:
            logger.debug("github readme decode failed %s: %s", full_name, str(e)[:120])
            return ""

    def close(self) -> None:
        self._client.close()
