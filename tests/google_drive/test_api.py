from __future__ import annotations

import json

import httplib2
import pytest
from googleapiclient.errors import HttpError
from tenacity import wait_none

from health_agent.google_drive import api


class FlakyRequest:
    def __init__(self) -> None:
        self.calls = 0

    def execute(self, *, num_retries: int) -> dict[str, str]:
        assert num_retries == 0
        self.calls += 1
        if self.calls < 3:
            response = httplib2.Response({"status": "429"})
            raise HttpError(response, b'{"error":{"errors":[]}}')
        return {"status": "ok"}


def test_retries_rate_limit_without_exposing_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = FlakyRequest()
    monkeypatch.setattr(api, "wait_random_exponential", lambda **_: wait_none())
    assert api._execute(request) == {"status": "ok"}
    assert request.calls == 3


@pytest.mark.parametrize(
    ("status", "reason", "expected"),
    (
        (403, "userRateLimitExceeded", True),
        (403, "insufficientFilePermissions", False),
        (401, "authError", False),
        (500, "backendError", True),
    ),
)
def test_only_transient_http_errors_are_retryable(
    status: int, reason: str, expected: bool
) -> None:
    response = httplib2.Response({"status": str(status)})
    content = json.dumps({"error": {"errors": [{"reason": reason}]}}).encode()
    assert api._is_retryable(HttpError(response, content)) is expected
