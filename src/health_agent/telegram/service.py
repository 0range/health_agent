"""Bot-scoped private routing with fenced claims and durable deferral."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from health_agent.telegram.api import (
    MAX_DOWNLOAD_BYTES,
    MAX_SAFE_INTEGER,
    TelegramAPIError,
    TelegramDeferred,
    TelegramDeliveryUnknown,
    TelegramTransientError,
    TelegramWebhookConfigured,
)
from health_agent.telegram.attachments import (
    AttachmentValidationError,
    StagedAttachment,
    stage_attachment,
)
from health_agent.telegram.messenger import TelegramMessenger
from health_agent.telegram.types import (
    AttachmentKind,
    AttachmentProvenance,
    ClaimResult,
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
    TelegramTextActionService,
    UpdateClaim,
)

HELP_TEXT = (
    "Можно задать вопрос о здоровье или прислать PDF, JPEG/PNG. "
    "Распознанные значения нужно явно проверить: /review показывает один пункт, "
    "/confirm UUID подтверждает, /correct UUID ЗНАЧЕНИЕ ЕДИНИЦА исправляет, "
    "/reject UUID отклоняет. Вес в v0.1 берётся только из WHOOP. "
    "Голосовые сообщения пока не распознаются. /status — состояние данных, "
    "/sync — инструкции локальной синхронизации, /help — эта справка."
)
SAFE_FAILURE_TEXT = "Не удалось обработать сообщение. Попробуйте ещё раз позже."
FILE_TOO_LARGE_TEXT = "Файл больше лимита Telegram Bot API (20 МБ)."
INVALID_FILE_TEXT = "Файл повреждён или его формат не поддерживается."
MAX_UPDATE_ATTEMPTS = 4


@dataclass(frozen=True, slots=True)
class PollReport:
    received: int
    completed: int
    malformed: int
    next_offset: int | None
    blocked_until: datetime | None = None


class _ClaimHeartbeat:
    def __init__(
        self,
        state: TelegramState,
        claim: UpdateClaim,
        lease_seconds: float,
    ) -> None:
        self._state = state
        self.claim = claim
        self._lease_seconds = lease_seconds
        self._interval = max(lease_seconds / 3, 0.01)
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> bool:
        self._stop.set()
        self._thread.join(timeout=max(self._interval * 2, 0.1))
        if self._thread.is_alive():
            self._lost.set()
        return not self._lost.is_set()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                renewed = self._state.renew_claim(self.claim, self._lease_seconds)
            except Exception:  # noqa: BLE001 -- a lost heartbeat must fence the worker
                self._lost.set()
                return
            if renewed is None:
                self._lost.set()
                return
            self.claim = renewed


class TelegramUpdateService:
    def __init__(
        self,
        bot_id: int,
        gateway: TelegramGateway,
        state: TelegramState,
        messenger: TelegramMessenger,
        questions: HealthQuestionService,
        commands: HealthCommandService,
        inbox: MedicalInbox,
        *,
        staging_root: Path,
        text_actions: TelegramTextActionService | None = None,
        owner_id: str | None = None,
        lease_seconds: float = 60,
        clock=lambda: datetime.now(UTC),
    ) -> None:
        if bot_id <= 0 or lease_seconds <= 0:
            raise ValueError("bot ID and claim lease must be positive")
        self.bot_id = bot_id
        self.gateway = gateway
        self.state = state
        self.messenger = messenger
        self.questions = questions
        self.commands = commands
        self.inbox = inbox
        self.staging_root = Path(staging_root)
        self.text_actions = text_actions
        self.owner_id = owner_id or str(uuid4())
        self.lease_seconds = lease_seconds
        self.clock = clock

    def process_update(self, update: dict[str, object]) -> ProcessResult:
        update_id = _optional_int(update.get("update_id"))
        if update_id is None or update_id < 0:
            return ProcessResult(-1, "malformed_update", True)
        message = update.get("message")
        if not isinstance(message, dict):
            return self._claim_and_ignore(
                update_id, None, None, None, None, "unsupported_update"
            )

        sender = message.get("from")
        chat = message.get("chat")
        user_id = _optional_int(sender.get("id")) if isinstance(sender, dict) else None
        chat_id = _optional_int(chat.get("id")) if isinstance(chat, dict) else None
        message_id = _optional_int(message.get("message_id"))
        identity = (
            None
            if user_id is None
            else self.state.identity_for_user(self.bot_id, user_id)
        )
        profile_id = None if identity is None else identity.profile_id
        claimed = self.state.claim_update(
            bot_id=self.bot_id,
            update_id=update_id,
            owner_id=self.owner_id,
            lease_seconds=self.lease_seconds,
            telegram_user_id=user_id,
            chat_id=chat_id,
            message_id=message_id,
            profile_id=profile_id,
            kind=_message_kind(message),
        )
        if claimed.claim is None:
            return _unclaimed_result(update_id, claimed)
        claim = claimed.claim
        heartbeat = _ClaimHeartbeat(self.state, claim, self.lease_seconds)
        heartbeat.start()

        sender_is_human = (
            isinstance(sender, dict) and sender.get("is_bot") is False
        )
        chat_type = str(chat.get("type") or "") if isinstance(chat, dict) else ""
        if identity is None:
            return self._finish(claim, heartbeat, "ignored_unknown_user")
        if (
            not sender_is_human
            or chat_type != "private"
            or chat_id is None
            or message_id is None
            or user_id is None
            or chat_id != identity.private_chat_id
        ):
            return self._finish(claim, heartbeat, "ignored_ambiguous_chat")

        context = MessageContext(
            bot_id=self.bot_id,
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
                return self._finish(claim, heartbeat, "replied")
            attachment = _attachment_from_message(context, message)
            if attachment is not None:
                return self._route_attachment(claim, heartbeat, attachment)
            return self._finish(claim, heartbeat, "ignored_unsupported_message")
        except TelegramDeferred as error:
            return self._defer(claim, heartbeat, error.safe_error_code, error.retry_at)
        except TelegramDeliveryUnknown as error:
            return self._finish(
                claim, heartbeat, "delivery_unknown", error.safe_error_code
            )
        except TelegramTransientError as error:
            return self._defer(claim, heartbeat, error.safe_error_code)
        except AttachmentValidationError as error:
            if error.safe_error_code == "file_too_large":
                try:
                    self._reply(context, FILE_TOO_LARGE_TEXT)
                except TelegramDeferred as reply_error:
                    return self._defer(
                        claim,
                        heartbeat,
                        reply_error.safe_error_code,
                        reply_error.retry_at,
                    )
                except TelegramAPIError as reply_error:
                    return self._finish(
                        claim,
                        heartbeat,
                        "delivery_unknown"
                        if isinstance(reply_error, TelegramDeliveryUnknown)
                        else "failed",
                        reply_error.safe_error_code,
                    )
                return self._finish(
                    claim, heartbeat, "file_too_large", "file_too_large"
                )
            try:
                self._reply(context, INVALID_FILE_TEXT)
            except TelegramDeferred as reply_error:
                return self._defer(
                    claim,
                    heartbeat,
                    reply_error.safe_error_code,
                    reply_error.retry_at,
                )
            except TelegramDeliveryUnknown as reply_error:
                return self._finish(
                    claim, heartbeat, "delivery_unknown", reply_error.safe_error_code
                )
            except TelegramAPIError as reply_error:
                return self._finish(
                    claim, heartbeat, "failed", reply_error.safe_error_code
                )
            return self._finish(
                claim, heartbeat, "needs_attention", error.safe_error_code
            )
        except TelegramAPIError as error:
            if error.safe_error_code == "file_too_large":
                try:
                    self._reply(context, FILE_TOO_LARGE_TEXT)
                except TelegramDeferred as reply_error:
                    return self._defer(
                        claim,
                        heartbeat,
                        reply_error.safe_error_code,
                        reply_error.retry_at,
                    )
                except TelegramAPIError as reply_error:
                    return self._finish(
                        claim,
                        heartbeat,
                        "delivery_unknown"
                        if isinstance(reply_error, TelegramDeliveryUnknown)
                        else "failed",
                        reply_error.safe_error_code,
                    )
                return self._finish(
                    claim, heartbeat, "file_too_large", "file_too_large"
                )
            return self._finish(claim, heartbeat, "failed", error.safe_error_code)
        # Injected adapters can raise domain-specific exceptions. Convert them to
        # a content-free terminal audit state without exposing exception text.
        except Exception:  # noqa: BLE001
            try:
                self._reply(context, SAFE_FAILURE_TEXT)
            except TelegramDeferred as error:
                return self._defer(
                    claim, heartbeat, error.safe_error_code, error.retry_at
                )
            except TelegramDeliveryUnknown as error:
                return self._finish(
                    claim, heartbeat, "delivery_unknown", error.safe_error_code
                )
            except TelegramAPIError as error:
                return self._finish(claim, heartbeat, "failed", error.safe_error_code)
            return self._finish(claim, heartbeat, "needs_attention", "downstream_error")

    def _route_text(self, context: MessageContext, text: str) -> str:
        command = _command_name(text)
        if command in {"help", "start"}:
            return HELP_TEXT
        if command == "status":
            return self.commands.status(TelegramCommand(context, "status"))
        if command == "sync":
            return self.commands.sync(TelegramCommand(context, "sync"))
        if self.text_actions is not None:
            reply = self.text_actions.handle(context, text)
            if reply is not None:
                return reply
        if command is not None:
            return HELP_TEXT
        return self.questions.answer(HealthQuestion(context, text))

    def _route_attachment(
        self,
        claim: UpdateClaim,
        heartbeat: _ClaimHeartbeat,
        provenance: AttachmentProvenance,
    ) -> ProcessResult:
        if (
            provenance.declared_size_bytes is not None
            and provenance.declared_size_bytes > MAX_DOWNLOAD_BYTES
        ):
            self._reply(provenance.context, FILE_TOO_LARGE_TEXT)
            return self._finish(claim, heartbeat, "file_too_large", "file_too_large")
        remote = self.gateway.get_file(provenance.file_id)
        if remote.file_size is not None and remote.file_size > MAX_DOWNLOAD_BYTES:
            self._reply(provenance.context, FILE_TOO_LARGE_TEXT)
            return self._finish(claim, heartbeat, "file_too_large", "file_too_large")
        with stage_attachment(
            self.staging_root,
            self.gateway.download_chunks(remote.file_path),
            kind=provenance.kind,
            declared_mime_type=provenance.mime_type,
            declared_size=provenance.declared_size_bytes,
            remote_size=remote.file_size,
        ) as staged:
            validated = replace(provenance, validated_media_type=staged.media_type)
            try:
                receipt = self.inbox.ingest(validated, staged.chunks())
                _validate_receipt(receipt, staged)
            except Exception:
                failure = InboxReceipt(
                    staged.sha256,
                    staged.size_bytes,
                    "inbox_needs_attention",
                    "",
                )
                if not self.state.record_attachment(claim, failure):
                    return self._claim_lost(claim, heartbeat)
                raise
            if not self.state.record_attachment(claim, receipt):
                return self._claim_lost(claim, heartbeat)
            self._reply(validated.context, receipt.reply_text)
            return self._finish(claim, heartbeat, receipt.status)

    def _reply(self, context: MessageContext, text: str) -> None:
        self.messenger.send_to_chat(
            context.profile_id,
            context.chat_id,
            text,
            delivery_key=f"telegram-update:{context.update_id}:reply",
        )

    def _claim_and_ignore(
        self,
        update_id: int,
        user_id: int | None,
        chat_id: int | None,
        message_id: int | None,
        profile_id: UUID | None,
        kind: str,
    ) -> ProcessResult:
        result = self.state.claim_update(
            bot_id=self.bot_id,
            update_id=update_id,
            owner_id=self.owner_id,
            lease_seconds=self.lease_seconds,
            telegram_user_id=user_id,
            chat_id=chat_id,
            message_id=message_id,
            profile_id=profile_id,
            kind=kind,
        )
        if result.claim is None:
            return _unclaimed_result(update_id, result)
        heartbeat = _ClaimHeartbeat(self.state, result.claim, self.lease_seconds)
        heartbeat.start()
        return self._finish(result.claim, heartbeat, kind)

    def _finish(
        self,
        claim: UpdateClaim,
        heartbeat: _ClaimHeartbeat,
        status: str,
        error_code: str | None = None,
    ) -> ProcessResult:
        if not heartbeat.stop() or not self.state.complete_update(
            claim, status, error_code
        ):
            return ProcessResult(claim.update_id, "claim_lost", False)
        self._cleanup_reply(claim)
        return ProcessResult(claim.update_id, status, True)

    def _cleanup_reply(self, claim: UpdateClaim) -> None:
        # Optional adapter hook; only a committed terminal audit permits deletion.
        cleanup = getattr(self.questions, "complete_update", None)
        if callable(cleanup):
            try:
                cleanup(claim.bot_id, claim.update_id)
            except Exception:  # noqa: BLE001, S110 -- TTL sweep handles orphaned replies
                pass

    def _claim_lost(
        self, claim: UpdateClaim, heartbeat: _ClaimHeartbeat
    ) -> ProcessResult:
        heartbeat.stop()
        return ProcessResult(claim.update_id, "claim_lost", False)

    def _defer(
        self,
        claim: UpdateClaim,
        heartbeat: _ClaimHeartbeat,
        error_code: str,
        retry_at: datetime | None = None,
    ) -> ProcessResult:
        if not heartbeat.stop():
            return ProcessResult(claim.update_id, "claim_lost", False)
        if claim.attempt_count >= MAX_UPDATE_ATTEMPTS:
            if not self.state.complete_update(
                claim, "needs_attention", "retry_budget_exhausted"
            ):
                return ProcessResult(claim.update_id, "claim_lost", False)
            self._cleanup_reply(claim)
            return ProcessResult(claim.update_id, "needs_attention", True)
        scheduled = retry_at or (
            self.clock().astimezone(UTC)
            + timedelta(seconds=5 * (2 ** (claim.attempt_count - 1)))
        )
        if not self.state.defer_update(claim, error_code, scheduled):
            return ProcessResult(claim.update_id, "claim_lost", False)
        return ProcessResult(claim.update_id, "retryable_error", False, scheduled)


class TelegramLongPoller:
    def __init__(
        self,
        bot_id: int,
        gateway: TelegramGateway,
        state: TelegramState,
        updates: TelegramUpdateService,
        *,
        timeout_seconds: int = 30,
        clock=lambda: datetime.now(UTC),
        sleeper=time.sleep,
    ) -> None:
        self.bot_id = bot_id
        self.gateway = gateway
        self.state = state
        self.updates = updates
        self.timeout_seconds = timeout_seconds
        self.clock = clock
        self.sleeper = sleeper
        self._identity_verified = False

    def validate_startup(self) -> None:
        """Verify the local credential namespace before reporting a running poller."""

        try:
            if _gateway_bot_id(self.gateway.get_me()) != self.bot_id:
                raise TelegramAPIError("bot_identity_mismatch")
            if self.gateway.get_webhook_url():
                raise TelegramWebhookConfigured()
            self._identity_verified = True
        except TelegramAPIError as error:
            self.state.record_poll(self.bot_id, error.safe_error_code)
            raise

    def poll_once(self) -> PollReport:
        try:
            if not self._identity_verified:
                if _gateway_bot_id(self.gateway.get_me()) != self.bot_id:
                    raise TelegramAPIError("bot_identity_mismatch")
                self._identity_verified = True
            if self.gateway.get_webhook_url():
                raise TelegramWebhookConfigured()
            raw_updates = self.gateway.get_updates(
                offset=self.state.next_offset(self.bot_id),
                timeout_seconds=self.timeout_seconds,
            )
        except TelegramAPIError as error:
            self.state.record_poll(self.bot_id, error.safe_error_code)
            raise
        valid: list[tuple[int, dict[str, object]]] = []
        malformed = 0
        for update in raw_updates:
            update_id = _optional_int(update.get("update_id"))
            if update_id is None or update_id < 0:
                malformed += 1
                continue
            valid.append((update_id, update))
        valid.sort(key=lambda item: item[0])
        completed = 0
        blocked_until: datetime | None = None
        for update_id, update in valid:
            result = self.updates.process_update(update)
            if not result.terminal:
                blocked_until = result.next_retry_at or (
                    self.clock().astimezone(UTC) + timedelta(seconds=5)
                )
                break
            self.state.advance_offset(self.bot_id, update_id + 1)
            completed += 1
        error_code = None
        if malformed:
            error_code = "malformed_update"
            if blocked_until is None and not valid:
                blocked_until = self.clock().astimezone(UTC) + timedelta(seconds=5)
        elif blocked_until is not None:
            error_code = "update_deferred"
        self.state.record_poll(self.bot_id, error_code)
        return PollReport(
            len(raw_updates),
            completed,
            malformed,
            self.state.next_offset(self.bot_id),
            blocked_until,
        )

    def run_forever(self) -> None:
        while True:
            try:
                report = self.poll_once()
                if report.blocked_until is not None:
                    self._sleep_until(report.blocked_until)
            except TelegramDeferred as error:
                self._sleep_until(error.retry_at)
            except TelegramTransientError:
                self.sleeper(5)
            except TelegramAPIError:
                self.sleeper(30)

    def _sleep_until(self, value: datetime) -> None:
        delay = (value.astimezone(UTC) - self.clock().astimezone(UTC)).total_seconds()
        self.sleeper(max(delay, 0.1))


def _unclaimed_result(update_id: int, result: ClaimResult) -> ProcessResult:
    terminal = result.status not in {"processing", "retryable_error"}
    return ProcessResult(update_id, result.status, terminal, result.next_retry_at)


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
        mime = voice.get("mime_type")
        return _attachment_provenance(
            context,
            "voice",
            voice,
            mime_type=mime if isinstance(mime, str) else "audio/ogg",
        )
    return None


def _attachment_provenance(
    context: MessageContext,
    kind: AttachmentKind,
    value: dict[str, object],
    *,
    mime_type: str | None = None,
) -> AttachmentProvenance:
    file_id = value.get("file_id")
    unique_id = value.get("file_unique_id")
    supplied_mime = value.get("mime_type")
    filename = value.get("file_name")
    if (
        not isinstance(file_id, str)
        or not file_id
        or not isinstance(unique_id, str)
        or not unique_id
        or (supplied_mime is not None and not isinstance(supplied_mime, str))
        or (filename is not None and not isinstance(filename, str))
    ):
        raise TelegramAPIError("invalid_attachment_metadata")
    return AttachmentProvenance(
        context=context,
        kind=kind,
        file_id=file_id,
        file_unique_id=unique_id,
        file_name=filename,
        mime_type=mime_type or supplied_mime or "application/octet-stream",
        validated_media_type=None,
        declared_size_bytes=_optional_int(value.get("file_size")),
        duration_seconds=_optional_int(value.get("duration")),
        source_external_id=(
            f"telegram:{context.bot_id}:{context.chat_id}:{context.message_id}:{unique_id}"
        ),
    )


def _validate_receipt(receipt: InboxReceipt, staged: StagedAttachment) -> None:
    if (
        receipt.sha256 != staged.sha256
        or receipt.size_bytes != staged.size_bytes
        or not receipt.status
        or not receipt.reply_text
    ):
        raise RuntimeError("medical inbox returned an invalid receipt")


def _telegram_datetime(value: object) -> datetime | None:
    timestamp = _optional_int(value)
    if timestamp is None:
        return None
    try:
        return datetime.fromtimestamp(timestamp, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _gateway_bot_id(value: dict[str, object]) -> int:
    bot_id = _optional_int(value.get("id"))
    if bot_id is None or bot_id <= 0 or value.get("is_bot") is not True:
        raise TelegramAPIError("invalid_get_me_response")
    return bot_id


def _optional_int(value: object) -> int | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < -MAX_SAFE_INTEGER
        or value > MAX_SAFE_INTEGER
    ):
        return None
    return value
