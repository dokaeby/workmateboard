"""Auth routes — extracted from api/app.py create_app().

Handlers are byte-for-byte copies; create_app locals arrive via `ctx`
(SimpleNamespace) and security helpers via module imports.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote, urlencode

import httpx
from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ...security import (
    MFA_PENDING_COOKIE,
    OAUTH_STATE_COOKIE,
    SESSION_COOKIE,
    allowed_emails,
    api_key,
    authorized,
    decrypt_str,
    email_allowed,
    encrypt_str,
    hash_password,
    hash_token,
    new_api_key,
    new_session_token,
    password_min_len,
    password_pwned,
    totp_secret,
    totp_verify_step,
    validate_password,
    verify_password,
)

logger = logging.getLogger("jobmate.api")


def register(app: FastAPI, ctx: Any) -> None:
    _EMAIL_RE = ctx._EMAIL_RE
    _VERIFY_RESEND_MIN_INTERVAL = ctx._VERIFY_RESEND_MIN_INTERVAL
    _VERIFY_OK_HTML = ctx._VERIFY_OK_HTML
    _VERIFY_FAIL_HTML = ctx._VERIFY_FAIL_HTML
    _cookie_secure = ctx._cookie_secure
    _dummy_password_hash = ctx._dummy_password_hash
    _effective_role = ctx._effective_role
    _admin_user = ctx._admin_user
    _email_verification_required = ctx._email_verification_required
    _google_oauth_env = ctx._google_oauth_env
    _google_redirect_uri = ctx._google_redirect_uri
    _login_key = ctx._login_key
    _login_record = ctx._login_record
    _login_throttled = ctx._login_throttled
    _mfa_challenge_page = ctx._mfa_challenge_page
    _mfa_record = ctx._mfa_record
    _mfa_throttled = ctx._mfa_throttled
    _notify_new_device = ctx._notify_new_device
    _db_api_key = ctx._db_api_key
    _API_KEY_SCOPES = ctx._API_KEY_SCOPES
    _rate_hit = ctx._rate_hit
    _maybe_first_user_admin = ctx._maybe_first_user_admin
    _pub_user = ctx._pub_user
    _remote_open_mode = ctx._remote_open_mode
    _send_verification = ctx._send_verification
    _session_user = ctx._session_user
    _set_session = ctx._set_session
    _RESET_RESEND_MIN_INTERVAL = ctx._RESET_RESEND_MIN_INTERVAL
    _reset_page_html = ctx._reset_page_html
    _reset_record = ctx._reset_record
    _reset_throttled = ctx._reset_throttled
    _deliver_password_reset = ctx._deliver_password_reset
    _issue_password_reset = ctx._issue_password_reset
    _signup_record = ctx._signup_record
    _signup_throttled = ctx._signup_throttled
    err = ctx.err
    store = ctx.store

    def _reject_weak_password(request: Request, password: str, email: str) -> JSONResponse | None:
        """비밀번호 정책 위반 시 400 응답, 통과 시 None.
        validate_password(길이/denylist/이메일 포함/단순 패턴) +
        HIBP k-anonymity 유출 검사(JOBMATE_PWNED_CHECK=1일 때만 네트워크 호출)."""
        reason = validate_password(password, email)
        if reason == "too_short":
            return err(
                400,
                "weak_password",
                f"비밀번호는 {password_min_len()}자 이상이어야 합니다",
                request,
            )
        if reason is None and password_pwned(password):
            reason = "pwned_password"
        if reason is None:
            return None
        msg = {
            "common_password": "너무 흔한 비밀번호는 사용할 수 없습니다",
            "contains_email": "비밀번호에 이메일을 포함할 수 없습니다",
            "numeric_only": "숫자만으로 된 비밀번호는 사용할 수 없습니다",
            "repeated_chars": "동일 문자 반복 비밀번호는 사용할 수 없습니다",
            "sequential_chars": "연속된 문자/숫자 비밀번호는 사용할 수 없습니다",
            "pwned_password": "유출된 비밀번호는 사용할 수 없습니다. 다른 비밀번호를 선택하세요",
        }.get(reason, "비밀번호 정책을 만족하지 않습니다")
        # spec 002 US3 AS1 — 거부 사유를 감사에 남긴다(비밀번호 원문 금지)
        try:
            store.audit(
                "",
                (email or "-").strip().lower() or "-",
                "auth_password_rejected",
                "-",
                {"reason": reason, "path": request.url.path},
            )
        except Exception:
            logger.debug("weak-password audit failed", exc_info=True)
        return err(400, "weak_password", msg, request)

    @app.post("/api/v1/auth/signup")
    def auth_signup(body: dict[str, Any], request: Request) -> JSONResponse:
        # 인증 비활성 환경의 원격 요청 — 세션 발급 경로를 닫아 무키 배포가
        # 외부에 노출돼도 member API까지 열리지 않게 한다(loopback 전용).
        if _remote_open_mode(request):
            return err(403, "forbidden", "이 인스턴스는 로컬 전용입니다", request)
        # 허용 목록이 설정된 경우에만 비밀번호 가입을 제한한다 — 비밀번호는
        # 이메일 소유권을 증명할 수 없어 허용 목록 이메일을 임의로 선점할 수 있다.
        if allowed_emails():
            if not api_key() or not authorized(request):
                logger.warning("signup rejected: administrator API key required")
                return err(403, "signup_restricted", "관리자 API 키가 필요합니다", request)
        # IP 기준 가입 시도 throttle — 계정 대량 생성/메일 발송 폭주 방지.
        # 성공 여부와 무관하게 시도 자체를 센다(login 실패 카운터와 별개).
        ip = request.client.host if request.client else "-"
        if _signup_throttled(ip):
            store.audit("", "-", "auth_signup_throttled", "", {})
            logger.warning("signup throttled: ip=%s", ip)
            return err(
                429,
                "too_many_attempts",
                "가입 시도 횟수가 너무 많습니다. 잠시 후 다시 시도하세요",
                request,
            )
        _signup_record(ip)
        email = str(body.get("email", "")).strip().lower()
        password = str(body.get("password", ""))
        name = str(body.get("display_name", "")).strip()[:80]
        if not _EMAIL_RE.match(email):
            return err(400, "invalid_email", "이메일 형식이 올바르지 않습니다", request)
        weak = _reject_weak_password(request, password, email)
        if weak is not None:
            return weak
        if not email_allowed(email):
            logger.warning("signup denied (not allowed): %s", email)
            return err(403, "email_not_allowed", "허용되지 않은 이메일입니다", request)
        # 이용약관 동의 — 인증 필요 모드(실배포)에서만 필수.
        # 로컬 개발 모드(api_key·allowed_emails·VERCEL 모두 없음)는 면제.
        terms_agreed = body.get("agree_terms") is True
        if _email_verification_required() and not terms_agreed:
            return err(
                422,
                "terms_required",
                "이용약관 및 개인정보처리방침에 동의해야 가입할 수 있습니다",
                request,
            )
        user = store.create_user(email, hash_password(password), name)
        if user is None:
            return err(409, "email_taken", "이미 가입된 이메일입니다", request)
        _maybe_first_user_admin(user, request)
        store.audit("", user["email"], "auth_signup", user["id"], {"terms_agreed": terms_agreed})
        logger.info("auth signup: user=%s", user["id"])
        verify_required = _email_verification_required()
        verify_sent = True
        if verify_required:
            # 비밀번호는 이메일 소유권을 증명하지 못하므로 인증 전까지 로그인 불가
            verify_sent = _send_verification(request, email)
        resp = JSONResponse(
            {
                "user": _pub_user(user),
                "verify_email_sent": verify_sent,
                "email_verification_pending": verify_required,
            },
            status_code=201,
        )
        if not verify_required:
            _set_session(resp, request, user["id"])
        return resp

    @app.post("/api/v1/auth/login")
    def auth_login(
        body: dict[str, Any], request: Request, background_tasks: BackgroundTasks
    ) -> JSONResponse:
        if _remote_open_mode(request):
            return err(403, "forbidden", "이 인스턴스는 로컬 전용입니다", request)
        email = str(body.get("email", "")).strip().lower()
        password = str(body.get("password", ""))
        key = _login_key(request, email)
        if _login_throttled(key):
            logger.warning("login throttled: %s", key)
            return err(
                429,
                "too_many_attempts",
                "시도 횟수가 너무 많습니다. 잠시 후 다시 시도하세요",
                request,
            )
        row = store.get_user_password_hash(email)
        # 미가입/Google-only 계정도 동일 비용의 scrypt 검증을 1회 수행 —
        # 응답 시간으로 계정 유형(미가입 vs 비밀번호 계정)을 열거하지 못하게 한다.
        stored_hash = row[1] if row and row[1] else _dummy_password_hash()
        password_ok = verify_password(password, stored_hash)
        if row is None or not row[1] or not password_ok:
            _login_record(key, ok=False)
            store.audit("", email or "-", "auth_login_failed", "", {})
            logger.info("login failed: %s", email)
            return err(
                401, "invalid_credentials", "이메일 또는 비밀번호가 올바르지 않습니다", request
            )
        if not email_allowed(email):
            return err(403, "email_not_allowed", "허용되지 않은 이메일입니다", request)
        user = store.get_user_by_id(row[0])
        if _email_verification_required() and user is not None and not user.get("email_verified"):
            # 비밀번호는 맞지만 이메일 미인증 — 실패 카운트는 비밀번호 오류에만 적용
            _login_record(key, ok=True)
            store.audit("", email, "auth_login_unverified", row[0], {})
            logger.info("login blocked (email unverified): user=%s", row[0])
            return err(
                403,
                "email_not_verified",
                "이메일 인증이 필요합니다. 가입 시 발송된 인증 메일을 확인하거나 재발송하세요",
                request,
            )
        _login_record(key, ok=True)
        mfa = store.get_mfa_secret(row[0])
        if mfa is not None and mfa.get("status") == "active":
            # 비밀번호 계정 + MFA 활성 — 세션 대신 5분·1회성 챌린지 티켓 발급.
            # Google 로그인은 Google 측 인증에 위임하므로 이 경로를 타지 않는다.
            ticket = store.create_mfa_ticket(row[0])
            store.audit("", email, "auth_login_mfa_challenge", row[0], {})
            logger.info("login mfa challenge: user=%s", row[0])
            return JSONResponse({"mfa_required": True, "mfa_ticket": ticket})
        store.audit("", email, "auth_login", row[0], {})
        logger.info("auth login: user=%s", row[0])
        resp = JSONResponse({"user": _pub_user(user) if user else {}})
        _set_session(resp, request, row[0])
        if user is not None:
            _notify_new_device(request, user, background_tasks)
        return resp

    @app.post("/api/v1/auth/verify/resend")
    def auth_verify_resend(body: dict[str, Any], request: Request) -> JSONResponse:
        """미인증 계정의 인증 메일 재발송 — 같은 이메일 10분에 1회(rate limit).

        /api/v1/auth/ prefix는 미들웨어 인증 예외이므로 남용 방지로
        미가입/이미 인증 이메일에도 동일한 응답 형태를 반환한다(계정 열거 방지)."""
        if _remote_open_mode(request):
            return err(403, "forbidden", "이 인스턴스는 로컬 전용입니다", request)
        email = str(body.get("email", "")).strip().lower()
        if not _EMAIL_RE.match(email):
            return err(400, "invalid_email", "이메일 형식이 올바르지 않습니다", request)
        user = store.get_user_by_email(email)
        if user is None or user.get("email_verified"):
            return JSONResponse({"sent": False})
        last = store.latest_verification_at(email)
        if last is not None and (datetime.now(UTC) - last).total_seconds() < (
            _VERIFY_RESEND_MIN_INTERVAL
        ):
            store.audit("", email, "auth_verify_resend_throttled", user["id"], {})
            logger.info("verification resend throttled: user=%s", user["id"])
            return err(
                429,
                "resend_too_soon",
                "인증 메일은 10분에 한 번만 재발송할 수 있습니다",
                request,
            )
        sent = _send_verification(request, email)
        store.audit("", email, "auth_verify_resend", user["id"], {"sent": sent})
        logger.info("verification resend: user=%s sent=%s", user["id"], sent)
        return JSONResponse({"sent": sent})

    @app.get("/api/v1/auth/verify")
    def auth_verify(request: Request, token: str = "") -> Any:
        """인증 링크 — 유효 토큰이면 email_verified=1 + 세션 쿠키 발급.

        consume은 UPDATE ... WHERE used_at IS NULL AND expires_at>? 의 원자적
        rowcount 판정 — 동시 요청이 같은 토큰을 둘 다 유효하다고 보지 않는다."""
        # 무키 원격 오픈 모드에서는 토큰 소비·인증 완료 자체를 차단한다 —
        # signup/login이 원격에서 막혀 있으므로 정상 원격 사용자는 존재하지 않는다.
        if _remote_open_mode(request):
            return err(403, "forbidden", "이 인스턴스는 로컬 전용입니다", request)
        token_hash = hash_token(token) if token else ""
        email = store.consume_email_verification(token_hash) if token_hash else None
        if email is None:
            # 토큰 원문은 기록하지 않는다 — 실패 시도만 감사 로그에 남긴다
            store.audit("", "-", "auth_email_verify_failed", "", {"reason": "invalid_or_expired"})
            logger.info("email verify failed: invalid_or_expired token")
            return HTMLResponse(_VERIFY_FAIL_HTML, status_code=400)
        user = store.get_user_by_email(email)
        if user is None:
            store.audit("", email, "auth_email_verify_failed", "", {"reason": "user_missing"})
            return HTMLResponse(_VERIFY_FAIL_HTML, status_code=400)
        store.set_user_email_verified(user["id"])
        store.audit("", email, "auth_email_verified", user["id"], {})
        logger.info("email verified: user=%s", user["id"])
        resp = HTMLResponse(_VERIFY_OK_HTML)
        _set_session(resp, request, user["id"])
        return resp

    @app.post("/api/v1/auth/password/forgot")
    def auth_password_forgot(
        body: dict[str, Any], request: Request, background_tasks: BackgroundTasks
    ) -> JSONResponse:
        """비밀번호 재설정 링크 발송 — 계정 열거 방지를 위해 등록 여부와 무관하게
        항상 {"sent": true}를 반환한다(실제 발송은 비밀번호 계정만).

        재발송 throttle도 동일 응답으로 처리한다 — 429를 반환하면 두 번째 요청의
        상태 코드 차이로 계정 존재 여부가 노출된다. 토큰 생성(고속 DB)만 동기
        수행하고 메일 발송(네트워크 I/O)은 백그라운드로 보내 응답 시간을
        균일하게 유지한다."""
        if _remote_open_mode(request):
            return err(403, "forbidden", "이 인스턴스는 로컬 전용입니다", request)
        ip = request.client.host if request.client else "-"
        if _reset_throttled(ip):
            store.audit("", "-", "auth_password_reset_throttled", "", {})
            return err(
                429,
                "reset_too_soon",
                "요청이 너무 많습니다. 잠시 후 다시 시도하세요",
                request,
            )
        _reset_record(ip)
        email = str(body.get("email", "")).strip().lower()
        if not _EMAIL_RE.match(email):
            return err(400, "invalid_email", "이메일 형식이 올바르지 않습니다", request)
        row = store.get_user_password_hash(email)
        if row is not None and row[1]:  # 비밀번호 계정만 — Google-only는 발송 안 함
            user = store.get_user_by_id(row[0])
            last = store.latest_password_reset_at(email)
            if (
                user is not None
                and last is not None
                and (datetime.now(UTC) - last).total_seconds() < _RESET_RESEND_MIN_INTERVAL
            ):
                store.audit("", email, "auth_password_reset_throttled", row[0], {})
            elif user is not None:
                issued = _issue_password_reset(request, user)
                if issued is not None:
                    uemail, reset_url, token_hash = issued
                    background_tasks.add_task(
                        _deliver_password_reset, uemail, reset_url, token_hash, row[0]
                    )
        return JSONResponse({"sent": True})

    @app.get("/api/v1/auth/password/reset")
    def auth_password_reset_page(request: Request, token: str = "") -> Any:
        """재설정 링크 랜딩 — 폼만 표시하고 토큰은 소비하지 않는다(소비는 POST)."""
        if _remote_open_mode(request):
            return err(403, "forbidden", "이 인스턴스는 로컬 전용입니다", request)
        return HTMLResponse(_reset_page_html(token))

    @app.post("/api/v1/auth/password/reset")
    def auth_password_reset(body: dict[str, Any], request: Request) -> JSONResponse:
        """토큰 소비 + 비밀번호 교체 + 전체 세션 폐기.

        consume은 UPDATE ... WHERE used_at IS NULL AND expires_at>? 의 원자적
        rowcount 판정 — 동시 요청이 같은 토큰을 둘 다 유효하다고 보지 않는다.
        비밀번호 검증은 consume보다 먼저 — 형식 오류가 토큰을 태우지 않게 한다."""
        if _remote_open_mode(request):
            return err(403, "forbidden", "이 인스턴스는 로컬 전용입니다", request)
        token = str(body.get("token", ""))
        new_password = str(body.get("new_password", ""))
        token_hash = hash_token(token) if token else ""
        # consume 전에 토큰의 계정 이메일을 들여다봐 contains_email 정책을 적용한다
        # — 정책 위반으로 토큰이 소진되는 것을 막는다(peek은 소비하지 않음).
        peeked = store.peek_password_reset(token_hash) if token_hash else None
        weak = _reject_weak_password(request, new_password, peeked[1] if peeked else "")
        if weak is not None:
            return weak
        consumed = store.consume_password_reset(token_hash) if token_hash else None
        if consumed is None:
            store.audit("", "-", "auth_password_reset_failed", "", {"reason": "invalid_or_expired"})
            logger.info("password reset failed: invalid_or_expired token")
            return err(
                400,
                "invalid_or_expired_token",
                "재설정 링크가 만료되었거나 이미 사용되었습니다",
                request,
            )
        user_id, email = consumed
        store.set_user_password(user_id, hash_password(new_password))
        revoked = store.revoke_user_sessions(user_id)
        store.audit(
            "",
            email,
            "auth_password_reset",
            user_id,
            {"sessions_revoked": revoked},
        )
        logger.info("password reset: user=%s sessions_revoked=%d", user_id, revoked)
        return JSONResponse({"status": "password_reset"})

    # ---- MFA/TOTP ----
    _MFA_BACKUP_COUNT = 10

    def _mfa_backup_hash(user_id: str, code: str) -> str:
        """백업 코드 해시 — user_id를 salt로 써 DB 유출 시 오프라인 복구를 어렵게 한다."""
        return hashlib.sha256(f"{user_id}:{str(code).strip().lower()}".encode()).hexdigest()

    def _issue_mfa_backup_codes(user_id: str) -> list[str]:
        """새 백업 코드 세트 발급 — 평문은 이 응답에서 1회만 반환, DB엔 해시만."""
        codes = [secrets.token_hex(5) for _ in range(_MFA_BACKUP_COUNT)]
        store.replace_mfa_backup_codes(user_id, [_mfa_backup_hash(user_id, c) for c in codes])
        return codes

    def _mfa_reauth_ok(user: dict[str, Any], body: dict[str, Any]) -> bool:
        """MFA 변경 작업 재인증 — 현재 비밀번호 또는 유효 TOTP 코드."""
        if _mfa_throttled(user["id"]):
            return False
        password = str(body.get("password", ""))
        row = store.get_user_password_hash(str(user.get("email") or ""))
        ok = bool(password and row is not None and row[1] and verify_password(password, row[1]))
        if not ok:
            code = str(body.get("code", ""))
            sec = store.get_mfa_secret(user["id"])
            if code and sec is not None and sec.get("status") == "active":
                # TOTP 재인증도 verify와 같은 사용자 단위 실패 카운터를 공유한다 —
                # 세션만으로 disable/backup-codes에 무제한 코드 추측이 가능하면 안 된다.
                step = totp_verify_step(decrypt_str(sec["secret_enc"]), code)
                # CAS 선점 — 같은 스텝의 코드 재사용(리플레이)과 동시 요청 경합을
                # 원자적으로 차단한다.
                ok = step is not None and store.mfa_mark_totp_step(user["id"], step)
        # 비밀번호·코드 어느 쪽이든 실제 시도가 있었으면 결과를 카운터에 반영한다 —
        # 비밀번호 경로만 무제한이면 세션 탈취자가 disable을 무차별 대입할 수 있다.
        if password or str(body.get("code", "")):
            _mfa_record(user["id"], ok=ok)
        return ok

    @app.post("/api/v1/auth/mfa/setup")
    def auth_mfa_setup(request: Request) -> JSONResponse:
        """pending TOTP 비밀키 발급 — Fernet 암호화해 저장, otpauth URI 1회 반환."""
        user = _session_user(request)
        if user is None:
            return err(401, "unauthorized", "로그인이 필요합니다", request)
        # setup의 upsert와 confirm의 상태 전이를 같은 임계구역에 둬
        # "pending 확인 중 비밀키가 바뀌는" 레이스를 차단한다.
        with store.exclusive():
            sec = store.get_mfa_secret(user["id"])
            if sec is not None and sec.get("status") == "active":
                return err(409, "mfa_already_enabled", "이미 MFA가 활성화되어 있습니다", request)
            secret = totp_secret()
            store.upsert_mfa_secret(user["id"], encrypt_str(secret))
        email = str(user.get("email") or "")
        otpauth = f"otpauth://totp/jobMate:{quote(email)}?secret={secret}&issuer=jobMate"
        store.audit("", email, "auth_mfa_setup", user["id"], {})
        logger.info("mfa setup: user=%s", user["id"])
        return JSONResponse({"secret": secret, "otpauth_uri": otpauth, "status": "pending"})

    @app.post("/api/v1/auth/mfa/confirm")
    def auth_mfa_confirm(body: dict[str, Any], request: Request) -> JSONResponse:
        """pending 비밀키의 TOTP 코드 1회 검증 → 활성화 + 백업 코드 발급 + 세션 폐기."""
        user = _session_user(request)
        if user is None:
            return err(401, "unauthorized", "로그인이 필요합니다", request)
        if _mfa_throttled(user["id"]):
            return err(
                429,
                "too_many_attempts",
                "시도 횟수가 너무 많습니다. 잠시 후 다시 시도하세요",
                request,
            )
        with store.exclusive():
            sec = store.get_mfa_secret(user["id"])
            if sec is None or sec.get("status") != "pending":
                return err(400, "mfa_not_pending", "확인 대기 중인 MFA 등록이 없습니다", request)
            step = totp_verify_step(decrypt_str(sec["secret_enc"]), str(body.get("code", "")))
            if step is None:
                _mfa_record(user["id"], ok=False)
                store.audit(
                    "",
                    str(user.get("email") or "-"),
                    "auth_mfa_confirm_failed",
                    user["id"],
                    {},
                )
                logger.info("mfa confirm failed: user=%s", user["id"])
                return err(400, "invalid_code", "인증 코드가 올바르지 않습니다", request)
            _mfa_record(user["id"], ok=True)
            # confirm의 코드 사용은 last_totp_step에 기록하지 않는다 — 등록 확인과
            # 로그인 verify는 목적이 달라, 같은 스텝의 코드를 로그인에도 쓸 수 있어야 한다.
            store.confirm_mfa_secret(user["id"])
        codes = _issue_mfa_backup_codes(user["id"])
        revoked = store.revoke_user_sessions(user["id"])
        store.audit(
            "",
            str(user.get("email") or "-"),
            "auth_mfa_enabled",
            user["id"],
            {"backup_codes": len(codes), "sessions_revoked": revoked},
        )
        logger.info("mfa enabled: user=%s sessions_revoked=%d", user["id"], revoked)
        return JSONResponse({"status": "active", "backup_codes": codes})

    @app.post("/api/v1/auth/mfa/verify")
    def auth_mfa_verify(
        body: dict[str, Any], request: Request, background_tasks: BackgroundTasks
    ) -> JSONResponse:
        """MFA 챌린지 완료 — ticket consume(1회성) → TOTP(±1, 리플레이 거부)
        또는 미사용 백업 코드 검증 → 세션 발급.

        ticket을 코드 검증 전에 consume하므로 티켓당 코드 시도는 1회뿐이다 —
        별도 실패 rate limit 없이 무차별 대입이 원천 차단된다(티켓 발급 자체가
        비밀번호 인증을 요구)."""
        if _remote_open_mode(request):
            return err(403, "forbidden", "이 인스턴스는 로컬 전용입니다", request)
        ticket = str(body.get("mfa_ticket", ""))
        ticket_hash = hash_token(ticket) if ticket else ""
        user_id = store.consume_mfa_ticket(ticket_hash) if ticket_hash else None
        if user_id is None:
            store.audit("", "-", "auth_mfa_verify_failed", "", {"reason": "invalid_ticket"})
            return err(
                400,
                "invalid_or_expired_ticket",
                "MFA 인증이 만료되었거나 이미 사용되었습니다. 다시 로그인하세요",
                request,
            )
        if _mfa_throttled(user_id):
            store.audit("", "-", "auth_mfa_verify_throttled", user_id, {})
            logger.warning("mfa verify throttled: user=%s", user_id)
            return err(
                429,
                "too_many_attempts",
                "시도 횟수가 너무 많습니다. 잠시 후 다시 시도하세요",
                request,
            )
        sec = store.get_mfa_secret(user_id)
        code = str(body.get("code", "")).strip()
        ok = False
        if sec is not None and sec.get("status") == "active":
            step = totp_verify_step(decrypt_str(sec["secret_enc"]), code)
            # CAS 선점 — 같은 스텝의 코드 재사용(리플레이)과 동시 verify 경합을
            # 원자적으로 차단한다.
            if step is not None and store.mfa_mark_totp_step(user_id, step):
                ok = True
            if not ok and code:
                ok = store.consume_mfa_backup_code(user_id, _mfa_backup_hash(user_id, code))
        if not ok:
            _mfa_record(user_id, ok=False)
            store.audit("", "-", "auth_mfa_verify_failed", user_id, {"reason": "bad_code"})
            logger.info("mfa verify failed: user=%s", user_id)
            return err(401, "invalid_mfa_code", "인증 코드가 올바르지 않습니다", request)
        _mfa_record(user_id, ok=True)
        user = store.get_user_by_id(user_id)
        email = str((user or {}).get("email") or "-")
        store.audit("", email, "auth_login_mfa", user_id, {})
        logger.info("mfa login: user=%s", user_id)
        resp = JSONResponse({"user": _pub_user(user) if user else {}})
        _set_session(resp, request, user_id)
        # 챌린지 쿠키는 티켓 소비와 함께 무효 — 남겨두면 login CSRF(타인 티켓 심기)와
        # stale 챌린지 페이지 UX 문제가 생긴다.
        resp.delete_cookie(MFA_PENDING_COOKIE, path="/")
        if user is not None:
            _notify_new_device(request, user, background_tasks)
        return resp

    @app.get("/api/v1/auth/mfa/challenge")
    def auth_mfa_challenge(request: Request) -> Any:
        """Google 로그인 MFA 챌린지 페이지 — mfa_pending 쿠키의 티켓을 HTML에 심는다.
        티켓 원문은 URL/응답 본문에 노출하지 않고 서버가 렌더링 시에만 삽입한다."""
        ticket = request.cookies.get(MFA_PENDING_COOKIE, "")
        if not ticket:
            return RedirectResponse("/", status_code=302)
        return HTMLResponse(_mfa_challenge_page(ticket))

    @app.get("/api/v1/auth/mfa/status")
    def auth_mfa_status(request: Request) -> JSONResponse:
        user = _session_user(request)
        if user is None:
            return err(401, "unauthorized", "로그인이 필요합니다", request)
        sec = store.get_mfa_secret(user["id"])
        enabled = sec is not None and sec.get("status") == "active"
        return JSONResponse(
            {
                "mfa_enabled": enabled,
                "status": (sec or {}).get("status", "disabled"),
                "backup_codes_remaining": (
                    store.count_mfa_backup_codes(user["id"]) if enabled else 0
                ),
            }
        )

    @app.post("/api/v1/auth/mfa/disable")
    def auth_mfa_disable(body: dict[str, Any], request: Request) -> JSONResponse:
        """MFA 해제 — 비밀번호 또는 TOTP 재인증 필수. 성공 시 백업 코드 전량
        무효화 + 모든 세션 폐기."""
        user = _session_user(request)
        if user is None:
            return err(401, "unauthorized", "로그인이 필요합니다", request)
        sec = store.get_mfa_secret(user["id"])
        if sec is None or sec.get("status") != "active":
            return err(400, "mfa_not_enabled", "활성화된 MFA가 없습니다", request)
        if not _mfa_reauth_ok(user, body):
            store.audit(
                "", str(user.get("email") or "-"), "auth_mfa_disable_failed", user["id"], {}
            )
            return err(
                403,
                "invalid_credentials",
                "비밀번호 또는 인증 코드가 올바르지 않습니다",
                request,
            )
        store.set_mfa_status(user["id"], "disabled")
        store.delete_mfa_backup_codes(user["id"])
        revoked = store.revoke_user_sessions(user["id"])
        store.audit(
            "",
            str(user.get("email") or "-"),
            "auth_mfa_disabled",
            user["id"],
            {"sessions_revoked": revoked},
        )
        logger.info("mfa disabled: user=%s sessions_revoked=%d", user["id"], revoked)
        return JSONResponse({"status": "disabled", "sessions_revoked": revoked})

    @app.post("/api/v1/auth/mfa/backup-codes")
    def auth_mfa_backup_codes(body: dict[str, Any], request: Request) -> JSONResponse:
        """백업 코드 재발급 — 재인증 후 새 세트 발급, 기존 미사용 코드는 전부 무효화.
        세션은 유지한다(사용자 본인이 재인증했으므로)."""
        user = _session_user(request)
        if user is None:
            return err(401, "unauthorized", "로그인이 필요합니다", request)
        sec = store.get_mfa_secret(user["id"])
        if sec is None or sec.get("status") != "active":
            return err(400, "mfa_not_enabled", "활성화된 MFA가 없습니다", request)
        if not _mfa_reauth_ok(user, body):
            store.audit(
                "",
                str(user.get("email") or "-"),
                "auth_mfa_backup_regen_failed",
                user["id"],
                {},
            )
            return err(
                403,
                "invalid_credentials",
                "비밀번호 또는 인증 코드가 올바르지 않습니다",
                request,
            )
        codes = _issue_mfa_backup_codes(user["id"])
        store.audit(
            "",
            str(user.get("email") or "-"),
            "auth_mfa_backup_regen",
            user["id"],
            {"backup_codes": len(codes)},
        )
        logger.info("mfa backup codes regenerated: user=%s", user["id"])
        return JSONResponse({"backup_codes": codes})

    @app.post("/api/v1/auth/logout")
    def auth_logout(request: Request) -> Any:
        resp = JSONResponse({"status": "logged_out"})
        token = request.cookies.get(SESSION_COOKIE, "")
        if token:
            store.revoke_session(hash_token(token))
        resp.delete_cookie(SESSION_COOKIE, path="/")
        return resp

    @app.post("/api/v1/auth/password")
    def auth_change_password(body: dict[str, Any], request: Request) -> JSONResponse:
        """비밀번호 변경 — 현재 비밀번호 확인 후 새 해시 저장 + 전체 세션 revoke.

        세션 탈취 상황에서 공격자가 비밀번호를 바꿔 소유자를 밀어내는 것을 막기 위해
        현재 비밀번호를 요구하고, 변경 성공 시 모든 세션(현재 포함)을 폐기한다 —
        호출자는 다시 로그인해야 한다."""
        user = _session_user(request)
        if user is None:
            return err(401, "unauthorized", "로그인이 필요합니다", request)
        current = str(body.get("current_password", ""))
        new_password = str(body.get("new_password", ""))
        weak = _reject_weak_password(request, new_password, str(user.get("email") or ""))
        if weak is not None:
            return weak
        row = store.get_user_password_hash(str(user.get("email") or ""))
        # Google-only 계정(password_hash 빈 값)은 현재 비밀번호가 없어 변경 불가 —
        # 명시적 연결 플로우가 생기기 전까지 지원하지 않는다.
        if row is None or not row[1] or not verify_password(current, row[1]):
            store.audit(
                "",
                str(user.get("email") or "-"),
                "auth_password_change_failed",
                user["id"],
                {},
            )
            logger.info("password change failed: user=%s", user["id"])
            return err(403, "invalid_credentials", "현재 비밀번호가 올바르지 않습니다", request)
        store.set_user_password(user["id"], hash_password(new_password))
        revoked = store.revoke_user_sessions(user["id"])
        store.audit(
            "",
            str(user.get("email") or "-"),
            "auth_password_change",
            user["id"],
            {"sessions_revoked": revoked},
        )
        logger.info("password changed: user=%s sessions_revoked=%s", user["id"], revoked)
        resp = JSONResponse({"status": "ok", "sessions_revoked": revoked})
        resp.delete_cookie(SESSION_COOKIE, path="/")
        return resp

    @app.get("/api/v1/auth/sessions")
    def auth_sessions(request: Request) -> JSONResponse:
        """현재 사용자의 유효 세션 목록 — 현재 세션은 current=true로 표시."""
        user = _session_user(request)
        if user is None:
            return err(401, "unauthorized", "로그인이 필요합니다", request)
        current_hash = hash_token(request.cookies.get(SESSION_COOKIE, ""))
        sessions = [
            {
                "id": s["id"],
                "created_at": s["created_at"],
                "expires_at": s["expires_at"],
                "current": s["id"] == current_hash,
            }
            for s in store.list_user_sessions(user["id"])
        ]
        return JSONResponse({"sessions": sessions})

    @app.delete("/api/v1/auth/sessions/{session_id}")
    def auth_revoke_session(session_id: str, request: Request) -> JSONResponse:
        """특정 세션 폐기 — 본인 소유 세션만 가능(타인 세션 id는 404)."""
        user = _session_user(request)
        if user is None:
            return err(401, "unauthorized", "로그인이 필요합니다", request)
        owned = any(s["id"] == session_id for s in store.list_user_sessions(user["id"]))
        if not owned:
            return err(404, "not_found", "세션을 찾을 수 없습니다", request)
        store.revoke_session(session_id)
        store.audit("", str(user.get("email") or "-"), "auth_session_revoke", session_id[:12], {})
        logger.info("session revoked: user=%s", user["id"])
        return JSONResponse({"status": "revoked"})

    @app.delete("/api/v1/auth/account")
    def auth_delete_account(body: dict[str, Any], request: Request) -> JSONResponse:
        """계정 삭제(개인정보보호법 대응) — 세션 인증 필수.

        비밀번호 계정은 {password} 검증, Google-only(비밀번호 해시 없음)는
        {confirm_email} 일치로 본인 확인한다. 마지막 admin은 409.
        audit_events는 법적 보존을 위해 actor만 'deleted_user'로 마스킹해 유지."""
        user = _session_user(request)
        if user is None:
            return err(401, "unauthorized", "로그인이 필요합니다", request)
        uid = str(user["id"])
        email = str(user.get("email") or "")
        row = store.get_user_password_hash(email)
        if row is not None and row[1]:
            # 비밀번호 계정 — 현재 비밀번호 재확인 (타인 세션 탈취 대비)
            if not verify_password(str(body.get("password", "")), row[1]):
                store.audit("", email, "auth_account_delete_failed", uid, {})
                logger.info("account delete failed (bad password): user=%s", uid)
                return err(401, "invalid_credentials", "비밀번호가 올바르지 않습니다", request)
        else:
            # Google-only 계정 — 비밀번호가 없으므로 이메일 재입력으로 확인
            confirm = str(body.get("confirm_email", "")).strip().lower()
            if confirm != email.lower():
                store.audit(
                    "",
                    email,
                    "auth_account_delete_failed",
                    uid,
                    {"reason": "confirm_mismatch"},
                )
                return err(
                    422,
                    "confirm_required",
                    "confirm_email이 가입 이메일과 일치해야 합니다",
                    request,
                )
        # 마지막 admin 보호 + 삭제를 store 락 안에서 원자적으로 — 동시
        # 삭제/강등 요청이 둘 다 admin 수를 통과해 관리자가 0명이 되는
        # TOCTOU를 막는다(프로세스 내 직렬화; 멀티 인스턴스는 한계).
        with store.exclusive():
            if _effective_role(user) == "admin":
                admins = [u for u in store.list_users() if _effective_role(u) == "admin"]
                if len(admins) <= 1:
                    store.audit(
                        "", email, "auth_account_delete_blocked", uid, {"reason": "last_admin"}
                    )
                    logger.warning("account delete blocked: last admin user=%s", uid)
                    return err(
                        409,
                        "last_admin",
                        "마지막 관리자 계정은 삭제할 수 없습니다",
                        request,
                    )
            # 감사 기록은 actor 마스킹 전에 남긴다 — 삭제 후에도 행위가 추적된다.
            # delete가 실패하면 감사도 남기지 않기 위해 삭제 성공 후 기록한다.
            counts = store.delete_user_cascade(uid)
            store.audit("", email, "auth_account_delete", uid, {})
        logger.info("account deleted: user=%s removed=%s", uid, counts)
        resp = JSONResponse({"status": "deleted", "removed": counts})
        resp.delete_cookie(SESSION_COOKIE, path="/")
        return resp

    @app.get("/api/v1/auth/export")
    def auth_export(request: Request) -> JSONResponse:
        """본인 데이터 다운로드(개인정보 열람권) — 비밀번호 해시·세션/인증
        토큰 해시는 제외하고 본인 귀속 데이터만 반환한다."""
        user = _session_user(request)
        if user is None:
            return err(401, "unauthorized", "로그인이 필요합니다", request)
        uid = str(user["id"])
        data = store.export_user_data(uid)
        store.audit("", str(user.get("email") or "-"), "auth_data_export", uid, {})
        logger.info("data export: user=%s", uid)
        return JSONResponse(
            content=data,
            headers={"Content-Disposition": 'attachment; filename="jobmate-data-export.json"'},
        )

    @app.get("/api/v1/auth/config")
    def auth_config() -> dict[str, bool]:
        """로그인 화면용 공개 설정 — 외부 노출 정보 없음."""
        return {"google_enabled": bool(_google_oauth_env("CLIENT_ID"))}

    @app.get("/api/v1/auth/me")
    def auth_me(request: Request) -> JSONResponse:
        user = _session_user(request)
        if user is None:
            # 세션 없지만 API 키/로컬 오픈 모드 — 백엔드 권한(_admin_user)과
            # 동일한 role을 반환해 UI 메뉴 게이팅이 실제 권한과 일치하게 한다
            pseudo = _admin_user(request)
            if pseudo is not None:
                return JSONResponse(
                    {
                        "user": {
                            "id": pseudo.get("id", ""),
                            "email": pseudo.get("email", ""),
                            "display_name": pseudo.get("display_name", ""),
                            "role": pseudo.get("role", "admin"),
                        }
                    }
                )
            return err(401, "unauthorized", "로그인이 필요합니다", request)
        return JSONResponse({"user": _pub_user(user)})

    @app.get("/api/v1/auth/google")
    def auth_google(request: Request) -> Any:
        if _remote_open_mode(request):
            return err(403, "forbidden", "이 인스턴스는 로컬 전용입니다", request)
        client_id = _google_oauth_env("CLIENT_ID")
        if not client_id:
            return err(501, "not_configured", "Google 로그인이 설정되지 않았습니다", request)
        state = new_session_token()
        url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(
            {
                "client_id": client_id,
                "redirect_uri": _google_redirect_uri(request),
                "response_type": "code",
                "scope": "openid email profile",
                "state": state,
                "prompt": "select_account",
            }
        )
        resp = RedirectResponse(url, status_code=302)
        resp.set_cookie(
            OAUTH_STATE_COOKIE,
            state,
            max_age=600,
            httponly=True,
            samesite="lax",
            secure=_cookie_secure(request),
            path="/",
        )
        return resp

    @app.get("/api/v1/auth/google/callback")
    def auth_google_callback(
        request: Request,
        background_tasks: BackgroundTasks,
        code: str = "",
        state: str = "",
        error: str = "",
    ) -> Any:
        def back(q: str) -> RedirectResponse:
            return RedirectResponse(f"/?auth_error={q}", status_code=302)

        # 콜백 최종 처리 전에 진입 차단 — OAuth 왕복 동안 DB 계정 생성
        # (create_user/_maybe_first_user_admin)이 이미 실행된 뒤 막히면
        # 원격 공격자가 첫 사용자 admin을 선점할 수 있다.
        if _remote_open_mode(request):
            return err(403, "forbidden", "이 인스턴스는 로컬 전용입니다", request)
        if error:
            return back("google_denied")
        expected = request.cookies.get(OAUTH_STATE_COOKIE, "")
        if not state or not expected or not hmac.compare_digest(state, expected):
            logger.warning("google oauth state mismatch")
            return err(400, "bad_state", "OAuth state가 일치하지 않습니다", request)
        client_id = _google_oauth_env("CLIENT_ID")
        client_secret = _google_oauth_env("CLIENT_SECRET")
        if not client_id or not client_secret:
            return err(501, "not_configured", "Google 로그인이 설정되지 않았습니다", request)
        try:
            token_resp = httpx.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "code": code,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uri": _google_redirect_uri(request),
                    "grant_type": "authorization_code",
                },
                timeout=15,
            )
            token_resp.raise_for_status()
            access_token = token_resp.json().get("access_token", "")
            info_resp = httpx.get(
                "https://openidconnect.googleapis.com/v1/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=15,
            )
            info_resp.raise_for_status()
            info = info_resp.json()
        except Exception as e:
            logger.warning("google oauth exchange failed: %s", str(e)[:200])
            return back("google_exchange_failed")
        gsub = str(info.get("sub", ""))
        email = str(info.get("email", "")).strip().lower()
        name = str(info.get("name", "")).strip()[:80]
        if not gsub or not _EMAIL_RE.match(email):
            return back("google_no_email")
        if info.get("email_verified") is not True:
            logger.warning("google login rejected: unverified email")
            return back("google_unverified_email")
        if not email_allowed(email):
            logger.warning("google login denied (not allowed): %s", email)
            return back("email_not_allowed")
        user = store.get_user_by_google_sub(gsub)
        if user is None:
            user = store.get_user_by_email(email)
            if user is not None and user.get("google_sub") not in ("", gsub):
                return back("google_account_mismatch")
            if user is not None and not user.get("google_sub"):
                # Pre-hijacking 방지: 비밀번호(자격증명)가 설정된 계정에 Google
                # sub를 자동 연결하면, 공격자가 피해자 이메일로 비밀번호 가입을
                # 선점해둔 상태에서 피해자의 Google 로그인이 공격자 계정에
                # 연결되어 계정을 탈취할 수 있다. 자동 연결은 password_hash가
                # 비어 있는 Google-only 계정(기존 Google 로그인 계정)에만 허용.
                cred = store.get_user_password_hash(email)
                if cred is not None and cred[1]:
                    if user.get("email_verified"):
                        store.audit(
                            "",
                            email,
                            "auth_google_link_refused",
                            user["id"],
                            {"reason": "password_account"},
                        )
                        logger.warning(
                            "google login refused: password account exists user=%s",
                            user["id"],
                        )
                        return back("google_account_conflict")
                    # 미인증 계정 — 공격자가 피해자 이메일로 비밀번호 가입을
                    # 심어둬도(미인증) Google이 이메일 소유권을 증명했으므로
                    # 공격자 해시를 비우고 Google 계정을 연결한다. 그렇지 않으면
                    # cred[1] 존재만으로 피해자의 Google 로그인이 영구 차단된다.
                    store.set_user_password(user["id"], "")
                    store.audit("", email, "auth_google_claim_unverified", user["id"], {})
                    logger.info("google login claimed unverified account: user=%s", user["id"])
                store.link_google_sub(user["id"], gsub)
        if user is None:
            # Google이 이메일 소유권을 증명했으므로 인증 완료로 간주
            user = store.create_user(email, "", name, gsub, email_verified=True)
            if user is None:
                return back("signup_failed")
            _maybe_first_user_admin(user, request)
            logger.info("auth signup via google: user=%s", user["id"])
        else:
            # 기존 계정에 Google 연결 — 이메일 인증 완료 처리
            store.set_user_email_verified(user["id"])
        store.audit("", email, "auth_login_google", user["id"], {})
        logger.info("auth login via google: user=%s", user["id"])
        mfa = store.get_mfa_secret(user["id"])
        if mfa is not None and mfa.get("status") == "active":
            # MFA 활성 계정 — Google 로그인도 두 번째 요소를 요구한다. 세션 대신
            # 5분·1회성 티켓을 HttpOnly 쿠키로 넘기고 챌린지 페이지로 보낸다.
            # /mfa/verify 성공 시에만 세션이 발급된다.
            ticket = store.create_mfa_ticket(user["id"])
            store.audit("", email, "auth_login_mfa_challenge", user["id"], {})
            logger.info("google login mfa challenge: user=%s", user["id"])
            resp = RedirectResponse("/api/v1/auth/mfa/challenge", status_code=302)
            resp.set_cookie(
                MFA_PENDING_COOKIE,
                ticket,
                max_age=300,
                httponly=True,
                samesite="lax",
                secure=_cookie_secure(request),
                path="/",
            )
            resp.delete_cookie(OAUTH_STATE_COOKIE, path="/")
            return resp
        resp = RedirectResponse("/", status_code=302)
        _set_session(resp, request, user["id"])
        resp.delete_cookie(OAUTH_STATE_COOKIE, path="/")
        resp.delete_cookie(MFA_PENDING_COOKIE, path="/")
        _notify_new_device(request, user, background_tasks)
        return resp

    # ---- 스코프드 API 키 관리 (spec 002 US4) ----
    _API_KEY_CREATE_BUCKET: dict[str, list[float]] = {}

    def _api_key_actor(request: Request) -> tuple[str, dict[str, Any] | None] | None:
        """키 관리 주체 — ("user", 세션유저) / ("key", db키정보) / ("env", None).
        env 키(JOBMATE_API_KEY)와 api_keys:manage 스코프 키, 세션 사용자만 허용."""
        user = _session_user(request)
        if user is not None:
            return ("user", user)
        ka = _db_api_key(request)
        if ka is not None and "api_keys:manage" in ka["scopes"]:
            return ("key", ka)
        if api_key() and authorized(request):
            return ("env", None)
        return None

    @app.post("/api/v1/auth/api-keys")
    def auth_api_key_create(body: dict[str, Any], request: Request) -> JSONResponse:
        """API 키 발급 — 원문은 이 응답에 1회만 포함, DB엔 해시+prefix만."""
        if _remote_open_mode(request):
            return err(403, "forbidden", "이 환경에서는 사용할 수 없습니다", request)
        actor = _api_key_actor(request)
        if actor is None:
            return err(401, "unauthorized", "로그인 또는 API 키 인증이 필요합니다", request)
        kind, obj = actor
        # FR-024 — 키 발급은 admin 세션·env 키·api_keys:manage 키만.
        # member 세션은 자기 키의 조회·폐기만 가능(발급 불가).
        if kind == "user" and obj is not None and _effective_role(obj) != "admin":
            return err(403, "forbidden", "API 키 발급은 관리자만 가능합니다", request)
        # 발급 rate limit — 주체당 분당 10회
        if kind == "user" and obj is not None:
            rate_key = f"user:{obj['id']}"
        elif kind == "key" and obj is not None:
            rate_key = f"key:{obj['id']}"
        else:
            rate_key = "env"
        if _rate_hit(_API_KEY_CREATE_BUCKET, rate_key, 60.0, 10):
            store.audit("", "-", "auth_api_key_create_throttled", "", {})
            return err(429, "too_many_attempts", "키 발급 요청이 너무 많습니다", request)
        name = str(body.get("name", "")).strip()[:80] or "default"
        scopes_raw = body.get("scopes")
        if scopes_raw is None:
            scopes_raw = []
        if not isinstance(scopes_raw, list) or not all(isinstance(s, str) for s in scopes_raw):
            return err(422, "invalid_scopes", "scopes는 문자열 배열이어야 합니다", request)
        scopes = sorted({s.strip() for s in scopes_raw if s.strip()})
        bad = [s for s in scopes if s not in _API_KEY_SCOPES]
        if bad:
            return err(
                422,
                "invalid_scopes",
                f"허용되지 않는 스코프: {', '.join(bad[:5])}",
                request,
            )
        # 권한 상승 방지 — *, admin, api_keys:manage 스코프는 admin 세션 또는
        # env 키만 발급 가능. api_keys:manage 키가 동일/상위 권한 키를
        # 발급해 권한이 키를 타고 영속 확장되는 것을 차단한다.
        _ELEVATED = {"*", "admin", "api_keys:manage"}
        if _ELEVATED & set(scopes) and kind == "key":
            return err(
                403,
                "forbidden",
                "관리자 수준 스코프는 admin 세션 또는 서버 키로만 발급할 수 있습니다",
                request,
            )
        expires_at = None
        days = body.get("expires_in_days")
        if days is not None:
            try:
                days = int(days)
            except (TypeError, ValueError):
                return err(422, "invalid_expires", "expires_in_days는 정수여야 합니다", request)
            if not 1 <= days <= 365:
                return err(
                    422, "invalid_expires", "expires_in_days는 1~365 범위여야 합니다", request
                )
            expires_at = (datetime.now(UTC) + timedelta(days=days)).isoformat()
        owner_id = ""
        if kind == "user" and obj is not None:
            owner_id = str(obj["id"])
            # admin 세션은 body.owner_user_id로 타인 소유 키 발급 가능
            target = str(body.get("owner_user_id", "")).strip()
            if target and _effective_role(obj) == "admin":
                owner_id = target
        elif kind == "key" and obj is not None:
            # manage 키가 발급한 키는 부모 키 소유자를 계승 —
            # ownerless 키는 member 목록/폐기에서 영구히 보이지 않는다
            owner_id = str(obj.get("owner_user_id") or "")
        raw, prefix, khash = new_api_key()
        kid = store.create_api_key(owner_id, name, khash, prefix, scopes, expires_at)
        store.audit(
            "",
            str((obj or {}).get("email") or "-"),
            "auth_api_key_created",
            kid,
            {"name": name, "scopes": scopes, "owner": owner_id},
        )
        logger.info("api key created: id=%s owner=%s scopes=%s", kid, owner_id, scopes)
        return JSONResponse(
            {
                "id": kid,
                "name": name,
                "key_prefix": prefix,
                "key": raw,  # 원문은 이 응답에만 포함 — 재조회 불가
                "scopes": scopes,
                "expires_at": expires_at,
            },
            status_code=201,
        )

    @app.get("/api/v1/auth/api-keys")
    def auth_api_key_list(request: Request) -> JSONResponse:
        """키 메타데이터 목록 — 세션 사용자는 본인 키만, admin/env/manage 키는 전체.
        key_hash·원문은 어떤 응답에도 포함하지 않는다."""
        if _remote_open_mode(request):
            return err(403, "forbidden", "이 환경에서는 사용할 수 없습니다", request)
        user = _session_user(request)
        if user is not None:
            if _effective_role(user) == "admin":
                keys = store.list_api_keys(None)
            else:
                keys = store.list_api_keys(user["id"])
            return JSONResponse({"keys": keys})
        if api_key() and authorized(request):
            return JSONResponse({"keys": store.list_api_keys(None)})
        ka = _db_api_key(request)
        if ka is not None and "api_keys:manage" in ka["scopes"]:
            return JSONResponse({"keys": store.list_api_keys(None)})
        return err(401, "unauthorized", "로그인 또는 API 키 인증이 필요합니다", request)

    @app.delete("/api/v1/auth/api-keys/{key_id}")
    def auth_api_key_revoke(key_id: str, request: Request) -> JSONResponse:
        """키 폐기 — 본인 소유 키 또는 관리 권한자만."""
        if _remote_open_mode(request):
            return err(403, "forbidden", "이 환경에서는 사용할 수 없습니다", request)
        user = _session_user(request)
        if user is not None:
            if _effective_role(user) == "admin":
                ok = store.revoke_api_key(key_id)
            else:
                ok = store.revoke_api_key(key_id, owner_user_id=user["id"])
            if not ok:
                return err(404, "not_found", "키를 찾을 수 없습니다", request)
            store.audit(
                "",
                str(user.get("email") or "-"),
                "auth_api_key_revoked",
                key_id,
                {},
            )
            logger.info("api key revoked: id=%s by=%s", key_id, user["id"])
            return JSONResponse({"status": "revoked"})
        if api_key() and authorized(request):
            if not store.revoke_api_key(key_id):
                return err(404, "not_found", "키를 찾을 수 없습니다", request)
            store.audit("", "-", "auth_api_key_revoked", key_id, {"via": "env_key"})
            return JSONResponse({"status": "revoked"})
        ka = _db_api_key(request)
        if ka is not None and "api_keys:manage" in ka["scopes"]:
            if not store.revoke_api_key(key_id):
                return err(404, "not_found", "키를 찾을 수 없습니다", request)
            store.audit("", "-", "auth_api_key_revoked", key_id, {"via": f"key:{ka['id']}"})
            return JSONResponse({"status": "revoked"})
        return err(401, "unauthorized", "로그인 또는 API 키 인증이 필요합니다", request)
