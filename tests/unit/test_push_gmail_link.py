"""알림 예약 발송 시 gmail 원본 링크가 푸시 source_url로 전달되는지 검증."""

from __future__ import annotations

from jobmate.adapters.fakes import FakeNotifier
from jobmate.api.jobs import _process_due_notification_jobs
from jobmate.store.db import Store


def _store(tmp_path) -> Store:
    return Store(str(tmp_path / "t.db"))


def test_due_job_sends_gmail_source_url(tmp_path) -> None:
    store = _store(tmp_path)
    store.save_scheduled_event(
        event_id="ev1",
        source_provider="gmail",
        source_id="18f3a1b2c3d4e5f6",
        title="팀 회의",
        start_at="2026-11-19T15:00:00+09:00",
        end_at="2026-11-19T16:00:00+09:00",
        timezone="Asia/Seoul",
        status="scheduled",
    )
    store.save_notification_job("gmail:18f3a1b2c3d4e5f6", "reminder", "2000-01-01T00:00:00+00:00")
    notifier = FakeNotifier()
    _process_due_notification_jobs(store, {"notifier": notifier})
    assert len(notifier.sent) == 1
    sent = notifier.sent[0]
    assert sent["title"] == "곧 시작: 팀 회의"
    assert sent["source_url"] == ("https://mail.google.com/mail/u/0/#all/18f3a1b2c3d4e5f6")
    store.close()


def test_due_job_non_gmail_has_empty_url(tmp_path) -> None:
    store = _store(tmp_path)
    store.save_scheduled_event(
        event_id="ev2",
        source_provider="manual",
        source_id="x1",
        title="수동 일정",
        start_at="2026-11-19T15:00:00+09:00",
        end_at="2026-11-19T16:00:00+09:00",
        timezone="Asia/Seoul",
        status="scheduled",
    )
    store.save_notification_job("manual:x1", "reminder", "2000-01-01T00:00:00+00:00")
    notifier = FakeNotifier()
    _process_due_notification_jobs(store, {"notifier": notifier})
    assert notifier.sent[0]["source_url"] == ""
    store.close()
