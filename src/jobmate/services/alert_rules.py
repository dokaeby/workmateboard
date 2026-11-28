"""spec 006 US1 — 알림 규칙 조건 평가 + 채널 디스패치.

- validate_condition: {field, op, value} 엄격 검증(라우터 422 매핑용 ValueError).
- condition_matches: 이벤트 컨텍스트 dict에 대한 조건 평가.
- emit_alert_event: 매칭 규칙별 dedup/cooldown 판정 후 채널 발송 +
  alert_rule_fires 기록 (FR-003/FR-005).
- emit_alert_event_async: run 경로를 블로킹하지 않는 daemon thread 래퍼.

이벤트 컨텍스트는 emit 호출자가 채우는 flat dict — 지원 키는
CONDITION_FIELDS 참조. 알 수 없는 키는 평가 시 None으로 취급된다.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from .. import mailer, messenger
from ..store import orgctx
from ..store.alerts import FIRE_FIRED, FIRE_PARTIAL
from ..store.db import Store
from ..store.util import _now

logger = logging.getLogger("jobmate.alerts")

# FR-002 — 허용 condition field.
CONDITION_FIELDS = frozenset(
    {
        "run.status",
        "run.workflow",
        "run.project",
        "run.cost_usd",
        "run.duration_ms",
        "stage.status",
        "stage.key",
        "budget.used_pct",
        "budget.scope",
        "budget.threshold",
        "schedule.missed",
        "schedule.id",
        "schedule.workflow",
        # spec 007 US4 — 신규 조건 필드
        "run.latency_ms",  # run.duration_ms의 alias (동일 값)
        "error.error_class",  # stage/run 실패 이벤트의 error_class
        "error.rate_pct",  # 주기 평가 — trailing window 실패율(%)
        "session.stale_count",  # 주기 평가 — stale/long_idle 세션 수
        "cost.delta_pct",  # 주기 평가 — 오늘 비용 vs 직전 7일 일평균 증감(%)
    }
)

# spec 007 US4 — 이벤트 컨텍스트에 값이 없고 주기 집계가 필요한 필드.
# emit_alert_event(이벤트 경로)에서는 평가하지 않고
# evaluate_periodic_rules(스윕/이벤트 경유 주기 평가)에서만 매칭한다.
PERIODIC_FIELDS = frozenset({"error.rate_pct", "session.stale_count", "cost.delta_pct"})

# 주기 평가 최소 간격 — emit 이벤트가 잦아도 SQL 집계는 이 간격으로 스로틀.
_PERIODIC_MIN_INTERVAL = 300.0
# (id(store), org_id) → 마지막 평가 monotonic 시각
_PERIODIC_LAST: dict[tuple[int, str], float] = {}
# error.rate_pct 집계 윈도우(분)
_ERROR_RATE_WINDOW_MIN = 60
# cost.delta_pct 비교 기간(일) — 오늘 vs 직전 7일 일평균
_COST_BASELINE_DAYS = 7

OPS = frozenset({"eq", "ne", "gt", "gte", "lt", "lte", "in", "contains"})
_NUMERIC_OPS = frozenset({"gt", "gte", "lt", "lte"})
_CHANNELS = frozenset({"inbox", "email", "slack", "telegram", "push"})
# rule.channels 항목 — 문자열 또는 {"type": ..., "to": ...} 객체.
# email의 to 미지정 시 created_by(보통 이메일)를 사용한다.


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def validate_condition(cond: Any) -> dict[str, Any]:
    """condition 스키마 검증 — 위반 시 ValueError(라우터가 422로 변환)."""
    if not isinstance(cond, dict):
        raise ValueError("condition must be an object")
    field = str(cond.get("field") or "").strip()
    op = str(cond.get("op") or "").strip().lower()
    if field not in CONDITION_FIELDS:
        raise ValueError(f"unknown condition field: {field or '(empty)'}")
    if op not in OPS:
        raise ValueError(f"unknown condition op: {op or '(empty)'}")
    value = cond.get("value")
    if op == "in":
        if not isinstance(value, list) or not value:
            raise ValueError("op 'in' requires a non-empty list value")
        if len(value) > 100:
            raise ValueError("op 'in' value list too long (max 100)")
    elif op in _NUMERIC_OPS:
        if not _is_num(value):
            raise ValueError(f"op '{op}' requires a numeric value")
    elif op == "contains":
        if not isinstance(value, str) or not value:
            raise ValueError("op 'contains' requires a non-empty string value")
    else:  # eq/ne — 문자열/숫자/불리언 모두 허용하되 None은 거부
        if value is None:
            raise ValueError("condition value must not be null")
    return {"field": field, "op": op, "value": value}


def validate_channels(channels: Any) -> list[Any]:
    """channels 배열 검증 — 각 항목은 알려진 채널 문자열 또는 dict."""
    if channels is None:
        return []
    if not isinstance(channels, list):
        raise ValueError("channels must be a list")
    if len(channels) > len(_CHANNELS) * 2:
        raise ValueError("too many channels")
    out: list[Any] = []
    for ch in channels:
        if isinstance(ch, str):
            name = ch.strip().lower()
            if name not in _CHANNELS:
                raise ValueError(f"unknown channel: {ch}")
            out.append(name)
        elif isinstance(ch, dict):
            name = str(ch.get("type") or ch.get("channel") or "").strip().lower()
            if name not in _CHANNELS:
                raise ValueError(f"unknown channel: {name or ch}")
            item: dict[str, Any] = {"type": name}
            to = ch.get("to") or ch.get("target")
            if to is not None:
                item["to"] = str(to)[:500]
            out.append(item)
        else:
            raise ValueError("channel entries must be strings or objects")
    # 중복 제거(문자열 기준) — dict 항목은 type+to 조합으로 구분
    seen: set[str] = set()
    deduped: list[Any] = []
    for ch in out:
        key = ch if isinstance(ch, str) else f"{ch['type']}:{ch.get('to', '')}"
        if key not in seen:
            seen.add(key)
            deduped.append(ch)
    return deduped


def _channel_name(ch: Any) -> str:
    if isinstance(ch, str):
        return ch
    return str(ch.get("type") or ch.get("channel") or "")


def _channel_target(ch: Any) -> str:
    if not isinstance(ch, dict):
        return ""
    return str(ch.get("to") or ch.get("target") or "")


def _eq(a: Any, b: Any) -> bool:
    if _is_num(a) and _is_num(b):
        return float(a) == float(b)
    if isinstance(a, bool) or isinstance(b, bool):
        return bool(a) == bool(b)
    return str(a) == str(b)


def _compare(a: Any, b: Any) -> int | None:
    """a - b 부호. 비교 불가 타입이면 None."""
    if _is_num(a) and _is_num(b):
        return (float(a) > float(b)) - (float(a) < float(b))
    if isinstance(a, str) and isinstance(b, str):
        return (a > b) - (a < b)
    return None


def condition_matches(condition: dict[str, Any], fields: dict[str, Any]) -> bool:
    """검증된 condition을 이벤트 필드 dict에 평가. 빈 condition은 매칭 안 함."""
    try:
        cond = validate_condition(condition)
    except ValueError:
        return False
    actual = fields.get(cond["field"])
    op = cond["op"]
    want = cond["value"]
    if op == "eq":
        return _eq(actual, want)
    if op == "ne":
        return not _eq(actual, want)
    if op in _NUMERIC_OPS:
        cmp = _compare(actual, want)
        if cmp is None:
            return False
        return {"gt": cmp > 0, "gte": cmp >= 0, "lt": cmp < 0, "lte": cmp <= 0}[op]
    if op == "in":
        return any(_eq(actual, v) for v in want)
    if op == "contains":
        if isinstance(actual, str):
            return str(want) in actual
        if isinstance(actual, dict):
            return any(_eq(k, want) or _eq(v, want) for k, v in actual.items())
        if isinstance(actual, (list, tuple)):
            return any(_eq(item, want) for item in actual)
        return False
    return False


def _inbox_send(store: Store, rule: dict[str, Any], fields: dict[str, Any]) -> str:
    name = str(rule.get("name") or "alert rule")
    store.upsert_inbox_item(
        kind="alert",
        title=f"알림 규칙 발동: {name}",
        body=_alert_body(fields),
        link="/?view=office",
        dedupe_key=f"alert:{rule.get('id')}:{fields.get('_fingerprint', '')}"[:200],
        reset_read=False,
    )
    return "sent"


def _alert_body(fields: dict[str, Any]) -> str:
    parts = []
    for k in sorted(fields):
        if k.startswith("_"):
            continue
        v = fields[k]
        if isinstance(v, (dict, list)):
            continue
        s = str(v)
        parts.append(f"{k}={s[:120]}")
        if len(parts) >= 8:
            break
    return "; ".join(parts)[:500]


def _email_send(rule: dict[str, Any], fields: dict[str, Any], to: str = "") -> str:
    """SMTP/Gmail 백엔드가 설정돼 있을 때만 발송 — mailer._deliver 재사용."""
    recipient = (to or "").strip()
    if not recipient:
        recipient = str(rule.get("created_by") or "").strip()
    if not recipient or "@" not in recipient:
        return "skipped_unconfigured"
    if not mailer.smtp_configured():
        # Gmail API 백엔드는 인증 컨텍스트가 필요해 여기서는 지원하지 않는다 —
        # SMTP 미설정이면 unconfigured로 기록한다.
        return "skipped_unconfigured"
    name = str(rule.get("name") or "alert rule")
    subject = f"[jobMate] 알림 규칙 발동: {name}"
    body = f"jobMate 알림 규칙이 발동했습니다.\n\n{_alert_body(fields)}\n"
    try:
        ok = mailer._deliver(recipient, subject, body, None, "alert", "")  # noqa: SLF001
    except Exception as e:
        logger.warning("alert email send failed: %s", type(e).__name__)
        return "failed"
    return "sent" if ok else "failed"


def _slack_send(fields: dict[str, Any]) -> str:
    url = os.environ.get("JOBMATE_SLACK_WEBHOOK_URL", "")
    if not url:
        return "skipped_unconfigured"
    try:
        ok = messenger.send_slack(url, f"[jobMate] {_alert_body(fields)}")
    except Exception:
        logger.warning("alert slack send error", exc_info=True)
        return "failed"
    return "sent" if ok else "failed"


def _telegram_send(fields: dict[str, Any]) -> str:
    token = os.environ.get("JOBMATE_TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("JOBMATE_TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return "skipped_unconfigured"
    try:
        ok = messenger.send_telegram(token, chat_id, f"[jobMate] {_alert_body(fields)}")
    except Exception:
        logger.warning("alert telegram send error", exc_info=True)
        return "failed"
    return "sent" if ok else "failed"


def _push_send(store: Store, fields: dict[str, Any]) -> str:
    """Web Push — VAPID 키 + 구독이 모두 있어야 configured로 본다."""
    if not os.environ.get("VAPID_PRIVATE_KEY", ""):
        return "skipped_unconfigured"
    subs = store.list_push_subscriptions()
    if not subs:
        return "skipped_unconfigured"
    from ..adapters.push import WebPushNotifier

    try:
        notifier = WebPushNotifier(
            store,
            private_key=os.environ.get("VAPID_PRIVATE_KEY", ""),
            subject=os.environ.get("VAPID_SUBJECT", "mailto:admin@localhost"),
        )
        notifier.send(title="[jobMate] 알림 규칙 발동", body=_alert_body(fields), source_url="/")
    except Exception:
        logger.warning("alert push send error", exc_info=True)
        return "failed"
    return "sent"


def dispatch_channel(
    store: Store, channel: Any, rule: dict[str, Any], fields: dict[str, Any]
) -> dict[str, str]:
    """단일 채널 발송 — {"channel": name, "status": sent|skipped_unconfigured|failed}."""
    name = _channel_name(channel)
    target = _channel_target(channel)
    try:
        if name == "inbox":
            status = _inbox_send(store, rule, fields)
        elif name == "email":
            status = _email_send(rule, fields, to=target)
        elif name == "slack":
            status = _slack_send(fields)
        elif name == "telegram":
            status = _telegram_send(fields)
        elif name == "push":
            status = _push_send(store, fields)
        else:
            status = "skipped_unconfigured"
    except Exception:
        logger.warning("alert channel %s dispatch failed", name, exc_info=True)
        status = "failed"
    logger.info(
        "alert channel dispatch rule=%s channel=%s status=%s",
        rule.get("id"),
        name,
        status,
    )
    return {"channel": name, "status": status}


def emit_alert_event(
    store: Store,
    *,
    event_fingerprint: str,
    fields: dict[str, Any],
    org_id: str = "",
) -> int:
    """이벤트 평가 — org_scope(org_id) 안에서 매칭 규칙을 발송한다.

    반환: 실제 채널 발송이 발생한(fired) 규칙 수.
    멱등: (rule_id, event_fingerprint) 기존 fire가 있으면 재발송하지 않는다.
    cooldown 내 재매칭은 suppressed_cooldown으로 기록하고 채널을 호출하지 않는다.
    """
    fp = str(event_fingerprint or "")[:300]
    if not fp:
        return 0
    ctx_fields = dict(fields or {})
    ctx_fields["_fingerprint"] = fp
    fired = 0
    oid = str(org_id or "")
    if oid and orgctx.current_org_id() != oid:
        with orgctx.org_scope(oid):
            return emit_alert_event(store, event_fingerprint=fp, fields=fields, org_id=oid)
    rules = store.list_alert_rules(enabled_only=True)
    if not rules:
        return 0
    # 조직 격리 — 이벤트의 org와 다른 org의 규칙은 평가하지 않는다.
    # ctx=''(_org_filter 무필터) 경로에서도 org-b 규칙이 플랫폼 run에
    # 발화하는 것을 막는다. org_scope(oid) 안에서는 이미 동일 org만
    # 반환되지만, oid='' 이벤트는 명시 필터가 필요하다.
    rules = [r for r in rules if str(r.get("org_id") or "") == oid]
    if not rules:
        return 0
    now = _now()
    for rule in rules:
        # 주기 집계 필드 규칙은 이벤트 컨텍스트에 값이 없으므로 여기서
        # 건너뛴다 — ne/eq가 None에 매칭되는 오발화를 막는다. 평가는
        # 아래 evaluate_periodic_rules가 담당한다.
        if _condition_field(rule) in PERIODIC_FIELDS:
            continue
        if not condition_matches(rule.get("condition") or {}, ctx_fields):
            continue
        fired += _claim_and_dispatch(store, rule, ctx_fields, fp, now)
    # spec 007 US4 — 주기 집계 필드 규칙 평가. emit 경로(직접 emit +
    # jobs._alert_rule_sweep 경유 emit 포함)마다 호출되며 내부 스로틀로
    # SQL 집계 빈도를 제한한다. 실패가 이벤트 평가를 깨지 않게 삼킨다.
    try:
        fired += evaluate_periodic_rules(store, org_id=oid)
    except Exception:
        logger.warning("periodic alert eval failed org=%s", oid, exc_info=True)
    return fired


def _condition_field(rule: dict[str, Any]) -> str:
    """rule.condition.field — 미검증/누락 시 ''."""
    return str((rule.get("condition") or {}).get("field") or "")


def _claim_and_dispatch(
    store: Store,
    rule: dict[str, Any],
    ctx_fields: dict[str, Any],
    fp: str,
    now: datetime,
) -> int:
    """매칭된 단일 규칙의 dedup/cooldown claim + 채널 발송.

    반환: 실제 발송이 발생했으면 1, dedup/cooldown 억제면 0.
    emit_alert_event와 evaluate_periodic_rules가 공유한다."""
    rid = str(rule.get("id") or "")
    if not rid:
        return 0
    # dedup/cooldown 판정 + fire claim을 store의 단일 tx로 원자화 —
    # 직접 emit·스윕·비동기·주기 평가 경로가 동시에 들어와도 채널 발송은
    # claim에 성공한 emitter 1명뿐이다 (check-then-act 경합 제거).
    try:
        cooldown = int(rule.get("cooldown_minutes") or 0)
    except (TypeError, ValueError):
        cooldown = 0
    fire_id = store.claim_alert_fire(
        rule_id=rid,
        event_fingerprint=fp,
        cooldown_minutes=cooldown,
        org_id=str(rule.get("org_id") or "") or None,
        now=now,
    )
    if fire_id is None:
        # 같은 fingerprint의 fire가 이미 있으면 조용히 skip(멱등),
        # 없으면 규칙 쿨다운 억제 — suppressed_cooldown을 1회 기록한다.
        if store.find_alert_fire(rid, fp) is None:
            store.record_suppressed_fire(
                rule_id=rid,
                event_fingerprint=fp,
                org_id=str(rule.get("org_id") or "") or None,
            )
            logger.info("alert rule %s suppressed cooldown fp=%s", rid, fp[:60])
        return 0
    channels = rule.get("channels") or []
    results = [dispatch_channel(store, ch, rule, ctx_fields) for ch in channels]
    any_failed = any(r.get("status") == "failed" for r in results)
    store.complete_alert_fire(
        fire_id,
        status=FIRE_PARTIAL if any_failed else FIRE_FIRED,
        channels=results,
    )
    logger.info(
        "alert rule %s fired fp=%s channels=%s",
        rid,
        fp[:60],
        [r.get("status") for r in results],
    )
    return 1


def emit_alert_event_async(
    store: Store,
    *,
    event_fingerprint: str,
    fields: dict[str, Any],
    org_id: str = "",
) -> None:
    """비동기 발송 — 호출 경로(run 종료 등)를 블로킹하지 않는다(FR-003).

    daemon thread에서 emit_alert_event 실행. 실패는 내부에서 삼킨다."""

    def _run() -> None:
        try:
            emit_alert_event(
                store,
                event_fingerprint=event_fingerprint,
                fields=fields,
                org_id=org_id,
            )
        except Exception:
            logger.warning(
                "alert event eval failed fp=%s",
                str(event_fingerprint)[:60],
                exc_info=True,
            )

    try:
        threading.Thread(target=_run, daemon=True, name="jobmate-alert-eval").start()
    except Exception:
        logger.warning("alert eval thread spawn failed", exc_info=True)


# ---------------- 주기 집계 필드 평가 (spec 007 US4) ----------------


def _periodic_org_scope(store: Store, org_id: str) -> Any:
    """emit_alert_event와 동일한 org 스코프 재진입 — ctx가 다르면 감싼다.
    반환은 orgctx.org_scope 컨텍스트 매니저 또는 None."""
    oid = str(org_id or "")
    if oid and orgctx.current_org_id() != oid:
        return orgctx.org_scope(oid)
    return None


def evaluate_periodic_rules(
    store: Store,
    *,
    org_id: str = "",
    now: datetime | None = None,
    force: bool = False,
) -> int:
    """주기 집계 필드(error.rate_pct/session.stale_count/cost.delta_pct)
    규칙을 평가·발송한다 — 반환은 실제 발송된 규칙 수.

    호출 경로: emit_alert_event 끝에서 자동 호출(이벤트가 흐를 때만,
    _PERIODIC_MIN_INTERVAL 스로틀) + api/jobs.py::_alert_rule_sweep 끝에서
    규칙이 있는 org별로 무조건 호출 — 이벤트가 없는 조용한 시스템의
    주기 규칙도 평가된다(동일 스로틀 공유).

    멱등/쿨다운은 _claim_and_dispatch 경유 — fingerprint는
    `periodic:{rule_id}:{field}:{YYYYMMDDHH}`라 같은 시간 버킷 내
    재평가는 dedup되고, cooldown_minutes가 지나면 재발화한다."""
    oid = str(org_id or "")
    scope = _periodic_org_scope(store, oid)
    if scope is not None:
        with scope:
            return evaluate_periodic_rules(store, org_id=oid, now=now, force=force)
    rules = [
        r for r in store.list_alert_rules(enabled_only=True) if str(r.get("org_id") or "") == oid
    ]
    periodic = [r for r in rules if _condition_field(r) in PERIODIC_FIELDS]
    if not periodic:
        return 0
    key = (id(store), oid)
    mono = time.monotonic()
    last = _PERIODIC_LAST.get(key)
    if not force and last is not None and mono - last < _PERIODIC_MIN_INTERVAL:
        return 0
    now_dt = now if isinstance(now, datetime) else _now()
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=UTC)
    needed = {_condition_field(r) for r in periodic}
    try:
        values = _compute_periodic_values(store, oid, needed, now_dt)
    except Exception:
        logger.warning("periodic value compute failed org=%s", oid, exc_info=True)
        return 0
    _PERIODIC_LAST[key] = mono
    # 시간 버킷 fingerprint — 쿨다운=0 규칙도 시간 단위로만 재발화한다.
    window = now_dt.strftime("%Y%m%d%H")
    fired = 0
    for rule in periodic:
        rid = str(rule.get("id") or "")
        field = _condition_field(rule)
        if not rid or field not in values:
            continue
        fp = f"periodic:{rid}:{field}:{window}"
        pfields: dict[str, Any] = dict(values)
        pfields["_fingerprint"] = fp
        if not condition_matches(rule.get("condition") or {}, pfields):
            continue
        fired += _claim_and_dispatch(store, rule, pfields, fp, now_dt)
    return fired


def _compute_periodic_values(
    store: Store, org_id: str, needed: set[str], now: datetime
) -> dict[str, float]:
    """필요한 주기 필드만 계산 — (store, org) 단위 1회 호출당 1쿼리씩."""
    values: dict[str, float] = {}
    if "error.rate_pct" in needed:
        values["error.rate_pct"] = _error_rate_pct(store, org_id, now)
    if "session.stale_count" in needed:
        values["session.stale_count"] = float(_session_stale_count(store, now))
    if "cost.delta_pct" in needed:
        values["cost.delta_pct"] = _cost_delta_pct(store, org_id, now)
    return values


def _error_rate_pct(store: Store, org_id: str, now: datetime) -> float:
    """trailing 60분 run 실패율(%) — status='failed' / 전체 run.

    윈도우 내 run이 없으면 0.0 — gt 계열 조건이 오발화하지 않는다."""
    since = (now - timedelta(minutes=_ERROR_RATE_WINDOW_MIN)).isoformat()
    row = store._conn.execute(  # noqa: SLF001 - sweep과 동일한 내부 집계 조회
        "SELECT COUNT(*) AS total,"
        " SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed"
        " FROM runs WHERE created_at >= ? AND org_id=?",
        (since, org_id),
    ).fetchone()
    total = int(row["total"] or 0) if row else 0
    if total <= 0:
        return 0.0
    failed = int(row["failed"] or 0) if row else 0
    return round(failed * 100.0 / total, 2)


def _session_stale_count(store: Store, now: datetime | None = None) -> int:
    """health가 stale/long_idle인 agent_sessions 수 — lead_common.session_health
    와 동일 규칙을 SQL COUNT로 재현한다.

    이전 구현은 list_agent_sessions(limit=500, last_event_at DESC) 상위만
    스캔했는데, 정렬이 최신순이라 가장 오래된(stale) 세션이 limit 밖으로
    밀려 과소계상됐다. session_health 규칙:
    - status='ended' 제외
    - working/decision: 마지막 활동(last_event_at → updated_at 폴백) 이후
      _SESSION_STALE_SECONDS(300s) 초과 → stale
    - rest: _SESSION_LONG_IDLE_SECONDS(12h) 초과 → long_idle
    - 그 외 status/타임스탬프 없음 → active
    org 필터는 list_agent_sessions의 _org_filter와 동일(ctx 조직 + '' 공유 행).
    타임스탬프는 ISO 문자열 비교 — 파싱 불가한 비정형 값은 세션 행을
    만들지 않는 upsert 경로상 발생하지 않는다고 가정한다."""
    from ..lead_common import _SESSION_LONG_IDLE_SECONDS, _SESSION_STALE_SECONDS

    now_dt = now if isinstance(now, datetime) else _now()
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=UTC)
    stale_cutoff = (now_dt - timedelta(seconds=_SESSION_STALE_SECONDS)).isoformat()
    idle_cutoff = (now_dt - timedelta(seconds=_SESSION_LONG_IDLE_SECONDS)).isoformat()
    # COALESCE(NULLIF(...,'')) — session_health의
    # `_parse_ts(last_event_at) or _parse_ts(updated_at)` 폴백과 동일 의미.
    ts_expr = "COALESCE(NULLIF(last_event_at,''), NULLIF(updated_at,''))"
    oid = orgctx.current_org_id()
    org_cond = ""
    params: list[Any] = [stale_cutoff, idle_cutoff]
    if oid:
        org_cond = " AND (org_id=? OR org_id='')"
        params.append(oid)
    sql = (
        "SELECT COUNT(*) AS n FROM agent_sessions"  # noqa: S608 - org_cond/ts_expr는 고정 리터럴
        " WHERE status != 'ended'"
        " AND ("
        f"  (status IN ('working','decision') AND {ts_expr} < ?)"
        f"  OR (status='rest' AND {ts_expr} < ?)"
        " )" + org_cond
    )
    row = store._conn.execute(sql, params).fetchone()  # noqa: SLF001 - 주기 집계 내부 조회
    try:
        return int(row["n"] or 0) if row else 0
    except (TypeError, ValueError):
        return 0


def _cost_delta_pct(store: Store, org_id: str, now: datetime) -> float:
    """오늘(UTC) 비용 vs 직전 7일 일평균 증감률(%) — 양수=증가.

    cost_events.cost_microusd 합계를 occurred_at 날짜별로 묶어 계산.
    unpriced(cost_microusd NULL)는 합계에서 자연 제외된다.
    기준선(직전 7일 합계)이 0이면: 오늘 비용>0 → 100.0(기준선 없음의
    최대 증감 sentinel), 둘 다 0 → 0.0."""
    today = now.date()
    day0 = datetime(today.year, today.month, today.day, tzinfo=UTC)
    since = (day0 - timedelta(days=_COST_BASELINE_DAYS)).isoformat()
    rows = store._conn.execute(  # noqa: SLF001
        "SELECT substr(occurred_at,1,10) AS d, SUM(cost_microusd) AS m"
        " FROM cost_events WHERE occurred_at >= ? AND org_id=?"
        " AND cost_microusd IS NOT NULL GROUP BY d",
        (since, org_id),
    ).fetchall()
    daily: dict[str, float] = {}
    for r in rows:
        try:
            daily[str(r["d"])] = float(r["m"] or 0)
        except (TypeError, ValueError):
            continue
    today_key = today.isoformat()
    today_sum = daily.get(today_key, 0.0)
    prev_sum = 0.0
    for i in range(1, _COST_BASELINE_DAYS + 1):
        prev_sum += daily.get((today - timedelta(days=i)).isoformat(), 0.0)
    avg = prev_sum / _COST_BASELINE_DAYS
    if avg > 0:
        return round((today_sum - avg) / avg * 100.0, 2)
    return 100.0 if today_sum > 0 else 0.0


# ---------------- 이벤트 컨텍스트 빌더 ----------------

_TERMINAL_RUN_STATUSES = {
    "completed",
    "failed",
    "rejected",
    "cancelled",
    "budget_exhausted",
    "needs_human",
}


def run_event_fields(store: Store, run_id: str) -> tuple[dict[str, Any], str] | None:
    """run 행 → (fields, org_id). run이 없거나 조회 실패 시 None."""
    run = store.get_run(run_id)
    if run is None:
        return None
    from ..services.management_common import row_org_id

    org_id = row_org_id(store, "runs", run_id)
    duration_ms = 0
    try:
        if run.created_at and run.updated_at:
            duration_ms = max(0, int((run.updated_at - run.created_at).total_seconds() * 1000))
    except Exception:
        duration_ms = 0
    fields: dict[str, Any] = {
        "run.id": run.id,
        "run.status": run.status.value,
        "run.workflow": run.workflow_key,
        "run.project": run.project or "",
        "run.duration_ms": duration_ms,
        # spec 007 US4 — duration_ms의 alias. 값은 위에서 run
        # created_at→updated_at 차이로 이미 계산된다.
        "run.latency_ms": duration_ms,
        "run.cost_usd": float((run.metadata or {}).get("cost_usd") or 0.0),
        # spec 007 US4 — runs.error_class 컬럼(007 US3 스키마). 모델이
        # 아직 필드를 노출하지 않을 수 있어 원시 컬럼을 직접 읽는다.
        "error.error_class": _row_error_class(store, "runs", run.id),
    }
    return fields, org_id


def _row_error_class(store: Store, table: str, row_id: str) -> str:
    """runs/stage_runs.error_class 원시 컬럼 조회 — 컬럼이 없는 구 스키마나
    조회 실패 시 ''로 수렴한다(이벤트 평가는 비차단)."""
    try:
        row = store._conn.execute(  # noqa: SLF001 - 이벤트 필드용 내부 조회
            f"SELECT error_class FROM {table} WHERE id=?",  # noqa: S608 - table은 고정 리터럴
            (row_id,),
        ).fetchone()
        return str(row["error_class"] or "") if row else ""
    except Exception:
        return ""


def _error_class_of(obj: Any, store: Store, table: str, row_id: str) -> str:
    """error_class 방어적 읽기 — 병렬 작업으로 모델/행에 error_class가
    생기기 전후 모두 동작한다: dict.get → getattr → 원시 컬럼 순."""
    eclass = ""
    if isinstance(obj, dict):
        eclass = str(obj.get("error_class") or "")
    elif obj is not None:
        eclass = str(getattr(obj, "error_class", "") or "")
    if not eclass:
        eclass = _row_error_class(store, table, row_id)
    return eclass


def stage_event_fields(
    store: Store, run_id: str, stage_key: str, stage_status: str
) -> tuple[dict[str, Any], str] | None:
    """stage 종료 이벤트 — run.* 컨텍스트 + stage.* 필드."""
    base = run_event_fields(store, run_id)
    if base is None:
        return None
    fields, org_id = base
    fields["stage.key"] = stage_key
    fields["stage.status"] = stage_status
    return fields, org_id


def run_status_fingerprint(run_id: str, status: str, updated_iso: str) -> str:
    return f"run:{run_id}:{status}:{updated_iso}"


def emit_run_status_event(store: Store, run_id: str) -> int:
    """run 상태 변경 → alert 규칙 평가 (동기). 호출자는 비동기 래퍼 사용 권장."""
    built = run_event_fields(store, run_id)
    if built is None:
        return 0
    fields, org_id = built
    run = store.get_run(run_id)
    status = str(fields.get("run.status") or "")
    updated = run.updated_at.isoformat() if run is not None and run.updated_at else ""
    return emit_alert_event(
        store,
        event_fingerprint=run_status_fingerprint(run_id, status, updated),
        fields=fields,
        org_id=org_id,
    )


def emit_run_status_event_async(store: Store, run_id: str) -> None:
    """run 상태 평가를 daemon thread에서 — 지문은 스레드 안에서 계산해
    호출 시점과 최종 updated_at이 어긋나지 않게 한다."""

    def _run() -> None:
        try:
            emit_run_status_event(store, run_id)
        except Exception:
            logger.warning("alert run-status eval failed run=%s", run_id, exc_info=True)

    try:
        threading.Thread(target=_run, daemon=True, name="jobmate-alert-run").start()
    except Exception:
        logger.warning("alert run eval thread spawn failed", exc_info=True)


def emit_stage_failed_event(store: Store, run_id: str, stage_run_id: str) -> None:
    """stage_run 실패 → stage.status=failed 이벤트 평가 (비동기)."""

    def _run() -> None:
        try:
            sr = store.get_stage_run(stage_run_id)
            if sr is None:
                return
            built = stage_event_fields(store, run_id, sr.stage_key, "failed")
            if built is None:
                return
            fields, org_id = built
            # spec 007 US4 — stage 실패의 error_class. StageRun 모델에
            # 필드가 아직 없을 수 있어 attr/dict/원시 컬럼 순으로 읽는다.
            fields["error.error_class"] = _error_class_of(sr, store, "stage_runs", stage_run_id)
            emit_alert_event(
                store,
                event_fingerprint=f"stage:{stage_run_id}:failed",
                fields=fields,
                org_id=org_id,
            )
        except Exception:
            logger.warning("alert stage eval failed sr=%s", stage_run_id, exc_info=True)

    try:
        threading.Thread(target=_run, daemon=True, name="jobmate-alert-stage").start()
    except Exception:
        logger.warning("alert stage thread spawn failed", exc_info=True)


def emit_budget_threshold_event(
    store: Store,
    *,
    policy: dict[str, Any],
    threshold: str,
    used_pct: float,
    month: str,
) -> None:
    """FR-011 — budget 임계 도달을 alert 규칙 이벤트로 발행 (비동기).

    fingerprint는 인박스 dedupe_key와 동일 기준 — 같은 (policy, month,
    threshold) 재평가는 규칙 dedup으로 1회만 발송된다."""
    pid = str(policy.get("id") or "")
    fp = f"budget:{pid}:{month}:{threshold}"

    def _run() -> None:
        try:
            # 정책 행의 org로 스코프 — evaluate_admission 호출 ctx와 무관하게
            # 규칙 평가가 올바른 조직 규칙만 보게 한다.
            oid = str(policy.get("org_id") or "")
            fields = {
                "budget.used_pct": float(used_pct or 0.0),
                "budget.scope": f"{policy.get('scope_type')}:{policy.get('scope_id') or ''}",
                "budget.threshold": threshold,
                "budget.policy_id": pid,
            }
            emit_alert_event(store, event_fingerprint=fp, fields=fields, org_id=oid)
        except Exception:
            logger.warning("alert budget eval failed policy=%s", pid, exc_info=True)

    try:
        threading.Thread(target=_run, daemon=True, name="jobmate-alert-budget").start()
    except Exception:
        logger.warning("alert budget thread spawn failed", exc_info=True)
