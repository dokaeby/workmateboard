"""jobMate CLI — run 제출/조회/승인, gmail 상태."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time

from .config import (
    configure_langsmith,
    configure_logging,
    database_dsn,
    get_settings,
)
from .llm import CompositeLLM
from .models import RunStatus
from .store.db import Store
from .workflows import WORKFLOWS
from .workflows.registry_builder import build_runner

logger = logging.getLogger("jobmate.cli")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="jobmate")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("run", help="dev_review run 실행 (동기)")
    sp.add_argument("task")
    sp.add_argument("--workflow", default="dev_review")

    sp = sub.add_parser("status", help="run 상태 조회")
    sp.add_argument("run_id")

    sp = sub.add_parser("approve", help="승인 대기 run 승인/거부")
    sp.add_argument("run_id")
    sp.add_argument("decision", choices=["approve", "reject"])
    sp.add_argument("--comment", default="")

    sp = sub.add_parser("resume", help="인터럽트된 run 재개(내부용)")
    sp.add_argument("run_id")
    sp.add_argument("decision", choices=["approve", "reject"])

    sp = sub.add_parser("workflows", help="등록된 워크플로 목록")

    sp = sub.add_parser(
        "agent-bridge",
        help="로컬 에이전트 세션(claude/codex/gemini/devin) 수집 → API push",
    )
    sp.add_argument(
        "--url",
        default=os.environ.get("JOBMATE_URL", "http://127.0.0.1:8000"),
        help="jobMate API base URL (기본: JOBMATE_URL env 또는 http://127.0.0.1:8000)",
    )
    sp.add_argument(
        "--api-key-env",
        default="JOBMATE_API_KEY",
        help="API 키를 담은 환경변수 이름 (기본: JOBMATE_API_KEY)",
    )
    sp.add_argument("--interval", type=float, default=15.0, help="스캔 주기(초)")
    sp.add_argument("--once", action="store_true", help="1회 스캔+push 후 종료")

    sp = sub.add_parser("google-auth", help="Google OAuth 토큰 발급 (gmail/calendar)")
    sp.add_argument("kind", choices=["gmail", "calendar"])
    sp.add_argument(
        "--credentials",
        default=None,
        help="OAuth client JSON(credentials.json) 경로 — --client-id/--client-secret 대신 사용",
    )
    sp.add_argument("--client-id", default=None)
    sp.add_argument("--client-secret", default=None)
    sp.add_argument(
        "--token-path", required=True, help="토큰 JSON 저장 경로 (예: data/google-gmail.json)"
    )
    args = p.parse_args(argv)

    settings = get_settings()
    configure_logging(settings)
    configure_langsmith(settings)

    if args.cmd == "agent-bridge":
        return _agent_bridge(args)

    store = Store(database_dsn(settings))
    llm = CompositeLLM()

    if args.cmd == "workflows":
        for w in WORKFLOWS.values():
            print(f"{w.key} v{w.version}")
        return 0

    if args.cmd == "google-auth":
        from .adapters.google import GoogleAuthError, issue_token, load_client_credentials

        try:
            if args.credentials:
                cid, csec = load_client_credentials(args.credentials)
            elif args.client_id and args.client_secret:
                cid, csec = args.client_id, args.client_secret
            else:
                print("--credentials 또는 --client-id/--client-secret 필요", file=sys.stderr)
                return 1
            path = issue_token(
                args.kind, client_id=cid, client_secret=csec, token_path=args.token_path
            )
        except (GoogleAuthError, OSError, KeyError, ValueError) as e:
            logger.warning("google-auth %s failed: %s", args.kind, e)
            print(f"google-auth failed: {e}", file=sys.stderr)
            return 1
        logger.info("google-auth %s token saved: %s", args.kind, path)
        print(f"token saved: {path}")
        return 0

    if args.cmd == "run":
        run = store.create_run(
            args.workflow, WORKFLOWS[args.workflow].version, {"task": args.task}, trace_id="cli"
        )
        runner = build_runner(args.workflow, store=store, llm=llm, settings=settings)
        logger.info("cli run %s workflow=%s", run.id, args.workflow)
        try:
            runner.invoke(run.id, {"task": args.task})
        except Exception as e:
            store.update_run(run.id, status=RunStatus.FAILED, error=str(e)[:300])
            logger.error("cli run %s failed: %s", run.id, str(e)[:300])
        r = store.get_run(run.id)
        print(
            json.dumps(
                {
                    "id": run.id,
                    "status": r.status.value if r else "?",
                    "error": r.error if r else "",
                },
                ensure_ascii=False,
            )
        )
        return 0

    if args.cmd == "status":
        r = store.get_run(args.run_id)
        if not r:
            print("not found", file=sys.stderr)
            return 1
        print(
            json.dumps(
                {
                    "id": r.id,
                    "status": r.status.value,
                    "stages": [s.stage_key for s in store.get_stage_runs(r.id)],
                },
                ensure_ascii=False,
            )
        )
        return 0

    if args.cmd in ("approve", "resume"):
        r = store.get_run(args.run_id)
        if not r:
            print("not found", file=sys.stderr)
            return 1
        runner = build_runner(r.workflow_key, store=store, llm=llm, settings=settings)
        runner.resume(r.id, {"decision": args.decision, "comment": getattr(args, "comment", "")})
        r2 = store.get_run(r.id)
        print(json.dumps({"id": r.id, "status": r2.status.value if r2 else "?"}))
        return 0

    return 1


def _agent_bridge(args: argparse.Namespace) -> int:
    from .agent_bridge import Bridge

    api_key = os.environ.get(args.api_key_env, "")
    if not api_key:
        logger.warning("agent-bridge: env %s not set — push will likely fail", args.api_key_env)
    bridge = Bridge(args.url, api_key or None)
    if args.once:
        counts = bridge.push()
        logger.info("agent-bridge once: %s", counts)
        return 0
    logger.info("agent-bridge start url=%s interval=%ss", args.url, args.interval)
    try:
        while True:
            counts = bridge.push()
            if counts["pushed"] or counts["ended"]:
                logger.info("agent-bridge push: %s", counts)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        logger.info("agent-bridge stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
