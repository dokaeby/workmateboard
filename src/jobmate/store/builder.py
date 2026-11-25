# mypy: disable-error-code="attr-defined"
"""Builder/store mixin methods extracted verbatim from store/db.py."""

from __future__ import annotations

import json
import uuid
from typing import Any

from .util import _iso, _loads, _now


class BuilderStoreMixin:
    # ---- playbooks (원클릭 작업 템플릿) ----
    @staticmethod
    def _playbook_json(r: Any) -> dict[str, Any]:
        d = dict(r)
        d["input"] = _loads(d.get("input") or "{}")
        return d

    def create_playbook(
        self, name: str, workflow: str, input_data: dict[str, Any], created_by: str = ""
    ) -> dict[str, Any]:
        pid = uuid.uuid4().hex[:12]
        now = _iso(_now())
        with self.tx() as c:
            c.execute(
                "INSERT INTO playbooks(id,name,workflow,input,created_by,created_at)"
                " VALUES(?,?,?,?,?,?)",
                (pid, name, workflow, json.dumps(input_data or {}), created_by, now),
            )
        pb = self.get_playbook(pid)
        return pb if pb is not None else {"id": pid}

    def list_playbooks(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM playbooks ORDER BY created_at DESC").fetchall()
        return [self._playbook_json(r) for r in rows]

    def get_playbook(self, playbook_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM playbooks WHERE id=?", (playbook_id,)).fetchone()
        return self._playbook_json(row) if row else None

    def delete_playbook(self, playbook_id: str) -> bool:
        with self.tx() as c:
            cur = c.execute("DELETE FROM playbooks WHERE id=?", (playbook_id,))
        return cur.rowcount > 0

    # ---- run_comments (run/diff 토론) ----
    def add_run_comment(self, run_id: str, user_id: str, email: str, body: str) -> dict[str, Any]:
        cid = uuid.uuid4().hex[:12]
        now = _iso(_now())
        with self.tx() as c:
            c.execute(
                "INSERT INTO run_comments(id,run_id,user_id,email,body,created_at)"
                " VALUES(?,?,?,?,?,?)",
                (cid, run_id, user_id, email, body, now),
            )
        return {
            "id": cid,
            "run_id": run_id,
            "user_id": user_id,
            "email": email,
            "body": body,
            "created_at": now,
        }

    def list_run_comments(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT id,run_id,user_id,email,body,created_at FROM run_comments"
            " WHERE run_id=? ORDER BY created_at, id",
            (run_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- connectors (외부 데이터 소스) ----
    @staticmethod
    def _connector_json(r: Any, *, with_config: bool = False) -> dict[str, Any]:
        d = dict(r)
        d["enabled"] = bool(d.get("enabled"))
        d["has_token"] = bool(d.get("has_token"))
        if with_config:
            d["config"] = _loads(d.get("config") or "{}")
        else:
            d.pop("config", None)
        return d

    def create_connector(
        self,
        ctype: str,
        name: str,
        config: dict[str, Any],
        enabled: bool = True,
        has_token: bool = False,
    ) -> dict[str, Any]:
        cid = uuid.uuid4().hex[:12]
        now = _iso(_now())
        with self.tx() as c:
            c.execute(
                "INSERT INTO connectors(id,type,name,config,enabled,has_token,"
                "last_fetch_at,last_error,created_at) VALUES(?,?,?,?,?,?,NULL,'',?)",
                (
                    cid,
                    ctype,
                    name,
                    json.dumps(config or {}, ensure_ascii=False),
                    1 if enabled else 0,
                    int(has_token),
                    now,
                ),
            )
        conn = self.get_connector(cid)
        if conn is not None:
            conn.pop("config", None)  # 생성 응답에도 config(암호문 포함) 비노출
        return conn if conn is not None else {"id": cid}

    def list_connectors(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM connectors ORDER BY created_at DESC").fetchall()
        return [self._connector_json(r) for r in rows]

    def get_connector(self, connector_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM connectors WHERE id=?", (connector_id,)).fetchone()
        return self._connector_json(row, with_config=True) if row else None

    def delete_connector(self, connector_id: str) -> bool:
        with self.tx() as c:
            cur = c.execute("DELETE FROM connectors WHERE id=?", (connector_id,))
        return cur.rowcount > 0

    def set_connector_enabled(self, connector_id: str, enabled: bool) -> dict[str, Any] | None:
        conn = self.get_connector(connector_id)
        if conn is None:
            return None
        with self.tx() as c:
            c.execute(
                "UPDATE connectors SET enabled=? WHERE id=?",
                (1 if enabled else 0, connector_id),
            )
        return self.get_connector(connector_id)

    def mark_connector_fetch(self, connector_id: str, error: str = "") -> None:
        """fetch 시도 결과 기록 — last_fetch_at은 성공/실패 무관하게 갱신."""
        with self.tx() as c:
            c.execute(
                "UPDATE connectors SET last_fetch_at=?, last_error=? WHERE id=?",
                (_iso(_now()), error[:300], connector_id),
            )

    # ---- custom_workflows (비주얼 빌더 WorkflowSpec) ----
    def upsert_custom_workflow(self, name: str, spec: dict[str, Any]) -> dict[str, Any]:
        now = _iso(_now())
        with self.tx() as c:
            c.execute(
                "INSERT INTO custom_workflows(name,spec,created_at) VALUES(?,?,?)"
                " ON CONFLICT(name) DO UPDATE SET spec=excluded.spec",
                (name, json.dumps(spec, ensure_ascii=False), now),
            )
        return {"name": name, "spec": spec, "created_at": now}

    def list_custom_workflows(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT name,spec,created_at FROM custom_workflows ORDER BY name"
        ).fetchall()
        out = []
        for r in rows:
            spec = _loads(r["spec"])
            out.append({"name": r["name"], "spec": spec, "created_at": r["created_at"]})
        return out

    def get_custom_workflow(self, name: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT name,spec,created_at FROM custom_workflows WHERE name=?", (name,)
        ).fetchone()
        if not row:
            return None
        return {"name": row["name"], "spec": _loads(row["spec"]), "created_at": row["created_at"]}

    def delete_custom_workflow(self, name: str) -> bool:
        with self.tx() as c:
            cur = c.execute("DELETE FROM custom_workflows WHERE name=?", (name,))
        return cur.rowcount > 0

    # ---- triggers (세션 상태 전이 자동화) ----
    @staticmethod
    def _trigger_json(r: Any) -> dict[str, Any]:
        d = dict(r)
        d["enabled"] = bool(d.get("enabled"))
        d["match"] = _loads(d.get("match") or "{}")
        d["action"] = _loads(d.get("action") or "{}")
        return d

    def create_trigger(
        self, match: dict[str, Any], action: dict[str, Any], enabled: bool = True
    ) -> dict[str, Any]:
        tid = uuid.uuid4().hex[:12]
        now = _iso(_now())
        with self.tx() as c:
            c.execute(
                "INSERT INTO triggers(id,enabled,match,action,created_at,"
                "last_fired_at,last_error) VALUES(?,?,?,?,?,NULL,'')",
                (
                    tid,
                    1 if enabled else 0,
                    json.dumps(match or {}, ensure_ascii=False),
                    json.dumps(action or {}, ensure_ascii=False),
                    now,
                ),
            )
        trig = self.get_trigger(tid)
        return trig if trig is not None else {"id": tid}

    def list_triggers(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM triggers"
        if enabled_only:
            sql += " WHERE enabled=1"
        sql += " ORDER BY created_at DESC"
        rows = self._conn.execute(sql).fetchall()
        return [self._trigger_json(r) for r in rows]

    def get_trigger(self, trigger_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM triggers WHERE id=?", (trigger_id,)).fetchone()
        return self._trigger_json(row) if row else None

    def delete_trigger(self, trigger_id: str) -> bool:
        with self.tx() as c:
            cur = c.execute("DELETE FROM triggers WHERE id=?", (trigger_id,))
            c.execute("DELETE FROM trigger_fires WHERE trigger_id=?", (trigger_id,))
        return cur.rowcount > 0

    def set_trigger_enabled(self, trigger_id: str, enabled: bool) -> dict[str, Any] | None:
        trig = self.get_trigger(trigger_id)
        if trig is None:
            return None
        with self.tx() as c:
            c.execute(
                "UPDATE triggers SET enabled=? WHERE id=?",
                (1 if enabled else 0, trigger_id),
            )
        return self.get_trigger(trigger_id)

    def claim_trigger_fire(
        self,
        trigger_id: str,
        tool: str,
        session_key: str,
        state: str,
        fp: str,
    ) -> bool:
        """trigger+session+state+fp 조합의 발사권 선점 — 최초 1회만 True.

        fp는 상태 전이 지문(세션 updated_at 등). 같은 전이의 반복 평가는
        UNIQUE 충돌로 False를 반환해 중복 발사를 막는다."""
        fid = uuid.uuid4().hex[:12]
        with self.tx() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO trigger_fires"
                "(id,trigger_id,tool,session_key,state,fp,fired_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (fid, trigger_id, tool, session_key, state, fp, _iso(_now())),
            )
            return cur.rowcount > 0

    def mark_trigger_error(self, trigger_id: str, error: str) -> None:
        """발사 실패 기록 — last_error만 갱신(last_fired_at은 유지)."""
        with self.tx() as c:
            c.execute(
                "UPDATE triggers SET last_error=? WHERE id=?",
                (error[:300], trigger_id),
            )

    def mark_trigger_fired(self, trigger_id: str) -> None:
        """발사 성공 기록 — last_fired_at 갱신 + last_error 클리어."""
        with self.tx() as c:
            c.execute(
                "UPDATE triggers SET last_fired_at=?, last_error='' WHERE id=?",
                (_iso(_now()), trigger_id),
            )

    # ---- share_links (공개 스냅샷 공유) ----
    def create_share_link(
        self,
        token: str,
        kind: str,
        ref_id: str,
        payload: dict[str, Any],
        created_by: str,
        expires_at: str,
    ) -> None:
        now = _iso(_now())
        with self.tx() as c:
            c.execute(
                "INSERT INTO share_links(token,kind,ref_id,payload,created_by,"
                "expires_at,created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    token,
                    kind,
                    ref_id,
                    json.dumps(payload, ensure_ascii=False, default=str),
                    created_by,
                    expires_at,
                    now,
                ),
            )

    def get_share_link(self, token: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT token,kind,ref_id,payload,created_by,expires_at,created_at"
            " FROM share_links WHERE token=?",
            (token,),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["payload"] = _loads(d.get("payload") or "{}")
        return d

    # ---- dashboard_cards (사용자 등록 JSON 위젯) ----
    def create_card(
        self,
        title: str,
        url: str,
        json_path: str = "",
        enabled: bool = True,
        created_by: str = "",
    ) -> dict[str, Any]:
        cid = uuid.uuid4().hex[:12]
        now = _iso(_now())
        with self.tx() as c:
            c.execute(
                "INSERT INTO dashboard_cards(id,title,url,json_path,enabled,"
                "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                (cid, title, url, json_path, 1 if enabled else 0, created_by, now),
            )
        card = self.get_card(cid)
        return card if card is not None else {"id": cid}

    def list_cards(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM dashboard_cards ORDER BY created_at DESC"
        ).fetchall()
        return [self._card_json(r) for r in rows]

    @staticmethod
    def _card_json(r: Any) -> dict[str, Any]:
        d = dict(r)
        d["enabled"] = bool(d.get("enabled"))
        return d

    def get_card(self, card_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM dashboard_cards WHERE id=?", (card_id,)).fetchone()
        return self._card_json(row) if row else None

    def delete_card(self, card_id: str) -> bool:
        with self.tx() as c:
            cur = c.execute("DELETE FROM dashboard_cards WHERE id=?", (card_id,))
        return cur.rowcount > 0

    def set_card_enabled(self, card_id: str, enabled: bool) -> dict[str, Any] | None:
        card = self.get_card(card_id)
        if card is None:
            return None
        with self.tx() as c:
            c.execute(
                "UPDATE dashboard_cards SET enabled=? WHERE id=?",
                (1 if enabled else 0, card_id),
            )
        return self.get_card(card_id)
