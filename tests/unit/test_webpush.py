"""WebPushNotifier 단위 테스트 — webpush 호출 모의."""

import json

import jobmate.adapters.push as push_mod
from jobmate.adapters.push import WebPushNotifier


class FakeStore:
    def __init__(self, subs):
        self._subs = list(subs)
        self.deleted: list[str] = []

    def list_push_subscriptions(self):
        return list(self._subs)

    def delete_push_subscription(self, endpoint):
        self.deleted.append(endpoint)
        self._subs = [s for s in self._subs if s["endpoint"] != endpoint]


SUB = {"endpoint": "https://fcm.googleapis.com/1", "keys": {"p256dh": "pk", "auth": "ak"}}


def _patch_webpush(monkeypatch, calls, exc=None):
    def fake(**kwargs):
        calls.append(kwargs)
        if exc is not None:
            raise exc

    monkeypatch.setattr(push_mod, "webpush", fake)


def test_send_to_all_subscriptions(monkeypatch):
    calls = []
    _patch_webpush(monkeypatch, calls)
    store = FakeStore([SUB, dict(SUB, endpoint="https://fcm.googleapis.com/2")])
    WebPushNotifier(store, private_key="priv").send(
        title="일정 등록: 회의", body="2026-01-05T15:00:00+09:00", source_url="https://mail"
    )
    assert len(calls) == 2
    assert calls[0]["vapid_private_key"] == "priv"
    assert calls[0]["vapid_claims"] == {"sub": "mailto:admin@localhost"}
    payload = json.loads(calls[0]["data"])
    assert payload == {
        "title": "일정 등록: 회의",
        "body": "2026-01-05T15:00:00+09:00",
        "url": "https://mail",
    }
    assert store.deleted == []


def test_noop_without_private_key(monkeypatch):
    calls = []
    _patch_webpush(monkeypatch, calls)
    store = FakeStore([SUB])
    WebPushNotifier(store, private_key="").send(title="t", body="b", source_url="")
    assert calls == []


def test_noop_without_subscriptions(monkeypatch):
    calls = []
    _patch_webpush(monkeypatch, calls)
    WebPushNotifier(FakeStore([]), private_key="priv").send(title="t", body="b", source_url="")
    assert calls == []


def test_expired_subscription_deleted(monkeypatch):
    class Resp:
        status_code = 410

    exc = push_mod.WebPushException("gone", response=Resp())
    calls = []
    _patch_webpush(monkeypatch, calls, exc=exc)
    store = FakeStore([SUB])
    # 두 번째 호출은 성공하도록 — send는 구독별로 호출하므로 exc는 모든 호출에 적용됨
    WebPushNotifier(store, private_key="priv").send(title="t", body="b", source_url="")
    assert calls
    assert store.deleted == [SUB["endpoint"]]
