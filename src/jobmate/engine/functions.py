"""function 단계 구현 — 모델 없이 결정적 검사/외부 부수효과.

구현은 fn_common/fn_calendar/fn_gmail/fn_radar 모듈에 나뉘어 있고,
이 모듈은 FUNCTIONS 레지스트리와 기존 import 경로 호환을 위한 재수출을 제공한다.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..sensors import git_apply_check, looks_like_diff, scan_secrets

# 재수출 — `from jobmate.engine.functions import X` / fn_mod.X 접근 호환 유지
from .fn_calendar import (
    _event_key as _event_key,
)
from .fn_calendar import (
    calendar_conflict_check as calendar_conflict_check,
)
from .fn_common import (
    FunctionContext as FunctionContext,
)
from .fn_common import (
    _prev_output as _prev_output,
)
from .fn_common import (
    _src as _src,
)
from .fn_common import (
    send_web_push_text as send_web_push_text,
)
from .fn_gmail import (
    _schedule_event_notifications as _schedule_event_notifications,
)
from .fn_gmail import (
    _send_web_push as _send_web_push,
)
from .fn_gmail import (
    gmail_notify as gmail_notify,
)
from .fn_gmail import (
    gmail_register as gmail_register,
)
from .fn_gmail import (
    gmail_validate as gmail_validate,
)
from .fn_radar import (
    _radar_budget_tracker as _radar_budget_tracker,
)
from .fn_radar import (
    _radar_llm_models as _radar_llm_models,
)
from .fn_radar import (
    _radar_notify as _radar_notify,
)
from .fn_radar import (
    _radar_platform_name as _radar_platform_name,
)
from .fn_radar import (
    _radar_platforms as _radar_platforms,
)
from .fn_radar import (
    _radar_weekly_digest as _radar_weekly_digest,
)
from .fn_radar import (
    _skipped_budget_item as _skipped_budget_item,
)
from .fn_radar import (
    _tracker_sync as _tracker_sync,
)
from .fn_radar import (
    radar_collect as radar_collect,
)
from .fn_radar import (
    radar_dedupe as radar_dedupe,
)
from .fn_radar import (
    radar_publish as radar_publish,
)
from .fn_radar import (
    radar_rank as radar_rank,
)
from .fn_radar import (
    radar_summarize as radar_summarize,
)
from .fn_radar import (
    radar_verify as radar_verify,
)


def auto_checks(ctx: FunctionContext) -> dict[str, Any]:
    """구현 산출물의 결정적 검사. diff 형식/시크릿/apply check/설정 명령."""
    impl = _prev_output(ctx, "implement")
    diff = impl.get("diff", "")
    checks: list[dict[str, Any]] = []
    passed = True

    if not diff.strip():
        checks.append({"name": "diff_present", "passed": False, "detail": "empty diff"})
        passed = False
    else:
        checks.append({"name": "diff_present", "passed": True})
        if not looks_like_diff(diff):
            checks.append(
                {"name": "diff_format", "passed": False, "detail": "no 'diff --git' header"}
            )
            passed = False
        else:
            checks.append({"name": "diff_format", "passed": True})
        ok, detail = git_apply_check(diff)
        checks.append({"name": "git_apply_check", "passed": ok, "detail": detail})
        passed = passed and ok

    hits = scan_secrets(diff)
    checks.append(
        {"name": "secret_scan", "passed": not hits, "detail": ",".join(hits) if hits else "clean"}
    )
    passed = passed and not hits

    for cmd in ctx.settings.check_commands:
        try:
            proc = subprocess.run(  # noqa: S603
                cmd.split(),
                capture_output=True,
                text=True,
                timeout=120,
                cwd=Path.cwd(),
            )
            ok = proc.returncode == 0
        except (OSError, subprocess.TimeoutExpired) as e:
            ok, proc = False, None
            detail = str(e)
        else:
            detail = (proc.stdout + proc.stderr)[-500:] if proc else ""
        checks.append({"name": f"cmd:{cmd}", "passed": ok, "detail": detail})
        passed = passed and ok

    return {"passed": passed, "checks": checks}


FUNCTIONS: dict[str, Callable[[FunctionContext], dict[str, Any]]] = {
    "auto_checks": auto_checks,
    "gmail_validate": gmail_validate,
    "gmail_register": gmail_register,
    "gmail_notify": gmail_notify,
    "calendar_conflict_check": calendar_conflict_check,
    "radar_collect": radar_collect,
    "radar_dedupe": radar_dedupe,
    "radar_summarize": radar_summarize,
    "radar_verify": radar_verify,
    "radar_rank": radar_rank,
    "radar_publish": radar_publish,
}
