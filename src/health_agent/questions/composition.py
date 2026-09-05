"""Safe production composition for health questions and Telegram.

This module is intentionally the only place that joins database retrieval, the
OpenAI responder, and the hardened Telegram transport.  All dependencies can
be replaced by tests so importing or exercising a command never requires a
network request or a real credential.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import UUID

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from health_agent.config import Settings
from health_agent.db import build_engine, session_scope
from health_agent.importer import ImportReport, import_document
from health_agent.models import Profile
from health_agent.questions.context import HealthContextBuilder
from health_agent.questions.models import EvidenceSource, HealthQuestionContext
from health_agent.questions.openai import OpenAIResponsesResponder
from health_agent.questions.replies import PrivateReplyStore, delivery_request_id
from health_agent.questions.service import (
    QUESTION_UNAVAILABLE_TEXT,
    HealthQuestionApplicationService,
    HealthQuestionResponder,
    QuestionAnswerResult,
)
from health_agent.reminders.telegram import DatabaseReminderCommands
from health_agent.telegram.actions import (
    CompositeTelegramTextActions,
    PreparedTelegramTextActions,
)
from health_agent.telegram.api import MAX_DOWNLOAD_BYTES, TelegramBotAPI
from health_agent.telegram.messenger import TelegramMessenger
from health_agent.telegram.review import TelegramReviewActions
from health_agent.telegram.service import TelegramLongPoller, TelegramUpdateService
from health_agent.telegram.stores import (
    PrivateBotTokenStore,
    SqliteTelegramState,
    private_directory,
)
from health_agent.telegram.types import (
    AttachmentProvenance,
    HealthQuestion,
    InboxReceipt,
    MedicalInbox,
    TelegramCommand,
    TelegramGateway,
    TelegramState,
)
from health_agent.vault import FileVault

QUESTION_STATUS_UNAVAILABLE = "Сейчас не удалось получить состояние данных о здоровье."
SYNC_INSTRUCTIONS = "Синхронизация не запускается из Telegram."
ATTACHMENT_NEEDS_ATTENTION_TEXT = (
    "Этот файл требует проверки и не импортирован. Используйте разрешённый локальный "
    "процесс импорта медицинских данных."
)
_SOURCE_LABELS = {
    EvidenceSource.LAB: "анализы",
    EvidenceSource.SLEEP: "сон",
    EvidenceSource.RECOVERY: "восстановление",
    EvidenceSource.CYCLE: "циклы",
    EvidenceSource.WORKOUT: "тренировки",
    EvidenceSource.WEIGHT: "вес",
}


class ContextBuilderFactory(Protocol):
    def __call__(self, engine: Engine) -> DatabaseHealthContextBuilder: ...


class ResponderFactory(Protocol):
    def __call__(self, settings: Settings) -> HealthQuestionResponder: ...


class QuestionApplicationFactory(Protocol):
    def __call__(self, settings: Settings) -> QuestionApplication: ...


class QuestionApplication(Protocol):
    """The single method the Telegram adapter needs from the application layer."""

    def answer(
        self, profile_id: UUID, question: str, *, request_id: str | None = None
    ) -> QuestionAnswerResult: ...


@dataclass(frozen=True, slots=True)
class QuestionStatus:
    """Safe, count-only profile status for CLI and Telegram views."""

    available: bool
    source_counts: dict[EvidenceSource, int]
    safe_error_code: str | None = None


@dataclass(frozen=True, slots=True)
class TelegramQuestionRuntime:
    """Fully composed poller; credential material is deliberately not retained."""

    poller: TelegramLongPoller


class DatabaseHealthContextBuilder:
    """Open a short-lived session for each read-only profile-scoped lookup."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def build(self, profile_id: UUID, question: str) -> HealthQuestionContext:
        with session_scope(self._engine) as session:
            if session.get(Profile, profile_id) is None:
                raise ValueError("profile is unavailable")
            return HealthContextBuilder(session).build(profile_id, question)


class TelegramHealthQuestionService:
    """Adapt only the authenticated Telegram context to the question boundary."""

    def __init__(
        self,
        application: QuestionApplication,
        reply_store: PrivateReplyStore | None = None,
    ) -> None:
        self._application = application
        self._reply_store = reply_store

    def answer(self, question: HealthQuestion) -> str:
        # TelegramUpdateService has already fenced this context to one bound,
        # private identity.  Do not accept a profile ID from message text.
        if self._reply_store is not None:
            self._reply_store.sweep()
            prepared = self._reply_store.get(question.context)
            if prepared is not None:
                return prepared
        answer = self._application.answer(
            question.context.profile_id,
            question.text,
            request_id=delivery_request_id(
                question.context.bot_id, question.context.update_id
            ),
        ).text
        if self._reply_store is not None:
            return self._reply_store.put(question.context, answer)
        return answer

    def complete_update(self, bot_id: int, update_id: int) -> None:
        if self._reply_store is not None:
            self._reply_store.complete(bot_id, update_id)


class ReadOnlyQuestionCommands:
    """Telegram commands that can inspect state but never synchronize it."""

    def __init__(self, status_reader: Callable[[UUID], QuestionStatus]) -> None:
        self._status_reader = status_reader

    def status(self, command: TelegramCommand) -> str:
        status = self._status_reader(command.context.profile_id)
        if not status.available:
            return QUESTION_STATUS_UNAVAILABLE
        parts = ["Состояние данных о здоровье:"]
        parts.extend(
            f"{_SOURCE_LABELS[source]}={status.source_counts.get(source, 0)}"
            for source in EvidenceSource
        )
        return " ".join(parts)

    def sync(self, command: TelegramCommand) -> str:
        return sync_instructions(command.context.profile_id)


def sync_instructions(profile_id: UUID) -> str:
    """Return existing, profile-bound connector invocations without mutating state."""

    return (
        f"{SYNC_INSTRUCTIONS} Запустите локально: health-agent gmail sync {profile_id} "
        f"и/или health-agent whoop sync --profile-id {profile_id}."
    )


class NeedsAttentionMedicalInbox:
    """Safe attachment fallback when no real atomic Telegram inbox is installed.

    It consumes the already signature-validated staged stream so the update
    service can produce a durable needs-attention audit outcome.  It neither
    writes a document nor implies that the attachment was ingested.
    """

    def ingest(
        self, provenance: AttachmentProvenance, chunks: Iterable[bytes]
    ) -> InboxReceipt:
        _ = provenance.context.profile_id
        digest = hashlib.sha256()
        size = 0
        for chunk in chunks:
            if not isinstance(chunk, bytes):
                raise TypeError("attachment stream is invalid")
            digest.update(chunk)
            size += len(chunk)
        return InboxReceipt(
            sha256=digest.hexdigest(),
            size_bytes=size,
            status="needs_attention",
            reply_text=ATTACHMENT_NEEDS_ATTENTION_TEXT,
        )


class TelegramMedicalInbox:
    """Import validated Telegram PDFs/images through the normal vault pipeline.

    The temporary file is private and exists only while the full staged stream is
    hashed and handed to ``import_document``. Telegram provenance is persisted by
    the existing source-record transaction, so a replay is a duplicate rather than
    a second import.
    """

    def __init__(
        self,
        engine: Engine,
        vault: FileVault,
        temporary_root: Path,
        *,
        importer: Callable[..., ImportReport] = import_document,
        session_scope_factory: Callable[
            [Engine], AbstractContextManager[Session]
        ] = session_scope,
    ) -> None:
        self._engine = engine
        self._vault = vault
        self._temporary_root = Path(temporary_root)
        self._importer = importer
        self._session_scope_factory = session_scope_factory

    def ingest(
        self, provenance: AttachmentProvenance, chunks: Iterable[bytes]
    ) -> InboxReceipt:
        temporary = self._write_private_copy(chunks)
        try:
            sha256, size_bytes = _sha256_and_size(temporary)
            if provenance.validated_media_type not in {
                "application/pdf",
                "image/jpeg",
                "image/png",
            }:
                return InboxReceipt(
                    sha256,
                    size_bytes,
                    "needs_attention",
                    ATTACHMENT_NEEDS_ATTENTION_TEXT,
                )
            with self._session_scope_factory(self._engine) as session:
                report = self._importer(
                    session,
                    self._vault,
                    temporary,
                    None,
                    profile_id=provenance.context.profile_id,
                    source_provider="telegram",
                    source_external_id=provenance.source_external_id,
                    media_type=provenance.validated_media_type,
                )
            return InboxReceipt(
                sha256,
                size_bytes,
                "received",
                (
                    "Медицинский PDF-файл получен и сохранён."
                    if provenance.validated_media_type == "application/pdf"
                    else "Медицинское изображение получено и сохранено."
                )
                + " Перед использованием данные могут потребовать "
                "проверки. Используйте /review, чтобы проверить один извлечённый "
                "показатель. Распознавание текста (OCR) может быть недоступно.",
                external_reference=str(report.document_id),
            )
        finally:
            temporary.unlink(missing_ok=True)

    def _write_private_copy(self, chunks: Iterable[bytes]) -> Path:
        private_directory(self._temporary_root)
        descriptor, name = tempfile.mkstemp(
            dir=self._temporary_root, prefix="telegram-", suffix=".upload"
        )
        temporary = Path(name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as destination:
                size = 0
                for chunk in chunks:
                    if not isinstance(chunk, bytes):
                        raise TypeError("attachment stream is invalid")
                    size += len(chunk)
                    if size > MAX_DOWNLOAD_BYTES:
                        raise ValueError("attachment exceeds the import size limit")
                    destination.write(chunk)
                destination.flush()
                os.fsync(destination.fileno())
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            temporary.unlink(missing_ok=True)
            raise
        return temporary


def _sha256_and_size(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size_bytes = 0
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
            size_bytes += len(chunk)
    return digest.hexdigest(), size_bytes


def build_question_application(
    settings: Settings,
    *,
    engine_factory: Callable[[Settings], Engine] = build_engine,
    context_builder_factory: ContextBuilderFactory = DatabaseHealthContextBuilder,
    responder_factory: ResponderFactory | None = None,
) -> HealthQuestionApplicationService:
    """Build the shared CLI/Telegram service without calling any remote API."""

    responder = (
        responder_factory(settings)
        if responder_factory is not None
        else _build_responder(settings)
    )
    return HealthQuestionApplicationService(
        context_builder_factory(engine_factory(settings)), responder
    )


def question_status(
    settings: Settings,
    profile_id: UUID,
    *,
    engine_factory: Callable[[Settings], Engine] = build_engine,
    context_builder_factory: ContextBuilderFactory = DatabaseHealthContextBuilder,
    responder_readiness: Callable[[Settings], object] | None = None,
) -> QuestionStatus:
    """Read count-only readiness without exposing evidence, requests, or secrets."""

    try:
        readiness = responder_readiness or _build_responder
        readiness(settings)
    except Exception:  # noqa: BLE001 -- settings details are private
        return QuestionStatus(False, {}, "responder_unavailable")
    try:
        context = context_builder_factory(engine_factory(settings)).build(
            profile_id, "status"
        )
    except Exception:  # noqa: BLE001 -- database details are private
        return QuestionStatus(False, {}, "context_unavailable")
    return QuestionStatus(True, dict(context.source_counts))


def build_telegram_question_runtime(
    settings: Settings,
    *,
    question_application_factory: QuestionApplicationFactory = build_question_application,
    token_store_factory: Callable[[Path], PrivateBotTokenStore] = PrivateBotTokenStore,
    state_factory: Callable[[Path], SqliteTelegramState] = SqliteTelegramState,
    gateway_factory: Callable[[str], TelegramGateway] = TelegramBotAPI,
    messenger_factory: Callable[
        [int, TelegramGateway, TelegramState], TelegramMessenger
    ] = TelegramMessenger,
    update_service_factory: Callable[
        ..., TelegramUpdateService
    ] = TelegramUpdateService,
    poller_factory: Callable[..., TelegramLongPoller] = TelegramLongPoller,
    medical_inbox: MedicalInbox | None = None,
    status_reader: Callable[[UUID], QuestionStatus] | None = None,
    engine_factory: Callable[[Settings], Engine] = build_engine,
) -> TelegramQuestionRuntime:
    """Compose verified local Telegram state with profile-bound question handling.

    ``medical_inbox`` remains injectable for tests and deployments. The
    production default imports validated PDFs/images through the established vault and
    database provenance pipeline.
    """

    credential = token_store_factory(
        settings.effective_telegram_token_file
    ).load_verified()
    state = state_factory(settings.telegram_state_file)
    state.register_bot(credential.bot_id, credential.username)
    gateway = gateway_factory(credential.token)
    application = question_application_factory(settings)
    engine = engine_factory(settings)
    reply_store = PrivateReplyStore(settings.telegram_root / "prepared-replies")
    question_service = TelegramHealthQuestionService(application, reply_store)
    engine = engine_factory(settings)
    text_actions = PreparedTelegramTextActions(
        CompositeTelegramTextActions(
            (TelegramReviewActions(engine), DatabaseReminderCommands(engine))
        ),
        reply_store,
    )
    commands = ReadOnlyQuestionCommands(
        status_reader or (lambda profile_id: question_status(settings, profile_id))
    )
    messenger = messenger_factory(credential.bot_id, gateway, state)
    inbox = medical_inbox or TelegramMedicalInbox(
        engine,
        FileVault(settings.vault_root),
        settings.temporary_root,
    )
    updates = update_service_factory(
        credential.bot_id,
        gateway,
        state,
        messenger,
        question_service,
        commands,
        inbox,
        staging_root=settings.telegram_staging_root,
        text_actions=text_actions,
    )
    return TelegramQuestionRuntime(
        poller_factory(credential.bot_id, gateway, state, updates)
    )


def _build_responder(settings: Settings) -> OpenAIResponsesResponder:
    """Validate the exact local responder configuration used by ``ask``."""

    return OpenAIResponsesResponder(
        settings.load_openai_api_key(),
        model=settings.openai_model,
        max_output_tokens=settings.openai_max_output_tokens,
        reasoning_effort=settings.openai_reasoning_effort,
    )


def safe_question_setup_error() -> str:
    """Stable CLI message for failures before the application error boundary."""

    return QUESTION_UNAVAILABLE_TEXT
