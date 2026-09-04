from __future__ import annotations

import json
from typing import Any

import httplib2
import pytest
from googleapiclient.errors import HttpError
from tenacity import wait_none

from health_agent.gmail import api


class Request:
    def __init__(self, response: dict[str, Any] | None = None, error: Exception | None = None) -> None:
        self.response = response or {}
        self.error = error
        self.calls = 0

    def execute(self, *, num_retries: int) -> dict[str, Any]:
        assert num_retries == 0
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.response


class FakeResource:
    def __init__(self) -> None:
        self.list_response: dict[str, Any] = {}
        self.message_response: dict[str, Any] = {}
        self.attachment_response: dict[str, Any] = {}
        self.history_response: dict[str, Any] = {}
        self.kwargs: list[tuple[str, dict[str, Any]]] = []

    def users(self) -> FakeResource:
        return self

    def messages(self) -> FakeResource:
        return self

    def attachments(self) -> FakeResource:
        return self

    def history(self) -> FakeResource:
        return self

    def list(self, **kwargs: Any) -> Request:
        self.kwargs.append(("list", kwargs))
        if "startHistoryId" in kwargs:
            return Request(self.history_response)
        return Request(self.list_response)

    def get(self, **kwargs: Any) -> Request:
        self.kwargs.append(("get", kwargs))
        if "messageId" in kwargs:
            return Request(self.attachment_response)
        return Request(self.message_response)


def http_error(status: int, reason: str) -> HttpError:
    response = httplib2.Response({"status": str(status)})
    content = json.dumps({"error": {"errors": [{"reason": reason}]}}).encode()
    return HttpError(response, content)


def test_execute_retries_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    request = Request()

    def flaky(*, num_retries: int) -> dict[str, str]:
        request.calls += 1
        if request.calls < 3:
            raise http_error(429, "rateLimitExceeded")
        return {"status": "ok"}

    request.execute = flaky  # type: ignore[method-assign]
    monkeypatch.setattr(api, "wait_random_exponential", lambda **_: wait_none())
    assert api._execute(request) == {"status": "ok"}
    assert request.calls == 3


def test_history_404_is_mapped_to_expired_cursor() -> None:
    with pytest.raises(api.HistoryCursorExpired):
        api._execute(
            Request(error=http_error(404, "notFound")), history_request=True
        )


def test_gateway_parses_nested_mime_headers_and_history() -> None:
    resource = FakeResource()
    resource.message_response = {
        "id": "m1",
        "threadId": "t1",
        "historyId": "10",
        "internalDate": "1725449600000",
        "payload": {
            "partId": "",
            "mimeType": "multipart/mixed",
            "headers": [
                {"name": "Subject", "value": "=?UTF-8?B?0JDQvdCw0LvQuNC30Ys=?="},
                {"name": "From", "value": "Clinic <LAB@EXAMPLE.COM>"},
            ],
            "parts": [
                {
                    "partId": "1",
                    "mimeType": "application/pdf",
                    "filename": "labs.pdf",
                    "headers": [{"name": "Content-Disposition", "value": "attachment"}],
                    "body": {"attachmentId": "a1", "size": 4},
                }
            ],
        },
    }
    resource.history_response = {
        "historyId": "12",
        "history": [
            {
                "messagesAdded": [{"message": {"id": "m1"}}, {"message": {"id": "m1"}}],
                "messagesDeleted": [{"message": {"id": "old"}}],
            }
        ],
    }
    gateway = api.GoogleGmailGateway(resource)  # type: ignore[arg-type]

    message = gateway.get_message("m1")
    history = gateway.list_history("9", None)

    assert message.subject == "Анализы"
    assert message.sender == "lab@example.com"
    assert message.payload.children[0].attachment_id == "a1"
    assert history.added_message_ids == ("m1",)
    assert history.removed_message_ids == ("old",)
    _, history_kwargs = resource.kwargs[-1]
    assert history_kwargs["historyTypes"] == ["messageAdded", "messageDeleted"]


@pytest.mark.parametrize(
    ("status", "reason", "expected"),
    (
        (403, "userRateLimitExceeded", True),
        (403, "domainPolicy", False),
        (401, "authError", False),
        (503, "backendError", True),
    ),
)
def test_only_transient_failures_are_retryable(
    status: int, reason: str, expected: bool
) -> None:
    assert api._is_retryable(http_error(status, reason)) is expected
