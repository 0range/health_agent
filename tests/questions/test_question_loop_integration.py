"""Offline component coverage for the complete bound Telegram question path."""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from health_agent.db import session_scope
from health_agent.importer import ImportReport
from health_agent.models import (
    Document,
    DocumentPage,
    LabObservation,
    Profile,
    ReviewStatus,
)
from health_agent.questions.composition import (
    DatabaseHealthContextBuilder,
    QuestionStatus,
    ReadOnlyQuestionCommands,
    TelegramHealthQuestionService,
    TelegramMedicalInbox,
)
from health_agent.questions.openai import OpenAIResponsesResponder
from health_agent.questions.replies import PrivateReplyStore, delivery_request_id
from health_agent.questions.service import HealthQuestionApplicationService
from health_agent.telegram.api import TelegramDeferred
from health_agent.telegram.messenger import TelegramMessenger, split_message
from health_agent.telegram.service import TelegramLongPoller, TelegramUpdateService
from health_agent.telegram.stores import SqliteTelegramState
from health_agent.telegram.types import (
    AttachmentProvenance,
    InboxReceipt,
    RemoteFile,
    TelegramIdentity,
)
from health_agent.vault import FileVault
from health_agent.whoop.normalize import normalize_whoop
from health_agent.whoop.repository import (
    register_authorized_connection,
    store_normalized_record,
)

BOT_ID = 701
PROFILE_ID = UUID("00000000-0000-0000-0000-000000000001")


@pytest.mark.parametrize("deferred_part,restart", ((0, False), (0, True), (1, True)))
def test_deferred_question_reuses_exact_prepared_reply_across_restart_and_parts(
    clean_database: Engine, tmp_path: Path, deferred_part: int, restart: bool
) -> None:
    now = datetime.now(UTC)
    with session_scope(clean_database) as session:
        _add_verified_lab(session, PROFILE_ID, "Ferritin", Decimal(42), now)

    class ChangingResponses:
        def __init__(self):
            self.calls = []

        def create(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(status="completed", output_text=(
                f"Attempt {len(self.calls)}: recorded ferritin [LAB1]. " + "Detail. " * 900
            ))

    responses = ChangingResponses()

    class DeferredGateway(FakeTelegramGateway):
        def __init__(self):
            super().__init__((_free_form_update(),))
            self.attempted: list[str] = []
            self.deferred = False

        def send_message(self, chat_id: int, text: str) -> int:
            self.attempted.append(text)
            if len(self.sent) == deferred_part and not self.deferred:
                self.deferred = True
                raise TelegramDeferred(now + timedelta(seconds=1))
            return super().send_message(chat_id, text)

    state_path = tmp_path / "state.sqlite3"
    state = SqliteTelegramState(state_path, clock=lambda: now)
    state.register_bot(BOT_ID, "offline_bot")
    state.bind_identity(BOT_ID, TelegramIdentity(1001, PROFILE_ID, 1001))
    gateway = DeferredGateway()
    spool_root = tmp_path / "prepared-replies"

    def service(state):
        application = HealthQuestionApplicationService(
            DatabaseHealthContextBuilder(clean_database),
            OpenAIResponsesResponder("fake-key", client=SimpleNamespace(responses=responses)),
        )
        return TelegramUpdateService(
            BOT_ID, gateway, state, TelegramMessenger(BOT_ID, gateway, state),
            TelegramHealthQuestionService(application, PrivateReplyStore(spool_root)),
            ReadOnlyQuestionCommands(lambda _: QuestionStatus(True, {})), _NoAttachments(),
            staging_root=tmp_path / "staging", clock=lambda: now,
        )

    updates = service(state)
    first = updates.process_update(_free_form_update())
    assert not first.terminal and first.status == "retryable_error"
    path, = spool_root.iterdir()
    prepared = path.read_bytes().partition(b"\n")[2].decode("utf-8")
    original_parts = split_message(prepared)
    assert len(original_parts) >= 2
    assert len(responses.calls) == 1
    assert responses.calls[0]["extra_headers"] == {
        "X-Client-Request-Id": delivery_request_id(BOT_ID, 701)
    }

    # Simulate both changing evidence and a fresh application/responder process.
    with session_scope(clean_database) as session:
        _add_verified_lab(session, PROFILE_ID, "Changed data", Decimal(99), now)
    now += timedelta(seconds=2)
    if restart:
        state = SqliteTelegramState(state_path, clock=lambda: now)
        updates = service(state)
    second = updates.process_update(_free_form_update())

    assert second.terminal and second.status == "replied"
    assert len(responses.calls) == 1
    assert tuple(text for _, text in gateway.sent) == original_parts
    assert gateway.attempted[deferred_part] == gateway.attempted[deferred_part + 1]
    assert list(spool_root.iterdir()) == []
    assert all(row["status"] == "sent" for row in state.audit_rows("outbound_audit"))
    assert "Attempt 1" not in json.dumps(state.audit_rows("updates"))


def test_imported_pdf_deferred_reply_replays_identical_duplicate_receipt(tmp_path: Path):
    now = datetime.now(UTC)
    payload = b"%PDF-1.4\nsynthetic only\n%%EOF"
    statuses: list[str] = []

    def importer(*_args, **_kwargs):
        status = "imported" if not statuses else "duplicate"
        statuses.append(status)
        return ImportReport(status, "processed", PROFILE_ID, 0, 0)

    @contextmanager
    def sessions(_engine):
        yield object()

    inbox = TelegramMedicalInbox(
        object(), FileVault(tmp_path / "vault"), tmp_path / "temporary",  # type: ignore[arg-type]
        importer=importer, session_scope_factory=sessions,
    )

    class PdfGateway(FakeTelegramGateway):
        def __init__(self):
            super().__init__(())
            self.attempted = []

        def get_file(self, file_id):
            return RemoteFile(file_id, "unique-pdf", "pdf", len(payload))

        def download_chunks(self, file_path):
            yield payload

        def send_message(self, chat_id, text):
            self.attempted.append(text)
            if len(self.attempted) == 1:
                raise TelegramDeferred(now + timedelta(seconds=1))
            return super().send_message(chat_id, text)

    state = SqliteTelegramState(tmp_path / "state.sqlite3", clock=lambda: now)
    state.register_bot(BOT_ID, "offline_bot")
    state.bind_identity(BOT_ID, TelegramIdentity(1001, PROFILE_ID, 1001))
    gateway = PdfGateway()
    update = _free_form_update()
    message = update["message"]
    assert isinstance(message, dict)
    del message["text"]
    message["document"] = {
        "file_id": "pdf", "file_unique_id": "unique-pdf", "file_name": "test.pdf",
        "mime_type": "application/pdf", "file_size": len(payload),
    }
    application = SimpleNamespace(answer=lambda *_args, **_kwargs: None)

    def service():
        return TelegramUpdateService(
            BOT_ID, gateway, state, TelegramMessenger(BOT_ID, gateway, state),
            TelegramHealthQuestionService(application),
            ReadOnlyQuestionCommands(lambda _: QuestionStatus(True, {})), inbox,
            staging_root=tmp_path / "staging", clock=lambda: now,
        )

    assert service().process_update(update).status == "retryable_error"
    now += timedelta(seconds=2)
    result = service().process_update(update)
    assert result.terminal and result.status == "imported"
    assert statuses == ["imported", "duplicate"]
    assert gateway.attempted[0] == gateway.attempted[1]
    assert state.audit_rows("attachment_audit")[0]["status"] == "imported"
    assert state.audit_rows("outbound_audit")[0]["status"] == "sent"


class FakeResponses:
    """A strict in-process Responses endpoint; it makes no HTTP requests."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return SimpleNamespace(
            status="completed",
            output_text=(
                "The verified ferritin result is 42 ug/L [LAB1]. "
                "The recent recorded sleep duration is 7 hours [SLEEP1]."
            ),
        )


class FakeTelegramGateway:
    """A deterministic long-poll and delivery transport with no network boundary."""

    def __init__(self, updates: tuple[dict[str, object], ...]) -> None:
        self._updates = updates
        self.requested_offsets: list[int | None] = []
        self.sent: list[tuple[int, str]] = []

    def get_me(self) -> dict[str, object]:
        return {"id": BOT_ID, "is_bot": True}

    def get_webhook_url(self) -> str:
        return ""

    def get_updates(
        self, *, offset: int | None, timeout_seconds: int
    ) -> tuple[dict[str, object], ...]:
        assert timeout_seconds == 30
        self.requested_offsets.append(offset)
        return self._updates

    def send_message(self, chat_id: int, text: str) -> int:
        self.sent.append((chat_id, text))
        return len(self.sent)

    def get_file(self, file_id: str) -> RemoteFile:
        raise AssertionError(f"unexpected attachment request: {file_id}")

    def download_chunks(self, file_path: str) -> Iterator[bytes]:
        raise AssertionError(f"unexpected attachment download: {file_path}")


def test_bound_telegram_question_uses_only_profile_scoped_cited_evidence(
    clean_database: Engine, tmp_path
) -> None:
    """Exercise poller -> SQLite state -> retrieval -> Responses -> cited delivery.

    The fixture intentionally includes distinct lab and WHOOP values for another
    profile. Neither marker may cross the profile-scoped retrieval boundary into
    the mocked Responses request or the Telegram reply.
    """

    observed_at = datetime.now(UTC) - timedelta(days=1)
    other_profile_id = uuid4()
    with session_scope(clean_database) as session:
        session.add(Profile(id=other_profile_id, name="Other"))
        session.flush()
        _add_verified_lab(session, PROFILE_ID, "Ferritin", Decimal(42), observed_at)
        _add_verified_lab(
            session,
            other_profile_id,
            "Other-profile-secret-marker",
            Decimal(999),
            observed_at,
        )
        _add_whoop_sleep(session, PROFILE_ID, "primary-sleep", observed_at)
        _add_whoop_sleep(session, other_profile_id, "other-profile-secret-marker", observed_at)

    responses = FakeResponses()
    responder = OpenAIResponsesResponder(
        "test-key-not-a-token",
        client=SimpleNamespace(responses=responses),
    )
    application = HealthQuestionApplicationService(
        DatabaseHealthContextBuilder(clean_database), responder
    )
    state = SqliteTelegramState(tmp_path / "telegram-state.sqlite3")
    state.register_bot(BOT_ID, "offline_test_bot")
    state.bind_identity(BOT_ID, TelegramIdentity(1001, PROFILE_ID, 1001))
    gateway = FakeTelegramGateway((_free_form_update(),))
    updates = TelegramUpdateService(
        BOT_ID,
        gateway,
        state,
        TelegramMessenger(BOT_ID, gateway, state),
        TelegramHealthQuestionService(application),
        ReadOnlyQuestionCommands(lambda _profile_id: QuestionStatus(True, {})),
        _NoAttachments(),
        staging_root=tmp_path / "staging",
        owner_id="integration-test",
    )
    poller = TelegramLongPoller(BOT_ID, gateway, state, updates)

    report = poller.poll_once()

    assert (report.received, report.completed, report.next_offset) == (1, 1, 702)
    assert gateway.requested_offsets == [None]
    assert len(responses.calls) == 1
    call = responses.calls[0]
    assert call["store"] is False
    assert call["safety_identifier"] != str(PROFILE_ID)
    request_text = json.dumps(call["input"], sort_keys=True)
    assert "Ferritin" in request_text
    assert "primary-sleep" not in request_text
    assert "Other-profile-secret-marker" not in request_text
    assert "999" not in request_text
    assert len(gateway.sent) == 1
    chat_id, reply = gateway.sent[0]
    assert chat_id == 1001
    assert "[LAB1]" in reply and "[SLEEP1]" in reply
    assert "Sources:" in reply
    assert "Other-profile-secret-marker" not in reply
    assert "999" not in reply
    assert state.audit_rows("updates")[0]["status"] == "replied"
    outbound = state.audit_rows("outbound_audit")
    assert len(outbound) == 1
    assert outbound[0]["status"] == "sent"
    assert outbound[0]["profile_id"] == str(PROFILE_ID)


class _NoAttachments:
    def ingest(
        self, _provenance: AttachmentProvenance, _chunks: Iterable[bytes]
    ) -> InboxReceipt:
        raise AssertionError("free-form text must not enter the attachment path")


def _free_form_update() -> dict[str, object]:
    return {
        "update_id": 701,
        "message": {
            "message_id": 88,
            "date": int(datetime.now(UTC).timestamp()),
            "from": {"id": 1001, "is_bot": False},
            "chat": {"id": 1001, "type": "private"},
            "text": "How do my recent ferritin and sleep records look?",
        },
    }


def _add_verified_lab(
    session: Session,
    profile_id: UUID,
    name: str,
    value: Decimal,
    observed_at: datetime,
) -> None:
    document = Document(
        profile_id=profile_id,
        sha256=uuid4().hex + uuid4().hex,
        vault_path="test-only",
        media_type="application/pdf",
        document_type="lab",
        collected_date=observed_at.date(),
    )
    session.add(document)
    session.flush()
    session.add(
        DocumentPage(document_id=document.id, page_number=1, extraction_method="text")
    )
    session.flush()
    session.add(
        LabObservation(
            document_id=document.id,
            page_number=1,
            canonical_name=name,
            source_name=name,
            source_value=str(value),
            parsed_value=value,
            source_unit="ug/L",
            normalized_value=value,
            normalized_unit="ug/L",
            evidence_excerpt="test-only",
            confidence=Decimal(1),
            status=ReviewStatus.VERIFIED,
        )
    )


def _add_whoop_sleep(
    session: Session, profile_id: UUID, external_id: str, observed_at: datetime
) -> None:
    connection = register_authorized_connection(
        session, profile_id, "test", 1 if profile_id == PROFILE_ID else 2, ("read:sleep",)
    )
    payload: dict[str, object] = {
        "id": external_id,
        "cycle_id": 1,
        "user_id": 1 if profile_id == PROFILE_ID else 2,
        "start": observed_at.isoformat().replace("+00:00", "Z"),
        "score": {
            "stage_summary": {
                "total_light_sleep_time_milli": 14_400_000,
                "total_slow_wave_sleep_time_milli": 7_200_000,
                "total_rem_sleep_time_milli": 3_600_000,
            }
        },
    }
    store_normalized_record(
        session, connection, normalize_whoop("sleep", payload), payload, observed_at
    )
