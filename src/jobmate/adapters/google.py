"""Google API 어댑터 — Gmail 읽기 + Calendar 쓰기 (OAuth refresh token).

토큰 파일(JSON) 형식:
    {"client_id": "...", "client_secret": "...", "refresh_token": "..."}

발급은 `jobmate google-auth gmail|calendar` CLI로 수행한다.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("jobmate.adapters.google")


def _raise_for_status(r: httpx.Response, api: str) -> None:
    """4xx/5xx를 WARNING 로그로 남긴 뒤 raise_for_status. 토큰·자격증명은 기록하지 않는다."""
    if r.status_code >= 400:
        try:
            path = r.request.url.path
        except Exception:
            path = ""
        logger.warning("%s api error: status=%s path=%s", api, r.status_code, path)
    r.raise_for_status()


TOKEN_URI = "https://oauth2.googleapis.com/token"  # noqa: S105 - URL 상수
AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"
CALENDAR_API = "https://www.googleapis.com/calendar/v3/calendars/primary"

SCOPES = {
    # gmail.send: 이메일 인증 메일 발송용 (mailer.py Gmail 백엔드)
    "gmail": [
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.send",
    ],
    "calendar": ["https://www.googleapis.com/auth/calendar.events"],
}


class GoogleAuthError(Exception):
    pass


class _GoogleAuth:
    """토큰 파일 기반 인증 — access token은 만료 전까지 메모리 캐시."""

    def __init__(self, token_path: str | Path, client: httpx.Client | None = None) -> None:
        data = json.loads(Path(token_path).expanduser().read_text())
        self._client_id = data["client_id"]
        self._client_secret = data["client_secret"]
        self._refresh_token = data.get("refresh_token", "")
        self._client = client or httpx.Client(timeout=30)
        self._access_token = ""
        self._expires_at = 0.0

    def _refresh(self) -> None:
        if not self._refresh_token:
            raise GoogleAuthError("refresh_token missing in token file")
        r = self._client.post(
            TOKEN_URI,
            data={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "refresh_token": self._refresh_token,
                "grant_type": "refresh_token",
            },
        )
        if r.status_code != 200:
            logger.warning("google token refresh failed: status=%s", r.status_code)
            raise GoogleAuthError(f"token refresh failed: {r.status_code} {r.text[:200]}")
        body = r.json()
        self._access_token = body["access_token"]
        self._expires_at = time.time() + float(body.get("expires_in", 3600))
        logger.info("google access token refreshed, expires_in=%ss", body.get("expires_in"))

    def headers(self) -> dict[str, str]:
        if not self._access_token or time.time() >= self._expires_at - 60:
            self._refresh()
        return {"Authorization": f"Bearer {self._access_token}"}


def _b64url_decode(data: str) -> str:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad).decode("utf-8", errors="replace")


def _header(payload: dict[str, Any], name: str) -> str:
    for h in payload.get("headers") or []:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _decode_header(value: str) -> str:
    """RFC 2047 인코딩 또는 Gmail Mojibake를 디코딩 (calendar-agent 포팅).

    Gmail 헤더는 RFC 2047(=?charset?B/Q?...?=), Mojibake(UTF-8을
    latin-1으로 읽은 형태), 순수 UTF-8 중 하나로 올 수 있다.
    """
    if not value:
        return ""

    decoded_parts = re.findall(r"=\?([^?]+)\?([BbQq])\?([^?]*)\?=", value)
    if decoded_parts:
        result: list[str] = []
        for charset, enc, text in decoded_parts:
            try:
                if enc.lower() == "b":
                    decoded_bytes = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
                    result.append(decoded_bytes.decode(charset, errors="replace"))
                elif enc.lower() == "q":
                    normalized = text.replace("_", " ")
                    result.append(normalized.encode("latin-1").decode(charset, errors="replace"))
            except Exception:
                logger.warning("RFC 2047 decode failed (charset=%s)", charset)
                continue
        if result:
            return "".join(result)

    try:
        return value.encode("iso-8859-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value


def _strip_html(text: str) -> str:
    """HTML 태그 제거 + 엔티티 디코딩 (calendar-agent 포팅)."""
    if not text:
        return ""
    try:
        text = re.sub(r"<\s*/?\s*(br|p|tr|td)\s*>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
        for a, b in (
            ("&amp;", "&"),
            ("&lt;", "<"),
            ("&gt;", ">"),
            ("&quot;", '"'),
            ("&#39;", "'"),
            ("&nbsp;", " "),
        ):
            text = text.replace(a, b)
        text = re.sub(r"[\r\n]+", "\n", text)
        text = re.sub(r" {3,}", " ", text)
        return text.strip()
    except Exception as e:
        logger.debug("html strip fallback: %s", e)
        return text


def _extract_body_text(payload: dict[str, Any]) -> str:
    """text/plain 파트를 우선 추출, 없으면 text/html을 평문화 (calendar-agent 포팅)."""
    plain: list[str] = []
    html: list[str] = []

    def walk(part: dict[str, Any]) -> None:
        mime = part.get("mimeType", "")
        body = part.get("body") or {}
        data = body.get("data")
        if data:
            text = _b64url_decode(data)
            if mime == "text/plain":
                plain.append(text)
            elif mime == "text/html":
                html.append(text)
        for sub in part.get("parts") or []:
            walk(sub)

    # 단일 파트(non-multipart) 본문도 처리
    if not plain and not html:
        raw = (payload.get("body") or {}).get("data")
        if raw:
            text = _b64url_decode(raw)
            if payload.get("mimeType") == "text/html":
                return _strip_html(text)
            return text

    walk(payload)
    if plain:
        return "\n".join(plain)
    return _strip_html("\n".join(html))


class GoogleGmail:
    """GmailClient — gmail.readonly + gmail.send scope. list/get/send 사용."""

    name = "gmail"

    def __init__(self, token_path: str | Path, client: httpx.Client | None = None) -> None:
        self._auth = _GoogleAuth(token_path, client)
        self._client = self._auth._client

    def list_messages(self, query: str = "", max_results: int = 10) -> list[dict[str, Any]]:
        r = self._client.get(
            f"{GMAIL_API}/messages",
            params={"q": query, "maxResults": max_results},
            headers=self._auth.headers(),
        )
        _raise_for_status(r, "gmail list_messages")
        return [{"id": m["id"]} for m in r.json().get("messages", [])]

    def get_message(self, message_id: str) -> dict[str, Any]:
        r = self._client.get(
            f"{GMAIL_API}/messages/{message_id}",
            params={"format": "full"},
            headers=self._auth.headers(),
        )
        _raise_for_status(r, "gmail get_message")
        msg = r.json()
        payload = msg.get("payload") or {}
        return {
            "id": msg.get("id", message_id),
            "thread_id": msg.get("threadId", ""),
            "subject": _decode_header(_header(payload, "Subject")),
            "from_addr": _decode_header(_header(payload, "From")),
            "internal_date": msg.get("internalDate", ""),
            "body_text": _extract_body_text(payload),
        }

    def send_message(self, to: str, subject: str, body_text: str) -> str:
        """RFC822 plain-text 메일 발송 (gmail.send scope). 발송된 메시지 id 반환."""
        from email.message import EmailMessage

        # Gmail API는 발신자를 토큰 계정으로 강제 — To/Subject/본문만 지정
        msg = EmailMessage()
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body_text)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
        r = self._client.post(
            f"{GMAIL_API}/messages/send",
            json={"raw": raw},
            headers=self._auth.headers(),
        )
        _raise_for_status(r, "gmail send_message")
        return str(r.json().get("id", ""))


class GoogleCalendar:
    """CalendarClient — calendar.events scope. primary 캘린더 사용.

    멱등 마커는 extendedProperties.private.jobmate_marker에 저장하고
    privateExtendedProperty 쿼리로 조회한다.
    """

    name = "calendar"
    MARKER_KEY = "jobmate_marker"

    def __init__(self, token_path: str | Path, client: httpx.Client | None = None) -> None:
        self._auth = _GoogleAuth(token_path, client)
        self._client = self._auth._client

    def _event_body(
        self,
        *,
        title: str,
        start_at: str,
        end_at: str,
        timezone: str,
        location: str,
        marker: str,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "summary": title,
            "location": location,
            "start": {"dateTime": start_at, "timeZone": timezone},
            "end": {"dateTime": end_at, "timeZone": timezone},
            "extendedProperties": {"private": {self.MARKER_KEY: marker}},
        }
        return body

    def create_event(
        self,
        *,
        title: str,
        start_at: str,
        end_at: str,
        timezone: str,
        location: str,
        marker: str,
        event_id: str | None = None,
    ) -> str:
        r = self._client.post(
            f"{CALENDAR_API}/events",
            params={"sendUpdates": "none"},
            json=self._event_body(
                title=title,
                start_at=start_at,
                end_at=end_at,
                timezone=timezone,
                location=location,
                marker=marker,
            ),
            headers=self._auth.headers(),
        )
        _raise_for_status(r, "calendar create_event")
        return r.json()["id"]

    def find_by_marker(self, marker: str) -> str | None:
        r = self._client.get(
            f"{CALENDAR_API}/events",
            params={
                "privateExtendedProperty": f"{self.MARKER_KEY}={marker}",
                "singleEvents": "true",
                "maxResults": 1,
            },
            headers=self._auth.headers(),
        )
        _raise_for_status(r, "calendar find_by_marker")
        items = r.json().get("items", [])
        return items[0]["id"] if items else None

    def search_events(self, start_at: str, end_at: str) -> list[dict[str, Any]]:
        """시간대 겹치는 이벤트를 nextPageToken까지 전부 수집."""
        out: list[dict[str, Any]] = []
        page_token = ""
        while True:
            params: dict[str, Any] = {
                "timeMin": start_at,
                "timeMax": end_at,
                "singleEvents": "true",
                "showDeleted": "false",
                "maxResults": 250,
                "orderBy": "startTime",
            }
            if page_token:
                params["pageToken"] = page_token
            r = self._client.get(
                f"{CALENDAR_API}/events", params=params, headers=self._auth.headers()
            )
            _raise_for_status(r, "calendar search_events")
            body = r.json()
            for ev in body.get("items", []):
                marker = (
                    (ev.get("extendedProperties") or {}).get("private", {}).get(self.MARKER_KEY, "")
                )
                out.append(
                    {
                        "id": ev.get("id", ""),
                        "summary": ev.get("summary", ""),
                        "status": ev.get("status", "confirmed"),
                        "transparency": ev.get("transparency", "opaque"),
                        "start": ev.get("start") or {},
                        "end": ev.get("end") or {},
                        "marker": marker,
                    }
                )
            page_token = body.get("nextPageToken", "")
            if not page_token:
                return out

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        r = self._client.get(f"{CALENDAR_API}/events/{event_id}", headers=self._auth.headers())
        if r.status_code == 404:
            return None
        _raise_for_status(r, "calendar get_event")
        ev = r.json()
        return {
            "id": ev.get("id"),
            "summary": ev.get("summary", ""),
            "status": ev.get("status", "confirmed"),
            "start": ev.get("start") or {},
            "end": ev.get("end") or {},
        }


# ---------- OAuth 토큰 발급 (로컬 1회성 플로우) ----------


def issue_token(
    kind: str, *, client_id: str, client_secret: str, token_path: str | Path, timeout: float = 300.0
) -> Path:
    """loopback redirect로 auth code를 받아 refresh token을 저장한다.

    calendar-agent scripts/authorize.py와 동일한 플로우 + state 검증.
    """
    import secrets

    scopes = SCOPES[kind]
    state = secrets.token_urlsafe(16)
    result: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if "code" in qs:
                result["code"] = qs["code"][0]
                result["state"] = qs.get("state", [""])[0]
                self.send_response(200)
                self.end_headers()
                self.wfile.write("jobMate 인증 완료 — 이 창을 닫아도 됩니다.".encode())
            else:
                result["error"] = qs.get("error", ["unknown"])[0]
                self.send_response(400)
                self.end_headers()

        def log_message(self, *args: Any) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    redirect_uri = f"http://127.0.0.1:{port}/"
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    auth_url = f"{AUTH_URI}?{urllib.parse.urlencode(params)}"
    print(f"다음 URL을 브라우저에서 여세요:\n{auth_url}")
    webbrowser.open(auth_url)

    server.timeout = timeout
    while "code" not in result and "error" not in result:
        server.handle_request()
    server.server_close()
    if "error" in result:
        raise GoogleAuthError(f"oauth error: {result['error']}")
    if result.get("state") != state:
        raise GoogleAuthError("oauth state mismatch")

    with httpx.Client(timeout=30) as client:
        r = client.post(
            TOKEN_URI,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": result["code"],
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            },
        )
    if r.status_code != 200:
        raise GoogleAuthError(f"code exchange failed: {r.status_code} {r.text[:200]}")
    body = r.json()
    if "refresh_token" not in body:
        raise GoogleAuthError("refresh_token not returned — prompt=consent 필요")
    path = Path(token_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    # google-auth Credentials.to_json()과 동일 필드를 포함해
    # calendar-agent의 token_*.json도 그대로 읽을 수 있게 한다
    path.write_text(
        json.dumps(
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": body["refresh_token"],
                "token_uri": TOKEN_URI,
                "scopes": scopes,
            },
            indent=2,
        )
    )
    path.chmod(0o600)
    return path


def load_client_credentials(path: str | Path) -> tuple[str, str]:
    """OAuth client JSON(credentials.json)에서 (client_id, client_secret) 추출."""
    data = json.loads(Path(path).expanduser().read_text())
    client = data.get("installed") or data.get("web") or data
    return client["client_id"], client["client_secret"]
