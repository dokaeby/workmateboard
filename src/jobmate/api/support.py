"""app.py에서 분리한 모듈 레벨 헬퍼 — 라우트/상태 없는 순수 지원 함수."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .. import messenger, webutil
from ..adapters import LoggingNotifier, WebPushNotifier
from ..config import Settings
from ..engine.trace import job_traceable
from ..models import WorkflowSpec
from ..radar import GitHubAPI
from ..store.db import Store
from ..workflows import WORKFLOWS

logger = logging.getLogger("jobmate.api")
worker_logger = logging.getLogger("jobmate.worker")


def _endpoint_fp(endpoint: str) -> str:
    """push endpoint 비식별 지문 — 원문 대신 SHA256 앞 12자."""
    import hashlib

    return hashlib.sha256(endpoint.encode()).hexdigest()[:12]


# 세션 토큰 누적치 → USD 추정치 (1M 토큰당). tool별 오버라이드, 없으면 default.
_SESSION_TOKEN_PRICES: dict[str, tuple[float, float]] = {
    "default": (3.0, 15.0),
    "claude": (3.0, 15.0),
    "codex": (1.5, 6.0),
    "gemini": (1.0, 4.0),
}

_SESSION_STALE_SECONDS = 300.0
_SESSION_LONG_IDLE_SECONDS = 43200.0


def _parse_session_ts(value: Any) -> datetime | None:
    """ISO 타임스탬프 파싱 — 실패/naive는 UTC 간주."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _session_health(row: dict[str, Any]) -> str:
    """세션 상태 건강도 — ended/stale/long_idle/active (Python 계산, SQL 아님)."""
    status = str(row.get("status") or "")
    if status == "ended":
        return "ended"
    ts = _parse_session_ts(row.get("last_event_at")) or _parse_session_ts(row.get("updated_at"))
    age = (datetime.now(UTC) - ts).total_seconds() if ts is not None else 0.0
    if status in ("working", "decision"):
        return "stale" if age > _SESSION_STALE_SECONDS else "active"
    if status == "rest":
        return "long_idle" if age > _SESSION_LONG_IDLE_SECONDS else "active"
    return "active"


def _session_est_cost_usd(row: dict[str, Any]) -> float | None:
    """tokens_in/out 누적치 → USD 추정. 둘 다 없으면 None."""
    tin = row.get("tokens_in")
    tout = row.get("tokens_out")
    if tin is None and tout is None:
        return None
    price_in, price_out = _SESSION_TOKEN_PRICES.get(
        str(row.get("tool") or ""), _SESSION_TOKEN_PRICES["default"]
    )
    return round(((tin or 0) * price_in + (tout or 0) * price_out) / 1_000_000, 6)


def _session_view(row: dict[str, Any]) -> dict[str, Any]:
    return {**row, "health": _session_health(row), "est_cost_usd": _session_est_cost_usd(row)}


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


# ---------- audit detail 비밀/내부경로 마스킹 ----------

# API 응답에서는 _project_out/_alias_project_out이 비-명시 lead에게 숨기는
# 프로젝트 필드 — audit detail에도 원문을 남기지 않는다(/api/v1/audit은
# admin=머신 키까지 읽을 수 있어 응답 필터를 우회한다).
_AUDIT_REDACTED_PROJECT_FIELDS = frozenset({"workspace_path"})


def sanitize_project_audit_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """project create/patch audit detail용 — 민감 필드 값을 '<redacted>'로
    치환한 사본을 반환한다(키 존재 여부는 유지해 변경 필드 목록은 남긴다)."""
    return {
        k: ("<redacted>" if k in _AUDIT_REDACTED_PROJECT_FIELDS else v) for k, v in fields.items()
    }


# ---------- connector config 비밀값 암호화 ----------
_SECRET_FIELD_RE = re.compile(
    r"(?i)(token|secret|password|api[_-]?key|private[_-]?key|credential|authorization)"
)


def _config_has_secret(config: dict[str, Any]) -> bool:
    """config에 비밀로 취급할 비어있지 않은 문자열 필드가 있는지."""

    def walk(v: Any, key_hint: str = "") -> bool:
        if isinstance(v, dict):
            return any(walk(x, str(k)) for k, x in v.items())
        if isinstance(v, list):
            return any(walk(x, key_hint) for x in v)
        return isinstance(v, str) and bool(v) and bool(_SECRET_FIELD_RE.search(key_hint))

    return walk(config)


def _encrypt_config_secrets(config: dict[str, Any]) -> dict[str, Any]:
    """비밀 필드(token/secret/password/api_key 등) 값만 Fernet 암호화한 config 사본."""
    from ..security import encrypt_str

    def walk(v: Any, key_hint: str = "") -> Any:
        if isinstance(v, dict):
            return {k: walk(x, str(k)) for k, x in v.items()}
        if isinstance(v, list):
            return [walk(x, key_hint) for x in v]
        if (
            isinstance(v, str)
            and v
            and _SECRET_FIELD_RE.search(key_hint)
            and not v.startswith("enc:v1:")
        ):
            return encrypt_str(v)
        return v

    return walk(config)


def _decrypt_config_secrets(config: dict[str, Any]) -> dict[str, Any]:
    """저장된 config의 enc:v1: 값을 복호화한 사본 — fetch 직전 사용."""
    from ..security import decrypt_str

    def walk(v: Any) -> Any:
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}
        if isinstance(v, list):
            return [walk(x) for x in v]
        if isinstance(v, str) and v.startswith("enc:v1:"):
            return decrypt_str(v)
        return v

    return walk(config)


def _connector_token(cfg: dict[str, Any]) -> str:
    """복호화된 config에서 bearer token 후보를 찾는다."""
    for key in ("token", "access_token", "api_key", "apikey", "secret", "password"):
        v = cfg.get(key)
        if isinstance(v, str) and v:
            return v
    return ""


def _fetch_connector_items(connector: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    """커넥터 라이브 fetch — github는 전용 API, 나머지는 config.url JSON fetch.

    jira/notion/linear는 config.url(+선택적 token/json_path/fields)만으로 동작하는
    generic JSON fetch로 지원한다. 실패는 FetchError/ValueError로 전파."""
    ctype = str(connector.get("type") or "")
    cfg = _decrypt_config_secrets(dict(connector.get("config") or {}))
    headers = {"Accept": "application/json", "User-Agent": "jobmate-connector"}
    token = _connector_token(cfg)
    if token:
        headers["Authorization"] = f"Bearer {token}"

    if ctype == "github":
        owner = str(cfg.get("owner") or "").strip()
        repo = str(cfg.get("repo") or cfg.get("repository") or "").strip()
        if not owner or not repo:
            raise ValueError("github connector requires config.owner and config.repo")
        state = str(cfg.get("state") or "all")
        url = (
            f"https://api.github.com/repos/{owner}/{repo}/issues"
            f"?state={state}&per_page={min(limit, 100)}&sort=updated"
        )
        gh_headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "jobmate-connector",
        }
        if token:
            gh_headers["Authorization"] = f"Bearer {token}"
        data = webutil.fetch_json(url, headers=gh_headers, timeout=10.0)
        items = []
        for it in data if isinstance(data, list) else []:
            user = it.get("user") if isinstance(it.get("user"), dict) else {}
            items.append(
                {
                    "id": str(it.get("number") or it.get("id") or ""),
                    "title": str(it.get("title") or ""),
                    "url": str(it.get("html_url") or ""),
                    "state": str(it.get("state") or ""),
                    "updated_at": str(it.get("updated_at") or ""),
                    "author": str(user.get("login") or ""),
                }
            )
        return items[:limit]

    url = str(cfg.get("url") or "").strip()
    if not url:
        raise ValueError(f"{ctype or 'connector'} requires config.url")
    # SSRF 가드(방어 심층) — 생성 시점 검증을 우회한 행(DB 직접 삽입,
    # config 변경 등)도 여기서 차단한다. fetch_json 내부 assert와 이중 확인.
    if not webutil.is_safe_fetch_url(url):
        raise ValueError("url_not_allowed")
    for k, v in (cfg.get("headers") or {}).items():
        if isinstance(k, str) and isinstance(v, str):
            headers[k] = v
    data = webutil.fetch_json(url, headers=headers, timeout=10.0)
    path = str(cfg.get("json_path") or cfg.get("items_path") or "")
    raw = webutil.find_item_list(data, path)
    fields = cfg.get("fields") if isinstance(cfg.get("fields"), dict) else None
    return webutil.normalize_items(raw, fields)[:limit]


# connector items 60초 인메모리 캐시 (connector_id → (fetched_at, items))
_CONNECTOR_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_CONNECTOR_CACHE_TTL = 60.0


# ---------- triggers (세션 상태 전이 자동화) ----------
_TRIGGER_STATE_KEY = "trigger_session_states"
_TRIGGER_MIN_INTERVAL = 45.0
_TRIGGER_LAST_CHECK = 0.0


def _trigger_match(match: dict[str, Any], sess: dict[str, Any]) -> bool:
    """match {tool?,project?,state?} — 지정된 키만 정확 일치 비교."""
    tool = match.get("tool")
    if tool and sess.get("tool") != tool:
        return False
    project = match.get("project")
    if project and sess.get("project") != project:
        return False
    state = match.get("state")
    if state and (sess.get("status") or "") != state:
        return False
    return True


def _session_transition_fp(sess: dict[str, Any], state: str) -> str:
    """전이 지문 — 같은 상태 재진입도 새 이벤트면 다른 fp가 되도록
    last_event_at/updated_at을 포함한다."""
    return f"{state}:{sess.get('last_event_at') or sess.get('updated_at') or ''}"


def _evaluate_triggers(
    store: Store,
    settings: Settings,
    llm: Any,
    adapters: dict[str, Any],
    defer: list[tuple[str, str, dict[str, Any], dict[str, Any] | None]] | None = None,
) -> int:
    """현재 agent_sessions 스냅샷 vs 이전 스냅샷(automation_state) 비교.

    세션이 match 조건을 '만족하는 상태로 전이'할 때 enabled trigger를 발사한다.
    첫 관측(이전 스냅샷에 없는 세션)은 시드만 하고 발사하지 않아 기동 직후
    기존 세션에 대한 폭주를 막는다. 발사 멱등은 trigger_fires UNIQUE가 보장.
    반환: 이번 평가에서 발사된 trigger 수."""
    triggers = store.list_triggers(enabled_only=True)
    sessions = store.list_agent_sessions(limit=200, include_ended=True)
    prev = store.get_state(_TRIGGER_STATE_KEY, {})
    if not isinstance(prev, dict):
        prev = {}
    new_prev: dict[str, str] = {}
    fired = 0
    for sess in sessions:
        tool = str(sess.get("tool") or "")
        skey = str(sess.get("session_key") or "")
        if not tool or not skey:
            continue
        ident = f"{tool}\x1f{skey}"
        cur = str(sess.get("status") or "")
        new_prev[ident] = cur
        if ident not in prev or prev.get(ident) == cur:
            continue  # 첫 관측 또는 상태 변화 없음 — 전이 아님
        for trig in triggers:
            match = trig.get("match") or {}
            action = trig.get("action") or {}
            if not _trigger_match(match, sess):
                continue
            fp = _session_transition_fp(sess, cur)
            if not store.claim_trigger_fire(trig["id"], tool, skey, cur, fp):
                continue  # 동일 전이 재평가 — 이미 발사됨
            try:
                _fire_trigger_action(store, settings, llm, adapters, trig, sess, defer)
            except Exception as e:
                store.mark_trigger_error(trig["id"], str(e)[:200])
                worker_logger.warning("trigger %s fire failed: %s", trig["id"], str(e)[:200])
                continue
            store.mark_trigger_fired(trig["id"])
            store.audit(
                "",
                "trigger",
                f"trigger_fire:{trig['id']}",
                ident,
                {"action": str(action.get("type") or ""), "state": cur},
            )
            fired += 1
            worker_logger.info(
                "trigger %s fired session=%s state=%s action=%s",
                trig["id"],
                ident,
                cur,
                action.get("type"),
            )
    store.set_state(_TRIGGER_STATE_KEY, new_prev)
    return fired


def _fire_trigger_action(
    store: Store,
    settings: Settings,
    llm: Any,
    adapters: dict[str, Any],
    trig: dict[str, Any],
    sess: dict[str, Any],
    defer: list[tuple[str, str, dict[str, Any], dict[str, Any] | None]] | None,
) -> None:
    """trigger action 실행 — run_workflow/run_playbook/report.

    run input/audit에는 세션 식별자(tool/session_key/project/status)만 넣고
    doing/ask/ended_summary 등 세션 본문은 절대 포함하지 않는다."""
    from ..reports import REPORT_KIND_LABELS, build_report

    action = trig.get("action") or {}
    atype = str(action.get("type") or "")
    tid = str(trig.get("id") or "")
    tool = str(sess.get("tool") or "")
    skey = str(sess.get("session_key") or "")
    sess_ref = {
        "tool": tool,
        "session_key": skey,
        "project": sess.get("project"),
        "status": sess.get("status"),
    }
    trigger_meta = {"id": tid, "session": sess_ref}

    def _launch(workflow: str, payload: dict[str, Any]) -> None:
        if workflow not in WORKFLOWS:
            raise ValueError(f"unknown workflow {workflow}")
        # 스코프드 예산 — hard_stop 초과 시 트리거 발사 보류(예외 없이 스킵)
        from .. import budget_scope, wakeups

        blocks = budget_scope.evaluate_admission(store, workflow, str(payload.get("project") or ""))
        if blocks:
            store.audit(
                "",
                "trigger",
                "trigger_budget_exceeded",
                tid,
                {"workflow": workflow, "scope": blocks[0]["scope"]},
            )
            worker_logger.warning(
                "trigger %s blocked: budget exceeded scope=%s", tid, blocks[0]["scope"]
            )
            return
        res = wakeups.admit_run(
            store,
            workflow_key=workflow,
            input_data=payload,
            source="trigger",
            created_by=f"trigger:{tid}",
            version=WORKFLOWS[workflow].version,
            job_payload={"task": payload.get("task", "")},
        )
        if res["action"] == "invalid_goal":
            worker_logger.warning("trigger %s blocked: invalid goal %s", tid, res.get("error"))
            return
        if res["action"] != "created":
            worker_logger.info(
                "trigger %s %s into run=%s", tid, res["action"], res.get("run_id") or "-"
            )
            return
        run = res["run"]
        job = (run.id, workflow, {"task": payload.get("task", "")}, None)
        if defer is not None:
            defer.append(job)
        else:
            # jobs.py는 support를 import하므로 순환 방지로 지연 import
            from .jobs import _execute_run_job

            threading.Thread(
                target=_execute_run_job,
                args=(store, settings, llm, adapters, *job),
                daemon=True,
            ).start()
        worker_logger.info("trigger %s launched run=%s workflow=%s", tid, run.id, workflow)

    if atype == "run_workflow":
        payload: dict[str, Any] = {
            "task": f"trigger {tid}: {tool}/{skey[:12]}",
            "trigger": trigger_meta,
        }
        # 세션의 project 태그를 run input에 전파 — project 스코프 예산
        # 귀속과 _launch의 예산 평가가 세션 프로젝트를 보게 한다.
        if sess.get("project"):
            payload["project"] = str(sess["project"])
        _launch(str(action.get("workflow") or ""), payload)
    elif atype == "run_playbook":
        pb = store.get_playbook(str(action.get("playbook_id") or ""))
        if pb is None:
            raise ValueError("playbook not found")
        payload = dict(pb.get("input") or {})
        payload.setdefault("task", f"trigger {tid}: {pb.get('name') or pb['id']}")
        payload["trigger"] = {**trigger_meta, "playbook_id": pb["id"]}
        _launch(str(pb.get("workflow") or ""), payload)
    elif atype == "report":
        kind = str(action.get("report_kind") or "daily")
        if kind not in ("daily", "weekly"):
            kind = "daily"
        content = build_report(store, kind, settings)
        rid = store.save_report(kind, content)
        store.upsert_inbox_item(
            "report",
            f"{REPORT_KIND_LABELS.get(kind, kind)} 리포트(트리거)",
            body=(
                f"runs={content['runs']['total']} failed={content['runs']['failed']} "
                f"pending={content['approvals']['pending']}"
            ),
            link="/?view=reports",
            dedupe_key=f"trigger-report:{tid}:{tool}:{skey}:{sess.get('status')}",
        )
        try:
            messenger.notify(
                f"jobMate {REPORT_KIND_LABELS.get(kind, kind)} 리포트(트리거): "
                f"run {content['runs']['total']}건"
            )
        except Exception:
            worker_logger.debug("messenger notify after trigger report failed", exc_info=True)
        worker_logger.info("trigger %s generated report id=%s kind=%s", tid, rid, kind)
    else:
        raise ValueError(f"unknown trigger action type {atype}")


@job_traceable("jobmate.check_triggers")
def _maybe_check_triggers(
    store: Store,
    settings: Settings,
    llm: Any,
    adapters: dict[str, Any],
    defer: list[tuple[str, str, dict[str, Any], dict[str, Any] | None]] | None = None,
) -> None:
    """45초 스로틀로 trigger 평가 — automation loop(2s)와 cron tick 공용."""
    global _TRIGGER_LAST_CHECK
    now = time.time()
    if now - _TRIGGER_LAST_CHECK < _TRIGGER_MIN_INTERVAL:
        return
    _TRIGGER_LAST_CHECK = now
    try:
        _evaluate_triggers(store, settings, llm, adapters, defer=defer)
    except Exception:
        worker_logger.exception("trigger evaluation error")
