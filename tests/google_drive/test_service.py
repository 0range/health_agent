from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator
from contextlib import nullcontext
from dataclasses import replace

import httplib2
import pytest
from googleapiclient.errors import HttpError

from health_agent.google_drive.config import DriveProfile
from health_agent.google_drive.service import (
    FOLDER_MIME_TYPE,
    DriveProfileMismatch,
    DriveService,
    SharedDriveUnsupported,
)
from health_agent.google_drive.types import (
    ChangePage,
    ContentReceipt,
    DriveAccountIdentity,
    DriveChange,
    DriveItem,
    DriveProvenance,
    ItemPage,
    SeenItem,
)

ALICE = "11111111-1111-4111-8111-111111111111"
BOB = "22222222-2222-4222-8222-222222222222"


def item(
    file_id: str,
    name: str,
    mime_type: str,
    *,
    parents: tuple[str, ...] = (),
    version: str = "1",
    size: int | None = None,
    can_download: bool = True,
) -> DriveItem:
    return DriveItem(
        file_id=file_id,
        name=name,
        mime_type=mime_type,
        parent_ids=parents,
        version=version,
        size_bytes=size,
        can_download=can_download,
        web_view_link=f"https://drive.google.com/file/d/{file_id}/view",
    )


class MemoryState:
    def __init__(self) -> None:
        self.cursors: dict[str, str] = {}
        self.items: dict[tuple[str, str], SeenItem] = {}

    def get_cursor(self, profile_id: str) -> str | None:
        return self.cursors.get(profile_id)

    def sync_lock(self, profile_id: str):
        return nullcontext()

    def set_cursor(self, profile_id: str, cursor: str) -> None:
        self.cursors[profile_id] = cursor

    def clear_cursor(self, profile_id: str) -> None:
        self.cursors.pop(profile_id, None)

    def get_seen(self, profile_id: str, file_id: str) -> SeenItem | None:
        return self.items.get((profile_id, file_id))

    def record_seen(self, value: SeenItem) -> None:
        self.items[(value.profile_id, value.file_id)] = value

    def mark_removed(self, profile_id: str, file_id: str) -> bool:
        key = (profile_id, file_id)
        current = self.items.get(key)
        if current is None or current.status == "removed":
            return False
        self.items[key] = replace(current, status="removed")
        return True

    def mark_tree_removed(self, profile_id: str, folder_id: str) -> int:
        changed = 0
        for key, current in list(self.items.items()):
            if (
                key[0] == profile_id
                and folder_id in current.ancestor_folder_ids
                and current.status != "removed"
            ):
                self.items[key] = replace(current, status="removed")
                changed += 1
        return changed

    def mark_missing_removed(
        self, profile_id: str, root_folder_ids: tuple[str, ...], seen_file_ids: set[str]
    ) -> int:
        del root_folder_ids
        changed = 0
        for key, current in list(self.items.items()):
            if (
                key[0] == profile_id
                and current.file_id not in seen_file_ids
                and current.status != "removed"
            ):
                self.items[key] = replace(current, status="removed")
                changed += 1
        return changed

    def count_seen(self, profile_id: str) -> int:
        return sum(
            key[0] == profile_id and value.status == "medically_imported"
            for key, value in self.items.items()
        )

    def counts(self, profile_id: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for key, value in self.items.items():
            if key[0] == profile_id:
                counts[value.status] = counts.get(value.status, 0) + 1
        return counts

    def begin_sync(self, profile_id: str, mode: str) -> None:
        pass

    def finish_sync(self, profile_id: str) -> None:
        pass

    def fail_sync(self, profile_id: str, safe_error_code: str) -> None:
        pass

    def run_state(self, profile_id: str) -> dict[str, str | None]:
        return {}


class FakeGateway:
    def __init__(self, email: str = "alice@example.com") -> None:
        self.email = email
        self.files: dict[str, DriveItem] = {}
        self.children: dict[tuple[str, str | None], ItemPage] = {}
        self.changes: dict[str, ChangePage] = {}
        self.content: dict[str, bytes] = {}
        self.downloads: list[tuple[str, str | None]] = []
        self.download_errors: dict[str, Exception] = {}
        self.start_token = "start-1"

    def account_identity(self) -> DriveAccountIdentity:
        return DriveAccountIdentity(f"permission-{self.email}", self.email)

    def get_file(self, file_id: str) -> DriveItem:
        return self.files[file_id]

    def list_children(self, folder_id: str, page_token: str | None) -> ItemPage:
        return self.children.get((folder_id, page_token), ItemPage((), None))

    def get_start_page_token(self) -> str:
        return self.start_token

    def list_changes(self, page_token: str) -> ChangePage:
        return self.changes[page_token]

    def download_chunks(
        self, drive_item: DriveItem, export_media_type: str | None
    ) -> Iterator[bytes]:
        self.downloads.append((drive_item.file_id, export_media_type))
        if drive_item.file_id in self.download_errors:
            raise self.download_errors[drive_item.file_id]
        content = self.content[drive_item.file_id]
        yield content[:2]
        yield content[2:]


class MemoryConsumer:
    def __init__(self, profile_id: str, *, fail_on: str | None = None) -> None:
        self.profile_id = profile_id
        self.fail_on = fail_on
        self.received: list[DriveProvenance] = []

    def consume(
        self, provenance: DriveProvenance, chunks: Iterable[bytes]
    ) -> ContentReceipt:
        assert provenance.profile_id == self.profile_id
        if provenance.item.file_id == self.fail_on:
            raise RuntimeError("injected consumer failure")
        content = b"".join(chunks)
        self.received.append(provenance)
        return ContentReceipt(
            hashlib.sha256(content).hexdigest(),
            len(content),
            f"vault/{self.profile_id}/{provenance.item.file_id}",
        )


def configured_gateway() -> FakeGateway:
    gateway = FakeGateway()
    root = item("root-folder", "Medical", FOLDER_MIME_TYPE)
    scans = item("scans-folder", "Scans", FOLDER_MIME_TYPE, parents=(root.file_id,))
    pdf = item(
        "blood-pdf", "blood.pdf", "application/pdf", parents=(root.file_id,), size=4
    )
    doc = item(
        "doctor-doc",
        "Doctor note",
        "application/vnd.google-apps.document",
        parents=(root.file_id,),
    )
    text = item("notes-txt", "notes.txt", "text/plain", parents=(root.file_id,))
    image = item(
        "scan-jpg", "scan.jpg", "image/jpeg", parents=(scans.file_id,), size=3
    )
    gateway.files = {value.file_id: value for value in (root, scans, pdf, doc, text, image)}
    gateway.children[(root.file_id, None)] = ItemPage((scans, pdf), "next")
    gateway.children[(root.file_id, "next")] = ItemPage((doc, text), None)
    gateway.children[(scans.file_id, None)] = ItemPage((image,), None)
    gateway.content = {pdf.file_id: b"labs", doc.file_id: b"pdf", image.file_id: b"jpg"}
    return gateway


def test_full_scan_is_recursive_paginated_streamed_and_idempotent() -> None:
    gateway = configured_gateway()
    state = MemoryState()
    consumer = MemoryConsumer(ALICE)
    profile = DriveProfile.create(ALICE, ["root-folder"]).with_account(
        "permission-alice@example.com", "alice@example.com"
    )
    service = DriveService(profile, gateway, state, consumer)

    first = service.sync()
    second = service.sync(full=True)

    assert (first.discovered, first.imported, first.skipped) == (3, 3, 1)
    assert (second.imported, second.unchanged) == (0, 3)
    assert state.cursors[ALICE] == "start-1"
    assert gateway.downloads == [
        ("blood-pdf", None),
        ("doctor-doc", "application/pdf"),
        ("scan-jpg", None),
    ]
    scan = state.items[(ALICE, "scan-jpg")]
    assert scan.folder_path == ("Medical", "Scans")
    assert scan.sha256 == hashlib.sha256(b"jpg").hexdigest()
    assert scan.source_url == "https://drive.google.com/file/d/scan-jpg/view"
    assert state.items[(ALICE, "notes-txt")].status == "unsupported_media_type"


def test_identical_drive_ids_remain_separate_between_profiles() -> None:
    state = MemoryState()
    alice_gateway = configured_gateway()
    bob_gateway = configured_gateway()
    bob_gateway.email = "bob@example.com"
    alice = DriveProfile.create(ALICE, ["root-folder"]).with_account(
        "permission-alice@example.com", "alice@example.com"
    )
    bob = DriveProfile.create(BOB, ["root-folder"]).with_account(
        "permission-bob@example.com", "bob@example.com"
    )

    assert DriveService(alice, alice_gateway, state, MemoryConsumer(ALICE)).sync().imported == 3
    assert DriveService(bob, bob_gateway, state, MemoryConsumer(BOB)).sync().imported == 3
    assert state.items[(ALICE, "blood-pdf")].storage_reference != state.items[
        (BOB, "blood-pdf")
    ].storage_reference


def test_account_mismatch_stops_before_inventory() -> None:
    gateway = configured_gateway()
    profile = DriveProfile.create(ALICE, ["root-folder"]).with_account(
        "someone-else", "someone-else@example.com"
    )
    with pytest.raises(DriveProfileMismatch):
        DriveService(profile, gateway, MemoryState(), MemoryConsumer(ALICE)).sync()
    assert gateway.downloads == []


def test_incremental_sync_pages_changes_and_advances_cursor_only_on_success() -> None:
    gateway = configured_gateway()
    state = MemoryState()
    profile = DriveProfile.create(ALICE, ["root-folder"]).with_account(
        "permission-alice@example.com", "alice@example.com"
    )
    DriveService(profile, gateway, state, MemoryConsumer(ALICE)).sync()
    changed = replace(gateway.files["blood-pdf"], version="2")
    gateway.content["blood-pdf"] = b"new!"
    gateway.changes["start-1"] = ChangePage(
        (DriveChange("blood-pdf", False, changed),), "change-page-2", None
    )
    gateway.changes["change-page-2"] = ChangePage(
        (DriveChange("scan-jpg", True, None),), None, "start-2"
    )

    report = DriveService(profile, gateway, state, MemoryConsumer(ALICE)).sync()

    assert report.mode == "incremental"
    assert (report.imported, report.removed) == (1, 1)
    assert state.cursors[ALICE] == "start-2"
    assert state.items[(ALICE, "scan-jpg")].status == "removed"


def test_failed_incremental_consumer_is_recorded_and_cursor_advances() -> None:
    gateway = configured_gateway()
    state = MemoryState()
    profile = DriveProfile.create(ALICE, ["root-folder"]).with_account(
        "permission-alice@example.com", "alice@example.com"
    )
    DriveService(profile, gateway, state, MemoryConsumer(ALICE)).sync()
    changed = replace(gateway.files["blood-pdf"], version="2")
    gateway.changes["start-1"] = ChangePage(
        (DriveChange("blood-pdf", False, changed),), None, "start-2"
    )

    report = DriveService(
        profile,
        gateway,
        state,
        MemoryConsumer(ALICE, fail_on="blood-pdf"),
    ).sync()
    assert report.failed == 1
    assert state.cursors[ALICE] == "start-2"
    assert state.items[(ALICE, "blood-pdf")].status == "processing_failed"


def test_download_restriction_and_unsupported_google_native_are_explicit() -> None:
    gateway = configured_gateway()
    root = gateway.files["root-folder"]
    restricted = item(
        "restricted-pdf",
        "restricted.pdf",
        "application/pdf",
        parents=(root.file_id,),
        size=10,
        can_download=False,
    )
    form = item(
        "medical-form",
        "Form",
        "application/vnd.google-apps.form",
        parents=(root.file_id,),
    )
    gateway.children[(root.file_id, None)] = ItemPage((restricted, form), None)
    state = MemoryState()
    profile = DriveProfile.create(ALICE, ["root-folder"]).with_account(
        "permission-alice@example.com", "alice@example.com"
    )

    report = DriveService(profile, gateway, state, MemoryConsumer(ALICE)).sync()

    assert report.skipped == 2
    assert state.items[(ALICE, "restricted-pdf")].status == "download_restricted"
    assert state.items[(ALICE, "medical-form")].status == "unsupported_google_native"
    assert gateway.downloads == []


def test_full_rescan_marks_previously_seen_missing_file_removed() -> None:
    gateway = configured_gateway()
    state = MemoryState()
    profile = DriveProfile.create(ALICE, ["root-folder"]).with_account(
        "permission-alice@example.com", "alice@example.com"
    )
    service = DriveService(profile, gateway, state, MemoryConsumer(ALICE))
    service.sync()
    gateway.children[("scans-folder", None)] = ItemPage((), None)

    report = service.sync(full=True)

    assert report.removed == 1
    assert state.items[(ALICE, "scan-jpg")].status == "removed"


def test_shortcut_is_not_followed_outside_configured_root() -> None:
    gateway = configured_gateway()
    root = gateway.files["root-folder"]
    shortcut = DriveItem(
        file_id="external-shortcut",
        name="Other person's archive",
        mime_type="application/vnd.google-apps.shortcut",
        parent_ids=(root.file_id,),
        shortcut_target_id="external-folder",
        shortcut_target_mime_type=FOLDER_MIME_TYPE,
    )
    gateway.children[(root.file_id, None)] = ItemPage((shortcut,), None)
    profile = DriveProfile.create(ALICE, ["root-folder"]).with_account(
        "permission-alice@example.com", "alice@example.com"
    )

    report = DriveService(
        profile, gateway, MemoryState(), MemoryConsumer(ALICE)
    ).sync()

    assert report.skipped == 1
    assert gateway.downloads == []


def test_one_bad_file_does_not_block_later_valid_file() -> None:
    gateway = configured_gateway()
    root = gateway.files["root-folder"]
    bad = item("bad-pdf", "bad.pdf", "application/pdf", parents=(root.file_id,))
    good = gateway.files["blood-pdf"]
    gateway.children[(root.file_id, None)] = ItemPage((bad, good), None)
    gateway.content[bad.file_id] = b"bad"
    state = MemoryState()
    profile = DriveProfile.create(ALICE, [root.file_id]).with_account(
        "permission-alice@example.com", "alice@example.com"
    )

    report = DriveService(
        profile, gateway, state, MemoryConsumer(ALICE, fail_on=bad.file_id)
    ).sync()

    assert (report.failed, report.medically_imported) == (1, 1)
    assert state.items[(ALICE, bad.file_id)].status == "processing_failed"
    assert state.items[(ALICE, good.file_id)].status == "medically_imported"
    assert state.cursors[ALICE] == "start-1"


def test_oversized_export_gets_machine_status_and_does_not_abort() -> None:
    gateway = configured_gateway()
    response = httplib2.Response({"status": "403"})
    gateway.download_errors["doctor-doc"] = HttpError(
        response,
        b'{"error":{"errors":[{"reason":"exportSizeLimitExceeded"}]}}',
    )
    state = MemoryState()
    profile = DriveProfile.create(ALICE, ["root-folder"]).with_account(
        "permission-alice@example.com", "alice@example.com"
    )

    report = DriveService(profile, gateway, state, MemoryConsumer(ALICE)).sync()

    assert report.failed == 1
    assert state.items[(ALICE, "doctor-doc")].status == "too_large"
    assert state.items[(ALICE, "doctor-doc")].safe_error_code == "too_large"
    assert state.items[(ALICE, "scan-jpg")].status == "medically_imported"


def test_unchanged_content_refreshes_folder_path() -> None:
    gateway = configured_gateway()
    state = MemoryState()
    profile = DriveProfile.create(ALICE, ["root-folder"]).with_account(
        "permission-alice@example.com", "alice@example.com"
    )
    DriveService(profile, gateway, state, MemoryConsumer(ALICE)).sync()
    renamed = replace(gateway.files["scans-folder"], name="Renamed scans")
    gateway.files[renamed.file_id] = renamed
    gateway.children[("root-folder", None)] = ItemPage((renamed,), None)

    DriveService(profile, gateway, state, MemoryConsumer(ALICE)).sync(full=True)

    assert state.items[(ALICE, "scan-jpg")].folder_path == (
        "Medical",
        "Renamed scans",
    )


def test_trash_and_untrash_are_reconciled_incrementally() -> None:
    gateway = configured_gateway()
    state = MemoryState()
    profile = DriveProfile.create(ALICE, ["root-folder"]).with_account(
        "permission-alice@example.com", "alice@example.com"
    )
    DriveService(profile, gateway, state, MemoryConsumer(ALICE)).sync()
    trashed = replace(gateway.files["blood-pdf"], trashed=True, version="2")
    gateway.changes["start-1"] = ChangePage(
        (DriveChange("blood-pdf", False, trashed),), None, "start-2"
    )
    DriveService(profile, gateway, state, MemoryConsumer(ALICE)).sync()
    assert state.items[(ALICE, "blood-pdf")].status == "removed"

    restored = replace(trashed, trashed=False, version="3")
    gateway.changes["start-2"] = ChangePage(
        (DriveChange("blood-pdf", False, restored),), None, "start-3"
    )
    DriveService(profile, gateway, state, MemoryConsumer(ALICE)).sync()
    assert state.items[(ALICE, "blood-pdf")].status == "medically_imported"


def test_shared_drive_root_is_rejected_before_inventory() -> None:
    gateway = configured_gateway()
    gateway.files["root-folder"] = replace(
        gateway.files["root-folder"], drive_id="shared-drive-1"
    )
    profile = DriveProfile.create(ALICE, ["root-folder"]).with_account(
        "permission-alice@example.com", "alice@example.com"
    )

    with pytest.raises(SharedDriveUnsupported, match="shared-drive"):
        DriveService(profile, gateway, MemoryState(), MemoryConsumer(ALICE)).sync()


def test_drive_membership_change_is_ignored_without_file_key_error() -> None:
    gateway = configured_gateway()
    state = MemoryState()
    profile = DriveProfile.create(ALICE, ["root-folder"]).with_account(
        "permission-alice@example.com", "alice@example.com"
    )
    DriveService(profile, gateway, state, MemoryConsumer(ALICE)).sync()
    gateway.changes["start-1"] = ChangePage(
        (DriveChange(None, False, None, change_type="drive"),), None, "start-2"
    )

    report = DriveService(profile, gateway, state, MemoryConsumer(ALICE)).sync()

    assert report.mode == "incremental"
    assert state.cursors[ALICE] == "start-2"
