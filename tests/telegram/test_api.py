from __future__ import annotations

import json

import httpx
import pytest

from health_agent.telegram.api import TelegramAPIError, TelegramBotAPI

TOKEN = "123456:super-secret-token"


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
    body = json.loads(requests[0].content)
    assert body == {
        "offset": 4,
        "timeout": 30,
        "limit": 100,
        "allowed_updates": ["message"],
    }


def test_get_file_and_streamed_download() -> None:
    content = b"medical bytes"

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


def test_rate_limit_uses_retry_after_and_retries() -> None:
    calls = 0
    waits: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return _response(
                request,
                {
                    "ok": False,
                    "error_code": 429,
                    "parameters": {"retry_after": 3},
                },
                429,
            )
        return _response(request, {"ok": True, "result": {"message_id": 9}})

    api = TelegramBotAPI(
        TOKEN,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleeper=waits.append,
    )

    assert api.send_message(10, "safe reply") == 9
    assert calls == 2
    assert waits == [3]


def test_server_failure_uses_bounded_retry() -> None:
    calls = 0
    waits: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return _response(request, {"ok": False, "error_code": 500}, 500)
        return _response(request, {"ok": True, "result": {"message_id": 9}})

    api = TelegramBotAPI(
        TOKEN,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleeper=waits.append,
    )

    assert api.send_message(10, "safe reply") == 9
    assert calls == 2
    assert waits == [1]


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


def test_send_message_payload_and_webhook_status() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/getWebhookInfo"):
            return _response(request, {"ok": True, "result": {"url": ""}})
        return _response(request, {"ok": True, "result": {"message_id": 8}})

    api = TelegramBotAPI(
        TOKEN, client=httpx.Client(transport=httpx.MockTransport(handler))
    )

    assert api.get_webhook_url() == ""
    assert api.send_message(55, "reply") == 8
    assert json.loads(requests[1].content) == {"chat_id": 55, "text": "reply"}
