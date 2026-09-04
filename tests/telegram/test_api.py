from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from health_agent.telegram.api import (
    TelegramAPIError,
    TelegramBotAPI,
    TelegramDeferred,
    TelegramDeliveryUnknown,
)

TOKEN = "123456:super-secret-token"
NOW = datetime(2026, 9, 4, tzinfo=UTC)


def _response(
    request: httpx.Request, payload: object, status: int = 200
) -> httpx.Response:
    return httpx.Response(status, json=payload, request=request)


def test_long_poll_uses_offset_timeout_and_message_allowlist() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _response(request, {"ok": True, "result": [{"update_id": 4}]})

    api = TelegramBotAPI(
        TOKEN, client=httpx.Client(transport=httpx.MockTransport(handler))
    )

    assert api.get_updates(offset=4, timeout_seconds=30) == ({"update_id": 4},)
    assert json.loads(requests[0].content) == {
        "offset": 4,
        "timeout": 30,
        "limit": 100,
        "allowed_updates": ["message"],
    }


def test_read_method_retries_server_failure() -> None:
    calls = 0
    waits: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return _response(request, {"ok": False, "error_code": 500}, 500)
        return _response(request, {"ok": True, "result": {"url": ""}})

    api = TelegramBotAPI(
        TOKEN,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleeper=waits.append,
    )

    assert api.get_webhook_url() == ""
    assert calls == 2
    assert waits == [1]


@pytest.mark.parametrize("failure", ["transport", "server"])
def test_send_message_never_retries_ambiguous_failure(failure: str) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if failure == "transport":
            raise httpx.ReadTimeout("accepted response lost", request=request)
        return _response(request, {"ok": False, "error_code": 500}, 500)

    api = TelegramBotAPI(
        TOKEN,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleeper=lambda _: pytest.fail("mutating retry must not sleep"),
    )

    with pytest.raises(TelegramDeliveryUnknown):
        api.send_message(10, "medical reply")
    assert calls == 1


def test_full_retry_after_is_returned_as_typed_deferral_without_sleep() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _response(
            request,
            {
                "ok": False,
                "error_code": 429,
                "parameters": {"retry_after": 600},
            },
            429,
        )

    api = TelegramBotAPI(
        TOKEN,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleeper=lambda _: pytest.fail(
            "retry_after must be deferred, not blocked/capped"
        ),
        clock=lambda: NOW,
    )

    with pytest.raises(TelegramDeferred) as captured:
        api.send_message(10, "medical reply")
    assert captured.value.retry_at == NOW + timedelta(seconds=600)


def test_get_file_and_streamed_download() -> None:
    content = b"%PDF-medical bytes"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getFile"):
            return _response(
                request,
                {
                    "ok": True,
                    "result": {
                        "file_id": "file-1",
                        "file_unique_id": "unique-1",
                        "file_path": "documents/lab.pdf",
                        "file_size": len(content),
                    },
                },
            )
        return httpx.Response(200, content=content, request=request)

    api = TelegramBotAPI(
        TOKEN, client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    remote = api.get_file("file-1")

    assert remote.file_size == len(content)
    assert b"".join(api.download_chunks(remote.file_path)) == content


def test_malformed_numeric_objects_are_sanitized() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getFile"):
            return _response(
                request,
                {
                    "ok": True,
                    "result": {"file_path": "file", "file_size": "huge"},
                },
            )
        return _response(request, {"ok": True, "result": {"message_id": "bad"}})

    api = TelegramBotAPI(
        TOKEN, client=httpx.Client(transport=httpx.MockTransport(handler))
    )

    with pytest.raises(TelegramAPIError, match="invalid_file_response"):
        api.get_file("file")
    with pytest.raises(TelegramDeliveryUnknown, match="invalid_send_response"):
        api.send_message(1, "reply")


def test_malformed_success_envelope_for_send_is_delivery_unknown() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _response(request, {"unexpected": "shape"})

    api = TelegramBotAPI(
        TOKEN,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleeper=lambda _: pytest.fail("ambiguous mutation must not retry"),
    )

    with pytest.raises(TelegramDeliveryUnknown, match="invalid_send_response"):
        api.send_message(1, "reply")
    assert calls == 1


def test_successful_download_is_not_buffered_before_first_chunk() -> None:
    yielded = 0
    first = b"%PDF-" + (b"a" * (1024 * 1024 - 5))
    second = b"b" * (1024 * 1024)
    third = b"c" * (1024 * 1024)

    class GuardedStream(httpx.SyncByteStream):
        def __iter__(self):
            nonlocal yielded
            for chunk in (first, second, third):
                yielded += 1
                yield chunk

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=GuardedStream(), request=request)

    api = TelegramBotAPI(
        TOKEN, client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    chunks = api.download_chunks("documents/lab.pdf")

    assert next(chunks) == first
    assert yielded == 1
    assert b"".join(chunks) == second + third


def test_non_retryable_error_never_leaks_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _response(
            request, {"ok": False, "error_code": 403, "description": TOKEN}, 403
        )

    api = TelegramBotAPI(
        TOKEN, client=httpx.Client(transport=httpx.MockTransport(handler))
    )

    with pytest.raises(TelegramAPIError) as captured:
        api.send_message(10, "hello")
    assert captured.value.safe_error_code == "api_error_403"
    assert TOKEN not in str(captured.value)
