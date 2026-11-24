"""SSE 단기 토큰 발급/검증 단위 테스트."""

from __future__ import annotations

import time

from jobmate.security import issue_stream_token, valid_stream_token


def test_issue_and_validate() -> None:
    token, ttl = issue_stream_token()
    assert token.startswith("sse_")
    assert ttl > 0
    assert valid_stream_token(token)


def test_rejects_wrong_and_malformed() -> None:
    assert not valid_stream_token("sse_nonexistent")
    assert not valid_stream_token("")
    assert not valid_stream_token("not-a-token")
    # API 키 형태의 값은 sse_ 접두사가 없어 거부
    assert not valid_stream_token("stream-secret")


def test_expired_token_rejected(monkeypatch) -> None:
    import jobmate.security as sec

    token, _ = issue_stream_token()
    assert valid_stream_token(token)
    # 저장된 만료 시각을 과거로 조작
    digest = __import__("hashlib").sha256(token.encode()).hexdigest()
    with sec._stream_tokens_lock:
        sec._stream_tokens[digest] = time.time() - 1
    assert not valid_stream_token(token)
