"""Prometheus 메트릭 레지스트리 + exposition — spec 004 US5 (FR-029~FR-033).

런타임 의존성 없이 counter/gauge/histogram을 인메모리로 유지하고
Prometheus text exposition format 0.0.4를 직접 렌더한다.

FR-031 라벨 카디널리티: user_id/email/run_id/request_id/raw path는
라벨 값으로 절대 나가지 않는다 — 금지 키는 렌더 단계에서 드롭하고,
HTTP route는 라우트 템플릿 또는 "unmatched"만 허용한다.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime
from typing import Any

logger = logging.getLogger("jobmate.metrics")

_LOCK = threading.RLock()
# (name, labels_tuple) -> value. labels_tuple은 정렬된 (key, value) 쌍.
_COUNTERS: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
_GAUGES: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
_HISTOGRAMS: dict[tuple[str, tuple[tuple[str, str], ...]], dict[str, Any]] = {}

DEFAULT_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
_HTTP_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)

# FR-031 — 이 이름의 라벨은 렌더 단계에서 드롭한다(값이 아니라 키 기준).
_DENIED_LABEL_KEYS = frozenset(
    {
        "user_id",
        "userid",
        "email",
        "run_id",
        "request_id",
        "session_id",
        "actor",
        "token",
        "authorization",
        "cookie",
        "set_cookie",
        "password",
        "secret",
        "path",
        "raw_path",
        "query",
        "query_string",
    }
)
_MAX_LABEL_VALUE = 120
_LABEL_KEY_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-")
# route 라벨 전용 — raw path의 id 유사 세그먼트 탐지.
_ROUTE_IDISH_RE = re.compile(r"[0-9a-fA-F]{8,}|\d{6,}|^[a-zA-Z]+_[0-9A-Za-z]{6,}$")
# model/workflow/event/component 등 유한 집합 바인딩용 값 형식 검사.
_SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-/]{0,63}$")

# FR-030 유한 라벨 집합.
_RUN_STATUSES = frozenset(
    {
        "queued",
        "running",
        "waiting_approval",
        "waiting_review",
        "completed",
        "failed",
        "rejected",
        "cancelled",
        "budget_exhausted",
        "needs_human",
    }
)
_WEBHOOK_EVENT_TYPES = frozenset(
    {
        "run.completed",
        "run.failed",
        "run.needs_human",
        "run.waiting_approval",
        "approval.requested",
        "approval.decided",
        "budget.exceeded",
        "backup.failed",
        "backup.verified",
        "health.degraded",
        "error.unhandled",
    }
)
_WEBHOOK_DELIVERY_STATUSES = frozenset(
    {"pending", "delivering", "success", "failed", "dead", "cancelled"}
)
_HEALTH_COMPONENTS = frozenset(
    {"api", "db", "llm", "gmail", "messenger", "disk", "backup", "webhook", "cron"}
)
_HEALTH_STATES = frozenset({"ok", "degraded", "down"})
_IN_FLIGHT_STATUSES = ("queued", "running", "waiting_approval", "waiting_review")

# DB 기반 메트릭 TTL 캐시(FR-033) — 요청 핫패스에 store 쿼리를 두지 않는다.
_DB_TTL_SECONDS = 15.0
_db_cache_at = 0.0
_db_cache_lines: list[str] | None = None


# ---------- 라벨 정제 (FR-031) ----------


def normalize_label_value(value: Any) -> str:
    """라벨 값을 문자열화·정제한다. 제어문자/개행 제거, 길이 상한."""
    s = str(value if value is not None else "")
    s = "".join(ch for ch in s if ch >= " " and ch != "\x7f")
    return s[:_MAX_LABEL_VALUE]


def _route_value_ok(value: str) -> bool:
    """route 라벨 값이 템플릿/허용 형태인지 검사 (FR-031).

    허용: "unmatched", "{param}" 포함 템플릿, id 유사 세그먼트가 없는
    정적 경로. run_id/uuid/토큰 형태의 raw path는 드롭한다."""
    if value == "unmatched" or "{" in value:
        return True
    if not value.startswith("/"):
        return False
    for seg in value.strip("/").split("/"):
        if _ROUTE_IDISH_RE.search(seg):
            return False
    return True


def _clean_labels(labels: dict[str, Any] | None) -> dict[str, str]:
    """금지 키·고카디널리티 값이 섞인 라벨을 렌더 전에 제거한다."""
    out: dict[str, str] = {}
    for k, v in (labels or {}).items():
        key = str(k)
        if not _LABEL_KEY_RE.match(key):
            continue
        if key.lower() in _DENIED_LABEL_KEYS:
            continue
        val = normalize_label_value(v)
        # 이메일(@ 포함)·UUID 형태는 식별자 유출 위험이 있어 드롭한다.
        if "@" in val or _UUID_RE.match(val):
            continue
        if key == "route" and not _route_value_ok(val):
            continue
        out[key] = val
    return out


def _escape_label_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _labels_str(labels: dict[str, str]) -> str:
    if not labels:
        return ""
    inner = ",".join(f'{k}="{_escape_label_value(v)}"' for k, v in sorted(labels.items()))
    return "{" + inner + "}"


def _emit(
    lines: list[str],
    name: str,
    labels: dict[str, str],
    value: float,
    type_: str,
    help_text: str,
    emitted: set[tuple[str, str]],
) -> None:
    key = (name, type_)
    if key not in emitted:
        emitted.add(key)
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {type_}")
    lines.append(f"{name}{_labels_str(labels)} {value}")


# ---------- 인메모리 레지스트리 ----------


def _series_key(
    name: str, labels: dict[str, Any] | None
) -> tuple[str, tuple[tuple[str, str], ...]]:
    return (name, tuple(sorted(_clean_labels(labels).items())))


def inc_counter(name: str, labels: dict[str, Any] | None = None, value: float = 1.0) -> None:
    """Counter 증가 — thread-safe."""
    key = _series_key(name, labels)
    with _LOCK:
        _COUNTERS[key] = _COUNTERS.get(key, 0.0) + float(value)


def set_gauge(name: str, value: float, labels: dict[str, Any] | None = None) -> None:
    """Gauge 설정 — thread-safe."""
    key = _series_key(name, labels)
    with _LOCK:
        _GAUGES[key] = float(value)


def observe_histogram(
    name: str,
    value: float,
    labels: dict[str, Any] | None = None,
    buckets: tuple[float, ...] | None = None,
) -> None:
    """Histogram 관측 — thread-safe."""
    key = _series_key(name, labels)
    with _LOCK:
        h = _HISTOGRAMS.get(key)
        if h is None:
            h = {
                "buckets": tuple(sorted(buckets or DEFAULT_BUCKETS)),
                "counts": [0] * (len(buckets or DEFAULT_BUCKETS) + 1),
                "sum": 0.0,
                "count": 0,
            }
            _HISTOGRAMS[key] = h
        v = float(value)
        h["sum"] += v
        h["count"] += 1
        # counts[i] = le=upper bound i 인 버킷 이하 관측 수(누적은 렌더 시 계산)
        for i, b in enumerate(h["buckets"]):
            if v <= b:
                h["counts"][i] += 1
                break
        else:
            h["counts"][-1] += 1


def observe_http_request(method: str, route: str, status: int, duration_seconds: float) -> None:
    """HTTP 요청 1건 관측 — counter + duration histogram (FR-030/033).

    route는 호출자가 라우트 템플릿을 넘겨야 한다("unmatched" 허용).
    원시 path·파라미터 값은 절대 넘기지 않는다."""
    m = (method or "").upper()[:10] or "UNKNOWN"
    r = route if isinstance(route, str) and route.startswith("/") else "unmatched"
    try:
        code = int(status)
    except (TypeError, ValueError):
        code = 0
    status_class = f"{code // 100}xx" if 100 <= code <= 599 else "other"
    inc_counter(
        "jobmate_http_requests_total",
        {"method": m, "route": r, "status": status_class},
    )
    try:
        d = max(0.0, float(duration_seconds))
    except (TypeError, ValueError):
        d = 0.0
    observe_histogram(
        "jobmate_http_request_duration_seconds",
        d,
        {"route": r},
        buckets=_HTTP_BUCKETS,
    )


# ---------- 라우트 템플릿 해석 ----------


def route_template(routes: Any, path: str) -> str:
    """등록 라우트 목록에서 path에 매칭되는 라우트 템플릿 반환.

    미들웨어는 라우팅 전에 실행되므로 scope["route"]를 쓸 수 없는 경로를
    위해 등록 패턴을 직접 매칭한다. 매칭 실패 시 "unmatched"."""
    try:
        for route in routes or []:
            regex = getattr(route, "path_regex", None)
            if regex is None:
                continue
            if regex.match(path):
                tmpl = getattr(route, "path", "") or ""
                if tmpl.startswith("/"):
                    return tmpl
        return "unmatched"
    except Exception:
        logger.debug("route_template match failed", exc_info=True)
        return "unmatched"


# ---------- exposition 렌더 ----------


def _render_registry(lines: list[str], emitted: set[tuple[str, str]]) -> None:
    with _LOCK:
        counters = sorted(_COUNTERS.items())
        gauges = sorted(_GAUGES.items())
        histograms = sorted(_HISTOGRAMS.items())
    for (name, lkey), value in counters:
        _emit(lines, name, dict(lkey), value, "counter", "jobMate counter.", emitted)
    for (name, lkey), value in gauges:
        _emit(lines, name, dict(lkey), value, "gauge", "jobMate gauge.", emitted)
    for (name, lkey), h in histograms:
        labels = dict(lkey)
        buckets = h["buckets"]
        counts = h["counts"]
        running = 0
        for i, b in enumerate(buckets):
            running += counts[i]
            le_labels = dict(labels)
            le_labels["le"] = _fmt_float(b)
            _emit(
                lines,
                f"{name}_bucket",
                le_labels,
                float(running),
                "counter",
                f"{name} histogram buckets.",
                emitted,
            )
        le_labels = dict(labels)
        le_labels["le"] = "+Inf"
        _emit(
            lines,
            f"{name}_bucket",
            le_labels,
            float(h["count"]),
            "counter",
            f"{name} histogram buckets.",
            emitted,
        )
        _emit(
            lines,
            f"{name}_sum",
            labels,
            float(h["sum"]),
            "counter",
            f"{name} histogram sum.",
            emitted,
        )
        _emit(
            lines,
            f"{name}_count",
            labels,
            float(h["count"]),
            "counter",
            f"{name} histogram count.",
            emitted,
        )


def _fmt_float(v: float) -> str:
    if v == int(v):
        return str(int(v))
    return repr(v)


# ---------- DB 기반 메트릭 (TTL 캐시) ----------


def _query(store: Any, sql: str, params: tuple[Any, ...] = ()) -> list[Any]:
    """store._conn 직접 조회 — services.health와 동일한 내부 접근 패턴."""
    return list(store._conn.execute(sql, params).fetchall())  # noqa: SLF001


def _bound_workflow(value: Any, known: set[str]) -> str:
    s = normalize_label_value(value)
    if s in known and _SAFE_TOKEN_RE.match(s):
        return s
    return "other"


def _known_workflows(store: Any) -> set[str]:
    keys: set[str] = set()
    try:
        from ..workflows import WORKFLOWS

        keys.update(str(k) for k in WORKFLOWS)
    except Exception:
        logger.debug("metrics workflow registry import failed", exc_info=True)
    try:
        for r in _query(store, "SELECT DISTINCT name FROM custom_workflows"):
            keys.add(str(r["name"]))
    except Exception:
        logger.debug("metrics custom_workflows query failed", exc_info=True)
    return keys


def _collect_runs(store: Any, lines: list[str], emitted: set[tuple[str, str]]) -> None:
    try:
        rows = _query(
            store,
            "SELECT workflow_key,status,COUNT(*) AS n FROM runs GROUP BY workflow_key,status",
        )
    except Exception:
        logger.debug("metrics runs query failed", exc_info=True)
        return
    known = _known_workflows(store)
    for r in rows:
        wf = _bound_workflow(r["workflow_key"], known)
        st = str(r["status"] or "")
        if st not in _RUN_STATUSES:
            st = "other"
        _emit(
            lines,
            "jobmate_runs",
            {"workflow": wf, "status": st},
            float(r["n"]),
            "gauge",
            "jobMate runs by workflow and status (current snapshot, not cumulative).",
            emitted,
        )
    try:
        ph = ",".join("?" for _ in _IN_FLIGHT_STATUSES)
        row = _query(
            store,
            f"SELECT COUNT(*) AS n FROM runs WHERE status IN ({ph})",  # noqa: S608
            tuple(_IN_FLIGHT_STATUSES),
        )
        n = int(row[0]["n"]) if row else 0
    except Exception:
        logger.debug("metrics runs in-flight query failed", exc_info=True)
        return
    _emit(
        lines,
        "jobmate_runs_in_flight",
        {},
        float(n),
        "gauge",
        "jobMate runs currently queued/running/waiting.",
        emitted,
    )


def _collect_webhooks(store: Any, lines: list[str], emitted: set[tuple[str, str]]) -> None:
    try:
        rows = _query(
            store,
            "SELECT event_type,status,COUNT(*) AS n FROM webhook_deliveries"
            " GROUP BY event_type,status",
        )
        for r in rows:
            ev = str(r["event_type"] or "")
            if ev not in _WEBHOOK_EVENT_TYPES:
                ev = "other"
            st = str(r["status"] or "")
            if st not in _WEBHOOK_DELIVERY_STATUSES:
                st = "other"
            _emit(
                lines,
                "jobmate_webhook_deliveries",
                {"event": ev, "status": st},
                float(r["n"]),
                "gauge",
                "jobMate webhook deliveries by event and status"
                " (current snapshot, not cumulative).",
                emitted,
            )
    except Exception:
        # 테이블 미존재(스펙 004 US1 이전 DB)도 exposition을 죽이지 않는다.
        logger.debug("metrics webhook deliveries query failed", exc_info=True)
    try:
        counts = store.webhook_delivery_status_counts()
    except Exception:
        logger.debug("metrics webhook status counts failed", exc_info=True)
        return
    pending = int(counts.get("pending", 0)) + int(counts.get("delivering", 0))
    dead = int(counts.get("dead", 0))
    _emit(
        lines,
        "jobmate_webhook_pending",
        {},
        float(pending),
        "gauge",
        "jobMate webhook deliveries pending/delivering.",
        emitted,
    )
    _emit(
        lines,
        "jobmate_webhook_dead",
        {},
        float(dead),
        "gauge",
        "jobMate webhook deliveries dead-lettered.",
        emitted,
    )


def _collect_llm(store: Any, lines: list[str], emitted: set[tuple[str, str]]) -> None:
    try:
        rows = _query(
            store,
            "SELECT model,SUM(tokens_in) AS ti,SUM(tokens_out) AS to_"
            " FROM llm_usage GROUP BY model",
        )
        for r in rows:
            model = normalize_label_value(r["model"])
            if not model or model == "unknown":
                model = "unknown"
            elif not _SAFE_TOKEN_RE.match(model):
                model = "other"
            ti = int(r["ti"] or 0)
            to_ = int(r["to_"] or 0)
            _emit(
                lines,
                "jobmate_llm_tokens",
                {"model": model, "direction": "in"},
                float(ti),
                "gauge",
                "jobMate LLM tokens consumed (current snapshot, not cumulative).",
                emitted,
            )
            _emit(
                lines,
                "jobmate_llm_tokens",
                {"model": model, "direction": "out"},
                float(to_),
                "gauge",
                "jobMate LLM tokens consumed (current snapshot, not cumulative).",
                emitted,
            )
    except Exception:
        logger.debug("metrics llm usage query failed", exc_info=True)
    try:
        rows = _query(
            store,
            "SELECT model,SUM(cost_microusd) AS m FROM cost_events"
            " WHERE cost_microusd IS NOT NULL GROUP BY model",
        )
        for r in rows:
            model = normalize_label_value(r["model"])
            if not model:
                model = "unknown"
            elif not _SAFE_TOKEN_RE.match(model):
                model = "other"
            usd = float(r["m"] or 0) / 1_000_000.0
            _emit(
                lines,
                "jobmate_llm_cost_usd",
                {"model": model},
                usd,
                "gauge",
                "jobMate LLM cost in USD (current snapshot, not cumulative).",
                emitted,
            )
    except Exception:
        logger.debug("metrics cost events query failed", exc_info=True)


def _iso_to_epoch(value: Any) -> float:
    try:
        s = str(value or "").strip()
        if not s:
            return 0.0
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _collect_backup(store: Any, lines: list[str], emitted: set[tuple[str, str]]) -> None:
    try:
        latest = store.latest_backup_success()
    except Exception:
        logger.debug("metrics latest backup query failed", exc_info=True)
        latest = None
    ts = _iso_to_epoch((latest or {}).get("finished_at"))
    _emit(
        lines,
        "jobmate_backup_last_success_timestamp",
        {},
        ts,
        "gauge",
        "Unix timestamp of last successful backup.",
        emitted,
    )


def _collect_health(store: Any, lines: list[str], emitted: set[tuple[str, str]]) -> None:
    try:
        report = store.get_state("health_last_report", {}) or {}
    except Exception:
        logger.debug("metrics health state query failed", exc_info=True)
        return
    comps = report.get("components") or {}
    if not isinstance(comps, dict):
        return
    for name, status in comps.items():
        comp = str(name)
        if comp not in _HEALTH_COMPONENTS:
            comp = "other"
        st = str(status or "")
        if st not in _HEALTH_STATES:
            st = "other"
        _emit(
            lines,
            "jobmate_health_status",
            {"component": comp, "state": st},
            1.0,
            "gauge",
            "jobMate component health status (1 = current state).",
            emitted,
        )


def _collect_inbox(store: Any, lines: list[str], emitted: set[tuple[str, str]]) -> None:
    try:
        # 플랫폼 전역 미읽음 수 — store.inbox_unread_count()는 org ctx 필터를
        # 타므로 요청 조직의 값이 전역 TTL 캐시에 올라가는 오염이 있었다.
        # org 필터 없는 원시 COUNT로 직접 집계한다(inbox_items.read=0).
        row = _query(store, "SELECT COUNT(*) AS n FROM inbox_items WHERE read=0")
        n = int(row[0]["n"]) if row else 0
    except Exception:
        logger.debug("metrics inbox unread query failed", exc_info=True)
        return
    _emit(
        lines,
        "jobmate_inbox_unread",
        {},
        float(n),
        "gauge",
        "jobMate unread inbox items.",
        emitted,
    )


def _collect_db_lines(store: Any) -> list[str]:
    lines: list[str] = []
    emitted: set[tuple[str, str]] = set()
    for fn in (
        _collect_runs,
        _collect_webhooks,
        _collect_llm,
        _collect_backup,
        _collect_health,
        _collect_inbox,
    ):
        try:
            fn(store, lines, emitted)
        except Exception:
            logger.debug("metrics collector %s failed", fn.__name__, exc_info=True)
    return lines


def _db_lines(store: Any) -> list[str]:
    """TTL 캐시된 DB 메트릭 라인 — 만료 시에만 store를 조회한다(FR-033)."""
    global _db_cache_at, _db_cache_lines
    now = time.monotonic()
    with _LOCK:
        if _db_cache_lines is not None and now - _db_cache_at < _DB_TTL_SECONDS:
            return list(_db_cache_lines)
    try:
        lines = _collect_db_lines(store)
    except Exception:
        logger.debug("metrics db collect failed", exc_info=True)
        lines = []
    with _LOCK:
        _db_cache_at = time.monotonic()
        _db_cache_lines = lines
    return lines


def render_metrics(store: Any, settings: Any = None) -> str:
    """전체 exposition — 인메모리 HTTP 시리즈 + TTL 캐시 DB 메트릭."""
    lines: list[str] = []
    emitted: set[tuple[str, str]] = set()
    _render_registry(lines, emitted)
    lines.extend(_db_lines(store))
    return "\n".join(lines) + "\n"


def reset_cache() -> None:
    """테스트/운영용 — TTL 캐시와 인메모리 시리즈를 초기화한다."""
    global _db_cache_at, _db_cache_lines
    with _LOCK:
        _COUNTERS.clear()
        _GAUGES.clear()
        _HISTOGRAMS.clear()
        _db_cache_at = 0.0
        _db_cache_lines = None
