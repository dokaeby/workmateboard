"""Organization routes — spec 003 US1/US5.

조직 생성·조회·수정·삭제·스위치·소유권 이관 + 멤버 관리.
조직 역할(org_owner|org_admin|member)은 organization_members.role로 평가하고
플랫폼 역할(users.role)과 분리한다(FR-004).
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ...security import SESSION_COOKIE, hash_token
from ...store.orgs import ORG_ROLES, OrgTransferError, normalize_org_slug

logger = logging.getLogger("jobmate.api")

_ORG_NAME_MAX = 80
_ORG_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,47}$")
_ROLE_RANK = {"member": 0, "org_admin": 1, "org_owner": 2}


def register(app: FastAPI, ctx: Any) -> None:
    store = ctx.store
    err = ctx.err
    _session_user = ctx._session_user
    _remote_open_mode = ctx._remote_open_mode
    _rate_hit = ctx._rate_hit
    _EMAIL_RE = ctx._EMAIL_RE
    _send_org_invite = ctx.send_org_invite

    def _session_or_401(request: Request) -> dict[str, Any] | JSONResponse:
        user = _session_user(request)
        if user is None:
            return err(401, "unauthorized", "로그인이 필요합니다", request)
        return user

    def _org_or_404(org_id: str) -> dict[str, Any] | None:
        org = store.get_organization(org_id)
        if org is None or org.get("status") == "deleted":
            return None
        return org

    def _membership(user: dict[str, Any], org_id: str) -> dict[str, Any] | None:
        return store.get_org_membership(org_id, str(user["id"]))

    def _role_at_least(m: dict[str, Any] | None, min_role: str) -> bool:
        return m is not None and _ROLE_RANK.get(str(m.get("role")), -1) >= _ROLE_RANK[min_role]

    def _suspended_write(request: Request, org: dict[str, Any]) -> JSONResponse | None:
        """suspended 조직의 모든 쓰기 차단 — owner도 읽기 전용(FR-031)."""
        if org.get("status") == "suspended":
            return err(403, "org_suspended", "정지된 조직입니다", request)
        return None

    def _suspended_read(
        request: Request, org: dict[str, Any], m: dict[str, Any]
    ) -> JSONResponse | None:
        """suspended 조직 읽기 — org_owner만 허용, 일반 멤버는 403(FR-031)."""
        if org.get("status") == "suspended" and m.get("role") != "org_owner":
            return err(403, "org_suspended", "정지된 조직입니다", request)
        return None

    def _audit(actor: str, action: str, org_id: str, detail: dict[str, Any]) -> None:
        try:
            store.audit("", actor, action, org_id, detail)
        except Exception:
            logger.debug("org audit failed: %s", action, exc_info=True)

    _ORG_CREATE_BUCKET: dict[str, list[float]] = {}  # 사용자당 시간당 조직 생성 상한

    # ---- 조직 CRUD ----

    @app.post("/api/v1/orgs", status_code=201)
    def org_create(body: dict[str, Any], request: Request) -> JSONResponse:
        """조직 생성 — 호출자는 org_owner가 된다. slug 생략 시 name에서 생성."""
        if _remote_open_mode(request):
            return err(403, "forbidden", "이 환경에서는 사용할 수 없습니다", request)
        user = _session_or_401(request)
        if isinstance(user, JSONResponse):
            return user
        if _rate_hit(_ORG_CREATE_BUCKET, str(user["id"]), 3600.0, 20):
            return err(429, "too_many_attempts", "조직 생성 요청이 너무 많습니다", request)
        name = str(body.get("name", "")).strip()[:_ORG_NAME_MAX]
        if not name:
            return err(422, "invalid_name", "조직 이름을 입력하세요", request)
        slug_raw = str(body.get("slug", "")).strip() or name
        slug = normalize_org_slug(slug_raw)
        if not slug or not _ORG_SLUG_RE.match(slug):
            return err(
                422,
                "invalid_slug",
                "슬러그는 소문자 영숫자·하이픈 3~48자여야 합니다",
                request,
            )
        oid = store.create_organization(name, slug, kind="team", created_by=user["id"])
        if oid is None:
            return err(409, "slug_taken", "이미 사용 중인 슬러그입니다", request)
        store.add_org_member(oid, user["id"], "org_owner")
        _audit(str(user.get("email") or "-"), "org_created", oid, {"slug": slug})
        logger.info("org created: id=%s slug=%s by=%s", oid, slug, user["id"])
        org = store.get_organization(oid)
        return JSONResponse({"org": org, "my_role": "org_owner"}, status_code=201)

    @app.get("/api/v1/orgs")
    def org_list(request: Request) -> JSONResponse:
        """내 멤버십 조직 목록."""
        user = _session_or_401(request)
        if isinstance(user, JSONResponse):
            return user
        return JSONResponse({"orgs": store.list_user_orgs(user["id"])})

    @app.get("/api/v1/orgs/{org_id}")
    def org_get(org_id: str, request: Request) -> JSONResponse:
        user = _session_or_401(request)
        if isinstance(user, JSONResponse):
            return user
        org = _org_or_404(org_id)
        m = _membership(user, org_id) if org else None
        if org is None or m is None:
            return err(404, "not_found", "조직을 찾을 수 없습니다", request)
        blocked = _suspended_read(request, org, m)
        if blocked is not None:
            return blocked
        return JSONResponse({"org": org, "my_role": m["role"]})

    @app.patch("/api/v1/orgs/{org_id}")
    def org_update(org_id: str, body: dict[str, Any], request: Request) -> JSONResponse:
        """이름/슬러그 변경 — org_admin 이상."""
        user = _session_or_401(request)
        if isinstance(user, JSONResponse):
            return user
        org = _org_or_404(org_id)
        m = _membership(user, org_id) if org else None
        if org is None or m is None:
            return err(404, "not_found", "조직을 찾을 수 없습니다", request)
        if not _role_at_least(m, "org_admin"):
            return err(403, "forbidden", "조직 관리자 권한이 필요합니다", request)
        blocked = _suspended_write(request, org)
        if blocked is not None:
            return blocked
        name = body.get("name")
        slug = body.get("slug")
        new_name = str(name).strip()[:_ORG_NAME_MAX] if name is not None else None
        new_slug = None
        if slug is not None:
            new_slug = normalize_org_slug(str(slug))
            if not new_slug or not _ORG_SLUG_RE.match(new_slug):
                return err(
                    422,
                    "invalid_slug",
                    "슬러그는 소문자 영숫자·하이픈 3~48자여야 합니다",
                    request,
                )
        if new_name is not None and not new_name:
            return err(422, "invalid_name", "조직 이름을 입력하세요", request)
        result = store.update_organization(org_id, name=new_name, slug=new_slug)
        if result is None:
            return err(409, "slug_taken", "이미 사용 중인 슬러그입니다", request)
        if not result:
            return err(404, "not_found", "조직을 찾을 수 없습니다", request)
        _audit(
            str(user.get("email") or "-"),
            "org_updated",
            org_id,
            {"name": new_name, "slug": new_slug},
        )
        return JSONResponse({"org": store.get_organization(org_id)})

    @app.delete("/api/v1/orgs/{org_id}")
    def org_delete(org_id: str, request: Request) -> JSONResponse:
        """조직 soft-delete — org_owner만. 멤버 2명 이상이면 먼저 제거 요구(FR-028)."""
        user = _session_or_401(request)
        if isinstance(user, JSONResponse):
            return user
        org = _org_or_404(org_id)
        m = _membership(user, org_id) if org else None
        if org is None or m is None:
            return err(404, "not_found", "조직을 찾을 수 없습니다", request)
        if m["role"] != "org_owner":
            return err(403, "forbidden", "소유자만 삭제할 수 있습니다", request)
        if org.get("kind") == "personal":
            return err(
                409,
                "personal_org",
                "개인 워크스페이스는 계정 삭제로 정리됩니다",
                request,
            )
        blocked = _suspended_write(request, org)
        if blocked is not None:
            return blocked
        members = [mm for mm in store.list_org_members(org_id) if mm.get("status") == "active"]
        if len(members) > 1:
            return err(
                409,
                "org_has_members",
                "멤버를 먼저 모두 제거해야 합니다",
                request,
                extra={"member_count": len(members)},
            )
        # 활성 run 차단 검사 — runs.org_id는 US3에서 도입, 도입 전까지
        # org 스코프 run이 없으므로 생략(US3에서 org_has_active_runs 추가)
        store.set_organization_status(org_id, "deleted")
        for mm in store.list_org_members(org_id):
            store.clear_session_org_context(str(mm["user_id"]), org_id)
        _audit(str(user.get("email") or "-"), "org_deleted", org_id, {})
        logger.info("org deleted: id=%s by=%s", org_id, user["id"])
        return JSONResponse({"status": "deleted"})

    @app.post("/api/v1/orgs/{org_id}/switch")
    def org_switch(org_id: str, request: Request) -> JSONResponse:
        """세션의 현재 조직 전환 — 멤버 + 활성 조직만."""
        user = _session_or_401(request)
        if isinstance(user, JSONResponse):
            return user
        org = _org_or_404(org_id)
        m = _membership(user, org_id) if org else None
        if org is None or m is None:
            return err(404, "not_found", "조직을 찾을 수 없습니다", request)
        if org.get("status") == "suspended" and m["role"] != "org_owner":
            return err(403, "org_suspended", "정지된 조직입니다", request)
        token = request.cookies.get(SESSION_COOKIE, "")
        if not token or not store.set_session_org(hash_token(token), org_id):
            return err(401, "unauthorized", "세션이 유효하지 않습니다", request)
        _audit(
            str(user.get("email") or "-"),
            "org_switched",
            org_id,
            {"from": user.get("current_org_id") or ""},
        )
        return JSONResponse({"org_id": org_id, "my_role": m["role"]})

    @app.post("/api/v1/orgs/{org_id}/transfer")
    def org_transfer(org_id: str, body: dict[str, Any], request: Request) -> JSONResponse:
        """소유권 이관 — 현재 org_owner만, 대상은 member 이상."""
        user = _session_or_401(request)
        if isinstance(user, JSONResponse):
            return user
        org = _org_or_404(org_id)
        m = _membership(user, org_id) if org else None
        if org is None or m is None:
            return err(404, "not_found", "조직을 찾을 수 없습니다", request)
        if m["role"] != "org_owner":
            return err(403, "forbidden", "소유자만 이관할 수 있습니다", request)
        blocked = _suspended_write(request, org)
        if blocked is not None:
            return blocked
        target_id = str(body.get("user_id", "")).strip()
        tm = _membership({"id": target_id}, org_id) if target_id else None
        if tm is None:
            return err(404, "not_found", "대상 사용자를 찾을 수 없습니다", request)
        # transfer와 last-owner 판정이 같은 임계구역을 공유하도록 exclusive로 묶는다
        with store.exclusive():
            try:
                transferred = store.transfer_org_ownership(org_id, user["id"], target_id)
            except OrgTransferError:
                transferred = False
        if not transferred:
            return err(409, "transfer_failed", "소유권 이관에 실패했습니다", request)
        _audit(
            str(user.get("email") or "-"),
            "org_ownership_transferred",
            org_id,
            {"from": user["id"], "to": target_id},
        )
        return JSONResponse({"status": "transferred", "new_owner": target_id})

    # ---- 멤버 관리 ----

    @app.get("/api/v1/orgs/{org_id}/members")
    def org_members(org_id: str, request: Request) -> JSONResponse:
        user = _session_or_401(request)
        if isinstance(user, JSONResponse):
            return user
        org = _org_or_404(org_id)
        m = _membership(user, org_id) if org else None
        if org is None or m is None:
            return err(404, "not_found", "조직을 찾을 수 없습니다", request)
        blocked = _suspended_read(request, org, m)
        if blocked is not None:
            return blocked
        return JSONResponse({"members": store.list_org_members(org_id)})

    def _guard_member_target(
        request: Request, user: dict[str, Any], org_id: str, target_uid: str
    ) -> tuple[dict[str, Any], dict[str, Any]] | JSONResponse:
        """대상 멤버십 + 호출자 org_admin+ 검증."""
        org = _org_or_404(org_id)
        m = _membership(user, org_id) if org else None
        if org is None or m is None:
            return err(404, "not_found", "조직을 찾을 수 없습니다", request)
        if not _role_at_least(m, "org_admin"):
            return err(403, "forbidden", "조직 관리자 권한이 필요합니다", request)
        blocked = _suspended_write(request, org)
        if blocked is not None:
            return blocked
        tm = store.get_org_membership(org_id, target_uid)
        if tm is None:
            return err(404, "not_found", "멤버를 찾을 수 없습니다", request)
        return m, tm

    @app.patch("/api/v1/orgs/{org_id}/members/{target_uid}")
    def org_member_role(
        org_id: str, target_uid: str, body: dict[str, Any], request: Request
    ) -> JSONResponse:
        """멤버 역할 변경 — org_admin+. org_owner 지정/해제는 transfer만."""
        user = _session_or_401(request)
        if isinstance(user, JSONResponse):
            return user
        role = str(body.get("role", "")).strip()
        if role not in ORG_ROLES or role == "org_owner":
            return err(
                422,
                "invalid_role",
                "role은 org_admin|member만 지정할 수 있습니다(org_owner는 이관 사용)",
                request,
            )
        guarded = _guard_member_target(request, user, org_id, target_uid)
        if isinstance(guarded, JSONResponse):
            return guarded
        _, _tm = guarded
        # 마지막 org_owner 강등 방지(FR-030) — count 확인과 변경을
        # exclusive 임계구역으로 묶고, 대상 role도 임계구역 안에서 재조회해
        # transfer 등 동시 변경과의 stale-read 레이스를 차단한다.
        with store.exclusive():
            tm = store.get_org_membership(org_id, target_uid)
            if tm is None:
                return err(404, "not_found", "멤버를 찾을 수 없습니다", request)
            if tm["role"] == "org_owner" and store.count_org_owners(org_id) <= 1:
                return err(
                    409,
                    "last_org_owner",
                    "마지막 소유자는 강등할 수 없습니다 — 먼저 소유권을 이관하세요",
                    request,
                )
            if not store.set_org_member_role(org_id, target_uid, role):
                return err(404, "not_found", "멤버를 찾을 수 없습니다", request)
        _audit(
            str(user.get("email") or "-"),
            "org_member_role_changed",
            org_id,
            {"user_id": target_uid, "role": role},
        )
        return JSONResponse({"status": "updated", "role": role})

    @app.delete("/api/v1/orgs/{org_id}/members/me")
    def org_leave(org_id: str, request: Request) -> JSONResponse:
        """본인 탈퇴 — 마지막 org_owner 보호 동일 적용.
        /members/{target_uid}보다 먼저 등록해야 'me'가 파라미터로 잡히지 않는다."""
        user = _session_or_401(request)
        if isinstance(user, JSONResponse):
            return user
        m = _membership(user, org_id)
        org = _org_or_404(org_id)
        if org is None or m is None:
            return err(404, "not_found", "조직을 찾을 수 없습니다", request)
        # suspended 조직에서도 탈퇴는 허용 — 멤버가 갇히지 않도록 하는 탈출 경로.
        # last-owner 판정은 exclusive 안에서 멤버십을 재조회해 stale-read 차단.
        with store.exclusive():
            m = store.get_org_membership(org_id, str(user["id"]))
            if m is None:
                return err(404, "not_found", "조직을 찾을 수 없습니다", request)
            if m["role"] == "org_owner" and store.count_org_owners(org_id) <= 1:
                return err(
                    409,
                    "last_org_owner",
                    "마지막 소유자는 탈퇴할 수 없습니다 — 먼저 소유권을 이관하세요",
                    request,
                )
            store.remove_org_member(org_id, user["id"])
        store.clear_session_org_context(str(user["id"]), org_id)
        _audit(str(user.get("email") or "-"), "org_leave", org_id, {})
        return JSONResponse({"status": "left"})

    @app.delete("/api/v1/orgs/{org_id}/members/{target_uid}")
    def org_member_remove(org_id: str, target_uid: str, request: Request) -> JSONResponse:
        """멤버 제거 — org_admin+. 마지막 org_owner 보호(FR-030)."""
        user = _session_or_401(request)
        if isinstance(user, JSONResponse):
            return user
        guarded = _guard_member_target(request, user, org_id, target_uid)
        if isinstance(guarded, JSONResponse):
            return guarded
        with store.exclusive():
            tm = store.get_org_membership(org_id, target_uid)
            if tm is None:
                return err(404, "not_found", "멤버를 찾을 수 없습니다", request)
            if tm["role"] == "org_owner" and store.count_org_owners(org_id) <= 1:
                return err(
                    409,
                    "last_org_owner",
                    "마지막 소유자는 제거할 수 없습니다 — 먼저 소유권을 이관하세요",
                    request,
                )
            store.remove_org_member(org_id, target_uid)
        store.clear_session_org_context(target_uid, org_id)
        _audit(str(user.get("email") or "-"), "org_member_removed", org_id, {"user_id": target_uid})
        return JSONResponse({"status": "removed"})

    # ---- 초대 (spec 003 US2, FR-007~FR-012) ----

    _INVITE_RESEND_MIN_INTERVAL = 600.0  # (org,email) 재발송 10분에 1회
    _INVITE_ORG_BUCKET: dict[str, list[float]] = {}  # 조직당 시간당 초대 상한
    _INVITE_IP_BUCKET: dict[str, list[float]] = {}  # IP 버스트 제한
    _INVITE_TOKEN_BUCKET: dict[str, list[float]] = {}  # 토큰당 accept/decline 시도 상한

    def _invite_admin_guard(
        request: Request, user: dict[str, Any], org_id: str, write: bool = True
    ) -> tuple[dict[str, Any], dict[str, Any]] | JSONResponse:
        """org_admin+ 게이트 + suspended 조직 쓰기 차단. (org, membership) 반환."""
        org = _org_or_404(org_id)
        if org is None:
            return err(404, "not_found", "조직을 찾을 수 없습니다", request)
        m = _membership(user, org_id)
        if not _role_at_least(m, "org_admin"):
            return err(403, "permission_denied", "조직 관리자 권한이 필요합니다", request)
        if write and org.get("status") == "suspended":
            return err(403, "org_suspended", "정지된 조직입니다", request)
        return org, m  # type: ignore[return-value]

    def _client_ip(request: Request) -> str:
        return request.client.host if request.client else "-"

    def _invite_by_token(request: Request, token: str) -> dict[str, Any] | JSONResponse:
        """토큰 해시 조회 + 토큰당 시도 rate limit."""
        th = hash_token(token)
        if _rate_hit(_INVITE_TOKEN_BUCKET, th[:16], 600.0, 20):
            return err(429, "too_many_attempts", "시도가 너무 많습니다", request)
        inv = store.get_invitation_by_hash(th)
        if inv is None:
            return err(404, "invite_not_found", "초대 링크가 올바르지 않습니다", request)
        return inv

    def _invite_public(inv: dict[str, Any], org: dict[str, Any] | None) -> dict[str, Any]:
        return {
            "email": inv["email"],
            "role": inv["role"],
            "status": inv["status"],
            "expires_at": inv["expires_at"],
            "org": {"id": inv["org_id"], "name": (org or {}).get("name", "")},
        }

    @app.get("/api/v1/orgs/{org_id}/invitations")
    def org_invitations_list(org_id: str, request: Request) -> JSONResponse:
        """대기 중인 초대 목록 — org_admin+ (읽기라 suspended여도 owner는 허용)."""
        user = _session_or_401(request)
        if isinstance(user, JSONResponse):
            return user
        org = _org_or_404(org_id)
        if org is None:
            return err(404, "not_found", "조직을 찾을 수 없습니다", request)
        m = _membership(user, org_id)
        if not _role_at_least(m, "org_admin"):
            return err(403, "permission_denied", "조직 관리자 권한이 필요합니다", request)
        if org.get("status") == "suspended" and (m is None or m.get("role") != "org_owner"):
            return err(403, "org_suspended", "정지된 조직입니다", request)
        invs = store.list_org_invitations(org_id, "pending")
        return JSONResponse({"invitations": invs})

    @app.post("/api/v1/orgs/{org_id}/invitations", status_code=201)
    def org_invite_create(org_id: str, body: dict[str, Any], request: Request) -> JSONResponse:
        """멤버 초대 — org_admin+. 토큰은 메일 링크에만 포함(FR-008)."""
        if _remote_open_mode(request):
            return err(403, "forbidden", "이 환경에서는 사용할 수 없습니다", request)
        user = _session_or_401(request)
        if isinstance(user, JSONResponse):
            return user
        guarded = _invite_admin_guard(request, user, org_id)
        if isinstance(guarded, JSONResponse):
            return guarded
        org, _ = guarded
        email = str(body.get("email", "")).strip().lower()
        if not _EMAIL_RE.match(email):
            return err(422, "invalid_email", "이메일 형식이 올바르지 않습니다", request)
        role = str(body.get("role", "member")).strip()
        if role not in ("member", "org_admin"):
            return err(
                422,
                "invalid_role",
                "초대 역할은 member 또는 org_admin만 가능합니다",
                request,
            )
        if _rate_hit(_INVITE_ORG_BUCKET, org_id, 3600.0, 30) or _rate_hit(
            _INVITE_IP_BUCKET, _client_ip(request), 3600.0, 60
        ):
            return err(429, "too_many_attempts", "초대 요청이 너무 많습니다", request)
        target = store.get_user_by_email(email)
        if target is not None and store.get_org_membership(org_id, str(target["id"])):
            return err(409, "already_member", "이미 조직 멤버입니다", request)
        created = store.create_org_invitation(
            org_id, email, role, str(user.get("email") or user["id"])
        )
        if created is None:
            return err(500, "invite_failed", "초대 생성에 실패했습니다", request)
        inv_id, token = created
        sent = _send_org_invite(request, org, email, role, token)
        inv = store.get_org_invitation(inv_id)
        _audit(
            str(user.get("email") or "-"),
            "org_invite_created",
            org_id,
            {"email": email, "role": role, "sent": sent},
        )
        return JSONResponse({"invitation": inv, "email_sent": sent}, status_code=201)

    @app.delete("/api/v1/orgs/{org_id}/invitations/{inv_id}")
    def org_invite_revoke(org_id: str, inv_id: str, request: Request) -> JSONResponse:
        """초대 취소 — org_admin+. pending만 revoked로 전이."""
        if _remote_open_mode(request):
            return err(403, "forbidden", "이 환경에서는 사용할 수 없습니다", request)
        user = _session_or_401(request)
        if isinstance(user, JSONResponse):
            return user
        guarded = _invite_admin_guard(request, user, org_id)
        if isinstance(guarded, JSONResponse):
            return guarded
        inv = store.get_org_invitation(inv_id)
        if inv is None or inv["org_id"] != org_id:
            return err(404, "not_found", "초대를 찾을 수 없습니다", request)
        if not store.revoke_org_invitation(org_id, inv_id):
            return err(409, "invite_not_pending", "대기 중인 초대가 아닙니다", request)
        _audit(
            str(user.get("email") or "-"),
            "org_invite_revoked",
            org_id,
            {"invitation_id": inv_id, "email": inv["email"]},
        )
        return JSONResponse({"status": "revoked"})

    @app.post("/api/v1/orgs/{org_id}/invitations/{inv_id}/resend")
    def org_invite_resend(org_id: str, inv_id: str, request: Request) -> JSONResponse:
        """초대 재발송 — 기존 pending 토큰 무효화 후 새 토큰 발급(FR-010).
        (org,email) 기준 10분 미만 재발송은 429 resend_too_soon."""
        if _remote_open_mode(request):
            return err(403, "forbidden", "이 환경에서는 사용할 수 없습니다", request)
        user = _session_or_401(request)
        if isinstance(user, JSONResponse):
            return user
        guarded = _invite_admin_guard(request, user, org_id)
        if isinstance(guarded, JSONResponse):
            return guarded
        org, _ = guarded
        inv = store.get_org_invitation(inv_id)
        if inv is None or inv["org_id"] != org_id:
            return err(404, "not_found", "초대를 찾을 수 없습니다", request)
        if inv["status"] != "pending" or inv.get("used_at"):
            return err(409, "invite_not_pending", "대기 중인 초대가 아닙니다", request)
        last = store.latest_invitation_at(org_id, inv["email"])
        if (
            last is not None
            and (datetime.now(UTC) - last).total_seconds() < _INVITE_RESEND_MIN_INTERVAL
        ):
            _audit(
                str(user.get("email") or "-"),
                "org_invite_resend_throttled",
                org_id,
                {"email": inv["email"]},
            )
            return err(429, "resend_too_soon", "재발송은 10분에 한 번만 가능합니다", request)
        if _rate_hit(_INVITE_IP_BUCKET, _client_ip(request), 3600.0, 60):
            return err(429, "too_many_attempts", "요청이 너무 많습니다", request)
        created = store.create_org_invitation(
            org_id, inv["email"], inv["role"], str(user.get("email") or user["id"])
        )
        if created is None:
            return err(500, "invite_failed", "초대 재발급에 실패했습니다", request)
        new_id, token = created
        sent = _send_org_invite(request, org, inv["email"], inv["role"], token)
        _audit(
            str(user.get("email") or "-"),
            "org_invite_resent",
            org_id,
            {"email": inv["email"], "role": inv["role"], "sent": sent},
        )
        return JSONResponse({"invitation": store.get_org_invitation(new_id), "email_sent": sent})

    # ---- 수신자 측 (세션 필요) ----
    # 토큰은 ?token= 쿼리로만 전달 — 접근 로그가 path만 기록해 토큰 원문이
    # 로그에 남지 않는다(NFR-005). path 방식은 로그 유출 경로가 된다.

    @app.get("/api/v1/invitations/info")
    def invite_info(token: str, request: Request) -> JSONResponse:
        """초대 정보 조회 — 로그인 전 확인용 공개 엔드포인트(최小 정보만)."""
        inv = _invite_by_token(request, token)
        if isinstance(inv, JSONResponse):
            return inv
        if inv["status"] == "pending" and store.invitation_expired(inv):
            store.mark_invitation_expired(str(inv["id"]))
            inv = store.get_invitation_by_hash(hash_token(token)) or inv
            _audit("-", "org_invite_expired", str(inv["org_id"]), {"invitation_id": inv["id"]})
        if inv["status"] == "pending" and inv.get("used_at"):
            # 재발송으로 무효화된 토큰 — status는 pending 잔존, used_at이 실효 상태
            inv = {**inv, "status": "revoked"}
        org = store.get_organization(str(inv["org_id"]))
        return JSONResponse(_invite_public(inv, org))

    @app.post("/api/v1/invitations/accept")
    def invite_accept(token: str, request: Request) -> JSONResponse:
        """초대 수락 — 세션 이메일이 초대 이메일과 정확히 일치해야 한다(FR-009)."""
        if _remote_open_mode(request):
            return err(403, "forbidden", "이 환경에서는 사용할 수 없습니다", request)
        user = _session_or_401(request)
        if isinstance(user, JSONResponse):
            return user
        inv = _invite_by_token(request, token)
        if isinstance(inv, JSONResponse):
            return inv
        org_id = str(inv["org_id"])
        if inv["status"] == "pending" and store.invitation_expired(inv):
            store.mark_invitation_expired(str(inv["id"]))
            _audit(
                str(user.get("email") or "-"),
                "org_invite_expired",
                org_id,
                {"invitation_id": inv["id"]},
            )
            return err(410, "invite_expired", "만료된 초대입니다", request)
        if inv["status"] == "expired":
            return err(410, "invite_expired", "만료된 초대입니다", request)
        if inv["status"] != "pending":
            return err(410, "invite_unavailable", "사용할 수 없는 초대입니다", request)
        if str(user.get("email") or "").lower() != str(inv["email"]).lower():
            _audit(
                str(user.get("email") or "-"),
                "org_invite_email_mismatch",
                org_id,
                {"invitation_id": inv["id"]},
            )
            return err(
                403,
                "invite_email_mismatch",
                "초대받은 이메일과 동일한 계정으로 로그인해야 합니다",
                request,
            )
        org = store.get_organization(org_id)
        if org is None or org.get("status") == "deleted":
            return err(404, "not_found", "조직을 찾을 수 없습니다", request)
        if org.get("status") == "suspended":
            return err(403, "org_suspended", "정지된 조직입니다", request)
        consumed = store.consume_org_invitation(hash_token(token))
        if consumed is None:
            return err(410, "invite_unavailable", "사용할 수 없는 초대입니다", request)
        uid = str(user["id"])
        existing = store.get_org_membership(org_id, uid)
        if existing is not None:
            return JSONResponse({"status": "already_member", "org_id": org_id})
        store.add_org_member(org_id, uid, str(consumed["role"]))
        _audit(
            str(user.get("email") or "-"),
            "org_invite_accepted",
            org_id,
            {"invitation_id": inv["id"], "role": consumed["role"]},
        )
        logger.info("org invite accepted: org=%s user=%s", org_id, uid)
        return JSONResponse({"status": "accepted", "org_id": org_id})

    @app.post("/api/v1/invitations/decline")
    def invite_decline(token: str, request: Request) -> JSONResponse:
        """초대 거절 — 세션 이메일 일치 필요. pending·미만료만 declined 전이."""
        if _remote_open_mode(request):
            return err(403, "forbidden", "이 환경에서는 사용할 수 없습니다", request)
        user = _session_or_401(request)
        if isinstance(user, JSONResponse):
            return user
        inv = _invite_by_token(request, token)
        if isinstance(inv, JSONResponse):
            return inv
        if str(user.get("email") or "").lower() != str(inv["email"]).lower():
            return err(
                403,
                "invite_email_mismatch",
                "초대받은 이메일과 동일한 계정으로 로그인해야 합니다",
                request,
            )
        if not store.decline_org_invitation(hash_token(token)):
            return err(410, "invite_unavailable", "사용할 수 없는 초대입니다", request)
        _audit(
            str(user.get("email") or "-"),
            "org_invite_declined",
            str(inv["org_id"]),
            {"invitation_id": inv["id"]},
        )
        return JSONResponse({"status": "declined"})
