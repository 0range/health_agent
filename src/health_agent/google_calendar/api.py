"""Bounded no-retry Google Calendar v3 transport."""

from __future__ import annotations

from contextlib import suppress
from typing import Any

import httpx
from google.oauth2.credentials import Credentials

BASE_URL = "https://www.googleapis.com/calendar/v3"


def close_gateway_safely(gateway: object) -> None:
    """Attempt cleanup without replacing an established result or request error."""
    # Cleanup is best-effort. Never expose transport details or misreport a
    # confirmed write as failed because releasing its connection failed.
    with suppress(Exception):
        close = getattr(gateway, "close", None)
        if close is not None:
            close()


class _BorrowedTransport(httpx.BaseTransport):
    def __init__(self, transport: httpx.BaseTransport):
        self._transport = transport

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        return self._transport.handle_request(request)

    def close(self) -> None:
        pass


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
        if not isinstance(credentials.token, str) or not credentials.token:
            raise CalendarAPIError(401)
        self._authorization = f"Bearer {credentials.token}"
        transport = _BorrowedTransport(http) if http is not None else None
        self._client = httpx.Client(
            timeout=timeout_seconds, follow_redirects=False, transport=transport
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _request(
        self,
        method: str,
        url: str,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ):
        try:
            response = self._client.request(
                method,
                url,
                json=body,
                headers={"Authorization": self._authorization, **(headers or {})},
            )
        except httpx.HTTPError as error:
            raise CalendarAPIError() from error
        status = response.status_code
        if status == 404:
            return None
        if status < 200 or status >= 300:
            raise CalendarAPIError(status)
        try:
            value = response.json()
        except ValueError as error:
            raise CalendarAPIError() from error
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
