"""Stable Telegram connector contracts independent of bot and agent implementations."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

AttachmentKind = Literal["document", "photo", "voice"]


@dataclass(frozen=True, slots=True)
class MessageContext:
    profile_id: UUID
    telegram_user_id: int
    chat_id: int
    message_id: int
    update_id: int
    sent_at: datetime | None
    received_at: datetime


@dataclass(frozen=True, slots=True)
class HealthQuestion:
    context: MessageContext
    text: str


@dataclass(frozen=True, slots=True)
class TelegramCommand:
    context: MessageContext
    name: Literal["status", "sync"]


@dataclass(frozen=True, slots=True)
class AttachmentProvenance:
    context: MessageContext
    kind: AttachmentKind
    file_id: str
    file_unique_id: str
    file_name: str | None
    mime_type: str
    declared_size_bytes: int | None
    duration_seconds: int | None
    source_external_id: str


@dataclass(frozen=True, slots=True)
class InboxReceipt:
    sha256: str
    size_bytes: int
    status: str
    reply_text: str
    external_reference: str | None = None


@dataclass(frozen=True, slots=True)
class RemoteFile:
    file_id: str
    file_unique_id: str
    file_path: str
    file_size: int | None


@dataclass(frozen=True, slots=True)
class TelegramIdentity:
    telegram_user_id: int
    profile_id: UUID
    private_chat_id: int
    active: bool = True


@dataclass(frozen=True, slots=True)
class TelegramStatus:
    token_configured: bool
    profile_id: UUID | None
    identity_bound: bool
    next_offset: int | None
    last_poll_at: datetime | None
    last_error_code: str | None


@dataclass(frozen=True, slots=True)
class ProcessResult:
    update_id: int
    status: str
    terminal: bool


class HealthQuestionService(Protocol):
    """UI-callable boundary implemented by the real Health Agent."""

    def answer(self, question: HealthQuestion) -> str: ...


class HealthCommandService(Protocol):
    """Profile-scoped operational commands exposed to Telegram and future UI."""

    def status(self, command: TelegramCommand) -> str: ...

    def sync(self, command: TelegramCommand) -> str: ...


class MedicalInbox(Protocol):
    """Consumes one attachment stream and commits only after the stream completes.

    Implementations must deduplicate on ``(profile_id, source_external_id)``. This
    preserves isolation while making a post-commit connector retry harmless.
    """

    def ingest(
        self, provenance: AttachmentProvenance, chunks: Iterable[bytes]
    ) -> InboxReceipt: ...


class TelegramGateway(Protocol):
    def get_me(self) -> dict[str, object]: ...

    def get_webhook_url(self) -> str: ...

    def get_updates(
        self, *, offset: int | None, timeout_seconds: int
    ) -> tuple[dict[str, object], ...]: ...

    def get_file(self, file_id: str) -> RemoteFile: ...

    def download_chunks(self, file_path: str) -> Iterator[bytes]: ...

    def send_message(self, chat_id: int, text: str) -> int: ...


class TelegramState(Protocol):
    def identity_for_user(self, telegram_user_id: int) -> TelegramIdentity | None: ...

    def identity_for_profile(self, profile_id: UUID) -> TelegramIdentity | None: ...

    def next_offset(self) -> int | None: ...

    def begin_update(
        self,
        *,
        update_id: int,
        telegram_user_id: int | None,
        chat_id: int | None,
        message_id: int | None,
        profile_id: UUID | None,
        kind: str,
    ) -> str: ...

    def complete_update(
        self, update_id: int, status: str, error_code: str | None = None
    ) -> None: ...

    def advance_offset(self, offset: int) -> None: ...

    def record_poll(self, error_code: str | None = None) -> None: ...

    def record_attachment(self, update_id: int, receipt: InboxReceipt) -> None: ...

    def reserve_outbound(
        self,
        *,
        delivery_key: str,
        part_index: int,
        profile_id: UUID,
        chat_id: int,
    ) -> bool: ...

    def mark_outbound_sent(
        self, delivery_key: str, part_index: int, telegram_message_id: int
    ) -> None: ...

    def mark_outbound_failed(
        self, delivery_key: str, part_index: int, error_code: str
    ) -> None: ...


class ProfileDirectory(Protocol):
    def exists(self, profile_id: UUID) -> bool: ...
