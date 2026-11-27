"""외부 HTTP fetch 공용 유틸 — SSRF 가드, 크기 제한 fetch, JSON dot-path 추출.

대시보드 카드/커넥터처럼 사용자 지정 URL을 서버가 대신 호출하는 경로에서
localhost/사설 IP 리터럴 접근을 차단한다. DNS 이름은 getaddrinfo로 해석해
결과 IP 전부가 공인인지 검사한다(localtest.me/nip.io/lvh.me 같은
공개 DNS 이름의 사설 IP 해석을 차단). DNS 해석은 OS resolver에 위임되며
getaddrinfo 자체에 타임아웃을 둘 수 없어, 해석 지연 시 호출이 OS의
resolver 타임아웃까지 블로킹될 수 있다(허용된 트레이드오프).

DNS 리바인딩(TOCTOU) 대응: 검증 시점과 실제 접속 시점의 DNS 해석이
달라지면(getaddrinfo가 매번 다른 IP를 반환하는 공격 도메인) 사설 IP로
연결될 수 있다. 이를 막기 위해 fetch_bytes/fetch_json은 검증을 통과한
호스트에 대해 **검증에 사용한 공인 IP 하나를 connect 대상에 고정**한다
(httpcore NetworkBackend 래퍼). TLS SNI와 Host 헤더는 httpcore가
origin(원래 호스트명) 기준으로 생성하므로 도메인 인증서 검증과
가상호스트 라우팅이 그대로 유지된다.
"""

from __future__ import annotations

import ipaddress
import json
import socket
import time
from typing import Any
from urllib.parse import urlparse

import httpcore
import httpx

MAX_FETCH_BYTES = 256 * 1024
DEFAULT_TIMEOUT = 8.0

# SSRF 포트 제한 — 공인 호스트라도 임의 TCP 포트(6379, 8080 등 내부/관리
# 서비스 포트)를 두드리는 것을 차단한다. 카드/커넥터는 일반 웹 리소스만
# 조회하므로 80/443만 허용한다.
_ALLOWED_PORTS = frozenset({80, 443})

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
# host -> (만료 monotonic, 공인 IP 튜플). 빈 튜플 = 검증 실패/사설 해석.
_dns_cache: dict[str, tuple[float, tuple[str, ...]]] = {}


def _resolve_public_ips(host: str) -> tuple[str, ...]:
    """DNS 이름이 공인 IP로만 해석되면 그 IP 문자열 튜플, 아니면 빈 튜플.

    getaddrinfo(host, port, proto=IPPROTO_TCP) 결과의 모든 IP가
    _ip_is_public을 만족해야 반환한다. 하나라도 사설/루프백/링크로컬/
    멀티캐스트/예약/unspecified거나 해석 실패(OSError/gaierror)면 빈
    튜플 — 검증 불가한 이름은 거부한다(명확한 일시적 DNS 장애로 인한
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
        return ()
    ips: list[str] = []
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            ips = []
            break
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            ips = []
            break
        if not _ip_is_public(ip):
            ips = []
            break
        ips.append(str(ip))
    result = tuple(sorted(set(ips)))
    if len(_dns_cache) >= _DNS_CACHE_MAX:
        _dns_cache.clear()
    _dns_cache[host] = (now + _DNS_CACHE_TTL, result)
    return result


def _host_dns_is_public(host: str) -> bool:
    return bool(_resolve_public_ips(host))


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
    try:
        port = parsed.port
    except ValueError:
        return False
    if port is not None and port not in _ALLOWED_PORTS:
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


def is_loopback_hostname(host: str) -> bool:
    """loopback 호스트 판정 — localhost 계열 또는 loopback IP 리터럴."""
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


def validate_outbound_url(url: str, *, what: str = "url") -> str:
    """자격증명이 실리는 아웃바운드 URL 정책 — 정규화된 URL을 반환한다.

    - scheme은 http/https만, userinfo(내장 credential) 금지.
    - **http는 loopback 전용** — 평문 채널로 공인 호스트에 자격증명을
      보내는 것을 금지한다.
    - 비-loopback 호스트는 is_safe_fetch_url(SSRF 가드) 통과 필수.
    위반 시 FetchError(에러 메시지에 URL 원문/자격증명 미포함)."""
    raw = (url or "").strip()
    if not raw:
        raise FetchError(f"{what}_empty")
    if "://" not in raw:
        raw = f"https://{raw}"
    try:
        parsed = urlparse(raw)
    except ValueError as e:
        raise FetchError(f"{what}_invalid") from e
    if parsed.scheme not in ("http", "https"):
        raise FetchError(f"{what}_scheme")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise FetchError(f"{what}_no_host")
    if parsed.username or parsed.password:
        raise FetchError(f"{what}_credentials")
    loopback = is_loopback_hostname(host)
    if parsed.scheme == "http" and not loopback:
        raise FetchError(f"{what}_https_required")
    if not loopback and not is_safe_fetch_url(raw):
        raise FetchError(f"{what}_not_allowed")
    return raw.rstrip("/")


def pinned_transport_for(url: str) -> httpx.HTTPTransport | None:
    """공인 호스트 URL용 DNS-pinned transport를 반환한다.

    loopback·검증 불가 호스트는 None — 호출자가 일반 transport를 쓰면
    된다(루프백은 리바인딩 대상이 아니고, 검증 불가 호스트는
    validate_outbound_url이 이미 거부했어야 한다)."""
    pinned = _pinned_ip_for(url)
    if pinned is None:
        return None
    return _PinnedTransport(pinned)


def assert_safe_fetch_url(url: str) -> None:
    """SSRF 가드 — 안전하지 않으면 FetchError."""
    if not is_safe_fetch_url(url):
        raise FetchError("url_not_allowed")


class _PinnedSyncBackend(httpcore.SyncBackend):
    """connect_tcp의 대상을 검증된 공인 IP로 고정하는 NetworkBackend.

    httpcore는 TLS SNI(server_hostname)와 Host 헤더를 origin(원래 URL의
    호스트명)에서 가져오므로, 여기서 connect 주소만 IP로 바꿔도
    인증서 검증·가상호스트는 원래 도메인 기준으로 동작한다.
    DNS 리바인딩(검증 후 재해석)이 차단된다.
    """

    def __init__(self, pinned_ip: str) -> None:
        self._pinned_ip = pinned_ip

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.NetworkStream:
        return super().connect_tcp(
            self._pinned_ip,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )


class _PinnedTransport(httpx.HTTPTransport):
    """pinned IP로만 연결하는 httpx transport — fetch 1회 = origin 1개."""

    def __init__(self, pinned_ip: str) -> None:
        super().__init__()
        # super().__init__이 만든 기본 pool을 pinned backend pool로 교체.
        # ssl_context는 httpx 기본 생성 결과(verify=True)를 그대로 재사용.
        old_pool = self._pool
        self._pool = httpcore.ConnectionPool(
            ssl_context=old_pool._ssl_context,  # noqa: SLF001 - httpcore 내부 속성
            network_backend=_PinnedSyncBackend(pinned_ip),
        )
        old_pool.close()


def _pinned_ip_for(url: str) -> str | None:
    """검증 통과 URL의 connect 대상이 될 공인 IP 1개를 반환.

    is_safe_fetch_url과 동일한 검증을 거치되, DNS 이름은 검증에 사용한
    해석 결과(캐시된 공인 IP 목록) 중 첫 번째를 고정한다. IP 리터럴은
    그 자체를 반환. 검증 불가/사설이면 None."""
    try:
        parsed = urlparse(url if "://" in url else f"https://{url}")
    except ValueError:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    if port is not None and port not in _ALLOWED_PORTS:
        return None
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return None
    if host in _BLOCKED_HOSTNAMES or host.endswith(_BLOCKED_SUFFIXES):
        return None
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ips = _resolve_public_ips(host)
        return ips[0] if ips else None
    return str(ip) if _ip_is_public(ip) else None


def _read_limited(resp: httpx.Response, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    for chunk in resp.iter_bytes(8192):
        chunks.append(chunk)
        total += len(chunk)
        if total >= max_bytes:
            break
    return b"".join(chunks)[:max_bytes]


def fetch_bytes(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    max_bytes: int = MAX_FETCH_BYTES,
) -> bytes:
    """GET → 본문 bytes (max_bytes에서 절단). 리다이렉트는 따라가지 않는다
    (리다이렉트 대상이 가드를 우회할 수 있으므로).

    DNS 이름은 검증에 사용한 공인 IP로 connect를 고정해 DNS 리바인딩을
    차단한다(_PinnedTransport). 고정에 실패하면 FetchError."""
    assert_safe_fetch_url(url)
    pinned = _pinned_ip_for(url)
    if pinned is None:
        raise FetchError("url_not_allowed")
    try:
        with (
            _PinnedTransport(pinned) as transport,
            httpx.Client(transport=transport, timeout=timeout) as client,
            client.stream("GET", url, headers=headers or {}, follow_redirects=False) as resp,
        ):
            if resp.status_code >= 400:
                raise FetchError(f"http_{resp.status_code}")
            return _read_limited(resp, max_bytes)
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


def _safe_item_url(v: Any) -> str:
    """커넥터 응답의 url 필드 스킴 검증 — http/https 절대 URL과 단일
    슬래시 상대경로만 통과. javascript:/data:/vbscript:/프로토콜 상대
    //host 등은 빈 문자열로 정규화한다(프론트 safeUrl과 중복 방어)."""
    s = str(v or "").strip()
    if not s:
        return ""
    low = s.lower()
    if low.startswith(("http://", "https://")):
        return s
    if s.startswith("//"):
        return ""
    if s.startswith(("/", "./", "../", "#", "?")):
        return s
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
                or "(제목 없음)"
            )
        out.append(
            {
                "id": str(get("id") or ""),
                "title": str(title),
                "url": _safe_item_url(get("url")),
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
