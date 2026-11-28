"""/api/v1/me/* 라우트 — 사용자별 대시보드 설정/저장 뷰/온보딩 (spec 006 US5/US6).

모든 엔드포인트는 세션 사용자 전용이다 — API 키·로컬 오픈 모드만으로는
_session_user가 None이라 401을 반환한다(/api/v1/auth/me와 달리 pseudo
fallback을 두지 않는다 — per-user 저장소는 세션 사용자 id가 필요하다).

주요 결정 사항:
- GET /me/dashboard는 per-user 행이 없으면 전역 prefs.dashboard_widgets
  ([{id,visible,order}])를 {card_id:{order,visible}}로 변환해 폴백한다
  (per-user wins, fallback global — FR-021 읽기 호환).
- PATCH /me/dashboard는 부분 병합(merge) 시맨틱 — 언급된 card_id만 갱신하고
  나머지는 유지한다. 알 수 없는 card_id는 422.
- 뷰 layout 저장 시 알 수 없는 card_id는 거부하지 않고 제거한다
  (spec edge case: "삭제된 카드 id가 뷰에 남아있음 → 저장 시 정리").
- apply는 서버에 대시보드 렌더 상태가 없으므로(렌더링은 클라이언트) 뷰의
  layout을 user_dashboard_prefs에 기록(active_view_id 포함)하고
  {view, dashboard}를 반환한다 — 클라이언트는 dashboard.cards를 그대로 적용.
- 기본 뷰는 is_default=1인 뷰다(사용자당 최대 1개). is_default 뷰 삭제는
  400 — 삭제하려면 먼저 is_default=false로 내린다. 이름 "default"는 예약어로
  생성/변경 모두 422.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("jobmate.api")

# 레거시 대시보드 위젯 id (static/index.html·legacy/index.html의 DASH_WIDGETS와
# 동일 집합) — 여기에 org 스코프 dashboard_cards 행 id를 합쳐 known card 집합을
# 만든다.
_STATIC_CARD_IDS = frozenset({"ask", "inbox", "usage", "reports", "cards"})

_RESERVED_VIEW_NAME = "default"
_MAX_CARD_ENTRIES = 100
_MAX_VIEW_NAME = 80


def register(app: FastAPI, ctx: Any) -> None:
    store = ctx.store
    err = ctx.err
    _prefs = ctx._prefs
    _session_user = ctx._session_user
    _audit_api = ctx._audit_api

    # ---------- 공통 헬퍼 ----------

    def _known_card_ids() -> set[str]:
        ids = set(_STATIC_CARD_IDS)
        try:
            for c in store.list_cards():
                cid = str(c.get("id") or "")
                if cid:
                    ids.add(cid)
        except Exception:
            logger.debug("list_cards failed during card-id validation", exc_info=True)
        return ids

    def _clean_card_entry(raw: Any) -> tuple[dict[str, Any] | None, str | None]:
        """{order,visible} 엔트리 검증. (cleaned, error) 반환."""
        if not isinstance(raw, dict):
            return None, "card entry must be an object"
        out: dict[str, Any] = {}
        if "order" in raw:
            o = raw["order"]
            if isinstance(o, bool) or not isinstance(o, int):
                return None, "card order must be an integer"
            out["order"] = o
        if "visible" in raw:
            v = raw["visible"]
            if not isinstance(v, bool):
                return None, "card visible must be a boolean"
            out["visible"] = v
        if not out:
            return None, "card entry must set order and/or visible"
        return out, None

    def _normalize_cards(raw: Any) -> dict[str, dict[str, Any]]:
        """저장된 cards 맵을 {card_id:{order:int|None,visible:bool}}로 정규화."""
        out: dict[str, dict[str, Any]] = {}
        if not isinstance(raw, dict):
            return out
        for cid, entry in raw.items():
            if not isinstance(entry, dict):
                continue
            order = entry.get("order")
            visible = entry.get("visible")
            out[str(cid)] = {
                "order": order if isinstance(order, int) and not isinstance(order, bool) else None,
                "visible": bool(visible) if visible is not None else True,
            }
        return out

    def _global_widgets_fallback() -> dict[str, dict[str, Any]]:
        """전역 prefs.dashboard_widgets([{id,visible,order}]) → card map 변환."""
        cards: dict[str, dict[str, Any]] = {}
        try:
            widgets = _prefs().get("dashboard_widgets") or []
        except Exception:
            widgets = []
        if not isinstance(widgets, list):
            return cards
        for w in widgets:
            if not isinstance(w, dict) or not w.get("id"):
                continue
            order = w.get("order")
            cards[str(w["id"])] = {
                "order": order if isinstance(order, int) and not isinstance(order, bool) else None,
                "visible": w.get("visible") is not False,
            }
        return cards

    def _dashboard_payload(user_id: str) -> dict[str, Any]:
        prefs = store.get_dashboard_prefs(user_id)
        if prefs is not None:
            return {
                "cards": _normalize_cards(prefs.get("cards")),
                "active_view_id": prefs.get("active_view_id"),
                "updated_at": prefs.get("updated_at"),
            }
        return {
            "cards": _global_widgets_fallback(),
            "active_view_id": None,
            "updated_at": None,
        }

    def _view_json(view: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": view["id"],
            "name": view["name"],
            "layout": _normalize_cards(view.get("layout")),
            "is_default": bool(view.get("is_default")),
            "created_at": view.get("created_at"),
            "updated_at": view.get("updated_at"),
        }

    def _clean_view_layout(
        raw: Any,
    ) -> tuple[dict[str, dict[str, Any]] | None, list[str], str | None]:
        """뷰 layout 검증 — 알 수 없는 card_id는 제거(저장 시 정리),
        엔트리 형식 오류는 (None, [], err)로 거부."""
        if raw is None:
            return {}, [], None
        if not isinstance(raw, dict):
            return None, [], "layout must be an object"
        if len(raw) > _MAX_CARD_ENTRIES * 4:
            return None, [], "layout has too many entries"
        known = _known_card_ids()
        cleaned: dict[str, dict[str, Any]] = {}
        dropped: list[str] = []
        for cid, entry in raw.items():
            key = str(cid)
            if key not in known:
                dropped.append(key)
                continue
            ce, eerr = _clean_card_entry(entry)
            if eerr is not None:
                return None, [], f"layout.{key}: {eerr}"
            merged = dict(cleaned.get(key) or {})
            merged.update(ce or {})
            cleaned[key] = merged
        return cleaned, sorted(dropped), None

    # ---------- dashboard (FR-021/022/023) ----------

    @app.get("/api/v1/me/dashboard")
    def me_dashboard_get(request: Request) -> Any:
        user = _session_user(request)
        if user is None:
            return err(401, "unauthorized", "로그인이 필요합니다", request)
        return JSONResponse(_dashboard_payload(str(user["id"])))

    @app.patch("/api/v1/me/dashboard")
    def me_dashboard_patch(body: dict[str, Any], request: Request) -> Any:
        user = _session_user(request)
        if user is None:
            return err(401, "unauthorized", "로그인이 필요합니다", request)
        uid = str(user["id"])
        if not isinstance(body, dict):
            return err(422, "validation", "body must be an object", request)
        patch = body.get("cards") if set(body.keys()) <= {"cards"} else body
        if not isinstance(patch, dict):
            return err(422, "validation", "cards must be an object", request)
        if len(patch) > _MAX_CARD_ENTRIES:
            return err(422, "validation", "too many card entries", request)
        known = _known_card_ids()
        cleaned: dict[str, dict[str, Any]] = {}
        for cid, entry in patch.items():
            key = str(cid)
            if key not in known:
                return err(
                    422,
                    "unknown_card",
                    f"unknown card_id: {key}",
                    request,
                    extra={"unknown_card_id": key},
                )
            ce, eerr = _clean_card_entry(entry)
            if eerr is not None:
                return err(422, "validation", f"cards.{key}: {eerr}", request)
            merged = dict(cleaned.get(key) or {})
            merged.update(ce or {})
            cleaned[key] = merged
        # 부분 병합 — 기존 카드 설정을 유지한 채 patch만 반영한다.
        current = _dashboard_payload(uid)["cards"]
        merged_cards = dict(current)
        for key, entry in cleaned.items():
            base = dict(merged_cards.get(key) or {"order": None, "visible": True})
            base.update(entry)
            merged_cards[key] = base
        store.set_dashboard_cards(uid, merged_cards)
        _audit_api(
            request,
            "me_dashboard_update",
            uid,
            {"cards": sorted(cleaned.keys())},
        )
        return JSONResponse(_dashboard_payload(uid))

    # ---------- views (FR-022) ----------

    @app.get("/api/v1/me/views")
    def me_views_list(request: Request) -> Any:
        user = _session_user(request)
        if user is None:
            return err(401, "unauthorized", "로그인이 필요합니다", request)
        views = [_view_json(v) for v in store.list_user_views(str(user["id"]))]
        return JSONResponse({"items": views})

    @app.post("/api/v1/me/views", status_code=201)
    def me_views_create(body: dict[str, Any], request: Request) -> Any:
        user = _session_user(request)
        if user is None:
            return err(401, "unauthorized", "로그인이 필요합니다", request)
        uid = str(user["id"])
        if not isinstance(body, dict):
            return err(422, "validation", "body must be an object", request)
        name = str(body.get("name") or "").strip()
        if not name or len(name) > _MAX_VIEW_NAME:
            return err(
                422,
                "validation",
                f"name must be 1-{_MAX_VIEW_NAME} characters",
                request,
            )
        if name.lower() == _RESERVED_VIEW_NAME:
            return err(
                422,
                "reserved_name",
                f"view name '{_RESERVED_VIEW_NAME}' is reserved",
                request,
            )
        layout, dropped, lerr = _clean_view_layout(body.get("layout"))
        if lerr is not None:
            return err(422, "validation", lerr, request)
        is_default = body.get("is_default") is True
        if store.user_view_name_exists(uid, name):
            return err(409, "name_taken", "view name already exists", request)
        view = store.create_user_view(uid, name, layout or {}, is_default=is_default)
        if view is None:
            return err(409, "name_taken", "view name already exists", request)
        _audit_api(
            request,
            "me_view_create",
            str(view["id"]),
            {"name": name[:80], "is_default": is_default},
        )
        resp: dict[str, Any] = {"view": _view_json(view)}
        if dropped:
            resp["dropped_card_ids"] = dropped
        return JSONResponse(resp, status_code=201)

    @app.patch("/api/v1/me/views/{view_id}")
    def me_views_update(view_id: str, body: dict[str, Any], request: Request) -> Any:
        user = _session_user(request)
        if user is None:
            return err(401, "unauthorized", "로그인이 필요합니다", request)
        uid = str(user["id"])
        if not isinstance(body, dict):
            return err(422, "validation", "body must be an object", request)
        existing = store.get_user_view(uid, view_id)
        if existing is None:
            return err(404, "not_found", "view not found", request)
        kwargs: dict[str, Any] = {}
        dropped: list[str] = []
        if "name" in body:
            name = str(body.get("name") or "").strip()
            if not name or len(name) > _MAX_VIEW_NAME:
                return err(
                    422,
                    "validation",
                    f"name must be 1-{_MAX_VIEW_NAME} characters",
                    request,
                )
            if name.lower() == _RESERVED_VIEW_NAME:
                return err(
                    422,
                    "reserved_name",
                    f"view name '{_RESERVED_VIEW_NAME}' is reserved",
                    request,
                )
            if name != existing["name"] and store.user_view_name_exists(
                uid, name, exclude_id=view_id
            ):
                return err(409, "name_taken", "view name already exists", request)
            kwargs["name"] = name
        if "layout" in body:
            layout, dropped, lerr = _clean_view_layout(body.get("layout"))
            if lerr is not None:
                return err(422, "validation", lerr, request)
            kwargs["layout"] = layout or {}
        if "is_default" in body:
            if not isinstance(body.get("is_default"), bool):
                return err(422, "validation", "is_default must be a boolean", request)
            kwargs["is_default"] = body["is_default"]
        if not kwargs:
            return JSONResponse({"view": _view_json(existing)})
        view = store.update_user_view(uid, view_id, **kwargs)
        if view is None:
            # 사전 검사와 UPDATE 사이의 경합 또는 이름 충돌 — 둘 다 409로 수렴
            return err(409, "name_taken", "view name already exists", request)
        _audit_api(
            request,
            "me_view_update",
            view_id,
            {"fields": sorted(kwargs.keys())},
        )
        resp: dict[str, Any] = {"view": _view_json(view)}
        if dropped:
            resp["dropped_card_ids"] = dropped
        return JSONResponse(resp)

    @app.delete("/api/v1/me/views/{view_id}")
    def me_views_delete(view_id: str, request: Request) -> Any:
        user = _session_user(request)
        if user is None:
            return err(401, "unauthorized", "로그인이 필요합니다", request)
        uid = str(user["id"])
        existing = store.get_user_view(uid, view_id)
        if existing is None:
            return err(404, "not_found", "view not found", request)
        if existing.get("is_default"):
            # 기본 뷰는 삭제 불가 — 먼저 is_default=false로 내린 뒤 삭제한다.
            return err(
                400,
                "default_view_undeletable",
                "기본 뷰는 삭제할 수 없습니다. 먼저 기본 뷰 지정을 해제하세요",
                request,
            )
        store.clear_dashboard_active_view(uid, view_id)
        store.delete_user_view(uid, view_id)
        _audit_api(request, "me_view_delete", view_id, {"name": existing["name"][:80]})
        return JSONResponse({"status": "deleted"})

    @app.post("/api/v1/me/views/{view_id}/apply")
    def me_views_apply(view_id: str, request: Request) -> Any:
        user = _session_user(request)
        if user is None:
            return err(401, "unauthorized", "로그인이 필요합니다", request)
        uid = str(user["id"])
        view = store.get_user_view(uid, view_id)
        if view is None:
            return err(404, "not_found", "view not found", request)
        # 서버에 대시보드 렌더 상태가 없으므로 layout을 prefs에 기록하고
        # 정규화된 dashboard 상태를 함께 반환한다 — 클라이언트는 그대로 적용.
        layout = _normalize_cards(view.get("layout"))
        store.apply_dashboard_view(uid, view_id, layout)
        _audit_api(request, "me_view_apply", view_id, {"name": view["name"][:80]})
        return JSONResponse(
            {
                "view": _view_json(view),
                "dashboard": _dashboard_payload(uid),
            }
        )

    # ---------- onboarding (FR-029/030/031) ----------

    def _onboarding_checklist(user: dict[str, Any]) -> list[dict[str, Any]]:
        """체크리스트 항목의 done을 실제 store 상태로 실시간 계산한다.

        - account: users.password_hash 또는 google_sub 존재(로그인 수단 연동됨)
        - connector: 현재 조직(org ctx)에 enabled 커넥터 1건 이상
        - first_run: runs.created_by가 사용자 email/id와 일치하는 run 1건 이상
        - notify_channel: push 구독 또는 Slack/Telegram/SMTP 채널 env 설정 존재
          (채널 credential은 인스턴스 env 기반 — spec assumptions 참조)
        """
        uid = str(user["id"])
        email = str(user.get("email") or "")

        acct = store.user_account_info(uid)
        account_done = acct["has_password"] or bool(acct["google_sub"])
        account_detail = (
            "google" if acct["google_sub"] else ("password" if acct["has_password"] else "none")
        )

        try:
            connectors = [c for c in store.list_connectors() if c.get("enabled", True)]
        except Exception:
            connectors = []

        first_run_done = store.user_has_run(uid, email)

        channels: list[str] = []
        try:
            if store.list_push_subscriptions():
                channels.append("push")
        except Exception:
            logger.debug("push subscription check failed", exc_info=True)
        try:
            from ... import mailer, messenger

            channels.extend(messenger.configured_channels())
            if mailer.smtp_configured():
                channels.append("email")
        except Exception:
            logger.debug("notify channel detection failed", exc_info=True)

        return [
            {
                "id": "account",
                "label": "계정 연동 확인",
                "done": account_done,
                "detail": account_detail,
            },
            {
                "id": "connector",
                "label": "커넥터·채널 연결",
                "done": len(connectors) > 0,
                "detail": f"{len(connectors)} configured",
            },
            {
                "id": "first_run",
                "label": "첫 워크플로 실행",
                "done": first_run_done,
                "detail": "run_exists" if first_run_done else "no_runs",
            },
            {
                "id": "notify_channel",
                "label": "알림 채널 설정",
                "done": len(channels) > 0,
                "detail": ",".join(sorted(set(channels))) or "none",
            },
        ]

    @app.get("/api/v1/me/onboarding")
    def me_onboarding_get(request: Request) -> Any:
        user = _session_user(request)
        if user is None:
            return err(401, "unauthorized", "로그인이 필요합니다", request)
        uid = str(user["id"])
        state = store.get_onboarding_state(uid)
        checklist = _onboarding_checklist(user)
        all_done = all(item["done"] for item in checklist)
        completed_at = state.get("completed_at")
        if all_done and not completed_at:
            # 전체 완료 자동 기록 — 다음 GET부터 completed_at이 유지된다.
            completed_at = datetime.now(UTC).isoformat()
            state = store.save_onboarding(
                uid,
                dismissed=state["dismissed"],
                completed_at=completed_at,
                checklist={item["id"]: item["done"] for item in checklist},
            )
        show_wizard = (not all_done) and not state["dismissed"]
        return JSONResponse(
            {
                "dismissed": state["dismissed"],
                "completed_at": completed_at,
                "checklist": checklist,
                "all_done": all_done,
                "show_wizard": show_wizard,
                "can_skip": True,
                "updated_at": state.get("updated_at"),
            }
        )

    @app.post("/api/v1/me/onboarding")
    def me_onboarding_post(body: dict[str, Any], request: Request) -> Any:
        user = _session_user(request)
        if user is None:
            return err(401, "unauthorized", "로그인이 필요합니다", request)
        uid = str(user["id"])
        if not isinstance(body, dict):
            return err(422, "validation", "body must be an object", request)
        state = store.get_onboarding_state(uid)
        dismissed = state["dismissed"]
        if "dismissed" in body:
            if not isinstance(body.get("dismissed"), bool):
                return err(422, "validation", "dismissed must be a boolean", request)
            dismissed = body["dismissed"]
        completed_at = state.get("completed_at")
        if "completed_at" in body:
            raw = body.get("completed_at")
            if raw is None:
                completed_at = None
            elif isinstance(raw, str) and raw.strip():
                completed_at = raw.strip()[:64]
            else:
                return err(
                    422,
                    "validation",
                    "completed_at must be an ISO8601 string or null",
                    request,
                )
        checklist = _onboarding_checklist(user)
        snapshot = {item["id"]: item["done"] for item in checklist}
        new_state = store.save_onboarding(
            uid,
            dismissed=dismissed,
            completed_at=completed_at,
            checklist=snapshot,
        )
        all_done = all(item["done"] for item in checklist)
        show_wizard = (not all_done) and not new_state["dismissed"]
        _audit_api(
            request,
            "me_onboarding_update",
            uid,
            {"dismissed": dismissed, "completed_at_set": bool(completed_at)},
        )
        return JSONResponse(
            {
                "dismissed": new_state["dismissed"],
                "completed_at": new_state.get("completed_at"),
                "checklist": checklist,
                "all_done": all_done,
                "show_wizard": show_wizard,
                "can_skip": True,
                "updated_at": new_state.get("updated_at"),
            }
        )
