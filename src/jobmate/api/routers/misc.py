"""기타 라우트 — insights/automation/archive/developer + team/audit/export/share/cards."""

from __future__ import annotations

import json
import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse

from ... import webutil
from ...insights import (
    archive_search,
    automation_overview,
    developer_status,
    insights_summary,
)
from ...workflows import WORKFLOWS
from ..schemas import CardCreateRequest, ScheduleToggleRequest, ShareCreateRequest
from ..session_stats import _parse_session_ts
from ..support import _last_cron_tick_at

logger = logging.getLogger("jobmate.api")


def register(app: FastAPI, ctx: Any) -> None:
    store = ctx.store
    settings = ctx.settings
    adapters = ctx.adapters
    err = ctx.err
    run_detail = ctx.run_detail
    _actor_label = ctx._actor_label
    _audit_api = ctx._audit_api
    _admin_gate = ctx._admin_gate
    _admin_user = ctx._admin_user
    _platform_admin_gate = ctx._platform_admin_gate
    _lead_gate = ctx._lead_gate
    _prefs = ctx._prefs

    def _workflow_keys() -> list[str]:
        """내장 + 현재 조직의 커스텀 workflow key — 인사이트 집계용(spec 003 US3)."""
        keys = set(WORKFLOWS)
        try:
            keys |= {str(r.get("name") or "") for r in store.list_custom_workflows()}
        except Exception:
            logger.debug("custom workflow list failed for insights", exc_info=True)
        return sorted(keys)

    @app.get("/api/v1/insights")
    def insights(request: Request, days: int = 30) -> JSONResponse:
        """인사이트 뷰 — 세션/런/비용/실패 집계 (최대 365일). lead 이상."""
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        return JSONResponse(
            content=insights_summary(store, _workflow_keys(), max(1, min(days, 365)))
        )

    @app.get("/api/v1/automation/overview")
    def automation_overview_endpoint(request: Request) -> JSONResponse:
        """자동화 허브 뷰 — 스케줄/알림 규칙/cron/메신저/리포트 상태. admin 전용."""
        gate = _admin_gate(request)
        if gate is not None:
            return gate
        return JSONResponse(
            content=automation_overview(
                store, _prefs().get("notify_rules", []), _last_cron_tick_at()
            )
        )

    @app.get("/api/v1/archive")
    def archive(request: Request, q: str = "", kind: str = "", limit: int = 50) -> Any:
        """지식/아카이브 검색 — 리포트·run·종료 세션 통합. lead 이상."""
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        if kind and kind not in ("report", "run", "session"):
            return err(422, "validation_error", "kind must be report|run|session", request)
        return archive_search(store, q, kind, max(1, min(limit, 200)))

    @app.get("/api/v1/developer/status")
    def developer_status_endpoint(request: Request) -> JSONResponse:
        """개발자 뷰 — 연동/인증 구성 상태 (값이 아닌 bool/노트만). admin 전용."""
        gate = _platform_admin_gate(request)
        if gate is not None:
            return gate
        return JSONResponse(content=developer_status(store, settings, adapters))

    @app.get("/api/v1/team")
    def team_view(request: Request) -> JSONResponse:
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        return JSONResponse(content={"items": store.team_activity()})

    # ---- 7. AUDIT LOG VIEW (admin only) ----
    @app.get("/api/v1/audit")
    def audit_log(request: Request, limit: int = 200) -> JSONResponse:
        if _admin_user(request) is None:
            return err(403, "forbidden", "admin role required", request)
        items = [
            {
                "ts": e["ts"],
                "actor_email": e["actor"],
                "action": e["action"],
                "target": e["target"],
                "detail": e["detail"],
                "run_id": e["run_id"],
            }
            for e in store.list_audit(min(max(limit, 1), 500))
        ]
        return JSONResponse(content={"items": items})

    # ---- 8. EXPORT & SHARE ----
    @app.get("/api/v1/export")
    def export_data(
        request: Request, kind: str = "runs", format: str = "json", days: int = 30
    ) -> Any:
        """runs|sessions|usage|reports × csv|json 다운로드. lead 이상."""
        from fastapi.responses import Response

        gate = _lead_gate(request)
        if gate is not None:
            return gate
        if kind not in ("runs", "sessions", "usage", "reports"):
            return err(422, "validation", "kind must be runs|sessions|usage|reports", request)
        if format not in ("csv", "json"):
            return err(422, "validation", "format must be csv|json", request)
        days = max(1, min(days, 365))
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        if kind == "runs":
            rows = store.export_runs(cutoff)
        elif kind == "sessions":
            rows = store.export_sessions(cutoff)
        elif kind == "usage":
            rows = store.llm_usage_rows(cutoff)
        else:
            rows = store.export_reports(cutoff)
        fname = f"jobmate-{kind}-{datetime.now(UTC).date().isoformat()}.{format}"
        if format == "json":
            payload = {
                "kind": kind,
                "days": days,
                "generated_at": datetime.now(UTC).isoformat(),
                "rows": rows,
            }
            return Response(
                content=json.dumps(payload, ensure_ascii=False, default=str),
                media_type="application/json",
                headers={"Content-Disposition": f'attachment; filename="{fname}"'},
            )
        import csv
        import io

        def _csv_safe(v: Any) -> Any:
            """스프레드시트 수식 인젝션 방지 — =,+,-,@,tab,CR로 시작하는 문자열에 ' 접두."""
            if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
                return "'" + v
            return v

        cols: list[str] = []
        for r in rows:
            for k in r:
                if k not in cols:
                    cols.append(k)
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow(
                {
                    k: _csv_safe(
                        json.dumps(v, ensure_ascii=False, default=str)
                        if isinstance(v, (dict, list))
                        else v
                    )
                    for k, v in r.items()
                }
            )
        return Response(
            content=buf.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )

    @app.post("/api/v1/share", status_code=201)
    def share_create(body: ShareCreateRequest, request: Request) -> JSONResponse:
        """run/report/insights 스냅샷을 토큰 링크로 공유 (+30일 만료). lead 이상."""
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        kind = body.kind
        ref = body.ref_id.strip()
        if kind == "run":
            if not ref:
                return err(422, "validation", "ref_id required for kind=run", request)
            payload = run_detail(ref)
            if payload is None:
                return err(404, "not_found", "run not found", request)
        elif kind == "report":
            rep = store.get_report(ref) if ref else store.latest_report("daily")
            if rep is None:
                return err(404, "not_found", "report not found", request)
            payload = rep
        else:
            payload = insights_summary(store, _workflow_keys(), 30)
        token = secrets.token_urlsafe(24)
        expires_at = (datetime.now(UTC) + timedelta(days=30)).isoformat()
        store.create_share_link(token, kind, ref, payload, _actor_label(request), expires_at)
        _audit_api(request, "share_create", f"{kind}:{ref or '-'}", {})
        logger.info("share link created kind=%s ref=%s", kind, ref or "-")
        return JSONResponse(
            status_code=201,
            content={
                "url": f"/share/{token}",
                "token": token,
                "expires_at": expires_at,
            },
        )

    @app.get("/api/v1/share/{token}")
    def share_get(token: str, request: Request) -> JSONResponse:
        """공개 공유 스냅샷 — 미들웨어에서 GET만 인증 예외."""
        link = store.get_share_link(token)
        if link is None:
            return err(404, "not_found", "share link not found", request)
        exp = link.get("expires_at") or ""
        exp_dt = _parse_session_ts(exp)
        # 만료 시각이 없거나 파싱 불가면 만료로 취급한다(DB 변조 방어).
        if exp_dt is None or exp_dt <= datetime.now(UTC):
            return err(410, "gone", "share link expired", request)
        return JSONResponse(
            content={
                "kind": link["kind"],
                "created_at": link["created_at"],
                "payload": link["payload"],
            }
        )

    @app.get("/share/{token}")
    def share_redirect(token: str) -> RedirectResponse:
        """POST /share 응답의 /share/<token> URL → API 엔드포인트로 리다이렉트."""
        return RedirectResponse(f"/api/v1/share/{token}", status_code=302)

    # ---- 9. CUSTOM DASHBOARD CARDS (inbox 뷰에서 조회하므로 GET은 무인증, 쓰기는 lead) ----
    @app.get("/api/v1/cards")
    def list_cards() -> JSONResponse:
        # inbox 뷰 위젯 — 읽기는 전체 인증 사용자, 생성/수정/삭제는 lead
        return JSONResponse(content={"items": store.list_cards()})

    @app.post("/api/v1/cards", status_code=201)
    def create_card(body: CardCreateRequest, request: Request) -> JSONResponse:
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        url = body.url.strip()
        if not webutil.is_safe_fetch_url(url):
            return err(422, "url_not_allowed", "http(s) 공인 URL만 허용됩니다", request)
        item = store.create_card(
            body.title.strip(), url, body.json_path.strip(), body.enabled, _actor_label(request)
        )
        _audit_api(request, "card_create", item["id"], {"title": item["title"][:80]})
        logger.info("dashboard card created id=%s", item["id"])
        return JSONResponse(status_code=201, content=item)

    @app.delete("/api/v1/cards/{card_id}")
    def delete_card(card_id: str, request: Request) -> JSONResponse:
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        if not store.delete_card(card_id):
            return err(404, "not_found", "card not found", request)
        _audit_api(request, "card_delete", card_id, {})
        logger.info("dashboard card deleted id=%s", card_id)
        return JSONResponse(content={"ok": True})

    @app.post("/api/v1/cards/{card_id}/toggle")
    def toggle_card(card_id: str, body: ScheduleToggleRequest, request: Request) -> JSONResponse:
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        card = store.set_card_enabled(card_id, body.enabled)
        if card is None:
            return err(404, "not_found", "card not found", request)
        _audit_api(request, "card_toggle", card_id, {"enabled": body.enabled})
        logger.info("dashboard card %s toggled enabled=%s", card_id, body.enabled)
        return JSONResponse(content=card)

    @app.get("/api/v1/cards/{card_id}/data")
    def card_data(card_id: str, request: Request) -> JSONResponse:
        """카드 URL을 서버에서 fetch(8s/256KB) 후 json_path로 값 추출.
        inbox 위젯 표시용 읽기 — 카드 생성은 lead 전용이라 URL은 이미 검증됨."""
        card = store.get_card(card_id)
        if card is None:
            return err(404, "not_found", "card not found", request)
        if not card.get("enabled"):
            return err(409, "card_disabled", "card is disabled", request)
        url = str(card.get("url") or "")
        if not webutil.is_safe_fetch_url(url):
            return err(422, "url_not_allowed", "URL이 허용되지 않습니다", request)
        try:
            body_bytes = webutil.fetch_bytes(url, timeout=8.0)
        except webutil.FetchError as e:
            logger.warning("card fetch failed id=%s host=%s: %s", card_id, webutil.host_of(url), e)
            return err(502, "fetch_failed", str(e)[:200], request)
        path = str(card.get("json_path") or "").strip()
        if not path:
            try:
                value: Any = json.loads(body_bytes.decode("utf-8", errors="replace"))
            except ValueError:
                value = body_bytes.decode("utf-8", errors="replace")[:4000]
            return JSONResponse(content={"value": value})
        try:
            data = json.loads(body_bytes.decode("utf-8", errors="replace"))
        except ValueError:
            logger.warning("card fetch non-json id=%s", card_id)
            return err(502, "invalid_json", "response is not JSON", request)
        return JSONResponse(content={"value": webutil.extract_json_path(data, path)})
