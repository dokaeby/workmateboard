"""보안 감사 확정 항목 회귀 테스트.

커버 범위:
- ?secret= cron 쿼리 파라미터 폐기 (Bearer 헤더만)
- 이메일 인증 토큰 원자 consume + 재발급 시 구 토큰 무효화
- WorkflowSpec 구조 검증 (entry/branch 타겟/agent·function/상한)
- POST /auth/password — 현재 비밀번호 확인 + 전체 세션 revoke
- POST /runs 사이드이펙트 워크플로 lead 게이트
- validate_event aware/naive datetime 비교 TypeError → 정규화
- 메신저 per-user allowlist (JOBMATE_MESSENGER_ALLOWED_USERS)
- 인증 링크 Host allowlist (JOBMATE_PUBLIC_HOSTS)
"""

import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from jobmate import mailer, messenger
from jobmate.api.app import create_app
from jobmate.config import Settings
from jobmate.models import ExtractedEvent, WorkflowSpec
from jobmate.models.spec import RESERVED_STAGE_KEYS
from jobmate.security import hash_token
from jobmate.sensors.event_validation import validate_event
from jobmate.store.db import Store
from tests.integration.helpers import default_outputs, scripted_llm


def _client(monkeypatch=None, llm="scripted"):
    db = str(Path(tempfile.mkdtemp()) / "t.db")
    settings = Settings(db_path=db, budget_max_tokens=100_000)
    store = Store(db)
    kwargs = {"llm": scripted_llm(default_outputs())} if llm == "scripted" else {"llm": None}
    app = create_app(settings=settings, store=store, adapters={}, **kwargs)
    return TestClient(app), store


def _verify_session(client, store, email):
    """이메일 인증 링크로 세션 발급 — API 키 모드의 인증 요구를 통과."""
    token = store.create_email_verification(email)
    r = client.get(f"/api/v1/auth/verify?token={token}")
    assert r.status_code == 200
    return token


def _signup_verified(client, store, email, password="password123", name="U"):  # noqa: S107
    r = client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": password, "display_name": name, "agree_terms": True},
        headers={"X-API-Key": os.environ.get("JOBMATE_API_KEY") or "k"},
    )
    assert r.status_code == 201, r.text
    _verify_session(client, store, email)
    return r


# ---------- ?secret= cron 파라미터 폐기 ----------


def test_cron_tick_rejects_query_secret(monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "cron-key")
    monkeypatch.delenv("VERCEL", raising=False)
    client, _ = _client()
    # 쿼리스트링 secret은 더 이상 허용되지 않는다
    assert client.get("/api/v1/cron/tick?secret=cron-key").status_code == 401
    assert client.post("/api/v1/cron/tick?secret=cron-key").status_code == 401
    # Bearer 헤더는 계속 동작
    r = client.get("/api/v1/cron/tick", headers={"Authorization": "Bearer cron-key"})
    assert r.status_code == 200


# ---------- 이메일 인증 토큰 원자 consume + 구 토큰 무효화 ----------


def test_new_verification_invalidates_previous_token(monkeypatch):
    monkeypatch.setenv("JOBMATE_API_KEY", "k")
    client, store = _client()
    _signup_verified(client, store, "old-token@b.com")

    # 두 번째 발급 — 첫 토큰은 무효화되어야 한다
    tok1 = store.create_email_verification("old-token@b.com")
    tok2 = store.create_email_verification("old-token@b.com")
    assert tok1 != tok2

    r1 = client.get(f"/api/v1/auth/verify?token={tok1}")
    assert r1.status_code == 400
    r2 = client.get(f"/api/v1/auth/verify?token={tok2}")
    assert r2.status_code == 200


def test_consume_email_verification_atomic():
    """consume은 1회만 성공 — 두 번째 호출은 None."""
    store = Store(str(Path(tempfile.mkdtemp()) / "t.db"))
    token = store.create_email_verification("atomic@b.com")
    th = hash_token(token)
    assert store.consume_email_verification(th) == "atomic@b.com"
    assert store.consume_email_verification(th) is None
    # 존재하지 않는/만료 토큰도 None
    assert store.consume_email_verification(hash_token("nope")) is None
    store.close()


# ---------- WorkflowSpec 구조 검증 ----------


def _spec(**over):
    base = {
        "key": "cw",
        "version": "1.0.0",
        "entry": "s1",
        "stages": [{"key": "s1", "kind": "human", "description": "d"}],
    }
    base.update(over)
    return base


def test_workflow_spec_valid():
    spec = WorkflowSpec.model_validate(_spec())
    assert spec.entry == "s1"
    # finalize는 예약 종결 단계 — branch 타겟으로 허용
    spec2 = WorkflowSpec.model_validate(
        _spec(branches={"s1": {"decision": {"approve": "finalize"}}})
    )
    assert "finalize" in RESERVED_STAGE_KEYS
    assert spec2.branches["s1"]["decision"]["approve"] == "finalize"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda s: s.update(entry="missing"),
        lambda s: s.update(branches={"ghost": {"f": {"v": "s1"}}}),
        lambda s: s.update(branches={"s1": {"f": {"v": "missing"}}}),
        lambda s: s.update(stages=[{"key": "s1", "kind": "llm", "agent": ""}]),
        lambda s: s.update(stages=[{"key": "s1", "kind": "function", "function": "  "}]),
        lambda s: s.update(stages=[{"key": "s1", "kind": "llm", "agent": "bad name!"}]),
        lambda s: s.update(max_review_rounds=-1),
        lambda s: s.update(max_fix_rounds=101),
        lambda s: s.update(
            stages=[
                {"key": "s1", "kind": "human"},
                {"key": "s1", "kind": "human"},
            ]
        ),
    ],
)
def test_workflow_spec_invalid(mutate):
    d = _spec()
    mutate(d)
    with pytest.raises(ValidationError):
        WorkflowSpec.model_validate(d)


def test_builtin_workflows_still_valid():
    """신규 validator가 내장 spec을 깨지 않는다."""
    from jobmate.workflows import WORKFLOWS

    for key, spec in WORKFLOWS.items():
        assert WorkflowSpec.model_validate(spec.model_dump()).key == key


# ---------- POST /auth/password — 비밀번호 변경 + 세션 revoke ----------


def test_password_change_revokes_all_sessions(monkeypatch):
    monkeypatch.setenv("JOBMATE_API_KEY", "k")
    client, store = _client()
    _signup_verified(client, store, "pw@b.com", password="old-password-1")  # noqa: S106
    user = store.get_user_by_email("pw@b.com")
    assert user is not None

    # 틀린 현재 비밀번호 → 403
    bad = client.post(
        "/api/v1/auth/password",
        json={"current_password": "wrong", "new_password": "new-password-1"},
    )
    assert bad.status_code == 403

    # 짧은 새 비밀번호 → 400
    weak = client.post(
        "/api/v1/auth/password",
        json={"current_password": "old-password-1", "new_password": "short"},
    )
    assert weak.status_code == 400

    # 정상 변경 → 모든 세션 폐기
    ok = client.post(
        "/api/v1/auth/password",
        json={"current_password": "old-password-1", "new_password": "new-password-1"},
    )
    assert ok.status_code == 200
    assert store.list_user_sessions(user["id"]) == []

    # 이전 세션은 폐기됨 — 쿠키가 남아 있어도 인증 실패
    me = client.get("/api/v1/auth/me")
    # 로컬 폴백이 아니라 세션 기반이어야 함 — JOBMATE_API_KEY 설정으로 401
    assert me.status_code == 401

    # 새 비밀번호 로그인 성공, 이전 비밀번호 실패
    old_login = client.post(
        "/api/v1/auth/login", json={"email": "pw@b.com", "password": "old-password-1"}
    )
    assert old_login.status_code == 401
    new_login = client.post(
        "/api/v1/auth/login", json={"email": "pw@b.com", "password": "new-password-1"}
    )
    # 이메일 인증은 이미 완료 상태 — 로그인 성공
    assert new_login.status_code == 200


def test_password_change_requires_session():
    client, _ = _client()
    r = client.post(
        "/api/v1/auth/password",
        json={"current_password": "x" * 9, "new_password": "y" * 9},
    )
    assert r.status_code == 401


def test_sessions_list_and_revoke(monkeypatch):
    monkeypatch.setenv("JOBMATE_API_KEY", "k")
    client, store = _client()
    _signup_verified(client, store, "sess@b.com")

    r = client.get("/api/v1/auth/sessions")
    assert r.status_code == 200
    sessions = r.json()["sessions"]
    assert len(sessions) == 1
    assert sessions[0]["current"] is True
    sid = sessions[0]["id"]

    # 본인 세션 폐기 → 이후 /auth/me는 세션 없음(로컬 폴백이 아니라 401)
    d = client.delete(f"/api/v1/auth/sessions/{sid}")
    assert d.status_code == 200
    me = client.get("/api/v1/auth/me")
    assert me.status_code == 401

    # 없는 세션 id → 404
    client2, store2 = _client()
    _signup_verified(client2, store2, "sess2@b.com")
    assert client2.delete("/api/v1/auth/sessions/deadbeef").status_code == 404


# ---------- POST /runs 사이드이펙트 워크플로 lead 게이트 ----------


def _member_client(monkeypatch):
    """member role 세션 — 첫 사용자는 admin이 되므로 두 번째 계정을 member로."""
    monkeypatch.setenv("JOBMATE_API_KEY", "k")
    client, store = _client()
    _signup_verified(client, store, "admin@b.com", name="Admin")
    # 두 번째 가입은 member — 별도 세션으로 로그인
    client.post(
        "/api/v1/auth/signup",
        json={"email": "member@b.com", "password": "password123", "agree_terms": True},
    )
    _verify_session(client, store, "member@b.com")
    return client, store


def test_member_cannot_run_gmail_calendar(monkeypatch):
    client, _ = _member_client(monkeypatch)
    r = client.post("/api/v1/runs", json={"workflow": "gmail_calendar", "task": "t"})
    assert r.status_code == 403


def test_member_can_run_dev_review(monkeypatch):
    client, _ = _member_client(monkeypatch)
    r = client.post("/api/v1/runs", json={"workflow": "dev_review", "task": "t"})
    assert r.status_code == 202


def test_admin_can_run_gmail_calendar(monkeypatch):
    monkeypatch.setenv("JOBMATE_API_KEY", "k")
    client, store = _client()
    _signup_verified(client, store, "boss@b.com")  # 첫 사용자 → admin
    r = client.post("/api/v1/runs", json={"workflow": "gmail_calendar", "task": "t"})
    assert r.status_code == 202


def test_custom_workflow_side_effect_needs_lead(monkeypatch):
    """커스텀 spec의 function stage가 부수효과 함수면 member 실행 403."""
    monkeypatch.setenv("JOBMATE_API_KEY", "k")
    client, store = _client()
    _signup_verified(client, store, "lead@b.com")

    # gmail_register(실제 캘린더 쓰기) 사용 커스텀 워크플로 — lead가 등록
    spec = {
        "key": "cw_sidefx",
        "version": "1.0.0",
        "entry": "reg",
        "stages": [{"key": "reg", "kind": "function", "function": "gmail_register"}],
    }
    r = client.post("/api/v1/workflows/custom", json={"name": "cw_sidefx", "spec": spec})
    assert r.status_code == 201, r.text

    # member 계정으로 실행 시도 → 403
    client.post(
        "/api/v1/auth/signup",
        json={"email": "m2@b.com", "password": "password123", "agree_terms": True},
    )
    _verify_session(client, store, "m2@b.com")
    r2 = client.post("/api/v1/runs", json={"workflow": "cw_sidefx", "task": "t"})
    assert r2.status_code == 403

    # 읽기성(llm/human만) 커스텀 워크플로는 member 허용
    spec2 = {
        "key": "cw_safe",
        "version": "1.0.0",
        "entry": "s1",
        "stages": [{"key": "s1", "kind": "human", "description": "d"}],
    }
    r3 = client.post("/api/v1/workflows/custom", json={"name": "cw_safe", "spec": spec2})
    # member는 custom workflow 등록 자체가 lead 전용 — admin 세션으로 재로그인 필요.
    # 여기서는 등록이 member 세션으로 403인지 먼저 확인한다.
    assert r3.status_code == 403


# ---------- validate_event aware/naive 혼합 ----------


def test_validate_event_mixed_timezone_no_typeerror():
    """aware start + naive end (또는 반대)도 TypeError 없이 판정."""
    ev = ExtractedEvent(
        title="회의",
        start_at="2026-01-01T10:00:00+09:00",  # aware
        end_at="2026-01-01T11:00:00",  # naive
        timezone="Asia/Seoul",
        confidence=0.9,
    )
    ok, reason = validate_event(ev)
    assert ok is True and reason == "ok"

    # naive가 이벤트 tz로 해석되어 end<=start도 올바르게 판정
    ev_bad = ExtractedEvent(
        title="회의",
        start_at="2026-01-01T11:00:00",  # naive → Asia/Seoul
        end_at="2026-01-01T10:00:00+09:00",  # aware
        timezone="Asia/Seoul",
        confidence=0.9,
    )
    ok2, reason2 = validate_event(ev_bad)
    assert ok2 is False and reason2 == "end_before_start"


# ---------- 메신저 per-user allowlist ----------


def _telegram_payload(sender_id=999, chat_id=555, text="도움말", msg_id=1):
    return {
        "message": {
            "from": {"id": sender_id, "is_bot": False},
            "chat": {"id": chat_id},
            "text": text,
            "message_id": msg_id,
        }
    }


def test_messenger_allowed_users_blocks_approve(monkeypatch):
    """JOBMATE_MESSENGER_ALLOWED_USERS 설정 시 목록 외 사용자의 승인/시작 거부."""
    monkeypatch.setenv("JOBMATE_MESSENGER_ALLOW_ALL", "1")
    monkeypatch.setenv("JOBMATE_MESSENGER_ALLOWED_USERS", "111,222")
    monkeypatch.setenv("JOBMATE_TELEGRAM_BOT_TOKEN", "tok")
    replies: list[str] = []
    monkeypatch.setattr(
        messenger, "send_telegram", lambda token, chat_id, text: replies.append(text) or True
    )
    db = str(Path(tempfile.mkdtemp()) / "t.db")
    settings = Settings(db_path=db, budget_max_tokens=100_000, telegram_webhook_secret="sec")  # noqa: S106
    store = Store(db)
    app = create_app(settings=settings, store=store, llm=None, adapters={})
    client = TestClient(app)

    # 허용 목록 외 사용자(999)의 승인 커맨드 → 권한 없음 답장
    r = client.post(
        "/api/v1/webhooks/telegram",
        json=_telegram_payload(sender_id=999, text="승인", msg_id=1),
        headers={"X-Telegram-Bot-Api-Secret-Token": "sec"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "command"
    assert replies and "권한이 없습니다" in replies[-1]

    # help는 읽기 전용이라 allowlist와 무관하게 동작
    r2 = client.post(
        "/api/v1/webhooks/telegram",
        json=_telegram_payload(sender_id=999, text="도움말", msg_id=2),
        headers={"X-Telegram-Bot-Api-Secret-Token": "sec"},
    )
    assert r2.json()["status"] == "command"
    assert "커맨드" in replies[-1]

    # 허용 사용자(111)의 승인 커맨드 → 권한 검사 통과(실행은 run 없어 실패 답장)
    r3 = client.post(
        "/api/v1/webhooks/telegram",
        json=_telegram_payload(sender_id=111, text="승인", msg_id=3),
        headers={"X-Telegram-Bot-Api-Secret-Token": "sec"},
    )
    assert r3.json()["status"] == "command"
    assert "권한이 없습니다" not in replies[-1]
    assert "승인 대기 중인 run이 없습니다" in replies[-1]
    store.close()


def test_messenger_allowed_users_unset_backward_compat(monkeypatch):
    """env 미설정 시 기존 동작 — 채널 allowlist만 적용, 커맨드 실행 가능."""
    monkeypatch.setenv("JOBMATE_MESSENGER_ALLOW_ALL", "1")
    monkeypatch.delenv("JOBMATE_MESSENGER_ALLOWED_USERS", raising=False)
    monkeypatch.setenv("JOBMATE_TELEGRAM_BOT_TOKEN", "tok")
    replies: list[str] = []
    monkeypatch.setattr(
        messenger, "send_telegram", lambda token, chat_id, text: replies.append(text) or True
    )
    db = str(Path(tempfile.mkdtemp()) / "t.db")
    settings = Settings(db_path=db, budget_max_tokens=100_000, telegram_webhook_secret="sec")  # noqa: S106
    store = Store(db)
    app = create_app(settings=settings, store=store, llm=None, adapters={})
    client = TestClient(app)
    r = client.post(
        "/api/v1/webhooks/telegram",
        json=_telegram_payload(sender_id=999, text="승인", msg_id=1),
        headers={"X-Telegram-Bot-Api-Secret-Token": "sec"},
    )
    assert r.json()["status"] == "command"
    assert "승인 대기 중인 run이 없습니다" in replies[-1]
    store.close()


# ---------- 인증 링크 Host allowlist ----------


def test_verification_blocked_for_unknown_host(monkeypatch):
    """JOBMATE_PUBLIC_HOSTS 불일치 호스트 → 인증 메일 발송 거부."""
    monkeypatch.setenv("JOBMATE_API_KEY", "k")
    monkeypatch.setenv("JOBMATE_PUBLIC_HOSTS", "jobmate.example.com")
    mails: list[tuple[str, str]] = []

    def fake_send(to: str, verify_url: str, gmail=None) -> bool:
        mails.append((to, verify_url))
        return True

    monkeypatch.setattr(mailer, "send_verification_email", fake_send)
    client, store = _client()
    # TestClient 기본 host는 testserver — 허용 목록에 없으므로 발송 거부
    r = client.post(
        "/api/v1/auth/signup",
        json={"email": "host@b.com", "password": "password123", "agree_terms": True},
        headers={"X-API-Key": "k", "Host": "evil.example.com"},
    )
    assert r.status_code == 201
    assert r.json()["verify_email_sent"] is False
    assert mails == []
    # 허용 호스트로는 발송됨
    r2 = client.post(
        "/api/v1/auth/signup",
        json={"email": "host2@b.com", "password": "password123", "agree_terms": True},
        headers={"X-API-Key": "k", "Host": "jobmate.example.com"},
    )
    assert r2.status_code == 201
    assert r2.json()["verify_email_sent"] is True
    assert mails and "jobmate.example.com" in mails[-1][1]
    store.close()


def test_verify_url_uses_public_base_url(monkeypatch):
    """PUBLIC_BASE_URL 설정 시 요청 Host와 무관하게 그 base로 링크 생성."""
    monkeypatch.setenv("JOBMATE_API_KEY", "k")
    mails: list[tuple[str, str]] = []

    def fake_send(to: str, verify_url: str, gmail=None) -> bool:
        mails.append((to, verify_url))
        return True

    monkeypatch.setattr(mailer, "send_verification_email", fake_send)
    db = str(Path(tempfile.mkdtemp()) / "t.db")
    settings = Settings(
        db_path=db, budget_max_tokens=100_000, public_base_url="https://jobmate.example.com"
    )
    store = Store(db)
    app = create_app(settings=settings, store=store, llm=None, adapters={})
    client = TestClient(app)
    r = client.post(
        "/api/v1/auth/signup",
        json={"email": "base@b.com", "password": "password123", "agree_terms": True},
        headers={"X-API-Key": "k"},
    )
    assert r.status_code == 201
    assert mails and mails[0][1].startswith("https://jobmate.example.com/api/v1/auth/verify")
    store.close()


# ---------- 로그인 타이밍 오라클 (동일 코드 경로 검증) ----------


def test_login_unknown_and_google_only_both_401(monkeypatch):
    """미가입/Google-only/비밀번호 계정 모두 동일 401 — dummy scrypt 경로 포함."""
    monkeypatch.setenv("JOBMATE_API_KEY", "k")
    client, store = _client()
    # Google-only 계정 (password_hash 빈 값)
    store.create_user("googleonly@b.com", "", "G")
    # 비밀번호 계정
    _signup_verified(client, store, "pw2@b.com")

    for email in ("nobody@b.com", "googleonly@b.com", "pw2@b.com"):
        r = client.post("/api/v1/auth/login", json={"email": email, "password": "whatever1"})
        assert r.status_code == 401, (email, r.status_code)
        assert r.json()["error"]["code"] == "invalid_credentials"
    store.close()


# ---------- JOBMATE_PUBLIC_HOSTS opt-in (미설정 시 기존 계약대로 허용) ----------


def test_verification_sent_when_public_hosts_unset(monkeypatch):
    """PUBLIC_HOSTS 미설정 배포에서 fail-closed하면 모든 인증 메일이 거부돼
    가입이 무력화된다 — 미설정 시에는 허용한다(04 문서 계약)."""
    monkeypatch.setenv("JOBMATE_API_KEY", "k")
    monkeypatch.delenv("JOBMATE_PUBLIC_HOSTS", raising=False)
    monkeypatch.delenv("JOBMATE_PUBLIC_BASE_URL", raising=False)
    mails: list[tuple[str, str]] = []

    def fake_send(to: str, verify_url: str, gmail=None) -> bool:
        mails.append((to, verify_url))
        return True

    monkeypatch.setattr(mailer, "send_verification_email", fake_send)
    client, store = _client()
    r = client.post(
        "/api/v1/auth/signup",
        json={"email": "open@b.com", "password": "password123", "agree_terms": True},
        headers={"X-API-Key": "k", "Host": "anything.example.com"},
    )
    assert r.status_code == 201
    assert r.json()["verify_email_sent"] is True
    assert mails
    store.close()


# ---------- 무키 모드 원격 세션 발급 차단 ----------


def _remote_open_client(monkeypatch):
    """인증 완전 비활성(키·허용목록·VERCEL 없음) + 비-loopback 요청 시뮬레이션."""
    for v in ("JOBMATE_API_KEY", "JOBMATE_ALLOWED_EMAILS", "VERCEL"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr("jobmate.security.is_loopback_request", lambda request: False)
    return _client()


def test_remote_signup_denied_in_open_mode(monkeypatch):
    """무키 배포가 외부에 노출돼도 원격에서는 세션 발급 경로가 닫혀 있어야 한다."""
    client, store = _remote_open_client(monkeypatch)
    r = client.post(
        "/api/v1/auth/signup",
        json={"email": "remote@b.com", "password": "password123", "agree_terms": True},
    )
    assert r.status_code == 403
    assert store.get_user_by_email("remote@b.com") is None
    store.close()


def test_remote_login_denied_in_open_mode(monkeypatch):
    client, store = _remote_open_client(monkeypatch)
    r = client.post("/api/v1/auth/login", json={"email": "a@b.com", "password": "password123"})
    assert r.status_code == 403
    store.close()


def test_remote_verify_issues_no_session(monkeypatch):
    """인증 링크 자체는 유효 처리하되(이메일 인증은 완료) 세션 쿠키는 발급하지 않는다."""
    client, store = _remote_open_client(monkeypatch)
    store.create_user("verify@b.com", "", "V")
    token = store.create_email_verification("verify@b.com")
    r = client.get(f"/api/v1/auth/verify?token={token}")
    assert r.status_code == 200
    assert store.get_user_by_email("verify@b.com")["email_verified"] is True
    assert "jobmate_session" not in r.headers.get("set-cookie", "")
    store.close()


def test_loopback_signup_still_issues_session(monkeypatch):
    """로컬 개발 모드(loopback)는 기존대로 가입 즉시 세션 발급."""
    for v in ("JOBMATE_API_KEY", "JOBMATE_ALLOWED_EMAILS", "VERCEL"):
        monkeypatch.delenv(v, raising=False)
    client, store = _client()
    r = client.post(
        "/api/v1/auth/signup",
        json={"email": "local@b.com", "password": "password123", "agree_terms": True},
    )
    assert r.status_code == 201
    assert "jobmate_session" in r.headers.get("set-cookie", "")
    store.close()
