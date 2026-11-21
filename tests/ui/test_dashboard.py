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
    llm = FakeLLM()

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
        ("office", "Agent Command"),
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


def test_settings_dialog_save(page: Page, base_url: str) -> None:
    """설정 다이얼로그에서 값을 바꿔 저장하면 API에 반영된다."""
    page.click("#settings-open")
    page.wait_for_function("document.querySelector('#settings-dialog').open")
    page.wait_for_function("document.querySelector('#set-gmail-minutes').value !== ''")
    page.select_option("#set-gmail-minutes", "60")
    page.select_option("#set-radar-interval", "weekly")
    page.click("#settings-save")
    page.wait_for_function(
        "document.querySelector('#settings-status').textContent.includes('저장했습니다')",
        timeout=5000,
    )
    resp = page.request.get(base_url + "/api/v1/settings")
    prefs = resp.json()
    assert prefs["gmail_check_minutes"] == 60
    assert prefs["radar_interval"] == "weekly"


def test_push_toggle_without_vapid(page: Page) -> None:
    """VAPID 키 미설정 시 알림 켜기가 alert로 안내하고 화면이 유지된다."""
    messages: list[str] = []
    page.on("dialog", lambda d: (messages.append(d.message), d.dismiss()))
    page.click("#push-toggle")
    page.wait_for_timeout(1000)
    assert any("VAPID" in m or "지원" in m for m in messages)
    assert page.locator("#push-toggle").is_visible()


def test_office_view_empty_state(page: Page) -> None:
    """dev_review run이 없으면 빈 상태 안내 문구를 표시한다."""
    page.click('.nav-item[data-view="office"]')
    _wait_text(page, "#office-stage", "dev_review")


def test_office_view_shows_dev_review_run(page: Page, ui_server: SimpleNamespace) -> None:
    """dev_review run + stage_run을 시드하면 헤더 통계와 노드가 렌더링된다."""
    store = ui_server.store
    run = store.create_run("dev_review", "v1", {"task": "ui"}, trace_id="uitrace1")
    sr = store.start_stage(run.id, "analysis", 1, {})
    store.finish_stage(sr.id, status="succeeded", tokens_in=10, tokens_out=5)
    # 실패한 다른 stage의 토큰도 총 토큰에 합산되어야 한다 (재시도 비용 포함)
    sr2 = store.start_stage(run.id, "implement", 1, {})
    store.finish_stage(sr2.id, status="failed", tokens_in=3, tokens_out=2)
    page.click('.nav-item[data-view="office"]')
    _wait_text(page, "#office-tokens", "20")  # 15 + 5 (실패 시도 포함 전체 합산)
    _wait_text(page, "#office-progress", "14%")  # 1/7 succeeded
    _wait_text(page, "#office-stage", "succeeded")
    assert page.locator(".office-node").count() == 7


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
