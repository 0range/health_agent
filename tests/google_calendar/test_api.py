import httpx
import pytest
from google.oauth2.credentials import Credentials

from health_agent.google_calendar.api import CalendarAPIError, GoogleCalendarGateway


def credentials():
    return Credentials(token="access")


class Transport(httpx.BaseTransport):
    def __init__(self, statuses):
        self.statuses = iter(statuses)
        self.calls = []
        self.closed = False

    def close(self):
        self.closed = True

    def handle_request(self, request):
        self.calls.append(request)
        status = next(self.statuses)
        if isinstance(status, Exception):
            raise status
        payload = {
            "id": "event",
            "sub": "subject",
            "email": "a@b.test",
            "email_verified": True,
        }
        return httpx.Response(status, json=payload, request=request)


def test_real_gateway_uses_encoded_path_no_redirects_notifications_and_if_match():
    transport = Transport([200, 200, 200, 200])
    gateway = GoogleCalendarGateway(credentials(), http=transport)
    gateway.get("team%2Fa", "event")
    gateway.insert("team%2Fa", {"id": "event"})
    gateway.patch("team%2Fa", "event", {"summary": "x"}, '"etag"')
    assert (
        gateway.userinfo("https://openidconnect.googleapis.com/v1/userinfo")["sub"]
        == "subject"
    )
    assert gateway._client.follow_redirects is False
    assert "/calendars/team%2Fa/events" in str(transport.calls[0].url)
    assert "sendUpdates=none" in str(transport.calls[1].url)
    assert transport.calls[2].headers["If-Match"] == '"etag"'
    gateway.close()
    assert transport.closed is False


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, "oauth_required"),
        (403, "permission_denied"),
        (429, "rate_limited"),
        (500, "google_unavailable"),
    ],
)
def test_gateway_errors_are_safe_and_never_retry_writes(status, code):
    transport = Transport([status])
    gateway = GoogleCalendarGateway(credentials(), http=transport)
    with pytest.raises(CalendarAPIError) as caught:
        gateway.insert("primary", {"id": "private"})
    assert caught.value.safe_code == code
    assert len(transport.calls) == 1


@pytest.mark.parametrize("operation", ["insert", "patch"])
def test_ambiguous_write_transport_failure_has_one_attempt(operation):
    transport = Transport([httpx.ReadError("ambiguous")])
    gateway = GoogleCalendarGateway(credentials(), http=transport)
    with pytest.raises(CalendarAPIError, match="google_unavailable"):
        if operation == "insert":
            gateway.insert("primary", {"id": "private"})
        else:
            gateway.patch("primary", "private", {"summary": "x"}, '"etag"')
    assert len(transport.calls) == 1
