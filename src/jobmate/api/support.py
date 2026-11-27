"""app.py에서 분리한 모듈 레벨 헬퍼 — 라우트/상태 없는 순수 지원 함수.

도메인별 헬퍼는 아래 모듈로 분리됐다:
- session_stats.py: agent_sessions 읽기 모델(health/비용 추정)
- sensitive_fields.py: audit 마스킹 + connector config 비밀값 암호화
- connectors.py: connector 라이브 fetch + 캐시
- triggers.py: 세션 상태 전이 트리거 평가/발사

이 파일에는 automation loop/cron tick이 공유하는 런타임 지원(크론 틱 상태,
run 세마포어, stale reaper, retention sweep, 어댑터 생성, 워크플로 레지스트리)만
남긴다. tests/unit/test_dashboard_features.py가 support 경유로 접근하는
_CONNECTOR_CACHE/_evaluate_triggers는 호환 re-export를 유지한다.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..adapters import LoggingNotifier, WebPushNotifier
from ..config import Settings
from ..models import WorkflowSpec
from ..radar import GitHubAPI
from ..store.db import Store
from ..workflows import WORKFLOWS

# 테스트/레거시 import 호환 — 실제 구현은 도메인 모듈에 있다.
from .connectors import _CONNECTOR_CACHE  # noqa: F401
from .triggers import _evaluate_triggers  # noqa: F401

logger = logging.getLogger("jobmate.api")
worker_logger = logging.getLogger("jobmate.worker")

# 마지막 자동화 틱 시각 — /health/detail의 cron.last_tick_at.
# automation_state('cron_tick')가 인스턴스 간 공유 1순위, 인메모리는 폴백.
_LAST_CRON_TICK_AT: str | None = None


def _mark_cron_tick(store: Store | None = None) -> None:
    global _LAST_CRON_TICK_AT
    now = datetime.now(UTC).isoformat()
    _LAST_CRON_TICK_AT = now
    if store is not None:
        try:
            store.set_state("cron_tick", {"last_tick_at": now})
        except Exception:
            logger.debug("cron tick state persist failed", exc_info=True)


def _last_cron_tick_at(store: Store | None = None) -> str | None:
    """마지막 틱 시각 — DB(automation_state) 우선, 인메모리 폴백."""
    if store is not None:
        try:
            v = store.get_state("cron_tick", {}).get("last_tick_at")
            if v:
                return str(v)
        except Exception:
            logger.debug("cron tick state read failed", exc_info=True)
    return _LAST_CRON_TICK_AT


# ---- 동시 run 실행 세마포어 ----
# Gmail 50건 동시 수신/악의적 호출 시 LLM 폭주 방지. JOBMATE_MAX_CONCURRENT_RUNS
# 로 조정(기본 4). 백그라운드 job은 슬롯을 기다리고, API resume은 타임아웃 시 503.
_RUN_SEMAPHORE: threading.BoundedSemaphore | None = None
_RUN_SEM_LOCK = threading.Lock()
_RUN_SEM_TIMEOUT_S = 600.0


def _run_slots() -> threading.BoundedSemaphore:
    """프로세스 전역 run 슬롯 세마포어 — env는 최초 생성 시 1회 읽는다."""
    global _RUN_SEMAPHORE
    if _RUN_SEMAPHORE is None:
        with _RUN_SEM_LOCK:
            if _RUN_SEMAPHORE is None:
                try:
                    n = int(os.environ.get("JOBMATE_MAX_CONCURRENT_RUNS", "4"))
                except ValueError:
                    n = 4
                _RUN_SEMAPHORE = threading.BoundedSemaphore(max(1, n))
    return _RUN_SEMAPHORE


def _acquire_run_slot(run_id: str = "") -> bool:
    """run 실행 슬롯 대기 — 타임아웃(기본 600s) 시 False.

    무한 대기는 호출 스레드 고갈을 유발하므로 타임아웃을 두고, 실패 시
    호출자가 run을 failed로 마감하거나 503을 반환하게 한다."""
    if _run_slots().acquire(timeout=_RUN_SEM_TIMEOUT_S):
        return True
    worker_logger.warning("run slot acquire timeout run=%s", run_id)
    return False


# ---- stale run reaper ----
_STALE_RUN_MINUTES = 30
_REAPER_INTERVAL_S = 60.0  # cron tick이 분 단위로 와도 루프 중복 호출 방지
_REAPER_STATE: dict[str, Any] = {"last_at": 0.0, "last_reaped": 0, "last_sweep_at": ""}


def _reap_stale_runs(
    store: Store,
    settings: Settings | None = None,
    llm: Any | None = None,
    adapters: dict[str, Any] | None = None,
    schedule: Any | None = None,
) -> int:
    """30분 이상 갱신이 없는 queued/running run을 failed로 마감.

    재시작/강제종료로 고착된 run 복구용 — waiting_approval은 사람 승인
    대기라 제외. 인스턴스당 60초에 1회로 스로틀한다.
    settings/llm/adapters가 주어지면 마감된 run의 wakeup finalize
    (deferred promote 포함)도 수행한다 — 없으면 deferred wakeup이 굶는다.
    schedule은 _promote_deferred_runs로 전달 — cron tick HTTP 핸들러는
    BackgroundTasks 기반 스케줄러를 넘겨 promote 실행이 요청 처리를
    블로킹하지 않게 한다."""
    now = time.time()
    if now - float(_REAPER_STATE.get("last_at") or 0.0) < _REAPER_INTERVAL_S:
        return 0
    _REAPER_STATE["last_at"] = now
    cutoff = (datetime.now(UTC) - timedelta(minutes=_STALE_RUN_MINUTES)).isoformat()
    reaped_ids: list[str] = []
    try:
        n = store.fail_stale_runs(cutoff, error="stale_timeout", reaped_out=reaped_ids)
    except Exception:
        worker_logger.exception("stale run reaper failed")
        return 0
    _REAPER_STATE["last_reaped"] = n
    _REAPER_STATE["last_sweep_at"] = datetime.now(UTC).isoformat()
    if n:
        worker_logger.info("stale run reaper closed %d runs", n)
    if reaped_ids and settings is not None:
        # jobs.py는 support를 import하므로 순환 방지로 지연 import
        from .jobs import _promote_deferred_runs

        for rid in reaped_ids:
            try:
                _promote_deferred_runs(store, settings, llm, adapters or {}, rid, schedule=schedule)
            except Exception:
                worker_logger.warning(
                    "wakeup finalize after stale reap failed run=%s", rid, exc_info=True
                )
    # 관리 task 투영 — reaped run이 failed가 됐으므로 연결 task는
    # in_progress에 영구 고착되지 않고 blocked로 내려간다 (1차 E항).
    if reaped_ids:
        try:
            from ..services import agent_management as _mgmt

            for rid in reaped_ids:
                _mgmt.project_run_status(store, rid)
        except Exception:
            worker_logger.warning("management projection after reap failed", exc_info=True)
    return n


# ---- retention sweep ----
_RETENTION_STATE_KEY = "retention_state"
_RETENTION_INTERVAL_S = 20 * 3600  # 하루 1회


def _retention_sweep(store: Store) -> dict[str, int] | None:
    """보존 기간 경과 데이터 삭제 — automation_state에 마지막 실행을 기록해
    인스턴스/서버리스 간에도 하루 1회만 돌도록 한다. 20시간 내 재호출 스킵."""
    try:
        state = store.get_state(_RETENTION_STATE_KEY, {})
        last = str(state.get("last_sweep_at") or "")
        if last:
            prev = datetime.fromisoformat(last)
            if prev.tzinfo is None:
                prev = prev.replace(tzinfo=UTC)
            if (datetime.now(UTC) - prev).total_seconds() < _RETENTION_INTERVAL_S:
                return None
    except Exception:
        logger.debug("retention state read failed — sweep will run", exc_info=True)
    try:
        counts = store.retention_sweep()
    except Exception:
        worker_logger.exception("retention sweep failed")
        return None
    try:
        store.set_state(
            _RETENTION_STATE_KEY,
            {"last_sweep_at": datetime.now(UTC).isoformat(), "last_counts": counts},
        )
    except Exception:
        worker_logger.debug("retention state persist failed", exc_info=True)
    if any(counts.values()):
        worker_logger.info("retention sweep deleted: %s", counts)
    return counts


def _token_path_for(kind: str, settings: Settings) -> str | None:
    """Google 토큰 파일 경로 해석.

    서버리스(Vercel)에는 토큰 파일이 없으므로 JOBMATE_{KIND}_TOKEN_JSON env의
    토큰 JSON을 임시 파일(0600)로 materialize한다. 로컬은 설정된 경로가
    실제 존재할 때만 사용 — 경로만 있고 파일이 없으면 어댑터 생성을 건너뛴다.
    """
    import tempfile

    env_json = os.environ.get(f"JOBMATE_{kind.upper()}_TOKEN_JSON", "")
    if env_json:
        try:
            json.loads(env_json)
        except ValueError:
            logger.warning("%s token json env is invalid JSON — adapter disabled", kind)
        else:
            fd, path = tempfile.mkstemp(prefix=f"jobmate-{kind}-token-", suffix=".json")
            with os.fdopen(fd, "w") as f:
                f.write(env_json)
            os.chmod(path, 0o600)
            logger.info("%s token materialized from env to temp file", kind)
            return path
    configured = getattr(settings, f"{kind}_token_path", None)
    if configured:
        p = Path(configured).expanduser()
        if p.is_file():
            return str(p)
        logger.warning("%s token path configured but file missing: %s", kind, p)
    return None


def _default_adapters(settings: Settings, store: Store) -> dict[str, Any]:
    from ..adapters.google import GoogleCalendar, GoogleGmail

    adapters: dict[str, Any] = {
        "notifier": LoggingNotifier(),
        "github": GitHubAPI(token=os.environ.get(settings.github_token_env, "")),
        "webpush": WebPushNotifier(
            store,
            private_key=os.environ.get("VAPID_PRIVATE_KEY", ""),
            subject=os.environ.get("VAPID_SUBJECT", "mailto:admin@localhost"),
        ),
    }
    # 실제 Google 연동 — 토큰 파일/env JSON이 확인된 경우에만 주입
    gmail_path = _token_path_for("gmail", settings)
    if gmail_path:
        adapters["gmail"] = GoogleGmail(gmail_path)
    cal_path = _token_path_for("calendar", settings)
    if cal_path:
        adapters["calendar"] = GoogleCalendar(cal_path)
    return adapters


# ---------- custom workflow registry ----------
# 내장 WORKFLOWS는 모듈 상수 — custom_workflows 테이블 내용을 여기에 동기화해
# POST /runs·플레이북·스케줄·트리거 등 모든 해석 경로가 커스텀 spec을 쓰게 한다.
_BUILTIN_WF_KEYS = set(WORKFLOWS)
_CUSTOM_WF_KEYS: set[str] = set()


def _sync_workflow_registry(store: Store) -> int:
    """custom_workflows 테이블 → WORKFLOWS dict 동기화 (앱 시작/등록·삭제 후).

    이전에 주입한 커스텀 키는 제거 후 재등록 — 앱 재시작 시 테이블에서 복원되므로
    별도 영속 레지스트리 없이도 해석이 유지된다. 내장 키와 충돌하는 spec은 스킵."""
    global _CUSTOM_WF_KEYS
    for key in _CUSTOM_WF_KEYS:
        WORKFLOWS.pop(key, None)
    _CUSTOM_WF_KEYS = set()
    for row in store.list_custom_workflows():
        name = str(row.get("name") or "")
        try:
            spec = WorkflowSpec.model_validate(row.get("spec"))
        except Exception as e:
            logger.warning("custom workflow %s spec invalid: %s", name, str(e)[:120])
            continue
        if spec.key in _BUILTIN_WF_KEYS:
            logger.warning("custom workflow %s collides with builtin — skipped", name)
            continue
        WORKFLOWS[spec.key] = spec
        _CUSTOM_WF_KEYS.add(spec.key)
    return len(_CUSTOM_WF_KEYS)


# 외부 부수효과(실제 캘린더/알림 쓰기, 외부 명령 실행)가 있는 function stage —
# 커스텀 워크플로가 이 이름을 쓰면 member 실행을 막고 lead 게이트를 적용한다.
_SIDE_EFFECT_FUNCTIONS = frozenset(
    {"gmail_register", "gmail_notify", "radar_publish", "auto_checks"}
)

# 내장 워크플로 판정용 — auto_checks는 dev_review/code_pipelines의 핵심
# 단계로 member 실행이 허용되어야 하므로 제외한다. 외부 쓰기·알림만 해당.
_BUILTIN_SIDE_EFFECT_FUNCTIONS = frozenset({"gmail_register", "gmail_notify", "radar_publish"})


def _workflow_needs_lead(key: str) -> bool:
    """POST /runs 실행에 lead 이상이 필요한 워크플로인지 판정.

    - gmail_calendar: 실제 Google Calendar에 일정을 쓰는 유일한 내장 워크플로
      (human 승인 stage 없음) → lead 전용.
    - 사이드이펙트 함수 stage가 있는 워크플로(내장·커스텀 모두): lead 전용.
      skill_radar의 radar_publish처럼 외부 쓰기/알림을 발생시키는 함수를
      포함하면 member의 POST /runs 직접 실행을 막는다.
    - 커스텀 워크플로: 레지스트리에 없는 function 이름이면 lead 전용.
      레지스트리 import 실패 시 보수적으로 lead 요구.
    - 그 외 내장(dev_review* 등)과 읽기성 커스텀은 member 허용."""
    if key == "gmail_calendar":
        return True
    spec = WORKFLOWS.get(key)
    if spec is None:
        return True
    is_custom = key in _CUSTOM_WF_KEYS
    _fn_registry: dict[str, Any] | None = None
    try:
        from ..engine.functions import FUNCTIONS

        _fn_registry = FUNCTIONS
    except Exception:
        _fn_registry = None
    for st in getattr(spec, "stages", []) or []:
        if getattr(st, "kind", None) != "function":
            continue
        fn = str(getattr(st, "function", "") or "")
        side_fx = _SIDE_EFFECT_FUNCTIONS if is_custom else _BUILTIN_SIDE_EFFECT_FUNCTIONS
        if fn in side_fx:
            return True
        if is_custom and (_fn_registry is None or fn not in _fn_registry):
            # 알 수 없는 function 이름은 실행 시 실패하지만, 레지스트리를
            # 확인할 수 없는 환경에서는 보수적으로 lead를 요구한다.
            return True
    return False
