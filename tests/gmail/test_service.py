from __future__ import annotations

import base64
import hashlib
from collections.abc import Iterable
from dataclasses import replace

import pytest

from health_agent.gmail.api import HistoryCursorExpired
from health_agent.gmail.config import GmailAccount
from health_agent.gmail.service import (
    GmailAccountMismatch,
    GmailService,
    InvalidAttachmentEncoding,
    iter_base64url_chunks,
)
from health_agent.gmail.types import (
    AttachmentProvenance,
    GmailMessage,
    GmailPart,
    HistoryPage,
    ImportReceipt,
    MailboxProfile,
    MessagePage,
    SeenAttachment,
    SeenMessage,
)

PROFILE_A = "11111111-1111-1111-1111-111111111111"
PROFILE_B = "22222222-2222-2222-2222-222222222222"


def encoded(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


class MemoryState:
    def __init__(self) -> None:
        self.cursors: dict[tuple[str, str], str] = {}
        self.messages: dict[tuple[str, str, str], SeenMessage] = {}
        self.attachments: dict[tuple[str, str, str, str], SeenAttachment] = {}

    def get_cursor(self, profile_id: str, account_id: str) -> str | None:
        return self.cursors.get((profile_id, account_id))

    def set_cursor(self, profile_id: str, account_id: str, history_id: str) -> None:
        self.cursors[(profile_id, account_id)] = history_id

    def get_message(
        self, profile_id: str, account_id: str, message_id: str
    ) -> SeenMessage | None:
        return self.messages.get((profile_id, account_id, message_id))

    def record_message(self, message: SeenMessage) -> None:
        self.messages[(message.profile_id, message.account_id, message.message_id)] = message

    def get_attachment(
        self, profile_id: str, account_id: str, message_id: str, part_id: str
    ) -> SeenAttachment | None:
        return self.attachments.get((profile_id, account_id, message_id, part_id))

    def record_attachment(self, attachment: SeenAttachment) -> None:
        self.attachments[
            (
                attachment.profile_id,
                attachment.account_id,
                attachment.message_id,
                attachment.part_id,
            )
        ] = attachment

    def mark_message_removed(
        self, profile_id: str, account_id: str, message_id: str
    ) -> int:
        changed = 0
        key = (profile_id, account_id, message_id)
        current = self.messages.get(key)
        if current is not None and current.status != "removed":
            self.messages[key] = replace(current, status="removed")
            changed += 1
        for attachment_key, attachment in list(self.attachments.items()):
            if (
                attachment_key[:3] == key
                and attachment.status != "removed"
            ):
                self.attachments[attachment_key] = replace(attachment, status="removed")
                changed += 1
        return changed

    def counts(self, profile_id: str, account_id: str) -> tuple[int, int, int]:
        messages = sum(
            key[:2] == (profile_id, account_id) and value.status != "removed"
            for key, value in self.messages.items()
        )
        imported = sum(
            key[:2] == (profile_id, account_id) and value.status == "imported"
            for key, value in self.attachments.items()
        )
        ambiguous = sum(
            key[:2] == (profile_id, account_id) and value.status == "ambiguous"
            for key, value in self.attachments.items()
        )
        return messages, imported, ambiguous


class FakeGateway:
    def __init__(self, email: str = "alice@example.com") -> None:
        self.profile_values = [MailboxProfile(email, "100")]
        self.message_pages: dict[str | None, MessagePage] = {}
        self.messages: dict[str, GmailMessage] = {}
        self.history_pages: dict[str | None, HistoryPage] = {}
        self.attachments: dict[tuple[str, str], str] = {}
        self.queries: list[str] = []
        self.history_error: Exception | None = None
        self.profile_calls = 0

    def get_profile(self) -> MailboxProfile:
        value = self.profile_values[min(self.profile_calls, len(self.profile_values) - 1)]
        self.profile_calls += 1
        return value

    def list_messages(self, query: str, page_token: str | None) -> MessagePage:
        self.queries.append(query)
        return self.message_pages.get(page_token, MessagePage((), None))

    def get_message(self, message_id: str) -> GmailMessage:
        return self.messages[message_id]

    def list_history(self, history_id: str, page_token: str | None) -> HistoryPage:
        if self.history_error is not None:
            raise self.history_error
        return self.history_pages[page_token]

    def attachment_data(self, message_id: str, attachment_id: str) -> str:
        return self.attachments[(message_id, attachment_id)]


class MemoryImporter:
    def __init__(self, profile_id: str, account_id: str, fail: bool = False) -> None:
        self.profile_id = profile_id
        self.account_id = account_id
        self.fail = fail
        self.imports: list[tuple[AttachmentProvenance, bytes]] = []

    def import_attachment(
        self, provenance: AttachmentProvenance, chunks: Iterable[bytes]
    ) -> ImportReceipt:
        assert provenance.profile_id == self.profile_id
        assert provenance.account_id == self.account_id
        if self.fail:
            raise RuntimeError("injected importer failed")
        content = b"".join(chunks)
        self.imports.append((provenance, content))
        return ImportReceipt(
            hashlib.sha256(content).hexdigest(),
            len(content),
            f"vault/{self.profile_id}/{self.account_id}/{provenance.part_id}",
        )


def nested_message(message_id: str = "m1", history_id: str = "90") -> GmailMessage:
    external_pdf = GmailPart(
        "1", "application/pdf", "Результаты анализов.pdf", "a1", 4
    )
    ambiguous = GmailPart("2", "image/jpeg", "scan.jpg", "a2", 3)
    inline_logo = GmailPart(
        "3", "image/png", "logo.png", None, 2, encoded(b"xx"), "inline"
    )
    nested = GmailPart(
        "container", "multipart/related", "", None, None, children=(ambiguous, inline_logo)
    )
    root = GmailPart(
        "", "multipart/mixed", "", None, None, children=(external_pdf, nested)
    )
    return GmailMessage(
        message_id,
        "thread-1",
        history_id,
        1_725_449_600_000,
        "Your documents",
        "clinic@example.com",
        root,
    )


def test_full_scan_is_paginated_nested_conservative_and_idempotent() -> None:
    gateway = FakeGateway()
    message = nested_message()
    trusted_inline = GmailPart(
        "4",
        "application/octet-stream",
        "document.pdf",
        None,
        5,
        encoded(b"hello"),
    )
    second = GmailMessage(
        "m2",
        "thread-2",
        "91",
        1_725_449_700_000,
        "Laboratory report",
        "other@example.com",
        GmailPart("", "multipart/mixed", "", None, None, children=(trusted_inline,)),
    )
    gateway.messages = {"m1": message, "m2": second}
    gateway.message_pages[None] = MessagePage(("m1",), "p2")
    gateway.message_pages["p2"] = MessagePage(("m2", "m1"), None)
    gateway.attachments[("m1", "a1")] = encoded(b"labs")
    gateway.attachments[("m1", "a2")] = encoded(b"jpg")
    state = MemoryState()
    importer = MemoryImporter(PROFILE_A, "personal")
    account = GmailAccount.create("personal")
    service = GmailService(PROFILE_A, account, gateway, state, importer)

    first = service.sync()
    second_report = service.sync(full=True)

    assert gateway.queries == [
        "newer_than:7d has:attachment",
        "newer_than:7d has:attachment",
        "newer_than:7d has:attachment",
        "newer_than:7d has:attachment",
    ]
    assert (first.messages_seen, first.attachments_imported, first.ambiguous) == (2, 2, 1)
    assert first.ignored == 1
    assert second_report.attachments_imported == 0
    assert second_report.unchanged == 4
    assert state.cursors[(PROFILE_A, "personal")] == "100"
    assert [content for _, content in importer.imports] == [b"labs", b"hello"]
    assert state.attachments[(PROFILE_A, "personal", "m1", "2")].status == "ambiguous"
    assert state.attachments[(PROFILE_A, "personal", "m1", "3")].filename == ""


def test_same_message_id_is_isolated_by_profile_and_account() -> None:
    state = MemoryState()
    for profile_id, account_id, email in (
        (PROFILE_A, "personal", "alice@example.com"),
        (PROFILE_A, "work", "alice.work@example.com"),
        (PROFILE_B, "personal", "bob@example.com"),
    ):
        gateway = FakeGateway(email)
        gateway.message_pages[None] = MessagePage(("m1",), None)
        gateway.messages["m1"] = nested_message()
        gateway.attachments[("m1", "a1")] = encoded(b"labs")
        gateway.attachments[("m1", "a2")] = encoded(b"jpg")
        account = GmailAccount.create(account_id).with_email(email)
        report = GmailService(
            profile_id,
            account,
            gateway,
            state,
            MemoryImporter(profile_id, account_id),
        ).sync()
        assert report.attachments_imported == 1
    assert len([value for value in state.attachments.values() if value.status == "imported"]) == 3


def test_incremental_pages_added_and_removed_then_advances_cursor() -> None:
    state = MemoryState()
    state.set_cursor(PROFILE_A, "personal", "100")
    gateway = FakeGateway()
    gateway.messages["m2"] = nested_message("m2", "110")
    gateway.attachments[("m2", "a1")] = encoded(b"labs")
    gateway.attachments[("m2", "a2")] = encoded(b"jpg")
    state.record_message(
        SeenMessage(PROFILE_A, "personal", "old", "90", 1000, "ignored", "processed")
    )
    gateway.history_pages[None] = HistoryPage(("m2",), (), "p2", "110")
    gateway.history_pages["p2"] = HistoryPage(("m2",), ("old",), None, "111")

    report = GmailService(
        PROFILE_A,
        GmailAccount.create("personal").with_email("alice@example.com"),
        gateway,
        state,
        MemoryImporter(PROFILE_A, "personal"),
    ).sync()

    assert report.mode == "incremental"
    assert report.attachments_imported == 1
    assert report.removed == 1
    assert state.cursors[(PROFILE_A, "personal")] == "111"
    assert state.messages[(PROFILE_A, "personal", "old")].status == "removed"


def test_expired_history_recovers_with_configured_lookback() -> None:
    state = MemoryState()
    state.set_cursor(PROFILE_A, "personal", "old")
    gateway = FakeGateway()
    gateway.history_error = HistoryCursorExpired("expired")
    gateway.profile_values = [
        MailboxProfile("alice@example.com", "200"),
        MailboxProfile("alice@example.com", "201"),
    ]
    gateway.message_pages[None] = MessagePage((), None)

    report = GmailService(
        PROFILE_A,
        GmailAccount.create("personal", initial_lookback_days=12),
        gateway,
        state,
        MemoryImporter(PROFILE_A, "personal"),
    ).sync()

    assert report.mode == "recovery"
    assert gateway.queries == ["newer_than:12d has:attachment"]
    assert state.cursors[(PROFILE_A, "personal")] == "201"


def test_importer_failure_never_advances_incremental_cursor() -> None:
    state = MemoryState()
    state.set_cursor(PROFILE_A, "personal", "100")
    gateway = FakeGateway()
    gateway.messages["m2"] = GmailMessage(
        "m2",
        "t2",
        "110",
        1000,
        "Medical laboratory",
        "sender@example.com",
        GmailPart(
            "", "multipart/mixed", "", None, None,
            children=(GmailPart("1", "application/pdf", "labs.pdf", "a1", 4),),
        ),
    )
    gateway.attachments[("m2", "a1")] = encoded(b"labs")
    gateway.history_pages[None] = HistoryPage(("m2",), (), None, "110")

    with pytest.raises(RuntimeError, match="injected importer"):
        GmailService(
            PROFILE_A,
            GmailAccount.create("personal"),
            gateway,
            state,
            MemoryImporter(PROFILE_A, "personal", fail=True),
        ).sync()
    assert state.cursors[(PROFILE_A, "personal")] == "100"


def test_account_mismatch_stops_before_mail_listing() -> None:
    gateway = FakeGateway("wrong@example.com")
    account = GmailAccount.create("personal").with_email("alice@example.com")
    with pytest.raises(GmailAccountMismatch):
        GmailService(
            PROFILE_A, account, gateway, MemoryState(), MemoryImporter(PROFILE_A, "personal")
        ).sync()
    assert gateway.queries == []


def test_base64url_decoder_streams_unpadded_data_and_rejects_invalid() -> None:
    content = bytes(range(256)) * 1000
    assert b"".join(iter_base64url_chunks(encoded(content), 17)) == content
    with pytest.raises(InvalidAttachmentEncoding):
        b"".join(iter_base64url_chunks("%%%", 4))


def test_invalid_attachment_encoding_is_kept_internal_and_scan_can_resume() -> None:
    gateway = FakeGateway()
    gateway.message_pages[None] = MessagePage(("m1",), None)
    gateway.messages["m1"] = GmailMessage(
        "m1",
        "thread-1",
        "90",
        1_725_449_600_000,
        "Medical laboratory",
        "clinic@example.com",
        GmailPart(
            "",
            "multipart/mixed",
            "",
            None,
            None,
            children=(GmailPart("1", "application/pdf", "labs.pdf", "a1", 4),),
        ),
    )
    gateway.attachments[("m1", "a1")] = "%%%"
    state = MemoryState()

    report = GmailService(
        PROFILE_A,
        GmailAccount.create("personal"),
        gateway,
        state,
        MemoryImporter(PROFILE_A, "personal"),
    ).sync()

    assert report.ambiguous == 1
    assert report.attachments_imported == 0
    assert state.cursors[(PROFILE_A, "personal")] == "100"
    seen = state.attachments[(PROFILE_A, "personal", "m1", "1")]
    assert seen.status == "invalid_encoding"
    assert seen.filename == "labs.pdf"
