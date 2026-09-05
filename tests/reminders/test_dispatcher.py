from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import UUID

from sqlalchemy import Engine

from health_agent.db import session_scope
from health_agent.reminders.dispatcher import ReminderDispatcher
from health_agent.reminders.repository import ReminderRepository
from health_agent.telegram.messenger import TelegramMessenger
from health_agent.telegram.stores import SqliteTelegramState
from health_agent.telegram.types import TelegramGateway, TelegramIdentity

BOT_ID = 111
PROFILE_ID = UUID("00000000-0000-0000-0000-000000000001")
NOW = datetime(2026, 9, 5, 7, 0, tzinfo=UTC)


class FakeGateway:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    def send_message(self, chat_id: int, text: str) -> int:
        self.sent.append((chat_id, text))
        return len(self.sent)


def _messenger(path: Path, gateway: FakeGateway, profile_id: UUID = PROFILE_ID):
    state = SqliteTelegramState(path)
    state.register_bot(BOT_ID, "health_bot")
    state.bind_identity(BOT_ID, TelegramIdentity(101, profile_id, 101))
    return TelegramMessenger(BOT_ID, cast(TelegramGateway, gateway), state)


def _propose(engine: Engine, *, code: str = "safe-code-1"):
    with session_scope(engine) as session:
        return ReminderRepository(session).propose(
            profile_id=PROFILE_ID,
            title="Repeat ferritin test",
            reason="Doctor requested a repeat after treatment",
            source_type="doctor_note",
            source_reference="document:abc",
            due_at=NOW,
            timezone_name="Europe/Moscow",
            now=NOW,
            public_code=code,
        )


def test_proposal_then_confirmed_due_delivery_is_idempotent(
    clean_database: Engine, tmp_path: Path
) -> None:
    proposal = _propose(clean_database)
    gateway = FakeGateway()
    messenger = _messenger(tmp_path / "telegram.sqlite3", gateway)
    dispatcher = ReminderDispatcher(clean_database, messenger, clock=lambda: NOW)

    first = dispatcher.run()
    second = dispatcher.run()
    with session_scope(clean_database) as session:
        repository = ReminderRepository(session)
        assert repository.due_occurrences(NOW) == ()
        repository.confirm(PROFILE_ID, proposal.public_code, now=NOW)
    third = dispatcher.run()
    fourth = dispatcher.run()

    assert (first.proposals_sent, first.due_sent, first.failed) == (1, 0, 0)
    assert (second.proposals_sent, second.due_sent) == (0, 0)
    assert (third.proposals_sent, third.due_sent, third.failed) == (0, 1, 0)
    assert (fourth.proposals_sent, fourth.due_sent) == (0, 0)
    assert len(gateway.sent) == 2
    assert "/reminder_confirm safe-code-1" in gateway.sent[0][1]
    assert "/reminder_done safe-code-1" in gateway.sent[1][1]
    assert "Doctor requested a repeat" in gateway.sent[1][1]
    assert "document:abc" in gateway.sent[1][1]


def test_restart_after_send_before_database_ack_does_not_duplicate(
    clean_database: Engine, tmp_path: Path
) -> None:
    proposal = _propose(clean_database)
    with session_scope(clean_database) as session:
        repository = ReminderRepository(session)
        repository.mark_proposal_notified(PROFILE_ID, proposal.id, notified_at=NOW)
        repository.confirm(PROFILE_ID, proposal.public_code, now=NOW)

    class FailingAckRepository(ReminderRepository):
        def mark_due_delivered(self, *args, **kwargs):
            super().mark_due_delivered(*args, **kwargs)
            raise RuntimeError("simulated crash")

    gateway = FakeGateway()
    messenger = _messenger(tmp_path / "telegram.sqlite3", gateway)
    failed = ReminderDispatcher(
        clean_database,
        messenger,
        clock=lambda: NOW,
        repository_factory=FailingAckRepository,
    ).run()
    recovered = ReminderDispatcher(clean_database, messenger, clock=lambda: NOW).run()

    assert failed.failed == 1
    assert recovered.due_acknowledged == 1
    assert recovered.due_sent == 0
    assert len(gateway.sent) == 1
    with session_scope(clean_database) as session:
        assert (
            ReminderRepository(session)
            .get(PROFILE_ID, proposal.public_code)
            .delivered_at
            == NOW
        )


def test_missing_binding_for_one_profile_does_not_block_other_reminders(
    clean_database: Engine, tmp_path: Path
) -> None:
    from health_agent.models import Profile

    other = UUID("00000000-0000-0000-0000-000000000002")
    with session_scope(clean_database) as session:
        session.add(Profile(id=other, name="Other"))
        repository = ReminderRepository(session)
        first = repository.propose(
            profile_id=PROFILE_ID,
            title="First",
            reason="First reason",
            source_type="user",
            source_reference="telegram",
            due_at=NOW,
            timezone_name="UTC",
            now=NOW,
            public_code="unbound-code",
        )
        second = repository.propose(
            profile_id=other,
            title="Second",
            reason="Second reason",
            source_type="user",
            source_reference="telegram",
            due_at=NOW,
            timezone_name="UTC",
            now=NOW,
            public_code="bound-code",
        )
        repository.mark_proposal_notified(PROFILE_ID, first.id, notified_at=NOW)
        repository.mark_proposal_notified(other, second.id, notified_at=NOW)
        repository.confirm(PROFILE_ID, first.public_code, now=NOW)
        repository.confirm(other, second.public_code, now=NOW)

    gateway = FakeGateway()
    messenger = _messenger(tmp_path / "telegram.sqlite3", gateway, other)
    report = ReminderDispatcher(clean_database, messenger, clock=lambda: NOW).run()

    assert report.due_sent == 1
    assert report.failed == 1
    assert gateway.sent[0][0] == 101
    assert "Second" in gateway.sent[0][1]
