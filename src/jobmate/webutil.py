"""외부 HTTP fetch 공용 유틸 — SSRF 가드, 크기 제한 fetch, JSON dot-path 추출.

대시보드 카드/커넥터처럼 사용자 지정 URL을 서버가 대신 호출하는 경로에서
localhost/사설 IP 리터럴 접근을 차단한다. DNS 이름은 getaddrinfo로 해석해
결과 IP 전부가 공인인지 검사한다(localtest.me/nip.io/lvh.me 같은
공개 DNS 이름의 사설 IP 해석을 차단). DNS 해석은 OS resolver에 위임되며
getaddrinfo 자체에 타임아웃을 둘 수 없어, 해석 지연 시 호출이 OS의
resolver 타임아웃까지 블로킹될 수 있다(허용된 트레이드오프).
"""

from __future__ import annotations

import ipaddress
import json
import socket
import time
from typing import Any
from urllib.parse import urlparse

import httpx

MAX_FETCH_BYTES = 256 * 1024
DEFAULT_TIMEOUT = 8.0

_BLOCKED_HOSTNAMES = {
    "localhost",
    "localhost.localdomain",
    "metadata.google.internal",
    "instance-data",
}
_BLOCKED_SUFFIXES = (".local", ".internal", ".localhost", ".home.arpa")


class FetchError(Exception):
    """외부 fetch 실패 — 네트워크/HTTP/파싱 오류를 하나로 묶는다."""


def _ip_is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


_DNS_CACHE_TTL = 60.0
_DNS_CACHE_MAX = 512
_dns_cache: dict[str, tuple[float, bool]] = {}


def _host_dns_is_public(host: str) -> bool:
    """DNS 이름이 공인 IP로만 해석되는지 검사.

    getaddrinfo(host, port, proto=IPPROTO_TCP) 결과의 모든 IP가
    _ip_is_public을 만족해야 True. 하나라도 사설/루프백/링크로컬/
    멀티캐스트/예약/unspecified면 False. 해석 실패(OSError/gaierror)도
    False — 검증 불가한 이름은 거부한다(명확한 일시적 DNS 장애로 인한
    false positive는 허용 범위).

    해석은 OS resolver에 위임 — getaddrinfo에 적용 가능한 타임아웃이
    없어 느린 DNS가 호출을 블로킹할 수 있다(현재 트레이드오프로 허용).
    결과는 60초 TTL 캐시로 반복 조회 비용을 줄인다.
    """
    now = time.monotonic()
    cached = _dns_cache.get(host)
    if cached is not None and cached[0] > now:
        return cached[1]
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError:
        return False
    allowed = True
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            allowed = False
            break
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            allowed = False
            break
        if not _ip_is_public(ip):
            allowed = False
            break
    if len(_dns_cache) >= _DNS_CACHE_MAX:
        _dns_cache.clear()
    _dns_cache[host] = (now + _DNS_CACHE_TTL, allowed)
    return allowed


def is_safe_fetch_url(url: str) -> bool:
    """http/https + 공인 호스트만 허용.

    IP 리터럴은 직접 _ip_is_public으로 검사하고, DNS 이름은
    _host_dns_is_public으로 해석 결과 전부가 공인인지 확인한다."""
    try:
        parsed = urlparse(url if "://" in url else f"https://{url}")
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return False
    if host in _BLOCKED_HOSTNAMES or host.endswith(_BLOCKED_SUFFIXES):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return _host_dns_is_public(host)
    return _ip_is_public(ip)


def assert_safe_fetch_url(url: str) -> None:
    """SSRF 가드 — 안전하지 않으면 FetchError."""
    if not is_safe_fetch_url(url):
        raise FetchError("url_not_allowed")


def fetch_bytes(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    max_bytes: int = MAX_FETCH_BYTES,
) -> bytes:
    """GET → 본문 bytes (max_bytes에서 절단). 리다이렉트는 따라가지 않는다
    (리다이렉트 대상이 가드를 우회할 수 있으므로)."""
    assert_safe_fetch_url(url)
    try:
        with httpx.stream(
            "GET", url, headers=headers or {}, timeout=timeout, follow_redirects=False
        ) as resp:
            if resp.status_code >= 400:
                raise FetchError(f"http_{resp.status_code}")
            chunks: list[bytes] = []
            total = 0
            for chunk in resp.iter_bytes(8192):
                chunks.append(chunk)
                total += len(chunk)
                if total >= max_bytes:
                    break
            return b"".join(chunks)[:max_bytes]
    except FetchError:
        raise
    except httpx.HTTPError as e:
        raise FetchError(type(e).__name__) from e


def fetch_json(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    max_bytes: int = MAX_FETCH_BYTES,
) -> Any:
    """GET → JSON 파싱 결과. 파싱 실패 시 FetchError."""
    body = fetch_bytes(url, headers=headers, timeout=timeout, max_bytes=max_bytes)
    try:
        return json.loads(body.decode("utf-8", errors="replace"))
    except (ValueError, UnicodeDecodeError) as e:
        raise FetchError("invalid_json") from e


def dig_path(data: Any, path: str) -> Any:
    """dot-path 탐색 — dict 키와 list 인덱스("items.0.name") 지원.
    path가 비어 있으면 data 그대로."""
    cur = data
    if not path:
        return cur
    for part in str(path).split("."):
        if part == "":
            continue
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit():
            idx = int(part)
            cur = cur[idx] if idx < len(cur) else None
        else:
            return None
        if cur is None:
            return None
    return cur


def extract_json_path(data: Any, path: str) -> Any:
    """json_path 추출 — 빈 path는 본문 전체."""
    return dig_path(data, path)


def find_item_list(data: Any, path: str = "") -> list[Any]:
    """응답에서 아이템 리스트 탐색 — path 지정 시 해당 위치, 아니면
    첫 번째 list 값을 가진 흔한 키(items/results/data/issues/value)를 찾는다."""
    if path:
        v = dig_path(data, path)
        return v if isinstance(v, list) else []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("items", "results", "data", "issues", "value", "nodes"):
            v = data.get(key)
            if isinstance(v, list):
                return v
        for v in data.values():
            if isinstance(v, list):
                return v
    return []


def _first(d: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return ""


def normalize_items(
    raw_items: list[Any], fields: dict[str, str] | None = None
) -> list[dict[str, Any]]:
    """외부 API 아이템 → {id,title,url,state,updated_at,author} 정규화.

    fields: {출력키: dot-path} 매핑(선택). 없으면 흔한 키 이름으로 추론한다."""
    out: list[dict[str, Any]] = []
    for it in raw_items[:200]:
        if not isinstance(it, dict):
            continue
        if fields:
            get = lambda k, _it=it: (  # noqa: E731
                dig_path(_it, fields[k]) if fields.get(k) else ""
            )
        else:
            get = lambda k, _it=it: next(  # noqa: E731
                (
                    v
                    for v in (dig_path(_it, c) for c in _FIELD_CANDIDATES[k])
                    if v not in (None, "")
                ),
                "",
            )
        author = get("author")
        if isinstance(author, dict):
            author = _first(author, "login", "name", "displayName", "email") or author.get(
                "username", ""
            )
        title = get("title")
        if not title:
            title = (
                dig_path(it, "name")
                or dig_path(it, "summary")
                or dig_path(it, "key")
                or "(untitled)"
            )
        out.append(
            {
                "id": str(get("id") or ""),
                "title": str(title),
                "url": str(get("url") or ""),
                "state": str(get("state") or ""),
                "updated_at": str(get("updated_at") or ""),
                "author": str(author or ""),
            }
        )
    return out


_FIELD_CANDIDATES: dict[str, tuple[str, ...]] = {
    "id": ("id", "number", "key", "identifier"),
    "title": ("title", "name", "summary"),
    "url": ("html_url", "url", "link", "webUrl", "self"),
    "state": ("state", "status", "state.name"),
    "updated_at": ("updated_at", "updatedAt", "updated", "last_edited_time", "modified"),
    "author": ("author.login", "user.login", "author.name", "creator.displayName", "author"),
}


def host_of(url: str) -> str:
    """로깅용 호스트 추출 — 실패 시 빈 문자열."""
    try:
        return (urlparse(url).hostname or "")[:80]
    except ValueError:
        return ""


def resolve_host(host: str) -> str:
    """테스트/진단용 DNS 해석 — 실패 시 원문 반환."""
    try:
        return socket.gethostbyname(host)
    except OSError:
        return host
