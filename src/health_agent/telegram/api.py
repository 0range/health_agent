"""Minimal official Telegram Bot API gateway with sanitized failures."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from typing import Any, cast
from urllib.parse import quote

import httpx

from health_agent.telegram.types import RemoteFile

BOT_API_ORIGIN = "https://api.telegram.org"
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
ALLOWED_UPDATES = ("message",)


class TelegramAPIError(RuntimeError):
    def __init__(self, safe_error_code: str) -> None:
        super().__init__(safe_error_code)
        self.safe_error_code = safe_error_code


class TelegramTransientError(TelegramAPIError):
    """A bounded retry budget was exhausted."""


class TelegramWebhookConfigured(TelegramAPIError):
    def __init__(self) -> None:
        super().__init__("webhook_configured")


class TelegramBotAPI:
    def __init__(
        self,
        token: str,
        *,
        client: httpx.Client | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        max_attempts: int = 4,
    ) -> None:
        if (
            not token
            or ":" not in token
            or any(character.isspace() for character in token)
        ):
            raise ValueError("Telegram bot token has an invalid format")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self._token = token
        self._client = client or httpx.Client(timeout=httpx.Timeout(10, read=50))
        self._sleeper = sleeper
        self._max_attempts = max_attempts

    def get_me(self) -> dict[str, object]:
        return cast(dict[str, object], self._post("getMe", {}))

    def get_webhook_url(self) -> str:
        result = self._post("getWebhookInfo", {})
        if not isinstance(result, dict):
            raise TelegramAPIError("invalid_api_response")
        return str(result.get("url") or "")

    def require_long_polling(self) -> None:
        if self.get_webhook_url():
            raise TelegramWebhookConfigured()

    def get_updates(
        self, *, offset: int | None, timeout_seconds: int
    ) -> tuple[dict[str, object], ...]:
        if timeout_seconds < 1 or timeout_seconds > 50:
            raise ValueError("long-poll timeout must be between 1 and 50 seconds")
        payload: dict[str, object] = {
            "timeout": timeout_seconds,
            "limit": 100,
            "allowed_updates": list(ALLOWED_UPDATES),
        }
        if offset is not None:
            payload["offset"] = offset
        result = self._post("getUpdates", payload, read_timeout=timeout_seconds + 10)
        if not isinstance(result, list) or not all(
            isinstance(item, dict) for item in result
        ):
            raise TelegramAPIError("invalid_api_response")
        return tuple(cast(dict[str, object], item) for item in result)

    def get_file(self, file_id: str) -> RemoteFile:
        result = self._post("getFile", {"file_id": file_id})
        if not isinstance(result, dict) or not result.get("file_path"):
            raise TelegramAPIError("invalid_file_response")
        size = result.get("file_size")
        return RemoteFile(
            file_id=str(result.get("file_id") or file_id),
            file_unique_id=str(result.get("file_unique_id") or ""),
            file_path=str(result["file_path"]),
            file_size=None if size is None else int(size),
        )

    def download_chunks(self, file_path: str) -> Iterator[bytes]:
        safe_path = "/".join(
            quote(part, safe="") for part in file_path.split("/") if part
        )
        if not safe_path or ".." in file_path.split("/"):
            raise TelegramAPIError("invalid_file_path")
        url = f"{BOT_API_ORIGIN}/file/bot{self._token}/{safe_path}"
        emitted = False
        for attempt in range(self._max_attempts):
            try:
                with self._client.stream("GET", url) as response:
                    if response.status_code == 429 and not emitted:
                        self._wait_for_response(response, attempt)
                        continue
                    if response.status_code >= 500 and not emitted:
                        self._wait(attempt)
                        continue
                    if response.status_code >= 400:
                        raise TelegramAPIError(f"download_http_{response.status_code}")
                    size = 0
                    for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        emitted = True
                        size += len(chunk)
                        if size > MAX_DOWNLOAD_BYTES:
                            raise TelegramAPIError("file_too_large")
                        yield chunk
                    return
            except TelegramAPIError:
                raise
            except httpx.TransportError:
                if emitted or attempt + 1 >= self._max_attempts:
                    raise TelegramTransientError("download_transport_error") from None
                self._wait(attempt)
        raise TelegramTransientError("download_retry_exhausted")

    def send_message(self, chat_id: int, text: str) -> int:
        result = self._post("sendMessage", {"chat_id": chat_id, "text": text})
        if not isinstance(result, dict) or result.get("message_id") is None:
            raise TelegramAPIError("invalid_send_response")
        return int(result["message_id"])

    def _post(
        self, method: str, payload: dict[str, object], *, read_timeout: float = 20
    ) -> object:
        url = f"{BOT_API_ORIGIN}/bot{self._token}/{method}"
        for attempt in range(self._max_attempts):
            try:
                response = self._client.post(url, json=payload, timeout=read_timeout)
            except httpx.TransportError:
                if attempt + 1 >= self._max_attempts:
                    raise TelegramTransientError("api_transport_error") from None
                self._wait(attempt)
                continue
            data = self._response_json(response)
            if response.status_code == 429:
                if attempt + 1 >= self._max_attempts:
                    raise TelegramTransientError("api_rate_limited")
                self._wait_for_data(data, attempt)
                continue
            if response.status_code >= 500:
                if attempt + 1 >= self._max_attempts:
                    raise TelegramTransientError("api_server_error")
                self._wait(attempt)
                continue
            if response.status_code >= 400 or data.get("ok") is not True:
                code = int(data.get("error_code") or response.status_code or 0)
                raise TelegramAPIError(f"api_error_{code}")
            return data.get("result")
        raise TelegramTransientError("api_retry_exhausted")

    @staticmethod
    def _response_json(response: httpx.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def _wait_for_response(self, response: httpx.Response, attempt: int) -> None:
        self._wait_for_data(self._response_json(response), attempt)

    def _wait_for_data(self, data: dict[str, Any], attempt: int) -> None:
        parameters = data.get("parameters")
        retry_after = (
            parameters.get("retry_after") if isinstance(parameters, dict) else None
        )
        delay = (
            float(retry_after) if isinstance(retry_after, (int, float)) else 2**attempt
        )
        self._sleeper(min(max(delay, 0), 30))

    def _wait(self, attempt: int) -> None:
        self._sleeper(float(min(2**attempt, 8)))
