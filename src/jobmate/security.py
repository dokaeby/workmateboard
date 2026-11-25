"""보안 유틸 — API 키 인증 헬퍼 + Fernet 기반 저장 데이터 암호화.

암호화 키 우선순위:
1. JOBMATE_ENCRYPTION_KEY (Fernet 키 또는 임의 문자열 → SHA256 파생)
2. JOBMATE_API_KEY / CRON_SECRET / JOBMATE_CRON_SECRET (파생 재료)
키 자료가 없으면 암호화 없이 평문 저장(접두사 없음) — 로컬 개발 호환.
암호화된 값은 "enc:v1:" 접두사로 표시해 복호화 여부를 구분한다.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
import threading
import time
from typing import Any

logger = logging.getLogger("jobmate.security")

PREFIX = "enc:v1:"
ENCRYPTION_KEY_ENV = "JOBMATE_ENCRYPTION_KEY"
API_KEY_ENV = "JOBMATE_API_KEY"
_CRON_SECRET_ENVS = ("CRON_SECRET", "JOBMATE_CRON_SECRET")


def _key_material() -> str:
    return (
        os.environ.get(ENCRYPTION_KEY_ENV, "")
        or os.environ.get(API_KEY_ENV, "")
        or os.environ.get(_CRON_SECRET_ENVS[0], "")
        or os.environ.get(_CRON_SECRET_ENVS[1], "")
    )


def _fernet() -> Any | None:
    """설정된 키로 Fernet 생성. 유효한 Fernet 키면 그대로, 아니면 SHA256 파생."""
    material = _key_material()
    if not material:
        return None
    from cryptography.fernet import Fernet

    try:
        return Fernet(material.encode())
    except Exception:
        derived = base64.urlsafe_b64encode(hashlib.sha256(material.encode()).digest())
        return Fernet(derived)


def production_mode() -> bool:
    """JOBMATE_MODE=release 또는 VERCEL — 프로덕션 fail-fast 판정.

    Settings.mode는 JOBMATE_MODE env로 설정된다. config.py를 import하지 않고
    env를 직접 읽어 순환 import를 피한다.
    """
    return bool(os.environ.get("VERCEL")) or os.environ.get("JOBMATE_MODE", "") == "release"


def enforce_production_secrets() -> None:
    """프로덕션 모드에서 암호화 키 부재 시 RuntimeError — 기동 시점 검증용.

    키 없이 encrypt_str이 평문을 저장하면 push endpoint·시크릿이 DB에
    그대로 남는다. 로컬 debug 모드는 기존 동작(평문 허용)을 유지한다.
    """
    if not production_mode():
        return
    if not _key_material():
        raise RuntimeError(
            "JOBMATE_ENCRYPTION_KEY(또는 JOBMATE_API_KEY/CRON_SECRET) 미설정 — "
            "프로덕션 모드에서는 저장 데이터 암호화 키가 필수입니다. "
            "키 없이 기동하면 민감값이 평문으로 DB에 저장됩니다."
        )


def encrypt_str(value: str) -> str:
    """평문 → "enc:v1:" + Fernet 토큰. 키 없으면 평문 그대로.

    프로덕션 모드에서 키가 없으면 평문 저장 대신 raise — 기동 검증
    (enforce_production_secrets)을 우회한 경로에서도 시크릿 평문 저장을
    막는 방어선이다.
    """
    if not value:
        return value
    f = _fernet()
    if f is None:
        if production_mode():
            raise RuntimeError(
                "암호화 키 미설정 상태에서 encrypt_str 호출 — JOBMATE_ENCRYPTION_KEY를 설정하세요."
            )
        return value
    return PREFIX + f.encrypt(value.encode()).decode()


def decrypt_str(value: str) -> str:
    """ "enc:v1:" 접두사 값만 복호화. 평문/키 없음/복호화 실패 시 원문 반환."""
    if not value or not value.startswith(PREFIX):
        return value
    f = _fernet()
    if f is None:
        logger.warning("encrypted value found but no key material configured")
        return value
    try:
        from cryptography.fernet import InvalidToken

        return f.decrypt(value[len(PREFIX) :].encode()).decode()
    except InvalidToken:
        logger.warning("failed to decrypt value (wrong key?)")
        return value
    except Exception as e:
        logger.warning("decrypt error: %s", e)
        return value


def api_key() -> str:
    """설정된 API 키. 비어 있으면 인증 비활성(로컬 개발)."""
    return os.environ.get(API_KEY_ENV, "")


def authorized(request: Any) -> bool:
    """Authorization: Bearer <key>, X-API-Key 헤더 검증.

    /api/v1/stream에 한해 ?token= SSE 단기 토큰도 허용한다 —
    EventSource는 커스텀 헤더를 설정할 수 없기 때문. 장기 API 키의
    쿼리스트링 전달(?api_key=)은 브라우저 히스토리/프록시 로그/Referer
    유출 위험이 있어 폐기했다. 접근 로그는 쿼리스트링을 기록하지 않는다."""
    key = api_key()
    if not key:
        return not os.environ.get("VERCEL") and not allowed_emails()
    auth = request.headers.get("authorization", "")
    provided = request.headers.get("x-api-key", "")
    if auth.lower().startswith("bearer "):
        provided = auth[7:].strip() or provided
    if not provided:
        # EventSource/fetch 폴백 — /api/v1/stream 전용. 단기 수명의
        # SSE 토큰(?token=)만 허용한다. 장기 API 키를 URL에 넣지 않기 위해
        # ?api_key= 쿼리 인증은 폐기했다(브라우저 히스토리/프록시 로그/Referer 유출).
        path = str(getattr(getattr(request, "url", None), "path", "") or "")
        if path.rstrip("/") == "/api/v1/stream":
            qp = getattr(request, "query_params", None)
            if qp is not None:
                try:
                    st = str(qp.get("token") or "")
                except Exception:
                    st = ""
                if st and valid_stream_token(st):
                    return True
    return hmac.compare_digest(provided.encode(), key.encode())


# ---------- SSE 단기 토큰 ----------
# EventSource는 헤더를 설정할 수 없으므로, 인증된 클라이언트가
# POST /api/v1/stream/token으로 짧은 수명의 토큰을 발급받아
# ?token= 쿼리로 사용한다. 프로세스 메모리 저장 — TTL 만료 시 자동 폐기.

STREAM_TOKEN_TTL_SEC = 300
_STREAM_TOKEN_PREFIX = "sse_"  # noqa: S105 - 토큰이 아닌 형식 접두사 상수
_stream_tokens: dict[str, float] = {}  # sha256(token) -> expiry epoch
_stream_tokens_lock = threading.Lock()


def issue_stream_token() -> tuple[str, int]:
    """입력: 없음 / 출력: (토큰, TTL 초).

    동작: 256bit 랜덤 토큰 발급, 해시만 메모리에 저장(평문 미보관),
    만료 항목을 lazy 정리한다.
    """
    token = _STREAM_TOKEN_PREFIX + secrets.token_urlsafe(32)
    expiry = time.time() + STREAM_TOKEN_TTL_SEC
    digest = hashlib.sha256(token.encode()).hexdigest()
    now = time.time()
    with _stream_tokens_lock:
        for k in [k for k, exp in _stream_tokens.items() if exp <= now]:
            del _stream_tokens[k]
        _stream_tokens[digest] = expiry
    return token, STREAM_TOKEN_TTL_SEC


def valid_stream_token(token: str) -> bool:
    """입력: ?token= 쿼리 값 / 출력: 유효 여부.

    동작: sha256 해시 조회 + 만료 검사. 잘못된 형식/만료/미등록은 False.
    """
    if not token.startswith(_STREAM_TOKEN_PREFIX):
        return False
    digest = hashlib.sha256(token.encode()).hexdigest()
    with _stream_tokens_lock:
        expiry = _stream_tokens.get(digest)
        if expiry is None:
            return False
        if expiry <= time.time():
            del _stream_tokens[digest]
            return False
    return True


# ---------- 계정 인증 (세션 쿠키 + 이메일/Google 로그인) ----------

SESSION_COOKIE = "jobmate_session"
OAUTH_STATE_COOKIE = "jobmate_oauth_state"
SESSION_TTL_DAYS = 30
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1


def hash_password(password: str) -> str:
    """scrypt 해시 → 'scrypt$N$r$p$<salt_b64>$<hash_b64>' 저장 형식."""
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return (
        f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}$"
        f"{base64.urlsafe_b64encode(salt).decode()}${base64.urlsafe_b64encode(dk).decode()}"
    )


def verify_password(password: str, stored: str) -> bool:
    """저장된 scrypt 해시와 평문 비교. 형식 불일치/실패 시 False."""
    try:
        scheme, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt = base64.urlsafe_b64decode(salt_b64)
        expected = base64.urlsafe_b64decode(hash_b64)
        dk = hashlib.scrypt(
            password.encode(),
            salt=salt,
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False


def new_session_token() -> str:
    """세션 토큰 — URL-safe 랜덤 256bit."""
    import secrets

    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """세션 토큰의 저장용 SHA-256 — DB 유출 시 토큰 원문 복구 불가."""
    return hashlib.sha256(token.encode()).hexdigest()


def allowed_emails() -> set[str]:
    """JOBMATE_ALLOWED_EMAILS(쉼표 구분)가 설정되면 해당 이메일만 가입/로그인 허용."""
    raw = os.environ.get("JOBMATE_ALLOWED_EMAILS", "")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def email_allowed(email: str) -> bool:
    allowed = allowed_emails()
    return not allowed or email.strip().lower() in allowed


def admin_emails() -> set[str]:
    """JOBMATE_ADMIN_EMAILS(쉼표 구분) — 이 목록의 계정은 role과 무관하게 admin."""
    raw = os.environ.get("JOBMATE_ADMIN_EMAILS", "")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def lead_emails() -> set[str]:
    """JOBMATE_LEAD_EMAILS(쉼표 구분) — 이 목록의 계정은 role과 무관하게 lead
    (admin_emails에 있으면 admin이 우선)."""
    raw = os.environ.get("JOBMATE_LEAD_EMAILS", "")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}
