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

    sp = sub.add_parser("run", help="dev_review run을 동기로 실행")
    sp.add_argument("task")
    sp.add_argument("--workflow", default="dev_review")

    sp = sub.add_parser("status", help="run 상태 조회")
    sp.add_argument("run_id")

    sp = sub.add_parser("approve", help="승인 대기 중인 run 승인/거부")
    sp.add_argument("run_id")
    sp.add_argument("decision", choices=["approve", "reject"])
    sp.add_argument("--comment", default="")

    sp = sub.add_parser("resume", help="인터럽트된 run 재개 (내부용)")
    sp.add_argument("run_id")
    sp.add_argument("decision", choices=["approve", "reject"])

    sp = sub.add_parser("workflows", help="등록된 워크플로 목록")

    sp = sub.add_parser(
        "agent-bridge",
        help="로컬 에이전트 세션(claude/codex/gemini/devin)을 수집해 API로 push",
    )
    sp.add_argument(
        "--url",
        default=None,
        help="jobMate API base URL (기본: --url > JOBMATE_URL env > http://127.0.0.1:8000)",
    )
    sp.add_argument(
        "--api-key-env",
        default="JOBMATE_API_KEY",
        help="API 키가 담긴 환경변수 이름 (기본: JOBMATE_API_KEY)",
    )
    sp.add_argument("--interval", type=float, default=15.0, help="스캔 주기(초)")
    sp.add_argument("--once", action="store_true", help="1회 스캔+push 후 종료")

    sp = sub.add_parser(
        "paperclip-bridge",
        help="Paperclip API를 폴링해 jobMate external projection으로 push",
    )
    sp.add_argument(
        "--paperclip-url",
        default=None,
        help="Paperclip base URL (기본: --paperclip-url > PAPERCLIP_URL env)",
    )
    sp.add_argument(
        "--company-id",
        default=None,
        help="Paperclip company id (기본: --company-id > PAPERCLIP_COMPANY_ID env)",
    )
    sp.add_argument(
        "--paperclip-api-key-env",
        default="PAPERCLIP_API_KEY",
        help="Paperclip API 키가 담긴 환경변수 이름 (기본: PAPERCLIP_API_KEY)",
    )
    sp.add_argument(
        "--jobmate-url",
        default=None,
        help="jobMate API base URL (기본: --jobmate-url > JOBMATE_URL env > http://127.0.0.1:8000)",
    )
    sp.add_argument(
        "--jobmate-api-key-env",
        default="JOBMATE_API_KEY",
        help="jobMate API 키가 담긴 환경변수 이름 (기본: JOBMATE_API_KEY)",
    )
    sp.add_argument(
        "--interval",
        type=float,
        default=60.0,
        help="폴링 주기(초, 최소 30초 — 기본 60)",
    )
    sp.add_argument("--once", action="store_true", help="1회 동기화 후 종료")

    sp = sub.add_parser("google-auth", help="Google OAuth 토큰 발급 (gmail/calendar)")
    sp.add_argument("kind", choices=["gmail", "calendar"])
    sp.add_argument(
        "--credentials",
        default=None,
        help="OAuth client JSON(credentials.json) 경로. --client-id/--client-secret 대신 사용",
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

    if args.cmd == "paperclip-bridge":
        return _paperclip_bridge(args)

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
                print("--credentials나 --client-id/--client-secret가 필요합니다", file=sys.stderr)
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

    # get_settings()의 load_dotenv() 이후 호출되므로 .env의 JOBMATE_URL도 반영된다.
    url = args.url or os.environ.get("JOBMATE_URL") or "http://127.0.0.1:8000"
    api_key = os.environ.get(args.api_key_env, "")
    if not api_key:
        logger.warning("agent-bridge: env %s not set — push will likely fail", args.api_key_env)
    bridge = Bridge(url, api_key or None)
    if args.once:
        counts = bridge.push()
        logger.info("agent-bridge once: %s", counts)
        return 0
    logger.info("agent-bridge start url=%s interval=%ss", url, args.interval)
    try:
        while True:
            counts = bridge.push()
            if counts["pushed"] or counts["ended"]:
                logger.info("agent-bridge push: %s", counts)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        logger.info("agent-bridge stopped")
    return 0


# paperclip-bridge 폴링 최소 주기(초) — spec E "--interval (최소값 적용)".
_PAPERCLIP_MIN_INTERVAL = 30.0


def _paperclip_bridge(args: argparse.Namespace) -> int:
    """Paperclip → jobMate external projection 브리지 (spec 3차 E).

    secret은 환경변수 우선 — argv에 키를 받지 않고 로그에도 남기지 않는다.
    동기화 실패는 safe 로그(오류 종류/상태만 — URL·페이로드 금지) 후
    다음 주기에 재시도한다."""
    from .paperclip_client import (
        PaperclipBridge,
        PaperclipClient,
        PaperclipError,
    )

    paperclip_url = args.paperclip_url or os.environ.get("PAPERCLIP_URL", "")
    company_id = args.company_id or os.environ.get("PAPERCLIP_COMPANY_ID", "")
    if not paperclip_url or not company_id:
        print(
            "paperclip-bridge: --paperclip-url/--company-id (또는 "
            "PAPERCLIP_URL/PAPERCLIP_COMPANY_ID env)가 필요합니다",
            file=sys.stderr,
        )
        return 2
    jobmate_url = args.jobmate_url or os.environ.get("JOBMATE_URL") or "http://127.0.0.1:8000"
    jm_key = os.environ.get(args.jobmate_api_key_env, "")
    pc_key = os.environ.get(args.paperclip_api_key_env, "")
    if not jm_key:
        logger.warning(
            "paperclip-bridge: env %s not set — push will likely fail",
            args.jobmate_api_key_env,
        )
    try:
        orch = PaperclipClient(
            paperclip_url,
            api_key=pc_key or None,
            company_id=company_id,
        )
    except PaperclipError as e:
        logger.error("paperclip-bridge config rejected: %s", type(e).__name__)
        print(f"paperclip-bridge: {e}", file=sys.stderr)
        return 2
    bridge = PaperclipBridge(
        orch,
        jobmate_url=jobmate_url,
        api_key=jm_key or None,
        company_id=company_id,
    )
    interval = max(float(args.interval), _PAPERCLIP_MIN_INTERVAL)
    if args.once:
        try:
            result = bridge.sync_once()
        except Exception as e:
            # URL·페이로드 없는 safe 로그 — 오류 클래스/HTTP 상태만.
            logger.warning("paperclip-bridge sync failed: %s", type(e).__name__)
            return 1
        logger.info(
            "paperclip-bridge once: agents=%s tasks=%s runs=%s replayed=%s",
            result.get("agents"),
            result.get("tasks"),
            result.get("runs"),
            result.get("replayed"),
        )
        bridge.close()
        orch.close()
        return 0
    logger.info("paperclip-bridge start interval=%ss", interval)
    try:
        while True:
            try:
                result = bridge.sync_once()
                logger.info(
                    "paperclip-bridge sync: agents=%s tasks=%s runs=%s replayed=%s",
                    result.get("agents"),
                    result.get("tasks"),
                    result.get("runs"),
                    result.get("replayed"),
                )
            except KeyboardInterrupt:
                raise
            except Exception as e:
                # safe 로그 — 예외 메시지에 URL/자격증명이 섞일 수 있으므로
                # 클래스명(+HTTP 상태)만 남긴다.
                status = getattr(e, "status_code", "")
                logger.warning(
                    "paperclip-bridge sync failed: %s%s",
                    type(e).__name__,
                    f" http_{status}" if status else "",
                )
            time.sleep(interval)
    except KeyboardInterrupt:
        logger.info("paperclip-bridge stopped")
    finally:
        bridge.close()
        orch.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
