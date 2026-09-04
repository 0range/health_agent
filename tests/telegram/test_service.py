from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from health_agent.telegram.api import (
    MAX_DOWNLOAD_BYTES,
    TelegramTransientError,
    TelegramWebhookConfigured,
)
from health_agent.telegram.messenger import TelegramMessenger
from health_agent.telegram.service import (
    HELP_TEXT,
    TelegramLongPoller,
    TelegramUpdateService,
)
from health_agent.telegram.stores import SqliteTelegramState
from health_agent.telegram.types import (
    AttachmentProvenance,
    HealthQuestion,
    InboxReceipt,
    RemoteFile,
    TelegramCommand,
    TelegramIdentity,
)


@dataclass
class FakeGateway:
    files: dict[str, bytes] = field(default_factory=dict)
    updates: tuple[dict[str, object], ...] = ()
    webhook_url: str = ""
    sent: list[tuple[int, str]] = field(default_factory=list)
    requested_offsets: list[int | None] = field(default_factory=list)

    def get_me(self) -> dict[str, object]:
        return {"id": 1}

    def get_webhook_url(self) -> str:
        return self.webhook_url

    def get_updates(
        self, *, offset: int | None, timeout_seconds: int
    ) -> tuple[dict[str, object], ...]:
        self.requested_offsets.append(offset)
        return self.updates

    def get_file(self, file_id: str) -> RemoteFile:
        value = self.files[file_id]
        return RemoteFile(file_id, f"unique-{file_id}", file_id, len(value))

    def download_chunks(self, file_path: str):
        value = self.files[file_path]
        yield value[:3]
        yield value[3:]

    def send_message(self, chat_id: int, text: str) -> int:
        self.sent.append((chat_id, text))
        return len(self.sent)


@dataclass
class FakeQuestions:
    calls: list[HealthQuestion] = field(default_factory=list)

    def answer(self, question: HealthQuestion) -> str:
        self.calls.append(question)
        return "grounded answer"


@dataclass
class FakeCommands:
    calls: list[TelegramCommand] = field(default_factory=list)

    def status(self, command: TelegramCommand) -> str:
        self.calls.append(command)
        return "profile status"

    def sync(self, command: TelegramCommand) -> str:
        self.calls.append(command)
        return "sync started"


@dataclass
class FakeInbox:
    calls: list[tuple[AttachmentProvenance, bytes]] = field(default_factory=list)

    def ingest(self, provenance: AttachmentProvenance, chunks) -> InboxReceipt:
        content = b"".join(chunks)
        self.calls.append((provenance, content))
        return InboxReceipt(
            hashlib.sha256(content).hexdigest(),
            len(content),
            "accepted",
            "Файл принят",
            external_reference=f"inbox:{provenance.context.profile_id}:{len(self.calls)}",
        )


def _message(
    update_id: int,
    user_id: int,
    *,
    text: str | None = None,
    chat_type: str = "private",
    attachment: tuple[str, dict[str, object]] | None = None,
) -> dict[str, object]:
    message: dict[str, object] = {
        "message_id": update_id + 10,
        "date": 1_700_000_000,
        "from": {"id": user_id, "is_bot": False},
        "chat": {"id": user_id, "type": chat_type},
    }
    if text is not None:
        message["text"] = text
    if attachment is not None:
        message[attachment[0]] = attachment[1]
    return {"update_id": update_id, "message": message}


def _service(tmp_path, *, two_profiles: bool = False):
    state = SqliteTelegramState(tmp_path / "state.sqlite3")
    first = uuid4()
    state.bind_identity(TelegramIdentity(101, first, 101))
    second = uuid4()
    if two_profiles:
        state.bind_identity(TelegramIdentity(202, second, 202))
    gateway = FakeGateway()
    questions = FakeQuestions()
    commands = FakeCommands()
    inbox = FakeInbox()
    service = TelegramUpdateService(
        gateway,
        state,
        TelegramMessenger(gateway, state),
        questions,
        commands,
        inbox,
        clock=lambda: datetime(2026, 9, 4, tzinfo=UTC),
    )
    return service, state, gateway, questions, commands, inbox, first, second


def test_unknown_and_group_messages_are_silently_ignored(tmp_path) -> None:
    service, state, gateway, questions, commands, inbox, *_ = _service(tmp_path)

    unknown = service.process_update(_message(1, 999, text="private medical question"))
    group = service.process_update(
        _message(2, 101, text="group medical question", chat_type="group")
    )

    assert unknown.status == "ignored_unknown_user"
    assert group.status == "ignored_ambiguous_chat"
    assert not gateway.sent
    assert not questions.calls
    assert not commands.calls
    assert not inbox.calls
    assert "private medical question" not in repr(state.audit_rows("updates"))


def test_questions_and_commands_are_profile_scoped(tmp_path) -> None:
    service, _, gateway, questions, commands, _, profile_id, _ = _service(tmp_path)

    service.process_update(_message(3, 101, text="Почему я плохо спал?"))
    service.process_update(_message(4, 101, text="/status"))
    service.process_update(_message(5, 101, text="/sync@my_health_bot"))
    service.process_update(_message(6, 101, text="/help"))

    context = questions.calls[0].context
    assert context.profile_id == profile_id
    assert context.telegram_user_id == 101
    assert context.message_id == 13
    assert context.update_id == 3
    assert context.sent_at == datetime.fromtimestamp(1_700_000_000, tz=UTC)
    assert context.received_at == datetime(2026, 9, 4, tzinfo=UTC)
    assert [call.name for call in commands.calls] == ["status", "sync"]
    assert [text for _, text in gateway.sent] == [
        "grounded answer",
        "profile status",
        "sync started",
        HELP_TEXT,
    ]


@pytest.mark.parametrize(
    ("kind", "metadata", "expected_mime"),
    [
        (
            "document",
            {
                "file_id": "document-1",
                "file_unique_id": "doc-unique",
                "file_name": "labs.pdf",
                "mime_type": "application/pdf",
                "file_size": 12,
            },
            "application/pdf",
        ),
        (
            "photo",
            [
                {
                    "file_id": "photo-small",
                    "file_unique_id": "small",
                    "file_size": 2,
                    "width": 10,
                    "height": 10,
                },
                {
                    "file_id": "photo-large",
                    "file_unique_id": "large",
                    "file_size": 12,
                    "width": 100,
                    "height": 100,
                },
            ],
            "image/jpeg",
        ),
        (
            "voice",
            {
                "file_id": "voice-1",
                "file_unique_id": "voice-unique",
                "mime_type": "audio/ogg",
                "file_size": 12,
                "duration": 5,
            },
            "audio/ogg",
        ),
    ],
)
def test_attachment_metadata_and_stream_reach_injected_inbox(
    tmp_path, kind, metadata, expected_mime
) -> None:
    service, state, gateway, _, _, inbox, profile_id, _ = _service(tmp_path)
    file_id = (
        metadata[-1]["file_id"] if isinstance(metadata, list) else metadata["file_id"]
    )
    gateway.files[str(file_id)] = b"medical data"

    result = service.process_update(_message(10, 101, attachment=(kind, metadata)))

    assert result.status == "accepted"
    provenance, content = inbox.calls[0]
    assert provenance.context.profile_id == profile_id
    assert provenance.kind == kind
    assert provenance.mime_type == expected_mime
    assert provenance.source_external_id.startswith("telegram:101:20:")
    assert content == b"medical data"
    audit = state.audit_rows("attachment_audit")[0]
    assert audit["sha256"] == hashlib.sha256(content).hexdigest()
    assert gateway.sent == [(101, "Файл принят")]


def test_repeated_update_imports_and_replies_only_once(tmp_path) -> None:
    service, _, gateway, _, _, inbox, *_ = _service(tmp_path)
    gateway.files["document-1"] = b"medical data"
    update = _message(
        11,
        101,
        attachment=(
            "document",
            {
                "file_id": "document-1",
                "file_unique_id": "unique-1",
                "file_size": 12,
            },
        ),
    )

    first = service.process_update(update)
    second = service.process_update(update)

    assert first.status == "accepted"
    assert second.status == "accepted"
    assert len(inbox.calls) == 1
    assert gateway.sent == [(101, "Файл принят")]


def test_declared_oversized_file_is_actionable_without_download(tmp_path) -> None:
    service, _, gateway, _, _, inbox, *_ = _service(tmp_path)

    result = service.process_update(
        _message(
            12,
            101,
            attachment=(
                "document",
                {
                    "file_id": "too-large",
                    "file_unique_id": "too-large-u",
                    "file_size": MAX_DOWNLOAD_BYTES + 1,
                },
            ),
        )
    )

    assert result.status == "file_too_large"
    assert not inbox.calls
    assert gateway.sent and "20" in gateway.sent[0][1]


def test_identical_bytes_for_two_profiles_remain_profile_scoped(tmp_path) -> None:
    service, _, gateway, _, _, inbox, first, second = _service(
        tmp_path, two_profiles=True
    )
    gateway.files["first"] = b"same bytes"
    gateway.files["second"] = b"same bytes"

    service.process_update(
        _message(
            20,
            101,
            attachment=(
                "document",
                {"file_id": "first", "file_unique_id": "first-u", "file_size": 10},
            ),
        )
    )
    service.process_update(
        _message(
            21,
            202,
            attachment=(
                "document",
                {"file_id": "second", "file_unique_id": "second-u", "file_size": 10},
            ),
        )
    )

    assert [call[0].context.profile_id for call in inbox.calls] == [first, second]
    assert inbox.calls[0][1] == inbox.calls[1][1]
    assert inbox.calls[0][0].source_external_id != inbox.calls[1][0].source_external_id


def test_poller_advances_only_past_terminal_updates_and_reuses_offset(tmp_path) -> None:
    service, state, gateway, *_ = _service(tmp_path)
    update = _message(30, 101, text="question")
    gateway.updates = (update,)
    poller = TelegramLongPoller(gateway, state, service)

    first = poller.poll_once()
    second = poller.poll_once()

    assert first.next_offset == 31
    assert second.next_offset == 31
    assert gateway.requested_offsets == [None, 31]
    assert len(gateway.sent) == 1


def test_poller_refuses_existing_webhook(tmp_path) -> None:
    service, state, gateway, *_ = _service(tmp_path)
    gateway.webhook_url = "https://example.invalid/hook"

    with pytest.raises(TelegramWebhookConfigured):
        TelegramLongPoller(gateway, state, service).poll_once()
    assert state.runtime_status()[2] == "webhook_configured"


def test_retryable_failure_does_not_advance_offset_or_clear_error(tmp_path) -> None:
    service, state, gateway, questions, *_ = _service(tmp_path)

    def fail(_: HealthQuestion) -> str:
        raise TelegramTransientError("agent_temporarily_unavailable")

    questions.answer = fail  # type: ignore[method-assign]
    gateway.updates = (_message(40, 101, text="question"),)

    report = TelegramLongPoller(gateway, state, service).poll_once()

    assert report.completed == 0
    assert report.next_offset is None
    assert state.runtime_status()[2] == "update_retryable_error"
