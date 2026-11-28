"""기타 라우트 — insights/automation/archive/developer + team/audit/export/share/cards."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse

from ... import webutil
from ...insights import (
    archive_search,
    automation_overview,
    developer_status,
    insights_summary,
)
from ...store.admin import encode_audit_cursor
from ...workflows import WORKFLOWS
from ..schemas import CardCreateRequest, ScheduleToggleRequest, ShareCreateRequest
from ..session_stats import _parse_session_ts
from ..support import _last_cron_tick_at

logger = logging.getLogger("jobmate.api")


def _csv_safe(v: Any) -> Any:
    """스프레드시트 수식 인젝션 방지 — =,+,-,@,tab,CR로 시작하는 문자열에 ' 접두."""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v


def _audit_iso(value: str) -> str | None:
    """ISO8601 → UTC isoformat 정규화. 빈 값은 "", 파싱 실패는 None."""
    v = (value or "").strip()
    if not v:
        return ""
    try:
        dt = datetime.fromisoformat(v[:-1] + "+00:00" if v.endswith(("Z", "z")) else v)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


# ---- audit export 무결성 해시 (spec 005 US3 / FR-022) ----
# 알고리즘 "sha256-chain-v1":
#   canonical(row) = json.dumps(row, ensure_ascii=False, sort_keys=True,
#                               separators=(",",":"), default=str)
#   genesis       = sha256(b"jobmate-audit-export-v1").hexdigest()
#   row_hash[i]   = sha256((prev_hash + "|" + canonical(row_i)).encode()).hexdigest()
#   prev_hash[i+1]= row_hash[i]
#   각 export 행에는 _seq(1-base)·_prev_hash·_row_hash 필드가 추가된다.
#   file_hash     = sha256(seed)를 초기값으로 row_hash[i]를 순서대로
#                   update한 최종 hexdigest — 클라이언트는 같은 순서로
#                   재계산해 변조 여부를 검증할 수 있다.
_AUDIT_HASH_SEED = b"jobmate-audit-export-v1"
_AUDIT_HASH_GENESIS = hashlib.sha256(_AUDIT_HASH_SEED).hexdigest()


def _audit_row_canonical(row: dict[str, Any]) -> str:
    return json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


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
    _platform_admin_user = ctx._platform_admin_user
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
    def _audit_filters(
        request: Request,
        frm: str,
        to: str,
        actor: str,
        action: str,
        run_id: str,
        target: str,
    ) -> tuple[dict[str, str], JSONResponse | None]:
        """from/to ISO8601 정규화 + 필터 dict — 실패 시 (filters, err응답)."""
        frm_iso = _audit_iso(frm)
        to_iso = _audit_iso(to)
        if frm_iso is None or to_iso is None:
            return {}, err(422, "validation", "from/to must be ISO8601", request)
        filters = {
            "frm": frm_iso,
            "to": to_iso,
            "actor": actor.strip(),
            "action": action.strip(),
            "run_id": run_id.strip(),
            "target": target.strip(),
        }
        return filters, None

    @app.get("/api/v1/audit")
    def audit_log(
        request: Request,
        limit: int = 200,
        frm: str = Query("", alias="from"),
        to: str = "",
        actor: str = "",
        action: str = "",
        run_id: str = "",
        target: str = "",
        cursor: str = "",
    ) -> Any:
        """감사 로그 조회 — admin 전용. spec 005 US3/FR-019.

        필터: from/to(ISO8601), actor(정확), action(prefix), run_id,
        target(object_ref 정확), cursor((created_at,id) keyset).
        응답은 기존 {items} 형태 유지 + next_cursor 추가."""
        if _admin_user(request) is None:
            return err(403, "forbidden", "admin role required", request)
        filters, bad = _audit_filters(request, frm, to, actor, action, run_id, target)
        if bad is not None:
            return bad
        size = min(max(limit, 1), 500)
        # 플랫폼 admin(세션 role=admin·env/스코프드 키)에게만 org_id=''인
        # 플랫폼 수준 감사 행(auth_login 등 비조직 이벤트)을 노출한다.
        # 조직 admin은 자기 조직 스코프만 본다(기본 False).
        try:
            it = store.iter_audit(
                frm=filters["frm"],
                to=filters["to"],
                actor=filters["actor"],
                action=filters["action"],
                run_id=filters["run_id"],
                target=filters["target"],
                cursor=cursor.strip(),
                batch=size + 1,
                include_platform=_platform_admin_user(request) is not None,
            )
        except ValueError:
            return err(422, "validation", "invalid cursor", request)
        rows: list[dict[str, Any]] = []
        for row in it:
            rows.append(row)
            if len(rows) > size:
                break
        has_more = len(rows) > size
        rows = rows[:size]
        next_cursor = (
            encode_audit_cursor(rows[-1]["ts"], rows[-1]["id"]) if has_more and rows else None
        )
        items = [
            {
                "id": e["id"],
                "ts": e["ts"],
                "actor_email": e["actor"],
                "action": e["action"],
                "target": e["target"],
                "detail": e["detail"],
                "run_id": e["run_id"],
            }
            for e in rows
        ]
        return JSONResponse(content={"items": items, "next_cursor": next_cursor})

    @app.get("/api/v1/audit/export")
    def audit_export(
        request: Request,
        format: str = "csv",
        integrity: str = "",
        frm: str = Query("", alias="from"),
        to: str = "",
        actor: str = "",
        action: str = "",
        run_id: str = "",
        target: str = "",
    ) -> Any:
        """감사 로그 스트리밍 export — admin 전용. spec 005 US3/FR-020~023.

        format=csv|json. JSON은 {generated_at, filters, rows:[...]} 구조를
        청크 단위로 흘려보낸다(전체 적재 없음). integrity=hash(JSON 전용)는
        행별 sha256 해시 체인 + 파일 해시를 포함한다 — 알고리즘은
        _AUDIT_HASH_* 주석 참조. export 완료 시 audit_export 이벤트를
        기록한다(행 내용은 기록하지 않음)."""
        if _admin_user(request) is None:
            return err(403, "forbidden", "admin role required", request)
        if format not in ("csv", "json"):
            return err(422, "validation", "format must be csv|json", request)
        if integrity not in ("", "hash"):
            return err(422, "validation", "integrity must be hash", request)
        if integrity == "hash" and format != "json":
            return err(422, "validation", "integrity=hash requires format=json", request)
        filters, bad = _audit_filters(request, frm, to, actor, action, run_id, target)
        if bad is not None:
            return bad
        try:
            row_iter = store.iter_audit(
                frm=filters["frm"],
                to=filters["to"],
                actor=filters["actor"],
                action=filters["action"],
                run_id=filters["run_id"],
                target=filters["target"],
                batch=500,
                include_platform=_platform_admin_user(request) is not None,
            )
        except ValueError:
            return err(422, "validation", "invalid cursor", request)
        # JSON filters 에코용 — 빈 문자열은 None으로 정규화한다.
        public_filters = {
            "from": filters["frm"] or None,
            "to": filters["to"] or None,
            "actor": filters["actor"] or None,
            "action": filters["action"] or None,
            "run_id": filters["run_id"] or None,
            "target": filters["target"] or None,
        }
        requester = _actor_label(request)
        fname = f"jobmate-audit-{datetime.now(UTC).date().isoformat()}.{format}"

        def _record_export(row_count: int, status: str = "completed") -> None:
            """FR-023 — export 실행 자체를 감사 로그에 기록(내용물 금지).

            스트림이 중간에 끊겨도(aborted) 기록이 남아야 하므로 제너레이터의
            finally에서 호출한다. 감사 기록 실패가 응답 스트림을 깨지 않도록
            예외를 삼킨다(_audit_api도 내부에서 예외를 잡지만 이중 방어).
            참고: 스트림 종료 시점에는 미들웨어 finally가 org ctx를 이미
            리셋했으므로 이 행의 org_id는 ''(플랫폼 수준)로 기록된다 —
            include_platform 조회로 플랫폼 admin에게 노출된다."""
            try:
                _audit_api(
                    request,
                    "audit_export",
                    "",
                    {
                        "format": format,
                        "integrity": integrity or "none",
                        "filters": public_filters,
                        "row_count": row_count,
                        "status": status,
                        "requester": requester,
                    },
                )
            except Exception:
                logger.warning("audit_export record failed", exc_info=True)

        # 스트림 중단(클라이언트 disconnect·예외) 시에도 FR-023 감사가
        # 남도록 본문은 _csv_body/_json_body로 분리하고, 감싼 제너레이터의
        # finally에서 부분 행 수와 함께 기록한다. counter[0]은 yield된
        # 데이터 행 수 — abort 시점까지의 부분 집계가 그대로 남는다.
        def _csv_body(counter: list[int]) -> Any:
            buf = io.StringIO()
            writer = csv.writer(buf)
            # UTF-8 BOM — Excel이 CSV를 바로 열 때 인코딩을 올바로 인식.
            yield "\ufeff"
            # 필터 조건 명시 — 헤더 주석 행(FR-021/US3 시나리오 2).
            yield "# filters: " + json.dumps(public_filters, ensure_ascii=False) + "\r\n"
            writer.writerow(["id", "ts", "run_id", "actor", "action", "target", "detail"])
            header = buf.getvalue()
            buf.seek(0)
            buf.truncate(0)
            yield header
            for row in row_iter:
                counter[0] += 1
                buf.seek(0)
                buf.truncate(0)
                writer.writerow(
                    [
                        _csv_safe(row["id"]),
                        _csv_safe(row["ts"]),
                        _csv_safe(row["run_id"]),
                        _csv_safe(row["actor"]),
                        _csv_safe(row["action"]),
                        _csv_safe(row["target"]),
                        _csv_safe(json.dumps(row["detail"], ensure_ascii=False, default=str)),
                    ]
                )
                yield buf.getvalue()

        def _gen_csv() -> Any:
            counter = [0]
            status = "completed"
            try:
                yield from _csv_body(counter)
            except GeneratorExit:
                # 클라이언트 disconnect/StreamingResponse close — 부분 행 수로
                # aborted 기록을 남긴다(FR-023은 완료 여부와 무관하게 필수).
                status = "aborted"
                raise
            except Exception:
                status = "aborted"
                raise
            finally:
                _record_export(counter[0], status)

        def _json_body(counter: list[int]) -> Any:
            prev_hash = _AUDIT_HASH_GENESIS
            file_h = hashlib.sha256(_AUDIT_HASH_SEED)
            yield (
                '{"generated_at":'
                + json.dumps(datetime.now(UTC).isoformat())
                + ',"filters":'
                + json.dumps(public_filters, ensure_ascii=False)
                + ',"rows":['
            )
            first = True
            for row in row_iter:
                counter[0] += 1
                payload = dict(row)
                if integrity == "hash":
                    canon = _audit_row_canonical(row)
                    row_hash = hashlib.sha256((prev_hash + "|" + canon).encode("utf-8")).hexdigest()
                    file_h.update(row_hash.encode("ascii"))
                    payload["_seq"] = counter[0]
                    payload["_prev_hash"] = prev_hash
                    payload["_row_hash"] = row_hash
                    prev_hash = row_hash
                if not first:
                    yield ","
                first = False
                yield json.dumps(payload, ensure_ascii=False, default=str)
            if integrity == "hash":
                yield (
                    '],"integrity":{"algorithm":"sha256-chain-v1",'
                    '"file_hash":"' + file_h.hexdigest() + '","row_count":' + str(counter[0]) + "}}"
                )
            else:
                yield "]}"

        def _gen_json() -> Any:
            counter = [0]
            status = "completed"
            try:
                yield from _json_body(counter)
            except GeneratorExit:
                status = "aborted"
                raise
            except Exception:
                status = "aborted"
                raise
            finally:
                _record_export(counter[0], status)

        gen = _gen_json if format == "json" else _gen_csv
        media = "application/json" if format == "json" else "text/csv"
        return StreamingResponse(
            gen(),
            media_type=media,
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )

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
