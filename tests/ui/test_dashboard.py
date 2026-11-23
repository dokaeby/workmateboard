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
from jobmate.models.enums import RunStatus
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
    page.evaluate("localStorage.setItem('jobmate.officeDemo','0')")
    page.reload()
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
    # 7개 stage 노드 + ORCH 노드
    assert page.locator(".office-node").count() == 8
    assert page.locator('.office-node[data-node="ORCH"]').count() == 1
    assert page.locator(".office-edges path").count() >= 7  # SVG 연결선


def test_office_node_click_shows_stage_detail(page: Page, ui_server: SimpleNamespace) -> None:
    """노드 클릭 → 상세 패널에 해당 stage_run 정보가 표시된다."""
    store = ui_server.store
    run = store.create_run("dev_review", "v1", {"task": "detail"}, trace_id="uitrace2")
    sr = store.start_stage(run.id, "analysis", 1, {"q": "x"})
    store.finish_stage(
        sr.id, status="succeeded", tokens_in=7, tokens_out=3, output={"summary": "done"}
    )
    page.click('.nav-item[data-view="office"]')
    page.wait_for_selector('.office-node[data-node="ANA"]', timeout=5000)
    # rotateX 평면 위 노드는 히트 테스트 좌표가 어긋날 수 있어 DOM click 사용
    page.locator('.office-node[data-node="ANA"]').dispatch_event("click")
    _wait_text(page, "#office-detail", "analysis")
    _wait_text(page, "#office-detail", "succeeded")
    _wait_text(page, "#office-detail", "합계 10")
    # 이벤트 로그에 stage audit이 시간순으로 렌더링된다
    _wait_text(page, "#office-log", "stage_done")


def test_office_waiting_approval_shows_approve_buttons(
    page: Page, ui_server: SimpleNamespace
) -> None:
    """waiting_approval run → ORCH 상세에 승인/거절 버튼, 클릭 시 approve API 호출."""
    store = ui_server.store
    run = store.create_run("dev_review", "v1", {"task": "approve"}, trace_id="uitrace3")
    store.update_run(run.id, status=RunStatus.WAITING_APPROVAL)
    called: list[dict[str, Any]] = []

    def handler(route: Any) -> None:
        called.append(route.request.post_data_json)
        route.fulfill(json={"status": "waiting_approval"})

    page.route("**/api/v1/runs/*/approve", handler)
    page.click('.nav-item[data-view="office"]')
    page.wait_for_selector("#office-approve-btn", timeout=5000)
    assert page.locator("#office-reject-btn").is_visible()
    page.click("#office-approve-btn")
    for _ in range(50):
        if called:
            break
        time.sleep(0.1)
    assert called and called[0]["decision"] == "approve"


def test_office_approve_buttons_hide_when_run_leaves_waiting(
    page: Page, ui_server: SimpleNamespace
) -> None:
    """stage 노드 선택 중 run.status가 waiting_approval을 벗어나면 버튼이 사라진다.

    stage_run 필드 변화 없이 run 상태만 바뀌는 경우에도 상세 패널이
    재렌더링되어 stale 승인 버튼이 남지 않아야 한다.
    """
    store = ui_server.store
    # 이전 테스트가 남긴 dev_review run들은 active 상태라 pickRun에 잡힌다 —
    # 이 테스트의 run만 선택되도록 전부 completed로 정리
    for r in store.list_runs():
        if r.workflow_key == "dev_review":
            store.update_run(r.id, status=RunStatus.COMPLETED)
    run = store.create_run("dev_review", "v1", {"task": "stale"}, trace_id="uitrace4")
    store.update_run(run.id, status=RunStatus.WAITING_APPROVAL)
    page.click('.nav-item[data-view="office"]')
    page.wait_for_selector('.office-node[data-node="HUM"]', timeout=5000)
    page.locator('.office-node[data-node="HUM"]').dispatch_event("click")
    page.wait_for_selector("#office-approve-btn", timeout=5000)
    # stage_run은 그대로 두고 run 상태만 completed로 전이
    store.update_run(run.id, status=RunStatus.COMPLETED)
    page.wait_for_selector("#office-approve-btn", state="detached", timeout=10000)


def test_office_sprite_mode_shows_characters(page: Page, ui_server: SimpleNamespace) -> None:
    """스프라이트 사무실 모드: 토글 전환, 캔버스 표시, 8명 캐릭터 매핑."""
    store = ui_server.store
    run = store.create_run("dev_review", "v1", {"task": "ui-sprite"}, trace_id="uisprite1")
    sr = store.start_stage(run.id, "analysis", 1, {})
    store.finish_stage(sr.id, status="succeeded", tokens_in=10, tokens_out=5)
    page.click('.nav-item[data-view="office"]')
    page.click('.office-mode-btn[data-office-mode="sprites"]')
    page.wait_for_selector("#jmo-canvas", state="visible", timeout=15000)
    # people provider가 officeState를 읽어 8명(7 stage + ORCH)을 생성할 때까지 대기
    page.wait_for_function(
        "window.officeDebug && window.officeDebug().length === 8",
        timeout=15000,
    )
    people = page.evaluate("window.officeDebug()")
    ids = {p["id"] for p in people}
    assert ids == {"ANA", "DEV", "TEST", "SEC", "CR", "QA", "HUM", "ORCH"}
    ana = next(p for p in people if p["id"] == "ANA")
    assert ana["status"] == "rest"  # succeeded → 휴식
    # 보드 모드로 복귀하면 기존 3D 노드가 다시 보인다
    page.click('.office-mode-btn[data-office-mode="board"]')
    page.wait_for_selector('.office-node[data-node="ANA"]', timeout=5000)
    assert page.locator(".office-node").count() == 8


def test_office_sessions_tab_shows_agent_characters(page: Page, ui_server: SimpleNamespace) -> None:
    """세션 탭: agent-bridge가 push한 세션이 스프라이트 사무실 캐릭터로 표시된다."""
    page.evaluate("localStorage.setItem('jobmate.officeDemo','0')")
    page.reload()
    page.wait_for_selector(".nav-item")
    store = ui_server.store
    store.upsert_agent_session(
        "claude",
        "ui-session-1",
        "working",
        project="workmateboard",
        title="UI 테스트 세션",
        doing="코드 수정 중",
        event_at="2026-11-22T01:00:00+00:00",
    )
    store.upsert_agent_session(
        "codex",
        "ui-session-2",
        "decision",
        project="workmateboard",
        ask="Bash 권한 요청",
        event_at="2026-11-22T01:00:00+00:00",
    )
    page.click('.nav-item[data-view="office"]')
    page.click('.office-tab-btn[data-office-tab="sessions"]')
    page.wait_for_selector("#jmo-canvas", state="visible", timeout=15000)
    # 기본 필터는 all — 두 세션 모두 표시된다
    page.wait_for_function(
        "window.officeDebug && window.officeDebug().length === 2",
        timeout=15000,
    )
    people = page.evaluate("window.officeDebug()")
    ids = {p["id"] for p in people}
    assert ids == {"claude:ui-session-1", "codex:ui-session-2"}
    statuses = {p["id"]: p["status"] for p in people}
    assert statuses["claude:ui-session-1"] == "working"
    assert statuses["codex:ui-session-2"] == "decision"
    # 'codex' 선택 시 claude 세션은 숨겨진다.
    # 사이드 패널(#jmo-people)은 provider 데이터를 바로 반영하지만,
    # officeDebug의 스프라이트는 퇴장 연출(걸어 나가기+페이드) 동안 남아 있으므로
    # 패널 목록으로 검증한다.
    page.click('.office-tool-btn[data-office-tool="codex"]')
    page.wait_for_function(
        "document.querySelectorAll('#jmo-people li.o-person').length === 1",
        timeout=15000,
    )
    ids = page.eval_on_selector_all("#jmo-people li.o-person", "els => els.map(e => e.dataset.id)")
    assert ids == ["codex:ui-session-2"]
    # 워크플로 탭으로 복귀하면 기존 보드가 보인다
    page.click('.office-tab-btn[data-office-tab="workflow"]')
    page.wait_for_selector("#office-stage", state="visible", timeout=5000)


def test_office_sessions_demo_mode(page: Page, ui_server: SimpleNamespace) -> None:
    """데모 토글: 기본 ON — 실제 세션 없이도 가상 캐스트 7명이 보이고,
    OFF로 끄면 데모 사람만 사라지고 실제 세션은 남는다."""
    ui_server.store.upsert_agent_session(
        "codex",
        "demo-real-1",
        "working",
        project="jobmate-demo",
        doing="실제 작업 중",
        event_at="2026-11-22T01:00:00+00:00",
    )
    page.click('.nav-item[data-view="office"]')
    page.click('.office-tab-btn[data-office-tab="sessions"]')
    page.wait_for_selector("#jmo-canvas", state="visible", timeout=15000)
    # 데모 기본 ON → 가상 캐스트 + 실제 세션
    page.wait_for_function(
        "window.officeDebug && "
        "window.officeDebug().filter(p => p.id.startsWith('/demo/')).length === 7",
        timeout=15000,
    )
    people = page.evaluate("window.officeDebug()")
    assert any(p["id"] == "codex:demo-real-1" for p in people)
    demo_statuses = {p["status"] for p in people if p["id"].startswith("/demo/")}
    assert demo_statuses <= {"working", "decision", "rest"}
    # 데모 OFF → 사이드 패널(프로바이더 데이터 기준)에서 가상 캐스트 7명만 사라진다.
    # 스프라이트는 퇴장 연출(걸어 나가기+페이드)로 officeDebug에 한동안 남으므로
    # 패널 목록으로 검증한다. 모듈 스코프 서버를 공유하는 다른 테스트가 남긴
    # 실제 세션이 있을 수 있어 절대 개수가 아니라 감소분으로 본다.
    before = page.locator("#jmo-people li.o-person").count()
    page.click("#office-demo-btn")
    page.wait_for_function(
        "n => document.querySelectorAll('#jmo-people li.o-person').length === n",
        arg=before - 7,
        timeout=15000,
    )
    names = page.eval_on_selector_all(
        "#jmo-people li.o-person b", "els => els.map(e => e.textContent)"
    )
    assert "my-app" not in names


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
