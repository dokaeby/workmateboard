# mypy: disable-error-code="attr-defined"
"""Store reporting 확장 믹스인 — spec 008 US2/US3.

- eval_schedules: 온라인 평가 스케줄(조직 리소스). run/stage terminal 시
  sample_rate로 기존 eval 파이프라인을 트리거한다.
- report_subscriptions: daily/weekly 리포트의 email/slack/webhook 전달 구독.
  webhook 채널의 HMAC 서명 시크릿은 secret_enc(Fernet)에만 저장하고
  API 응답에는 절대 노출하지 않는다.
- eval_runs의 source/schedule_id 컬럼은 _ensure_columns 마이그레이션으로
  보강된다(migrations._migrate_reporting_ext_columns).

evals.py의 _org_filter/org_for_write 패턴과 동일 — 모든 읽기는 orgctx
필터, 쓰기는 org_for_write로 org_id를 찍는다.
"""

from __future__ import annotations

import uuid
from typing import Any

from . import orgctx
from .util import _clamp_limit, _iso, _now

# eval schedule target 어휘 — services/online_evals.py도 같은 상수를 쓴다.
EVAL_SCHEDULE_TARGETS = ("run_completed", "stage_completed")
# report subscription 어휘 — services/reporting.py도 같은 상수를 쓴다.
REPORT_SUB_KINDS = ("daily", "weekly")
REPORT_SUB_CHANNELS = ("email", "slack", "webhook")


def _org_filter() -> tuple[str, list[Any]]:
    """ctx 조직이 있으면 ` AND org_id=?` 조건 — 시스템/미인증(ctx='')은 무필터."""
    oid = orgctx.current_org_id()
    return (" AND org_id=?", [oid]) if oid else ("", [])


def _schedule_dict(r: Any) -> dict[str, Any]:
    d = dict(r)
    try:
        d["sample_rate"] = float(d.get("sample_rate") or 0.0)
    except (TypeError, ValueError):
        d["sample_rate"] = 0.0
    d["enabled"] = bool(d.get("enabled"))
    return d


def _report_sub_dict(r: Any) -> dict[str, Any]:
    d = dict(r)
    d["enabled"] = bool(d.get("enabled"))
    # secret_enc는 내부 필드 — dict에는 남기되 라우터 응답에서 제외한다.
    return d


class ReportingExtStoreMixin:
    # ---- eval_schedules (spec 008 US2) ----
    def create_eval_schedule(
        self,
        *,
        name: str,
        target: str,
        eval_name: str,
        workflow_key: str = "",
        sample_rate: float = 1.0,
        enabled: bool = True,
        created_by: str = "",
    ) -> dict[str, Any]:
        sid = uuid.uuid4().hex[:12]
        now = _iso(_now())
        with self.tx() as c:
            c.execute(
                "INSERT INTO eval_schedules"
                "(id,org_id,name,target,workflow_key,sample_rate,eval_name,"
                "enabled,created_by,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    sid,
                    orgctx.org_for_write(),
                    name[:200],
                    target[:30],
                    workflow_key[:120],
                    max(0.0, min(1.0, float(sample_rate))),
                    eval_name[:200],
                    1 if enabled else 0,
                    (created_by or "")[:200],
                    now,
                    now,
                ),
            )
        row = self.get_eval_schedule(sid)
        return row or {}

    def list_eval_schedules(self, *, enabled_only: bool = False) -> list[dict[str, Any]]:
        frag, params = _org_filter()
        cond = frag.replace(" AND ", " WHERE ", 1) if frag else ""
        if enabled_only:
            cond += (" AND " if cond else " WHERE ") + "enabled=1"
        rows = self._conn.execute(
            "SELECT * FROM eval_schedules"  # noqa: S608 - cond는 _org_filter 고정 절
            + cond
            + " ORDER BY created_at DESC LIMIT 500",
            params,
        ).fetchall()
        return [_schedule_dict(r) for r in rows]

    def get_eval_schedule(self, schedule_id: str) -> dict[str, Any] | None:
        frag, params = _org_filter()
        row = self._conn.execute(
            "SELECT * FROM eval_schedules WHERE id=?" + frag,  # noqa: S608
            (schedule_id, *params),
        ).fetchone()
        return _schedule_dict(row) if row else None

    def update_eval_schedule(self, schedule_id: str, **fields: Any) -> dict[str, Any] | None:
        """부분 갱신 — name/target/workflow_key/sample_rate/eval_name/enabled.

        None 반환 = 스케줄 없음 또는 검증 실패(빈 name/eval_name, 범위 밖
        sample_rate, 알 수 없는 target)."""
        sched = self.get_eval_schedule(schedule_id)
        if sched is None:
            return None
        updates: dict[str, Any] = {}
        if "name" in fields:
            name = str(fields["name"] or "").strip()[:200]
            if not name:
                return None
            updates["name"] = name
        if "target" in fields:
            target = str(fields["target"] or "").strip().lower()[:30]
            if target not in EVAL_SCHEDULE_TARGETS:
                return None
            updates["target"] = target
        if "workflow_key" in fields:
            updates["workflow_key"] = str(fields["workflow_key"] or "").strip()[:120]
        if "sample_rate" in fields:
            try:
                rate = float(fields["sample_rate"])
            except (TypeError, ValueError):
                return None
            if not (0.0 <= rate <= 1.0):
                return None
            updates["sample_rate"] = rate
        if "eval_name" in fields:
            eval_name = str(fields["eval_name"] or "").strip()[:200]
            if not eval_name:
                return None
            updates["eval_name"] = eval_name
        if "enabled" in fields:
            updates["enabled"] = 1 if fields["enabled"] else 0
        if not updates:
            return sched
        sets = ", ".join(f"{k}=?" for k in updates)
        frag, params = _org_filter()
        with self.tx() as c:
            c.execute(
                f"UPDATE eval_schedules SET {sets}, updated_at=?"  # noqa: S608 - 고정 허용 목록
                " WHERE id=?" + frag,
                (*updates.values(), _iso(_now()), schedule_id, *params),
            )
        return self.get_eval_schedule(schedule_id)

    def delete_eval_schedule(self, schedule_id: str) -> bool:
        """하드 삭제 — 생성된 eval_runs 행은 schedule_id를 보존한다."""
        frag, params = _org_filter()
        with self.tx() as c:
            cur = c.execute(
                "DELETE FROM eval_schedules WHERE id=?" + frag,  # noqa: S608
                (schedule_id, *params),
            )
        return cur.rowcount > 0

    # ---- eval_runs 온라인 실행 (spec 008 US2) ----
    def eval_run_exists(self, schedule_id: str, run_id: str) -> bool:
        """(schedule_id, run_id) dedupe — 동일 스케줄의 동일 run 재채점 방지."""
        frag, params = _org_filter()
        row = self._conn.execute(
            "SELECT 1 AS x FROM eval_runs WHERE schedule_id=? AND run_id=?"  # noqa: S608
            + frag
            + " LIMIT 1",
            (schedule_id, run_id, *params),
        ).fetchone()
        return row is not None

    def get_online_eval_run(self, schedule_id: str, run_id: str) -> dict[str, Any] | None:
        """(schedule_id, run_id)의 온라인 eval_run — dedupe 충돌 시 기존 행 조회용.

        org 필터 없는 전역 조회 — 인덱스 충돌 경로에서 orgctx와 무관하게
        동일 키의 기존 행을 찾아야 한다."""
        row = self._conn.execute(
            "SELECT * FROM eval_runs WHERE schedule_id=? AND run_id=?"
            " ORDER BY created_at, id LIMIT 1",
            (schedule_id[:64], run_id),
        ).fetchone()
        return self._eval_run_json(row) if row else None

    def create_online_eval_run(self, eval_id: str, run_id: str, schedule_id: str) -> dict[str, Any]:
        """온라인 평가 eval_run 생성 — source='online', schedule_id 기록.

        org_id는 부모 eval의 org를 상속한다(evals.create_eval_run과 동일
        규칙 — 스케줄러/내부 호출 경로에서도 올바른 조직에 귀속).
        (schedule_id, run_id) UNIQUE 부분 인덱스가 최후 보루 — check-then-act
        경합으로 인한 중복 insert는 integrity error를 잡아 기존 행을
        반환한다(멱등 dedupe)."""
        rid = uuid.uuid4().hex[:12]
        now = _iso(_now())
        oid = self._eval_run_parent_org(eval_id)
        try:
            with self.tx() as c:
                c.execute(
                    "INSERT INTO eval_runs(id,eval_id,run_id,status,results,created_at,"
                    "org_id,source,schedule_id) VALUES(?,?,?,'pending','[]',?,?,?,?)",
                    (rid, eval_id, run_id, now, oid, "online", schedule_id[:64]),
                )
        except self._integrity_errors:
            existing = self.get_online_eval_run(schedule_id, run_id)
            if existing is not None:
                return existing
            raise
        return self.get_eval_run(rid) or {}

    def list_eval_runs_for_schedule(
        self, schedule_id: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        """스케줄이 만든 온라인 eval_run 목록 — 최신순(history 엔드포인트용)."""
        limit = _clamp_limit(limit, 50, 200)
        frag, params = _org_filter()
        rows = self._conn.execute(
            "SELECT * FROM eval_runs WHERE schedule_id=?"  # noqa: S608 - frag는 _org_filter 고정 절
            + frag
            + " ORDER BY created_at DESC, id DESC LIMIT ?",
            (schedule_id, *params, limit),
        ).fetchall()
        return [self._eval_run_json(r) for r in rows]

    # ---- report_subscriptions (spec 008 US3) ----
    def create_report_subscription(
        self,
        *,
        kind: str,
        channel: str,
        target: str,
        enabled: bool = True,
        created_by: str = "",
        secret_enc: str = "",
    ) -> dict[str, Any]:
        sid = uuid.uuid4().hex[:12]
        now = _iso(_now())
        with self.tx() as c:
            c.execute(
                "INSERT INTO report_subscriptions"
                "(id,org_id,kind,channel,target,enabled,last_sent_at,"
                "created_by,created_at,updated_at,secret_enc)"
                " VALUES(?,?,?,?,?,?,NULL,?,?,?,?)",
                (
                    sid,
                    orgctx.org_for_write(),
                    kind[:20],
                    channel[:20],
                    target[:2000],
                    1 if enabled else 0,
                    (created_by or "")[:200],
                    now,
                    now,
                    secret_enc,
                ),
            )
        row = self.get_report_subscription(sid)
        return row or {}

    def list_report_subscriptions(self, *, enabled_only: bool = False) -> list[dict[str, Any]]:
        frag, params = _org_filter()
        cond = frag.replace(" AND ", " WHERE ", 1) if frag else ""
        if enabled_only:
            cond += (" AND " if cond else " WHERE ") + "enabled=1"
        rows = self._conn.execute(
            "SELECT * FROM report_subscriptions"  # noqa: S608 - cond는 _org_filter 고정 절
            + cond
            + " ORDER BY created_at DESC LIMIT 500",
            params,
        ).fetchall()
        return [_report_sub_dict(r) for r in rows]

    def get_report_subscription(self, sub_id: str) -> dict[str, Any] | None:
        frag, params = _org_filter()
        row = self._conn.execute(
            "SELECT * FROM report_subscriptions WHERE id=?" + frag,  # noqa: S608
            (sub_id, *params),
        ).fetchone()
        return _report_sub_dict(row) if row else None

    def update_report_subscription(self, sub_id: str, **fields: Any) -> dict[str, Any] | None:
        """부분 갱신 — kind/channel/target/enabled/secret_enc.

        None 반환 = 구독 없음 또는 검증 실패(빈 target, 알 수 없는
        kind/channel)."""
        sub = self.get_report_subscription(sub_id)
        if sub is None:
            return None
        updates: dict[str, Any] = {}
        if "kind" in fields:
            kind = str(fields["kind"] or "").strip().lower()[:20]
            if kind not in REPORT_SUB_KINDS:
                return None
            updates["kind"] = kind
        if "channel" in fields:
            channel = str(fields["channel"] or "").strip().lower()[:20]
            if channel not in REPORT_SUB_CHANNELS:
                return None
            updates["channel"] = channel
        if "target" in fields:
            target = str(fields["target"] or "").strip()[:2000]
            if not target:
                return None
            updates["target"] = target
        if "enabled" in fields:
            updates["enabled"] = 1 if fields["enabled"] else 0
        if "secret_enc" in fields:
            updates["secret_enc"] = str(fields["secret_enc"] or "")
        if not updates:
            return sub
        sets = ", ".join(f"{k}=?" for k in updates)
        frag, params = _org_filter()
        with self.tx() as c:
            c.execute(
                f"UPDATE report_subscriptions SET {sets}, updated_at=?"  # noqa: S608 - 고정 허용 목록
                " WHERE id=?" + frag,
                (*updates.values(), _iso(_now()), sub_id, *params),
            )
        return self.get_report_subscription(sub_id)

    def delete_report_subscription(self, sub_id: str) -> bool:
        frag, params = _org_filter()
        with self.tx() as c:
            cur = c.execute(
                "DELETE FROM report_subscriptions WHERE id=?" + frag,  # noqa: S608
                (sub_id, *params),
            )
        return cur.rowcount > 0

    def list_report_subscription_org_ids(self, kind: str) -> list[str]:
        """kind의 enabled 구독이 1건 이상 있는 org_id 목록 — org 필터 없는
        전역 조회. 리포트 자동 발송(jobs._maybe_generate_reports)이 조직별
        순회 발송할 때 사용한다. org_id='' 레거시 행은 제외 — 무필터 ctx에서
        발송하면 다른 조직 구독까지 섞여 나간다."""
        rows = self._conn.execute(
            "SELECT DISTINCT org_id FROM report_subscriptions"
            " WHERE kind=? AND enabled=1 AND org_id != ''",
            (kind,),
        ).fetchall()
        return sorted({str(r["org_id"]) for r in rows if str(r["org_id"] or "")})

    def touch_report_subscription_sent(self, sub_id: str) -> None:
        """발송 성공 시 last_sent_at 갱신 — 실패 시 호출하지 않는다."""
        frag, params = _org_filter()
        with self.tx() as c:
            c.execute(
                "UPDATE report_subscriptions SET last_sent_at=? WHERE id=?"  # noqa: S608
                + frag,
                (_iso(_now()), sub_id, *params),
            )
