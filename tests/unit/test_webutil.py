"""webutil.is_safe_fetch_url SSRF 가드 테스트.

DNS 이름 경로는 socket.getaddrinfo를 monkeypatch해 해석 결과를 제어한다.
"""

import socket

import httpcore
import pytest

from jobmate import webutil


def _gai(*ips: str):
    """getaddrinfo 반환값 fake — 각 IP 문자열에 대한 sockaddr 튜플 생성."""

    def fake(host, port, proto=0, *args, **kwargs):
        out = []
        for ip in ips:
            family = socket.AF_INET6 if ":" in ip else socket.AF_INET
            sockaddr = (ip, port, 0, 0) if family == socket.AF_INET6 else (ip, port)
            out.append((family, socket.SOCK_STREAM, proto, "", sockaddr))
        return out

    return fake


@pytest.fixture(autouse=True)
def _clear_dns_cache():
    webutil._dns_cache.clear()
    yield
    webutil._dns_cache.clear()


# --- IP 리터럴 회귀 ---


@pytest.mark.parametrize(
    "url",
    [
        "http://8.8.8.8/x",
        "https://1.1.1.1",
        "http://[2606:4700:4700::1111]/",
    ],
)
def test_public_ip_literal_allowed(url):
    assert webutil.is_safe_fetch_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://172.16.0.1/",
        "http://169.254.169.254/latest/meta-data",  # cloud metadata link-local
        "http://0.0.0.0/",
        "http://[::1]/",
        "http://[fd00::1]/",
        "http://[fe80::1]/",
    ],
)
def test_private_ip_literal_blocked(url):
    assert webutil.is_safe_fetch_url(url) is False


# --- 포트 제한 (SSRF — 공인 IP의 임의 TCP 포트 스캔 차단) ---


@pytest.mark.parametrize(
    "url",
    [
        "http://8.8.8.8:6379/",  # redis
        "http://8.8.8.8:8080/",
        "http://8.8.8.8:22/",
        "https://1.1.1.1:8443/",
        "http://8.8.8.8:99999/",  # 범위 밖 포트 → 파싱 실패
    ],
)
def test_nonstandard_port_blocked(url):
    assert webutil.is_safe_fetch_url(url) is False


@pytest.mark.parametrize(
    "url",
    [
        "http://8.8.8.8/",
        "http://8.8.8.8:80/",
        "https://1.1.1.1:443/",
        "https://example.com:443/x",
    ],
)
def test_standard_port_allowed(url, monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _gai("93.184.216.34"))
    assert webutil.is_safe_fetch_url(url) is True


def test_pinned_ip_for_blocks_nonstandard_port():
    assert webutil._pinned_ip_for("http://8.8.8.8:6379/") is None
    assert webutil._pinned_ip_for("http://8.8.8.8/") == "8.8.8.8"


# --- 스킴/호스트 기본 가드 (DNS 해석 없이 차단되는 경로) ---


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/",
        "file:///etc/passwd",
        "http://localhost/",
        "http://foo.local/",
        "http://svc.internal/",
        "http://x.home.arpa/",
        "",
    ],
)
def test_scheme_and_blocked_hosts(url):
    assert webutil.is_safe_fetch_url(url) is False


# --- DNS 이름 해석 경로 ---


def test_dns_public_ip_allowed(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _gai("93.184.216.34"))
    assert webutil.is_safe_fetch_url("https://example.com/x") is True


def test_dns_private_ip_blocked(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _gai("127.0.0.1"))
    assert webutil.is_safe_fetch_url("http://localtest.me/") is False


def test_dns_mixed_public_private_blocked(monkeypatch):
    """하나라도 사설이면 거부 — public+private 혼합도 차단."""
    monkeypatch.setattr(socket, "getaddrinfo", _gai("93.184.216.34", "10.1.2.3"))
    assert webutil.is_safe_fetch_url("http://evil.example.com/") is False


def test_dns_ipv6_private_blocked(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _gai("2606:4700:4700::1111", "::1"))
    assert webutil.is_safe_fetch_url("http://dual.example.com/") is False


def test_dns_resolution_failure_denied(monkeypatch):
    """gaierror → 검증 불가이므로 거부."""

    def boom(*a, **kw):
        raise socket.gaierror("name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", boom)
    assert webutil.is_safe_fetch_url("https://nonexistent.invalid/") is False


def test_dns_cache_reuses_result(monkeypatch):
    calls = []

    def fake(host, port, proto=0, *a, **kw):
        calls.append(host)
        return _gai("93.184.216.34")(host, port, proto, *a, **kw)

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    assert webutil.is_safe_fetch_url("https://cached.example.com/") is True
    assert webutil.is_safe_fetch_url("https://cached.example.com/") is True
    assert calls == ["cached.example.com"]


# --- DNS 리바인딩(TOCTOU) 회귀 ---


def test_pinned_ip_uses_validated_resolution(monkeypatch):
    """검증에 사용한 공인 IP가 connect 고정 대상이 된다.

    getaddrinfo가 호출마다 다른 IP를 반환하는 리바인딩 도메인을 흉내낸다 —
    is_safe_fetch_url 검증은 공인 IP를 보고 통과하지만, 실제 접속은
    httpx의 재해석이 아니라 검증된 IP로 고정되어야 한다."""
    resolutions = iter(["93.184.216.34", "127.0.0.1"])

    def fake(host, port, proto=0, *a, **kw):
        ip = next(resolutions, "127.0.0.1")
        return _gai(ip)(host, port, proto, *a, **kw)

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    assert webutil.is_safe_fetch_url("http://rebind.example/x") is True
    # pinned는 검증 시 해석된 공인 IP — 두 번째(사설) 해석과 무관
    assert webutil._pinned_ip_for("http://rebind.example/x") == "93.184.216.34"


def test_pinned_backend_connects_to_validated_ip(monkeypatch):
    """_PinnedSyncBackend.connect_tcp는 원래 호스트명이 아니라
    고정된 공인 IP로 소켓을 연다(DNS 재해석 없음)."""
    seen = {}

    def fake_connect(address, timeout=None, source_address=None):
        seen["address"] = address
        raise OSError("no network in test")

    monkeypatch.setattr(socket, "create_connection", fake_connect)
    backend = webutil._PinnedSyncBackend("93.184.216.34")
    with pytest.raises(httpcore.ConnectError):
        backend.connect_tcp("rebind.example", 8080, timeout=1)
    assert seen["address"] == ("93.184.216.34", 8080)


def test_pinned_ip_for_blocks_private_and_literal_private(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _gai("10.0.0.1"))
    assert webutil._pinned_ip_for("http://internal.example/") is None
    assert webutil._pinned_ip_for("http://192.168.0.1/") is None
    assert webutil._pinned_ip_for("http://8.8.8.8/") == "8.8.8.8"


def test_fetch_bytes_rejects_unresolvable_after_check(monkeypatch):
    """assert는 통과해도 pinned IP를 얻지 못하면 FetchError."""
    # IP 리터럴 공인은 pinned가 항상 있으므로, 사설 리터럴로 검증 단계 차단 확인
    with pytest.raises(webutil.FetchError):
        webutil.fetch_bytes("http://127.0.0.1/", timeout=1)


# --- normalize_items url 스킴 검증 ---


@pytest.mark.parametrize(
    "bad",
    [
        "javascript:alert(1)",
        "JaVaScRiPt:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "vbscript:msgbox(1)",
        "//evil.example.com/x",  # 프로토콜 상대
        "ftp://example.com/x",
        "example.com/no-scheme",
    ],
)
def test_normalize_items_blocks_unsafe_url(bad):
    out = webutil.normalize_items([{"title": "t", "html_url": bad}])
    assert out[0]["url"] == ""


@pytest.mark.parametrize(
    "good",
    [
        "https://github.com/o/r/issues/1",
        "http://example.com/x",
        "/relative/path",
        "./rel",
        "../up",
        "#anchor",
        "?q=1",
    ],
)
def test_normalize_items_keeps_safe_url(good):
    out = webutil.normalize_items([{"title": "t", "url": good}])
    assert out[0]["url"] == good
