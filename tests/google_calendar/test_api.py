import json
from types import SimpleNamespace

import pytest
from google.oauth2.credentials import Credentials

from health_agent.google_calendar.api import CalendarAPIError, GoogleCalendarGateway


def credentials():
    return Credentials(token="access")


class Transport:
    def __init__(self, statuses):
        self.statuses = iter(statuses)
        self.calls = []

    def request(self, uri, method="GET", body=None, headers=None, **kwargs):
        self.calls.append((uri, method, body, headers, kwargs))
        status = next(self.statuses)
        payload = {
            "id": "event",
            "sub": "subject",
            "email": "a@b.test",
            "email_verified": True,
        }
        return SimpleNamespace(status=status), json.dumps(payload).encode()


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
    assert all(call[4]["redirections"] == 0 for call in transport.calls)
    assert "/calendars/team%2Fa/events" in transport.calls[0][0]
    assert "sendUpdates=none" in transport.calls[1][0]
    assert transport.calls[2][3]["If-Match"] == '"etag"'


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
