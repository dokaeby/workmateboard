"""테스트 격리 — 개발용 .env의 실제 비밀값/키가 테스트에 새지 않도록 무력화.

`jobmate.api.app` 모듈은 import 시 `create_app()`을 실행하고, `get_settings()`가
`load_dotenv()`로 .env를 os.environ에 로드한다. pop으로 지우면 dotenv가 다시
로드하므로, 빈 문자열로 설정해 덮어쓰기를 막는다(override=False 기본 동작).
빈 값은 `bool(env)` 검사에서 비활성으로 처리된다.
"""

import os

_SECRET_EXACT = {
    "JOBMATE_API_KEY",
    "CRON_SECRET",
    "JOBMATE_CRON_SECRET",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GITHUB_TOKEN",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "LANGSMITH_API_KEY",
    "CRONJOB_ORG_API_KEY",
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
    "GOOGLE_REDIRECT_URI",
    "POSTGRES_URL",
    "POSTGRES_PRISMA_URL",
    "VAPID_PUBLIC_KEY",
    "VAPID_PRIVATE_KEY",
}
_SECRET_PREFIXES = ("VAPID_", "CRONJOB_")
_SECRET_SUFFIXES = ("_TOKEN_JSON", "_API_KEY")

# .env에 실제로 존재하는 키는 이름을 알고 있으므로 무조건 빈 값으로 고정한다.
# (패턴 매칭은 쉘에서 export된 경우만 커버 — .env 로드 전이라 environ에 없을 수 있음)
for _name in _SECRET_EXACT:
    os.environ[_name] = ""

for _name in list(os.environ):
    if _name == "LANGCHAIN_API_KEY":
        continue  # configure_langsmith가 setdefault로 설정하는 파생 변수
    if _name.startswith(_SECRET_PREFIXES) or _name.endswith(_SECRET_SUFFIXES):
        os.environ[_name] = ""

# Google 토큰 "경로"는 비밀 패턴에 걸리지 않아 .env의 실제 토큰 파일을 그대로 쓴다 —
# 테스트가 진짜 Gmail/Calendar API를 호출해 실제 메일이 발송되는 것을 막기 위해
# 존재하지 않는 경로로 고정한다(_token_path_for는 is_file() 실패 시 어댑터 생략).
for _name in ("JOBMATE_GMAIL_TOKEN_PATH", "JOBMATE_CALENDAR_TOKEN_PATH"):
    os.environ[_name] = "/nonexistent/jobmate-test-token.json"

# spec 005 설정 — 개발 .env/쉘 값이 테스트 기대(기본 off·버전 v1.0)를
# 오염시키지 않게 빈 값으로 고정한다. Settings의 before-validator가
# terms/privacy_version ''→'v1.0', consent_enforce ''→'off'로 복원하고
# pii_mask ''는 off와 동일하게 동작한다.
for _name in (
    "JOBMATE_TERMS_VERSION",
    "JOBMATE_PRIVACY_VERSION",
    "JOBMATE_PRIVACY_POLICY_URL",
    "JOBMATE_CONSENT_ENFORCE",
    "JOBMATE_PII_MASK",
):
    os.environ[_name] = ""

# 알림 채널/가입 허용목록 — 개발 .env의 실제 webhook·SMTP 자격증명이
# 테스트에서 외부 발송을 일으키지 않게 빈 값으로 고정한다
# (services/alert_rules.py·mailer.py·messenger.py·security.py가 읽는
# 실제 env 이름 기준).
for _name in (
    "JOBMATE_SLACK_WEBHOOK_URL",
    "JOBMATE_TELEGRAM_BOT_TOKEN",
    "JOBMATE_TELEGRAM_CHAT_ID",
    "JOBMATE_SMTP_HOST",
    "JOBMATE_SMTP_PORT",
    "JOBMATE_SMTP_USER",
    "JOBMATE_SMTP_PASS",
    "JOBMATE_SMTP_FROM",
    "JOBMATE_SMTP_TLS",
    "JOBMATE_ALLOWED_EMAILS",
    "VAPID_SUBJECT",
):
    os.environ[_name] = ""


import pytest  # noqa: E402

# get_settings()는 호출마다 load_dotenv()를 실행한다. lru_cache가 살아 있는
# 동안은 1회뿐이지만, _clear_settings_cache가 테스트마다 캐시를 비우면
# 재호출 시 .env가 다시 로드된다 — 이때 테스트 픽스처가 delenv로 지운
# JOBMATE_API_KEY 등이 .env 실제값으로 되살아나 인증이 깨진다(회귀).
# 최초 1회만 실제 로드하고 이후 호출은 no-op으로 만든다.
import jobmate.config as _jobmate_config  # noqa: E402

_dotenv_loaded = {"done": False}
_dotenv_orig = _jobmate_config.load_dotenv


def _load_dotenv_once(*args: object, **kwargs: object) -> bool:
    if _dotenv_loaded["done"]:
        return False
    _dotenv_loaded["done"] = True
    return bool(_dotenv_orig(*args, **kwargs))


_jobmate_config.load_dotenv = _load_dotenv_once


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """get_settings() lru_cache를 테스트마다 비운다 — monkeypatch.setenv로
    바꾼 env가 다음 get_settings() 호출에 반영되게 한다."""
    from jobmate.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
