from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator
from dataclasses import replace

import pytest

from health_agent.google_drive.config import DriveProfile
from health_agent.google_drive.service import (
    FOLDER_MIME_TYPE,
    DriveProfileMismatch,
    DriveService,
)
from health_agent.google_drive.types import (
    ChangePage,
    ContentReceipt,
    DriveChange,
    DriveItem,
    DriveProvenance,
    ItemPage,
    SeenItem,
)


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

    def set_cursor(self, profile_id: str, cursor: str) -> None:
        self.cursors[profile_id] = cursor

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
        changed = 0
        for key, current in list(self.items.items()):
            if (
                key[0] == profile_id
                and current.root_folder_id in root_folder_ids
                and current.file_id not in seen_file_ids
                and current.status != "removed"
            ):
                self.items[key] = replace(current, status="removed")
                changed += 1
        return changed

    def count_seen(self, profile_id: str) -> int:
        return sum(
            key[0] == profile_id and value.status == "imported"
            for key, value in self.items.items()
        )


class FakeGateway:
    def __init__(self, email: str = "alice@example.com") -> None:
        self.email = email
        self.files: dict[str, DriveItem] = {}
        self.children: dict[tuple[str, str | None], ItemPage] = {}
        self.changes: dict[str, ChangePage] = {}
        self.content: dict[str, bytes] = {}
        self.downloads: list[tuple[str, str | None]] = []
        self.start_token = "start-1"

    def account_email(self) -> str:
        return self.email

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
    consumer = MemoryConsumer("alice")
    profile = DriveProfile.create("alice", ["root-folder"]).with_account(
        "alice@example.com"
    )
    service = DriveService(profile, gateway, state, consumer)

    first = service.sync()
    second = service.sync(full=True)

    assert (first.discovered, first.imported, first.skipped) == (3, 3, 1)
    assert (second.imported, second.unchanged) == (0, 3)
    assert state.cursors["alice"] == "start-1"
    assert gateway.downloads == [
        ("blood-pdf", None),
        ("doctor-doc", "application/pdf"),
        ("scan-jpg", None),
    ]
    scan = state.items[("alice", "scan-jpg")]
    assert scan.folder_path == ("Medical", "Scans")
    assert scan.sha256 == hashlib.sha256(b"jpg").hexdigest()
    assert scan.source_url == "https://drive.google.com/file/d/scan-jpg/view"
    assert ("alice", "notes-txt") not in state.items


def test_identical_drive_ids_remain_separate_between_profiles() -> None:
    state = MemoryState()
    alice_gateway = configured_gateway()
    bob_gateway = configured_gateway()
    bob_gateway.email = "bob@example.com"
    alice = DriveProfile.create("alice", ["root-folder"]).with_account(
        "alice@example.com"
    )
    bob = DriveProfile.create("bob", ["root-folder"]).with_account("bob@example.com")

    assert DriveService(alice, alice_gateway, state, MemoryConsumer("alice")).sync().imported == 3
    assert DriveService(bob, bob_gateway, state, MemoryConsumer("bob")).sync().imported == 3
    assert state.items[("alice", "blood-pdf")].storage_reference != state.items[
        ("bob", "blood-pdf")
    ].storage_reference


def test_account_mismatch_stops_before_inventory() -> None:
    gateway = configured_gateway()
    profile = DriveProfile.create("alice", ["root-folder"]).with_account(
        "someone-else@example.com"
    )
    with pytest.raises(DriveProfileMismatch):
        DriveService(profile, gateway, MemoryState(), MemoryConsumer("alice")).sync()
    assert gateway.downloads == []


def test_incremental_sync_pages_changes_and_advances_cursor_only_on_success() -> None:
    gateway = configured_gateway()
    state = MemoryState()
    profile = DriveProfile.create("alice", ["root-folder"]).with_account(
        "alice@example.com"
    )
    DriveService(profile, gateway, state, MemoryConsumer("alice")).sync()
    changed = replace(gateway.files["blood-pdf"], version="2")
    gateway.content["blood-pdf"] = b"new!"
    gateway.changes["start-1"] = ChangePage(
        (DriveChange("blood-pdf", False, changed),), "change-page-2", None
    )
    gateway.changes["change-page-2"] = ChangePage(
        (DriveChange("scan-jpg", True, None),), None, "start-2"
    )

    report = DriveService(profile, gateway, state, MemoryConsumer("alice")).sync()

    assert report.mode == "incremental"
    assert (report.imported, report.removed) == (1, 1)
    assert state.cursors["alice"] == "start-2"
    assert state.items[("alice", "scan-jpg")].status == "removed"


def test_failed_incremental_consumer_does_not_advance_cursor() -> None:
    gateway = configured_gateway()
    state = MemoryState()
    profile = DriveProfile.create("alice", ["root-folder"]).with_account(
        "alice@example.com"
    )
    DriveService(profile, gateway, state, MemoryConsumer("alice")).sync()
    changed = replace(gateway.files["blood-pdf"], version="2")
    gateway.changes["start-1"] = ChangePage(
        (DriveChange("blood-pdf", False, changed),), None, "start-2"
    )

    with pytest.raises(RuntimeError, match="injected consumer"):
        DriveService(
            profile,
            gateway,
            state,
            MemoryConsumer("alice", fail_on="blood-pdf"),
        ).sync()
    assert state.cursors["alice"] == "start-1"


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
    profile = DriveProfile.create("alice", ["root-folder"]).with_account(
        "alice@example.com"
    )

    report = DriveService(profile, gateway, state, MemoryConsumer("alice")).sync()

    assert report.skipped == 2
    assert state.items[("alice", "restricted-pdf")].status == "download_restricted"
    assert state.items[("alice", "medical-form")].status == "unsupported_google_native"
    assert gateway.downloads == []


def test_full_rescan_marks_previously_seen_missing_file_removed() -> None:
    gateway = configured_gateway()
    state = MemoryState()
    profile = DriveProfile.create("alice", ["root-folder"]).with_account(
        "alice@example.com"
    )
    service = DriveService(profile, gateway, state, MemoryConsumer("alice"))
    service.sync()
    gateway.children[("scans-folder", None)] = ItemPage((), None)

    report = service.sync(full=True)

    assert report.removed == 1
    assert state.items[("alice", "scan-jpg")].status == "removed"


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
    profile = DriveProfile.create("alice", ["root-folder"]).with_account(
        "alice@example.com"
    )

    report = DriveService(
        profile, gateway, MemoryState(), MemoryConsumer("alice")
    ).sync()

    assert report.skipped == 1
    assert gateway.downloads == []
