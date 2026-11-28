# mypy: disable-error-code="attr-defined"
"""Agent permission 스토어 메서드 믹스인 (db.py Store에서 추출).

agent-management 3차 (paperclip spec D) — agent_permissions 영속화와
capability 판정용 조회. 스키마는 schema_sqlite.py/schema_pg.py에 이미 정의돼
있다(scope_id NOT NULL DEFAULT '' — UNIQUE NULL 회피).

판정 규칙(deny 우선, scope precedence, 기본 deny capability)은
services/permissions.py가 담당하고, 여기는 행 저장/조회만 한다.
"""

from __future__ import annotations

import uuid
from typing import Any

from ..models import AgentCapability, PermissionEffect, PermissionScopeType
from . import orgctx
from .util import _clamp_limit, _iso, _now


def _org_filter() -> tuple[str, list[Any]]:
    """ctx 조직이 있으면 ` AND org_id=?` 조건 — 시스템/미인증(ctx='')은 무필터."""
    oid = orgctx.current_org_id()
    return (" AND org_id=?", [oid]) if oid else ("", [])


# 저장 계층 방어선 — 서비스가 검증하지만 직접 store 호출도 어휘를 벗어나지 않게 한다.
_CAPABILITIES = {c.value for c in AgentCapability}
_EFFECTS = {e.value for e in PermissionEffect}
_SCOPE_TYPES = {s.value for s in PermissionScopeType}
_MAX_RULES = 200


class PermissionStoreMixin:
    """agent_permissions Store 메서드."""

    @staticmethod
    def _perm_json(r: Any) -> dict[str, Any]:
        return dict(r)

    @staticmethod
    def _normalize_perm_rule(rule: dict[str, Any]) -> dict[str, Any] | None:
        """rule dict → 정규화된 저장 형태. 어휘 위반/필드 부족 시 None."""
        cap = str(rule.get("capability") or "").strip().lower()[:60]
        eff = str(rule.get("effect") or "").strip().lower()[:10]
        st = str(rule.get("scope_type") or PermissionScopeType.GLOBAL.value).strip().lower()[:10]
        sid = str(rule.get("scope_id") or "").strip()[:200]
        if cap not in _CAPABILITIES or eff not in _EFFECTS or st not in _SCOPE_TYPES:
            return None
        if st == PermissionScopeType.GLOBAL.value:
            sid = ""
        elif not sid:
            return None
        return {
            "capability": cap,
            "effect": eff,
            "scope_type": st,
            "scope_id": sid,
        }

    def list_agent_permissions(
        self,
        agent_id: str,
        *,
        capability: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """agent의 permission 규칙 — capability 필터 선택, 최신순."""
        frag, params = _org_filter()
        sql = "SELECT * FROM agent_permissions WHERE agent_id=?" + frag  # noqa: S608
        params = [agent_id.strip()[:64], *params]
        if capability:
            sql += " AND capability=?"
            params.append(capability.strip().lower()[:60])
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(_clamp_limit(limit, 500, 1000))
        return [self._perm_json(r) for r in self._conn.execute(sql, params).fetchall()]

    def replace_agent_permissions(
        self, agent_id: str, rules: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """agent의 permission 규칙 전체 교체 — DELETE + INSERT 단일 tx.

        PUT semantics — 기존 행을 모두 지우고 전달된 집합만 남긴다.
        규칙별 검증(capability/effect/scope 어휘, scope_id 필수)은
        서비스 계층이 먼저 수행하고, 여기서는 정규화 실패 규칙을
        방어적으로 건너뛴다. 반환값은 저장된 행 목록."""
        aid = agent_id.strip()[:64]
        now = _iso(_now())
        # org 귀속은 부모 agent 행에서 상속 — ctx=''인 내부/스케줄 경로에서도
        # 올바른 조직으로 기록된다. agent가 없으면 ctx/명시값으로 폴백.
        oid = ""
        arow = self._conn.execute("SELECT org_id FROM agents WHERE id=?", (aid,)).fetchone()
        if arow is not None:
            oid = str(arow["org_id"] or "")
        else:
            oid = orgctx.org_for_write()
        norm: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str, str]] = set()
        for rule in rules[:_MAX_RULES]:
            n = self._normalize_perm_rule(rule if isinstance(rule, dict) else {})
            if n is None:
                continue
            key = (n["capability"], n["scope_type"], n["scope_id"], n["effect"])
            if key in seen:
                continue
            seen.add(key)
            norm.append(n)
        with self.tx() as c:
            # org_id='' 레거시 행도 함께 정리 — agent_id는 전역 unique라
            # 같은 agent의 규칙만 지워진다(타 조직 행은 존재할 수 없음).
            c.execute(
                "DELETE FROM agent_permissions WHERE agent_id=? AND org_id IN (?, '')",
                (aid, oid),
            )
            for n in norm:
                c.execute(
                    "INSERT INTO agent_permissions(id,agent_id,capability,effect,"
                    "scope_type,scope_id,created_at,org_id) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        uuid.uuid4().hex[:12],
                        aid,
                        n["capability"],
                        n["effect"],
                        n["scope_type"],
                        n["scope_id"],
                        now,
                        oid,
                    ),
                )
        return self.list_agent_permissions(aid)

    def list_user_permissions(
        self,
        user_id: str,
        *,
        capability: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """user의 permission 규칙 — capability 필터 선택, 최신순."""
        frag, params = _org_filter()
        sql = "SELECT * FROM user_permissions WHERE user_id=?" + frag  # noqa: S608
        params = [user_id.strip()[:64], *params]
        if capability:
            sql += " AND capability=?"
            params.append(capability.strip().lower()[:60])
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(_clamp_limit(limit, 500, 1000))
        return [self._perm_json(r) for r in self._conn.execute(sql, params).fetchall()]

    def replace_user_permissions(
        self,
        user_id: str,
        rules: list[dict[str, Any]],
        *,
        scope_type: str = "",
        scope_id: str = "",
    ) -> list[dict[str, Any]]:
        """user의 permission 규칙 전체 교체 — DELETE + INSERT 단일 tx.

        PUT semantics — 기존 행을 모두 지우고 전달된 집합만 남긴다.
        scope_type/scope_id를 함께 지정하면 해당 scope의 행만 교체한다
        (프로젝트 권한 엔드포인트가 다른 scope의 규칙을 건드리지 않게).
        user_permissions는 부모 행이 없어 org 귀속은 요청 컨텍스트에서
        가져온다(orgctx.org_for_write — ctx=''면 org_id=''로 기록).
        DELETE는 ctx 조직 행만 지운다 — agent_permissions와 달리
        org_id='' 레거시 병합은 하지 않는다(user perms는 항상 조직
        스탬프가 원칙). 반환값은 저장된 행 목록."""
        uid = user_id.strip()[:64]
        now = _iso(_now())
        oid = orgctx.org_for_write()
        norm: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str, str]] = set()
        for rule in rules[:_MAX_RULES]:
            n = self._normalize_perm_rule(rule if isinstance(rule, dict) else {})
            if n is None:
                continue
            key = (n["capability"], n["scope_type"], n["scope_id"], n["effect"])
            if key in seen:
                continue
            seen.add(key)
            norm.append(n)
        del_sql = "DELETE FROM user_permissions WHERE user_id=? AND org_id=?"
        del_params: list[Any] = [uid, oid]
        if scope_type and scope_id:
            del_sql += " AND scope_type=? AND scope_id=?"
            del_params += [scope_type, scope_id]
        with self.tx() as c:
            c.execute(del_sql, del_params)
            for n in norm:
                c.execute(
                    "INSERT INTO user_permissions(id,user_id,capability,effect,"
                    "scope_type,scope_id,created_at,org_id) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        uuid.uuid4().hex[:12],
                        uid,
                        n["capability"],
                        n["effect"],
                        n["scope_type"],
                        n["scope_id"],
                        now,
                        oid,
                    ),
                )
        return self.list_user_permissions(uid)

    def delete_user_permissions(
        self, user_id: str, *, scope_type: str = "", scope_id: str = ""
    ) -> int:
        """ctx 조직의 user permission 규칙 삭제 — 삭제된 행 수 반환.

        scope_type/scope_id를 함께 지정하면 해당 scope의 행만 지운다.
        ctx 조직이 비어 있으면 org_id='' 행만 지운다(타 조직 행은 건드리지
        않는다 — `org_id IN (?, '')` 패턴은 ctx=''일 때 전 조직 삭제가 되므로
        사용하지 않는다)."""
        uid = user_id.strip()[:64]
        oid = orgctx.current_org_id()
        sql = "DELETE FROM user_permissions WHERE user_id=? AND org_id=?"
        params: list[Any] = [uid, oid]
        if scope_type and scope_id:
            sql += " AND scope_type=? AND scope_id=?"
            params += [scope_type, scope_id]
        cur = self._conn.execute(sql, params)
        return int(getattr(cur, "rowcount", 0) or 0)
