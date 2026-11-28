# mypy: disable-error-code="attr-defined"
"""Organization/멤버십 store 메서드 — mixin for Store. spec 003 US1."""

from __future__ import annotations

import re
import uuid
from datetime import timedelta
from typing import Any

from .util import _dt, _iso, _now

_ORG_SLUG_RE = re.compile(r"[^a-z0-9-]+")
ORG_ROLES = ("org_owner", "org_admin", "member")
ORG_STATUSES = ("active", "suspended", "deleted")


class OrgTransferError(RuntimeError):
    """소유권 이관 부분 실패 — tx 롤백 후 호출자에게 전달."""


class OrgInactiveError(RuntimeError):
    """suspended/deleted 조직에 대한 run 생성 시도 — FR-031.

    create_run이 모든 진입점의 choke point이므로 여기서 던진다.
    API 계층은 403 org_inactive로 변환한다."""


def normalize_org_slug(raw: str) -> str:
    """조직 슬러그 정규화 — 소문자 영숫자·하이픈 3~48자."""
    s = _ORG_SLUG_RE.sub("-", raw.strip().lower()).strip("-")
    s = re.sub(r"-{2,}", "-", s)[:48].strip("-")
    return s if len(s) >= 3 else ""


class OrgStoreMixin:
    @staticmethod
    def _org_json(r: Any) -> dict[str, Any]:
        return {
            "id": r["id"],
            "name": r["name"],
            "slug": r["slug"],
            "kind": r["kind"],
            "status": r["status"],
            "created_by": r["created_by"],
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
        }

    @staticmethod
    def _member_json(r: Any) -> dict[str, Any]:
        return {
            "org_id": r["org_id"],
            "user_id": r["user_id"],
            "role": r["role"],
            "status": r["status"],
            "joined_at": r["joined_at"],
        }

    def _unique_org_slug(self, base: str) -> str:
        """slug 충돌 시 -2, -3… 접미사로 유일값 생성."""
        slug = base
        for i in range(2, 100):
            r = self._conn.execute(
                "SELECT 1 FROM organizations WHERE lower(slug)=lower(?)", (slug,)
            ).fetchone()
            if not r:
                return slug
            suffix = f"-{i}"
            slug = base[: 48 - len(suffix)] + suffix
        raise RuntimeError("cannot allocate unique org slug")

    def create_organization(
        self,
        name: str,
        slug: str,
        kind: str = "team",
        created_by: str = "",
    ) -> str | None:
        """조직 생성 — slug 충돌 시 None. 반환: 조직 id."""
        oid = uuid.uuid4().hex[:16]
        now = _iso(_now())
        try:
            with self.tx() as c:
                c.execute(
                    "INSERT INTO organizations(id,name,slug,kind,status,created_by,"
                    "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (oid, name, slug, kind, "active", created_by, now, now),
                )
        except self._integrity_errors:
            return None
        return oid

    def get_organization(self, org_id: str) -> dict[str, Any] | None:
        r = self._conn.execute("SELECT * FROM organizations WHERE id=?", (org_id,)).fetchone()
        return self._org_json(r) if r else None

    def get_organization_by_slug(self, slug: str) -> dict[str, Any] | None:
        r = self._conn.execute(
            "SELECT * FROM organizations WHERE lower(slug)=lower(?)", (slug,)
        ).fetchone()
        return self._org_json(r) if r else None

    def list_organizations(self, include_deleted: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM organizations"
        if not include_deleted:
            sql += " WHERE status != 'deleted'"
        rows = self._conn.execute(sql + " ORDER BY created_at").fetchall()
        return [self._org_json(r) for r in rows]

    def update_organization(
        self, org_id: str, name: str | None = None, slug: str | None = None
    ) -> bool | None:
        """이름/슬러그 갱신. slug 충돌 시 None, 대상 없으면 False."""
        org = self.get_organization(org_id)
        if not org:
            return False
        new_slug = slug if slug is not None else org["slug"]
        try:
            with self.tx() as c:
                cur = c.execute(
                    "UPDATE organizations SET name=?, slug=?, updated_at=? WHERE id=?",
                    (name if name is not None else org["name"], new_slug, _iso(_now()), org_id),
                )
            return int(getattr(cur, "rowcount", 0) or 0) > 0
        except self._integrity_errors:
            return None

    def org_inactive(self, org_id: str) -> bool:
        """조직이 자동화 run 생성 차단 대상인지 — FR-031.

        suspended/deleted이거나 조직 행이 없으면(purge 등) True.
        org_id=''는 org_id 도입 전 레거시 컨텍스트라 active로 간주한다.
        조회 자체가 실패하면 차단하지 않는다(False) — 판정 불가로
        자동화를 멈추는 것보다 기존 동작 유지가 안전하다."""
        if not org_id:
            return False
        try:
            org = self.get_organization(org_id)
        except Exception:
            return False
        return org is None or str(org.get("status") or "active") != "active"

    def set_organization_status(self, org_id: str, status: str) -> bool:
        if status not in ORG_STATUSES:
            raise ValueError(f"invalid org status: {status}")
        with self.tx() as c:
            cur = c.execute(
                "UPDATE organizations SET status=?, updated_at=? WHERE id=?",
                (status, _iso(_now()), org_id),
            )
        return int(getattr(cur, "rowcount", 0) or 0) > 0

    # ---- 멤버십 ----

    def add_org_member(self, org_id: str, user_id: str, role: str = "member") -> bool:
        """멤버 추가 — 이미 있으면 role/status만 갱신(재초대·재가입 대응).
        반환: 멤버십이 active로 존재하면 True."""
        if role not in ORG_ROLES:
            raise ValueError(f"invalid org role: {role}")
        now = _iso(_now())
        with self.tx() as c:
            c.execute(
                "INSERT OR IGNORE INTO organization_members"
                "(org_id,user_id,role,status,joined_at) VALUES(?,?,?,'active',?)",
                (org_id, user_id, role, now),
            )
            c.execute(
                "UPDATE organization_members SET role=?, status='active'"
                " WHERE org_id=? AND user_id=?",
                (role, org_id, user_id),
            )
        return True

    def get_org_membership(self, org_id: str, user_id: str) -> dict[str, Any] | None:
        r = self._conn.execute(
            "SELECT * FROM organization_members WHERE org_id=? AND user_id=?",
            (org_id, user_id),
        ).fetchone()
        if not r or r["status"] != "active":
            return None
        return self._member_json(r)

    def list_org_members(self, org_id: str) -> list[dict[str, Any]]:
        """멤버 목록 + 사용자 표시 정보(email/display_name) 조인."""
        rows = self._conn.execute(
            "SELECT m.*, u.email, u.display_name FROM organization_members m"
            " JOIN users u ON u.id = m.user_id"
            " WHERE m.org_id=? ORDER BY m.joined_at",
            (org_id,),
        ).fetchall()
        out = []
        for r in rows:
            d = self._member_json(r)
            d["email"] = r["email"]
            d["display_name"] = r["display_name"]
            out.append(d)
        return out

    def list_user_orgs(self, user_id: str) -> list[dict[str, Any]]:
        """사용자가 속한 활성 조직 목록 — 조직 dict + 내 role."""
        rows = self._conn.execute(
            "SELECT o.*, m.role AS my_role FROM organization_members m"
            " JOIN organizations o ON o.id = m.org_id"
            " WHERE m.user_id=? AND m.status='active'"
            " AND o.status != 'deleted' ORDER BY m.joined_at",
            (user_id,),
        ).fetchall()
        out = []
        for r in rows:
            d = self._org_json(r)
            d["my_role"] = r["my_role"]
            out.append(d)
        return out

    def count_org_owners(self, org_id: str) -> int:
        r = self._conn.execute(
            "SELECT COUNT(*) AS n FROM organization_members"
            " WHERE org_id=? AND role='org_owner' AND status='active'",
            (org_id,),
        ).fetchone()
        return int(r["n"] if r else 0)

    def count_org_active_runs(self, org_id: str) -> int:
        """org의 활성 run 수 — 삭제 차단 판정용(FR-028).

        queued|running|waiting_approval만 활성으로 본다 — spec 명시 집합.
        waiting_review/needs_human 등은 사람 개입 대기라 진행 중 작업이
        아니므로 삭제를 막지 않는다."""
        r = self._conn.execute(
            "SELECT COUNT(*) AS n FROM runs WHERE org_id=?"
            " AND status IN ('queued','running','waiting_approval')",
            (org_id,),
        ).fetchone()
        return int(r["n"] if r else 0)

    def set_org_member_role(self, org_id: str, user_id: str, role: str) -> bool:
        if role not in ORG_ROLES:
            raise ValueError(f"invalid org role: {role}")
        with self.tx() as c:
            cur = c.execute(
                "UPDATE organization_members SET role=?"
                " WHERE org_id=? AND user_id=? AND status='active'",
                (role, org_id, user_id),
            )
        return int(getattr(cur, "rowcount", 0) or 0) > 0

    def remove_org_member(self, org_id: str, user_id: str) -> bool:
        with self.tx() as c:
            cur = c.execute(
                "DELETE FROM organization_members WHERE org_id=? AND user_id=?",
                (org_id, user_id),
            )
            removed = int(getattr(cur, "rowcount", 0) or 0) > 0
            if removed:
                # 멤버십과 함께 grant도 정리 — 재초대 시 stale 권한이
                # 조용히 부활하는 것을 막는다.
                c.execute(
                    "DELETE FROM user_permissions WHERE org_id=? AND user_id=?",
                    (org_id, user_id),
                )
        return removed

    def transfer_org_ownership(self, org_id: str, from_user_id: str, to_user_id: str) -> bool:
        """소유권 이관 — 단일 tx로 양쪽 role 갱신. 반환: 성공 여부.
        두 번째 UPDATE가 실패하면 RuntimeError로 롤백 — from만 admin으로
        강등된 채 커밋돼 ownerless 조직이 되는 것을 막는다."""
        with self.tx() as c:
            cur = c.execute(
                "UPDATE organization_members SET role='org_admin'"
                " WHERE org_id=? AND user_id=? AND role='org_owner' AND status='active'",
                (org_id, from_user_id),
            )
            if int(getattr(cur, "rowcount", 0) or 0) == 0:
                return False
            cur = c.execute(
                "UPDATE organization_members SET role='org_owner'"
                " WHERE org_id=? AND user_id=? AND status='active'",
                (org_id, to_user_id),
            )
            if int(getattr(cur, "rowcount", 0) or 0) == 0:
                raise OrgTransferError("transfer target is not an active org member")
            return True

    def orgs_blocking_owner_delete(self, user_id: str) -> list[dict[str, Any]]:
        """계정 삭제를 막아야 하는 조직 — 사용자가 유일 org_owner이고
        kind='personal'이 아닌 조직. 개인 워크스페이스는 계정 삭제가
        함께 정리하므로 차단하지 않는다."""
        rows = self._conn.execute(
            "SELECT o.* FROM organization_members m"
            " JOIN organizations o ON o.id = m.org_id"
            " WHERE m.user_id=? AND m.role='org_owner' AND m.status='active'"
            " AND o.status != 'deleted' AND o.kind != 'personal'",
            (user_id,),
        ).fetchall()
        return [self._org_json(r) for r in rows if self.count_org_owners(str(r["id"])) <= 1]

    # ---- 개인 워크스페이스 ----

    def create_personal_org(self, user: dict[str, Any]) -> str | None:
        """가입 시 개인 워크스페이스 자동 생성(FR-003).
        이미 personal 조직이 있으면 그 id를 반환(멱등)."""
        uid = str(user.get("id") or "")
        if not uid:
            return None
        for o in self.list_user_orgs(uid):
            if o.get("kind") == "personal":
                return o["id"]
        email = str(user.get("email") or "")
        base_name = str(user.get("display_name") or "").strip()
        if not base_name:
            base_name = email.split("@")[0] if email else "workspace"
        name = f"{base_name}의 워크스페이스"[:80]
        slug = self._unique_org_slug(f"u-{uid[:12]}")
        oid = self.create_organization(name, slug, kind="personal", created_by=uid)
        if oid is None:
            return None
        self.add_org_member(oid, uid, "org_owner")
        return oid

    def user_default_org_id(self, user_id: str) -> str:
        """세션 조직 컨텍스트 기본값 — 첫 번째 활성 멤버십 조직."""
        orgs = self.list_user_orgs(user_id)
        return str(orgs[0]["id"]) if orgs else ""

    # ---- org_invitations (초대 플로우, spec 003 US2) ----

    @staticmethod
    def _invite_json(r: Any) -> dict[str, Any]:
        return {
            "id": r["id"],
            "org_id": r["org_id"],
            "email": r["email"],
            "role": r["role"],
            "status": r["status"],
            "invited_by": r["invited_by"],
            "expires_at": r["expires_at"],
            "used_at": r["used_at"],
            "created_at": r["created_at"],
        }

    def create_org_invitation(
        self,
        org_id: str,
        email: str,
        role: str,
        invited_by: str,
        ttl_hours: int = 72,
    ) -> tuple[str, str] | None:
        """초대 발급 — 반환 (invitation_id, raw_token). DB에는 SHA-256 해시만 저장.
        같은 (org,email)의 기존 pending 토큰은 모두 무효화한다(FR-010 resend 패턴).
        role은 org_admin|member만 허용 — org_owner는 이관으로만 부여(FR-007)."""
        if role not in ("org_admin", "member"):
            raise ValueError(f"invalid invite role: {role}")
        import hashlib
        import secrets

        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        now = _now()
        email_l = email.strip().lower()
        inv_id = uuid.uuid4().hex[:16]
        try:
            with self.tx() as c:
                c.execute(
                    "UPDATE org_invitations SET used_at=?"
                    " WHERE org_id=? AND email=? AND status='pending'",
                    (_iso(now), org_id, email_l),
                )
                c.execute(
                    "INSERT INTO org_invitations"
                    "(id,org_id,email,role,token_hash,status,invited_by,"
                    "expires_at,used_at,created_at)"
                    " VALUES(?,?,?,?,?,'pending',?,?,NULL,?)",
                    (
                        inv_id,
                        org_id,
                        email_l,
                        role,
                        token_hash,
                        invited_by,
                        _iso(now + timedelta(hours=ttl_hours)),
                        _iso(now),
                    ),
                )
        except self._integrity_errors:
            return None
        return inv_id, token

    def get_org_invitation(self, inv_id: str) -> dict[str, Any] | None:
        r = self._conn.execute("SELECT * FROM org_invitations WHERE id=?", (inv_id,)).fetchone()
        return self._invite_json(r) if r else None

    def get_invitation_by_hash(self, token_hash: str) -> dict[str, Any] | None:
        r = self._conn.execute(
            "SELECT * FROM org_invitations WHERE token_hash=?", (token_hash,)
        ).fetchone()
        return self._invite_json(r) if r else None

    def list_org_invitations(
        self, org_id: str, status: str | None = "pending"
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM org_invitations WHERE org_id=?"
        args: list[Any] = [org_id]
        if status == "pending":
            # 재발송/생성으로 무효화된 행(used_at 마킹·status 잔존)을 제외
            sql += " AND status='pending' AND used_at IS NULL"
        elif status:
            sql += " AND status=?"
            args.append(status)
        rows = self._conn.execute(sql + " ORDER BY created_at DESC", args).fetchall()
        return [self._invite_json(r) for r in rows]

    def invitation_expired(self, inv: dict[str, Any]) -> bool:
        exp = _dt(str(inv.get("expires_at") or ""))
        return exp is None or exp <= _now()

    def mark_invitation_expired(self, inv_id: str) -> bool:
        with self.tx() as c:
            cur = c.execute(
                "UPDATE org_invitations SET status='expired' WHERE id=? AND status='pending'",
                (inv_id,),
            )
        return int(getattr(cur, "rowcount", 0) or 0) > 0

    def consume_org_invitation(self, token_hash: str) -> dict[str, Any] | None:
        """토큰 원자적 consume — 유효(pending·미사용·미만료)하면 used_at 마킹 후 행 반환.
        consume_email_verification과 동일한 rowcount 직렬화 패턴."""
        now = _now()
        with self.tx() as c:
            cur = c.execute(
                "UPDATE org_invitations SET used_at=?, status='accepted'"
                " WHERE token_hash=? AND status='pending' AND used_at IS NULL"
                " AND expires_at>?",
                (_iso(now), token_hash, _iso(now)),
            )
            if int(getattr(cur, "rowcount", 0) or 0) <= 0:
                return None
            r = c.execute(
                "SELECT * FROM org_invitations WHERE token_hash=?", (token_hash,)
            ).fetchone()
            return self._invite_json(r) if r else None

    def decline_org_invitation(self, token_hash: str) -> bool:
        """수신자 거절 — pending·미만료 토큰만 declined로 전이(원자적)."""
        now = _now()
        with self.tx() as c:
            cur = c.execute(
                "UPDATE org_invitations SET status='declined', used_at=?"
                " WHERE token_hash=? AND status='pending' AND used_at IS NULL"
                " AND expires_at>?",
                (_iso(now), token_hash, _iso(now)),
            )
        return int(getattr(cur, "rowcount", 0) or 0) > 0

    def revoke_org_invitation(self, org_id: str, inv_id: str) -> bool:
        """초대 취소 — pending만 revoked로 전이."""
        with self.tx() as c:
            cur = c.execute(
                "UPDATE org_invitations SET status='revoked'"
                " WHERE id=? AND org_id=? AND status='pending' AND used_at IS NULL",
                (inv_id, org_id),
            )
        return int(getattr(cur, "rowcount", 0) or 0) > 0

    def latest_invitation_at(self, org_id: str, email: str) -> Any:
        """(org,email)의 가장 최근 초대 발급 시각 — 재발송 rate limit용."""
        r = self._conn.execute(
            "SELECT created_at FROM org_invitations WHERE org_id=? AND email=?"
            " ORDER BY created_at DESC LIMIT 1",
            (org_id, email.strip().lower()),
        ).fetchone()
        return _dt(str(r["created_at"])) if r else None

    def delete_expired_invitations(self, now: Any = None) -> int:
        """만료된 pending 초대를 expired로 전이 + 오래된 종료 행 정리.
        반환: 만료 처리된 행 수."""
        cutoff = _iso(now or _now())
        with self.tx() as c:
            cur = c.execute(
                "UPDATE org_invitations SET status='expired'"
                " WHERE status='pending' AND expires_at<=?",
                (cutoff,),
            )
            n = int(getattr(cur, "rowcount", 0) or 0)
            c.execute(
                "DELETE FROM org_invitations WHERE expires_at<? AND status != 'pending'",
                (cutoff,),
            )
        return n
