"""Private-chat update routing and durable long-poll lifecycle."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from health_agent.telegram.api import (
    MAX_DOWNLOAD_BYTES,
    TelegramAPIError,
    TelegramTransientError,
    TelegramWebhookConfigured,
)
from health_agent.telegram.messenger import TelegramMessenger
from health_agent.telegram.types import (
    AttachmentKind,
    AttachmentProvenance,
    HealthCommandService,
    HealthQuestion,
    HealthQuestionService,
    InboxReceipt,
    MedicalInbox,
    MessageContext,
    ProcessResult,
    TelegramCommand,
    TelegramGateway,
    TelegramState,
)

HELP_TEXT = (
    "Можно задать любой вопрос о здоровье или прислать PDF, фотографию либо "
    "голосовое сообщение. Команды: /status — состояние данных, /sync — обновить "
    "источники, /help — эта справка."
)
SAFE_FAILURE_TEXT = "Не удалось обработать сообщение. Попробуйте ещё раз позже."
FILE_TOO_LARGE_TEXT = "Файл больше лимита Telegram Bot API (20 МБ)."


@dataclass(frozen=True, slots=True)
class PollReport:
    received: int
    completed: int
    next_offset: int | None


class TelegramUpdateService:
    def __init__(
        self,
        gateway: TelegramGateway,
        state: TelegramState,
        messenger: TelegramMessenger,
        questions: HealthQuestionService,
        commands: HealthCommandService,
        inbox: MedicalInbox,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.gateway = gateway
        self.state = state
        self.messenger = messenger
        self.questions = questions
        self.commands = commands
        self.inbox = inbox
        self.clock = clock

    def process_update(self, update: dict[str, object]) -> ProcessResult:
        update_id = _required_int(update.get("update_id"), "update_id")
        message = update.get("message")
        if not isinstance(message, dict):
            return self._ignore(update_id, None, None, None, None, "unsupported_update")

        sender = message.get("from")
        chat = message.get("chat")
        user_id = _optional_int(sender.get("id")) if isinstance(sender, dict) else None
        chat_id = _optional_int(chat.get("id")) if isinstance(chat, dict) else None
        message_id = _optional_int(message.get("message_id"))
        identity = None if user_id is None else self.state.identity_for_user(user_id)
        profile_id = None if identity is None else identity.profile_id
        kind = _message_kind(message)
        prior = self.state.begin_update(
            update_id=update_id,
            telegram_user_id=user_id,
            chat_id=chat_id,
            message_id=message_id,
            profile_id=profile_id,
            kind=kind,
        )
        if prior != "claimed":
            return ProcessResult(update_id, prior, prior != "processing")

        sender_is_bot = bool(sender.get("is_bot")) if isinstance(sender, dict) else True
        chat_type = str(chat.get("type") or "") if isinstance(chat, dict) else ""
        if identity is None:
            return self._finish(update_id, "ignored_unknown_user")
        if (
            sender_is_bot
            or chat_type != "private"
            or chat_id is None
            or message_id is None
            or user_id is None
            or chat_id != identity.private_chat_id
        ):
            return self._finish(update_id, "ignored_ambiguous_chat")

        context = MessageContext(
            profile_id=identity.profile_id,
            telegram_user_id=user_id,
            chat_id=chat_id,
            message_id=message_id,
            update_id=update_id,
            sent_at=_telegram_datetime(message.get("date")),
            received_at=self.clock(),
        )
        try:
            text = message.get("text")
            if isinstance(text, str) and text.strip():
                reply = self._route_text(context, text.strip())
                self._reply(context, reply)
                return self._finish(update_id, "replied")

            attachment = _attachment_from_message(context, message)
            if attachment is not None:
                return self._route_attachment(attachment)
            return self._finish(update_id, "ignored_unsupported_message")
        except TelegramTransientError as error:
            self.state.complete_update(
                update_id, "retryable_error", error.safe_error_code
            )
            return ProcessResult(update_id, "retryable_error", False)
        except TelegramAPIError as error:
            self.state.complete_update(update_id, "failed", error.safe_error_code)
            return ProcessResult(update_id, "failed", True)
        # Adapter implementations are injected and may fail with domain-specific
        # exceptions. Convert them to a content-free terminal audit state.
        except Exception:  # noqa: BLE001
            try:
                self._reply(context, SAFE_FAILURE_TEXT)
            except TelegramTransientError as error:
                self.state.complete_update(
                    update_id, "retryable_error", error.safe_error_code
                )
                return ProcessResult(update_id, "retryable_error", False)
            except TelegramAPIError as error:
                self.state.complete_update(update_id, "failed", error.safe_error_code)
                return ProcessResult(update_id, "failed", True)
            self.state.complete_update(update_id, "needs_attention", "downstream_error")
            return ProcessResult(update_id, "needs_attention", True)

    def _route_text(self, context: MessageContext, text: str) -> str:
        command = _command_name(text)
        if command in {"help", "start"}:
            return HELP_TEXT
        if command == "status":
            return self.commands.status(TelegramCommand(context, "status"))
        if command == "sync":
            return self.commands.sync(TelegramCommand(context, "sync"))
        if command is not None:
            return HELP_TEXT
        return self.questions.answer(HealthQuestion(context, text))

    def _route_attachment(self, provenance: AttachmentProvenance) -> ProcessResult:
        update_id = provenance.context.update_id
        if (
            provenance.declared_size_bytes is not None
            and provenance.declared_size_bytes > MAX_DOWNLOAD_BYTES
        ):
            self._reply(provenance.context, FILE_TOO_LARGE_TEXT)
            return self._finish(update_id, "file_too_large", "file_too_large")
        remote = self.gateway.get_file(provenance.file_id)
        if remote.file_size is not None and remote.file_size > MAX_DOWNLOAD_BYTES:
            self._reply(provenance.context, FILE_TOO_LARGE_TEXT)
            return self._finish(update_id, "file_too_large", "file_too_large")
        stream = _AuditedChunks(self.gateway.download_chunks(remote.file_path))
        receipt = self.inbox.ingest(provenance, stream)
        _validate_receipt(
            receipt,
            stream,
            provenance.declared_size_bytes,
            remote.file_size,
        )
        self.state.record_attachment(update_id, receipt)
        self._reply(provenance.context, receipt.reply_text)
        return self._finish(update_id, receipt.status)

    def _reply(self, context: MessageContext, text: str) -> None:
        self.messenger.send_to_chat(
            context.profile_id,
            context.chat_id,
            text,
            delivery_key=f"telegram-update:{context.update_id}:reply",
        )

    def _ignore(
        self,
        update_id: int,
        user_id: int | None,
        chat_id: int | None,
        message_id: int | None,
        profile_id: UUID | None,
        kind: str,
    ) -> ProcessResult:
        prior = self.state.begin_update(
            update_id=update_id,
            telegram_user_id=user_id,
            chat_id=chat_id,
            message_id=message_id,
            profile_id=profile_id,
            kind=kind,
        )
        if prior != "claimed":
            return ProcessResult(update_id, prior, prior != "processing")
        return self._finish(update_id, kind)

    def _finish(
        self, update_id: int, status: str, error_code: str | None = None
    ) -> ProcessResult:
        self.state.complete_update(update_id, status, error_code)
        return ProcessResult(update_id, status, True)


class TelegramLongPoller:
    def __init__(
        self,
        gateway: TelegramGateway,
        state: TelegramState,
        updates: TelegramUpdateService,
        *,
        timeout_seconds: int = 30,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.gateway = gateway
        self.state = state
        self.updates = updates
        self.timeout_seconds = timeout_seconds
        self.sleeper = sleeper

    def poll_once(self) -> PollReport:
        if self.gateway.get_webhook_url():
            self.state.record_poll("webhook_configured")
            raise TelegramWebhookConfigured()
        try:
            updates = sorted(
                self.gateway.get_updates(
                    offset=self.state.next_offset(),
                    timeout_seconds=self.timeout_seconds,
                ),
                key=lambda value: _required_int(value.get("update_id"), "update_id"),
            )
        except TelegramAPIError as error:
            self.state.record_poll(error.safe_error_code)
            raise
        completed = 0
        blocked = False
        for update in updates:
            result = self.updates.process_update(update)
            if not result.terminal:
                self.state.record_poll("update_retryable_error")
                blocked = True
                break
            self.state.advance_offset(result.update_id + 1)
            completed += 1
        if not blocked:
            self.state.record_poll(None)
        return PollReport(len(updates), completed, self.state.next_offset())

    def run_forever(self) -> None:
        while True:
            try:
                self.poll_once()
            except TelegramTransientError:
                self.sleeper(5)


def _command_name(text: str) -> str | None:
    first = text.split(maxsplit=1)[0]
    if not first.startswith("/"):
        return None
    return first[1:].split("@", 1)[0].casefold()


def _message_kind(message: dict[str, object]) -> str:
    for key in ("document", "photo", "voice", "text"):
        if key in message:
            return key
    return "unsupported_message"


def _attachment_from_message(
    context: MessageContext, message: dict[str, object]
) -> AttachmentProvenance | None:
    document = message.get("document")
    if isinstance(document, dict):
        return _attachment_provenance(context, "document", document)
    photos = message.get("photo")
    if isinstance(photos, list):
        candidates = [value for value in photos if isinstance(value, dict)]
        if candidates:
            selected = max(
                candidates,
                key=lambda value: (
                    _optional_int(value.get("file_size")) or 0,
                    (_optional_int(value.get("width")) or 0)
                    * (_optional_int(value.get("height")) or 0),
                ),
            )
            return _attachment_provenance(
                context, "photo", selected, mime_type="image/jpeg"
            )
    voice = message.get("voice")
    if isinstance(voice, dict):
        return _attachment_provenance(
            context,
            "voice",
            voice,
            mime_type=str(voice.get("mime_type") or "audio/ogg"),
        )
    return None


def _attachment_provenance(
    context: MessageContext,
    kind: AttachmentKind,
    value: dict[str, object],
    *,
    mime_type: str | None = None,
) -> AttachmentProvenance:
    file_id = str(value.get("file_id") or "")
    unique_id = str(value.get("file_unique_id") or "")
    if not file_id or not unique_id:
        raise TelegramAPIError("invalid_attachment_metadata")
    return AttachmentProvenance(
        context=context,
        kind=kind,
        file_id=file_id,
        file_unique_id=unique_id,
        file_name=(
            str(value["file_name"]) if value.get("file_name") is not None else None
        ),
        mime_type=mime_type
        or str(value.get("mime_type") or "application/octet-stream"),
        declared_size_bytes=_optional_int(value.get("file_size")),
        duration_seconds=_optional_int(value.get("duration")),
        source_external_id=(
            f"telegram:{context.chat_id}:{context.message_id}:{unique_id}"
        ),
    )


class _AuditedChunks:
    """One-pass bounded stream whose digest is independent of the inbox receipt."""

    def __init__(self, chunks: Iterable[bytes]) -> None:
        self._chunks = iter(chunks)
        self._digest = hashlib.sha256()
        self.size_bytes = 0
        self.exhausted = False
        self._started = False

    def __iter__(self) -> Iterator[bytes]:
        if self._started:
            raise RuntimeError("Telegram attachment stream may only be consumed once")
        self._started = True
        for chunk in self._chunks:
            if not isinstance(chunk, bytes):
                raise TypeError("Telegram download chunks must be bytes")
            self.size_bytes += len(chunk)
            if self.size_bytes > MAX_DOWNLOAD_BYTES:
                raise TelegramAPIError("file_too_large")
            self._digest.update(chunk)
            yield chunk
        self.exhausted = True

    @property
    def sha256(self) -> str:
        if not self.exhausted:
            raise RuntimeError("medical inbox did not consume the complete attachment")
        return self._digest.hexdigest()


def _validate_receipt(
    receipt: InboxReceipt,
    stream: _AuditedChunks,
    declared_size: int | None,
    remote_size: int | None,
) -> None:
    if (
        len(receipt.sha256) != 64
        or any(character not in "0123456789abcdef" for character in receipt.sha256)
        or receipt.size_bytes < 0
        or receipt.size_bytes > MAX_DOWNLOAD_BYTES
    ):
        raise RuntimeError("medical inbox returned an invalid receipt")
    if receipt.sha256 != stream.sha256 or receipt.size_bytes != stream.size_bytes:
        raise RuntimeError("medical inbox receipt does not match streamed bytes")
    for expected in (declared_size, remote_size):
        if expected is not None and expected != receipt.size_bytes:
            raise RuntimeError("medical inbox size does not match Telegram metadata")


def _telegram_datetime(value: object) -> datetime | None:
    timestamp = _optional_int(value)
    return None if timestamp is None else datetime.fromtimestamp(timestamp, tz=UTC)


def _required_int(value: object, field: str) -> int:
    parsed = _optional_int(value)
    if parsed is None:
        raise TelegramAPIError(f"invalid_{field}")
    return parsed


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value
