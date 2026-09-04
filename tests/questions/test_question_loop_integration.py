"""Offline component coverage for the complete bound Telegram question path."""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from health_agent.db import session_scope
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
)
from health_agent.questions.openai import OpenAIResponsesResponder
from health_agent.questions.service import HealthQuestionApplicationService
from health_agent.telegram.messenger import TelegramMessenger
from health_agent.telegram.service import TelegramLongPoller, TelegramUpdateService
from health_agent.telegram.stores import SqliteTelegramState
from health_agent.telegram.types import (
    AttachmentProvenance,
    InboxReceipt,
    RemoteFile,
    TelegramIdentity,
)
from health_agent.whoop.normalize import normalize_whoop
from health_agent.whoop.repository import (
    register_authorized_connection,
    store_normalized_record,
)

BOT_ID = 701
PROFILE_ID = UUID("00000000-0000-0000-0000-000000000001")


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
