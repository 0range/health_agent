"""Database-independent Gmail connector contracts and provenance types."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True, slots=True)
class MailboxProfile:
    email: str
    history_id: str


@dataclass(frozen=True, slots=True)
class MessagePage:
    message_ids: tuple[str, ...]
    next_page_token: str | None


@dataclass(frozen=True, slots=True)
class HistoryPage:
    added_message_ids: tuple[str, ...]
    removed_message_ids: tuple[str, ...]
    next_page_token: str | None
    history_id: str


@dataclass(frozen=True, slots=True)
class GmailPart:
    part_id: str
    mime_type: str
    filename: str
    attachment_id: str | None
    body_size: int | None
    body_data: str | None = field(default=None, repr=False)
    disposition: str | None = None
    children: tuple[GmailPart, ...] = ()


@dataclass(frozen=True, slots=True)
class GmailMessage:
    message_id: str
    thread_id: str
    history_id: str
    internal_date_ms: int
    subject: str = field(repr=False)
    sender: str = field(repr=False)
    payload: GmailPart


@dataclass(frozen=True, slots=True)
class AttachmentProvenance:
    profile_id: str
    account_id: str
    account_email: str
    message_id: str
    thread_id: str
    message_history_id: str
    internal_date_ms: int
    part_id: str
    attachment_id: str | None
    filename: str
    source_mime_type: str
    classification: str
    source_uri: str

    @property
    def revision(self) -> str:
        attachment_key = self.attachment_id or "inline"
        return f"{self.message_id}:{self.part_id}:{attachment_key}"


@dataclass(frozen=True, slots=True)
class ImportReceipt:
    sha256: str
    size_bytes: int
    storage_reference: str
    outcome: str = "stored"


@dataclass(frozen=True, slots=True)
class SeenAttachment:
    profile_id: str
    account_id: str
    message_id: str
    part_id: str
    revision: str
    attachment_id: str | None
    filename: str
    mime_type: str
    classification: str
    declared_size_bytes: int | None
    sha256: str | None
    size_bytes: int | None
    storage_reference: str | None
    status: str


@dataclass(frozen=True, slots=True)
class SeenMessage:
    profile_id: str
    account_id: str
    message_id: str
    history_id: str
    internal_date_ms: int
    classification: str
    status: str


@dataclass(frozen=True, slots=True)
class GmailSyncReport:
    profile_id: str
    account_id: str
    mode: str
    messages_seen: int = 0
    attachments_imported: int = 0
    ambiguous: int = 0
    ignored: int = 0
    unchanged: int = 0
    removed: int = 0


class GmailGateway(Protocol):
    def get_profile(self) -> MailboxProfile: ...

    def list_messages(self, query: str, page_token: str | None) -> MessagePage: ...

    def get_message(self, message_id: str) -> GmailMessage: ...

    def list_history(self, history_id: str, page_token: str | None) -> HistoryPage: ...

    def attachment_data(self, message_id: str, attachment_id: str) -> str: ...


class AttachmentImporter(Protocol):
    def import_attachment(
        self, provenance: AttachmentProvenance, chunks: Iterable[bytes]
    ) -> ImportReceipt: ...


class GmailStateStore(Protocol):
    def get_cursor(self, profile_id: str, account_id: str) -> str | None: ...

    def set_cursor(self, profile_id: str, account_id: str, history_id: str) -> None: ...

    def get_message(
        self, profile_id: str, account_id: str, message_id: str
    ) -> SeenMessage | None: ...

    def record_message(self, message: SeenMessage) -> None: ...

    def get_attachment(
        self, profile_id: str, account_id: str, message_id: str, part_id: str
    ) -> SeenAttachment | None: ...

    def record_attachment(self, attachment: SeenAttachment) -> None: ...

    def mark_message_removed(
        self, profile_id: str, account_id: str, message_id: str
    ) -> int: ...

    def counts(self, profile_id: str, account_id: str) -> tuple[int, int, int]: ...


def walk_parts(part: GmailPart) -> Iterator[GmailPart]:
    yield part
    for child in part.children:
        yield from walk_parts(child)
