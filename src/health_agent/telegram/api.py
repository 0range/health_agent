"""Official Telegram Bot API gateway with mutation-aware failure semantics."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from urllib.parse import quote

import httpx

from health_agent.telegram.types import RemoteFile

BOT_API_ORIGIN = "https://api.telegram.org"
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
ALLOWED_UPDATES = ("message",)
MAX_SAFE_INTEGER = (1 << 63) - 1


class TelegramAPIError(RuntimeError):
    def __init__(self, safe_error_code: str) -> None:
        super().__init__(safe_error_code)
        self.safe_error_code = safe_error_code


class TelegramTransientError(TelegramAPIError):
    """A safe read retry budget was exhausted."""


class TelegramDeferred(TelegramAPIError):
    """Telegram proved rejection and supplied the full future retry time."""

    def __init__(self, retry_at: datetime) -> None:
        super().__init__("api_rate_limited")
        self.retry_at = retry_at


class TelegramDeliveryUnknown(TelegramAPIError):
    """A mutating request may have reached Telegram and must not be retried."""

    def __init__(self, safe_error_code: str = "delivery_unknown") -> None:
        super().__init__(safe_error_code)


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
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
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
        self._clock = clock
        self._max_attempts = max_attempts

    def get_me(self) -> dict[str, object]:
        result = self._post("getMe", {})
        if not isinstance(result, dict):
            raise TelegramAPIError("invalid_get_me_response")
        return cast(dict[str, object], result)

    def get_webhook_url(self) -> str:
        result = self._post("getWebhookInfo", {})
        if not isinstance(result, dict):
            raise TelegramAPIError("invalid_webhook_response")
        url = result.get("url")
        if url is not None and not isinstance(url, str):
            raise TelegramAPIError("invalid_webhook_response")
        return url or ""

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
            raise TelegramAPIError("invalid_updates_response")
        return tuple(cast(dict[str, object], item) for item in result)

    def get_file(self, file_id: str) -> RemoteFile:
        result = self._post("getFile", {"file_id": file_id})
        if not isinstance(result, dict) or not isinstance(result.get("file_path"), str):
            raise TelegramAPIError("invalid_file_response")
        size_value = result.get("file_size")
        size = None if size_value is None else _safe_nonnegative_int(size_value, "file")
        returned_file_id = result.get("file_id")
        unique_id = result.get("file_unique_id")
        if returned_file_id is not None and not isinstance(returned_file_id, str):
            raise TelegramAPIError("invalid_file_response")
        if unique_id is not None and not isinstance(unique_id, str):
            raise TelegramAPIError("invalid_file_response")
        return RemoteFile(
            file_id=returned_file_id or file_id,
            file_unique_id=unique_id or "",
            file_path=cast(str, result["file_path"]),
            file_size=size,
        )

    def download_chunks(self, file_path: str) -> Iterator[bytes]:
        parts = file_path.split("/")
        safe_path = "/".join(quote(part, safe="") for part in parts if part)
        if not safe_path or ".." in parts:
            raise TelegramAPIError("invalid_file_path")
        url = f"{BOT_API_ORIGIN}/file/bot{self._token}/{safe_path}"
        emitted = False
        for attempt in range(self._max_attempts):
            try:
                with self._client.stream("GET", url) as response:
                    # A successful file response is binary and may be large. Do not
                    # call ``response.json()`` here: httpx would first buffer the
                    # entire stream, bypassing our incremental size bound.
                    if response.status_code == 429 and not emitted:
                        raise self._deferred(self._response_json(response))
                    if response.status_code >= 500 and not emitted:
                        if attempt + 1 >= self._max_attempts:
                            raise TelegramTransientError("download_server_error")
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
        result = self._post(
            "sendMessage", {"chat_id": chat_id, "text": text}, mutation=True
        )
        if not isinstance(result, dict):
            raise TelegramDeliveryUnknown("invalid_send_response")
        try:
            return _safe_positive_int(result.get("message_id"), "send")
        except TelegramAPIError:
            raise TelegramDeliveryUnknown("invalid_send_response") from None

    def _post(
        self,
        method: str,
        payload: dict[str, object],
        *,
        read_timeout: float = 20,
        mutation: bool = False,
    ) -> object:
        url = f"{BOT_API_ORIGIN}/bot{self._token}/{method}"
        for attempt in range(self._max_attempts):
            try:
                response = self._client.post(url, json=payload, timeout=read_timeout)
            except httpx.TransportError:
                if mutation:
                    raise TelegramDeliveryUnknown(
                        "delivery_transport_unknown"
                    ) from None
                if attempt + 1 >= self._max_attempts:
                    raise TelegramTransientError("api_transport_error") from None
                self._wait(attempt)
                continue
            data = self._response_json(response)
            if response.status_code == 429:
                raise self._deferred(data)
            if response.status_code >= 500:
                if mutation:
                    raise TelegramDeliveryUnknown("delivery_server_unknown")
                if attempt + 1 >= self._max_attempts:
                    raise TelegramTransientError("api_server_error")
                self._wait(attempt)
                continue
            ok = data.get("ok")
            if response.status_code >= 400 or ok is not True:
                # A malformed 2xx response cannot prove whether a mutation was
                # accepted. Treat it like a lost acknowledgement, never as a
                # definitely retryable application error.
                if mutation and response.status_code < 400 and ok is not False:
                    raise TelegramDeliveryUnknown("invalid_send_response")
                code_value = data.get("error_code")
                code = (
                    response.status_code
                    if code_value is None
                    else _safe_nonnegative_int(code_value, "error")
                )
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

    def _deferred(self, data: dict[str, Any]) -> TelegramDeferred:
        parameters = data.get("parameters")
        retry_value = (
            parameters.get("retry_after") if isinstance(parameters, dict) else None
        )
        seconds = _safe_nonnegative_int(retry_value, "retry_after")
        try:
            retry_at = self._clock().astimezone(UTC) + timedelta(seconds=seconds)
        except (OverflowError, OSError):
            raise TelegramAPIError("invalid_retry_after") from None
        return TelegramDeferred(retry_at)

    def _wait(self, attempt: int) -> None:
        self._sleeper(float(min(2**attempt, 8)))


def _safe_nonnegative_int(value: object, field: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > MAX_SAFE_INTEGER
    ):
        raise TelegramAPIError(f"invalid_{field}_response")
    return value


def _safe_positive_int(value: object, field: str) -> int:
    parsed = _safe_nonnegative_int(value, field)
    if parsed == 0:
        raise TelegramAPIError(f"invalid_{field}_response")
    return parsed
