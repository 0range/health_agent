"""Safe production composition for health questions and Telegram.

This module is intentionally the only place that joins database retrieval, the
OpenAI responder, and the hardened Telegram transport.  All dependencies can
be replaced by tests so importing or exercising a command never requires a
network request or a real credential.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import UUID

from sqlalchemy import Engine

from health_agent.config import Settings
from health_agent.db import build_engine, session_scope
from health_agent.questions.context import HealthContextBuilder
from health_agent.questions.models import EvidenceSource, HealthQuestionContext
from health_agent.questions.openai import OpenAIResponsesResponder
from health_agent.questions.service import (
    QUESTION_UNAVAILABLE_TEXT,
    HealthQuestionApplicationService,
    HealthQuestionResponder,
)
from health_agent.telegram.api import TelegramBotAPI
from health_agent.telegram.messenger import TelegramMessenger
from health_agent.telegram.service import TelegramLongPoller, TelegramUpdateService
from health_agent.telegram.stores import PrivateBotTokenStore, SqliteTelegramState
from health_agent.telegram.types import (
    AttachmentProvenance,
    HealthQuestion,
    InboxReceipt,
    MedicalInbox,
    TelegramCommand,
    TelegramGateway,
    TelegramState,
)

QUESTION_STATUS_UNAVAILABLE = "Health-question status is temporarily unavailable."
SYNC_INSTRUCTIONS = (
    "Synchronization is not started from Telegram. Run the existing connector "
    "CLI locally: health-agent gmail sync --profile-id <your-profile-id> and/or "
    "health-agent whoop sync --profile-id <your-profile-id>."
)
ATTACHMENT_NEEDS_ATTENTION_TEXT = (
    "This attachment needs attention and was not imported. Use an approved local "
    "medical-ingestion workflow."
)


class ContextBuilderFactory(Protocol):
    def __call__(self, engine: Engine) -> DatabaseHealthContextBuilder: ...


class ResponderFactory(Protocol):
    def __call__(self, settings: Settings) -> HealthQuestionResponder: ...


class QuestionApplicationFactory(Protocol):
    def __call__(self, settings: Settings) -> HealthQuestionApplicationService: ...


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
            return HealthContextBuilder(session).build(profile_id, question)


class TelegramHealthQuestionService:
    """Adapt only the authenticated Telegram context to the question boundary."""

    def __init__(self, application: HealthQuestionApplicationService) -> None:
        self._application = application

    def answer(self, question: HealthQuestion) -> str:
        # TelegramUpdateService has already fenced this context to one bound,
        # private identity.  Do not accept a profile ID from message text.
        return self._application.answer(question.context.profile_id, question.text).text


class ReadOnlyQuestionCommands:
    """Telegram commands that can inspect state but never synchronize it."""

    def __init__(self, status_reader: Callable[[UUID], QuestionStatus]) -> None:
        self._status_reader = status_reader

    def status(self, command: TelegramCommand) -> str:
        status = self._status_reader(command.context.profile_id)
        if not status.available:
            return QUESTION_STATUS_UNAVAILABLE
        parts = ["Health data status:"]
        parts.extend(
            f"{source.value}={status.source_counts.get(source, 0)}"
            for source in EvidenceSource
        )
        return " ".join(parts)

    def sync(self, command: TelegramCommand) -> str:
        # Keep the argument to make profile scoping explicit at this adapter
        # boundary; it is intentionally not used to trigger a connector action.
        _ = command.context.profile_id
        return SYNC_INSTRUCTIONS


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
        else OpenAIResponsesResponder(
            settings.load_openai_api_key(),
            model=settings.openai_model,
            max_output_tokens=settings.openai_max_output_tokens,
        )
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
        readiness = responder_readiness or _load_openai_readiness
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
    update_service_factory: Callable[..., TelegramUpdateService] = TelegramUpdateService,
    poller_factory: Callable[..., TelegramLongPoller] = TelegramLongPoller,
    medical_inbox: MedicalInbox | None = None,
    status_reader: Callable[[UUID], QuestionStatus] | None = None,
) -> TelegramQuestionRuntime:
    """Compose verified local Telegram state with profile-bound question handling.

    ``medical_inbox`` is deliberately injectable.  A deployment with a real,
    atomic inbox uses it unchanged; absent one, the fallback is truthful and
    records only a needs-attention outcome.
    """

    credential = token_store_factory(settings.effective_telegram_token_file).load_verified()
    state = state_factory(settings.telegram_state_file)
    gateway = gateway_factory(credential.token)
    application = question_application_factory(settings)
    question_service = TelegramHealthQuestionService(application)
    commands = ReadOnlyQuestionCommands(
        status_reader or (lambda profile_id: question_status(settings, profile_id))
    )
    messenger = messenger_factory(credential.bot_id, gateway, state)
    updates = update_service_factory(
        credential.bot_id,
        gateway,
        state,
        messenger,
        question_service,
        commands,
        medical_inbox or NeedsAttentionMedicalInbox(),
        staging_root=settings.telegram_staging_root,
    )
    return TelegramQuestionRuntime(
        poller_factory(credential.bot_id, gateway, state, updates)
    )


def _load_openai_readiness(settings: Settings) -> object:
    """Validate local responder configuration without constructing a client."""

    return settings.load_openai_api_key()


def safe_question_setup_error() -> str:
    """Stable CLI message for failures before the application error boundary."""

    return QUESTION_UNAVAILABLE_TEXT
