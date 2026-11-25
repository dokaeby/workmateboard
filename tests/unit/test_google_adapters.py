"""Google 어댑터 단위 테스트 — httpx.MockTransport로 API 모의."""

import base64
import json

import httpx
import pytest

from jobmate.adapters.google import (
    AUTH_URI,
    SCOPES,
    TOKEN_URI,
    GoogleAuthError,
    GoogleCalendar,
    GoogleGmail,
    _b64url_decode,
    _decode_header,
    _extract_body_text,
    load_client_credentials,
)


def _token_file(tmp_path):
    p = tmp_path / "tok.json"
    p.write_text(json.dumps({"client_id": "cid", "client_secret": "csec", "refresh_token": "rtok"}))
    return p


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def _token_resp(request: httpx.Request) -> httpx.Response:
    assert request.url == TOKEN_URI
    return httpx.Response(200, json={"access_token": "at-1", "expires_in": 3600})


def test_gmail_list_and_get(tmp_path):
    def handler(request):
        if request.url == TOKEN_URI:
            return _token_resp(request)
        if request.url.path == "/gmail/v1/users/me/messages":
            assert request.url.params["q"] == "in:inbox"
            return httpx.Response(200, json={"messages": [{"id": "m1"}, {"id": "m2"}]})
        if request.url.path == "/gmail/v1/users/me/messages/m1":
            payload = {
                "mimeType": "multipart/alternative",
                "headers": [
                    {"name": "Subject", "value": "회의 안내"},
                    {"name": "From", "value": "a@b.c"},
                ],
                "parts": [
                    {"mimeType": "text/plain", "body": {"data": _b64("내일 3시 회의")}},
                    {"mimeType": "text/html", "body": {"data": _b64("<p>html</p>")}},
                ],
            }
            return httpx.Response(
                200,
                json={
                    "id": "m1",
                    "threadId": "t1",
                    "internalDate": "1700000000000",
                    "payload": payload,
                },
            )
        return httpx.Response(404)

    gmail = GoogleGmail(_token_file(tmp_path), client=_client(handler))
    msgs = gmail.list_messages("in:inbox", 10)
    assert [m["id"] for m in msgs] == ["m1", "m2"]
    msg = gmail.get_message("m1")
    assert msg["subject"] == "회의 안내"
    assert msg["from_addr"] == "a@b.c"
    assert msg["body_text"] == "내일 3시 회의"
    assert msg["thread_id"] == "t1"


def test_gmail_send_message(tmp_path):
    sent = {}

    def handler(request):
        if request.url == TOKEN_URI:
            return _token_resp(request)
        if request.url.path == "/gmail/v1/users/me/messages/send":
            sent.update(json.loads(request.content))
            return httpx.Response(200, json={"id": "sent-1"})
        return httpx.Response(404)

    gmail = GoogleGmail(_token_file(tmp_path), client=_client(handler))
    mid = gmail.send_message("user@example.com", "인증 메일", "링크: http://x/v?token=t")
    assert mid == "sent-1"
    # raw RFC822를 디코딩해 To/Subject/본문 확인
    raw = base64.urlsafe_b64decode(sent["raw"]).decode()
    assert "To: user@example.com" in raw
    assert "Subject: =?utf-8?b?" in raw or "인증" in raw  # MIME encoded-word 허용
    assert "http://x/v?token=t" in raw


def test_calendar_create_find_search(tmp_path):
    created = {}

    def handler(request):
        if request.url == TOKEN_URI:
            return _token_resp(request)
        path = request.url.path
        if request.method == "POST" and path.endswith("/events"):
            created.update(json.loads(request.content))
            return httpx.Response(200, json={"id": "evt_9"})
        if path.endswith("/events") and request.method == "GET":
            params = request.url.params
            if "privateExtendedProperty" in params:
                return httpx.Response(200, json={"items": [{"id": "evt_9"}]})
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "evt_1",
                            "summary": "회의",
                            "status": "confirmed",
                            "transparency": "opaque",
                            "start": {"dateTime": "2026-01-05T15:00:00+09:00"},
                            "end": {"dateTime": "2026-01-05T16:00:00+09:00"},
                            "extendedProperties": {"private": {"jobmate_marker": "mk"}},
                        }
                    ]
                },
            )
        return httpx.Response(404)

    cal = GoogleCalendar(_token_file(tmp_path), client=_client(handler))
    eid = cal.create_event(
        title="회의",
        start_at="2026-01-05T15:00:00+09:00",
        end_at="2026-01-05T16:00:00+09:00",
        timezone="Asia/Seoul",
        location="",
        marker="mk1",
    )
    assert eid == "evt_9"
    assert created["summary"] == "회의"
    assert cal.find_by_marker("mk1") == "evt_9"
    events = cal.search_events("2026-01-05T00:00:00+09:00", "2026-01-06T00:00:00+09:00")
    assert events[0]["id"] == "evt_1"
    assert events[0]["marker"] == "mk"


def test_create_event_body_shape(tmp_path):
    bodies = []

    def handler(request):
        if request.url == TOKEN_URI:
            return _token_resp(request)
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "e1"})

    cal = GoogleCalendar(_token_file(tmp_path), client=_client(handler))
    cal.create_event(
        title="t",
        start_at="2026-01-05T15:00:00+09:00",
        end_at="2026-01-05T16:00:00+09:00",
        timezone="Asia/Seoul",
        location="room1",
        marker="mk",
    )
    b = bodies[0]
    assert b["summary"] == "t"
    assert b["start"] == {"dateTime": "2026-01-05T15:00:00+09:00", "timeZone": "Asia/Seoul"}
    assert b["extendedProperties"]["private"]["jobmate_marker"] == "mk"


def test_token_refresh_failure(tmp_path):
    def handler(request):
        return httpx.Response(400, json={"error": "invalid_grant"})

    gmail = GoogleGmail(_token_file(tmp_path), client=_client(handler))
    with pytest.raises(GoogleAuthError):
        gmail.list_messages()


def test_b64_and_html_fallback():
    payload = {"mimeType": "text/html", "body": {"data": _b64("<p>안녕 <b>세상</b></p>")}}
    assert "안녕" in _extract_body_text(payload)
    assert "<p>" not in _extract_body_text(payload)
    assert _b64url_decode(_b64("hi")) == "hi"


def test_calendar_search_pagination(tmp_path):
    calls = []

    def handler(request):
        if request.url == TOKEN_URI:
            return _token_resp(request)
        calls.append(request.url.params.get("pageToken", ""))
        if not calls[-1]:
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "e1",
                            "summary": "a",
                            "start": {"dateTime": "x"},
                            "end": {"dateTime": "y"},
                        }
                    ],
                    "nextPageToken": "p2",
                },
            )
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "e2",
                        "summary": "b",
                        "start": {"dateTime": "x"},
                        "end": {"dateTime": "y"},
                    }
                ]
            },
        )

    cal = GoogleCalendar(_token_file(tmp_path), client=_client(handler))
    events = cal.search_events("a", "b")
    assert [e["id"] for e in events] == ["e1", "e2"]
    assert calls == ["", "p2"]


def test_decode_header_rfc2047():
    # "회의" 를 UTF-8 base64로 인코딩한 RFC2047 헤더
    enc = _b64("회의")
    assert _decode_header(f"=?UTF-8?B?{enc}?=") == "회의"
    assert _decode_header("plain subject") == "plain subject"
    assert _decode_header("") == ""


def test_gmail_decodes_encoded_subject(tmp_path):
    enc = _b64("한글 제목")

    def handler(request):
        if request.url == TOKEN_URI:
            return _token_resp(request)
        payload = {
            "mimeType": "text/plain",
            "headers": [{"name": "Subject", "value": f"=?UTF-8?B?{enc}?="}],
            "body": {"data": _b64("본문")},
        }
        return httpx.Response(
            200, json={"id": "m9", "threadId": "t", "internalDate": "1", "payload": payload}
        )

    gmail = GoogleGmail(_token_file(tmp_path), client=_client(handler))
    assert gmail.get_message("m9")["subject"] == "한글 제목"


def test_load_client_credentials(tmp_path):
    p = tmp_path / "credentials.json"
    p.write_text(json.dumps({"installed": {"client_id": "cid", "client_secret": "csec"}}))
    assert load_client_credentials(p) == ("cid", "csec")


def test_scopes_cover_protocol():
    assert "gmail.readonly" in SCOPES["gmail"][0]
    assert "calendar.events" in SCOPES["calendar"][0]
    assert AUTH_URI.startswith("https://")
