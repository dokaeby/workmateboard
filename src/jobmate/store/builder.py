# mypy: disable-error-code="attr-defined"
"""Builder/store mixin methods extracted verbatim from store/db.py."""

from __future__ import annotations

import json
import uuid
from typing import Any

from ..models import WorkflowSpec
from . import orgctx
from .util import _iso, _loads, _now


def _org_filter() -> tuple[str, list[Any]]:
    """ctx 조직이 있으면 ` AND org_id=?` 조건 — 시스템/미인증(ctx='')은 무필터."""
    oid = orgctx.current_org_id()
    return (" AND org_id=?", [oid]) if oid else ("", [])


class BuilderStoreMixin:
    # ---- playbooks (원클릭 작업 템플릿) ----
    @staticmethod
    def _playbook_json(r: Any) -> dict[str, Any]:
        d = dict(r)
        d["input"] = _loads(d.get("input") or "{}")
        return d

    def create_playbook(
        self,
        name: str,
        workflow: str,
        input_data: dict[str, Any],
        created_by: str = "",
        org_id: str | None = None,
    ) -> dict[str, Any]:
        pid = uuid.uuid4().hex[:12]
        now = _iso(_now())
        with self.tx() as c:
            c.execute(
                "INSERT INTO playbooks(id,name,workflow,input,created_by,created_at,org_id)"
                " VALUES(?,?,?,?,?,?,?)",
                (
                    pid,
                    name,
                    workflow,
                    json.dumps(input_data or {}),
                    created_by,
                    now,
                    orgctx.org_for_write(org_id),
                ),
            )
        pb = self.get_playbook(pid)
        return pb if pb is not None else {"id": pid}

    def list_playbooks(self) -> list[dict[str, Any]]:
        frag, params = _org_filter()
        rows = self._conn.execute(
            "SELECT * FROM playbooks"  # noqa: S608
            + frag.replace(" AND ", " WHERE ", 1)
            + " ORDER BY created_at DESC",
            tuple(params),
        ).fetchall()
        return [self._playbook_json(r) for r in rows]

    def get_playbook(self, playbook_id: str) -> dict[str, Any] | None:
        frag, params = _org_filter()
        row = self._conn.execute(
            "SELECT * FROM playbooks WHERE id=?" + frag,  # noqa: S608
            (playbook_id, *params),
        ).fetchone()
        return self._playbook_json(row) if row else None

    def delete_playbook(self, playbook_id: str) -> bool:
        frag, params = _org_filter()
        with self.tx() as c:
            cur = c.execute("DELETE FROM playbooks WHERE id=?" + frag, (playbook_id, *params))  # noqa: S608
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
        org_id: str | None = None,
    ) -> dict[str, Any]:
        cid = uuid.uuid4().hex[:12]
        now = _iso(_now())
        with self.tx() as c:
            c.execute(
                "INSERT INTO connectors(id,type,name,config,enabled,has_token,"
                "last_fetch_at,last_error,created_at,org_id)"
                " VALUES(?,?,?,?,?,?,NULL,'',?,?)",
                (
                    cid,
                    ctype,
                    name,
                    json.dumps(config or {}, ensure_ascii=False),
                    1 if enabled else 0,
                    int(has_token),
                    now,
                    orgctx.org_for_write(org_id),
                ),
            )
        conn = self.get_connector(cid)
        if conn is not None:
            conn.pop("config", None)  # 생성 응답에도 config(암호문 포함) 비노출
        return conn if conn is not None else {"id": cid}

    def list_connectors(self) -> list[dict[str, Any]]:
        frag, params = _org_filter()
        rows = self._conn.execute(
            "SELECT * FROM connectors"  # noqa: S608
            + frag.replace(" AND ", " WHERE ", 1)
            + " ORDER BY created_at DESC",
            tuple(params),
        ).fetchall()
        return [self._connector_json(r) for r in rows]

    def get_connector(self, connector_id: str) -> dict[str, Any] | None:
        frag, params = _org_filter()
        row = self._conn.execute(
            "SELECT * FROM connectors WHERE id=?" + frag,  # noqa: S608
            (connector_id, *params),
        ).fetchone()
        return self._connector_json(row, with_config=True) if row else None

    def delete_connector(self, connector_id: str) -> bool:
        frag, params = _org_filter()
        with self.tx() as c:
            cur = c.execute("DELETE FROM connectors WHERE id=?" + frag, (connector_id, *params))  # noqa: S608
        return cur.rowcount > 0

    def set_connector_enabled(self, connector_id: str, enabled: bool) -> dict[str, Any] | None:
        conn = self.get_connector(connector_id)
        if conn is None:
            return None
        frag, fparams = _org_filter()
        with self.tx() as c:
            c.execute(
                "UPDATE connectors SET enabled=? WHERE id=?" + frag,  # noqa: S608
                (1 if enabled else 0, connector_id, *fparams),
            )
        return self.get_connector(connector_id)

    def mark_connector_fetch(self, connector_id: str, error: str = "") -> None:
        """fetch 시도 결과 기록 — last_fetch_at은 성공/실패 무관하게 갱신."""
        frag, params = _org_filter()
        with self.tx() as c:
            c.execute(
                "UPDATE connectors SET last_fetch_at=?, last_error=? WHERE id=?" + frag,  # noqa: S608
                (_iso(_now()), error[:300], connector_id, *params),
            )

    # ---- custom_workflows (비주얼 빌더 WorkflowSpec) + workflow_revisions ----
    @staticmethod
    def _custom_workflow_json(r: Any) -> dict[str, Any]:
        d = dict(r)
        d["spec"] = _loads(d.get("spec") or "{}")
        try:
            d["latest_revision"] = int(d.get("latest_revision") or 0)
        except (TypeError, ValueError):
            d["latest_revision"] = 0
        return d

    def upsert_custom_workflow(
        self, name: str, spec: dict[str, Any], org_id: str | None = None
    ) -> dict[str, Any]:
        """레거시 호환 저장 — 내부적으로 save_custom_workflow에 위임해
        리비전도 함께 남긴다(리비전 없는 spec 갱신 경로를 없애기 위함)."""
        self.save_custom_workflow(name, spec, note="upsert", org_id=org_id)
        # 읽기-다시 도 저장된 org 기준으로 — 명시 org_id가 ctx와 다르거나
        # ctx=''(기본 조직 귀속)일 때 다른 조직의 동명 행을 반환하는 것을 막는다.
        with orgctx.org_scope(self._custom_wf_write_org(org_id)):
            row = self.get_custom_workflow(name)
        return row if row is not None else {"name": name, "spec": spec}

    def _custom_wf_write_org(self, explicit: str | None = None) -> str:
        """custom_workflows 쓰기 org — 명시 > 요청 ctx > 첫 활성 조직.

        ctx='' 쓰기를 org_id=''에 두면 resolver의 ctx='' 폴백(첫 활성 조직)과
        어긋나 저장한 spec을 다시 해석하지 못한다. 읽기 경로의 무필터와 달리
        쓰기는 항상 단일 org에 귀속시킨다(spec 003 US3)."""
        oid = orgctx.org_for_write(explicit)
        if oid:
            return oid
        try:
            orgs = self.list_organizations()
        except Exception:
            return "__no_org__"
        # FR-031 — suspended 조직은 "활성"이 아니므로 건너뛴다. 활성 조직이
        # 없으면 '__no_org__' — '' 스탬프는 무필터 ctx 조회에 노출된다.
        for o in orgs:
            if str(o.get("status") or "active") == "active":
                return str(o["id"])
        return "__no_org__"

    def save_custom_workflow(
        self,
        name: str,
        spec: dict[str, Any],
        *,
        base_revision: int | None = None,
        note: str = "",
        created_by: str = "",
        org_id: str | None = None,
    ) -> dict[str, Any]:
        """WorkflowSpec 저장 + append-only 리비전 기록 (Paperclip document
        versioning 포팅).

        - base_revision이 주어지고 현재 latest_revision과 다르면
          {"ok": False, "conflict": True, "current_revision": N} — 409 CAS.
        - base_revision 생략 시 항상 latest+1 리비전을 추가한다
          (첫 저장은 revision 1).
        - 성공 시 custom_workflows.spec/latest_revision/updated_at을 갱신하고
          workflow_revisions에 (name, revision) 행을 append한다.
          리비전 행은 절대 재작성하지 않는다."""
        now = _iso(_now())
        spec_json = json.dumps(spec, ensure_ascii=False)
        oid = self._custom_wf_write_org(org_id)
        # SELECT/UPDATE도 "기록될 org"(oid) 기준으로 스코프한다 — 필터 없이
        # name만으로 UPDATE하면 ctx='' 쓰기가 모든 조직의 동명 행을 한꺼번에
        # 덮어쓰고, 명시 org_id와 ctx가 엇갈리면 엉뚱한 조직 행을 수정한다
        # (cross-org 덮어쓰기 — spec 003 US3).
        frag = " AND org_id=?"
        fparams: list[Any] = [oid]
        try:
            with self.tx() as c:
                row = c.execute(
                    "SELECT latest_revision FROM custom_workflows WHERE name=?" + frag,  # noqa: S608
                    (name, *fparams),
                ).fetchone()
                latest = int(row["latest_revision"] or 0) if row is not None else 0
                if row is not None and base_revision is not None:
                    try:
                        base = int(base_revision)
                    except (TypeError, ValueError):
                        base = -1
                    if base != latest:
                        return {
                            "ok": False,
                            "conflict": True,
                            "current_revision": latest,
                        }
                new_rev = latest + 1
                if row is None:
                    c.execute(
                        "INSERT INTO custom_workflows"
                        "(name,spec,created_at,latest_revision,updated_at,org_id)"
                        " VALUES(?,?,?,?,?,?)",
                        (name, spec_json, now, new_rev, now, oid),
                    )
                else:
                    cur = c.execute(
                        "UPDATE custom_workflows SET spec=?, latest_revision=?,"  # noqa: S608
                        " updated_at=? WHERE name=?" + frag,
                        (spec_json, new_rev, now, name, *fparams),
                    )
                    if int(getattr(cur, "rowcount", 0) or 0) <= 0:
                        # SELECT~UPDATE 사이 동시 삭제 — 리비전만 남는
                        # 고아 행과 false success를 막기 위해 conflict 반환.
                        return {
                            "ok": False,
                            "conflict": True,
                            "current_revision": 0,
                        }
                c.execute(
                    "INSERT INTO workflow_revisions"
                    "(id,name,revision,spec,note,created_by,created_at,org_id)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (
                        uuid.uuid4().hex[:12],
                        name,
                        new_rev,
                        spec_json,
                        str(note or "")[:500],
                        str(created_by or "")[:200],
                        now,
                        oid,
                    ),
                )
        except self._integrity_errors:
            # UNIQUE(name, revision) 충돌 — 동시 저장 경합. 현재 latest를
            # 다시 읽어 conflict로 보고한다(리비전은 이미 append됐을 수 있으나
            # latest+1 재시도는 호출자 몫 — 여기서는 안전하게 conflict 반환).
            cur = self._conn.execute(
                "SELECT latest_revision FROM custom_workflows WHERE name=?" + frag,  # noqa: S608
                (name, *fparams),
            ).fetchone()
            latest = int(cur["latest_revision"] or 0) if cur is not None else 0
            return {"ok": False, "conflict": True, "current_revision": latest}
        return {"ok": True, "revision": new_rev, "current_revision": new_rev}

    def list_custom_workflows(self) -> list[dict[str, Any]]:
        frag, params = _org_filter()
        rows = self._conn.execute(
            "SELECT * FROM custom_workflows"  # noqa: S608
            + frag.replace(" AND ", " WHERE ", 1)
            + " ORDER BY name",
            tuple(params),
        ).fetchall()
        return [self._custom_workflow_json(r) for r in rows]

    def get_custom_workflow(self, name: str) -> dict[str, Any] | None:
        frag, params = _org_filter()
        row = self._conn.execute(
            "SELECT * FROM custom_workflows WHERE name=?" + frag,  # noqa: S608
            (name, *params),
        ).fetchone()
        return self._custom_workflow_json(row) if row else None

    # ---- workflow_revisions (append-only spec 이력) ----
    @staticmethod
    def _workflow_revision_json(r: Any, *, with_spec: bool = False) -> dict[str, Any]:
        d = dict(r)
        try:
            d["revision"] = int(d.get("revision") or 0)
        except (TypeError, ValueError):
            d["revision"] = 0
        if with_spec:
            d["spec"] = _loads(d.get("spec") or "{}")
        else:
            # 목록 응답은 spec 전문 대신 미리보기만 — spec은 전체가 클 수 있다.
            raw = str(d.pop("spec", "") or "")
            d["spec_preview"] = raw[:200]
        return d

    def list_custom_workflow_revisions(self, name: str) -> list[dict[str, Any]]:
        """리비전 목록 — 최신순. spec은 spec_preview(200자)로 대체."""
        frag, params = _org_filter()
        rows = self._conn.execute(
            "SELECT id,name,revision,spec,note,created_by,created_at"  # noqa: S608
            " FROM workflow_revisions WHERE name=?" + frag + " ORDER BY revision DESC",
            (name, *params),
        ).fetchall()
        return [self._workflow_revision_json(r) for r in rows]

    def get_custom_workflow_revision(self, name: str, revision: int) -> dict[str, Any] | None:
        """특정 리비전의 전체 spec 조회."""
        frag, params = _org_filter()
        row = self._conn.execute(
            "SELECT id,name,revision,spec,note,created_by,created_at"  # noqa: S608
            " FROM workflow_revisions WHERE name=? AND revision=?" + frag,
            (name, int(revision), *params),
        ).fetchone()
        return self._workflow_revision_json(row, with_spec=True) if row else None

    def restore_custom_workflow_revision(
        self, name: str, revision: int, *, created_by: str = "", note: str = ""
    ) -> dict[str, Any]:
        """restore-as-new-revision — 과거 리비전 spec을 최신+1로 복제.

        이력은 절대 재작성하지 않는다. 반환 dict:
          성공 {"ok": True, "name", "revision"}
          실패 {"ok": False, "reason": "not_found"|"invalid_spec"|
                "conflict"|"save_failed", "current_revision"?}
        reason을 구분해 반환한다 — 호출자가 404/422/409를 정확히 매핑할 수
        있게(None 단일 반환은 conflict를 404로 오표기했다). 복원 대상 spec은
        WorkflowSpec으로 재검증하고 spec.key==name을 요구한다 — 레거시/
        손상 spec이 최신 리비전이 되는 것을 막는다."""
        rev = self.get_custom_workflow_revision(name, revision)
        if rev is None:
            return {"ok": False, "reason": "not_found"}
        spec = rev.get("spec")
        if not isinstance(spec, dict):
            return {"ok": False, "reason": "invalid_spec"}
        try:
            ws = WorkflowSpec.model_validate(spec)
        except Exception:
            return {"ok": False, "reason": "invalid_spec"}
        if ws.key != name:
            return {"ok": False, "reason": "invalid_spec"}
        auto_note = note or f"restore revision {int(revision)}"
        res = self.save_custom_workflow(
            name,
            ws.model_dump(mode="json"),
            note=auto_note,
            created_by=created_by,
        )
        if not res.get("ok"):
            if res.get("conflict"):
                return {
                    "ok": False,
                    "reason": "conflict",
                    "current_revision": res.get("current_revision"),
                }
            return {"ok": False, "reason": "save_failed"}
        return {"ok": True, "name": name, "revision": int(res["revision"])}

    def delete_custom_workflow(self, name: str) -> bool:
        """워크플로 삭제 — 리비전 이력도 함께 삭제(cascade).

        리비전은 spec의 파생 이력이라 워크플로 없이 보존할 의미가 없고,
        고아 리비전이 최신 복원 경로를 오염시키는 것을 막는다."""
        frag, params = _org_filter()
        with self.tx() as c:
            cur = c.execute(
                "DELETE FROM custom_workflows WHERE name=?" + frag,  # noqa: S608
                (name, *params),
            )
            c.execute("DELETE FROM workflow_revisions WHERE name=?" + frag, (name, *params))  # noqa: S608
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
        self,
        match: dict[str, Any],
        action: dict[str, Any],
        enabled: bool = True,
        org_id: str | None = None,
    ) -> dict[str, Any]:
        tid = uuid.uuid4().hex[:12]
        now = _iso(_now())
        with self.tx() as c:
            c.execute(
                "INSERT INTO triggers(id,enabled,match,action,created_at,"
                "last_fired_at,last_error,org_id) VALUES(?,?,?,?,?,NULL,'',?)",
                (
                    tid,
                    1 if enabled else 0,
                    json.dumps(match or {}, ensure_ascii=False),
                    json.dumps(action or {}, ensure_ascii=False),
                    now,
                    orgctx.org_for_write(org_id),
                ),
            )
        trig = self.get_trigger(tid)
        return trig if trig is not None else {"id": tid}

    def list_triggers(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        frag, params = _org_filter()
        sql = "SELECT * FROM triggers"
        conds: list[str] = []
        if frag:
            conds.append(frag.strip().removeprefix("AND").strip())
        if enabled_only:
            conds.append("enabled=1")
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY created_at DESC"
        rows = self._conn.execute(sql, params).fetchall()
        return [self._trigger_json(r) for r in rows]

    def get_trigger(self, trigger_id: str) -> dict[str, Any] | None:
        frag, params = _org_filter()
        row = self._conn.execute(
            "SELECT * FROM triggers WHERE id=?" + frag,  # noqa: S608
            (trigger_id, *params),
        ).fetchone()
        return self._trigger_json(row) if row else None

    def delete_trigger(self, trigger_id: str) -> bool:
        frag, params = _org_filter()
        with self.tx() as c:
            cur = c.execute("DELETE FROM triggers WHERE id=?" + frag, (trigger_id, *params))  # noqa: S608
            c.execute("DELETE FROM trigger_fires WHERE trigger_id=?", (trigger_id,))
        return cur.rowcount > 0

    def set_trigger_enabled(self, trigger_id: str, enabled: bool) -> dict[str, Any] | None:
        trig = self.get_trigger(trigger_id)
        if trig is None:
            return None
        frag, fparams = _org_filter()
        with self.tx() as c:
            c.execute(
                "UPDATE triggers SET enabled=? WHERE id=?" + frag,  # noqa: S608
                (1 if enabled else 0, trigger_id, *fparams),
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
        frag, params = _org_filter()
        with self.tx() as c:
            c.execute(
                "UPDATE triggers SET last_error=? WHERE id=?" + frag,  # noqa: S608
                (error[:300], trigger_id, *params),
            )

    def mark_trigger_fired(self, trigger_id: str) -> None:
        """발사 성공 기록 — last_fired_at 갱신 + last_error 클리어."""
        frag, params = _org_filter()
        with self.tx() as c:
            c.execute(
                "UPDATE triggers SET last_fired_at=?, last_error='' WHERE id=?" + frag,  # noqa: S608
                (_iso(_now()), trigger_id, *params),
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
        org_id: str | None = None,
    ) -> None:
        now = _iso(_now())
        with self.tx() as c:
            c.execute(
                "INSERT INTO share_links(token,kind,ref_id,payload,created_by,"
                "expires_at,created_at,org_id) VALUES(?,?,?,?,?,?,?,?)",
                (
                    token,
                    kind,
                    ref_id,
                    json.dumps(payload, ensure_ascii=False, default=str),
                    created_by,
                    expires_at,
                    now,
                    orgctx.org_for_write(org_id),
                ),
            )

    def get_share_link(self, token: str) -> dict[str, Any] | None:
        # 공개 공유 조회 — 토큰 자체가 capability라 org 필터를 걸지 않는다
        # (공개 경로는 ctx=''이며, 관리용 목록/삭제 경로만 org 스코프 대상).
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
        org_id: str | None = None,
    ) -> dict[str, Any]:
        cid = uuid.uuid4().hex[:12]
        now = _iso(_now())
        with self.tx() as c:
            c.execute(
                "INSERT INTO dashboard_cards(id,title,url,json_path,enabled,"
                "created_by,created_at,org_id) VALUES(?,?,?,?,?,?,?,?)",
                (
                    cid,
                    title,
                    url,
                    json_path,
                    1 if enabled else 0,
                    created_by,
                    now,
                    orgctx.org_for_write(org_id),
                ),
            )
        card = self.get_card(cid)
        return card if card is not None else {"id": cid}

    def list_cards(self) -> list[dict[str, Any]]:
        frag, params = _org_filter()
        rows = self._conn.execute(
            "SELECT * FROM dashboard_cards"  # noqa: S608
            + frag.replace(" AND ", " WHERE ", 1)
            + " ORDER BY created_at DESC",
            tuple(params),
        ).fetchall()
        return [self._card_json(r) for r in rows]

    @staticmethod
    def _card_json(r: Any) -> dict[str, Any]:
        d = dict(r)
        d["enabled"] = bool(d.get("enabled"))
        return d

    def get_card(self, card_id: str) -> dict[str, Any] | None:
        frag, params = _org_filter()
        row = self._conn.execute(
            "SELECT * FROM dashboard_cards WHERE id=?" + frag,  # noqa: S608
            (card_id, *params),
        ).fetchone()
        return self._card_json(row) if row else None

    def delete_card(self, card_id: str) -> bool:
        frag, params = _org_filter()
        with self.tx() as c:
            cur = c.execute("DELETE FROM dashboard_cards WHERE id=?" + frag, (card_id, *params))  # noqa: S608
        return cur.rowcount > 0

    def set_card_enabled(self, card_id: str, enabled: bool) -> dict[str, Any] | None:
        card = self.get_card(card_id)
        if card is None:
            return None
        frag, fparams = _org_filter()
        with self.tx() as c:
            c.execute(
                "UPDATE dashboard_cards SET enabled=? WHERE id=?" + frag,  # noqa: S608
                (1 if enabled else 0, card_id, *fparams),
            )
        return self.get_card(card_id)
