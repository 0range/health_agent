from __future__ import annotations

import hashlib
import stat
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from uuid import UUID

import pytest

from health_agent.config import Settings
from health_agent.importer import ImportReport
from health_agent.questions.composition import (
    ATTACHMENT_NEEDS_ATTENTION_TEXT,
    SYNC_INSTRUCTIONS,
    NeedsAttentionMedicalInbox,
    QuestionStatus,
    ReadOnlyQuestionCommands,
    TelegramHealthQuestionService,
    TelegramMedicalInbox,
    build_telegram_question_runtime,
)
from health_agent.questions.models import EvidenceSource
from health_agent.questions.service import QuestionAnswerResult
from health_agent.telegram.stores import SqliteTelegramState
from health_agent.telegram.types import (
    AttachmentProvenance,
    HealthQuestion,
    MessageContext,
    TelegramCommand,
    VerifiedBotCredential,
)
from health_agent.vault import FileVault

PROFILE_ID = UUID("00000000-0000-0000-0000-000000000001")
OTHER_PROFILE_ID = UUID("00000000-0000-0000-0000-000000000002")


class FakeApplication:
    def __init__(self) -> None:
        self.calls: list[tuple[UUID, str]] = []

    def answer(
        self, profile_id: UUID, question: str, *, request_id: str | None = None
    ) -> QuestionAnswerResult:
        assert request_id is not None
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
    assert sync.startswith(SYNC_INSTRUCTIONS)
    assert f"health-agent gmail sync {PROFILE_ID}" in sync
    assert f"health-agent whoop sync --profile-id {PROFILE_ID}" in sync
    assert "gmail sync --profile-id" not in sync


def test_default_attachment_inbox_is_truthful_needs_attention() -> None:
    receipt = NeedsAttentionMedicalInbox().ingest(
        _attachment(PROFILE_ID), [b"first", b"second"]
    )

    assert receipt.status == "needs_attention"
    assert receipt.size_bytes == len(b"firstsecond")
    assert receipt.reply_text == ATTACHMENT_NEEDS_ATTENTION_TEXT
    assert "not imported" in receipt.reply_text


def test_telegram_pdf_inbox_imports_with_profile_provenance_and_is_replay_safe(
    tmp_path: Path,
) -> None:
    calls: list[dict[str, object]] = []
    temporary_root = tmp_path / "temporary"
    vault = FileVault(tmp_path / "vault")

    def importer(*args: object, **kwargs: object) -> ImportReport:
        source = args[2]
        assert isinstance(source, Path)
        assert source.read_bytes() == b"pdf bytes"
        assert stat.S_IMODE(source.stat().st_mode) == 0o600
        calls.append(dict(kwargs))
        return ImportReport(
            "duplicate" if len(calls) == 2 else "imported",
            "processed",
            PROFILE_ID,
            0,
            0,
        )

    @contextmanager
    def sessions(_engine: object):
        yield object()

    inbox = TelegramMedicalInbox(
        object(),  # type: ignore[arg-type]
        vault,
        temporary_root,
        importer=importer,  # type: ignore[arg-type]
        session_scope_factory=sessions,  # type: ignore[arg-type]
    )

    first = inbox.ingest(_attachment(PROFILE_ID), [b"pdf ", b"bytes"])
    second = inbox.ingest(_attachment(PROFILE_ID), [b"pdf bytes"])

    assert first.status == "imported"
    assert second.status == "imported"
    assert first.reply_text == second.reply_text
    assert first.sha256 == hashlib.sha256(b"pdf bytes").hexdigest()
    assert first.size_bytes == len(b"pdf bytes")
    assert all(call["profile_id"] == PROFILE_ID for call in calls)
    assert all(call["source_provider"] == "telegram" for call in calls)
    assert all(
        call["source_external_id"] == _attachment(PROFILE_ID).source_external_id
        for call in calls
    )
    assert list(temporary_root.glob("*")) == []


def test_telegram_inbox_fully_consumes_non_pdf_without_importing(tmp_path: Path) -> None:
    called = False

    def importer(*_args: object, **_kwargs: object) -> ImportReport:
        nonlocal called
        called = True
        raise AssertionError("non-PDF must not reach importer")

    @contextmanager
    def sessions(_engine: object):
        yield object()

    provenance = _attachment(PROFILE_ID)
    non_pdf = replace(provenance, validated_media_type="image/jpeg")
    inbox = TelegramMedicalInbox(
        object(),  # type: ignore[arg-type]
        FileVault(tmp_path / "vault"),
        tmp_path / "temporary",
        importer=importer,  # type: ignore[arg-type]
        session_scope_factory=sessions,  # type: ignore[arg-type]
    )

    receipt = inbox.ingest(non_pdf, [b"not", b" a PDF"])

    assert not called
    assert receipt.status == "needs_attention"
    assert receipt.sha256 == hashlib.sha256(b"not a PDF").hexdigest()
    assert "not imported" in receipt.reply_text
    assert list((tmp_path / "temporary").glob("*")) == []


def test_telegram_inbox_rejects_symlinked_temporary_root(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    temporary = tmp_path / "temporary"
    temporary.symlink_to(target, target_is_directory=True)
    inbox = TelegramMedicalInbox(
        object(),  # type: ignore[arg-type]
        FileVault(tmp_path / "vault"),
        temporary,
    )

    try:
        inbox.ingest(_attachment(PROFILE_ID), [b"pdf bytes"])
    except ValueError:
        pass
    else:
        raise AssertionError("symlinked temporary root was accepted")

    assert list(target.iterdir()) == []


def test_telegram_inbox_removes_temporary_bytes_on_invalid_stream(tmp_path: Path) -> None:
    inbox = TelegramMedicalInbox(
        object(),  # type: ignore[arg-type]
        FileVault(tmp_path / "vault"),
        tmp_path / "temporary",
    )

    try:
        inbox.ingest(_attachment(PROFILE_ID), [b"pdf bytes", "invalid"])  # type: ignore[list-item]
    except TypeError:
        pass
    else:
        raise AssertionError("invalid attachment stream was accepted")

    assert list((tmp_path / "temporary").glob("*")) == []


def test_telegram_inbox_removes_temporary_bytes_when_write_is_interrupted(
    tmp_path: Path,
) -> None:
    inbox = TelegramMedicalInbox(
        object(),  # type: ignore[arg-type]
        FileVault(tmp_path / "vault"),
        tmp_path / "temporary",
    )

    def interrupted_stream():
        yield b"partial private bytes"
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        inbox.ingest(_attachment(PROFILE_ID), interrupted_stream())

    assert list((tmp_path / "temporary").glob("*")) == []


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

    registrations: list[tuple[int, str | None]] = []

    class State:

        def register_bot(self, bot_id: int, username: str | None) -> None:
            registrations.append((bot_id, username))

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
            telegram_root=tmp_path / "telegram",
            telegram_token_file=tmp_path / "token",
            telegram_state_path=tmp_path / "state.sqlite3",
            telegram_staging_path=tmp_path / "staging",
        ),
        question_application_factory=lambda _: application,  # type: ignore[arg-type]
        token_store_factory=TokenStore,  # type: ignore[arg-type]
        state_factory=lambda _: cast(SqliteTelegramState, State()),
        gateway_factory=lambda candidate: _gateway(candidate, token, gateway),
        messenger_factory=lambda *_: SimpleNamespace(),  # type: ignore[arg-type]
        update_service_factory=update_factory,  # type: ignore[arg-type]
        poller_factory=poller_factory,  # type: ignore[arg-type]
        medical_inbox=NeedsAttentionMedicalInbox(),
        status_reader=lambda _: QuestionStatus(True, {}),
    )

    assert runtime.poller is poller
    poller_args = captured["poller_args"]
    assert isinstance(poller_args, tuple)
    assert poller_args[:2] == (99, gateway)
    assert poller_args[-1] is captured["updates"]
    assert registrations == [(99, "safe_bot")]
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
