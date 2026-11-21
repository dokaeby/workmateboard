"""UI 자동화 테스트 — 실제 uvicorn 서버 + headless Chromium (Playwright).

docs/jobMate_v1.0.0/13·14 의 시나리오 중 현재 구현된 화면 흐름을 커버한다.
LLM/외부 어댑터 없이 빈 DB + fake 어댑터로 결정적으로 동작한다.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import uvicorn

pytest.importorskip("playwright.sync_api")

from playwright.sync_api import Page, sync_playwright

from jobmate.api.app import create_app
from jobmate.config import Settings
from jobmate.llm import FakeLLM
from jobmate.store.db import Store


def _ui_llm_handler(system: str, user: str, model: Any, schema: Any) -> dict[str, Any]:
    """결정적 코딩 에이전트 LLM.

    - "SLOWLOOP" 태스크: 매 스텝 0.4s 지연 + ls만 반복 → 취소 테스트용 장기 실행
    - 그 외: write_file 1스텝 후 final → 빠른 done 경로
    """
    if "SLOWLOOP" in user:
        time.sleep(0.4)
        return {"action": "tool", "thought": "폴링", "tool": "ls", "args": {"path": "."}}
    if "STEPS SO FAR" in user:
        return {"action": "final", "answer": "UI 테스트 답변"}
    return {
        "action": "tool",
        "thought": "파일 생성",
        "tool": "write_file",
        "args": {"path": "ui-check.txt", "content": "ok"},
    }


@pytest.fixture(scope="module")
def ui_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[SimpleNamespace]:
    """격리된 임시 DB + FakeLLM으로 실제 uvicorn 서버를 띄운다."""
    tmp = tmp_path_factory.mktemp("ui")
    settings = Settings(
        db_path=tmp / "jm.db",
        radar_platforms=[],
        worker_interval_seconds=60,
        gmail_poll_interval_seconds=3600,
    )
    store = Store(str(settings.db_path))
    llm = FakeLLM(_ui_llm_handler)

    class _FakeCalendar:
        def __init__(self) -> None:
            self.created: list[dict[str, Any]] = []

        def find_by_marker(self, marker: str) -> str | None:
            return None

        def create_event(self, **kwargs: Any) -> str:
            self.created.append(kwargs)
            return "ui-cal-1"

    app = create_app(
        settings=settings,
        store=store,
        llm=llm,
        adapters={"calendar": _FakeCalendar()},
    )

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    httpx.get(url + "/api/v1/health", timeout=2)
    yield SimpleNamespace(url=url, store=store)
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(scope="module")
def base_url(ui_server: SimpleNamespace) -> str:
    return ui_server.url


@pytest.fixture()
def page(ui_server: SimpleNamespace) -> Iterator[Page]:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        pg = browser.new_page()
        pg.goto(ui_server.url)
        pg.wait_for_selector(".nav-item")
        yield pg
        browser.close()


def _open_view(page: Page, view: str, title: str) -> None:
    page.click(f'.nav-item[data-view="{view}"]')
    section = page.locator(f"#view-{view}")
    assert section.is_visible()
    title_sel = ".sr-title" if view == "radar" else ".page-title"
    assert page.locator(f"#view-{view} {title_sel}").inner_text() == title


def test_sidebar_navigation(page: Page) -> None:
    """사이드바 5개 메뉴가 각각 우측 화면을 전환한다."""
    for view, title in [
        ("gmail", "Gmail Checker"),
        ("coding", "코딩 에이전트"),
        ("calendar", "Calendar"),
        ("radar", "SkillRadar"),
        ("runs", "실행"),
    ]:
        _open_view(page, view, title)


def _wait_text(page: Page, selector: str, text: str) -> None:
    page.wait_for_function(
        f"document.querySelector('{selector}')"
        f" && document.querySelector('{selector}').innerText.includes('{text}')",
        timeout=5000,
    )


def test_gmail_view_empty_state(page: Page) -> None:
    """메시지가 없으면 빈 상태 문구를 표시한다."""
    page.click('.nav-item[data-view="gmail"]')
    _wait_text(page, "#gmail-cards", "메시지 없음")


def test_calendar_view_empty_state(page: Page) -> None:
    page.click('.nav-item[data-view="calendar"]')
    _wait_text(page, "#calendar-cards", "등록된 일정 없음")


def test_radar_view_platforms(page: Page) -> None:
    page.click('.nav-item[data-view="radar"]')
    labels = page.locator("#sr-platform-tabs button").all_inner_texts()
    assert labels == ["Claude", "ChatGPT", "Gemini"]


def test_runs_view_table(page: Page) -> None:
    page.click('.nav-item[data-view="runs"]')
    _wait_text(page, "#runs-body", "실행 없음")


def test_coding_view_tool_options(page: Page) -> None:
    """코딩 에이전트 뷰에 실행 방식(내장 에이전트/CLI)과 도구 목록이 로드된다."""
    page.click('.nav-item[data-view="coding"]')
    page.wait_for_function("document.querySelectorAll('#coding-tool option').length > 0")
    values = page.locator("#coding-tool option").evaluate_all("els => els.map(e => e.value)")
    assert set(values) == {"claude", "codex", "antigravity", "opencode"}
    # 기본 실행 방식은 내장 에이전트, 선택 시 CLI 필드가 비활성화된다
    assert page.locator("#coding-engine").input_value() == "agent"
    assert page.locator("#coding-tool").is_disabled()
    page.select_option("#coding-engine", "cli")
    assert not page.locator("#coding-tool").is_disabled()


def test_settings_dialog_save(page: Page, base_url: str) -> None:
    """설정 다이얼로그에서 값을 바꿔 저장하면 API에 반영된다."""
    page.click("#settings-open")
    page.wait_for_function("document.querySelector('#settings-dialog').open")
    page.wait_for_function("document.querySelector('#set-gmail-minutes').value !== ''")
    page.select_option("#set-gmail-minutes", "60")
    page.select_option("#set-radar-interval", "weekly")
    page.select_option("#set-coding-tool", "codex")
    page.click("#settings-save")
    page.wait_for_function(
        "document.querySelector('#settings-status').textContent.includes('저장했습니다')",
        timeout=5000,
    )
    resp = page.request.get(base_url + "/api/v1/settings")
    prefs = resp.json()
    assert prefs["gmail_check_minutes"] == 60
    assert prefs["radar_interval"] == "weekly"
    assert prefs["coding_tool"] == "codex"


def test_push_toggle_without_vapid(page: Page) -> None:
    """VAPID 키 미설정 시 알림 켜기가 alert로 안내하고 화면이 유지된다."""
    messages: list[str] = []
    page.on("dialog", lambda d: (messages.append(d.message), d.dismiss()))
    page.click("#push-toggle")
    page.wait_for_timeout(1000)
    assert any("VAPID" in m or "지원" in m for m in messages)
    assert page.locator("#push-toggle").is_visible()


# ---------- 코딩 에이전트 실행 흐름 ----------


def _open_coding(page: Page) -> None:
    page.click('.nav-item[data-view="coding"]')
    page.wait_for_function("document.querySelectorAll('#coding-tool option').length > 0")


def _run_agent_task(page: Page, task: str) -> None:
    page.fill("#coding-task", task)
    page.click("#coding-run")


def test_coding_run_streams_to_done(page: Page) -> None:
    """실행 → SSE 스트림 → 완료 상태/답변/스텝 렌더링."""
    _open_coding(page)
    _run_agent_task(page, "ui-check.txt 파일을 만들어줘")
    _wait_text(page, "#coding-status", "완료")
    _wait_text(page, "#coding-result", "UI 테스트 답변")
    _wait_text(page, "#coding-result", "에이전트 스텝")
    # 완료 후 취소 버튼은 숨겨진다
    assert page.locator("#coding-cancel").is_hidden()


def test_coding_run_sse_fallback_polling(page: Page) -> None:
    """SSE 스트림이 실패하면 폴링 폴백으로도 완료 상태에 도달한다."""
    page.route("**/api/v1/coding/jobs/*/events", lambda route: route.abort())
    _open_coding(page)
    _run_agent_task(page, "폴백 경로 테스트")
    _wait_text(page, "#coding-status", "완료")
    _wait_text(page, "#coding-result", "UI 테스트 답변")


def test_coding_cancel_running_job(page: Page) -> None:
    """실행 중 취소 버튼 → cancel_requested → cancelled 상태 표시."""
    _open_coding(page)
    _run_agent_task(page, "SLOWLOOP 계속 실행해줘")
    _wait_text(page, "#coding-status", "실행 중")
    page.click("#coding-cancel")
    _wait_text(page, "#coding-status", "취소됨")
    assert page.locator("#coding-cancel").is_hidden()


def test_coding_recent_jobs_click_loads_result(ui_server: SimpleNamespace, page: Page) -> None:
    """최근 작업 목록의 완료 job 카드를 클릭하면 저장된 결과가 렌더링된다."""
    store = ui_server.store
    jid = "uihist0001"
    now = "2025-01-01T00:00:00+00:00"
    with store.tx() as c:
        c.execute(
            "INSERT INTO coding_jobs(id,engine,tool,model,task,workspace,status,ok,"
            " text,answer,steps,diff,error,attempts,max_attempts,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                jid,
                "agent",
                "agent",
                "",
                "이전 완료 작업",
                "workspace-dir",
                "done",
                1,
                "저장된 출력",
                "저장된 답변",
                '[{"step":1,"thought":"생각","tool":"write_file",'
                '"args":{"path":"a.txt"},"result":"wrote"}]',
                "diff --git a/a.txt b/a.txt",
                "",
                1,
                2,
                now,
                now,
            ),
        )
    _open_coding(page)
    card = page.locator(f'#coding-jobs [data-job="{jid}"]')
    card.wait_for(timeout=5000)
    card.click()
    _wait_text(page, "#coding-status", "불러옴")
    _wait_text(page, "#coding-result", "저장된 답변")
    _wait_text(page, "#coding-result", "에이전트 스텝")
    _wait_text(page, "#coding-result", "diff --git")


def _seed_review_event(store: Store, event_id: str) -> None:
    store.save_scheduled_event(
        event_id=event_id,
        source_provider="gmail",
        source_id=event_id.split(":", 1)[-1],
        title="UI 검토회의",
        start_at="not-a-date",
        end_at="2025-01-10T11:00:00+09:00",
        timezone="Asia/Seoul",
        status="needs_review",
        confidence=0.3,
        reason="bad_datetime",
    )


def test_calendar_review_reject(page: Page, ui_server: SimpleNamespace) -> None:
    """검토 필요 카드 표시 → 거부 시 rejected 전이 후 카드 제거."""
    _seed_review_event(ui_server.store, "ui:rev-reject")
    page.click('.nav-item[data-view="calendar"]')
    card = page.locator('[data-review="ui:rev-reject"]')
    card.wait_for(timeout=5000)
    assert "검토 필요" in page.locator("#calendar-review h3").inner_text()
    assert card.locator(".rv-title").input_value() == "UI 검토회의"
    card.locator(".rv-reject").click()
    _wait_text(page, '[data-review="ui:rev-reject"] .rv-msg', "거부됨")
    page.wait_for_selector('[data-review="ui:rev-reject"]', state="detached", timeout=5000)
    assert ui_server.store.get_scheduled_event("ui:rev-reject")["status"] == "rejected"


def test_calendar_review_approve_with_edit(page: Page, ui_server: SimpleNamespace) -> None:
    """수정값으로 승인 → 캘린더 등록 → registered 카드로 이동."""
    _seed_review_event(ui_server.store, "ui:rev-approve")
    page.click('.nav-item[data-view="calendar"]')
    card = page.locator('[data-review="ui:rev-approve"]')
    card.wait_for(timeout=5000)
    card.locator(".rv-start").fill("2025-01-10T10:00:00+09:00")
    card.locator(".rv-end").fill("2025-01-10T11:00:00+09:00")
    card.locator(".rv-approve").click()
    _wait_text(page, '[data-review="ui:rev-approve"] .rv-msg', "등록됨")
    page.wait_for_selector('[data-review="ui:rev-approve"]', state="detached", timeout=5000)
    ev = ui_server.store.get_scheduled_event("ui:rev-approve")
    assert ev["status"] == "registered"
    assert ev["calendar_event_id"] == "ui-cal-1"


def test_calendar_review_invalid_edit_rejected(page: Page, ui_server: SimpleNamespace) -> None:
    """수정값이 여전히 무효하면 등록 거부 + needs_review 유지."""
    _seed_review_event(ui_server.store, "ui:rev-invalid")
    page.click('.nav-item[data-view="calendar"]')
    card = page.locator('[data-review="ui:rev-invalid"]')
    card.wait_for(timeout=5000)
    card.locator(".rv-start").fill("2025-01-10T12:00:00+09:00")
    card.locator(".rv-end").fill("2025-01-10T10:00:00+09:00")  # 종료 < 시작
    card.locator(".rv-approve").click()
    _wait_text(page, '[data-review="ui:rev-invalid"] .rv-msg', "invalid")
    ev = ui_server.store.get_scheduled_event("ui:rev-invalid")
    assert ev["status"] == "needs_review"
    assert ev["calendar_event_id"] in (None, "")
