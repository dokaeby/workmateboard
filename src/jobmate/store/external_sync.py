# mypy: disable-error-code="attr-defined"
"""외부 오케스트레이터 projection 스토어 메서드 믹스인 (db.py Store에서 추출).

agent-management 3차 (paperclip spec E/F) — external_agents/external_tasks/
external_runs/external_sync_cursors upsert·snapshot ingest·커서/상태 조회.
스키마는 db.py SCHEMA/SCHEMA_PG에 이미 정의돼 있다(provider+external_id UNIQUE).

sync 의미:
- snapshot은 (provider, external_id) 기준 upsert — snapshot에 없는 기존
  행을 삭제하지 않는다. 외부 연결이 끊겨도 마지막 정상 snapshot이 stale
  배지와 함께 유지된다(spec F).
- 커서는 external_sync_cursors.cursor JSON 안에 entity별 값을 병합해
  보관한다(스키마에 entity 컬럼이 없으므로 {"agents":..,"tasks":..,"runs":..}
  형태). snapshot ingest 전체가 하나의 tx라 부분 실패 시 커서가 전진하지
  않는다(spec G).
- 멱등: Idempotency-Key + payload 해시를 external_snapshot_keys 테이블에
  기록한다(해당 테이블은 이 믹스인이 최초 사용 시 lazy 생성 — db.py 스키마를
  건드리지 않기 위함). 동일 키+동일 페이로드는 replay, 동일 키+다른
  페이로드는 SnapshotConflictError.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from .util import _iso, _now

logger = logging.getLogger("jobmate.store")

_PROVIDER_MAX = 40
_EXTERNAL_ID_MAX = 200
_COMPANY_MAX = 200
_TITLE_MAX = 300
_STATUS_MAX = 60
_SUMMARY_MAX = 4000
_CURSOR_VALUE_MAX = 500
_CURSOR_ENTITIES = ("agents", "tasks", "runs")
_SNAPSHOT_KEY_KEEP = 100
_SNAPSHOT_KEY_TTL = timedelta(days=7)
# status 응답의 stale 판정 — 마지막 동기화 후 이 시간 이상 지나면 stale.
_STALE_AFTER_SECONDS = 900

# agent_management._SECRET_KEY_RE와 동일 규칙 — store 계층이 services를
# import하지 않도록 여기서 독립 정의한다. payload_summary/token_cost_summary에
# credential·transcript·adapter env·prompt 원문을 저장하지 않는다(spec E).
_SECRET_KEY_RE = re.compile(
    r"(key|secret|token|password|passwd|api[_-]?key|credential|private|bearer"
    r"|dsn|database[_-]?url|connection[_-]?string|authorization|session|cookie"
    r"|authenticat|auth(?![a-z]))",
    re.IGNORECASE,
)
# 값 전체를 동기화 대상에서 제외하는 서브트리 키 — 대화/프롬프트/어댑터 설정.
_DROP_SUBTREE_KEYS = frozenset(
    {
        "transcript",
        "prompt",
        "messages",
        "logs",
        "events",
        "adapter_config",
        "adapterconfig",
        "env",
        "environment",
        "credentials",
        "secrets",
    }
)

# external_runs.token_cost_summary 허용 키 — 스칼라 값만 보관한다.
# 토큰/비용 요약만 남기고 임의 JSON 페이로드는 저장하지 않는다.
_RUN_SUMMARY_ALLOW = frozenset(
    {
        "inputtokens",
        "outputtokens",
        "cachedinputtokens",
        "totaltokens",
        "costmicrousd",
        "costusd",
        "model",
        "provider",
        "durationms",
        "exitcode",
        "startedat",
        "finishedat",
    }
)


class SnapshotConflictError(Exception):
    """동일 idempotency_key가 다른 payload로 재사용됨 — 라우터가 409로 변환."""


def _clean(v: Any, max_len: int) -> str:
    return str(v or "").strip()[:max_len]


def _norm_key(k: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(k).lower())


def sanitize_external_payload(value: Any, *, depth: int = 0) -> Any:
    """외부 페이로드 재귀 정제 — secret-like 키와 대화/프롬프트/어댑터
    서브트리를 제거한다. depth 6·list 50개·문자열 500자 상한."""
    if depth > 6:
        return None
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            key = str(k)[:80]
            if _SECRET_KEY_RE.search(key) or key.lower() in _DROP_SUBTREE_KEYS:
                continue
            sv = sanitize_external_payload(v, depth=depth + 1)
            if sv is not None:
                out[key] = sv
        return out
    if isinstance(value, list):
        return [sanitize_external_payload(v, depth=depth + 1) for v in value[:50]]
    if isinstance(value, str):
        return value[:500]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return None


def _summary_json(value: Any) -> str:
    """payload_summary용 JSON — 정제 후 _SUMMARY_MAX를 넘으면 키 목록만 남긴다
    (잘린 JSON은 파싱 불가 dict가 돼 조용히 정보를 잃으므로)."""
    if not isinstance(value, dict):
        return "{}"
    clean = sanitize_external_payload(value)
    s = json.dumps(clean if isinstance(clean, dict) else {}, ensure_ascii=False)
    if len(s) <= _SUMMARY_MAX:
        return s
    keys = sorted(str(k) for k in clean)[:50] if isinstance(clean, dict) else []
    return json.dumps({"_truncated": True, "keys": keys}, ensure_ascii=False)


def _run_cost_summary_json(value: Any) -> str:
    """token_cost_summary — allowlist 스칼라 키만 추출(평탄 dict 기준).
    'usage'/'cost' 같은 중첩 객체는 평탄화하지 않고 무시한다."""
    if not isinstance(value, dict):
        return "{}"
    out: dict[str, Any] = {}
    for k, v in value.items():
        nk = _norm_key(k)
        if nk not in _RUN_SUMMARY_ALLOW:
            continue
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            if abs(v) > 1e15:
                continue
            out[nk] = v
        elif isinstance(v, str):
            out[nk] = v[:120]
    s = json.dumps(out, ensure_ascii=False)
    return s if len(s) <= _SUMMARY_MAX else "{}"


def _loads_dict(s: Any) -> dict[str, Any]:
    try:
        v = json.loads(str(s or ""))
        return v if isinstance(v, dict) else {}
    except (ValueError, TypeError):
        return {}


def _parse_ts(s: Any) -> datetime | None:
    """ISO-8601 타임스탬프 비교용 파싱 — date-only도 허용, naive는 UTC 간주."""
    try:
        dt = datetime.fromisoformat(str(s or "").strip().replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _cursor_value_ok(v: Any) -> bool:
    """ingest 커서 값 검증 — 저장 커서는 entity별 watermark로도 쓰이므로
    ISO-8601 파싱 가능한 타임스탬프만 허용한다. malformed 값("zzz" 등)을
    저장하면 이후 모든 정상 타임스탬프가 문자열 비교에서 밀려 watermark가
    영구 정지한다(R2-4)."""
    s = str(v or "").strip()
    return bool(s) and _parse_ts(s) is not None


def _cursor_newer(old: Any, new: Any) -> bool:
    """커서 단조 전진 판정 — parse-aware 우선순위 비교.

    - new가 비어 있으면 False. new가 파싱 가능하고 old가 비어 있거나
      파싱 불가면 True — 깨진/비어 있는 기존 값을 정상 타임스탬프로
      복구한다(R2-4: "zzz" 같은 malformed 저장값이 이후 정상 커서를
      영구 차단하던 문제).
    - new가 파싱 불가하고 old가 파싱 가능하면 False — 정상 watermark를
      깨진 값으로 회귀시키지 않는다.
    - 둘 다 파싱 가능하면 시각 비교, 둘 다 불가하면 문자열 비교로 수렴
      (opaque 커서 호환 — store 직접 호출 경로용). ingest 경로는
      _cursor_value_ok가 먼저 걸러 이 분기에 도달하지 않는다."""
    old_s = str(old or "").strip()
    new_s = str(new or "").strip()
    if not new_s:
        return False
    odt, ndt = _parse_ts(old_s), _parse_ts(new_s)
    if ndt is not None:
        if odt is None:
            return True
        return ndt > odt
    if odt is not None:
        return False
    return not old_s or new_s > old_s


class ExternalSyncStoreMixin:
    """external_* projection Store 메서드 — snapshot ingest/커서/status."""

    # ---- 행 upsert (공개 — 테스트/부분 동기화용) ----

    def upsert_external_agent(
        self,
        *,
        provider: str,
        company_id: str,
        external_id: str,
        payload_summary: Any = None,
        status: str = "",
    ) -> dict[str, Any]:
        prov = _clean(provider, _PROVIDER_MAX) or "paperclip"
        comp = _clean(company_id, _COMPANY_MAX)
        ext = _clean(external_id, _EXTERNAL_ID_MAX)
        if not ext:
            raise ValueError("external_id is required")
        now = _now()
        ts = _iso(now)
        with self.tx() as c:
            c.execute(
                "INSERT INTO external_agents(id,provider,external_id,company_id,"
                "payload_summary,status,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?)"
                " ON CONFLICT (provider,company_id,external_id) DO UPDATE SET"
                " company_id=excluded.company_id,"
                " payload_summary=excluded.payload_summary,"
                " status=excluded.status,"
                " updated_at=excluded.updated_at",
                (
                    uuid.uuid4().hex[:12],
                    prov,
                    ext,
                    comp,
                    _summary_json(payload_summary),
                    _clean(status, _STATUS_MAX),
                    ts,
                    ts,
                ),
            )
            row = c.execute(
                "SELECT * FROM external_agents WHERE provider=? AND company_id=? AND external_id=?",
                (prov, comp, ext),
            ).fetchone()
        return dict(row) if row is not None else {}

    def upsert_external_task(
        self,
        *,
        provider: str,
        company_id: str,
        external_id: str,
        assignee_external_id: str | None = None,
        status: str = "",
        title: str = "",
    ) -> dict[str, Any]:
        prov = _clean(provider, _PROVIDER_MAX) or "paperclip"
        comp = _clean(company_id, _COMPANY_MAX)
        ext = _clean(external_id, _EXTERNAL_ID_MAX)
        if not ext:
            raise ValueError("external_id is required")
        now = _now()
        ts = _iso(now)
        with self.tx() as c:
            c.execute(
                "INSERT INTO external_tasks(id,provider,external_id,company_id,"
                "assignee_external_id,status,title,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT (provider,company_id,external_id) DO UPDATE SET"
                " assignee_external_id=excluded.assignee_external_id,"
                " status=excluded.status,"
                " title=excluded.title,"
                " updated_at=excluded.updated_at",
                (
                    uuid.uuid4().hex[:12],
                    prov,
                    ext,
                    comp,
                    _clean(assignee_external_id, _EXTERNAL_ID_MAX) or None,
                    _clean(status, _STATUS_MAX),
                    _clean(title, _TITLE_MAX),
                    ts,
                    ts,
                ),
            )
            row = c.execute(
                "SELECT * FROM external_tasks WHERE provider=? AND company_id=? AND external_id=?",
                (prov, comp, ext),
            ).fetchone()
        return dict(row) if row is not None else {}

    def upsert_external_run(
        self,
        *,
        provider: str,
        company_id: str,
        external_id: str,
        task_external_id: str | None = None,
        agent_external_id: str | None = None,
        status: str = "",
        token_cost_summary: Any = None,
    ) -> dict[str, Any]:
        prov = _clean(provider, _PROVIDER_MAX) or "paperclip"
        comp = _clean(company_id, _COMPANY_MAX)
        ext = _clean(external_id, _EXTERNAL_ID_MAX)
        if not ext:
            raise ValueError("external_id is required")
        now = _now()
        ts = _iso(now)
        with self.tx() as c:
            c.execute(
                "INSERT INTO external_runs(id,provider,external_id,company_id,"
                "task_external_id,agent_external_id,status,token_cost_summary,"
                "created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT (provider,company_id,external_id) DO UPDATE SET"
                " task_external_id=excluded.task_external_id,"
                " agent_external_id=excluded.agent_external_id,"
                " status=excluded.status,"
                " token_cost_summary=excluded.token_cost_summary,"
                " updated_at=excluded.updated_at",
                (
                    uuid.uuid4().hex[:12],
                    prov,
                    ext,
                    comp,
                    _clean(task_external_id, _EXTERNAL_ID_MAX) or None,
                    _clean(agent_external_id, _EXTERNAL_ID_MAX) or None,
                    _clean(status, _STATUS_MAX),
                    _run_cost_summary_json(token_cost_summary),
                    ts,
                    ts,
                ),
            )
            row = c.execute(
                "SELECT * FROM external_runs WHERE provider=? AND company_id=? AND external_id=?",
                (prov, comp, ext),
            ).fetchone()
        return dict(row) if row is not None else {}

    # ---- snapshot ingest (멱등 + 원자적) ----

    def _ensure_snapshot_keys_table(self) -> None:
        """snapshot 멱등키 테이블 lazy 생성 — db.py 스키마를 건드리지 않기
        위해 최초 ingest 시점에 만든다. CREATE IF NOT EXISTS라 동시 호출도
        안전하다."""
        if getattr(self, "_snapshot_keys_ready", False):
            return
        ddl = (
            "CREATE TABLE IF NOT EXISTS external_snapshot_keys("
            "idempotency_key TEXT PRIMARY KEY,"
            "payload_hash TEXT NOT NULL,"
            "provider TEXT NOT NULL,"
            "company_id TEXT NOT NULL,"
            "result_json TEXT NOT NULL DEFAULT '{}',"
            "created_at TEXT NOT NULL)"
        )
        with self.tx() as c:
            c.execute(ddl)
        self._snapshot_keys_ready = True

    def ingest_external_snapshot(
        self,
        *,
        provider: str,
        company_id: str,
        agents: list[dict[str, Any]] | None = None,
        tasks: list[dict[str, Any]] | None = None,
        runs: list[dict[str, Any]] | None = None,
        cursors: dict[str, Any] | None = None,
        idempotency_key: str = "",
        payload_hash: str = "",
    ) -> dict[str, Any]:
        """snapshot 1회를 원자적으로 반영한다.

        반환: {"replayed": bool, "agents": n, "tasks": n, "runs": n,
        "last_synced_at": iso}. 동일 idempotency_key+동일 payload_hash는
        replayed=True로 기존 결과를 반환하고, 동일 키+다른 해시는
        SnapshotConflictError. upsert·커서 갱신·멱등키 기록이 한 tx라
        중간 실패 시 아무것도 반영되지 않는다(커서 오전진 방지)."""
        self._ensure_snapshot_keys_table()
        if getattr(self, "_external_company_schema_broken", False):
            # company_id 마이그레이션 실패 상태 — 3컬럼 ON CONFLICT가
            # sqlite OperationalError로 깨지기 전에 원인을 명시한다.
            raise RuntimeError(
                "external_* schema migration failed — store 재기동으로 마이그레이션을 재시도하세요"
            )
        prov = _clean(provider, _PROVIDER_MAX) or "paperclip"
        comp = _clean(company_id, _COMPANY_MAX)
        key = _clean(idempotency_key, 200)
        phash = _clean(payload_hash, 128)
        agents = agents or []
        tasks = tasks or []
        runs = runs or []
        with self.tx() as c:
            if key:
                existing = c.execute(
                    "SELECT payload_hash,result_json FROM external_snapshot_keys"
                    " WHERE idempotency_key=?",
                    (key,),
                ).fetchone()
                if existing is not None:
                    if str(existing["payload_hash"] or "") != phash:
                        raise SnapshotConflictError(
                            "idempotency_key already used with a different payload"
                        )
                    replay = _loads_dict(existing["result_json"])
                    replay["replayed"] = True
                    return replay
            now = _now()
            ts = _iso(now)
            counts = {"agents": 0, "tasks": 0, "runs": 0}
            # M4 — stale snapshot 방어: entity별 저장 커서를 watermark로 써서
            # 그보다 오래된 행(item.updated_at < 저장 cursor)은 upsert하지
            # 않는다. 커서 자체도 단조 전진만 허용(회귀 거부).
            cur_row = c.execute(
                "SELECT cursor FROM external_sync_cursors WHERE provider=? AND company_id=?",
                (prov, comp),
            ).fetchone()
            stored_cursors = _loads_dict(cur_row["cursor"]) if cur_row is not None else {}

            def _stale(entity: str, item: dict[str, Any]) -> bool:
                wm = _parse_ts(stored_cursors.get(entity))
                it = _parse_ts(item.get("updated_at"))
                return wm is not None and it is not None and it < wm

            for item in agents:
                if not isinstance(item, dict):
                    continue
                ext = _clean(item.get("external_id"), _EXTERNAL_ID_MAX)
                if not ext or _stale("agents", item):
                    continue
                c.execute(
                    "INSERT INTO external_agents(id,provider,external_id,company_id,"
                    "payload_summary,status,created_at,updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?)"
                    " ON CONFLICT (provider,company_id,external_id) DO UPDATE SET"
                    " company_id=excluded.company_id,"
                    " payload_summary=excluded.payload_summary,"
                    " status=excluded.status,"
                    " updated_at=excluded.updated_at",
                    (
                        uuid.uuid4().hex[:12],
                        prov,
                        ext,
                        comp,
                        _summary_json(item.get("payload_summary")),
                        _clean(item.get("status"), _STATUS_MAX),
                        ts,
                        ts,
                    ),
                )
                counts["agents"] += 1
            for item in tasks:
                if not isinstance(item, dict):
                    continue
                ext = _clean(item.get("external_id"), _EXTERNAL_ID_MAX)
                if not ext or _stale("tasks", item):
                    continue
                c.execute(
                    "INSERT INTO external_tasks(id,provider,external_id,company_id,"
                    "assignee_external_id,status,title,created_at,updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT (provider,company_id,external_id) DO UPDATE SET"
                    " assignee_external_id=excluded.assignee_external_id,"
                    " status=excluded.status,"
                    " title=excluded.title,"
                    " updated_at=excluded.updated_at",
                    (
                        uuid.uuid4().hex[:12],
                        prov,
                        ext,
                        comp,
                        _clean(item.get("assignee_external_id"), _EXTERNAL_ID_MAX) or None,
                        _clean(item.get("status"), _STATUS_MAX),
                        _clean(item.get("title"), _TITLE_MAX),
                        ts,
                        ts,
                    ),
                )
                counts["tasks"] += 1
            for item in runs:
                if not isinstance(item, dict):
                    continue
                ext = _clean(item.get("external_id"), _EXTERNAL_ID_MAX)
                if not ext or _stale("runs", item):
                    continue
                c.execute(
                    "INSERT INTO external_runs(id,provider,external_id,company_id,"
                    "task_external_id,agent_external_id,status,token_cost_summary,"
                    "created_at,updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT (provider,company_id,external_id) DO UPDATE SET"
                    " task_external_id=excluded.task_external_id,"
                    " agent_external_id=excluded.agent_external_id,"
                    " status=excluded.status,"
                    " token_cost_summary=excluded.token_cost_summary,"
                    " updated_at=excluded.updated_at",
                    (
                        uuid.uuid4().hex[:12],
                        prov,
                        ext,
                        comp,
                        _clean(item.get("task_external_id"), _EXTERNAL_ID_MAX) or None,
                        _clean(item.get("agent_external_id"), _EXTERNAL_ID_MAX) or None,
                        _clean(item.get("status"), _STATUS_MAX),
                        _run_cost_summary_json(item.get("token_cost_summary")),
                        ts,
                        ts,
                    ),
                )
                counts["runs"] += 1
            # 커서 갱신 — 기존 entity별 cursor JSON에 병합하되 단조 전진만
            # 허용한다(더 오래된 snapshot이 커서를 되돌리지 못하게).
            # 값은 ISO-8601 파싱 가능해야 한다 — malformed 커서는 watermark
            # 비교를 오염시키므로 저장하지 않는다(R2-4).
            cur_map = dict(stored_cursors)
            if isinstance(cursors, dict):
                for ek in _CURSOR_ENTITIES:
                    v = cursors.get(ek)
                    if v is not None and _cursor_value_ok(v) and _cursor_newer(cur_map.get(ek), v):
                        cur_map[ek] = _clean(v, _CURSOR_VALUE_MAX)
            c.execute(
                "INSERT INTO external_sync_cursors(id,provider,company_id,cursor,"
                "last_synced_at,last_error_code,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?)"
                " ON CONFLICT (provider,company_id) DO UPDATE SET"
                " cursor=excluded.cursor,"
                " last_synced_at=excluded.last_synced_at,"
                " last_error_code='',"
                " updated_at=excluded.updated_at",
                (
                    uuid.uuid4().hex[:12],
                    prov,
                    comp,
                    json.dumps(cur_map, ensure_ascii=False)[:8000],
                    ts,
                    "",
                    ts,
                    ts,
                ),
            )
            result: dict[str, Any] = {
                "replayed": False,
                "provider": prov,
                "company_id": comp,
                **counts,
                "last_synced_at": ts,
            }
            if key:
                c.execute(
                    "INSERT INTO external_snapshot_keys(idempotency_key,payload_hash,"
                    "provider,company_id,result_json,created_at)"
                    " VALUES(?,?,?,?,?,?)",
                    (key, phash, prov, comp, json.dumps(result, ensure_ascii=False), ts),
                )
                # 멱등키 테이블 상한 — 최근 _SNAPSHOT_KEY_KEEP개 + 7일 이내만 유지.
                cutoff = _iso(now - _SNAPSHOT_KEY_TTL)
                c.execute(
                    "DELETE FROM external_snapshot_keys WHERE created_at < ?"
                    " OR idempotency_key NOT IN ("
                    " SELECT idempotency_key FROM external_snapshot_keys"
                    " ORDER BY created_at DESC LIMIT ?)",
                    (cutoff, _SNAPSHOT_KEY_KEEP),
                )
        return result

    # ---- 커서/상태 조회 ----

    def get_external_cursor(self, provider: str, company_id: str) -> dict[str, Any] | None:
        """(provider, company_id) 동기화 커서 — entity별 cursor 맵 포함."""
        prov = _clean(provider, _PROVIDER_MAX) or "paperclip"
        comp = _clean(company_id, _COMPANY_MAX)
        with self.tx() as c:
            row = c.execute(
                "SELECT * FROM external_sync_cursors WHERE provider=? AND company_id=?",
                (prov, comp),
            ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["cursors"] = _loads_dict(d.get("cursor"))
        return d

    def mark_external_sync_error(self, provider: str, company_id: str, error_code: str) -> None:
        """동기화 실패 기록 — last_error_code만 갱신하고 last_synced_at과
        entity 커서는 건드리지 않는다(부분 실패 시 커서 오전진 방지)."""
        prov = _clean(provider, _PROVIDER_MAX) or "paperclip"
        comp = _clean(company_id, _COMPANY_MAX)
        now = _now()
        ts = _iso(now)
        with self.tx() as c:
            c.execute(
                "INSERT INTO external_sync_cursors(id,provider,company_id,cursor,"
                "last_synced_at,last_error_code,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?)"
                " ON CONFLICT (provider,company_id) DO UPDATE SET"
                " last_error_code=excluded.last_error_code,"
                " updated_at=excluded.updated_at",
                (
                    uuid.uuid4().hex[:12],
                    prov,
                    comp,
                    "{}",
                    None,
                    _clean(error_code, 80),
                    ts,
                    ts,
                ),
            )

    def external_sync_status(
        self, provider: str | None = None, *, stale_after_seconds: int = _STALE_AFTER_SECONDS
    ) -> dict[str, Any]:
        """provider/company별 projection 상태 — 커서 + 엔티티별 행 수 + stale.

        stale은 마지막 성공 동기화(last_synced_at) 기준 시간 판정이며
        snapshot 행 자체는 삭제하지 않는다(spec F)."""
        prov = _clean(provider, _PROVIDER_MAX) if provider else ""
        now = _now()
        with self.tx() as c:
            cursor_rows = c.execute(
                "SELECT * FROM external_sync_cursors"  # noqa: S608 - 고정 절만, 값은 바인딩
                + (" WHERE provider=?" if prov else "")
                + " ORDER BY company_id, provider",
                (prov,) if prov else (),
            ).fetchall()
            # external_tasks/runs는 company_id 컬럼을 직접 가진다
            # (M5 — 이전에는 assignee/agent 조인으로 유도했다).
            agent_rows = c.execute(
                "SELECT provider, company_id, COUNT(*) AS n FROM external_agents"  # noqa: S608 - 고정 절만, 값은 바인딩
                + (" WHERE provider=?" if prov else "")
                + " GROUP BY provider, company_id",
                (prov,) if prov else (),
            ).fetchall()
            task_rows = c.execute(
                "SELECT provider, company_id, COUNT(*) AS n FROM external_tasks"  # noqa: S608 - 고정 절만, 값은 바인딩
                + (" WHERE provider=?" if prov else "")
                + " GROUP BY provider, company_id",
                (prov,) if prov else (),
            ).fetchall()
            run_rows = c.execute(
                "SELECT provider, company_id, COUNT(*) AS n FROM external_runs"  # noqa: S608 - 고정 절만, 값은 바인딩
                + (" WHERE provider=?" if prov else "")
                + " GROUP BY provider, company_id",
                (prov,) if prov else (),
            ).fetchall()
        counts: dict[tuple[str, str], dict[str, int]] = {}
        for rows, field in (
            (agent_rows, "agents"),
            (task_rows, "tasks"),
            (run_rows, "runs"),
        ):
            for r in rows:
                key = (str(r["provider"]), str(r["company_id"] or ""))
                slot = counts.setdefault(key, {"agents": 0, "tasks": 0, "runs": 0})
                slot[field] = int(r["n"] or 0)
        # sqlite3.Row는 .get이 없으므로 dict로 변환한다.
        cursor_map = {(str(r["provider"]), str(r["company_id"])): dict(r) for r in cursor_rows}
        items: list[dict[str, Any]] = []
        for key in sorted(set(counts) | set(cursor_map)):
            p, comp = key
            cur = cursor_map.get(key)
            cnt = counts.get(key, {"agents": 0, "tasks": 0, "runs": 0})
            last_synced = cur.get("last_synced_at") if cur else None
            stale = True
            if last_synced:
                try:
                    dt = datetime.fromisoformat(str(last_synced))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=UTC)
                    stale = (now - dt).total_seconds() > stale_after_seconds
                except ValueError:
                    stale = True
            item: dict[str, Any] = {
                "provider": p,
                "company_id": comp,
                "counts": cnt,
                "last_synced_at": last_synced,
                "last_error_code": (cur.get("last_error_code") if cur else "") or "",
                "cursors": _loads_dict(cur.get("cursor")) if cur else {},
                "stale": stale,
            }
            items.append(item)
        return {"items": items, "generated_at": _iso(now)}
