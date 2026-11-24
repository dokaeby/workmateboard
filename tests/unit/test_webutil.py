"""webutil.is_safe_fetch_url SSRF 가드 테스트.

DNS 이름 경로는 socket.getaddrinfo를 monkeypatch해 해석 결과를 제어한다.
"""

import socket

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
