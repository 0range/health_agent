from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from health_agent.telegram.api import (
    MAX_DOWNLOAD_BYTES,
    TelegramAPIError,
    TelegramDeliveryUnknown,
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
    ProcessResult,
    RemoteFile,
    TelegramCommand,
    TelegramIdentity,
)

BOT_ID = 111


@dataclass
class Clock:
    value: datetime = datetime(2026, 9, 4, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


@dataclass
class FakeGateway:
    files: dict[str, bytes] = field(default_factory=dict)
    updates: tuple[dict[str, object], ...] = ()
    webhook_url: str = ""
    sent: list[tuple[int, str]] = field(default_factory=list)
    requested_offsets: list[int | None] = field(default_factory=list)
    send_failure: Exception | None = None

    def get_me(self) -> dict[str, object]:
        return {"id": BOT_ID, "is_bot": True}

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
        if self.send_failure is not None:
            raise self.send_failure
        self.sent.append((chat_id, text))
        return len(self.sent)


@dataclass
class FakeQuestions:
    calls: list[HealthQuestion] = field(default_factory=list)
    failure: Exception | None = None

    def answer(self, question: HealthQuestion) -> str:
        self.calls.append(question)
        if self.failure is not None:
            raise self.failure
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
    wrong_receipt: bool = False

    def ingest(self, provenance: AttachmentProvenance, chunks) -> InboxReceipt:
        content = b"".join(chunks)
        self.calls.append((provenance, content))
        return InboxReceipt(
            "0" * 64 if self.wrong_receipt else hashlib.sha256(content).hexdigest(),
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
    attachment: tuple[str, object] | None = None,
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


def _service(
    tmp_path,
    *,
    two_profiles: bool = False,
    clock: Clock | None = None,
    lease_seconds: float = 60,
    owner_id: str = "worker-a",
):
    current_clock = clock or Clock()
    state = SqliteTelegramState(tmp_path / "state.sqlite3", clock=current_clock)
    state.register_bot(BOT_ID, "bot")
    first = uuid4()
    state.bind_identity(BOT_ID, TelegramIdentity(101, first, 101))
    second = uuid4()
    if two_profiles:
        state.bind_identity(BOT_ID, TelegramIdentity(202, second, 202))
    gateway = FakeGateway()
    questions = FakeQuestions()
    commands = FakeCommands()
    inbox = FakeInbox()
    service = TelegramUpdateService(
        BOT_ID,
        gateway,
        state,
        TelegramMessenger(BOT_ID, gateway, state),
        questions,
        commands,
        inbox,
        staging_root=tmp_path / "staging",
        owner_id=owner_id,
        lease_seconds=lease_seconds,
        clock=current_clock,
    )
    return (
        service,
        state,
        gateway,
        questions,
        commands,
        inbox,
        first,
        second,
        current_clock,
    )


def test_unknown_and_group_messages_are_silently_ignored(tmp_path) -> None:
    service, state, gateway, questions, commands, inbox, *_ = _service(tmp_path)

    unknown = service.process_update(_message(1, 999, text="private medical question"))
    group = service.process_update(
        _message(2, 101, text="group medical question", chat_type="group")
    )

    assert unknown.status == "ignored_unknown_user"
    assert group.status == "ignored_ambiguous_chat"
    assert not gateway.sent
    assert not questions.calls and not commands.calls and not inbox.calls
    assert "private medical question" not in repr(state.audit_rows("updates"))


def test_malformed_sender_without_explicit_human_flag_is_not_routed(tmp_path) -> None:
    service, _, gateway, questions, commands, inbox, *_ = _service(tmp_path)
    update = _message(22, 101, text="must not reach agent")
    message = update["message"]
    assert isinstance(message, dict)
    message["from"] = {"id": 101}

    result = service.process_update(update)

    assert result.status == "ignored_ambiguous_chat"
    assert not gateway.sent
    assert not questions.calls and not commands.calls and not inbox.calls


def test_questions_and_commands_are_profile_scoped(tmp_path) -> None:
    service, _, gateway, questions, commands, _, profile_id, *_ = _service(tmp_path)

    service.process_update(_message(3, 101, text="Почему я плохо спал?"))
    service.process_update(_message(4, 101, text="/status"))
    service.process_update(_message(5, 101, text="/sync@my_health_bot"))
    service.process_update(_message(6, 101, text="/help"))

    context = questions.calls[0].context
    assert context.profile_id == profile_id
    assert context.message_id == 13
    assert context.sent_at == datetime.fromtimestamp(1_700_000_000, tz=UTC)
    assert [call.name for call in commands.calls] == ["status", "sync"]
    assert [text for _, text in gateway.sent] == [
        "grounded answer",
        "profile status",
        "sync started",
        HELP_TEXT,
    ]


@pytest.mark.parametrize(
    ("kind", "file_id", "content", "metadata", "media_type"),
    [
        (
            "document",
            "document-1",
            b"%PDF-1.7 medical data",
            {"mime_type": "application/pdf", "file_name": "labs.pdf"},
            "application/pdf",
        ),
        (
            "photo",
            "photo-1",
            b"\xff\xd8\xffmedical image",
            None,
            "image/jpeg",
        ),
        (
            "voice",
            "voice-1",
            b"OggSmedical voice",
            {"mime_type": "audio/ogg", "duration": 5},
            "audio/ogg",
        ),
    ],
)
def test_validated_attachment_reaches_inbox(
    tmp_path, kind, file_id, content, metadata, media_type
) -> None:
    service, state, gateway, _, _, inbox, profile_id, *_ = _service(tmp_path)
    gateway.files[file_id] = content
    values = {
        "file_id": file_id,
        "file_unique_id": f"unique-{file_id}",
        "file_size": len(content),
        **(metadata or {}),
    }
    attachment: object = values
    if kind == "photo":
        attachment = [values]

    result = service.process_update(_message(10, 101, attachment=(kind, attachment)))

    assert result.status == "accepted"
    provenance, streamed = inbox.calls[0]
    assert provenance.context.bot_id == BOT_ID
    assert provenance.context.profile_id == profile_id
    assert provenance.validated_media_type == media_type
    assert provenance.source_external_id.startswith(f"telegram:{BOT_ID}:101:")
    assert streamed == content
    assert (
        state.audit_rows("attachment_audit")[0]["sha256"]
        == hashlib.sha256(content).hexdigest()
    )
    assert not list((tmp_path / "staging").iterdir())


@pytest.mark.parametrize(
    ("content", "mime", "size", "error"),
    [
        (b"not a PDF", "application/pdf", 9, "unsupported_attachment_signature"),
        (b"%PDF-valid", "image/jpeg", 10, "attachment_mime_mismatch"),
        (b"%PDF-valid", "application/pdf", 999, "attachment_size_mismatch"),
    ],
)
def test_invalid_signature_mime_or_size_never_reaches_inbox(
    tmp_path, content, mime, size, error
) -> None:
    service, state, gateway, _, _, inbox, *_ = _service(tmp_path)
    gateway.files["document"] = content

    result = service.process_update(
        _message(
            11,
            101,
            attachment=(
                "document",
                {
                    "file_id": "document",
                    "file_unique_id": "unique",
                    "file_size": size,
                    "mime_type": mime,
                },
            ),
        )
    )

    assert result.status == "needs_attention"
    assert state.audit_rows("updates")[0]["safe_error_code"] == error
    assert not inbox.calls


def test_wrong_inbox_receipt_records_staged_truth_and_needs_attention(tmp_path) -> None:
    service, state, gateway, _, _, inbox, *_ = _service(tmp_path)
    content = b"%PDF-valid"
    gateway.files["document"] = content
    inbox.wrong_receipt = True

    result = service.process_update(
        _message(
            12,
            101,
            attachment=(
                "document",
                {
                    "file_id": "document",
                    "file_unique_id": "unique",
                    "file_size": len(content),
                    "mime_type": "application/pdf",
                },
            ),
        )
    )

    assert result.status == "needs_attention"
    audit = state.audit_rows("attachment_audit")[0]
    assert audit["sha256"] == hashlib.sha256(content).hexdigest()
    assert audit["status"] == "inbox_needs_attention"


def test_declared_oversized_file_is_actionable_without_download(tmp_path) -> None:
    service, _, gateway, _, _, inbox, *_ = _service(tmp_path)

    result = service.process_update(
        _message(
            13,
            101,
            attachment=(
                "document",
                {
                    "file_id": "too-large",
                    "file_unique_id": "too-large-u",
                    "file_size": MAX_DOWNLOAD_BYTES + 1,
                    "mime_type": "application/pdf",
                },
            ),
        )
    )

    assert result.status == "file_too_large"
    assert not inbox.calls
    assert gateway.sent and "20" in gateway.sent[0][1]


def test_actual_oversized_stream_is_actionable_before_inbox(tmp_path) -> None:
    service, _, gateway, _, _, inbox, *_ = _service(tmp_path)
    gateway.get_file = lambda file_id: RemoteFile(  # type: ignore[method-assign]
        file_id, "unique", "huge", None
    )

    def oversized(_):
        yield b"%PDF-"
        yield b"x" * MAX_DOWNLOAD_BYTES

    gateway.download_chunks = oversized  # type: ignore[method-assign]

    result = service.process_update(
        _message(
            131,
            101,
            attachment=(
                "document",
                {
                    "file_id": "huge",
                    "file_unique_id": "huge-u",
                    "mime_type": "application/pdf",
                },
            ),
        )
    )

    assert result.status == "file_too_large"
    assert not inbox.calls
    assert gateway.sent and "20" in gateway.sent[0][1]


def test_repeated_update_imports_and_replies_only_once(tmp_path) -> None:
    service, _, gateway, _, _, inbox, *_ = _service(tmp_path)
    content = b"%PDF-medical"
    gateway.files["document"] = content
    update = _message(
        14,
        101,
        attachment=(
            "document",
            {
                "file_id": "document",
                "file_unique_id": "unique",
                "file_size": len(content),
                "mime_type": "application/pdf",
            },
        ),
    )

    assert service.process_update(update).status == "accepted"
    assert service.process_update(update).status == "accepted"
    assert len(inbox.calls) == 1
    assert gateway.sent == [(101, "Файл принят")]


def test_identical_bytes_for_two_profiles_remain_profile_scoped(tmp_path) -> None:
    service, _, gateway, _, _, inbox, first, second, _ = _service(
        tmp_path, two_profiles=True
    )
    content = b"%PDF-same"
    gateway.files.update({"first": content, "second": content})
    for update_id, user_id, file_id in ((20, 101, "first"), (21, 202, "second")):
        service.process_update(
            _message(
                update_id,
                user_id,
                attachment=(
                    "document",
                    {
                        "file_id": file_id,
                        "file_unique_id": f"{file_id}-u",
                        "file_size": len(content),
                        "mime_type": "application/pdf",
                    },
                ),
            )
        )
    assert [call[0].context.profile_id for call in inbox.calls] == [first, second]
    assert inbox.calls[0][1] == inbox.calls[1][1]


def test_retryable_update_has_backoff_and_bounded_attempts(tmp_path) -> None:
    clock = Clock()
    service, state, gateway, questions, *_ = _service(tmp_path, clock=clock)
    questions.failure = TelegramTransientError("agent_unavailable")
    gateway.updates = (_message(30, 101, text="question"),)
    poller = TelegramLongPoller(BOT_ID, gateway, state, service, clock=clock)

    first = poller.poll_once()
    immediate = poller.poll_once()
    assert first.blocked_until == clock.value + timedelta(seconds=5)
    assert immediate.blocked_until == first.blocked_until
    assert len(questions.calls) == 1

    clock.advance(5)
    assert not poller.poll_once().completed
    clock.advance(10)
    assert not poller.poll_once().completed
    clock.advance(20)
    terminal = poller.poll_once()
    assert terminal.completed == 1
    assert terminal.next_offset == 31
    assert len(questions.calls) == 4
    assert state.audit_rows("updates")[0]["status"] == "needs_attention"


def test_daemon_sleeps_until_persisted_update_retry_time(tmp_path) -> None:
    clock = Clock()
    service, state, gateway, questions, *_ = _service(tmp_path, clock=clock)
    questions.failure = TelegramTransientError("agent_unavailable")
    gateway.updates = (_message(31, 101, text="question"),)
    sleeps: list[float] = []

    def stop_after_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        raise KeyboardInterrupt

    poller = TelegramLongPoller(
        BOT_ID,
        gateway,
        state,
        service,
        clock=clock,
        sleeper=stop_after_sleep,
    )

    with pytest.raises(KeyboardInterrupt):
        poller.run_forever()
    assert sleeps == [5]
    assert len(questions.calls) == 1


def test_heartbeat_prevents_slow_update_from_concurrent_reclaim(tmp_path) -> None:
    state = SqliteTelegramState(tmp_path / "state.sqlite3")
    state.register_bot(BOT_ID, "bot")
    profile = uuid4()
    state.bind_identity(BOT_ID, TelegramIdentity(101, profile, 101))
    gateway = FakeGateway()
    entered = threading.Event()
    release = threading.Event()

    class SlowQuestions(FakeQuestions):
        def answer(self, question: HealthQuestion) -> str:
            self.calls.append(question)
            entered.set()
            assert release.wait(timeout=2)
            return "slow answer"

    slow = SlowQuestions()
    first = TelegramUpdateService(
        BOT_ID,
        gateway,
        state,
        TelegramMessenger(BOT_ID, gateway, state),
        slow,
        FakeCommands(),
        FakeInbox(),
        staging_root=tmp_path / "staging",
        owner_id="worker-a",
        lease_seconds=0.12,
    )
    second_questions = FakeQuestions()
    second = TelegramUpdateService(
        BOT_ID,
        gateway,
        state,
        TelegramMessenger(BOT_ID, gateway, state),
        second_questions,
        FakeCommands(),
        FakeInbox(),
        staging_root=tmp_path / "staging",
        owner_id="worker-b",
        lease_seconds=0.12,
    )
    update = _message(40, 101, text="slow")
    result: list[ProcessResult] = []
    worker = threading.Thread(
        target=lambda: result.append(first.process_update(update))
    )
    worker.start()
    assert entered.wait(timeout=1)
    time.sleep(0.25)

    blocked = second.process_update(update)
    release.set()
    worker.join(timeout=2)

    assert blocked.status == "processing"
    assert not blocked.terminal
    assert not second_questions.calls
    assert result and result[0].status == "replied"


def test_delivery_unknown_is_terminal_and_exposed(tmp_path) -> None:
    service, state, gateway, *_ = _service(tmp_path)
    gateway.send_failure = TelegramDeliveryUnknown("delivery_transport_unknown")

    result = service.process_update(_message(50, 101, text="question"))

    assert result.status == "delivery_unknown"
    update = state.audit_rows("updates")[0]
    outbound = state.audit_rows("outbound_audit")[0]
    assert update["safe_error_code"] == "delivery_transport_unknown"
    assert outbound["status"] == "unknown"


def test_malformed_update_is_quarantined_without_stopping_later_update(
    tmp_path,
) -> None:
    service, state, gateway, *_ = _service(tmp_path)
    gateway.updates = (
        {"update_id": "bad", "message": {"text": "not persisted"}},
        _message(60, 101, text="question"),
    )

    report = TelegramLongPoller(BOT_ID, gateway, state, service).poll_once()

    assert report.malformed == 1
    assert report.completed == 1
    assert report.next_offset == 61
    assert state.runtime_status(BOT_ID)[2] == "malformed_update"


def test_poller_refuses_existing_webhook_and_records_failure(tmp_path) -> None:
    service, state, gateway, *_ = _service(tmp_path)
    gateway.webhook_url = "https://example.invalid/hook"

    with pytest.raises(TelegramWebhookConfigured):
        TelegramLongPoller(BOT_ID, gateway, state, service).poll_once()
    assert state.runtime_status(BOT_ID)[2] == "webhook_configured"


def test_poller_refuses_gateway_from_a_different_bot_namespace(tmp_path) -> None:
    service, state, gateway, *_ = _service(tmp_path)
    gateway.get_me = lambda: {"id": 999, "is_bot": True}  # type: ignore[method-assign]

    with pytest.raises(TelegramAPIError, match="bot_identity_mismatch"):
        TelegramLongPoller(BOT_ID, gateway, state, service).poll_once()
    assert state.runtime_status(BOT_ID)[2] == "bot_identity_mismatch"


def test_daemon_backs_off_instead_of_crashing_on_malformed_service_response(
    tmp_path,
) -> None:
    service, state, gateway, *_ = _service(tmp_path)
    gateway.get_me = lambda: {"id": "malformed", "is_bot": True}  # type: ignore[method-assign]
    sleeps: list[float] = []

    def stop_after_backoff(seconds: float) -> None:
        sleeps.append(seconds)
        raise KeyboardInterrupt

    poller = TelegramLongPoller(
        BOT_ID, gateway, state, service, sleeper=stop_after_backoff
    )

    with pytest.raises(KeyboardInterrupt):
        poller.run_forever()
    assert sleeps == [30]
    assert state.runtime_status(BOT_ID)[2] == "invalid_get_me_response"
