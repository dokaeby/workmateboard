# mypy: disable-error-code="attr-defined"
"""Eval/eval_runs 스토어 메서드 믹스인 (db.py Store에서 추출).

Paperclip pillar-3 포팅 — 저장된 입력 픽스처 + 결정적 체크를 일반 run
완료 후 채점한다. evals는 name이 lower() 기준 UNIQUE(대소문자 무시),
eval_runs는 run 1건당 1행의 채점 결과를 보관한다.

input/checks는 JSON TEXT로 저장하고 조회 시 파싱해 돌려준다 —
API 계약상 object/array로 노출된다.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from . import orgctx
from .util import _clamp_limit, _iso, _now


def _org_filter() -> tuple[str, list[Any]]:
    """ctx 조직이 있으면 ` AND org_id=?` 조건 — 시스템/미인증(ctx='')은 무필터."""
    oid = orgctx.current_org_id()
    return (" AND org_id=?", [oid]) if oid else ("", [])


class EvalStoreMixin:
    # ---- evals (평가 픽스처) ----
    @staticmethod
    def _eval_json(r: Any) -> dict[str, Any]:
        d = dict(r)
        try:
            inp = json.loads(d.get("input") or "{}")
            d["input"] = inp if isinstance(inp, dict) else {}
        except (ValueError, TypeError):
            d["input"] = {}
        try:
            checks = json.loads(d.get("checks") or "[]")
            d["checks"] = (
                [c for c in checks if isinstance(c, dict)] if isinstance(checks, list) else []
            )
        except (ValueError, TypeError):
            d["checks"] = []
        if not d.get("status"):
            d["status"] = "active"
        return d

    @staticmethod
    def _eval_run_json(r: Any) -> dict[str, Any]:
        d = dict(r)
        try:
            res = json.loads(d.get("results") or "[]")
            d["results"] = res if isinstance(res, list) else []
        except (ValueError, TypeError):
            d["results"] = []
        try:
            d["score"] = float(d["score"]) if d.get("score") is not None else None
        except (TypeError, ValueError):
            d["score"] = None
        if not d.get("status"):
            d["status"] = "pending"
        if d.get("error") is None:
            d["error"] = ""
        return d

    def create_eval(
        self,
        name: str,
        workflow_key: str,
        input_obj: dict[str, Any],
        checks: list[dict[str, Any]],
        *,
        status: str = "active",
        created_by: str = "",
        org_id: str | None = None,
    ) -> dict[str, Any] | None:
        """eval 생성. name은 trim 후 lower() 기준 중복 시 None(integrity 포함)."""
        name = name.strip()[:200]
        workflow_key = workflow_key.strip()[:120]
        if not name or not workflow_key:
            return None
        if self.find_eval_by_name(name) is not None:
            return None
        eid = uuid.uuid4().hex[:12]
        now = _iso(_now())
        try:
            with self.tx() as c:
                c.execute(
                    "INSERT INTO evals(id,name,workflow_key,input,checks,status,"
                    "created_by,created_at,updated_at,org_id)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        eid,
                        name,
                        workflow_key,
                        json.dumps(input_obj or {}, ensure_ascii=False, default=str),
                        json.dumps(checks or [], ensure_ascii=False, default=str),
                        status.strip().lower()[:20] or "active",
                        created_by[:200],
                        now,
                        now,
                        orgctx.org_for_write(org_id),
                    ),
                )
        except self._integrity_errors:
            return None
        return self.get_eval(eid)

    def get_eval(self, eval_id: str) -> dict[str, Any] | None:
        frag, params = _org_filter()
        row = self._conn.execute(
            "SELECT * FROM evals WHERE id=?" + frag,  # noqa: S608
            (eval_id, *params),
        ).fetchone()
        return self._eval_json(row) if row else None

    def find_eval_by_name(self, name: str) -> dict[str, Any] | None:
        """eval 이름 조회 — trim + case-insensitive (idx_evals_name_lower와 동일 규칙)."""
        frag, params = _org_filter()
        row = self._conn.execute(
            "SELECT * FROM evals WHERE lower(name)=lower(?)" + frag,  # noqa: S608
            (name.strip()[:200], *params),
        ).fetchone()
        return self._eval_json(row) if row else None

    def list_evals(self, status: str | None = None) -> list[dict[str, Any]]:
        frag, fparams = _org_filter()
        sql = "SELECT * FROM evals"
        conds: list[str] = []
        params: list[Any] = []
        if frag:
            conds.append(frag.strip().removeprefix("AND").strip())
            params.extend(fparams)
        if status:
            conds.append("status=?")
            params.append(status.strip().lower()[:20])
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY lower(name), name"
        return [self._eval_json(r) for r in self._conn.execute(sql, params).fetchall()]

    def update_eval(self, eval_id: str, **fields: Any) -> dict[str, Any] | None:
        """부분 갱신 — name/workflow_key/input/checks/status.

        input/checks는 파이썬 객체(dict/list)를 받아 JSON으로 저장한다.
        None 반환 = eval 없음 또는 name 중복."""
        ev = self.get_eval(eval_id)
        if ev is None:
            return None
        updates: dict[str, Any] = {}
        if "name" in fields:
            new_name = str(fields["name"] or "").strip()[:200]
            if not new_name:
                return None
            other = self.find_eval_by_name(new_name)
            if other is not None and other["id"] != eval_id:
                return None
            updates["name"] = new_name
        if "workflow_key" in fields:
            wk = str(fields["workflow_key"] or "").strip()[:120]
            if not wk:
                return None
            updates["workflow_key"] = wk
        if "input" in fields:
            inp = fields["input"]
            if not isinstance(inp, dict):
                return None
            updates["input"] = json.dumps(inp, ensure_ascii=False, default=str)
        if "checks" in fields:
            chk = fields["checks"]
            if not isinstance(chk, list):
                return None
            updates["checks"] = json.dumps(chk, ensure_ascii=False, default=str)
        if "status" in fields:
            st = str(fields["status"] or "").strip().lower()[:20]
            if st:
                updates["status"] = st
        if not updates:
            return ev
        sets = ", ".join(f"{k}=?" for k in updates)
        frag, fparams = _org_filter()
        try:
            with self.tx() as c:
                c.execute(
                    f"UPDATE evals SET {sets}, updated_at=? WHERE id=?" + frag,  # noqa: S608 - keys는 고정 허용 목록
                    (*updates.values(), _iso(_now()), eval_id, *fparams),
                )
        except self._integrity_errors:
            return None
        return self.get_eval(eval_id)

    def archive_eval(self, eval_id: str) -> bool:
        """소프트 삭제 — status='archived'. eval_runs 이력은 보존."""
        frag, params = _org_filter()
        with self.tx() as c:
            cur = c.execute(
                "UPDATE evals SET status='archived', updated_at=? WHERE id=?" + frag,  # noqa: S608
                (_iso(_now()), eval_id, *params),
            )
        return cur.rowcount > 0

    # ---- eval_runs (eval 실행/채점 결과) ----
    def _eval_run_parent_org(self, eval_id: str) -> str:
        """eval_run의 org — 부모 evals 행에서 상속(costs._cost_parent_org 패턴).

        eval_runs는 파생 테이블이라 org_id를 비정규화해 둔다 — 조회 필터가
        evals 조인 없이 eval_runs.org_id만으로 조직을 판별할 수 있게 하기 위함
        (spec 003 US3). 부모가 없거나 org_id=''면 ctx로 폴백한다."""
        row = self._conn.execute("SELECT org_id FROM evals WHERE id=?", (eval_id,)).fetchone()
        if row is not None and str(row["org_id"] or ""):
            return str(row["org_id"])
        return orgctx.org_for_write()

    def create_eval_run(self, eval_id: str, run_id: str) -> dict[str, Any]:
        """eval_run 행 생성 — status='pending', 채점은 lazy.

        org_id는 부모 eval의 org를 상속한다 — run 생성 경로가 ctx를 잃어도
        (스케줄러/내부 호출) eval_run이 올바른 조직에 귀속된다."""
        rid = uuid.uuid4().hex[:12]
        now = _iso(_now())
        oid = self._eval_run_parent_org(eval_id)
        with self.tx() as c:
            c.execute(
                "INSERT INTO eval_runs(id,eval_id,run_id,status,results,created_at,org_id)"
                " VALUES(?,?,?,'pending','[]',?,?)",
                (rid, eval_id, run_id, now, oid),
            )
        return self.get_eval_run(rid) or {}

    def get_eval_run(self, eval_run_id: str) -> dict[str, Any] | None:
        frag, params = _org_filter()
        row = self._conn.execute(
            "SELECT * FROM eval_runs WHERE id=?" + frag,  # noqa: S608
            (eval_run_id, *params),
        ).fetchone()
        return self._eval_run_json(row) if row else None

    def list_eval_runs(self, eval_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """eval의 eval_run 목록 — 최신순."""
        limit = _clamp_limit(limit, 50, 200)
        frag, params = _org_filter()
        sql = (
            "SELECT * FROM eval_runs WHERE eval_id=?"  # noqa: S608 - frag는 _org_filter 고정 절
            + frag
            + " ORDER BY created_at DESC, id DESC LIMIT ?"
        )
        rows = self._conn.execute(sql, (eval_id, *params, limit)).fetchall()
        return [self._eval_run_json(r) for r in rows]

    def mark_eval_run_scored(
        self,
        eval_run_id: str,
        *,
        score: float | None,
        results: list[dict[str, Any]],
        error: str = "",
    ) -> bool:
        """채점 완료 기록 — status='scored', scored_at=now.

        멱등: `WHERE status='pending'` 조건부 UPDATE라 이미 채점된 행은
        덮어쓰지 않는다(두 번째 호출은 rowcount=0)."""
        frag, params = _org_filter()
        sql = (
            "UPDATE eval_runs SET status='scored', score=?, results=?, error=?,"
            " scored_at=? WHERE id=? AND status='pending'"
        )
        sql += frag  # noqa: S608 - frag는 _org_filter의 고정 절
        with self.tx() as c:
            cur = c.execute(
                sql,
                (
                    score,
                    json.dumps(results or [], ensure_ascii=False, default=str),
                    (error or "")[:2000],
                    _iso(_now()),
                    eval_run_id,
                    *params,
                ),
            )
        return cur.rowcount > 0

    def mark_eval_run_error(self, eval_run_id: str, error: str) -> bool:
        """채점 자체 실패 — status='error'. 재시도하지 않는다(결정적 채점)."""
        frag, params = _org_filter()
        sql = "UPDATE eval_runs SET status='error', error=? WHERE id=? AND status='pending'"
        sql += frag  # noqa: S608 - frag는 _org_filter의 고정 절
        with self.tx() as c:
            cur = c.execute(sql, ((error or "")[:2000], eval_run_id, *params))
        return cur.rowcount > 0

    def latest_eval_runs(self, limit: int = 1000) -> dict[str, dict[str, Any]]:
        """eval_id → {"created_at": 최신 run 시각, "score": 최신 채점 점수}.

        목록 집계(last_score/last_run_at)용. created_at DESC 순으로 훑어
        eval별 첫 행(=최신)의 created_at과, score가 null이 아닌 첫 행(=
        최신 채점)의 score를 취한다 — 방금 트리거한 pending run이 최신이어도
        이전 채점 점수가 "점수 없음"으로 덮이지 않는다. limit은 스캔 상한 —
        그 이상 오래된 eval은 집계가 빠질 수 있으나 목록 규모상 충분하다.

        조직 스코프(spec 003 US3): eval_runs.org_id는 생성 시 부모 eval의
        org를 상속한 비정규화 값이라 evals 조인 없이 직접 필터해도 각 eval의
        조직과 동일하게 스코프된다 — ctx 조직의 run만 집계에 포함된다."""
        frag, params = _org_filter()
        sql = "SELECT * FROM eval_runs"
        if frag:
            sql += " WHERE " + frag.strip().removeprefix("AND").strip()  # noqa: S608
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        rows = self._conn.execute(sql, (*params, _clamp_limit(limit, 1000, 5000))).fetchall()
        out: dict[str, dict[str, Any]] = {}
        for r in rows:
            d = self._eval_run_json(r)
            eid = str(d.get("eval_id") or "")
            if not eid:
                continue
            slot = out.setdefault(eid, {"created_at": None, "score": None})
            if slot["created_at"] is None:
                slot["created_at"] = d.get("created_at")
            if slot["score"] is None and d.get("score") is not None:
                slot["score"] = d.get("score")
        return out
