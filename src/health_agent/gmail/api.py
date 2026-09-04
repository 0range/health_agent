"""Official Gmail v1 read gateway with bounded retry behavior."""

from __future__ import annotations

import json
from collections.abc import Callable
from email.header import decode_header, make_header
from email.utils import parseaddr
from typing import Any, cast

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import Resource, build  # type: ignore[import-untyped]
from googleapiclient.errors import HttpError  # type: ignore[import-untyped]
from tenacity import (
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_random_exponential,
)

from health_agent.gmail.types import (
    GmailMessage,
    GmailPart,
    HistoryPage,
    MailboxProfile,
    MessagePage,
)

_RETRYABLE_REASONS = {
    "backendError",
    "internalError",
    "rateLimitExceeded",
    "userRateLimitExceeded",
}


class HistoryCursorExpired(RuntimeError):
    """Gmail no longer retains the requested history range."""


class GmailItemUnavailable(RuntimeError):
    """A message or attachment disappeared after it was listed."""


def _is_retryable(error: BaseException) -> bool:
    if not isinstance(error, HttpError):
        return False
    status = int(getattr(error.resp, "status", 0))
    if status == 429 or status >= 500:
        return True
    if status != 403:
        return False
    try:
        payload = json.loads(error.content.decode("utf-8"))
        reasons = {
            detail.get("reason")
            for detail in payload.get("error", {}).get("errors", [])
            if isinstance(detail, dict)
        }
    except (AttributeError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return bool(reasons & _RETRYABLE_REASONS)


def _retry[T](call: Callable[[], T]) -> T:
    return cast(
        T,
        Retrying(
            stop=stop_after_attempt(5),
            wait=wait_random_exponential(multiplier=1, max=16),
            retry=retry_if_exception(_is_retryable),
            reraise=True,
        )(call),
    )


def _execute(request: Any, *, history_request: bool = False) -> dict[str, Any]:
    try:
        return cast(
            dict[str, Any], _retry(lambda: request.execute(num_retries=0))
        )
    except HttpError as error:
        if history_request and int(getattr(error.resp, "status", 0)) == 404:
            raise HistoryCursorExpired("Gmail history cursor expired") from error
        raise


class GoogleGmailGateway:
    """Expose only Gmail methods required by the read-only connector."""

    def __init__(self, service: Resource) -> None:
        self._service = service

    @classmethod
    def from_credentials(cls, credentials: Credentials) -> GoogleGmailGateway:
        return cls(build("gmail", "v1", credentials=credentials, cache_discovery=False))

    def get_profile(self) -> MailboxProfile:
        value = _execute(self._service.users().getProfile(userId="me"))
        return MailboxProfile(
            email=str(value["emailAddress"]).casefold(),
            history_id=str(value["historyId"]),
        )

    def list_messages(self, query: str, page_token: str | None) -> MessagePage:
        value = _execute(
            self._service.users().messages().list(
                userId="me",
                q=query,
                maxResults=500,
                pageToken=page_token,
                includeSpamTrash=False,
            )
        )
        return MessagePage(
            tuple(str(message["id"]) for message in value.get("messages", ())),
            value.get("nextPageToken"),
        )

    def get_message(self, message_id: str) -> GmailMessage:
        try:
            value = _execute(
                self._service.users().messages().get(
                    userId="me", id=message_id, format="full"
                )
            )
        except HttpError as error:
            if int(getattr(error.resp, "status", 0)) == 404:
                raise GmailItemUnavailable("Gmail message is no longer available") from error
            raise
        payload = value.get("payload") or {}
        headers = _headers(payload)
        return GmailMessage(
            message_id=str(value["id"]),
            thread_id=str(value.get("threadId", "")),
            history_id=str(value["historyId"]),
            internal_date_ms=int(value["internalDate"]),
            subject=_decode_header(headers.get("subject", "")),
            sender=parseaddr(_decode_header(headers.get("from", "")))[1].casefold(),
            payload=_parse_part(payload),
        )

    def list_history(self, history_id: str, page_token: str | None) -> HistoryPage:
        value = _execute(
            self._service.users().history().list(
                userId="me",
                startHistoryId=history_id,
                historyTypes=["messageAdded", "messageDeleted"],
                maxResults=500,
                pageToken=page_token,
            ),
            history_request=True,
        )
        added: dict[str, None] = {}
        removed: dict[str, None] = {}
        for event in value.get("history", ()):
            for entry in event.get("messagesAdded", ()):
                added[str(entry["message"]["id"])] = None
            for entry in event.get("messagesDeleted", ()):
                removed[str(entry["message"]["id"])] = None
        return HistoryPage(
            tuple(added),
            tuple(removed),
            value.get("nextPageToken"),
            str(value["historyId"]),
        )

    def attachment_data(self, message_id: str, attachment_id: str) -> str:
        try:
            value = _execute(
                self._service.users()
                .messages()
                .attachments()
                .get(userId="me", messageId=message_id, id=attachment_id)
            )
        except HttpError as error:
            if int(getattr(error.resp, "status", 0)) == 404:
                raise GmailItemUnavailable(
                    "Gmail attachment is no longer available"
                ) from error
            raise
        return str(value["data"])


def _headers(payload: dict[str, Any]) -> dict[str, str]:
    return {
        str(header.get("name", "")).casefold(): str(header.get("value", ""))
        for header in payload.get("headers", ())
    }


def _decode_header(value: str) -> str:
    try:
        return str(make_header(decode_header(value)))
    except (LookupError, UnicodeDecodeError):
        return value


def _parse_part(value: dict[str, Any]) -> GmailPart:
    body = value.get("body") or {}
    headers = _headers(value)
    size = body.get("size")
    return GmailPart(
        part_id=str(value.get("partId", "")),
        mime_type=str(value.get("mimeType", "application/octet-stream")).casefold(),
        filename=_decode_header(str(value.get("filename", ""))),
        attachment_id=body.get("attachmentId"),
        body_size=None if size is None else int(size),
        body_data=body.get("data"),
        disposition=headers.get("content-disposition"),
        children=tuple(_parse_part(part) for part in value.get("parts", ())),
    )
