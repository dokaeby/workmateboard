"""Builder routes — playbooks, run comments, connectors, custom workflows, triggers."""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse

from ... import webutil
from ...models import WorkflowSpec
from ...workflows import WORKFLOWS
from ..schemas import (
    CommentCreateRequest,
    ConnectorCreateRequest,
    CustomWorkflowRequest,
    PlaybookCreateRequest,
    ScheduleToggleRequest,
    TriggerCreateRequest,
)
from ..support import (
    _BUILTIN_WF_KEYS,
    _CONNECTOR_CACHE,
    _CONNECTOR_CACHE_TTL,
    _config_has_secret,
    _encrypt_config_secrets,
    _fetch_connector_items,
    _sync_workflow_registry,
)

logger = logging.getLogger("jobmate.api")


def register(app: FastAPI, ctx: Any) -> None:
    store = ctx.store
    err = ctx.err
    _lead_gate = ctx._lead_gate
    _session_user = ctx._session_user
    _actor_label = ctx._actor_label
    _audit_api = ctx._audit_api
    _schedule_run_jobs = ctx._schedule_run_jobs
    _validate_trigger = ctx._validate_trigger

    # ---- 1. PLAYBOOKS (플레이북 뷰는 lead 전용 — 목록 포함 전체 lead) ----
    @app.get("/api/v1/playbooks")
    def list_playbooks(request: Request) -> JSONResponse:
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        return JSONResponse(content={"items": store.list_playbooks()})

    @app.post("/api/v1/playbooks", status_code=201)
    def create_playbook(body: PlaybookCreateRequest, request: Request) -> JSONResponse:
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        if body.workflow not in WORKFLOWS:
            return err(404, "unknown_workflow", f"workflow '{body.workflow}' not found", request)
        name = body.name.strip()
        if not name:
            return err(422, "validation", "name must not be blank", request)
        item = store.create_playbook(name, body.workflow, body.input, _actor_label(request))
        _audit_api(
            request, "playbook_create", item["id"], {"name": name, "workflow": body.workflow}
        )
        logger.info("playbook created id=%s workflow=%s", item["id"], body.workflow)
        return JSONResponse(status_code=201, content=item)

    @app.delete("/api/v1/playbooks/{playbook_id}")
    def delete_playbook(playbook_id: str, request: Request) -> JSONResponse:
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        if not store.delete_playbook(playbook_id):
            return err(404, "not_found", "playbook not found", request)
        _audit_api(request, "playbook_delete", playbook_id, {})
        logger.info("playbook deleted id=%s", playbook_id)
        return JSONResponse(content={"ok": True})

    @app.post("/api/v1/playbooks/{playbook_id}/run", status_code=202)
    def run_playbook(
        playbook_id: str, request: Request, background_tasks: BackgroundTasks
    ) -> JSONResponse:
        """플레이북 input으로 run 생성 — POST /runs와 동일한 생성/실행 경로."""
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        pb = store.get_playbook(playbook_id)
        if pb is None:
            return err(404, "not_found", "playbook not found", request)
        wf = str(pb.get("workflow") or "")
        if wf not in WORKFLOWS:
            return err(409, "workflow_unavailable", f"workflow '{wf}' is not registered", request)
        payload = dict(pb.get("input") or {})
        payload.setdefault("task", str(pb.get("name") or pb["id"]))
        payload["playbook_id"] = pb["id"]
        # POST /runs와 동일한 스코프드 예산 + wakeup 코얼레싱 경로
        from ... import budget_scope, wakeups

        blocks = budget_scope.evaluate_admission(store, wf, str(payload.get("project") or ""))
        if blocks:
            _audit_api(request, "playbook_budget_exceeded", pb["id"], {"scope": blocks[0]["scope"]})
            return err(
                429,
                "budget_exceeded",
                f"budget exceeded: {blocks[0]['scope']}",
                request,
            )
        res = wakeups.admit_run(
            store,
            workflow_key=wf,
            input_data=payload,
            source="api",
            created_by=_actor_label(request),
            version=WORKFLOWS[wf].version,
            job_payload={"task": payload["task"]},
        )
        action = str(res.get("action") or "")
        if action != "created":
            run = res.get("run")
            _audit_api(
                request,
                "playbook_run",
                pb["id"],
                {"run_id": res.get("run_id"), "workflow": wf, "action": action},
            )
            return JSONResponse(
                status_code=202,
                content={
                    "run_id": str(res.get("run_id") or ""),
                    "status": run.status.value if run is not None else action,
                    "workflow": wf,
                    "action": action,
                },
            )
        run = res["run"]
        _audit_api(request, "playbook_run", pb["id"], {"run_id": run.id, "workflow": wf})
        logger.info("playbook run created playbook=%s run=%s workflow=%s", pb["id"], run.id, wf)
        _schedule_run_jobs([(run.id, wf, {"task": payload["task"]}, None)], background_tasks)
        return JSONResponse(
            status_code=202,
            content={"run_id": run.id, "status": run.status.value, "workflow": wf},
        )

    # ---- 2. RUN COMMENTS ----
    @app.get("/api/v1/runs/{run_id}/comments")
    def list_run_comments(run_id: str, request: Request) -> JSONResponse:
        if store.get_run(run_id) is None:
            return err(404, "not_found", "run not found", request)
        return JSONResponse(content={"items": store.list_run_comments(run_id)})

    @app.post("/api/v1/runs/{run_id}/comments", status_code=201)
    def add_run_comment(run_id: str, body: CommentCreateRequest, request: Request) -> JSONResponse:
        if store.get_run(run_id) is None:
            return err(404, "not_found", "run not found", request)
        text = body.body.strip()
        if not text:
            return err(422, "validation", "comment body must not be blank", request)
        if len(text) > 4000:
            return err(422, "validation", "comment body exceeds 4000 chars", request)
        user = _session_user(request)
        item = store.add_run_comment(
            run_id,
            user_id=str(user["id"]) if user else "",
            email=str(user["email"]) if user else _actor_label(request),
            body=text,
        )
        logger.info("run comment added run=%s", run_id)
        return JSONResponse(status_code=201, content=item)

    # ---- 3. CONNECTORS (커넥터 뷰는 lead 전용 — config에 비밀값 포함) ----
    _CONNECTOR_TYPES = {"github", "jira", "notion", "linear", "webhook", "json"}

    @app.get("/api/v1/connectors")
    def list_connectors(request: Request) -> JSONResponse:
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        return JSONResponse(content={"items": store.list_connectors()})

    @app.post("/api/v1/connectors", status_code=201)
    def create_connector(body: ConnectorCreateRequest, request: Request) -> JSONResponse:
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        ctype = body.type.strip().lower()
        if ctype not in _CONNECTOR_TYPES:
            return err(
                422,
                "validation",
                f"type must be one of {sorted(_CONNECTOR_TYPES)}",
                request,
            )
        name = body.name.strip()
        if not name:
            return err(422, "validation", "name must not be blank", request)
        # SSRF 가드 — generic 타입은 config.url을 서버가 대신 fetch하므로
        # 생성 시점에 공인 URL만 허용한다. github는 fetch 시 api.github.com으로
        # 고정되므로 config.url이 있어도 무시되며 검증하지 않는다.
        if ctype != "github":
            cfg_url = body.config.get("url")
            if (
                isinstance(cfg_url, str)
                and cfg_url.strip()
                and not webutil.is_safe_fetch_url(cfg_url.strip())
            ):
                logger.warning("connector create rejected: unsafe config.url type=%s", ctype)
                return err(422, "url_not_allowed", "config.url is not allowed", request)
        enc_config = _encrypt_config_secrets(body.config)
        has_token = _config_has_secret(body.config)
        item = store.create_connector(ctype, name, enc_config, body.enabled, has_token)
        _audit_api(request, "connector_create", item["id"], {"type": ctype, "name": name})
        logger.info("connector created id=%s type=%s", item["id"], ctype)
        return JSONResponse(status_code=201, content=item)

    @app.delete("/api/v1/connectors/{connector_id}")
    def delete_connector(connector_id: str, request: Request) -> JSONResponse:
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        if not store.delete_connector(connector_id):
            return err(404, "not_found", "connector not found", request)
        _CONNECTOR_CACHE.pop(connector_id, None)
        _audit_api(request, "connector_delete", connector_id, {})
        logger.info("connector deleted id=%s", connector_id)
        return JSONResponse(content={"ok": True})

    @app.post("/api/v1/connectors/{connector_id}/toggle")
    def toggle_connector(
        connector_id: str, body: ScheduleToggleRequest, request: Request
    ) -> JSONResponse:
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        conn = store.set_connector_enabled(connector_id, body.enabled)
        if conn is None:
            return err(404, "not_found", "connector not found", request)
        conn.pop("config", None)  # toggle 응답에도 config 비노출
        _audit_api(request, "connector_toggle", connector_id, {"enabled": body.enabled})
        logger.info("connector %s toggled enabled=%s", connector_id, body.enabled)
        return JSONResponse(content=conn)

    @app.get("/api/v1/connectors/{connector_id}/items")
    def connector_items(connector_id: str, request: Request, limit: int = 50) -> JSONResponse:
        """외부 API에서 live fetch (60초 인메모리 캐시). lead 이상 — SSRF 표면."""
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        conn = store.get_connector(connector_id)
        if conn is None:
            return err(404, "not_found", "connector not found", request)
        if not conn.get("enabled"):
            return err(409, "connector_disabled", "connector is disabled", request)
        limit = max(1, min(limit, 200))
        cached = _CONNECTOR_CACHE.get(connector_id)
        if cached and time.time() - cached[0] < _CONNECTOR_CACHE_TTL:
            return JSONResponse(content={"items": cached[1], "cached": True})
        try:
            items = _fetch_connector_items(conn, limit)
        except Exception as e:
            store.mark_connector_fetch(connector_id, error=str(e)[:200])
            logger.warning(
                "connector fetch failed id=%s type=%s: %s",
                connector_id,
                conn.get("type"),
                str(e)[:200],
            )
            if str(e) == "url_not_allowed":
                # SSRF 가드 차단 — 생성 시 422로 막지만 DB 직접 삽입 등 우회 경로 대비
                return err(422, "url_not_allowed", "connector url is not allowed", request)
            return err(502, "fetch_failed", str(e)[:200], request)
        store.mark_connector_fetch(connector_id)
        _CONNECTOR_CACHE[connector_id] = (time.time(), items)
        if len(_CONNECTOR_CACHE) > 200:
            _CONNECTOR_CACHE.clear()
        return JSONResponse(content={"items": items, "cached": False})

    # ---- 4. CUSTOM WORKFLOWS (빌더 뷰 lead 전용) ----
    @app.get("/api/v1/workflows/custom")
    def list_custom_workflows(request: Request) -> JSONResponse:
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        return JSONResponse(
            content={
                "items": [
                    {"name": r["name"], "spec": r["spec"]} for r in store.list_custom_workflows()
                ]
            }
        )

    @app.post("/api/v1/workflows/custom", status_code=201)
    def create_custom_workflow(body: CustomWorkflowRequest, request: Request) -> JSONResponse:
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        name = body.name
        if name in _BUILTIN_WF_KEYS:
            return err(409, "conflict", f"'{name}' is a builtin workflow", request)
        spec_dict = body.spec
        try:
            spec = (
                WorkflowSpec.model_validate_json(spec_dict)
                if isinstance(spec_dict, str)
                else WorkflowSpec.model_validate(spec_dict)
            )
        except Exception as e:
            return err(
                422,
                "invalid_spec",
                f"spec is not a valid WorkflowSpec: {str(e)[:200]}",
                request,
            )
        if spec.key != name:
            return err(422, "invalid_spec", "spec.key must equal the workflow name", request)
        store.upsert_custom_workflow(name, spec.model_dump(mode="json"))
        _sync_workflow_registry(store)
        _audit_api(request, "custom_workflow_save", name, {"version": spec.version})
        logger.info("custom workflow saved name=%s version=%s", name, spec.version)
        return JSONResponse(
            status_code=201, content={"name": name, "spec": spec.model_dump(mode="json")}
        )

    @app.delete("/api/v1/workflows/custom/{name}")
    def delete_custom_workflow(name: str, request: Request) -> JSONResponse:
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        if name in _BUILTIN_WF_KEYS:
            return err(400, "validation", "cannot delete a builtin workflow", request)
        if not store.delete_custom_workflow(name):
            return err(404, "not_found", "custom workflow not found", request)
        _sync_workflow_registry(store)
        _audit_api(request, "custom_workflow_delete", name, {})
        logger.info("custom workflow deleted name=%s", name)
        return JSONResponse(content={"ok": True})

    # ---- 5. TRIGGERS (트리거 뷰 lead 전용 — run 자동 발사 설정) ----
    @app.get("/api/v1/triggers")
    def list_triggers(request: Request) -> JSONResponse:
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        return JSONResponse(content={"items": store.list_triggers()})

    @app.post("/api/v1/triggers", status_code=201)
    def create_trigger(body: TriggerCreateRequest, request: Request) -> JSONResponse:
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        bad = _validate_trigger(body, request)
        if bad is not None:
            return bad
        item = store.create_trigger(body.match or {}, body.action or {})
        _audit_api(
            request,
            "trigger_create",
            item["id"],
            {"action": (body.action or {}).get("type")},
        )
        logger.info("trigger created id=%s", item["id"])
        return JSONResponse(status_code=201, content=item)

    @app.delete("/api/v1/triggers/{trigger_id}")
    def delete_trigger(trigger_id: str, request: Request) -> JSONResponse:
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        if not store.delete_trigger(trigger_id):
            return err(404, "not_found", "trigger not found", request)
        _audit_api(request, "trigger_delete", trigger_id, {})
        logger.info("trigger deleted id=%s", trigger_id)
        return JSONResponse(content={"ok": True})

    @app.post("/api/v1/triggers/{trigger_id}/toggle")
    def toggle_trigger(
        trigger_id: str, body: ScheduleToggleRequest, request: Request
    ) -> JSONResponse:
        gate = _lead_gate(request)
        if gate is not None:
            return gate
        trig = store.set_trigger_enabled(trigger_id, body.enabled)
        if trig is None:
            return err(404, "not_found", "trigger not found", request)
        _audit_api(request, "trigger_toggle", trigger_id, {"enabled": body.enabled})
        logger.info("trigger %s toggled enabled=%s", trigger_id, body.enabled)
        return JSONResponse(content=trig)
