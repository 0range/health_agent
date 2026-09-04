"""Stable connector contracts independent of database models."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Protocol

from health_agent.google_drive.config import DriveProfile


class GlobalDriveSyncError(RuntimeError):
    """A profile/database invariant failure that must fail the whole run."""


@dataclass(frozen=True, slots=True)
class DriveAccountIdentity:
    permission_id: str
    email: str


@dataclass(frozen=True, slots=True)
class DriveItem:
    file_id: str
    name: str
    mime_type: str
    parent_ids: tuple[str, ...]
    created_time: str | None = None
    modified_time: str | None = None
    version: str | None = None
    head_revision_id: str | None = None
    md5_checksum: str | None = None
    size_bytes: int | None = None
    web_view_link: str | None = None
    drive_id: str | None = None
    can_download: bool = False
    shortcut_target_id: str | None = None
    shortcut_target_mime_type: str | None = None
    trashed: bool = False

    @property
    def revision(self) -> str:
        parts = (
            self.version,
            self.head_revision_id,
            self.md5_checksum,
            self.modified_time,
        )
        return "|".join(part or "" for part in parts) if any(parts) else ""


@dataclass(frozen=True, slots=True)
class DriveChange:
    file_id: str | None
    removed: bool
    item: DriveItem | None
    change_type: str = "file"


@dataclass(frozen=True, slots=True)
class ChangePage:
    changes: tuple[DriveChange, ...]
    next_page_token: str | None
    new_start_page_token: str | None


@dataclass(frozen=True, slots=True)
class ItemPage:
    items: tuple[DriveItem, ...]
    next_page_token: str | None


@dataclass(frozen=True, slots=True)
class DriveProvenance:
    profile_id: str
    root_folder_id: str
    folder_path: tuple[str, ...]
    item: DriveItem
    output_media_type: str
    exported_from_google_native: bool


@dataclass(frozen=True, slots=True)
class ContentReceipt:
    sha256: str
    size_bytes: int
    storage_reference: str
    outcome: str = "medically_imported"
    processing_status: str | None = None
    document_id: str | None = None


@dataclass(frozen=True, slots=True)
class SeenItem:
    profile_id: str
    file_id: str
    revision: str
    root_folder_id: str
    ancestor_folder_ids: tuple[str, ...]
    folder_path: tuple[str, ...]
    source_url: str | None
    source_name: str
    source_mime_type: str
    output_media_type: str | None
    drive_id: str | None
    drive_version: str | None
    created_time: str | None
    modified_time: str | None
    head_revision_id: str | None
    drive_md5_checksum: str | None
    sha256: str | None
    size_bytes: int | None
    storage_reference: str | None
    status: str
    safe_error_code: str | None = None


@dataclass(frozen=True, slots=True)
class SyncReport:
    profile_id: str
    mode: str
    discovered: int = 0
    imported: int = 0
    unchanged: int = 0
    skipped: int = 0
    removed: int = 0
    medically_imported: int = 0
    duplicates: int = 0
    ocr_required: int = 0
    needs_attention: int = 0
    failed: int = 0


class DriveGateway(Protocol):
    def account_identity(self) -> DriveAccountIdentity: ...

    def get_file(self, file_id: str) -> DriveItem: ...

    def list_children(self, folder_id: str, page_token: str | None) -> ItemPage: ...

    def get_start_page_token(self) -> str: ...

    def list_changes(self, page_token: str) -> ChangePage: ...

    def download_chunks(
        self, item: DriveItem, export_media_type: str | None
    ) -> Iterator[bytes]: ...


class ContentConsumer(Protocol):
    def consume(
        self, provenance: DriveProvenance, chunks: Iterable[bytes]
    ) -> ContentReceipt: ...


class ProfileStore(Protocol):
    def load(self, profile_id: str) -> DriveProfile: ...

    def save(self, profile: DriveProfile) -> None: ...


class SyncStateStore(Protocol):
    def sync_lock(self, profile_id: str) -> AbstractContextManager[None]: ...

    def get_cursor(self, profile_id: str) -> str | None: ...

    def set_cursor(self, profile_id: str, cursor: str) -> None: ...

    def clear_cursor(self, profile_id: str) -> None: ...

    def get_seen(self, profile_id: str, file_id: str) -> SeenItem | None: ...

    def retryable_items(self, profile_id: str) -> tuple[SeenItem, ...]: ...

    def record_seen(self, item: SeenItem) -> None: ...

    def mark_removed(self, profile_id: str, file_id: str) -> bool: ...

    def mark_tree_removed(self, profile_id: str, folder_id: str) -> int: ...

    def mark_missing_removed(
        self, profile_id: str, root_folder_ids: tuple[str, ...], seen_file_ids: set[str]
    ) -> int: ...

    def count_seen(self, profile_id: str) -> int: ...

    def counts(self, profile_id: str) -> dict[str, int]: ...

    def begin_sync(self, profile_id: str, mode: str) -> None: ...

    def finish_sync(self, profile_id: str) -> None: ...

    def fail_sync(self, profile_id: str, safe_error_code: str) -> None: ...

    def run_state(self, profile_id: str) -> dict[str, str | None]: ...
