"""spec 008 US1 — OTLP/HTTP JSON 인제스트 + ingest 토큰 관리 라우트.

POST /api/v1/otlp/v1/traces — OTLP JSON span 수집. 인증은 세션/API 키가
아니라 전용 ingest 토큰(otlp_ingest_tokens.token_hash = sha256) —
Authorization: Bearer <token> 또는 x-jobmate-ingest-key 헤더.
토큰의 org_id로 orgctx.org_scope를 열어 스팬을 해당 조직에 귀속한다.

GET/POST /api/v1/otlp/tokens, POST /{id}/revoke — lead 이상(_lead_gate).
토큰 원문은 발급 응답에 1회만 포함되고 token_hash는 어떤 응답에도
나가지 않는다.

GET /api/v1/observability/external-spans — lead 이상 조회(org 스코프는
store의 orgctx 필터가 강제; sibling observability 엔드포인트와 동일 게이트).

주의: 보안 미들웨어(app.py)는 /api/*에 세션/키 인증을 요구한다 —
인제스트 클라이언트가 Bearer ingest 토큰만 보내는 배포에서는
"/api/v1/otlp/v1/traces" 경로의 미들웨어 면제 또는 ingest 토큰 인식이
app.py 쪽에서 필요하다(이 라우터는 자체 인증을 수행한다).
"""

from __future__ import annotations

import json
import logging
import re
import secrets
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ...security import hash_token
from ...store import orgctx
from ...store.otlp_ingest import external_span_json
from ...store.util import _iso, _now

logger = logging.getLogger("jobmate.api")

# ---- 인제스트 가드 ----
_MAX_SPANS_PER_REQUEST = 1000
_MAX_BODY_BYTES = 8 * 1024 * 1024
_MAX_ATTRS_PER_SPAN = 64
_MAX_ATTR_VALUE_CHARS = 4096
_INGEST_RATE_PER_MIN = 120  # 토큰당 분당 요청 상한(인메모리, 인스턴스 단위)

# 민감 attribute 키 — 세그먼트 단위 매칭 후 메트릭 접미사 예외 적용.
_SENSITIVE_SEGMENTS = frozenset(
    {
        "authorization",
        "cookie",
        "token",
        "key",
        "apikey",
        "secret",
        "password",
        "passwd",
        "credential",
        "bearer",
        "session",
    }
)
# 지표/계량 키는 세그먼트가 sensitive여도 보존 — "llm.token_count",
# "gen_ai.usage.input_tokens" 같은 정상 속성을 지우지 않기 위함.
_METRIC_SUFFIXES = (
    "count",
    "total",
    "sum",
    "usd",
    "cost",
    "ms",
    "sec",
    "bytes",
    "tokens",
    "calls",
    "items",
    "chars",
    "duration",
    "latency",
    "size",
    "requests",
    "responses",
    "attempts",
    "retries",
)

_SEG_STRIP_RE = re.compile(r"[^a-z0-9]")

_KIND_BY_INT = {0: "", 1: "internal", 2: "server", 3: "client", 4: "producer", 5: "consumer"}
_KIND_BY_NAME = {
    "SPAN_KIND_UNSPECIFIED": "",
    "SPAN_KIND_INTERNAL": "internal",
    "SPAN_KIND_SERVER": "server",
    "SPAN_KIND_CLIENT": "client",
    "SPAN_KIND_PRODUCER": "producer",
    "SPAN_KIND_CONSUMER": "consumer",
}


def _is_sensitive_attr_key(key: str) -> bool:
    """attribute 키가 비밀값을 담을 가능성이 있으면 True.

    정규화(소문자+비영숫자 제거) 키에 민감 세그먼트가 substring으로
    포함되면 민감 — camelCase(accessToken/clientSecret)도 잡는다.
    단 정규화 키가 계량 접미사(count/total/usd/tokens 등)로 끝나면
    예외 — "llm.token_count", "gen_ai.usage.input_tokens" 같은 정상
    속성을 지우지 않기 위함."""
    norm = _SEG_STRIP_RE.sub("", str(key).lower())
    if not any(seg in norm for seg in _SENSITIVE_SEGMENTS):
        return False
    return not norm.endswith(_METRIC_SUFFIXES)


def _clip_str(s: str, limit: int = _MAX_ATTR_VALUE_CHARS) -> str:
    return s if len(s) <= limit else s[:limit]


# kvlist/array 중첩 깊이 상한 — 비정상 깊은 재귀로 인한 스택 소진 방지.
_MAX_ATTR_DEPTH = 8


def _otlp_attr_value(v: Any, _depth: int = 0) -> Any:
    """OTLP AnyValue → JSON 직렬화 가능한 스칼라/문자열.

    arrayValue/kvlistValue는 JSON 문자열로 축약 저장한다(스키마가 평탄
    dict를 기대). 모든 문자열은 _MAX_ATTR_VALUE_CHARS로 절단.
    중첩은 _MAX_ATTR_DEPTH까지 — 초과 시 문자열 repr로 절단 저장."""
    if _depth >= _MAX_ATTR_DEPTH:
        return _clip_str(str(v)) if not isinstance(v, (int, float, bool)) else v
    if not isinstance(v, dict):
        return _clip_str(str(v)) if not isinstance(v, (int, float, bool)) else v
    if "stringValue" in v:
        return _clip_str(str(v.get("stringValue")))
    if "boolValue" in v:
        return bool(v.get("boolValue"))
    if "intValue" in v:
        raw = v.get("intValue")
        try:
            return int(str(raw))
        except (TypeError, ValueError):
            return _clip_str(str(raw))
    if "doubleValue" in v:
        raw = v.get("doubleValue")
        try:
            return float(str(raw))
        except (TypeError, ValueError):
            return _clip_str(str(raw))
    if "arrayValue" in v:
        vals = (v.get("arrayValue") or {}).get("values") or []
        try:
            return _clip_str(
                json.dumps([_otlp_attr_value(x, _depth + 1) for x in vals], ensure_ascii=False)
            )
        except (TypeError, ValueError):
            return "[]"
    if "kvlistValue" in v:
        pairs = (v.get("kvlistValue") or {}).get("values") or []
        out: dict[str, Any] = {}
        for item in pairs:
            if isinstance(item, dict) and item.get("key"):
                out[str(item["key"])[:200]] = _otlp_attr_value(item.get("value"), _depth + 1)
        try:
            return _clip_str(json.dumps(out, ensure_ascii=False, default=str))
        except (TypeError, ValueError):
            return "{}"
    if "bytesValue" in v:
        return _clip_str(str(v.get("bytesValue")))
    try:
        return _clip_str(json.dumps(v, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return ""


def _span_attributes(span: dict[str, Any]) -> dict[str, Any]:
    """span.attributes → 평탄 dict. 민감 키 제외, 64개·값 4KB 상한."""
    attrs: dict[str, Any] = {}
    for item in span.get("attributes") or []:
        if len(attrs) >= _MAX_ATTRS_PER_SPAN:
            break
        if not isinstance(item, dict):
            continue
        key = item.get("key")
        if not key or _is_sensitive_attr_key(str(key)):
            continue
        attrs[str(key)[:200]] = _otlp_attr_value(item.get("value"))
    return attrs


def _span_kind(v: Any) -> str:
    if isinstance(v, bool):
        return ""
    if isinstance(v, int):
        return _KIND_BY_INT.get(v, "")
    s = str(v or "").strip()
    if not s:
        return ""
    return _KIND_BY_NAME.get(s.upper(), s.lower())


def _nanos_iso(v: Any) -> str | None:
    """unix 나노초(int/str) → ISO UTC. 파싱 실패/0 이하면 None."""
    try:
        ns = int(str(v))
    except (TypeError, ValueError):
        return None
    if ns <= 0:
        return None
    try:
        return datetime.fromtimestamp(ns / 1e9, UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _iter_otlp_spans(payload: Any) -> list[dict[str, Any]]:
    """OTLP JSON → span dict 리스트(구조 결손은 관대하게 건너뜀)."""
    spans: list[dict[str, Any]] = []
    if not isinstance(payload, dict):
        return spans
    for rs in payload.get("resourceSpans") or []:
        if not isinstance(rs, dict):
            continue
        for ss in rs.get("scopeSpans") or []:
            if not isinstance(ss, dict):
                continue
            for sp in ss.get("spans") or []:
                if isinstance(sp, dict):
                    spans.append(sp)
    return spans


def _ingest_credential(request: Request) -> str:
    """ingest 토큰 추출 — Bearer 우선, x-jobmate-ingest-key 폴백."""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        tok = auth[7:].strip()
        if tok:
            return tok
    return (request.headers.get("x-jobmate-ingest-key") or "").strip()


async def _read_body_capped(request: Request) -> bytes | None:
    """Content-Length 선검사 + 스트림 읽기 상한 — 초과 시 None.

    request.body()는 전체를 먼저 버퍼링하므로 상한 검증이 늦다.
    messaging.py의 웹훅 본문 상한과 같은 패턴으로 일찍 끊는다."""
    cl = request.headers.get("content-length", "")
    if cl.isdigit() and int(cl) > _MAX_BODY_BYTES:
        return None
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > _MAX_BODY_BYTES:
            return None
    return bytes(body)


class _TokenCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)


def _process_otlp_body(store: Any, body: bytes, token_row: dict[str, Any]) -> tuple[str, Any]:
    """인제스트 본문 파싱 + 스팬 정규화 + sqlite 쓰기 — 동기 작업.

    이벤트 루프를 막지 않도록 호출부에서 run_in_threadpool로 돌린다.
    반환: ("ok", response_content) 또는 ("error", (status, code, message))
    — 에러 봉투는 핸들러의 err()가 표준 형식으로 만든다."""
    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        return "error", (400, "invalid_json", "request body is not valid JSON")

    raw_spans = _iter_otlp_spans(payload)
    if len(raw_spans) > _MAX_SPANS_PER_REQUEST:
        return "error", (
            413,
            "too_many_spans",
            f"span count exceeds {_MAX_SPANS_PER_REQUEST}",
        )

    oid = str(token_row.get("org_id") or "")
    accepted = 0
    rejected = 0
    rows: list[dict[str, Any]] = []
    with orgctx.org_scope(oid):
        for sp in raw_spans:
            span_id = str(sp.get("spanId") or sp.get("span_id") or "")
            if not span_id:
                rejected += 1
                continue
            attrs = _span_attributes(sp)
            run_hint = ""
            for k, v in attrs.items():
                if k.lower() == "jobmate.run_id":
                    run_hint = str(v)
                    break
            row = {
                "trace_id": str(sp.get("traceId") or sp.get("trace_id") or "")[:128],
                "span_id": span_id[:128],
                "parent_span_id": str(sp.get("parentSpanId") or sp.get("parent_span_id") or "")[
                    :128
                ],
                "name": str(sp.get("name") or "")[:500],
                "kind": _span_kind(sp.get("kind")),
                "started_at": _nanos_iso(sp.get("startTimeUnixNano")) or _iso(_now()),
                "ended_at": _nanos_iso(sp.get("endTimeUnixNano")),
                "attributes": attrs,
                # jobmate.run_id가 같은 조직의 실제 run을 가리킬 때만 링크
                "run_id": run_hint[:64] if run_hint and store.get_run(run_hint) else "",
                "org_id": oid,
            }
            rows.append(row)
        accepted = store.insert_external_spans(rows)
        store.touch_otlp_ingest_token(str(token_row["id"]))
    logger.info(
        "otlp ingest org=%s token=%s accepted=%d rejected=%d",
        oid,
        str(token_row.get("id") or "")[:12],
        accepted,
        rejected,
    )
    return "ok", {"accepted": accepted, "rejected": rejected}


def register(app: FastAPI, ctx: Any) -> None:
    store = ctx.store
    err = ctx.err
    _lead_gate = ctx._lead_gate
    _audit_api = ctx._audit_api
    _actor_label = ctx._actor_label
    _rate_hit = ctx._rate_hit
    _otlp_ingest_hits: dict[str, list[float]] = {}
    _otlp_auth_fail_hits: dict[str, list[float]] = {}

    # ---- US1: OTLP ingest ----
    @app.post("/api/v1/otlp/v1/traces")
    async def otlp_ingest(request: Request) -> JSONResponse:
        cred = _ingest_credential(request)
        if not cred:
            return err(401, "unauthorized", "ingest token required", request)
        token_row = store.find_otlp_ingest_token_by_hash(hash_token(cred))
        if token_row is None or token_row.get("revoked_at"):
            # 실패 인증 브루트포스 억제 — 클라이언트 IP당 분당 30회.
            ip = request.client.host if request.client else "-"
            if _rate_hit(_otlp_auth_fail_hits, f"ip:{ip}", 60.0, 30):
                return err(429, "rate_limited", "too many failed ingest auth attempts", request)
            logger.warning("otlp ingest auth failed path=%s", request.url.path)
            return err(401, "unauthorized", "invalid or revoked ingest token", request)
        if _rate_hit(_otlp_ingest_hits, f"tok:{token_row['id']}", 60.0, _INGEST_RATE_PER_MIN):
            return err(429, "rate_limited", "ingest rate limit exceeded", request)

        body = await _read_body_capped(request)
        if body is None:
            return err(413, "payload_too_large", "request body too large", request)
        # JSON 파싱 + 스팬 정규화 + sqlite 쓰기는 동기 — 스레드로 오프로드.
        kind, payload = await run_in_threadpool(_process_otlp_body, store, body, token_row)
        if kind == "error":
            status, code, message = payload
            return err(status, code, message, request)
        return JSONResponse(content=payload)

    # ---- US1: 토큰 CRUD (lead+) ----
    @app.get("/api/v1/otlp/tokens")
    def list_otlp_tokens(request: Request) -> JSONResponse:
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        rows = store.list_otlp_ingest_tokens()
        # 계약 필드만 노출 — token_hash/org_id/created_by는 응답에 싣지 않는다
        items = [
            {
                "id": t.get("id"),
                "name": t.get("name"),
                "prefix": t.get("prefix") or str(t.get("id") or "")[:8],
                "created_at": t.get("created_at"),
                "last_used_at": t.get("last_used_at"),
                "revoked_at": t.get("revoked_at"),
            }
            for t in rows
        ]
        return JSONResponse(content={"items": items})

    @app.post("/api/v1/otlp/tokens", status_code=201)
    def create_otlp_token(body: _TokenCreateRequest, request: Request) -> JSONResponse:
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        token = "jmo_" + secrets.token_urlsafe(32)
        row = store.create_otlp_ingest_token(
            name=body.name,
            token_hash=hash_token(token),
            created_by=_actor_label(request),
        )
        _audit_api(request, "otlp_token_create", str(row.get("id") or ""), {"name": body.name})
        logger.info("otlp ingest token created id=%s", row.get("id"))
        # 원문은 이 응답에 1회만 — 이후 조회/로그 어디에도 나오지 않는다
        return JSONResponse(
            status_code=201,
            content={
                "id": row.get("id"),
                "token": token,
                "name": row.get("name"),
                "created_at": row.get("created_at"),
            },
        )

    @app.post("/api/v1/otlp/tokens/{token_id}/revoke")
    def revoke_otlp_token(token_id: str, request: Request) -> JSONResponse:
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        if store.get_otlp_ingest_token(token_id) is None:
            return err(404, "not_found", "ingest token not found", request)
        store.revoke_otlp_ingest_token(token_id)
        _audit_api(request, "otlp_token_revoke", token_id, {})
        logger.info("otlp ingest token revoked id=%s", token_id)
        return JSONResponse(content={"ok": True})

    # ---- US1: 외부 스팬 조회 (lead+ — observability 엔드포인트와 동일) ----
    @app.get("/api/v1/observability/external-spans")
    def list_external_spans(
        request: Request,
        days: int = 7,
        trace_id: str = "",
        run_id: str = "",
        limit: int = 500,
    ) -> JSONResponse:
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        rows = store.list_external_spans(days=days, trace_id=trace_id, run_id=run_id, limit=limit)
        clamped_days = max(1, min(int(days), 90))
        return JSONResponse(
            content={
                "days": clamped_days,
                "items": [external_span_json(r) for r in rows],
            }
        )
