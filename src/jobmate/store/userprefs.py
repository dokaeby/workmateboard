# mypy: disable-error-code="attr-defined"
"""사용자별 대시보드 설정/저장 뷰/온보딩 상태 스토어 믹스인 (spec 006 US5/US6).

세 테이블 모두 사용자 레벨 — org_id가 없고 `_ORG_SCOPED_TABLES`에도 등록하지
않는다(다른 사용자의 행은 user_id 조건으로만 격리된다):

- ``user_dashboard_prefs(user_id PK, cards_json, updated_at)``
- ``user_views(id, user_id, name, layout_json, is_default, created_at, updated_at)``
  — ``UNIQUE(user_id, name)``
- ``user_onboarding(user_id PK, dismissed, completed_at, checklist_json, updated_at)``

``cards_json`` 내부 포맷은 ``{"v": 1, "cards": {card_id: {order, visible}},
"active_view_id": str|null}``다. ``"v"`` 마커로 래퍼와 카드 id ``"cards"``를
포함할 수 있는 raw card map을 구분한다 — raw map도 읽기는 허용한다.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from .util import _iso, _now

_UNSET: Any = object()


class UserPrefsStoreMixin:
    # ---- user_dashboard_prefs (spec 006 US5 / FR-021, FR-023) ----

    @staticmethod
    def _dashboard_prefs_load(raw: Any) -> dict[str, Any]:
        """cards_json → {"cards": {...}, "active_view_id": str|None}."""
        try:
            data = json.loads(str(raw or "{}"))
        except (json.JSONDecodeError, TypeError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        if data.get("v") == 1 and isinstance(data.get("cards"), dict):
            active = data.get("active_view_id")
            return {
                "cards": data["cards"],
                "active_view_id": str(active) if active else None,
            }
        # 레거시/raw card map — {"<card_id>": {"order":..,"visible":..}}
        return {"cards": data, "active_view_id": None}

    def get_dashboard_prefs(self, user_id: str) -> dict[str, Any] | None:
        """사용자 대시보드 설정. 행이 없으면 None(호출자가 전역 prefs로 폴백)."""
        row = self._conn.execute(
            "SELECT cards_json, updated_at FROM user_dashboard_prefs WHERE user_id=?",
            (user_id,),
        ).fetchone()
        if row is None:
            return None
        st = self._dashboard_prefs_load(row["cards_json"])
        st["updated_at"] = row["updated_at"]
        return st

    def _write_dashboard_prefs(
        self, user_id: str, cards: dict[str, Any], active_view_id: str | None
    ) -> dict[str, Any]:
        now = _iso(_now())
        payload = json.dumps({"v": 1, "cards": cards, "active_view_id": active_view_id})
        with self.tx() as c:
            c.execute(
                "INSERT INTO user_dashboard_prefs(user_id,cards_json,updated_at)"
                " VALUES(?,?,?)"
                " ON CONFLICT(user_id) DO UPDATE SET"
                " cards_json=excluded.cards_json, updated_at=excluded.updated_at",
                (user_id, payload, now),
            )
        return self.get_dashboard_prefs(user_id) or {"cards": cards, "active_view_id": None}

    def set_dashboard_cards(self, user_id: str, cards: dict[str, Any]) -> dict[str, Any]:
        """수동 카드 편집 저장 — 적용 뷰와 무관해지므로 active_view_id를 비운다."""
        return self._write_dashboard_prefs(user_id, cards, None)

    def apply_dashboard_view(
        self, user_id: str, view_id: str, layout: dict[str, Any]
    ) -> dict[str, Any]:
        """뷰 적용 — 카드 맵을 뷰 layout으로 교체하고 active_view_id를 기록한다."""
        return self._write_dashboard_prefs(user_id, layout, view_id)

    def clear_dashboard_active_view(self, user_id: str, view_id: str) -> None:
        """삭제된 뷰가 active로 남지 않게 active_view_id를 비운다(해당 뷰일 때만)."""
        prefs = self.get_dashboard_prefs(user_id)
        if prefs is None or prefs.get("active_view_id") != view_id:
            return
        self._write_dashboard_prefs(user_id, prefs.get("cards") or {}, None)

    # ---- user_views (spec 006 US5 / FR-022) ----

    @staticmethod
    def _user_view_json(r: Any) -> dict[str, Any]:
        try:
            layout = json.loads(r["layout_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            layout = {}
        if not isinstance(layout, dict):
            layout = {}
        return {
            "id": r["id"],
            "name": r["name"],
            "layout": layout,
            "is_default": bool(r["is_default"]),
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
        }

    def list_user_views(self, user_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM user_views WHERE user_id=? ORDER BY created_at, id",
            (user_id,),
        ).fetchall()
        return [self._user_view_json(r) for r in rows]

    def get_user_view(self, user_id: str, view_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM user_views WHERE user_id=? AND id=?",
            (user_id, view_id),
        ).fetchone()
        return self._user_view_json(row) if row else None

    def user_view_name_exists(self, user_id: str, name: str, exclude_id: str = "") -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM user_views WHERE user_id=? AND name=? AND id<>? LIMIT 1",
            (user_id, name, exclude_id),
        ).fetchone()
        return row is not None

    def create_user_view(
        self,
        user_id: str,
        name: str,
        layout: dict[str, Any],
        is_default: bool = False,
    ) -> dict[str, Any] | None:
        """뷰 생성. 이름 중복(UNIQUE) 시 None — 라우터가 409로 매핑."""
        vid = uuid.uuid4().hex[:12]
        now = _iso(_now())
        try:
            with self.tx() as c:
                if is_default:
                    c.execute(
                        "UPDATE user_views SET is_default=0 WHERE user_id=?",
                        (user_id,),
                    )
                c.execute(
                    "INSERT INTO user_views(id,user_id,name,layout_json,is_default,"
                    "created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        vid,
                        user_id,
                        name,
                        json.dumps(layout),
                        1 if is_default else 0,
                        now,
                        now,
                    ),
                )
        except self._integrity_errors:
            return None
        return self.get_user_view(user_id, vid)

    def update_user_view(
        self,
        user_id: str,
        view_id: str,
        *,
        name: Any = _UNSET,
        layout: Any = _UNSET,
        is_default: Any = _UNSET,
    ) -> dict[str, Any] | None:
        """뷰 부분 갱신. 행이 없거나 이름 충돌(UNIQUE) 시 None.

        is_default=True이면 같은 사용자의 다른 뷰 기본 플래그를 먼저 내린다
        (사용자당 기본 뷰는 최대 1개)."""
        sets: list[str] = []
        params: list[Any] = []
        if name is not _UNSET:
            sets.append("name=?")
            params.append(name)
        if layout is not _UNSET:
            sets.append("layout_json=?")
            params.append(json.dumps(layout))
        if is_default is not _UNSET:
            sets.append("is_default=?")
            params.append(1 if is_default else 0)
        if not sets:
            return self.get_user_view(user_id, view_id)
        sets.append("updated_at=?")
        params.append(_iso(_now()))
        params.extend([user_id, view_id])
        try:
            with self.tx() as c:
                if is_default is True:
                    c.execute(
                        "UPDATE user_views SET is_default=0 WHERE user_id=? AND id<>?",
                        (user_id, view_id),
                    )
                cur = c.execute(
                    "UPDATE user_views SET "  # noqa: S608 — sets는 내부 리터럴만
                    + ", ".join(sets)
                    + " WHERE user_id=? AND id=?",
                    tuple(params),
                )
        except self._integrity_errors:
            return None
        if getattr(cur, "rowcount", 0) < 1:
            return None
        return self.get_user_view(user_id, view_id)

    def delete_user_view(self, user_id: str, view_id: str) -> bool:
        with self.tx() as c:
            cur = c.execute(
                "DELETE FROM user_views WHERE user_id=? AND id=?",
                (user_id, view_id),
            )
        return int(getattr(cur, "rowcount", 0) or 0) > 0

    # ---- user_onboarding (spec 006 US6 / FR-029~FR-031) ----

    def get_onboarding_state(self, user_id: str) -> dict[str, Any]:
        """온보딩 상태. 행이 없으면 dismissed=False/completed_at=None 기본값."""
        row = self._conn.execute(
            "SELECT dismissed, completed_at, checklist_json, updated_at"
            " FROM user_onboarding WHERE user_id=?",
            (user_id,),
        ).fetchone()
        if row is None:
            return {
                "dismissed": False,
                "completed_at": None,
                "checklist": {},
                "updated_at": None,
            }
        return {
            "dismissed": bool(row["dismissed"]),
            "completed_at": row["completed_at"],
            "checklist": _loads_dict(row["checklist_json"]),
            "updated_at": row["updated_at"],
        }

    def save_onboarding(
        self,
        user_id: str,
        *,
        dismissed: bool,
        completed_at: str | None,
        checklist: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """온보딩 상태 upsert — dismissed/completed_at을 저장하고
        checklist는 마지막 계산 스냅샷으로 남긴다(실시간 계산은 라우터 책임)."""
        now = _iso(_now())
        checklist_json = json.dumps(checklist or {})
        with self.tx() as c:
            c.execute(
                "INSERT INTO user_onboarding(user_id,dismissed,completed_at,"
                "checklist_json,updated_at) VALUES(?,?,?,?,?)"
                " ON CONFLICT(user_id) DO UPDATE SET"
                " dismissed=excluded.dismissed,"
                " completed_at=excluded.completed_at,"
                " checklist_json=excluded.checklist_json,"
                " updated_at=excluded.updated_at",
                (
                    user_id,
                    1 if dismissed else 0,
                    completed_at,
                    checklist_json,
                    now,
                ),
            )
        return self.get_onboarding_state(user_id)

    # ---- 온보딩 체크리스트 실시간 판정용 store 조회 (FR-031) ----

    def user_account_info(self, user_id: str) -> dict[str, Any]:
        """계정 연동 상태 — 비밀번호 해시/google_sub/email_verified 존재 여부."""
        row = self._conn.execute(
            "SELECT password_hash, google_sub, email_verified FROM users WHERE id=?",
            (user_id,),
        ).fetchone()
        if row is None:
            return {"has_password": False, "google_sub": "", "email_verified": False}
        return {
            "has_password": bool(row["password_hash"]),
            "google_sub": str(row["google_sub"] or ""),
            "email_verified": bool(row["email_verified"]),
        }

    def user_has_run(self, user_id: str, email: str = "") -> bool:
        """첫 run 완료 여부 — runs.created_by는 행위자 라벨(email 우선,
        API 키/내부 경로는 'api-key'/'local' 등)이라 email과 user_id 둘 다 본다.
        org 필터를 걸지 않는다 — 온보딩은 사용자 레벨 집계다."""
        row = self._conn.execute(
            "SELECT 1 FROM runs WHERE created_by IN (?, ?) LIMIT 1",
            (email, user_id),
        ).fetchone()
        return row is not None


def _loads_dict(raw: Any) -> dict[str, Any]:
    try:
        v = json.loads(str(raw or "{}"))
    except (json.JSONDecodeError, TypeError):
        return {}
    return v if isinstance(v, dict) else {}
