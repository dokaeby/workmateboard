#!/bin/sh
# jobMate agent-bridge 상시 실행 래퍼 — launchd plist 또는 수동 실행용.
# .env(JOBMATE_API_KEY, JOBMATE_URL)는 jobmate CLI가 get_settings()에서 자동 로드한다.
set -eu
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
exec "$ROOT/.venv/bin/jobmate" agent-bridge "$@"
