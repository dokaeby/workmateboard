# mypy: disable-error-code="attr-defined"
"""spec 008 US1 — OTLP 인제스트 스토어 믹스인.

otlp_ingest_tokens/external_spans는 org-scoped 테이블 — 읽기는 orgctx
_org_filter, 쓰기는 org_for_write/명시 org_id 인자를 따른다
(webhooks.py·observability.py와 동일 규칙). token_hash는 sha256 해시만
저장되며 원문 토큰은 절대 보관·반환하지 않는다 — _token_dict가
token_hash를 응답 dict에서 제거한다.
"""

from __future__ import annotations

import json
import math
import uuid
from datetime import timedelta
from typing import Any

from . import orgctx
from .span_error import span_attrs_is_error
from .util import _clamp_limit, _iso, _loads, _now

# last_used_at 갱신 스로틀 — 인제스트 호출마다 UPDATE하면 쓰기가
# 읽기 트래픽에 묻힌다. 60초 이내 재사용은 갱신하지 않는다.
_LAST_USED_MIN_INTERVAL_S = 60.0

# 응답 시 attributes 값 1개당 최대 길이(문자) — 외부 스팬 attributes가
# 무한정 커지는 것을 막는다. ingest 시점 상한(4KB)보다 작은 표시용 절단.
_ATTR_VALUE_LIMIT = 2048


def _org_filter() -> tuple[str, list[Any]]:
    """ctx 조직이 있으면 ` AND org_id=?` 조건 — 시스템/미인증(ctx='')은 무필터."""
    oid = orgctx.current_org_id()
    return (" AND org_id=?", [oid]) if oid else ("", [])


def _token_dict(r: Any) -> dict[str, Any]:
    """토큰 행 → API dict. token_hash는 절대 포함하지 않는다."""
    d = dict(r)
    d.pop("token_hash", None)
    # 프론트엔드 계약의 표시용 prefix — 토큰 id 앞 8자(원문 토큰 아님).
    d["prefix"] = str(d.get("id") or "")[:8]
    return d


def _truncate_attr_value(v: Any, limit: int = _ATTR_VALUE_LIMIT) -> Any:
    if isinstance(v, str) and len(v) > limit:
        return v[:limit]
    return v


def external_span_json(
    row: dict[str, Any], *, attr_limit: int = _ATTR_VALUE_LIMIT
) -> dict[str, Any]:
    """external_spans 행 → API 응답 dict. attributes는 파싱된 dict이며
    각 문자열 값을 attr_limit자로 절단한다(저장값은 건드리지 않는다)."""
    attrs = row.get("attributes")
    if isinstance(attrs, str):
        attrs = _loads(attrs)
    if not isinstance(attrs, dict):
        attrs = {}
    attrs = _json_safe(attrs)
    return {
        "id": row.get("id"),
        "trace_id": row.get("trace_id") or "",
        "span_id": row.get("span_id") or "",
        "parent_span_id": row.get("parent_span_id") or "",
        "name": row.get("name") or "",
        "kind": row.get("kind") or "",
        "started_at": row.get("started_at"),
        "ended_at": row.get("ended_at"),
        "run_id": row.get("run_id") or "",
        "attributes": {str(k): _truncate_attr_value(v, attr_limit) for k, v in attrs.items()},
    }


# external_logs.body 응답 절단 상한 — 저장값은 건드리지 않는다.
_LOG_BODY_LIMIT = 4096


def _json_safe(v: Any) -> Any:
    """비유한 float(NaN/±Inf) → None. strict JSON 응답 오염 방지 —
    레거시 행이나 PG REAL에 저장된 NaN도 읽기 경로에서 정규화한다."""
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if isinstance(v, dict):
        return {str(k): _json_safe(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_json_safe(x) for x in v]
    return v


def _attrs_dict(row: dict[str, Any]) -> dict[str, Any]:
    """attributes 컬럼(JSON 문자열 또는 dict) → dict."""
    attrs = row.get("attributes")
    if isinstance(attrs, str):
        attrs = _loads(attrs)
    return attrs if isinstance(attrs, dict) else {}


def _attrs_json(attrs: Any) -> str:
    """attributes → 저장용 JSON. 비유한 float는 _json_safe로 정규화."""
    if isinstance(attrs, str):
        return attrs
    return json.dumps(_json_safe(attrs if isinstance(attrs, dict) else {}), ensure_ascii=False)


def external_metric_json(
    row: dict[str, Any], *, attr_limit: int = _ATTR_VALUE_LIMIT
) -> dict[str, Any]:
    """external_metrics 행 → API 응답 dict(spec 009 US1 계약 필드)."""
    attrs = _json_safe(_attrs_dict(row))
    return {
        "id": row.get("id"),
        "metric_name": row.get("metric_name") or "",
        "metric_type": row.get("metric_type") or "",
        "value": _json_safe(row.get("value")),
        "unit": row.get("unit") or "",
        "attributes": {str(k): _truncate_attr_value(v, attr_limit) for k, v in attrs.items()},
        "timestamp": row.get("timestamp"),
    }


def external_log_json(
    row: dict[str, Any], *, attr_limit: int = _ATTR_VALUE_LIMIT
) -> dict[str, Any]:
    """external_logs 행 → API 응답 dict(spec 009 US2 계약 필드).
    body는 응답 시 _LOG_BODY_LIMIT자로 절단한다."""
    attrs = _json_safe(_attrs_dict(row))
    body = str(row.get("body") or "")
    if len(body) > _LOG_BODY_LIMIT:
        body = body[:_LOG_BODY_LIMIT]
    return {
        "id": row.get("id"),
        "trace_id": row.get("trace_id") or "",
        "span_id": row.get("span_id") or "",
        "severity": row.get("severity") or "",
        "severity_number": row.get("severity_number"),
        "severity_text": str(attrs.get("severityText") or ""),
        "body": body,
        "attributes": {str(k): _truncate_attr_value(v, attr_limit) for k, v in attrs.items()},
        "timestamp": row.get("timestamp"),
    }


class OtlpIngestStoreMixin:
    # ---- otlp_ingest_tokens ----
    def create_otlp_ingest_token(
        self,
        *,
        name: str,
        token_hash: str,
        created_by: str = "",
        org_id: str | None = None,
    ) -> dict[str, Any]:
        """ingest 토큰 등록 — token_hash(sha256 hex)만 저장, 원문은 받지 않는다."""
        now = _iso(_now())
        tid = uuid.uuid4().hex[:12]
        with self.tx() as c:
            c.execute(
                "INSERT INTO otlp_ingest_tokens"
                "(id,org_id,name,token_hash,created_by,created_at,last_used_at,revoked_at)"
                " VALUES(?,?,?,?,?,?,?,NULL)",
                (
                    tid,
                    orgctx.org_for_write(org_id),
                    (name or "")[:200],
                    token_hash,
                    (created_by or "")[:200],
                    now,
                    None,
                ),
            )
        return self.get_otlp_ingest_token(tid) or {"id": tid}

    def list_otlp_ingest_tokens(self, limit: int = 200) -> list[dict[str, Any]]:
        frag, params = _org_filter()
        cond = frag.replace(" AND ", " WHERE ", 1) if frag else ""
        rows = self._conn.execute(
            "SELECT * FROM otlp_ingest_tokens"  # noqa: S608
            + cond
            + " ORDER BY created_at DESC LIMIT ?",
            (*params, _clamp_limit(limit, 200, 1000)),
        ).fetchall()
        return [_token_dict(r) for r in rows]

    def get_otlp_ingest_token(self, token_id: str) -> dict[str, Any] | None:
        frag, params = _org_filter()
        row = self._conn.execute(
            "SELECT * FROM otlp_ingest_tokens WHERE id=?" + frag,  # noqa: S608
            (token_id, *params),
        ).fetchone()
        return _token_dict(row) if row else None

    def find_otlp_ingest_token_by_hash(self, token_hash: str) -> dict[str, Any] | None:
        """인증용 전역 조회 — token_hash는 전역 UNIQUE라 조직 필터 없이 찾는다.
        반환 dict에는 token_hash가 포함되지 않는다(내부에서도 원문 해시 불필요)."""
        row = self._conn.execute(
            "SELECT * FROM otlp_ingest_tokens WHERE token_hash=?", (token_hash,)
        ).fetchone()
        return _token_dict(row) if row else None

    def revoke_otlp_ingest_token(self, token_id: str) -> bool:
        """폐기 — 이미 폐기된 토큰도 rowcount 기준 성공으로 본다(멱등)."""
        frag, params = _org_filter()
        with self.tx() as c:
            cur = c.execute(
                "UPDATE otlp_ingest_tokens SET revoked_at=? WHERE id=?"  # noqa: S608
                " AND revoked_at IS NULL" + frag,
                (_iso(_now()), token_id, *params),
            )
        return cur.rowcount > 0 or self.get_otlp_ingest_token(token_id) is not None

    def touch_otlp_ingest_token(self, token_id: str) -> None:
        """last_used_at 갱신 — 60초 스로틀로 인제스트 트래픽의 쓰기 증폭을 막는다."""
        cutoff = _iso(_now() - timedelta(seconds=_LAST_USED_MIN_INTERVAL_S))
        with self.tx() as c:
            c.execute(
                "UPDATE otlp_ingest_tokens SET last_used_at=? WHERE id=?"  # noqa: S608
                " AND (last_used_at IS NULL OR last_used_at < ?)",
                (_iso(_now()), token_id, cutoff),
            )

    # ---- external_spans ----
    def insert_external_spans(self, rows: list[dict[str, Any]]) -> int:
        """외부 스팬 벌크 insert. rows 항목: trace_id, span_id,
        parent_span_id, name, kind, started_at, ended_at, attributes(dict),
        run_id, org_id(명시 시 그 조직, 아니면 ctx)."""
        if not rows:
            return 0
        now = _iso(_now())
        created = 0
        with self.tx() as c:
            for r in rows:
                attrs = r.get("attributes")
                attrs_json = _attrs_json(attrs)
                c.execute(
                    "INSERT INTO external_spans"
                    "(id,org_id,trace_id,span_id,parent_span_id,name,kind,"
                    "started_at,ended_at,attributes,error_flag,run_id,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        str(r.get("id") or uuid.uuid4().hex)[:64],
                        orgctx.org_for_write(str(r.get("org_id") or "") or None),
                        str(r.get("trace_id") or "")[:128],
                        str(r.get("span_id") or "")[:128],
                        str(r.get("parent_span_id") or "")[:128],
                        str(r.get("name") or "")[:500],
                        str(r.get("kind") or "")[:40],
                        str(r.get("started_at") or now),
                        r.get("ended_at") or None,
                        attrs_json,
                        # spec 012 — tool-stats가 attributes를 다시 읽지 않도록
                        # 삽입 시점에 오류 여부를 플래그로 저장한다.
                        1 if span_attrs_is_error(attrs) else 0,
                        str(r.get("run_id") or "")[:64],
                        now,
                    ),
                )
                created += 1
        return created

    def list_external_spans(
        self,
        *,
        days: int = 7,
        trace_id: str = "",
        run_id: str = "",
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """org 스코프 외부 스팬 목록 — 최신 started_at 순."""
        days = max(1, min(int(days), 90))
        cutoff = _iso(_now() - timedelta(days=days))
        frag, params = _org_filter()
        conds = ["started_at >= ?"]
        qparams: list[Any] = [cutoff]
        if trace_id:
            conds.append("trace_id=?")
            qparams.append(str(trace_id)[:128])
        if run_id:
            conds.append("run_id=?")
            qparams.append(str(run_id)[:64])
        rows = self._conn.execute(
            "SELECT * FROM external_spans WHERE "  # noqa: S608
            + " AND ".join(conds)
            + frag
            + " ORDER BY started_at DESC LIMIT ?",
            (*qparams, *params, _clamp_limit(limit, 500, 500)),
        ).fetchall()
        return [dict(r) for r in rows]

    def external_spans_for_run(self, run_id: str, limit: int = 200) -> list[dict[str, Any]]:
        """run 타임라인 확장용 — run_id로 링크된 외부 스팬(org 스코프)."""
        frag, params = _org_filter()
        rows = self._conn.execute(
            "SELECT * FROM external_spans WHERE run_id=?"  # noqa: S608
            + frag
            + " ORDER BY started_at LIMIT ?",
            (run_id, *params, _clamp_limit(limit, 200, 500)),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- external_metrics (spec 009 US1) ----
    def insert_external_metrics(self, rows: list[dict[str, Any]]) -> int:
        """외부 메트릭 데이터포인트 벌크 insert.
        rows 항목: metric_name, metric_type, value, unit, attributes(dict),
        timestamp, org_id(명시 시 그 조직, 아니면 ctx)."""
        if not rows:
            return 0
        now = _iso(_now())
        created = 0
        with self.tx() as c:
            for r in rows:
                attrs_json = _attrs_json(r.get("attributes"))
                raw_value = r.get("value")
                try:
                    value = float(raw_value) if raw_value is not None else None
                except (TypeError, ValueError):
                    value = None
                if value is not None and not math.isfinite(value):
                    value = None
                c.execute(
                    "INSERT INTO external_metrics"
                    "(id,org_id,metric_name,metric_type,value,unit,attributes,"
                    "timestamp,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        str(r.get("id") or uuid.uuid4().hex)[:64],
                        orgctx.org_for_write(str(r.get("org_id") or "") or None),
                        str(r.get("metric_name") or "")[:500],
                        str(r.get("metric_type") or "")[:40],
                        value,
                        str(r.get("unit") or "")[:100],
                        attrs_json,
                        str(r.get("timestamp") or now),
                        now,
                    ),
                )
                created += 1
        return created

    def list_external_metrics(
        self,
        *,
        days: int = 7,
        metric_name: str = "",
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """org 스코프 외부 메트릭 목록 — 최신 timestamp 순."""
        days = max(1, min(int(days), 90))
        cutoff = _iso(_now() - timedelta(days=days))
        frag, params = _org_filter()
        conds = ["timestamp >= ?"]
        qparams: list[Any] = [cutoff]
        if metric_name:
            conds.append("metric_name=?")
            qparams.append(str(metric_name)[:500])
        rows = self._conn.execute(
            "SELECT * FROM external_metrics WHERE "  # noqa: S608
            + " AND ".join(conds)
            + frag
            + " ORDER BY timestamp DESC LIMIT ?",
            (*qparams, *params, _clamp_limit(limit, 200, 1000)),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- external_logs (spec 009 US2) ----
    def insert_external_logs(self, rows: list[dict[str, Any]]) -> int:
        """외부 로그 레코드 벌크 insert.
        rows 항목: trace_id, span_id, severity, severity_number, body,
        attributes(dict), timestamp, org_id(명시 시 그 조직, 아니면 ctx)."""
        if not rows:
            return 0
        now = _iso(_now())
        created = 0
        with self.tx() as c:
            for r in rows:
                attrs_json = _attrs_json(r.get("attributes"))
                sev_num = r.get("severity_number")
                try:
                    sev_num_i = int(sev_num) if sev_num is not None else None
                except (TypeError, ValueError):
                    sev_num_i = None
                body = str(r.get("body") or "")[:8192]
                c.execute(
                    "INSERT INTO external_logs"
                    "(id,org_id,trace_id,span_id,severity,severity_number,body,"
                    "attributes,timestamp,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        str(r.get("id") or uuid.uuid4().hex)[:64],
                        orgctx.org_for_write(str(r.get("org_id") or "") or None),
                        str(r.get("trace_id") or "")[:128],
                        str(r.get("span_id") or "")[:128],
                        str(r.get("severity") or "")[:40],
                        sev_num_i,
                        body,
                        attrs_json,
                        str(r.get("timestamp") or now),
                        now,
                    ),
                )
                created += 1
        return created

    @staticmethod
    def _like_escape(s: str) -> str:
        """LIKE 패턴 이스케이프 — %/_/\\를 리터럴로."""
        return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    def list_external_logs(
        self,
        *,
        days: int = 7,
        trace_id: str = "",
        severity: str = "",
        q: str = "",
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """org 스코프 외부 로그 목록 — 최신 timestamp 순.

        spec 012 US3 — q는 body/severityText 부분일치. FTS5는 SQLite
        전용이라 PG 호환을 위해 LIKE + ESCAPE '\\'를 쓴다. severityText는
        컬럼이 아니라 attributes JSON 안의 키라 attributes 텍스트의
        `"severityText": "...` 패턴으로 근사 매칭한다(다른 키의 값이
        우연히 같은 부분문자열을 포함하면 오탐 가능 — 허용된 근사)."""
        days = max(1, min(int(days), 90))
        cutoff = _iso(_now() - timedelta(days=days))
        frag, params = _org_filter()
        conds = ["timestamp >= ?"]
        qparams: list[Any] = [cutoff]
        if trace_id:
            conds.append("trace_id=?")
            qparams.append(str(trace_id)[:128])
        if severity:
            conds.append("severity=?")
            qparams.append(str(severity).upper()[:40])
        qv = str(q or "").strip()[:200]
        if qv:
            esc = self._like_escape(qv)
            conds.append("(body LIKE ? ESCAPE '\\' OR attributes LIKE ? ESCAPE '\\')")
            qparams.extend([f"%{esc}%", f'%"severityText": "%{esc}%'])
        rows = self._conn.execute(
            "SELECT * FROM external_logs WHERE "  # noqa: S608
            + " AND ".join(conds)
            + frag
            + " ORDER BY timestamp DESC LIMIT ?",
            (*qparams, *params, _clamp_limit(limit, 200, 1000)),
        ).fetchall()
        return [dict(r) for r in rows]
