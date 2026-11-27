# mypy: disable-error-code="attr-defined"
"""User/auth session/email verification/email summary store methods — mixin for Store."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from .util import _clamp_limit, _dt, _iso, _now


class UserStoreMixin:
    # ---- users / auth_sessions ----
    @staticmethod
    def _norm_role(role: Any) -> str:
        """users.role 정규화 — member|lead|admin. 레거시 'user'는 member."""
        r = str(role or "").strip().lower()
        if r == "user":
            r = "member"
        return r if r in ("member", "lead", "admin") else "member"

    @staticmethod
    def _user_json(r: Any) -> dict[str, Any]:
        return {
            "id": r["id"],
            "email": r["email"],
            "display_name": r["display_name"],
            "google_sub": r["google_sub"],
            "role": UserStoreMixin._norm_role(r["role"] if "role" in r.keys() else "user"),
            # 컬럼이 없는 구 스키마 SELECT(세션 JOIN 등)는 검증 완료로 간주
            "email_verified": bool(r["email_verified"]) if "email_verified" in r.keys() else True,
            "created_at": r["created_at"],
        }

    def create_user(
        self,
        email: str,
        password_hash: str = "",
        display_name: str = "",
        google_sub: str = "",
        email_verified: bool = False,
    ) -> dict[str, Any] | None:
        """신규 사용자 생성. email 중복 시 None.
        email_verified 기본 False — 비밀번호 가입은 이메일 소유권 미증명.
        Google OAuth처럼 이메일이 검증된 경로만 True를 넘긴다."""
        uid = uuid.uuid4().hex[:16]
        now = _iso(_now())
        try:
            with self.tx() as c:
                c.execute(
                    "INSERT INTO users(id,email,password_hash,display_name,google_sub,"
                    "email_verified,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        uid,
                        email.lower(),
                        password_hash,
                        display_name,
                        google_sub,
                        1 if email_verified else 0,
                        now,
                        now,
                    ),
                )
        except self._integrity_errors:
            return None
        return self.get_user_by_email(email)

    def get_user_by_email(self, email: str) -> dict[str, Any] | None:
        r = self._conn.execute("SELECT * FROM users WHERE email=?", (email.lower(),)).fetchone()
        return self._user_json(r) if r else None

    def get_user_by_id(self, uid: str) -> dict[str, Any] | None:
        r = self._conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        return self._user_json(r) if r else None

    def get_user_by_google_sub(self, sub: str) -> dict[str, Any] | None:
        r = self._conn.execute("SELECT * FROM users WHERE google_sub=?", (sub,)).fetchone()
        return self._user_json(r) if r else None

    def link_google_sub(self, uid: str, google_sub: str) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE users SET google_sub=?, updated_at=? WHERE id=?",
                (google_sub, _iso(_now()), uid),
            )

    def get_user_password_hash(self, email: str) -> tuple[str, str] | None:
        """(user_id, password_hash) — 로그인 검증용. 비밀번호 계정만."""
        r = self._conn.execute(
            "SELECT id, password_hash FROM users WHERE email=?", (email.lower(),)
        ).fetchone()
        return (r["id"], r["password_hash"]) if r else None

    def create_session(self, user_id: str, token_hash: str, ttl_days: int = 30) -> None:
        """세션 생성 — id는 토큰의 SHA-256 해시(원문 토큰은 저장하지 않음)."""
        now = _now()
        exp = now + timedelta(days=ttl_days)
        with self.tx() as c:
            c.execute(
                "INSERT INTO auth_sessions(id,user_id,created_at,expires_at) VALUES(?,?,?,?)",
                (token_hash, user_id, _iso(now), _iso(exp)),
            )

    def get_session_user(self, token_hash: str) -> dict[str, Any] | None:
        """유효 세션의 사용자 반환. 만료/폐기 세션은 None."""
        r = self._conn.execute(
            "SELECT u.id, u.email, u.display_name, u.google_sub, u.role, u.created_at,"
            " s.expires_at"
            " FROM auth_sessions s JOIN users u ON u.id = s.user_id"
            " WHERE s.id=? AND s.revoked_at IS NULL",
            (token_hash,),
        ).fetchone()
        if not r:
            return None
        exp = _dt(r["expires_at"])
        if exp is None or exp <= _now():
            return None
        return self._user_json(r)

    def revoke_session(self, token_hash: str) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE auth_sessions SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
                (_iso(_now()), token_hash),
            )

    def revoke_user_sessions(self, user_id: str) -> int:
        """사용자의 모든 유효 세션 폐기 — 비밀번호 변경 등 보안 이벤트용.
        반환값: 폐기된 세션 수."""
        with self.tx() as c:
            cur = c.execute(
                "UPDATE auth_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
                (_iso(_now()), user_id),
            )
            return int(getattr(cur, "rowcount", 0) or 0)

    def list_user_sessions(self, user_id: str) -> list[dict[str, Any]]:
        """사용자의 유효(미폐기·미만료) 세션 목록 — id는 토큰 해시."""
        rows = self._conn.execute(
            "SELECT id, created_at, expires_at FROM auth_sessions"
            " WHERE user_id=? AND revoked_at IS NULL AND expires_at>?"
            " ORDER BY created_at DESC",
            (user_id, _iso(_now())),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- email_verifications (가입 이메일 인증) ----
    def create_email_verification(self, email: str, ttl_hours: int = 24) -> str:
        """인증 토큰 발급 — 반환값이 원문(메일 링크용), DB에는 SHA-256 해시만 저장."""
        import hashlib
        import secrets

        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        now = _now()
        # opportunistic 정리 — used/expired 행이 누적되지 않도록 발급 시 함께 삭제
        self.delete_expired_verifications(now)
        with self.tx() as c:
            # 같은 이메일의 기존 미사용 토큰은 이번 발급으로 무효화 —
            # 재발송/재가입 시 이전 링크가 계속 유효한 채 남는 것을 막는다.
            c.execute(
                "UPDATE email_verifications SET used_at=? WHERE email=? AND used_at IS NULL",
                (_iso(now), email.lower()),
            )
            c.execute(
                "INSERT INTO email_verifications(id,email,token_hash,expires_at,created_at)"
                " VALUES(?,?,?,?,?)",
                (
                    uuid.uuid4().hex[:16],
                    email.lower(),
                    token_hash,
                    _iso(now + timedelta(hours=ttl_hours)),
                    _iso(now),
                ),
            )
        return token

    def get_valid_verification(self, token_hash: str) -> str | None:
        """미사용·미만료 토큰의 이메일 반환. 없거나 만료/사용이면 None."""
        r = self._conn.execute(
            "SELECT email, expires_at FROM email_verifications"
            " WHERE token_hash=? AND used_at IS NULL",
            (token_hash,),
        ).fetchone()
        if not r:
            return None
        exp = _dt(r["expires_at"])
        if exp is None or exp <= _now():
            return None
        return str(r["email"])

    def consume_email_verification(self, token_hash: str) -> str | None:
        """토큰 원자적 consume — 유효(미사용·미만료)하면 used_at 마킹 후 이메일 반환.

        SELECT 후 UPDATE하는 비원자 경로는 동시 요청이 같은 토큰을 둘 다
        유효하다고 판정할 수 있다. WHERE used_at IS NULL AND expires_at>? 조건의
        UPDATE rowcount로 단 한 번만 성공하도록 직렬화한다(SQLite/PG 공용)."""
        now = _now()
        with self.tx() as c:
            cur = c.execute(
                "UPDATE email_verifications SET used_at=?"
                " WHERE token_hash=? AND used_at IS NULL AND expires_at>?",
                (_iso(now), token_hash, _iso(now)),
            )
            if int(getattr(cur, "rowcount", 0) or 0) <= 0:
                return None
            r = c.execute(
                "SELECT email FROM email_verifications WHERE token_hash=?", (token_hash,)
            ).fetchone()
            return str(r["email"]) if r else None

    def mark_verification_used(self, token_hash: str) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE email_verifications SET used_at=? WHERE token_hash=? AND used_at IS NULL",
                (_iso(_now()), token_hash),
            )

    def delete_expired_verifications(self, now: datetime | None = None) -> int:
        """만료되었거나 이미 사용된 인증 토큰 행 삭제 — 누적 방지용.

        used 행도 함께 지운다(재사용은 어차피 불가). 최신 발급 시각 기반
        재발송 rate limit은 삭제 전에 평가되므로 영향 없음.
        반환값: 삭제된 행 수."""
        cutoff = _iso(now or _now())
        with self.tx() as c:
            cur = c.execute(
                "DELETE FROM email_verifications WHERE expires_at < ? OR used_at IS NOT NULL",
                (cutoff,),
            )
            return int(getattr(cur, "rowcount", 0) or 0)

    def latest_verification_at(self, email: str) -> datetime | None:
        """해당 이메일의 가장 최근 인증 토큰 발급 시각 — 재발송 rate limit용."""
        r = self._conn.execute(
            "SELECT created_at FROM email_verifications WHERE email=?"
            " ORDER BY created_at DESC LIMIT 1",
            (email.lower(),),
        ).fetchone()
        return _dt(r["created_at"]) if r else None

    def set_user_email_verified(self, user_id: str) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE users SET email_verified=1, updated_at=? WHERE id=?",
                (_iso(_now()), user_id),
            )

    # ---- email_summaries (3줄 요약 카드) ----
    def save_email_summary(
        self,
        provider: str,
        source_id: str,
        *,
        subject: str,
        sender: str,
        summary: str,
        message_at: str = "",
    ) -> None:
        from ..security import encrypt_str

        with self.tx() as c:
            c.execute(
                "INSERT INTO email_summaries(source_provider,source_id,"
                "subject,sender,summary,message_at,checked_at)"
                " VALUES(?,?,?,?,?,?,?)"
                " ON CONFLICT (source_provider,source_id) DO UPDATE SET"
                " subject=excluded.subject, sender=excluded.sender,"
                " summary=excluded.summary, message_at=excluded.message_at,"
                " checked_at=excluded.checked_at",
                (
                    provider,
                    source_id,
                    encrypt_str(subject),
                    encrypt_str(sender),
                    encrypt_str(summary),
                    message_at,
                    _iso(_now()),
                ),
            )

    def list_email_summaries(
        self, provider: str = "gmail", limit: int = 30
    ) -> list[dict[str, Any]]:
        from ..security import decrypt_str

        limit = _clamp_limit(limit, 30, 200)

        rows = self._conn.execute(
            "SELECT * FROM email_summaries WHERE source_provider=?"
            " ORDER BY checked_at DESC LIMIT ?",
            (provider, limit),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            for k in ("subject", "sender", "summary"):
                d[k] = decrypt_str(d.get(k, ""))
            out.append(d)
        return out

    def purge_undecryptable_email_summaries(self, provider: str = "gmail") -> int:
        """복호화되지 않는(enc:v1: 접두사가 복호화 후에도 남는) 행 삭제.
        암호화 키 로테이션 등으로 읽을 수 없게 된 행을 정리할 때 사용."""
        from ..security import PREFIX, decrypt_str

        rows = self._conn.execute(
            "SELECT source_provider, source_id, subject, sender, summary"
            " FROM email_summaries WHERE source_provider=?",
            (provider,),
        ).fetchall()
        bad = [
            (r["source_provider"], r["source_id"])
            for r in rows
            if any(
                decrypt_str(r.get(k, "")).startswith(PREFIX)
                for k in ("subject", "sender", "summary")
            )
        ]
        if not bad:
            return 0
        with self.tx() as c:
            for prov, sid in bad:
                c.execute(
                    "DELETE FROM email_summaries WHERE source_provider=? AND source_id=?",
                    (prov, sid),
                )
        return len(bad)
