"""Stable Telegram connector contracts independent of agent implementations."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

AttachmentKind = Literal["document", "photo", "voice"]
OutboundStatus = Literal["claimed", "duplicate", "conflict", "deferred", "unknown"]


@dataclass(frozen=True, slots=True)
class MessageContext:
    bot_id: int
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
    # Telegram-provided metadata is untrusted. Parsing decisions must use the
    # signature-derived validated_media_type populated after private staging.
    mime_type: str
    validated_media_type: str | None
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
class VerifiedBotCredential:
    token: str
    bot_id: int
    username: str | None = None


@dataclass(frozen=True, slots=True)
class TelegramStatus:
    token_configured: bool
    credential_verified: bool
    bot_id: int | None
    bot_username: str | None
    webhook_configured: bool | None
    poller_running: bool
    delivery_unknown_count: int
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
    next_retry_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class UpdateClaim:
    bot_id: int
    update_id: int
    owner_id: str
    generation: int
    attempt_count: int
    lease_until: datetime


@dataclass(frozen=True, slots=True)
class ClaimResult:
    status: str
    claim: UpdateClaim | None
    next_retry_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class OutboundReservation:
    status: OutboundStatus
    next_retry_at: datetime | None = None


class HealthQuestionService(Protocol):
    """UI-callable boundary implemented by the real Health Agent."""

    def answer(self, question: HealthQuestion) -> str: ...


class TelegramTextActionService(Protocol):
    """Explicit text actions after authenticated private-chat routing."""

    def handle(self, context: MessageContext, text: str) -> str | None: ...


class HealthCommandService(Protocol):
    """Profile-scoped operational commands exposed to Telegram and future UI."""

    def status(self, command: TelegramCommand) -> str: ...

    def sync(self, command: TelegramCommand) -> str: ...


class MedicalInbox(Protocol):
    """Atomically commit a fully validated, replayable attachment stream.

    Implementations must consume the complete stream before committing and
    deduplicate on ``(profile_id, source_external_id)``. MIME decisions must use
    ``validated_media_type``; ``mime_type`` is untrusted Telegram metadata.
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
    def identity_for_user(
        self, bot_id: int, telegram_user_id: int
    ) -> TelegramIdentity | None: ...

    def identity_for_profile(
        self, bot_id: int, profile_id: UUID
    ) -> TelegramIdentity | None: ...

    def next_offset(self, bot_id: int) -> int | None: ...

    def claim_update(
        self,
        *,
        bot_id: int,
        update_id: int,
        owner_id: str,
        lease_seconds: float,
        telegram_user_id: int | None,
        chat_id: int | None,
        message_id: int | None,
        profile_id: UUID | None,
        kind: str,
    ) -> ClaimResult: ...

    def renew_claim(
        self, claim: UpdateClaim, lease_seconds: float
    ) -> UpdateClaim | None: ...

    def complete_update(
        self, claim: UpdateClaim, status: str, error_code: str | None = None
    ) -> bool: ...

    def defer_update(
        self, claim: UpdateClaim, error_code: str, next_retry_at: datetime
    ) -> bool: ...

    def advance_offset(self, bot_id: int, offset: int) -> None: ...

    def record_poll(self, bot_id: int, error_code: str | None = None) -> None: ...

    def record_attachment(self, claim: UpdateClaim, receipt: InboxReceipt) -> bool: ...

    def reserve_outbound(
        self,
        *,
        bot_id: int,
        delivery_key: str,
        part_index: int,
        profile_id: UUID,
        chat_id: int,
        content_sha256: str,
    ) -> OutboundReservation: ...

    def mark_outbound_sent(
        self,
        bot_id: int,
        profile_id: UUID,
        delivery_key: str,
        part_index: int,
        telegram_message_id: int,
    ) -> bool: ...

    def mark_outbound_failed(
        self,
        bot_id: int,
        profile_id: UUID,
        delivery_key: str,
        part_index: int,
        error_code: str,
        *,
        status: Literal["failed", "unknown", "deferred"],
        next_retry_at: datetime | None = None,
    ) -> bool: ...


class ProfileDirectory(Protocol):
    def exists(self, profile_id: UUID) -> bool: ...
