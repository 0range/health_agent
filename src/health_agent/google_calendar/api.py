"""Bounded no-retry Google Calendar v3 transport."""

from __future__ import annotations

import json
from typing import Any

import httplib2  # type: ignore[import-untyped]
from google.oauth2.credentials import Credentials
from google_auth_httplib2 import AuthorizedHttp  # type: ignore[import-untyped]

BASE_URL = "https://www.googleapis.com/calendar/v3"


class CalendarAPIError(RuntimeError):
    def __init__(self, status: int = 0):
        self.status = status
        self.safe_code = (
            "oauth_required"
            if status == 401
            else "permission_denied"
            if status == 403
            else "rate_limited"
            if status == 429
            else "google_unavailable"
            if status >= 500 or status == 0
            else "calendar_request_failed"
        )
        super().__init__(self.safe_code)


class GoogleCalendarGateway:
    def __init__(
        self, credentials: Credentials, timeout_seconds: int = 30, *, http=None
    ):
        transport = http or httplib2.Http(timeout=timeout_seconds)
        self.http = AuthorizedHttp(
            credentials,
            http=transport,
            refresh_status_codes=(),
            max_refresh_attempts=0,
        )

    def _request(
        self,
        method: str,
        url: str,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ):
        try:
            response, content = self.http.request(
                url,
                method=method,
                body=None if body is None else json.dumps(body),
                headers={"Content-Type": "application/json", **(headers or {})},
                redirections=0,
            )
        except Exception as error:
            raise CalendarAPIError() from error
        status = int(response.status)
        if status == 404:
            return None
        if status < 200 or status >= 300:
            raise CalendarAPIError(status)
        value = json.loads(content or b"{}")
        if not isinstance(value, dict):
            raise CalendarAPIError()
        return value

    def get(self, calendar_id: str, event_id: str):
        return self._request(
            "GET", f"{BASE_URL}/calendars/{calendar_id}/events/{event_id}"
        )

    def insert(self, calendar_id: str, body: dict[str, Any]):
        return self._request(
            "POST", f"{BASE_URL}/calendars/{calendar_id}/events?sendUpdates=none", body
        )

    def patch(
        self, calendar_id: str, event_id: str, body: dict[str, Any], etag: str | None
    ):
        return self._request(
            "PATCH",
            f"{BASE_URL}/calendars/{calendar_id}/events/{event_id}?sendUpdates=none",
            body,
            {"If-Match": etag or ""},
        )

    def userinfo(self, url: str):
        if url != "https://openidconnect.googleapis.com/v1/userinfo":
            raise CalendarAPIError()
        return self._request("GET", url)
