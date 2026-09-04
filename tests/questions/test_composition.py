from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

from health_agent.config import Settings
from health_agent.questions.composition import (
    ATTACHMENT_NEEDS_ATTENTION_TEXT,
    SYNC_INSTRUCTIONS,
    NeedsAttentionMedicalInbox,
    QuestionStatus,
    ReadOnlyQuestionCommands,
    TelegramHealthQuestionService,
    build_telegram_question_runtime,
)
from health_agent.questions.models import EvidenceSource
from health_agent.questions.service import QuestionAnswerResult
from health_agent.telegram.types import (
    AttachmentProvenance,
    HealthQuestion,
    MessageContext,
    TelegramCommand,
    VerifiedBotCredential,
)

PROFILE_ID = UUID("00000000-0000-0000-0000-000000000001")
OTHER_PROFILE_ID = UUID("00000000-0000-0000-0000-000000000002")


class FakeApplication:
    def __init__(self) -> None:
        self.calls: list[tuple[UUID, str]] = []

    def answer(self, profile_id: UUID, question: str) -> QuestionAnswerResult:
        self.calls.append((profile_id, question))
        return QuestionAnswerResult("Safe answer [LAB1]", None)


def test_telegram_question_adapter_uses_only_bound_context_profile() -> None:
    application = FakeApplication()
    context = _context(PROFILE_ID)

    answer = TelegramHealthQuestionService(application).answer(
        HealthQuestion(context, "private health question")
    )

    assert answer == "Safe answer [LAB1]"
    assert application.calls == [(PROFILE_ID, "private health question")]
    assert all(profile_id != OTHER_PROFILE_ID for profile_id, _ in application.calls)


def test_commands_are_read_only_and_sync_explicitly_directs_to_existing_cli() -> None:
    calls: list[UUID] = []

    def status_reader(profile_id: UUID) -> QuestionStatus:
        calls.append(profile_id)
        return QuestionStatus(True, {EvidenceSource.LAB: 2})

    commands = ReadOnlyQuestionCommands(status_reader)
    command = TelegramCommand(_context(PROFILE_ID), "status")

    status = commands.status(command)
    sync = commands.sync(TelegramCommand(_context(PROFILE_ID), "sync"))

    assert calls == [PROFILE_ID]
    assert "lab=2" in status
    assert "health-agent gmail sync" in sync
    assert sync == SYNC_INSTRUCTIONS


def test_default_attachment_inbox_is_truthful_needs_attention() -> None:
    receipt = NeedsAttentionMedicalInbox().ingest(
        _attachment(PROFILE_ID), [b"first", b"second"]
    )

    assert receipt.status == "needs_attention"
    assert receipt.size_bytes == len(b"firstsecond")
    assert receipt.reply_text == ATTACHMENT_NEEDS_ATTENTION_TEXT
    assert "not imported" in receipt.reply_text


def test_runtime_composition_verifies_local_credential_without_printing_or_network(
    tmp_path: Path,
) -> None:
    token = "123:real-token-must-not-leak"
    captured: dict[str, object] = {}
    application = FakeApplication()

    class TokenStore:
        def __init__(self, path: Path) -> None:
            assert path == tmp_path / "token"

        def load_verified(self) -> VerifiedBotCredential:
            return VerifiedBotCredential(token, 99, "safe_bot")

    class State:
        pass

    gateway = SimpleNamespace()
    poller = SimpleNamespace(run_forever=lambda: None)

    def update_factory(*args: object, **kwargs: object) -> object:
        captured["update_args"] = args
        captured["update_kwargs"] = kwargs
        updates = object()
        captured["updates"] = updates
        return updates

    def poller_factory(*args: object, **kwargs: object) -> object:
        captured["poller_args"] = args
        return poller

    runtime = build_telegram_question_runtime(
        Settings(
            telegram_token_file=tmp_path / "token",
            telegram_state_path=tmp_path / "state.sqlite3",
            telegram_staging_path=tmp_path / "staging",
        ),
        question_application_factory=lambda _: application,  # type: ignore[arg-type]
        token_store_factory=TokenStore,  # type: ignore[arg-type]
        state_factory=lambda _: State(),  # type: ignore[arg-type]
        gateway_factory=lambda candidate: _gateway(candidate, token, gateway),
        messenger_factory=lambda *_: SimpleNamespace(),  # type: ignore[arg-type]
        update_service_factory=update_factory,  # type: ignore[arg-type]
        poller_factory=poller_factory,  # type: ignore[arg-type]
        status_reader=lambda _: QuestionStatus(True, {}),
    )

    assert runtime.poller is poller
    assert captured["poller_args"][:2] == (99, gateway)
    assert captured["poller_args"][-1] is captured["updates"]
    assert token not in repr(captured)


def _gateway(candidate: str, token: str, gateway: SimpleNamespace) -> SimpleNamespace:
    assert candidate == token
    return gateway


def _context(profile_id: UUID) -> MessageContext:
    return MessageContext(
        bot_id=99,
        profile_id=profile_id,
        telegram_user_id=123,
        chat_id=123,
        message_id=1,
        update_id=2,
        sent_at=None,
        received_at=datetime(2026, 9, 4, tzinfo=UTC),
    )


def _attachment(profile_id: UUID) -> AttachmentProvenance:
    return AttachmentProvenance(
        context=_context(profile_id),
        kind="document",
        file_id="file-id",
        file_unique_id="unique-id",
        file_name="ignored.pdf",
        mime_type="application/pdf",
        validated_media_type="application/pdf",
        declared_size_bytes=11,
        duration_seconds=None,
        source_external_id="telegram:99:123:1:unique-id",
    )
