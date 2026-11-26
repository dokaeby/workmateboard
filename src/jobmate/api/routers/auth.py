"""Auth routes — extracted from api/app.py create_app().

Handlers are byte-for-byte copies; create_app locals arrive via `ctx`
(SimpleNamespace) and security helpers via module imports.
"""

from __future__ import annotations

import hmac
import logging
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ...security import (
    OAUTH_STATE_COOKIE,
    SESSION_COOKIE,
    allowed_emails,
    api_key,
    authorized,
    email_allowed,
    hash_password,
    hash_token,
    new_session_token,
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
    _maybe_first_user_admin = ctx._maybe_first_user_admin
    _pub_user = ctx._pub_user
    _send_verification = ctx._send_verification
    _session_user = ctx._session_user
    _set_session = ctx._set_session
    _signup_record = ctx._signup_record
    _signup_throttled = ctx._signup_throttled
    err = ctx.err
    store = ctx.store

    @app.post("/api/v1/auth/signup")
    def auth_signup(body: dict[str, Any], request: Request) -> JSONResponse:
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
        if len(password) < 8:
            return err(400, "weak_password", "비밀번호는 8자 이상이어야 합니다", request)
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
        _maybe_first_user_admin(user)
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
    def auth_login(body: dict[str, Any], request: Request) -> JSONResponse:
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
        store.audit("", email, "auth_login", row[0], {})
        logger.info("auth login: user=%s", row[0])
        resp = JSONResponse({"user": _pub_user(user) if user else {}})
        _set_session(resp, request, row[0])
        return resp

    @app.post("/api/v1/auth/verify/resend")
    def auth_verify_resend(body: dict[str, Any], request: Request) -> JSONResponse:
        """미인증 계정의 인증 메일 재발송 — 같은 이메일 10분에 1회(rate limit).

        /api/v1/auth/ prefix는 미들웨어 인증 예외이므로 남용 방지로
        미가입/이미 인증 이메일에도 동일한 응답 형태를 반환한다(계정 열거 방지)."""
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
    def auth_verify(request: Request, token: str = "") -> HTMLResponse:
        """인증 링크 — 유효 토큰이면 email_verified=1 + 세션 쿠키 발급.

        consume은 UPDATE ... WHERE used_at IS NULL AND expires_at>? 의 원자적
        rowcount 판정 — 동시 요청이 같은 토큰을 둘 다 유효하다고 보지 않는다."""
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
        if len(new_password) < 8:
            return err(400, "weak_password", "새 비밀번호는 8자 이상이어야 합니다", request)
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
            return err(404, "not_found", "session not found", request)
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
        # 마지막 admin 보호 — 삭제 후 관리자가 0명이 되는 것을 막는다.
        if _effective_role(user) == "admin":
            admins = [u for u in store.list_users() if _effective_role(u) == "admin"]
            if len(admins) <= 1:
                store.audit("", email, "auth_account_delete_blocked", uid, {"reason": "last_admin"})
                logger.warning("account delete blocked: last admin user=%s", uid)
                return err(
                    409,
                    "last_admin",
                    "마지막 관리자 계정은 삭제할 수 없습니다",
                    request,
                )
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
        # 감사 기록은 actor 마스킹 전에 남긴다 — 삭제 후에도 행위가 추적된다
        store.audit("", email, "auth_account_delete", uid, {})
        counts = store.delete_user_cascade(uid)
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
        request: Request, code: str = "", state: str = "", error: str = ""
    ) -> Any:
        def back(q: str) -> RedirectResponse:
            return RedirectResponse(f"/?auth_error={q}", status_code=302)

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
                    store.audit(
                        "",
                        email,
                        "auth_google_link_refused",
                        user["id"],
                        {"reason": "password_account"},
                    )
                    logger.warning(
                        "google login refused: password account exists user=%s", user["id"]
                    )
                    return back("google_account_conflict")
                store.link_google_sub(user["id"], gsub)
        if user is None:
            # Google이 이메일 소유권을 증명했으므로 인증 완료로 간주
            user = store.create_user(email, "", name, gsub, email_verified=True)
            if user is None:
                return back("signup_failed")
            _maybe_first_user_admin(user)
            logger.info("auth signup via google: user=%s", user["id"])
        else:
            # 기존 계정에 Google 연결 — 이메일 인증 완료 처리
            store.set_user_email_verified(user["id"])
        store.audit("", email, "auth_login_google", user["id"], {})
        logger.info("auth login via google: user=%s", user["id"])
        resp = RedirectResponse("/", status_code=302)
        _set_session(resp, request, user["id"])
        resp.delete_cookie(OAUTH_STATE_COOKIE, path="/")
        return resp
