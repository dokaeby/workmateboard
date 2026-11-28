"""Health service — spec 004 US2 (FR-012~FR-016, FR-024).

컴포넌트별 헬스 체크(api/db/llm/gmail/messenger/disk/backup/webhook/cron)를
개별 타임아웃(settings.health_check_timeout)으로 병렬 실행하고,
ok/degraded/down 판정 + 전이 시 알림(웹훅/인박스/메신저)을 담당한다.

판정 규칙(FR-016):
- api 또는 db가 down → 전체 "down"
- 그 외 컴포넌트 down 1개 이상 또는 degraded 존재 → 전체 "degraded"
- 모두 ok → "ok"

알림은 ok→degraded/down 전이 시 1회만 — automation_state의
health_fp 지문 맵으로 중복 발행을 막는다(approval_wait_fp 패턴).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import threading
import time
import weakref
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..config import Settings
from ..store.db import Store

logger = logging.getLogger("jobmate.health")

_STATE_FP = "health_fp"
_STATE_REPORT = "health_last_report"
_CORE_COMPONENTS = frozenset({"api", "db"})

# 공개 헬스 TTL 캐시 — 비인증 엔드포인트가 요청마다 전체 체크
# (스레드풀 + DB 스캔)를 돌리는 DoS 증폭을 막는다. 보고서는 플랫폼
# 전역(조직 무관)이라 키는 store 인스턴스별 1개면 충분하다.
_PUBLIC_TTL_S = 15.0
_public_lock = threading.Lock()
# id(store) -> (weakref(store), monotonic_ts, {"status","components"})
_public_cache: dict[int, tuple[Any, float, dict[str, Any]]] = {}

_ABS_PATH_RE = re.compile(r"(?<![\w.])/(?:[^\s,'\")\]]+/)+[^\s,'\")\]]*")
_PATH_KV_RE = re.compile(r"\s*\bpath=\S+")


def _webhook_safe_detail(detail: str) -> str:
    """웹훅 페이로드용 detail 정제 — 파일시스템 경로/path= 값을 제거한다.

    health.degraded는 ctx=''로 발행돼 전 조직 구독에 팬아웃되므로
    내부 파일시스템 정보가 외부로 새지 않게 한다(FR-007 데이터 최소화).
    컴포넌트명·상태·수치(latency/age 등)는 유지한다."""
    s = _PATH_KV_RE.sub("", detail or "")
    s = _ABS_PATH_RE.sub("[path]", s)
    return s[:200]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _parse_ts(s: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(s)) if s else None
    except (ValueError, TypeError):
        return None


def _emit(store: Store, event_type: str, event_id: str, data: dict[str, Any]) -> None:
    """웹훅 이벤트 — services.webhooks 병렬 개발 대비 lazy import + broad except."""
    try:
        from .webhooks import emit_event

        emit_event(store, event_type, event_id, data)
    except Exception:
        logger.debug("webhook emit failed type=%s", event_type, exc_info=True)


# ---------- 개별 체크 — (status, detail) 반환, 예외는 호출부가 down으로 격리 ----------


def _check_api() -> tuple[str, str]:
    return "ok", "up"


def _check_db(store: Store) -> tuple[str, str]:
    started = time.monotonic()
    with store.tx() as c:
        c.execute("SELECT 1")
    ms = (time.monotonic() - started) * 1000
    return "ok", f"select1 {ms:.1f}ms"


def _check_llm(store: Store, settings: Settings) -> tuple[str, str]:
    """LLM 설정 존재(키 env) + 최근 llm_usage 기록 — 네트워크 reachability는
    하지 않는다(헬스 경로가 외부 장애에 묶이지 않도록)."""
    key_envs = {settings.llm_api_key_env, settings.codex_api_key_env} - {""}
    configured = any(os.environ.get(e, "") for e in key_envs)
    last_usage = ""
    try:
        rows = store.llm_usage_rows((datetime.now(UTC) - timedelta(days=7)).isoformat())
        if rows:
            last_usage = str(rows[-1].get("created_at") or "")
    except Exception:
        logger.debug("llm_usage recency check failed", exc_info=True)
    if configured:
        return "ok", f"configured last_usage={last_usage or 'none'}"
    # 키 미설정 — 실행은 되지만 모든 LLM 호출이 실패하므로 degraded
    return "degraded", "no api key configured"


def _check_gmail(settings: Settings) -> tuple[str, str]:
    """Gmail 어댑터 설정 여부 — 토큰 JSON env 또는 토큰 파일 존재.
    원문/토큰 값은 절대 읽지 않고 존재 여부만 확인한다."""
    if os.environ.get("JOBMATE_GMAIL_TOKEN_JSON", ""):
        return "ok", "token_json env"
    p = getattr(settings, "gmail_token_path", None)
    if p and Path(str(p)).expanduser().is_file():
        return "ok", "token file"
    # 선택적 연동 — 미설정은 장애가 아니므로 ok + detail로만 표현
    return "ok", "not configured"


def _check_messenger() -> tuple[str, str]:
    """메신저 알림 채널 설정 여부 — env 존재만 확인(값 로깅 금지)."""
    if os.environ.get("JOBMATE_SLACK_WEBHOOK_URL", ""):
        return "ok", "slack"
    if os.environ.get("JOBMATE_TELEGRAM_BOT_TOKEN", "") and os.environ.get(
        "JOBMATE_TELEGRAM_CHAT_ID", ""
    ):
        return "ok", "telegram"
    return "ok", "not configured"


def _check_disk(settings: Settings) -> tuple[str, str]:
    # DB/백업이 있는 파일시스템 기준 — backup_dir 우선, 없으면 db_path 부모, 최종 cwd
    candidates = [
        Path(settings.backup_dir),
        Path(settings.db_path).expanduser().parent,
        Path.cwd(),
    ]
    target = next((p for p in candidates if p.exists()), Path.cwd())
    usage = shutil.disk_usage(target)
    free_pct = usage.free / usage.total * 100 if usage.total else 0.0
    detail = f"free={free_pct:.1f}% path={target.name}"
    if free_pct < float(getattr(settings, "disk_min_free_pct", 15.0)):
        return "degraded", detail
    return "ok", detail


def _check_backup(store: Store, settings: Settings) -> tuple[str, str]:
    """마지막 성공 백업 경과 — FR-024 staleness 연결.
    백업 비활성이면 ok/disabled. 성공 이력 없으면 down, staleness 초과 degraded."""
    interval = float(getattr(settings, "backup_interval_hours", 0.0) or 0.0)
    if interval <= 0:
        return "ok", "disabled"
    row = store.latest_backup_success()
    if row is None:
        return "down", "no successful backup"
    finished = _parse_ts(row.get("finished_at")) or _parse_ts(row.get("started_at"))
    age_h = (datetime.now(UTC) - finished).total_seconds() / 3600 if finished else 1e9
    stale_h = float(getattr(settings, "backup_stale_hours", 30.0))
    detail = f"age_h={age_h:.1f} stale_h={stale_h} verify={row.get('verify_status')}"
    if row.get("verify_status") == "failed":
        return "degraded", f"verify_failed {detail}"
    if age_h > stale_h:
        return "degraded", f"stale {detail}"
    return "ok", detail


def _check_webhook(store: Store) -> tuple[str, str]:
    """webhook_deliveries dead/pending 깊이 — 테이블 부재 시 ok/n-a."""
    try:
        row = store._conn.execute(  # noqa: SLF001 - 헬스 체크는 store 내부 조회 허용
            "SELECT COUNT(*) AS dead,"
            " SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending"
            " FROM webhook_deliveries WHERE status IN ('dead','pending')"
        ).fetchone()
        dead = int(row["dead"] or 0)
        pending = int(row["pending"] or 0)
    except Exception:
        return "ok", "deliveries n/a"
    if dead > 0:
        return "degraded", f"dead={dead} pending={pending}"
    return "ok", f"dead=0 pending={pending}"


def _check_cron(store: Store) -> tuple[str, str]:
    """마지막 automation tick 경과 — support._last_cron_tick_at이 DB 우선,
    인메모리 폴백을 처리한다(lazy import로 순환 방지)."""
    try:
        from ..api.support import _last_cron_tick_at

        last = _last_cron_tick_at(store)
    except Exception:
        last = None
    ts = _parse_ts(last)
    if ts is None:
        return "degraded", "no tick recorded"
    age_s = (datetime.now(UTC) - ts).total_seconds()
    return "ok", f"last_tick_age_s={age_s:.0f}"


_CHECKS: dict[str, Any] = {
    "api": lambda store, settings: _check_api(),
    "db": _check_db,
    "llm": _check_llm,
    "gmail": lambda store, settings: _check_gmail(settings),
    "messenger": lambda store, settings: _check_messenger(),
    "disk": lambda store, settings: _check_disk(settings),
    "backup": _check_backup,
    "webhook": lambda store, settings: _check_webhook(store),
    "cron": lambda store, settings: _check_cron(store),
}


def run_health_checks(store: Store, settings: Settings) -> dict[str, Any]:
    """전체 컴포넌트 체크 — 병렬 실행 + 체크별 타임아웃(FR-014).

    각 체크는 독립 try/except로 'down' 격리되고, 타임아웃 초과도 'down'.
    엔드포인트 자체는 항상 200으로 응답할 수 있는 형태를 반환한다."""
    timeout = float(getattr(settings, "health_check_timeout", 2.0) or 2.0)
    checked_at = _now_iso()
    components: dict[str, dict[str, Any]] = {}

    def _run(name: str, fn: Any) -> tuple[str, str, str | None, float]:
        t0 = time.monotonic()
        try:
            status, detail = fn(store, settings)
            return name, status, detail, (time.monotonic() - t0) * 1000
        except Exception as e:
            return name, "down", f"{type(e).__name__}", (time.monotonic() - t0) * 1000

    # context manager 미사용 — `with` 탈출 시 shutdown(wait=True)이
    # 타임아웃을 넘긴 hung 체크의 종료를 무한 대기한다. 결과 수집 후
    # wait=False + cancel_futures로 미시작 future만 취소하고 반환한다.
    ex = ThreadPoolExecutor(max_workers=len(_CHECKS), thread_name_prefix="health")
    try:
        futures = {name: ex.submit(_run, name, fn) for name, fn in _CHECKS.items()}
        for name, fut in futures.items():
            try:
                _, status, detail, latency_ms = fut.result(timeout=timeout)
            except Exception as e:
                # TimeoutError 포함 — 체크 격리 원칙상 down 처리
                status, detail, latency_ms = "down", f"{type(e).__name__}", timeout * 1000
            components[name] = {
                "status": status,
                "latency_ms": round(latency_ms, 1),
                "detail": detail,
                "checked_at": checked_at,
            }
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    statuses = [c["status"] for c in components.values()]
    if "down" in [components[k]["status"] for k in _CORE_COMPONENTS]:
        overall = "down"
    elif "down" in statuses or "degraded" in statuses:
        overall = "degraded"
    else:
        overall = "ok"
    return {"status": overall, "checked_at": checked_at, "components": components}


def run_health_cycle(store: Store, settings: Settings) -> dict[str, Any]:
    """헬스 체크 + 전이 알림 — automation-cycle 진입점(FR-015).

    컴포넌트 상태가 ok→degraded/down으로 전이하면 health.degraded 웹훅 +
    인박스 항목(dedupe) + messenger.notify를 발행한다. 동일 상태 지속 중
    재알림 없음 — health_fp 지문 맵으로 판정."""
    report = run_health_checks(store, settings)
    comps = report.get("components") or {}
    try:
        prev = store.get_state(_STATE_FP, {})
    except Exception:
        prev = {}
    if not isinstance(prev, dict):
        prev = {}
    fp = {name: str(c.get("status")) for name, c in comps.items()}
    transitions: list[tuple[str, str]] = []
    for name, st in fp.items():
        p = prev.get(name, "ok")
        if st in ("degraded", "down") and p != st:
            transitions.append((name, st))
    if transitions or fp != prev:
        try:
            store.set_state(_STATE_FP, fp)
            store.set_state(
                _STATE_REPORT,
                {
                    "status": report["status"],
                    "checked_at": report["checked_at"],
                    "components": {k: v.get("status") for k, v in comps.items()},
                },
            )
        except Exception:
            logger.debug("health state persist failed", exc_info=True)
    for name, st in transitions:
        detail = str((comps.get(name) or {}).get("detail") or "")
        title = f"헬스 저하: {name} {st}"
        logger.warning("health transition component=%s status=%s detail=%s", name, st, detail[:120])
        # 인박스 — 컴포넌트+전이 상태별 dedupe, 재발생 시 미읽음 리셋
        try:
            store.upsert_inbox_item(
                "health",
                title,
                body=detail[:300],
                link="/?view=ops",
                dedupe_key=f"health:{name}:{st}",
                reset_read=True,
            )
        except Exception as e:
            logger.warning("health inbox item failed component=%s: %s", name, str(e)[:200])
        _emit(
            store,
            "health.degraded",
            f"health:{name}:{report['checked_at']}",
            {
                "component": name,
                "status": st,
                "detail": _webhook_safe_detail(detail),
                "overall": report["status"],
            },
        )
        try:
            from .. import messenger

            messenger.notify(f"{title} — {detail[:120]}")
        except Exception:
            logger.debug("health messenger notify failed", exc_info=True)
    return report


def _public_summary(store: Store, settings: Settings) -> dict[str, Any]:
    """공개 요약(status + components{name:status}) — TTL 캐시 + 스탬피드 가드.

    캐시 미스 시 lock 안에서 run_health_checks를 실행해 동시 요청이
    체크를 중복 실행하지 못하게 한다(단일 비행). 키는 id(store) —
    보고서는 플랫폼 전역이라 조직별 분기가 없고, 다른 Store 인스턴스의
    캐시가 새지 않도록 weakref로 생존을 검증한다."""
    key = id(store)
    now = time.monotonic()
    with _public_lock:
        ent = _public_cache.get(key)
        if ent is not None and ent[0]() is store and now - ent[1] < _PUBLIC_TTL_S:
            return ent[2]
        report = run_health_checks(store, settings)
        summary = {
            "status": report["status"],
            "components": {
                name: comp.get("status", "unknown")
                for name, comp in (report.get("components") or {}).items()
            },
        }
        _public_cache[key] = (weakref.ref(store), now, summary)
        # 죽은 store 항목 정리 — 장기 실행 프로세스의 메모리 누수 방지
        for k in [k for k, v in _public_cache.items() if v[0]() is None]:
            _public_cache.pop(k, None)
        return summary


def public_health(
    store: Store, settings: Settings, started_at: float | None = None
) -> dict[str, Any]:
    """FR-012 공개 상태 — 화이트리스트 요약만 반환.

    포함: status/version/uptime_s/components{name:status}.
    DB 경로·호스트·지연시간·detail·건수 등 내부 정보는 절대 포함하지 않는다.
    체크 결과는 _PUBLIC_TTL_S(15s) TTL로 캐시해 비인증 요청의
    스레드풀/DB 부하 증폭을 차단한다(uptime_s만 요청마다 갱신)."""
    summary = _public_summary(store, settings)
    started = started_at if started_at is not None else time.time()
    uptime = max(0.0, time.time() - started) if started else 0.0
    return {
        "status": summary["status"],
        "version": "0.1.0",
        "uptime_s": round(uptime, 1),
        "components": dict(summary["components"]),
    }
