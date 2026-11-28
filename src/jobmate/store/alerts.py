# mypy: disable-error-code="attr-defined"
"""Alert rule store mixin — spec 006 US1.

alert_rules/alert_rule_fires는 org-scoped 테이블 — webhooks.py와 동일 규칙:
읽기는 orgctx 필터(ctx=''는 무필터), 쓰기는 org_for_write로 org_id를 찍는다.
조건 평가·채널 발송은 services/alert_rules.py 책임 — 이 믹스인은 CRUD와
발사 이력 기록만 담당한다.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from . import orgctx
from .util import _clamp_limit, _iso, _now

# fire.status 어휘 — fired(채널 발송 완료)/partial(일부 채널 실패)/
# dispatching(claim 후 발송 진행 중)/suppressed_cooldown(FR-005).
FIRE_FIRED = "fired"
FIRE_PARTIAL = "partial"
FIRE_DISPATCHING = "dispatching"
FIRE_SUPPRESSED_COOLDOWN = "suppressed_cooldown"

_CHANNEL_STATUSES = ("sent", "skipped_unconfigured", "failed")

# alert_rule_fires 보존 기간 — claim 트랜잭션에서 함께 prune한다.
_FIRE_RETENTION_DAYS = 90
# cooldown_minutes=0인 규칙의 same-fingerprint dedup은 사실상 영구 —
# 동일 이벤트가 스윕/재평가로 반복 emit돼도 재발송하지 않는다.
# _FAR_PAST를 dedup cutoff로 쓰면 모든 기존 fire가 `created_at > cutoff`를
# 만족해 NOT EXISTS가 claim을 차단한다(역사적으로 _FAR_FUTURE를 썼다가
# dedup이 무력화돼 발송 스팸이 발생한 회귀 — _FAR_FUTURE는 사용 금지).
_FAR_PAST_ISO = "1970-01-01T00:00:00+00:00"


def _org_filter() -> tuple[str, list[Any]]:
    """ctx 조직이 있으면 ` AND org_id=?` 조건 — 시스템/미인증(ctx='')은 무필터."""
    oid = orgctx.current_org_id()
    return (" AND org_id=?", [oid]) if oid else ("", [])


def _rule_dict(r: Any) -> dict[str, Any]:
    d = dict(r)
    d["enabled"] = bool(d.get("enabled"))
    try:
        d["cooldown_minutes"] = max(0, int(d.get("cooldown_minutes") or 0))
    except (TypeError, ValueError):
        d["cooldown_minutes"] = 0
    try:
        cond = json.loads(d.get("condition_json") or "{}")
        d["condition"] = cond if isinstance(cond, dict) else {}
    except (ValueError, TypeError):
        d["condition"] = {}
    try:
        ch = json.loads(d.get("channels_json") or "[]")
        # 항목은 str 또는 {"type","to"} dict — str()로 변환하면 dict가
        # repr 문자열로 깨지므로 원형을 유지한다.
        d["channels"] = list(ch) if isinstance(ch, list) else []
    except (ValueError, TypeError):
        d["channels"] = []
    return d


def _fire_dict(r: Any) -> dict[str, Any]:
    d = dict(r)
    try:
        ch = json.loads(d.get("channels_json") or "[]")
        d["channels"] = ch if isinstance(ch, list) else []
    except (ValueError, TypeError):
        d["channels"] = []
    return d


def _clean_channels(channels: list[dict[str, Any]] | None) -> list[dict[str, str]]:
    """channels_json 저장용 정규화 — [{channel, status}]만 남긴다."""
    return [
        {
            "channel": str(ch.get("channel") or "")[:40],
            "status": (
                str(ch.get("status") or "")
                if str(ch.get("status") or "") in _CHANNEL_STATUSES
                else "failed"
            ),
        }
        for ch in (channels or [])
        if isinstance(ch, dict)
    ]


class AlertsStoreMixin:
    # ---- alert_rules CRUD ----
    def create_alert_rule(
        self,
        *,
        name: str,
        condition: dict[str, Any],
        channels: list[Any],
        cooldown_minutes: int = 60,
        enabled: bool = True,
        created_by: str = "",
        org_id: str | None = None,
    ) -> dict[str, Any]:
        rid = uuid.uuid4().hex[:12]
        now = _iso(_now())
        try:
            cooldown = max(0, int(cooldown_minutes))
        except (TypeError, ValueError):
            cooldown = 60
        oid = orgctx.org_for_write(org_id)
        with self.tx() as c:
            c.execute(
                "INSERT INTO alert_rules(id,org_id,name,enabled,condition_json,"
                "channels_json,cooldown_minutes,last_fired_at,created_by,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    rid,
                    oid,
                    name[:200],
                    1 if enabled else 0,
                    json.dumps(condition or {}, ensure_ascii=False),
                    json.dumps(list(channels or []), ensure_ascii=False),
                    cooldown,
                    None,
                    created_by[:200],
                    now,
                ),
            )
        return self.get_alert_rule(rid) or {"id": rid}

    def get_alert_rule(self, rule_id: str) -> dict[str, Any] | None:
        frag, params = _org_filter()
        row = self._conn.execute(
            "SELECT * FROM alert_rules WHERE id=?" + frag,  # noqa: S608 - frag는 고정 패턴
            (rule_id, *params),
        ).fetchone()
        return _rule_dict(row) if row else None

    def list_alert_rules(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        frag, params = _org_filter()
        sql = "SELECT * FROM alert_rules"
        conds: list[str] = []
        if enabled_only:
            conds.append("enabled=1")
        if frag:
            conds.append(frag.strip().removeprefix("AND").strip())
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY created_at DESC"
        return [_rule_dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def update_alert_rule(
        self,
        rule_id: str,
        *,
        name: str | None = None,
        enabled: bool | None = None,
        condition: dict[str, Any] | None = None,
        channels: list[Any] | None = None,
        cooldown_minutes: int | None = None,
    ) -> bool:
        """부분 갱신 — None 인자는 기존 값 유지."""
        frag, params = _org_filter()
        sets: list[str] = []
        vals: list[Any] = []
        if name is not None:
            sets.append("name=?")
            vals.append(name[:200])
        if enabled is not None:
            sets.append("enabled=?")
            vals.append(1 if enabled else 0)
        if condition is not None:
            sets.append("condition_json=?")
            vals.append(json.dumps(condition, ensure_ascii=False))
        if channels is not None:
            sets.append("channels_json=?")
            vals.append(json.dumps(list(channels), ensure_ascii=False))
        if cooldown_minutes is not None:
            try:
                cd = max(0, int(cooldown_minutes))
            except (TypeError, ValueError):
                cd = 0
            sets.append("cooldown_minutes=?")
            vals.append(cd)
        if not sets:
            return self.get_alert_rule(rule_id) is not None
        sql = "UPDATE alert_rules SET " + ", ".join(sets) + " WHERE id=?" + frag  # noqa: S608
        with self.tx() as c:
            cur = c.execute(sql, (*vals, rule_id, *params))
        return cur.rowcount > 0

    def delete_alert_rule(self, rule_id: str) -> bool:
        frag, params = _org_filter()
        with self.tx() as c:
            cur = c.execute(
                "DELETE FROM alert_rules WHERE id=?" + frag,  # noqa: S608
                (rule_id, *params),
            )
            if cur.rowcount > 0:
                c.execute(
                    "DELETE FROM alert_rule_fires WHERE rule_id=?" + frag,  # noqa: S608
                    (rule_id, *params),
                )
        return cur.rowcount > 0

    def touch_alert_rule_fired(self, rule_id: str, fired_at_iso: str) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE alert_rules SET last_fired_at=? WHERE id=?",
                (fired_at_iso, rule_id),
            )

    # ---- alert_rule_fires ----
    def find_alert_fire(self, rule_id: str, event_fingerprint: str) -> dict[str, Any] | None:
        """(rule_id, event_fingerprint) 기존 발사 — 멱등 재평가 dedup(FR-005)."""
        row = self._conn.execute(
            "SELECT * FROM alert_rule_fires WHERE rule_id=? AND event_fingerprint=?"
            " ORDER BY created_at DESC LIMIT 1",
            (rule_id, event_fingerprint),
        ).fetchone()
        return _fire_dict(row) if row else None

    def record_alert_fire(
        self,
        *,
        rule_id: str,
        event_fingerprint: str,
        status: str,
        channels: list[dict[str, Any]] | None = None,
        org_id: str | None = None,
    ) -> str:
        """발사 이력 기록. channels는 [{channel, status}] 결과 목록."""
        fid = uuid.uuid4().hex[:12]
        oid = orgctx.org_for_write(org_id)
        clean = _clean_channels(channels)
        with self.tx() as c:
            c.execute(
                "INSERT INTO alert_rule_fires(id,org_id,rule_id,event_fingerprint,"
                "status,channels_json,created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    fid,
                    oid,
                    rule_id,
                    event_fingerprint[:300],
                    status[:40],
                    json.dumps(clean, ensure_ascii=False),
                    _iso(_now()),
                ),
            )
        return fid

    def list_alert_fires(
        self, rule_id: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        frag, params = _org_filter()
        sql = "SELECT * FROM alert_rule_fires"
        conds: list[str] = []
        qparams: list[Any] = []
        if rule_id:
            conds.append("rule_id=?")
            qparams.append(rule_id)
        if frag:
            conds.append(frag.strip().removeprefix("AND").strip())
            qparams.extend(params)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY created_at DESC LIMIT ?"
        qparams.append(_clamp_limit(limit, 100, 500))
        return [_fire_dict(r) for r in self._conn.execute(sql, qparams).fetchall()]

    # ---- atomic claim (dedup/cooldown check-then-act 경합 수정) ----
    def claim_alert_fire(
        self,
        *,
        rule_id: str,
        event_fingerprint: str,
        cooldown_minutes: int,
        org_id: str | None = None,
        now: datetime | None = None,
    ) -> str | None:
        """발송 권리를 원자적으로 claim한다 — 성공 시 fire id, 실패 시 None.

        단일 tx 안에서 처리해 check-then-act 경합(동시 emit으로 인한
        중복 채널 발송)을 제거한다:

        1. alert_rule_fires 90일 초과분 prune (무한 증가 방지).
        2. alert_rules 행 UPDATE로 규칙 단위 락 + 쿨다운 판정 —
           last_fired_at이 cutoff 이내면 rowcount=0 → claim 실패.
           이 UPDATE가 규칙 행 락을 잡으므로 같은 규칙의 동시 claim은
           커밋 후 순차화된다(PG read-committed에서도 성립).
        3. status='dispatching' fire 행 INSERT — 같은
           (rule_id, event_fingerprint)의 최근 fire가 있으면
           WHERE NOT EXISTS가 rowcount=0 → claim 실패.
           dedup 윈도우는 cooldown과 동일하다:
           - cooldown>0: 윈도우(cooldown 분) 경과 후 같은 fingerprint도
             재발화 가능 — 지속 조건의 주기적 재알림. 규칙 레벨 쿨다운
             (last_fired_at)은 cooldown 분 내 어떤 발송도 막으므로
             같은 fingerprint가 cooldown 안에 연속 발화하지는 않는다.
           - cooldown=0: fp_cutoff가 _FAR_PAST라 같은 fingerprint의
             기존 fire(보존 90일 내)가 항상 존재 → 사실상 영구 dedup.
             다른 fingerprint는 last_fired_at 차단 없이 즉시 발화한다.
             주의: 주기 평가 fingerprint는 시간 버킷
             (periodic:{rid}:{field}:{YYYYMMDDHH})을 포함하므로
             cooldown=0 주기 규칙도 조건이 지속되는 한 시간마다
             새 fingerprint로 재발화한다 — 의도된 동작(시간당 1회
             상한 알림).
        4. claim 성공 시 last_fired_at을 같은 tx에서 갱신 — 다른
           fingerprint의 동시 이벤트도 쿨다운에 걸린다.

        claim 실패(3번) 시 last_fired_at을 원래 값으로 복원해 dedup
        조회가 쿨다운을 연장하지 않게 한다."""
        now_dt = now if isinstance(now, datetime) else _now()
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=UTC)
        now_iso = _iso(now_dt) or ""
        try:
            cooldown = max(0, int(cooldown_minutes or 0))
        except (TypeError, ValueError):
            cooldown = 0
        # cooldown=0은 _FAR_PAST — 어떤 과거 fire도 `created_at > cutoff`를
        # 만족시켜 same-fingerprint dedup이 영구 적용된다(_FAR_FUTURE면
        # 조건이 항상 거짓이라 dedup이 무력화돼 반복 발송 스팸이 된다).
        fp_cutoff = _iso(now_dt - timedelta(minutes=cooldown)) if cooldown > 0 else _FAR_PAST_ISO
        rule_cutoff = _iso(now_dt - timedelta(minutes=cooldown)) if cooldown > 0 else _FAR_PAST_ISO
        fid = uuid.uuid4().hex[:12]
        oid = orgctx.org_for_write(org_id)
        fp = str(event_fingerprint or "")[:300]
        with self.tx() as c:
            # fires 보존 — claim 경로에서 함께 정리한다.
            c.execute(
                "DELETE FROM alert_rule_fires WHERE created_at < ?",
                (_iso(now_dt - timedelta(days=_FIRE_RETENTION_DAYS)),),
            )
            prev = c.execute(
                "SELECT last_fired_at FROM alert_rules WHERE id=?", (rule_id,)
            ).fetchone()
            if prev is None:
                return None
            prev_lfa = prev["last_fired_at"]
            if cooldown > 0:
                cur = c.execute(
                    "UPDATE alert_rules SET last_fired_at=? WHERE id=?"
                    " AND (last_fired_at IS NULL OR last_fired_at <= ?)",
                    (now_iso, rule_id, rule_cutoff),
                )
                if cur.rowcount == 0:
                    return None
            else:
                # 쿨다운 없음 — 행 락 + last_fired_at 최신화만 한다.
                c.execute(
                    "UPDATE alert_rules SET last_fired_at=? WHERE id=?",
                    (now_iso, rule_id),
                )
            cur = c.execute(
                "INSERT INTO alert_rule_fires(id,org_id,rule_id,event_fingerprint,"
                "status,channels_json,created_at)"
                " SELECT ?,?,?,?,?,?,?"
                " WHERE NOT EXISTS("
                "   SELECT 1 FROM alert_rule_fires"
                "   WHERE rule_id=? AND event_fingerprint=? AND created_at > ?)",
                (
                    fid,
                    oid,
                    rule_id,
                    fp,
                    FIRE_DISPATCHING,
                    "[]",
                    now_iso,
                    rule_id,
                    fp,
                    fp_cutoff,
                ),
            )
            if cur.rowcount == 0:
                # same-fingerprint dedup — last_fired_at 갱신을 되돌린다.
                c.execute(
                    "UPDATE alert_rules SET last_fired_at=? WHERE id=?",
                    (prev_lfa, rule_id),
                )
                return None
        return fid

    def complete_alert_fire(
        self,
        fire_id: str,
        *,
        status: str,
        channels: list[dict[str, Any]] | None = None,
    ) -> None:
        """claim된 fire를 채널 발송 결과로 마감 — status/channels_json 갱신."""
        clean = _clean_channels(channels)
        with self.tx() as c:
            c.execute(
                "UPDATE alert_rule_fires SET status=?, channels_json=? WHERE id=?",
                (status[:40], json.dumps(clean, ensure_ascii=False), fire_id),
            )

    def record_suppressed_fire(
        self,
        *,
        rule_id: str,
        event_fingerprint: str,
        org_id: str | None = None,
    ) -> str | None:
        """쿨다운 억제 기록 — (rule_id, fingerprint)당 1행만 남긴다.

        동시 emit 경합으로 중복 suppressed 행이 생기지 않게
        WHERE NOT EXISTS 가드를 둔다. 이미 fire 행이 있으면 None."""
        fid = uuid.uuid4().hex[:12]
        oid = orgctx.org_for_write(org_id)
        fp = str(event_fingerprint or "")[:300]
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO alert_rule_fires(id,org_id,rule_id,event_fingerprint,"
                "status,channels_json,created_at)"
                " SELECT ?,?,?,?,?,?,?"
                " WHERE NOT EXISTS("
                "   SELECT 1 FROM alert_rule_fires"
                "   WHERE rule_id=? AND event_fingerprint=?)",
                (
                    fid,
                    oid,
                    rule_id,
                    fp,
                    FIRE_SUPPRESSED_COOLDOWN,
                    "[]",
                    _iso(_now()),
                    rule_id,
                    fp,
                ),
            )
        return fid if cur.rowcount > 0 else None

    def prune_alert_fires(self, older_than_days: int = _FIRE_RETENTION_DAYS) -> int:
        """보존 기간이 지난 fire 행 삭제 — claim tx에서 자동 호출되며
        수동 정리용으로도 노출한다. 삭제 행 수 반환."""
        try:
            days = max(1, int(older_than_days))
        except (TypeError, ValueError):
            days = _FIRE_RETENTION_DAYS
        cutoff = _iso(_now() - timedelta(days=days))
        with self.tx() as c:
            cur = c.execute("DELETE FROM alert_rule_fires WHERE created_at < ?", (cutoff,))
        return int(cur.rowcount or 0)
