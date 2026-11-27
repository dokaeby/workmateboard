import httpx
import pytest

from jobmate.adapters.cronjob import (
    CronJobOrgClient,
    CronJobOrgError,
    daily_at,
    interval_schedule,
    weekly_at,
)


def test_interval_schedule():
    s30 = interval_schedule(30)
    assert s30["minutes"] == [0, 30]
    assert s30["hours"] == [-1] and s30["wdays"] == [-1]
    assert s30["timezone"] == "Asia/Seoul"
    s60 = interval_schedule(60)
    assert s60["minutes"] == [0]
    with pytest.raises(CronJobOrgError):
        interval_schedule(15)


def test_daily_weekly_schedule():
    d = daily_at(6)
    assert d["hours"] == [6] and d["minutes"] == [0]
    w = weekly_at(6, wday=1)
    assert w["wdays"] == [1]


def test_update_schedule_patch():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        import json

        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"job": {}})

    client = httpx.Client(
        base_url="https://api.cron-job.org", transport=httpx.MockTransport(handler)
    )
    c = CronJobOrgClient("key123", "999", client=client)
    sched = interval_schedule(30)
    c.update_schedule(sched)
    assert captured["url"].endswith("/jobs/999")
    assert captured["auth"] == "Bearer key123"
    assert captured["body"] == {"job": {"schedule": sched}}


def test_update_schedule_error():
    client = httpx.Client(
        base_url="https://api.cron-job.org",
        transport=httpx.MockTransport(lambda r: httpx.Response(401, text="bad")),
    )
    c = CronJobOrgClient("k", "1", client=client)
    with pytest.raises(CronJobOrgError):
        c.update_schedule(interval_schedule(30))


def test_create_job():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"jobId": 424242})

    from jobmate.adapters.cronjob import create_job

    client = httpx.Client(
        base_url="https://api.cron-job.org", transport=httpx.MockTransport(handler)
    )
    jid = create_job(
        "key123",
        title="jobMate email check",
        url="https://jm.example.com/api/v1/email/check",
        schedule=interval_schedule(30),
        headers={"X-API-Key": "k"},
        body='{"limit":5}',
        client=client,
    )
    assert jid == 424242
    assert captured["method"] == "PUT"
    assert captured["url"].endswith("/jobs")
    assert captured["auth"] == "Bearer key123"
    job = captured["body"]["job"]
    assert job["title"] == "jobMate email check"
    assert job["url"] == "https://jm.example.com/api/v1/email/check"
    assert job["requestMethod"] == 1
    assert job["extendedData"] == {"headers": {"X-API-Key": "k"}, "body": '{"limit":5}'}
    assert job["schedule"]["minutes"] == [0, 30]


def test_create_job_error():
    from jobmate.adapters.cronjob import create_job

    client = httpx.Client(
        base_url="https://api.cron-job.org",
        transport=httpx.MockTransport(lambda r: httpx.Response(500, text="boom")),
    )
    with pytest.raises(CronJobOrgError):
        create_job("k", title="t", url="https://x", schedule=interval_schedule(60), client=client)
    with pytest.raises(CronJobOrgError):
        create_job("", title="t", url="https://x", schedule=interval_schedule(60), client=client)


def test_cronjob_client_rejects_public_http_base_url():
    from jobmate.adapters.cronjob import create_job

    # client 미주입 시 base_url 검증 — 공인 http는 Bearer 유출 경로라 거부
    with pytest.raises(CronJobOrgError, match="base_url"):
        CronJobOrgClient("k", "1", base_url="http://8.8.8.8")
    with pytest.raises(CronJobOrgError, match="base_url"):
        create_job(
            "k",
            title="t",
            url="https://x",
            schedule=interval_schedule(60),
            base_url="http://8.8.8.8",
        )


def test_cronjob_client_allows_loopback_http():
    c = CronJobOrgClient("k", "1", base_url="http://127.0.0.1:9")
    assert c is not None
