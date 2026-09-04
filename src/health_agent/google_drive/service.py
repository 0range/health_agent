"""Profile-safe orchestration for full and incremental Drive synchronization."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from health_agent.google_drive.config import DriveProfile
from health_agent.google_drive.types import (
    ContentConsumer,
    DriveGateway,
    DriveItem,
    DriveProvenance,
    SeenItem,
    SyncReport,
    SyncStateStore,
)

FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
SHORTCUT_MIME_TYPE = "application/vnd.google-apps.shortcut"
PDF_MIME_TYPE = "application/pdf"

_BINARY_MIME_TYPES = {
    PDF_MIME_TYPE,
    "image/heic",
    "image/heif",
    "image/jpeg",
    "image/png",
    "image/tiff",
    "image/webp",
}
_BINARY_SUFFIX_MIME_TYPES = {
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".pdf": PDF_MIME_TYPE,
    ".png": "image/png",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".webp": "image/webp",
}
_GOOGLE_NATIVE_PDF_EXPORT = {
    "application/vnd.google-apps.document",
    "application/vnd.google-apps.drawing",
    "application/vnd.google-apps.presentation",
    "application/vnd.google-apps.spreadsheet",
}
_GOOGLE_NATIVE_PREFIX = "application/vnd.google-apps."


class DriveProfileMismatch(RuntimeError):
    """Raised before sync when a profile token belongs to another account."""


class InvalidDriveRoot(ValueError):
    """Raised when a configured ID is accessible but is not a folder."""


@dataclass(frozen=True, slots=True)
class DriveStatus:
    profile_id: str
    account_email: str | None
    root_count: int
    has_cursor: bool
    imported_count: int


@dataclass(slots=True)
class _Stats:
    discovered: int = 0
    imported: int = 0
    unchanged: int = 0
    skipped: int = 0
    removed: int = 0

    def report(self, profile_id: str, mode: str) -> SyncReport:
        return SyncReport(
            profile_id=profile_id,
            mode=mode,
            discovered=self.discovered,
            imported=self.imported,
            unchanged=self.unchanged,
            skipped=self.skipped,
            removed=self.removed,
        )


class DriveService:
    """A reusable application service for CLI and a future localhost panel."""

    def __init__(
        self,
        profile: DriveProfile,
        gateway: DriveGateway,
        state: SyncStateStore,
        consumer: ContentConsumer,
    ) -> None:
        self.profile = profile
        self.gateway = gateway
        self.state = state
        self.consumer = consumer
        self._metadata_cache: dict[str, DriveItem] = {}
        self._encountered_file_ids: set[str] = set()

    def verify_account(self) -> str:
        actual = self.gateway.account_email().casefold()
        expected = self.profile.account_email
        if expected is not None and actual != expected.casefold():
            raise DriveProfileMismatch(
                f"profile {self.profile.profile_id!r} expects {expected!r}, not {actual!r}"
            )
        return actual

    def status(self) -> DriveStatus:
        return DriveStatus(
            profile_id=self.profile.profile_id,
            account_email=self.profile.account_email,
            root_count=len(self.profile.root_folder_ids),
            has_cursor=self.state.get_cursor(self.profile.profile_id) is not None,
            imported_count=self.state.count_seen(self.profile.profile_id),
        )

    def sync(self, *, full: bool = False) -> SyncReport:
        self.verify_account()
        cursor = self.state.get_cursor(self.profile.profile_id)
        if full or cursor is None:
            return self._full_sync()
        return self._incremental_sync(cursor)

    def _full_sync(self) -> SyncReport:
        # Taking the token before inventory avoids missing a mutation during the scan.
        start_token = self.gateway.get_start_page_token()
        stats = _Stats()
        self._encountered_file_ids.clear()
        for root_id in self.profile.root_folder_ids:
            root = self._get_file(root_id)
            if root.mime_type != FOLDER_MIME_TYPE:
                raise InvalidDriveRoot(f"configured Drive root {root_id!r} is not a folder")
            self._scan_tree(root, root_id, (root.name,), (root.file_id,), stats)
        stats.removed += self.state.mark_missing_removed(
            self.profile.profile_id,
            self.profile.root_folder_ids,
            self._encountered_file_ids,
        )
        self.state.set_cursor(self.profile.profile_id, start_token)
        return stats.report(self.profile.profile_id, "full")

    def _scan_tree(
        self,
        root: DriveItem,
        root_id: str,
        root_path: tuple[str, ...],
        root_ancestors: tuple[str, ...],
        stats: _Stats,
    ) -> None:
        queue = deque([(root, root_path, root_ancestors)])
        visited_folders: set[str] = set()
        while queue:
            folder, folder_path, ancestors = queue.popleft()
            if folder.file_id in visited_folders:
                continue
            visited_folders.add(folder.file_id)
            token: str | None = None
            while True:
                page = self.gateway.list_children(folder.file_id, token)
                for child in page.items:
                    self._metadata_cache[child.file_id] = child
                    if child.mime_type == FOLDER_MIME_TYPE:
                        queue.append(
                            (
                                child,
                                (*folder_path, child.name),
                                (*ancestors, child.file_id),
                            )
                        )
                    elif child.mime_type == SHORTCUT_MIME_TYPE:
                        # A shortcut target is not a descendant of the configured root.
                        # Following it would make later removals/changes ambiguous.
                        stats.skipped += 1
                    else:
                        self._process_item(child, root_id, ancestors, folder_path, stats)
                if page.next_page_token is None:
                    break
                token = page.next_page_token

    def _incremental_sync(self, cursor: str) -> SyncReport:
        stats = _Stats()
        page_token = cursor
        final_token: str | None = None
        while True:
            page = self.gateway.list_changes(page_token)
            for change in page.changes:
                if change.removed or change.item is None:
                    if self.state.mark_removed(self.profile.profile_id, change.file_id):
                        stats.removed += 1
                    stats.removed += self.state.mark_tree_removed(
                        self.profile.profile_id, change.file_id
                    )
                    continue

                item = change.item
                self._metadata_cache[item.file_id] = item
                location = self._location_under_root(item)
                if location is None:
                    if item.mime_type == FOLDER_MIME_TYPE:
                        stats.removed += self.state.mark_tree_removed(
                            self.profile.profile_id, item.file_id
                        )
                    elif self.state.mark_removed(self.profile.profile_id, item.file_id):
                        stats.removed += 1
                    continue

                root_id, ancestors, folder_path = location
                if item.mime_type == FOLDER_MIME_TYPE:
                    self._scan_tree(
                        item,
                        root_id,
                        (*folder_path, item.name),
                        (*ancestors, item.file_id),
                        stats,
                    )
                elif item.mime_type == SHORTCUT_MIME_TYPE:
                    stats.skipped += 1
                else:
                    self._process_item(item, root_id, ancestors, folder_path, stats)

            if page.next_page_token is None:
                final_token = page.new_start_page_token
                break
            page_token = page.next_page_token
        if final_token is None:
            raise RuntimeError("Drive Changes API ended without a new start page token")
        self.state.set_cursor(self.profile.profile_id, final_token)
        return stats.report(self.profile.profile_id, "incremental")

    def _process_item(
        self,
        item: DriveItem,
        root_id: str,
        ancestors: tuple[str, ...],
        folder_path: tuple[str, ...],
        stats: _Stats,
    ) -> None:
        self._encountered_file_ids.add(item.file_id)
        output_media_type, export = _download_format(item)
        if output_media_type is None:
            if item.mime_type.startswith(_GOOGLE_NATIVE_PREFIX):
                self._record_without_content(
                    item,
                    root_id,
                    ancestors,
                    folder_path,
                    status="unsupported_google_native",
                )
            stats.skipped += 1
            return

        stats.discovered += 1
        previous = self.state.get_seen(self.profile.profile_id, item.file_id)
        if (
            previous is not None
            and previous.revision == item.revision
            and previous.status == "imported"
        ):
            stats.unchanged += 1
            return
        if not item.can_download:
            self._record_without_content(
                item,
                root_id,
                ancestors,
                folder_path,
                status="download_restricted",
                output_media_type=output_media_type,
            )
            stats.skipped += 1
            return

        provenance = DriveProvenance(
            profile_id=self.profile.profile_id,
            root_folder_id=root_id,
            folder_path=folder_path,
            item=item,
            output_media_type=output_media_type,
            exported_from_google_native=export,
        )
        receipt = self.consumer.consume(
            provenance,
            self.gateway.download_chunks(item, output_media_type if export else None),
        )
        if not export and item.size_bytes is not None and receipt.size_bytes != item.size_bytes:
            raise RuntimeError(
                f"download size mismatch for Drive file {item.file_id!r}: "
                f"expected {item.size_bytes}, received {receipt.size_bytes}"
            )
        self.state.record_seen(
            SeenItem(
                profile_id=self.profile.profile_id,
                file_id=item.file_id,
                revision=item.revision,
                root_folder_id=root_id,
                ancestor_folder_ids=ancestors,
                folder_path=folder_path,
                source_url=item.web_view_link or _fallback_url(item.file_id),
                source_name=item.name,
                source_mime_type=item.mime_type,
                output_media_type=output_media_type,
                drive_id=item.drive_id,
                drive_version=item.version,
                created_time=item.created_time,
                modified_time=item.modified_time,
                head_revision_id=item.head_revision_id,
                drive_md5_checksum=item.md5_checksum,
                sha256=receipt.sha256,
                size_bytes=receipt.size_bytes,
                storage_reference=receipt.storage_reference,
                status="imported",
            )
        )
        stats.imported += 1

    def _record_without_content(
        self,
        item: DriveItem,
        root_id: str,
        ancestors: tuple[str, ...],
        folder_path: tuple[str, ...],
        *,
        status: str,
        output_media_type: str | None = None,
    ) -> None:
        self.state.record_seen(
            SeenItem(
                profile_id=self.profile.profile_id,
                file_id=item.file_id,
                revision=item.revision,
                root_folder_id=root_id,
                ancestor_folder_ids=ancestors,
                folder_path=folder_path,
                source_url=item.web_view_link or _fallback_url(item.file_id),
                source_name=item.name,
                source_mime_type=item.mime_type,
                output_media_type=output_media_type,
                drive_id=item.drive_id,
                drive_version=item.version,
                created_time=item.created_time,
                modified_time=item.modified_time,
                head_revision_id=item.head_revision_id,
                drive_md5_checksum=item.md5_checksum,
                sha256=None,
                size_bytes=item.size_bytes,
                storage_reference=None,
                status=status,
            )
        )

    def _location_under_root(
        self, item: DriveItem
    ) -> tuple[str, tuple[str, ...], tuple[str, ...]] | None:
        roots = set(self.profile.root_folder_ids)
        if item.file_id in roots:
            return item.file_id, (), ()
        queue: deque[tuple[str, tuple[str, ...], tuple[str, ...]]] = deque(
            (parent, (), ()) for parent in item.parent_ids
        )
        visited: set[str] = set()
        while queue:
            folder_id, descendant_ids, descendant_names = queue.popleft()
            if folder_id in visited:
                continue
            visited.add(folder_id)
            folder = self._get_file(folder_id)
            ids = (folder.file_id, *descendant_ids)
            names = (folder.name, *descendant_names)
            if folder.file_id in roots:
                return folder.file_id, ids, names
            for parent_id in folder.parent_ids:
                queue.append((parent_id, ids, names))
        return None

    def _get_file(self, file_id: str) -> DriveItem:
        if file_id not in self._metadata_cache:
            self._metadata_cache[file_id] = self.gateway.get_file(file_id)
        return self._metadata_cache[file_id]


def _download_format(item: DriveItem) -> tuple[str | None, bool]:
    if item.mime_type in _BINARY_MIME_TYPES:
        return item.mime_type, False
    if item.mime_type in _GOOGLE_NATIVE_PDF_EXPORT:
        return PDF_MIME_TYPE, True
    if item.mime_type == "application/octet-stream":
        suffix = "." + item.name.rsplit(".", 1)[-1].casefold() if "." in item.name else ""
        return _BINARY_SUFFIX_MIME_TYPES.get(suffix), False
    return None, False


def _fallback_url(file_id: str) -> str:
    return f"https://drive.google.com/file/d/{file_id}/view"
