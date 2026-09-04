"""Official Google Drive v3 gateway; it exposes read operations only."""

from __future__ import annotations

import io
import json
from collections.abc import Callable, Iterator
from typing import Any, cast

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import Resource, build  # type: ignore[import-untyped]
from googleapiclient.errors import HttpError  # type: ignore[import-untyped]
from googleapiclient.http import MediaIoBaseDownload  # type: ignore[import-untyped]
from tenacity import (
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_random_exponential,
)

from health_agent.google_drive.types import (
    ChangePage,
    DriveChange,
    DriveItem,
    ItemPage,
)

_FILE_FIELDS = (
    "id,name,mimeType,parents,createdTime,modifiedTime,version,headRevisionId,"
    "md5Checksum,size,webViewLink,driveId,capabilities(canDownload),"
    "shortcutDetails(targetId,targetMimeType)"
)
_RETRYABLE_REASONS = {
    "backendError",
    "internalError",
    "rateLimitExceeded",
    "userRateLimitExceeded",
}


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
    retryer = Retrying(
        stop=stop_after_attempt(5),
        wait=wait_random_exponential(multiplier=1, max=16),
        retry=retry_if_exception(_is_retryable),
        reraise=True,
    )
    return cast(T, retryer(call))


def _execute(request: Any) -> dict[str, Any]:
    return cast(dict[str, Any], _retry(lambda: request.execute(num_retries=0)))


def _parse_item(data: dict[str, Any]) -> DriveItem:
    shortcut = data.get("shortcutDetails") or {}
    capabilities = data.get("capabilities") or {}
    size = data.get("size")
    return DriveItem(
        file_id=str(data["id"]),
        name=str(data.get("name", "")),
        mime_type=str(data.get("mimeType", "application/octet-stream")),
        parent_ids=tuple(str(value) for value in data.get("parents", ())),
        created_time=data.get("createdTime"),
        modified_time=data.get("modifiedTime"),
        version=None if data.get("version") is None else str(data["version"]),
        head_revision_id=data.get("headRevisionId"),
        md5_checksum=data.get("md5Checksum"),
        size_bytes=None if size is None else int(size),
        web_view_link=data.get("webViewLink"),
        drive_id=data.get("driveId"),
        can_download=bool(capabilities.get("canDownload", False)),
        shortcut_target_id=shortcut.get("targetId"),
        shortcut_target_mime_type=shortcut.get("targetMimeType"),
    )


class GoogleDriveGateway:
    """Thin, mockable wrapper over the official Drive client."""

    def __init__(self, service: Resource) -> None:
        self._service = service

    @classmethod
    def from_credentials(cls, credentials: Credentials) -> GoogleDriveGateway:
        return cls(build("drive", "v3", credentials=credentials, cache_discovery=False))

    def account_email(self) -> str:
        response = _execute(self._service.about().get(fields="user(emailAddress)"))
        return str(response["user"]["emailAddress"]).casefold()

    def get_file(self, file_id: str) -> DriveItem:
        response = _execute(
            self._service.files().get(
                fileId=file_id,
                fields=_FILE_FIELDS,
                supportsAllDrives=True,
            )
        )
        return _parse_item(response)

    def list_children(self, folder_id: str, page_token: str | None) -> ItemPage:
        response = _execute(
            self._service.files().list(
                q=f"'{folder_id}' in parents and trashed = false",
                spaces="drive",
                pageSize=1000,
                pageToken=page_token,
                fields=f"nextPageToken,files({_FILE_FIELDS})",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
        )
        return ItemPage(
            tuple(_parse_item(value) for value in response.get("files", ())),
            response.get("nextPageToken"),
        )

    def get_start_page_token(self) -> str:
        response = _execute(
            self._service.changes().getStartPageToken(supportsAllDrives=True)
        )
        return str(response["startPageToken"])

    def list_changes(self, page_token: str) -> ChangePage:
        response = _execute(
            self._service.changes().list(
                pageToken=page_token,
                pageSize=1000,
                spaces="drive",
                includeRemoved=True,
                includeItemsFromAllDrives=True,
                supportsAllDrives=True,
                fields=(
                    "nextPageToken,newStartPageToken,"
                    f"changes(fileId,removed,file({_FILE_FIELDS}))"
                ),
            )
        )
        changes = tuple(
            DriveChange(
                file_id=str(value["fileId"]),
                removed=bool(value.get("removed", False)),
                item=(
                    _parse_item(value["file"])
                    if isinstance(value.get("file"), dict)
                    else None
                ),
            )
            for value in response.get("changes", ())
        )
        return ChangePage(
            changes,
            response.get("nextPageToken"),
            response.get("newStartPageToken"),
        )

    def download_chunks(
        self, item: DriveItem, export_media_type: str | None
    ) -> Iterator[bytes]:
        if export_media_type is None:
            request = self._service.files().get_media(
                fileId=item.file_id, supportsAllDrives=True
            )
        else:
            request = self._service.files().export_media(
                fileId=item.file_id, mimeType=export_media_type
            )

        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(buffer, request, chunksize=1024 * 1024)
        complete = False
        while not complete:
            _, complete = _retry(lambda: downloader.next_chunk(num_retries=0))
            chunk = buffer.getvalue()
            if chunk:
                yield chunk
            buffer.seek(0)
            buffer.truncate(0)
